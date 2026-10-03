"""Find My shared location during onboarding (sim), plus the Photon provider calls."""

import json
import logging
import re
import sqlite3
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
    """Every reply to this message (one per line)."""
    before = len(client.get(f"/sim/outbox/{HANDLE}").json())
    client.post("/sim/message", json={"sender_handle": HANDLE, "text": text}).raise_for_status()
    texts = [m["text"] for m in client.get(f"/sim/outbox/{HANDLE}").json()[before:]]
    return "\n".join(texts)


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
    assert dm(client, "yes").startswith("You're set!")


def test_far_away_share_gets_a_generic_label(client) -> None:
    to_location_step(client)
    sim(client).shared[HANDLE] = FAR_AWAY
    assert dm(client, "shared") == "Got it: your shared location. Right? (yes/no)"


def test_command_during_location_step_is_not_geocoded(client) -> None:
    to_location_step(client)
    assert dm(client, "@plan").startswith("Almost done! First, where are you starting from?")
    assert dm(client, "olin") == "Got it: Olin Library. Right? (yes/no)"


@pytest.mark.parametrize("reply", ["ok", "I shared my live location", "sharing now!", "done."])
def test_casual_share_replies_use_the_shared_location(client, reply) -> None:
    to_location_step(client)
    sim(client).shared[HANDLE] = COLLEGETOWN
    assert dm(client, reply) == "Got it: near Collegetown. Right? (yes/no)"


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
    for text in ["olin", "yes"]:
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


# --- already sharing: used at setup, refreshed at @go ---------------------------------


def test_already_sharing_skips_the_card(client) -> None:
    for text in ["start", "Maya", "MAYA1"]:
        dm(client, text)
    sim(client).shared[HANDLE] = COLLEGETOWN
    assert dm(client, "yes") == "Got it: near Collegetown. Right? (yes/no)"
    assert sim(client).location_requests == []  # nothing to tap: they already share
    assert dm(client, "yes").startswith("You're set!")


def stored_label(client: TestClient, handle: str) -> str | None:
    url = client.app.state.deps.settings.database_url
    db = sqlite3.connect(url.split("///", 1)[1])
    row = db.execute(
        "select p.origin_label from private_profiles p join users u on u.id = p.user_id "
        "where u.handle = ?",
        (handle,),
    ).fetchone()
    return row[0] if row else None


def test_go_refreshes_origins_from_live_shares(client, caplog) -> None:
    caplog.set_level(logging.INFO)
    sam = "+16075550102"
    for text in ["start", "Maya", "MAYA1", "yes", "olin", "yes"]:
        dm(client, text)
    for text in ["start", "Sam", "SAM1", "yes", "collegetown", "yes"]:
        client.post("/sim/message", json={"sender_handle": sam, "text": text})
    assert stored_label(client, HANDLE) == "Olin Library"

    started = dm(client, "@plan")
    code = re.search(r"join ([A-Z0-9]{4})", started).group(1)
    client.post("/sim/message", json={"sender_handle": sam, "text": f"join {code}"})
    for text in ["bike", "same"]:
        dm(client, text)
    for text in ["neither", "same"]:
        client.post("/sim/message", json={"sender_handle": sam, "text": text})
    sim(client).shared[HANDLE] = COLLEGETOWN  # Maya walked to Collegetown since setup
    dm(client, "@go")

    assert stored_label(client, HANDLE) == "near Collegetown"  # refreshed
    assert stored_label(client, sam) == "Collegetown"  # not sharing: onboarding origin kept
    assert "shared_origins_refreshed refreshed=1 members=2" in caplog.text


# --- per-plan questions, one at a time: how, then where from ----------------------------


def all_texts(client: TestClient, handle: str) -> list[str]:
    return [m["text"] for m in client.get(f"/sim/outbox/{handle}").json()]


def setup_maya_and_sam(client: TestClient) -> str:
    sam = "+16075550102"
    for text in ["start", "Maya", "MAYA1", "yes", "olin", "yes"]:
        dm(client, text)
    for text in ["start", "Sam", "SAM1", "yes", "collegetown", "yes"]:
        client.post("/sim/message", json={"sender_handle": sam, "text": text})
    return sam


def test_where_from_is_asked_after_how_unless_sharing(client) -> None:
    sam = setup_maya_and_sam(client)
    sim(client).shared[HANDLE] = COLLEGETOWN  # Maya shares; Sam doesn't
    sim(client).location_requests.clear()

    started = dm(client, "@plan")
    assert started.endswith(
        'Reply car, bike, both, or neither. (Add "no rideshare" if you\'d rather not take one.)'
    )
    assert "Where are you starting from" not in started  # one question at a time
    assert dm(client, "bike").startswith("Got it. I'll plan from your live location")

    code = re.search(r"join ([A-Z0-9]{4})", started).group(1)
    client.post("/sim/message", json={"sender_handle": sam, "text": f"join {code}"})
    assert "Where are you starting from" not in all_texts(client, sam)[-1]
    client.post("/sim/message", json={"sender_handle": sam, "text": "neither"})
    assert all_texts(client, sam)[-1].startswith("Where are you starting from this time?")
    assert sim(client).location_requests == [sam]  # the Find My card, with the question
    client.post("/sim/message", json={"sender_handle": sam, "text": "same"})
    assert all_texts(client, sam)[-1] == "Got it. Now tell me what you're in the mood for!"
    assert "Looking at options" in dm(client, "@go")


def test_typed_place_this_plan_beats_live_location_at_go(client, caplog) -> None:
    caplog.set_level(logging.INFO)
    sam = setup_maya_and_sam(client)
    sim(client).shared[HANDLE] = COLLEGETOWN  # Maya is sharing...
    code = re.search(r"join ([A-Z0-9]{4})", dm(client, "@plan")).group(1)
    dm(client, "bike")
    dm(client, "location")  # ...but types where she'll start from instead
    assert dm(client, "olin") == "Got it: Olin Library. Right? (yes/no)"
    assert dm(client, "yes") == "Got it. Now tell me what you're in the mood for!"
    client.post("/sim/message", json={"sender_handle": sam, "text": f"join {code}"})
    for text in ["neither", "same"]:
        client.post("/sim/message", json={"sender_handle": sam, "text": text})
    dm(client, "@go")
    assert stored_label(client, HANDLE) == "Olin Library"  # typed wins this plan
    assert "kept_typed=1" in caplog.text

    # Next plan: the typed place no longer pins her; live location is used again.
    dm(client, "@cancel")
    code = re.search(r"join ([A-Z0-9]{4})", dm(client, "@plan")).group(1)
    dm(client, "bike")
    client.post("/sim/message", json={"sender_handle": sam, "text": f"join {code}"})
    for text in ["neither", "same"]:
        client.post("/sim/message", json={"sender_handle": sam, "text": text})
    dm(client, "@go")
    assert stored_label(client, HANDLE) == "near Collegetown"


def test_go_waits_for_where_from_and_commands_still_work_mid_question(client) -> None:
    sam = setup_maya_and_sam(client)
    code = re.search(r"join ([A-Z0-9]{4})", dm(client, "@plan")).group(1)
    for text in ["bike", "same"]:
        dm(client, text)
    client.post("/sim/message", json={"sender_handle": sam, "text": f"join {code}"})
    client.post("/sim/message", json={"sender_handle": sam, "text": "neither"})  # not "where"
    assert dm(client, "@go") == "Still waiting on Sam to answer my questions."
    assert all_texts(client, sam)[-1].startswith("Where are you starting from this time?")
    client.post("/sim/message", json={"sender_handle": sam, "text": "@cancel"})
    assert all_texts(client, sam)[-1].startswith("Plan cancelled.")


async def test_photon_old_location_counts_as_not_sharing() -> None:
    from datetime import UTC, datetime, timedelta

    def at(minutes_ago: int) -> str:
        return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()

    fresh = ok({"lat": 42.44, "lng": -76.48, "at": at(5)})
    old = ok({"lat": 42.44, "lng": -76.48, "at": at(180)})
    no_time = ok({"lat": 42.44, "lng": -76.48, "at": None})
    assert await StubPhoton({"/location": fresh}).shared_location("+1") is not None
    assert await StubPhoton({"/location": old}).shared_location("+1") is None
    assert await StubPhoton({"/location": no_time}).shared_location("+1") is not None
    legacy = ok({"lat": 42.44, "lng": -76.48, "at": None, "type": "legacy"})
    assert await StubPhoton({"/location": legacy}).shared_location("+1") is None


# --- travel modes: asked per plan, never kept ------------------------------------------


def stored_modes(client: TestClient, handle: str) -> str | None:
    db = sqlite3.connect(client.app.state.deps.settings.database_url.split("///", 1)[1])
    row = db.execute(
        "select p.modes_json from private_profiles p join users u on u.id = p.user_id "
        "where u.handle = ?",
        (handle,),
    ).fetchone()
    return row[0]


def test_modes_are_asked_every_plan_and_go_waits_for_answers(client) -> None:
    sam = "+16075550102"
    for text in ["start", "Maya", "MAYA1", "yes", "olin", "yes"]:
        dm(client, text)
    for text in ["start", "Sam", "SAM1", "yes", "collegetown", "yes"]:
        client.post("/sim/message", json={"sender_handle": sam, "text": text})
    assert stored_modes(client, HANDLE) is None  # setup no longer asks

    started = dm(client, "@plan")
    assert "How are you getting there this time?" in started
    code = re.search(r"join ([A-Z0-9]{4})", started).group(1)
    client.post("/sim/message", json={"sender_handle": sam, "text": f"join {code}"})
    assert dm(client, "I'm driving").startswith("Where are you starting from this time?")
    assert dm(client, "same") == "Got it. Now tell me what you're in the mood for!"

    # Sam hasn't answered: @go waits and asks him again.
    assert dm(client, "@go") == "Still waiting on Sam to answer my questions."
    assert all_texts(client, sam)[-1].startswith("How are you getting there this time?")
    for text in ["walking", "same"]:
        client.post("/sim/message", json={"sender_handle": sam, "text": text})
    assert "Looking at options" in dm(client, "@go")

    # Next plan: last time's answer is forgotten and asked again.
    dm(client, "@cancel")
    assert "How are you getting there this time?" in dm(client, "@plan")
    assert stored_modes(client, HANDLE) is None


def test_cancel_while_asked_for_modes_does_not_strand_you(client) -> None:
    for text in ["start", "Maya", "MAYA1", "yes", "olin", "yes"]:
        dm(client, text)
    dm(client, "@plan")
    assert dm(client, "@cancel").startswith("Plan cancelled.")
    assert "join <code>" in dm(client, "help")  # back to normal, not "Reply car, bike..."
