"""Google Routes provider: traffic driving from Google, walk/bike from ORS, fallbacks.

Sample responses follow the Routes API v2 reference (computeRouteMatrix returns a
JSON array of elements; computeRoutes returns routes[].legs[].steps[]). No network.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.candidates import Uncertain
from app.models.private import LatLng
from app.models.routing import Mode, RouteDetail, RouteEstimate
from app.providers.cache import RecordReplayCache
from app.providers.costs import DRIVE_PARKING_MIN
from app.providers.real.google_routes import (
    GoogleRoutes,
    departure_time,
    parse_matrix,
    parse_route,
)
from app.settings import Settings

SETTINGS = Settings(_env_file=None, google_places_api_key="k", ors_api_key="o")
DEPART = datetime.fromisoformat("2026-10-04T09:45:00-04:00")
A = LatLng(lat=42.44, lng=-76.48)
B = LatLng(lat=42.45, lng=-76.49)
VENUE = LatLng(lat=42.43, lng=-76.50)

# 2 origins × 2 venues. Index 0 / 0 m are omitted like the real API does.
MATRIX = [
    {
        "destinationIndex": 1,
        "condition": "ROUTE_EXISTS",
        "distanceMeters": 1609,
        "duration": "600s",
    },
    {"originIndex": 1, "condition": "ROUTE_EXISTS", "distanceMeters": 3219, "duration": "900s"},
    {"condition": "ROUTE_EXISTS", "distanceMeters": 805, "duration": "300s", "status": {}},
    {"originIndex": 1, "destinationIndex": 1, "condition": "ROUTE_NOT_FOUND", "status": {}},
]
ROUTE = {
    "routes": [
        {
            "distanceMeters": 3219,
            "duration": "720s",
            "polyline": {"encodedPolyline": "abc"},
            "legs": [
                {
                    "steps": [
                        {
                            "distanceMeters": 1609,
                            "staticDuration": "300s",
                            "navigationInstruction": {"instructions": "Head north on College Ave"},
                        },
                        {"distanceMeters": 10, "staticDuration": "5s"},  # no instruction
                        {
                            "distanceMeters": 1610,
                            "staticDuration": "360s",
                            "navigationInstruction": {
                                "instructions": "Turn left onto State St\nDestination on the right"
                            },
                        },
                    ]
                }
            ],
        }
    ]
}


def by_key(estimates):
    return {(e.origin_pid, e.candidate_id, e.mode): e for e in estimates}


def test_parse_matrix_units_indices_and_missing_routes() -> None:
    modes = {"p1": [Mode.DRIVE, Mode.RIDESHARE], "p2": [Mode.DRIVE]}
    est = by_key(parse_matrix(MATRIX, ["p1", "p2"], ["v1", "v2"], modes, SETTINGS))
    drive = est[("p1", "v2", Mode.DRIVE)]
    assert drive.duration_min == 10.0 + DRIVE_PARKING_MIN
    assert drive.distance_mi == 1.0
    assert drive.source == "google"
    assert est[("p1", "v1", Mode.DRIVE)].duration_min == 5.0 + DRIVE_PARKING_MIN  # indices 0,0
    ride = est[("p1", "v2", Mode.RIDESHARE)]
    assert ride.duration_min == 10.0 + SETTINGS.rideshare_pickup_wait_min
    assert ride.fare_usd.value > 0
    assert ("p2", "v2", Mode.DRIVE) not in est  # ROUTE_NOT_FOUND: never faked
    assert len(est) == 5


def test_parse_route_steps_and_polyline() -> None:
    base_min, miles, steps, polyline = parse_route(ROUTE)
    assert base_min == 12.0 and miles == pytest.approx(2.0, abs=0.01)
    assert len(steps) == 3 and polyline == "abc"
    with pytest.raises(LookupError):
        parse_route({})


def test_departure_time_only_in_the_future() -> None:
    now = datetime(2026, 10, 4, 13, 0, tzinfo=UTC)
    assert departure_time(now + timedelta(minutes=15), now) == "2026-10-04T13:15:00Z"
    assert departure_time(now + timedelta(seconds=30), now) is None  # too close to now
    assert departure_time(now - timedelta(minutes=5), now) is None
    assert departure_time(None, now) is None


# --- provider behaviour (Google and ORS stubbed) --------------------------------------


def estimate(pid: str, cid: str, mode: Mode, source: str = "ors") -> RouteEstimate:
    return RouteEstimate(
        origin_pid=pid,
        candidate_id=cid,
        mode=mode,
        duration_min=1,
        distance_mi=1,
        walk_min=0,
        fare_usd=Uncertain[Decimal](value=Decimal(0), status="known", source="formula"),
        source=source,
    )


class StubOrs:
    def __init__(self) -> None:
        self.matrix_modes: list[dict] = []
        self.routes: list[Mode] = []

    async def matrix(self, origins, destinations, modes, depart_at):
        self.matrix_modes.append(modes)
        return [estimate(p, c, m) for p, ms in modes.items() for c in destinations for m in ms]

    async def route(self, origin, destination, mode, arrive_by=None, depart_at=None):
        self.routes.append(mode)
        e = estimate("", "", mode)
        return RouteDetail(**e.model_dump(), depart_at=DEPART, arrive_at=DEPART, steps=[])


class StubGoogle(GoogleRoutes):
    def __init__(self, response) -> None:
        super().__init__(SETTINGS, RecordReplayCache("off"))
        self.ors = StubOrs()
        self.response = response
        self.calls: list[tuple[str, dict, datetime | None]] = []

    async def _post(self, method, url, field_mask, body, depart_at):
        self.calls.append((method, body, depart_at))
        # A dict of travelMode → response lets one mode fail while others work.
        response = self.response
        if isinstance(response, dict) and body["travelMode"] in response:
            response = response[body["travelMode"]]
        if isinstance(response, Exception):
            raise response
        return response


ORIGINS = {"p1": A, "p2": B}
VENUES = {"v1": VENUE, "v2": B}


async def test_one_google_call_per_travel_mode_traffic_only_for_driving() -> None:
    g = StubGoogle(MATRIX)
    modes = {"p1": {Mode.WALK, Mode.RIDESHARE}, "p2": {Mode.BIKE, Mode.DRIVE}}
    est = await g.matrix(ORIGINS, VENUES, modes, DEPART)

    assert g.ors.matrix_modes == []  # ORS only as a fallback
    calls = {body["travelMode"]: (body, when) for _, body, when in g.calls}
    assert sorted(calls) == ["BICYCLE", "DRIVE", "WALK"]
    drive, drive_when = calls["DRIVE"]
    assert drive["routingPreference"] == "TRAFFIC_AWARE" and drive_when == DEPART
    assert len(drive["origins"]) == 2  # p1 rideshare + p2 drive share one call
    for travel_mode in ("WALK", "BICYCLE"):
        body, when = calls[travel_mode]
        assert "routingPreference" not in body and when is None  # no time, stable cache
        assert len(body["origins"]) == 1
    assert "departureTime" not in drive  # only the live request gets the exact time
    assert {e.source for e in est} == {"google"}
    assert {e.mode for e in est} == set(Mode)


async def test_failed_mode_falls_back_to_ors_others_stay_on_google() -> None:
    g = StubGoogle(
        {"BICYCLE": ConnectionError("no bike coverage"), "DRIVE": MATRIX, "WALK": MATRIX}
    )
    modes = {"p1": {Mode.WALK, Mode.DRIVE}, "p2": {Mode.BIKE}}
    est = await g.matrix(ORIGINS, VENUES, modes, DEPART)
    assert g.ors.matrix_modes == [{"p2": {Mode.BIKE}}]
    sources = {(e.mode, e.source) for e in est}
    assert sources == {(Mode.BIKE, "ors"), (Mode.WALK, "google"), (Mode.DRIVE, "google")}


async def test_google_down_everything_on_ors() -> None:
    g = StubGoogle(ConnectionError("down"))
    modes = {"p1": {Mode.WALK, Mode.DRIVE}, "p2": {Mode.RIDESHARE}}
    est = await g.matrix(ORIGINS, VENUES, modes, DEPART)
    assert {e.source for e in est} == {"ors"}
    assert ("p2", "v1", Mode.RIDESHARE) in by_key(est)
    assert ("p1", "v2", Mode.WALK) in by_key(est)


async def test_drive_route_uses_google_steps() -> None:
    g = StubGoogle(ROUTE)
    detail = await g.route(A, VENUE, Mode.DRIVE, depart_at=DEPART)
    assert detail.source == "google"
    assert detail.duration_min == 12.0 + DRIVE_PARKING_MIN
    assert [s.instruction for s in detail.steps] == [
        "Head north on College Ave",
        "Turn left onto State St Destination on the right",
    ]
    assert {s.mode for s in detail.steps} == {"drive"}
    assert detail.arrive_at - detail.depart_at == timedelta(minutes=detail.duration_min)
    [(method, body, when)] = g.calls
    assert method == "route" and body["routingPreference"] == "TRAFFIC_AWARE"
    assert when == DEPART
    assert body["origin"] == {"location": {"latLng": {"latitude": A.lat, "longitude": A.lng}}}


async def test_walk_route_on_google_without_traffic() -> None:
    g = StubGoogle(ROUTE)
    detail = await g.route(A, VENUE, Mode.WALK, depart_at=DEPART)
    [(_, body, when)] = g.calls
    assert body["travelMode"] == "WALK" and "routingPreference" not in body and when is None
    assert detail.source == "google" and detail.walk_min == detail.duration_min == 12.0
    assert {s.mode for s in detail.steps} == {"walk"}
    assert g.ors.routes == []


async def test_route_failure_uses_ors() -> None:
    failing = StubGoogle(ConnectionError("down"))
    detail = await failing.route(A, VENUE, Mode.RIDESHARE, depart_at=DEPART)
    assert failing.ors.routes == [Mode.RIDESHARE] and detail.source == "ors"


async def test_exact_time_is_sent_live_but_cache_key_is_rounded(monkeypatch) -> None:
    sent = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return MATRIX

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        async def post(self, url, json, headers):
            sent.append((json, headers))
            return FakeResponse()

    monkeypatch.setattr("app.providers.real.google_routes.httpx.AsyncClient", FakeClient)
    future = datetime.now(UTC) + timedelta(hours=1)
    g = GoogleRoutes(SETTINGS, RecordReplayCache("off"))
    await g._post("matrix", "u", "mask", {"travelMode": "DRIVE"}, future)
    body, headers = sent[0]
    assert body["departureTime"].endswith("Z")
    assert headers == {"X-Goog-Api-Key": "k", "X-Goog-FieldMask": "mask"}  # Places key reused
