"""OpenRouteService provider (§9.4): parsing, derived modes, null pairs, fallback.

The sample responses in tests/fixtures/ follow the shapes in the ORS v2 API
reference. No network: `_post` is replaced with a stub.
"""

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.models.private import LatLng
from app.models.routing import Mode
from app.providers.cache import RecordReplayCache
from app.providers.costs import DRIVE_PARKING_MIN, drive_cost, rideshare_fare
from app.providers.real.ors import OrsRouting, parse_matrix
from app.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures"
MATRIX = json.loads((FIXTURES / "ors_matrix_sample.json").read_text())
DIRECTIONS = json.loads((FIXTURES / "ors_directions_sample.json").read_text())
NOW = datetime.fromisoformat("2026-10-03T18:00:00-04:00")
SETTINGS = Settings(_env_file=None, ors_api_key="test")


def by_key(estimates):
    return {(e.origin_pid, e.candidate_id, e.mode): e for e in estimates}


def test_parse_matrix_converts_units_and_skips_null_pairs() -> None:
    estimates = by_key(
        parse_matrix(
            MATRIX,
            ["p1", "p2"],
            ["koko", "bagels", "far"],
            {"p1": [Mode.WALK], "p2": [Mode.WALK]},
            SETTINGS,
        )
    )
    walk = estimates[("p1", "koko", Mode.WALK)]
    assert walk.duration_min == 10.0  # 600 s
    assert walk.distance_mi == 0.5
    assert walk.walk_min == 10.0
    assert walk.fare_usd.value == 0
    assert walk.source == "ors"
    # durations[0][2] is null: no route, so no estimate (never faked).
    assert ("p1", "far", Mode.WALK) not in estimates
    assert ("p2", "far", Mode.WALK) in estimates
    assert len(estimates) == 5


def test_drive_and_rideshare_derived_from_one_driving_result() -> None:
    estimates = by_key(
        parse_matrix(MATRIX, ["p1"], ["koko"], {"p1": [Mode.DRIVE, Mode.RIDESHARE]}, SETTINGS)
    )
    drive = estimates[("p1", "koko", Mode.DRIVE)]
    ride = estimates[("p1", "koko", Mode.RIDESHARE)]
    assert drive.duration_min == 10.0 + DRIVE_PARKING_MIN
    assert drive.fare_usd.value == drive_cost(0.5, SETTINGS)
    assert drive.walk_min == 0
    assert ride.duration_min == 10.0 + SETTINGS.rideshare_pickup_wait_min
    assert ride.fare_usd.value == rideshare_fare(0.5, 10.0, SETTINGS)
    assert ride.fare_usd.status == "estimated"
    assert ride.fare_usd.source == "formula"


def test_rideshare_fare_formula_and_minimum() -> None:
    # 3 mi, 9 min: 2.50 + 2.50 + 1.20×3 + 0.30×9 = 11.30
    assert rideshare_fare(3.0, 9.0, SETTINGS) == Decimal("11.30")
    # Short hop is lifted to the $8 minimum fare.
    assert rideshare_fare(0.2, 1.0, SETTINGS) == Decimal("8.00")


class StubOrs(OrsRouting):
    def __init__(self, responses: dict[str, dict] | None = None, fail: bool = False) -> None:
        super().__init__(SETTINGS, RecordReplayCache("off"))
        self.responses = responses or {}
        self.fail = fail
        self.posts: list[tuple[str, dict]] = []

    async def _post(self, method: str, path: str, body: dict):
        self.posts.append((path, body))
        if self.fail:
            raise ConnectionError("ors down")
        return self.responses[path]


ORIGINS = {"p1": LatLng(lat=42.4430, lng=-76.4850), "p2": LatLng(lat=42.4560, lng=-76.4790)}
DESTS = {
    "bagels": LatLng(lat=42.4390, lng=-76.4970),
    "far": LatLng(lat=42.4000, lng=-76.5200),
    "koko": LatLng(lat=42.442, lng=-76.4852),
}


async def test_matrix_one_call_per_needed_profile_in_lng_lat_order() -> None:
    ors = StubOrs({"/v2/matrix/driving-car": MATRIX})
    modes = {"p1": {Mode.DRIVE, Mode.RIDESHARE}, "p2": {Mode.RIDESHARE}}
    estimates = await ors.matrix(ORIGINS, DESTS, modes, NOW)

    assert [path for path, _ in ors.posts] == ["/v2/matrix/driving-car"]  # no walk/bike call
    body = ors.posts[0][1]
    assert body["locations"][0] == [-76.4850, 42.4430]  # [lng, lat]
    assert body["sources"] == ["0", "1"]
    assert body["destinations"] == ["2", "3", "4"]
    assert body["metrics"] == ["duration", "distance"]
    assert body["units"] == "mi"
    modes_seen = {(e.origin_pid, e.mode) for e in estimates}
    assert modes_seen == {("p1", Mode.DRIVE), ("p1", Mode.RIDESHARE), ("p2", Mode.RIDESHARE)}


async def test_matrix_request_is_independent_of_pid_labels() -> None:
    a, b = (
        StubOrs({"/v2/matrix/foot-walking": MATRIX}),
        StubOrs({"/v2/matrix/foot-walking": MATRIX}),
    )
    walk = {Mode.WALK}
    await a.matrix(ORIGINS, DESTS, {"p1": walk, "p2": walk}, NOW)
    swapped = {"p1": ORIGINS["p2"], "p2": ORIGINS["p1"]}
    await b.matrix(swapped, dict(reversed(DESTS.items())), {"p1": walk, "p2": walk}, NOW)
    assert a.posts[0][1] == b.posts[0][1]  # same request → same cache key


async def test_matrix_falls_back_to_mock_when_ors_is_down() -> None:
    estimates = await StubOrs(fail=True).matrix(ORIGINS, DESTS, {"p1": {Mode.WALK}}, NOW)
    assert len(estimates) == 3
    assert {e.source for e in estimates} == {"mock"}


async def test_route_parses_turn_by_turn_steps() -> None:
    ors = StubOrs({"/v2/directions/foot-walking": DIRECTIONS})
    detail = await ors.route(ORIGINS["p1"], DESTS["koko"], Mode.WALK, depart_at=NOW)

    assert ors.posts[0][1]["coordinates"] == [[-76.4850, 42.4430], [-76.4852, 42.442]]
    assert ors.posts[0][1]["instructions"] is True
    assert [s.instruction for s in detail.steps][:2] == [
        "Head south on Ho Plaza",
        "Turn left onto College Avenue",
    ]
    assert detail.steps[1].duration_min == 7.0
    assert detail.duration_min == 9.0
    assert detail.arrive_at - detail.depart_at == (NOW.replace(minute=9) - NOW)
    assert detail.polyline == "a~l~Fjk~uOwHJy@P"


async def test_rideshare_route_is_one_request_a_ride_step() -> None:
    ors = StubOrs({"/v2/directions/driving-car": DIRECTIONS})
    detail = await ors.route(ORIGINS["p1"], DESTS["koko"], Mode.RIDESHARE, depart_at=NOW)
    assert len(detail.steps) == 1
    assert detail.steps[0].instruction.startswith("Request a ride")
    assert detail.duration_min == 9.0 + SETTINGS.rideshare_pickup_wait_min


async def test_route_without_a_route_raises_for_delivery_fallback() -> None:
    with pytest.raises(ConnectionError):
        await StubOrs(fail=True).route(ORIGINS["p1"], DESTS["koko"], Mode.WALK, depart_at=NOW)
