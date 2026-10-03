"""Typed locations resolve via Google; every member gets their own route; no local JSON.

1. "Young Boys Barbershop" resolves to its real Ithaca address (live Google).
2. Nonsense asks again: never a default (live Google + the conversation).
3. Confirmation: yes saves (with the place id), no re-asks, a new name is a new lookup.
4. "cheap food near Collegetown" returns real Google venues (live Google).
5. Three people (North Campus, Collegetown, the Commons) each get their own Routes origin
   and their own directions to the same venue.
6. Nothing reads the old local places JSON any more.

Live tests run only when GOOGLE_PLACES_API_KEY is set (.env or environment).
"""

import re
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient

from app.deps import build_deps
from app.main import create_app
from app.models.private import LatLng
from app.providers.cache import RecordReplayCache
from app.providers.real.google_places import GooglePlaces
from app.settings import Settings
from tests.places_stub import StubPlaces

ROOT = Path(__file__).resolve().parents[1]
ITHACA = LatLng(lat=42.44, lng=-76.50)
COLLEGETOWN = LatLng(lat=42.4422, lng=-76.4852)
GOOGLE_KEY = dotenv_values(ROOT / ".env").get("GOOGLE_PLACES_API_KEY") or ""
live = pytest.mark.skipif(not GOOGLE_KEY, reason="needs GOOGLE_PLACES_API_KEY")


def google() -> GooglePlaces:
    return GooglePlaces(
        Settings(_env_file=None, google_places_api_key=GOOGLE_KEY), RecordReplayCache("off")
    )


# --- 1, 2, 4: live Google -----------------------------------------------------------------


@live
async def test_1_young_boys_barbershop_resolves_to_its_real_address() -> None:
    place = await google().geocode("Young Boys Barbershop", ITHACA)
    assert place is not None
    assert "Dryden" in place.address and "Ithaca" in place.address
    assert "Olin" not in place.label
    assert place.place_id
    assert abs(place.location.lat - 42.44) < 0.05 and abs(place.location.lng + 76.49) < 0.05


@live
async def test_2_nonsense_finds_nothing() -> None:
    assert await google().geocode("asdkjh qwe zzz", ITHACA) is None


@live
async def test_4_cheap_food_near_collegetown_returns_real_venues() -> None:
    g = google()
    by_text = await g.text_search("cheap food near Collegetown", ITHACA)
    nearby = await g.search_nearby(COLLEGETOWN, 1500, ["food"], datetime.now().astimezone())
    for results in (by_text, nearby):
        assert results, "Google returned no venues"
        assert all(c.source == "google" and c.candidate_id.startswith("google:") for c in results)
        assert all(abs(c.location.lat - 42.44) < 0.1 for c in results)  # in Ithaca


# --- 2, 3: the conversation (Google stubbed) -----------------------------------------------


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        _env_file=None,
        provider_messaging="sim",
        nessie_api_key="",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}",
        poll_timeout_sec=3600,
    )
    app = create_app(build_deps(settings, places=StubPlaces()))
    with TestClient(app) as c:
        yield c


def dm(client: TestClient, handle: str, text: str) -> str:
    before = len(client.get(f"/sim/outbox/{handle}").json())
    client.post("/sim/message", json={"sender_handle": handle, "text": text}).raise_for_status()
    return "\n".join(m["text"] for m in client.get(f"/sim/outbox/{handle}").json()[before:])


def stored(client: TestClient, handle: str) -> tuple | None:
    url = client.app.state.deps.settings.database_url.split("///", 1)[1]
    return (
        sqlite3.connect(url)
        .execute(
            "select p.origin_label, p.origin_place_id from private_profiles p "
            "join users u on u.id = p.user_id where u.handle = ?",
            (handle,),
        )
        .fetchone()
    )


def to_location_step(client: TestClient, handle: str, name: str, code: str) -> None:
    for text in ["start", name, code]:
        dm(client, handle, text)
    assert dm(client, handle, "yes").startswith("where are you starting from?")


H1 = "+16075550101"


def test_2_nonsense_asks_again_and_never_defaults(client) -> None:
    to_location_step(client, H1, "Maya", "MAYA1")
    client.app.state.deps.messaging.shared[H1] = COLLEGETOWN  # sharing doesn't matter here
    for _ in range(3):
        reply = dm(client, H1, "asdkjh qwe zzz")
        assert reply.startswith("couldn't find that one")
        assert "Olin" not in reply.split("like")[0]  # no default place is offered as theirs
    assert stored(client, H1) == (None, None)


def test_3_yes_saves_no_reasks_new_name_looks_up_again(client) -> None:
    places = client.app.state.deps.places
    to_location_step(client, H1, "Maya", "MAYA1")
    # Not an obvious match ("north campus" → Robert Purcell): asked once.
    assert dm(client, H1, "meet me at north campus") == ("Robert Purcell Community Center, right?")
    assert dm(client, H1, "no").startswith("where are you starting from?")
    assert stored(client, H1) == (None, None)  # "no" clears it
    # A different place name is a fresh lookup; this one is obvious, so no "right?".
    reply = dm(client, H1, "olin")
    assert reply.startswith("got it, Olin Library 📍") and "you're all set!" in reply
    assert places.lookups == ["meet me at north campus", "olin"]
    assert stored(client, H1) == ("Olin Library", "stub:olin")
    assert dm(client, H1, "help").startswith("say plan to start one")  # stopped asking


# --- 5: three people, three origins, three routes -----------------------------------------

PEOPLE = [
    ("Maya", "+16075550101", "SAM1", "north campus", "neither"),
    ("Sam", "+16075550102", "SAM1", "collegetown", "neither"),
    ("Jordan", "+16075550103", "SAM1", "the commons", "neither"),
]


def test_5_each_member_gets_their_own_origin_and_directions(client) -> None:
    deps = client.app.state.deps
    calls: list[tuple[float, float]] = []
    real_route = deps.routing.route

    async def spy(origin, destination, mode, arrive_by=None, depart_at=None):
        calls.append((origin.lat, origin.lng))
        return await real_route(origin, destination, mode, arrive_by, depart_at)

    deps.routing.route = spy
    for name, handle, code, start, _ in PEOPLE:
        to_location_step(client, handle, name, code)
        dm(client, handle, start)
        dm(client, handle, "yes")
    code = re.search(r"join ([A-Z0-9]{4})", dm(client, PEOPLE[0][1], "@plan")).group(1)
    for _, handle, _, _, _ in PEOPLE[1:]:
        dm(client, handle, f"join {code}")
    for _, handle, _, _, modes in PEOPLE:
        dm(client, handle, modes)
        dm(client, handle, "same")
    assert "for everyone" in dm(client, PEOPLE[0][1], "@go")
    dm(client, PEOPLE[0][1], "A")
    dm(client, PEOPLE[1][1], "A")

    # One Routes call per member, each from that member's own starting point.
    assert len(calls) == 3 and len(set(calls)) == 3
    itineraries, links = {}, set()
    for name, handle, *_ in PEOPLE:
        sent = client.get(f"/sim/outbox/{handle}").json()
        texts = [m for m in sent if m["kind"] == "private"]
        itineraries[name] = next(m["text"] for m in texts if m["text"].startswith("your plan"))
        links |= {m["text"] for m in sent if m["kind"] == "link"}  # Maps preview cards
    venues = {t.splitlines()[0] for t in itineraries.values()}
    assert len(venues) == 1  # same venue...
    assert len(set(itineraries.values())) == 3  # ...different directions for each person
    assert len(links) == 3 and all("origin=" in link for link in links)


# --- 6: no local places JSON ---------------------------------------------------------------

OLD = ("venues.json", "venues_raw.json", "demo_locations", "venues_test", "MockPlaces", "OsmPlaces")


def test_6_nothing_reads_the_old_places_json() -> None:
    for gone in (
        "fixtures/venues.json",
        "fixtures/venues_raw.json",
        "fixtures/demo_locations.json",
        "tests/fixtures/venues_test.json",
        "app/providers/mock/places.py",
        "app/providers/real/osm_places.py",
    ):
        assert not (ROOT / gone).exists(), gone
    for folder in ("app", "scripts", "tests"):
        for path in (ROOT / folder).rglob("*.py"):
            if path.name == Path(__file__).name:
                continue
            text = path.read_text(encoding="utf-8")
            for name in OLD:
                assert name not in text, f"{path.relative_to(ROOT)} mentions {name}"
