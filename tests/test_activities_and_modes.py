"""Activity requests find activities (not restaurants); each person's travel mode is used.

1. "I want to play pickleball" → pickleball courts / sports facilities (live Google).
2. "let's do something sporty" → sports venues (live Google).
3. "cheap food near Collegetown" → still restaurants (live Google).
4. "something fun" → a mix of activities, no restaurants, no follow-up question.
5. "I'm driving" → a DRIVE route and driving directions.
6. One drives, one walks → one DRIVE route and one WALK route.
7. Never says a mode → WALK, and they're told.
8. "actually I'll drive" mid-plan → that person's route switches to DRIVE.
9. Per-user routes and typed/live locations: tests/test_locations_and_routes.py and
   tests/test_shared_location.py (unchanged, still passing).

Live Google tests need GOOGLE_PLACES_API_KEY. The Gemini check needs LIVE_GEMINI=1 (it
uses the daily Gemini quota).
"""

import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient

from app.deps import build_deps
from app.main import create_app
from app.models.conversation import ConstraintField as F
from app.models.conversation import ConstraintKind as K
from app.models.conversation import ExtractedConstraint, GroupPreferences, PseudonymousMessage
from app.models.private import LatLng
from app.models.routing import Mode
from app.planning import pipeline
from app.planning.extraction import validate
from app.providers.cache import RecordReplayCache
from app.providers.real.google_places import CATEGORY_TYPES, GooglePlaces
from app.settings import Settings
from tests.places_stub import StubPlaces

ROOT = Path(__file__).resolve().parents[1]
ENV = dotenv_values(ROOT / ".env")
GOOGLE_KEY = ENV.get("GOOGLE_PLACES_API_KEY") or ""
live = pytest.mark.skipif(not GOOGLE_KEY, reason="needs GOOGLE_PLACES_API_KEY")
COLLEGETOWN = LatLng(lat=42.4422, lng=-76.4852)
SUNDAY_2PM = datetime(2026, 10, 4, 14, 0, tzinfo=ZoneInfo("America/New_York"))
FOOD_TYPES = {"restaurant", "bar", "pub", "cafe"}


def prefs(intent: str, *constraints: tuple) -> GroupPreferences:
    return GroupPreferences(
        group_intent=intent,
        constraints=[
            ExtractedConstraint(
                pid="p1", field=f, value=v, kind=K.SOFT, confidence=0.9, evidence_msg_ids=["m1"]
            )
            for f, v in constraints
        ],
    )


async def options_for(p: GroupPreferences) -> list:
    """What the pipeline would search and shortlist for these preferences (live Google)."""
    g = GooglePlaces(
        Settings(_env_file=None, google_places_api_key=GOOGLE_KEY), RecordReplayCache("off")
    )
    cuisines, activities = pipeline._wanted_cuisines(p), pipeline._wanted_activities(p)
    found = await g.search_nearby(
        COLLEGETOWN,
        pipeline.SEARCH_RADIUS_M,
        pipeline._categories(p),
        SUNDAY_2PM,
        cuisines=cuisines,
        activities=activities,
    )
    return pipeline._shortlist(found, cuisines, activities, COLLEGETOWN)


# --- Bug 1: activities ----------------------------------------------------------------------


def test_activity_requests_never_search_restaurants_or_bars() -> None:
    assert pipeline._categories(prefs("activity", (F.CATEGORY, "activity"))) == [
        "activity",
        "sports",
    ]
    assert pipeline._categories(prefs("activity", (F.CATEGORY, "sports"))) == ["sports"]
    assert pipeline._categories(prefs("food", (F.CATEGORY, "food"))) == ["food", "cafe", "dessert"]
    # Bars only when someone asks for drinks.
    assert pipeline._categories(prefs("activity", (F.CATEGORY, "bar"))) == ["bar"]
    assert pipeline._categories(prefs("unknown")) == []  # nothing said: a mix of everything
    assert not set(CATEGORY_TYPES["sports"]) & FOOD_TYPES


def test_pickleball_is_kept_as_a_specific_activity() -> None:
    transcript = [
        PseudonymousMessage(message_id="m1", pid="p1", text="pickleball?", ts_local="14:00")
    ]
    raw = {
        "constraints": [
            {"pid": "p1", "field": "activity", "value": "Pickleball", "kind": "soft",
             "confidence": 0.9, "evidence_msg_ids": ["m1"]},
            {"pid": "p1", "field": "category", "value": "sporty", "kind": "soft",
             "confidence": 0.9, "evidence_msg_ids": ["m1"]},
        ],
        "group_intent": "activity",
    }  # fmt: skip
    p = validate(raw, transcript)
    assert pipeline._wanted_activities(p) == ["pickleball"]
    assert [c.value for c in p.constraints if c.field == F.CATEGORY] == ["sports"]


@live
async def test_1_pickleball_finds_courts_not_restaurants() -> None:
    options = await options_for(
        prefs("activity", (F.CATEGORY, "sports"), (F.ACTIVITY, "pickleball"))
    )
    assert options
    assert all(c.category in ("activity", "sports") for c in options)
    top = options[:5]
    assert all("pickleball" in c.cuisines for c in top)  # what was asked for comes first
    print("pickleball →", [c.name for c in top])


@live
async def test_2_sporty_finds_sports_venues() -> None:
    options = await options_for(prefs("activity", (F.CATEGORY, "sports")))
    assert options and all(c.category == "sports" for c in options)
    assert len(options) >= 5
    print("sporty →", [c.name for c in options[:5]])


@live
async def test_3_cheap_food_near_collegetown_is_still_restaurants() -> None:
    options = await options_for(prefs("food", (F.CATEGORY, "food")))
    assert options and all(c.category in ("food", "cafe", "dessert") for c in options)
    assert sum(c.category == "food" for c in options) >= 5


@live
async def test_4_something_fun_is_a_mix_of_activities_not_restaurants() -> None:
    options = await options_for(prefs("activity", (F.CATEGORY, "activity")))
    assert options and all(c.category in ("activity", "sports") for c in options)
    kinds = {c.cuisines[0] for c in options if c.cuisines}
    assert len(kinds) >= 4  # parks, gyms, theaters, courts... not one kind of place
    print("something fun →", [(c.name, c.cuisines[:1]) for c in options[:6]])


@pytest.mark.skipif(not os.getenv("LIVE_GEMINI"), reason="set LIVE_GEMINI=1 (uses Gemini quota)")
async def test_gemini_extracts_pickleball_sporty_and_fun() -> None:
    from app.providers.real.gemini import GeminiLLM

    llm = GeminiLLM(
        Settings(_env_file=None, gemini_api_key=ENV.get("GEMINI_API_KEY", "")),
        RecordReplayCache("off"),
    )

    async def read(text: str) -> GroupPreferences:
        t = [PseudonymousMessage(message_id="m1", pid="p1", text=text, ts_local="14:00")]
        return await llm.extract_preferences(t, SUNDAY_2PM)

    pickleball = await read("I want to play pickleball")
    assert pickleball.group_intent == "activity"
    assert pipeline._wanted_activities(pickleball) == ["pickleball"]
    sporty = await read("let's do something sporty")
    assert "sports" in pipeline._categories(sporty)
    fun = await read("something fun")
    assert fun.group_intent == "activity" and "food" not in pipeline._categories(fun)


# --- Bug 2: travel modes (simulator) ------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        _env_file=None,
        provider_messaging="sim",
        nessie_api_key="",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'm.db'}",
        poll_timeout_sec=3600,
    )
    app = create_app(build_deps(settings, places=StubPlaces()))
    with TestClient(app) as c:
        yield c


MAYA = ("Maya", "+16075550101", "SAM1", "north campus")
SAM = ("Sam", "+16075550102", "SAM1", "the commons")


def dm(client: TestClient, handle: str, text: str) -> str:
    before = len(client.get(f"/sim/outbox/{handle}").json())
    client.post("/sim/message", json={"sender_handle": handle, "text": text}).raise_for_status()
    return "\n".join(m["text"] for m in client.get(f"/sim/outbox/{handle}").json()[before:])


def run_plan(client: TestClient, answers: dict[str, str | None], chat: list[tuple] = ()) -> dict:
    """Set up Maya and Sam, plan, answer "how are you getting there?" (None = no answer),
    send `chat`, @go, vote A. Returns {name: (route modes, itinerary text)}."""
    deps = client.app.state.deps
    modes: dict[tuple, list[Mode]] = {}
    real_route = deps.routing.route

    async def spy(origin, destination, mode, arrive_by=None, depart_at=None):
        modes.setdefault((round(origin.lat, 4), round(origin.lng, 4)), []).append(mode)
        return await real_route(origin, destination, mode, arrive_by, depart_at)

    deps.routing.route = spy
    people = {"Maya": MAYA, "Sam": SAM}
    for name, handle, code, start in people.values():
        for text in ["start", name, code, "yes", start, "yes"]:
            dm(client, handle, text)
    code = re.search(r"join ([A-Z0-9]{4})", dm(client, MAYA[1], "@plan")).group(1)
    dm(client, SAM[1], f"join {code}")
    for name, (_, handle, _, _) in people.items():
        if answers.get(name):
            dm(client, handle, answers[name])
            dm(client, handle, "same")
    for name, text in chat:
        dm(client, people[name][1], text)
    dm(client, SAM[1], "go")  # more than half must say go: both of 2
    assert "for everyone" in dm(client, MAYA[1], "@go")
    dm(client, MAYA[1], "A")
    dm(client, SAM[1], "A")
    result = {}
    starts = {"Maya": (42.456, -76.4777), "Sam": (42.4393, -76.4977)}  # stub landmarks
    for name, (_, handle, _, _) in people.items():
        sent = client.get(f"/sim/outbox/{handle}").json()
        texts = [m["text"] for m in sent]
        link = next(m["text"] for m in sent if m["kind"] == "link")
        itinerary = next(t for t in texts if t.startswith("your plan")) + "\n" + link
        result[name] = (modes.get(starts[name], []), itinerary, texts)
    return result


def test_5_im_driving_gets_a_drive_route_and_driving_directions(client) -> None:
    out = run_plan(client, {"Maya": "I'm driving", "Sam": "walking"})
    route_modes, itinerary, _ = out["Maya"]
    assert route_modes == [Mode.DRIVE]
    assert "🚗" in itinerary and "drive" in itinerary.lower()
    assert "walk about" not in itinerary.lower()
    assert "travelmode=driving" in itinerary


def test_6_one_drives_one_walks(client) -> None:
    out = run_plan(client, {"Maya": "car", "Sam": "walk"})
    assert out["Maya"][0] == [Mode.DRIVE]
    assert out["Sam"][0] == [Mode.WALK]
    assert "travelmode=walking" in out["Sam"][1]


def test_7_no_answer_means_walking_and_they_are_told(client) -> None:
    out = run_plan(client, {"Maya": "car", "Sam": None})
    route_modes, itinerary, texts = out["Sam"]
    assert route_modes == [Mode.WALK]
    assert any(t.startswith("you didn't say how you're getting there") for t in texts)
    assert "walk" in itinerary.lower()


def test_8_actually_ill_drive_switches_the_next_route(client) -> None:
    out = run_plan(
        client,
        {"Maya": "walking", "Sam": "walking"},
        chat=[("Maya", "let's get food"), ("Maya", "actually I'll drive")],
    )
    route_modes, itinerary, texts = out["Maya"]
    assert any(t.startswith("got it, driving") for t in texts)
    assert route_modes == [Mode.DRIVE]
    assert "🚗" in itinerary


def test_a_named_activity_keeps_only_places_for_it() -> None:
    from tests.optimizer_helpers import venue

    court = venue("court", category="sports", cuisines=("pickleball", "tennis_court"))
    trail = venue("trail", category="sports", cuisines=("hiking_area",))
    assert pipeline._shortlist([trail, court], [], ["pickleball"]) == [court]
    # Nothing found for the activity: the wider search is the fallback.
    assert pipeline._shortlist([trail], [], ["pickleball"]) == [trail]
