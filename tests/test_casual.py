"""Texting like a friend: plain-word commands, tapbacks, no needless "right?", nudges."""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.conversation import copy
from app.conversation.commands import SessionCommand, parse_session_command
from app.delivery.itinerary import Itinerary
from app.deps import build_deps
from app.main import create_app
from app.models.candidates import ResolvedPlace
from app.models.outbound import PrivateMessage
from app.models.private import LatLng
from app.onboarding.fsm import is_clear_match
from app.settings import Settings
from tests.places_stub import StubPlaces

ITHACA = LatLng(lat=42.44, lng=-76.50)


@pytest.mark.parametrize(
    ("text", "command"),
    [
        ("go", SessionCommand.GO),
        ("ok go!", SessionCommand.GO),
        ("Let's go", SessionCommand.GO),
        ("nvm", SessionCommand.CANCEL),
        ("never mind", SessionCommand.CANCEL),
        ("let's plan something", SessionCommand.PLAN),
        ("@go", SessionCommand.GO),  # the @ versions still work
        ("ok @cancel", SessionCommand.CANCEL),
    ],
)
def test_plain_words_are_commands(text, command) -> None:
    assert parse_session_command(text).command == command


@pytest.mark.parametrize("text", ["let's go bowling", "go somewhere cheap", "I can't go far"])
def test_preferences_mentioning_go_are_not_commands(text) -> None:
    assert parse_session_command(text) is None


def place(name: str, lat: float = 42.44, lng: float = -76.49) -> ResolvedPlace:
    return ResolvedPlace(location=LatLng(lat=lat, lng=lng), name=name, address="1 Main St")


def test_obvious_matches_skip_the_yes_no() -> None:
    assert is_clear_match("Young Boys Barbershop", place("Young Boys barbershop"), ITHACA)
    assert is_clear_match("I'm at olin", place("Olin Library"), ITHACA)
    assert is_clear_match("collegetown bagels", place("Collegetown Bagels"), ITHACA)
    # Not plainly what they typed, or far away: still asked "right?".
    assert not is_clear_match("north campus", place("Robert Purcell Community Center"), ITHACA)
    assert not is_clear_match("my dorm", place("Dorm Co"), ITHACA)
    assert not is_clear_match("times square", place("Times Square", 40.758, -73.985), ITHACA)


def test_today_or_tonight() -> None:
    assert copy.day_word(datetime(2026, 10, 4, 14, 0)) == "today"
    assert copy.day_word(datetime(2026, 10, 4, 19, 0)) == "tonight"


# --- in the simulator ------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        _env_file=None,
        provider_messaging="sim",
        nessie_api_key="",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'c.db'}",
        poll_timeout_sec=3600,
    )
    with TestClient(create_app(build_deps(settings, places=StubPlaces()))) as c:
        yield c


H = "+16075550101"


def send(client: TestClient, text: str, message_id: str | None = None) -> list[str]:
    before = len(client.get(f"/sim/outbox/{H}").json())
    body = {"sender_handle": H, "text": text}
    if message_id:
        body["message_id"] = message_id
    client.post("/sim/message", json=body).raise_for_status()
    return [m["text"] for m in client.get(f"/sim/outbox/{H}").json()[before:]]


def test_unclear_answer_gets_a_question_mark_tapback(client) -> None:
    for text in ["start", "Maya", "MAYA1", "yes", "olin"]:
        send(client, text)
    send(client, "plan")
    reply = send(client, "teleport")  # not a way to get there
    assert reply == [copy.MODES_INVALID]
    assert client.app.state.deps.messaging.reactions[-1][2] == "❓"


def test_without_tapbacks_a_short_text_says_got_it(client) -> None:
    sim = client.app.state.deps.messaging
    sim.reactions_work = False
    for text in ["start", "Maya", "MAYA1", "yes", "olin", "plan", "bike", "same"]:
        send(client, text)
    assert send(client, "tacos?") == [copy.NOTED]
    assert send(client, "or pizza") == []  # only once, to keep the DM quiet


async def test_leave_nudge_arrives_before_their_leave_time(client) -> None:
    sessions = client.app.state.chat_router.sessions
    now = datetime.now().astimezone()
    sessions.deps.clock = lambda: now
    lead = timedelta(minutes=sessions.deps.settings.leave_nudge_min)
    item = Itinerary(
        H, PrivateMessage(text="x"), frozenset({Decimal(0)}), mode="walk",
        leave_by=now + lead + timedelta(seconds=0.05),
    )  # fmt: skip
    sessions._schedule_leave_nudges([item])
    await asyncio.sleep(0.3)
    texts = [m["text"] for m in sessions.deps.messaging.messages(H)]
    assert texts[-1] == copy.leave_nudge("walk", 5)


# --- "Ya" is a yes (the Perrigo Park conversation) -------------------------------------------


@pytest.mark.parametrize(
    ("text", "answer"),
    [
        ("Ya", "yes"), ("yesss", "yes"), ("yeahh", "yes"), ("bet", "yes"), ("mhm", "yes"),
        ("👍", "yes"), ("Bro what no ya means yes", "yes"), ("nahh", "no"),
        ("nope wrong place", "no"), ("not right", "no"), ("not sure", None),
        ("I'm at perrigo park rn", None),
    ],
)  # fmt: skip
def test_how_people_actually_say_yes_and_no(text, answer) -> None:
    from app.conversation.commands import yes_no

    assert yes_no(text) == answer


def test_ya_confirms_and_chat_is_never_searched_as_a_place(client) -> None:
    places = client.app.state.deps.places
    for text in ["start", "Maya", "MAYA1", "yes"]:
        send(client, text)
    assert send(client, "meet me at north campus") == ["Robert Purcell Community Center, right?"]
    looked_up = len(places.lookups)
    # The real conversation: "Ya" became a search for "Ya Ya's House" in Schenectady.
    assert send(client, "Ya")[0].startswith("you're all set!")
    assert len(places.lookups) == looked_up  # nothing searched

    send(client, "location")
    send(client, "meet me at north campus")
    assert send(client, "Bro what no ya means yes")[0].startswith("you're all set!")
    assert send(client, "location")[0].startswith("where are you starting from?")
    assert send(client, "lol") == [copy.LOCATION_UNCLEAR]  # chat, not a place
    assert send(client, "not sure") == [copy.LOCATION_UNCLEAR]
    assert places.lookups == ["meet me at north campus", "meet me at north campus"]


# --- "go" said inside a sentence ------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Ok thats all can you tell me where to go now", "that's it!", "we're all set",
        "show us the options", "where do we go now?", "what's the plan?", "ok so what now",
        "everyone's in", "that's all, somewhere cheap pls",
    ],
)  # fmt: skip
def test_done_sentences_mean_go(text) -> None:
    assert parse_session_command(text).command == SessionCommand.GO


@pytest.mark.parametrize(
    "text",
    [
        "let's go bowling", "where should we go for food", "I'm ready to eat tacos",
        "I'm done with class at 5", "I wanna go somewhere fun", "go somewhere cheap",
    ],
)  # fmt: skip
def test_preferences_that_mention_go_or_done_are_not_go(text) -> None:
    assert parse_session_command(text) is None


def test_a_go_sentence_also_keeps_what_was_said(client) -> None:
    sam = "+16075550102"
    for text in ["start", "Maya", "MAYA1", "yes", "olin"]:
        send(client, text)
    for text in ["start", "Sam", "SAM1", "yes", "olin"]:
        client.post("/sim/message", json={"sender_handle": sam, "text": text})
    code = __import__("re").search(r"join ([A-Z0-9]{4})", " ".join(send(client, "plan"))).group(1)
    client.post("/sim/message", json={"sender_handle": sam, "text": f"join {code}"})
    for who, text in [(H, "bike"), (H, "same"), (sam, "walk"), (sam, "same")]:
        client.post("/sim/message", json={"sender_handle": who, "text": text})

    llm_saw = []
    deps = client.app.state.deps

    class SeeingLLM:
        async def extract_preferences(self, transcript, now_local):
            from app.models.conversation import GroupPreferences

            llm_saw.extend(m.text for m in transcript)
            return GroupPreferences(constraints=[], group_intent="food")

        async def phrase_explanations(self, facts):
            return ["" for _ in facts]

    deps.llm = SeeingLLM()
    reply = " ".join(send(client, "ok thats all, somewhere cheap pls. tell me where to go"))
    assert "for everyone" in reply  # planned
    assert llm_saw == ["ok thats all, somewhere cheap pls. tell me where to go"]
