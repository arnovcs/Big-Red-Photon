"""Find My shared location during onboarding (sim), plus the Photon provider calls."""

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.deps import build_deps
from app.main import create_app
from app.models.private import LatLng
from app.providers.mock.places import load_demo_locations, near_label
from app.providers.real.photon import PhotonMessaging
from app.settings import Settings

HANDLE = "+16075550101"
LOCATIONS = {loc["name"]: loc for loc in load_demo_locations()}
COLLEGETOWN = LatLng(lat=LOCATIONS["Collegetown"]["lat"], lng=LOCATIONS["Collegetown"]["lng"])
FAR_AWAY = LatLng(lat=40.7128, lng=-74.0060)


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        _env_file=None,
        provider_messaging="sim",
        nessie_api_key="",
        venues_path="tests/fixtures/venues_test.json",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'loc.db'}",
    )
    app = create_app(build_deps(settings))
    with TestClient(app) as c:
        yield c


def dm(client: TestClient, text: str) -> str:
    client.post("/sim/message", json={"sender_handle": HANDLE, "text": text}).raise_for_status()
    return client.get(f"/sim/outbox/{HANDLE}").json()[-1]["text"]


def to_location_step(client: TestClient) -> str:
    for text in ["start", "Maya", "MAYA1"]:
        dm(client, text)
    return dm(client, "yes")


def sim(client: TestClient):
    return client.app.state.deps.messaging


def test_location_step_sends_the_find_my_card(client) -> None:
    ask = to_location_step(client)
    assert ask.startswith("Where are you starting from?")
    assert "Share your location" in ask
    assert sim(client).location_requests == [HANDLE]


def test_done_without_sharing_explains(client) -> None:
    to_location_step(client)
    assert dm(client, "done").startswith("I can't see your location yet")


def test_done_after_sharing_uses_it_and_names_the_nearest_landmark(client) -> None:
    to_location_step(client)
    sim(client).shared[HANDLE] = COLLEGETOWN
    assert dm(client, "Done!") == "Got it: near Collegetown. Right? (yes/no)"
    assert "car or a bike" in dm(client, "yes")


def test_far_away_share_gets_a_generic_label(client) -> None:
    to_location_step(client)
    sim(client).shared[HANDLE] = FAR_AWAY
    assert dm(client, "shared") == "Got it: your shared location. Right? (yes/no)"


def test_typed_landmark_still_wins(client) -> None:
    to_location_step(client)
    sim(client).shared[HANDLE] = FAR_AWAY
    assert dm(client, "olin") == "Got it: Olin Library. Right? (yes/no)"


def test_unknown_text_falls_back_to_shared_location(client) -> None:
    to_location_step(client)
    assert "couldn't find that" in dm(client, "my dorm")
    sim(client).shared[HANDLE] = COLLEGETOWN
    assert dm(client, "my dorm") == "Got it: near Collegetown. Right? (yes/no)"


def test_location_command_resends_the_card(client) -> None:
    to_location_step(client)
    for text in ["olin", "yes", "neither"]:
        dm(client, text)
    assert dm(client, "location").startswith("Where are you starting from?")
    assert sim(client).location_requests == [HANDLE, HANDLE]


def test_near_label_never_contains_coordinates() -> None:
    locations = load_demo_locations()
    assert near_label(COLLEGETOWN, locations) == "near Collegetown"
    assert near_label(FAR_AWAY, locations) == "your shared location"
    assert not any(ch.isdigit() for ch in near_label(FAR_AWAY, locations))


# --- Photon provider (bridge stubbed) -------------------------------------------------


class StubPhoton(PhotonMessaging):
    def __init__(self, responses: dict[str, httpx.Response | Exception]) -> None:
        super().__init__(Settings(_env_file=None))
        self.responses = responses

    async def _post(self, path: str, body: dict) -> httpx.Response:
        result = self.responses[path]
        if isinstance(result, Exception):
            raise result
        return result


def ok(body: dict) -> httpx.Response:
    return httpx.Response(200, content=json.dumps(body))


async def test_photon_request_location() -> None:
    assert await StubPhoton({"/request_location": ok({"status": "sent"})}).request_location("+1")
    failed = StubPhoton({"/request_location": httpx.Response(502, content=b"{}")})
    assert await failed.request_location("+1") is False
    down = StubPhoton({"/request_location": httpx.ConnectError("down")})
    assert await down.request_location("+1") is False


async def test_photon_shared_location() -> None:
    found = StubPhoton({"/location": ok({"lat": 42.44, "lng": -76.48, "label": None})})
    assert await found.shared_location("+1") == LatLng(lat=42.44, lng=-76.48)
    missing = StubPhoton({"/location": httpx.Response(404, content=b"{}")})
    assert await missing.shared_location("+1") is None
    down = StubPhoton({"/location": httpx.ConnectError("down")})
    assert await down.shared_location("+1") is None


def test_bridge_never_logs_coordinates() -> None:
    bridge = (Path(__file__).resolve().parents[1] / "bridge" / "src" / "index.ts").read_text()
    for line in bridge.splitlines():
        if "console.log" in line and "location" in line:
            assert "latitude" not in line and "lat" not in line.split("location", 1)[1]
