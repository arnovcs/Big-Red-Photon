"""v3 §18 demo through the simulator (DMs + join code), with mock places/routing and a
stubbed LLM defined here (there is no mock LLM in app/)."""

import json
import logging
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import session as db
from app.db.tables import SessionRow, UserRow
from app.deps import build_deps
from app.main import create_app
from app.models.conversation import (
    ConstraintField,
    ConstraintKind,
    ExtractedConstraint,
    GroupPreferences,
    PseudonymousMessage,
)
from app.models.plans import Plan
from app.settings import Settings
from tests.places_stub import VENUE_NAMES, StubPlaces

# 18:00 in Ithaca (EDT) on the demo date.
FIXED_NOW = datetime(2026, 10, 3, 22, 0, tzinfo=UTC)

# name, handle, bank code, stub limit, typed start, geocoded label, modes reply.
# Realistic, spread-out starts: Collegetown, North Campus, and downtown.
MAYA = ("Maya", "+16075550101", "MAYA1", 30, "collegetown", "Collegetown", "bike")
SAM = (
    "Sam",
    "+16075550102",
    "SAM1",
    50,
    "north campus",
    "Robert Purcell Community Center",
    "neither",
)
JORDAN = ("Jordan", "+16075550103", "JORDAN1", 15, "downtown", "Ithaca Commons", "neither")
PERSONAS = [MAYA, SAM, JORDAN]

# What each person DMs privately while the plan is collecting (§18 step 3).
PREFERENCES = {
    MAYA[1]: ["I'm starving"],
    SAM[1]: ["no sushi pls", "something we haven't tried?"],
    JORDAN[1]: ["I have to be back by 9, and nothing too far"],
}


class StubLLM:
    """Keyword stub of LLMProvider for the §18 transcript."""

    def __init__(self) -> None:
        self.transcripts: list[list[PseudonymousMessage]] = []

    async def extract_preferences(
        self, transcript: list[PseudonymousMessage], now_local: datetime
    ) -> GroupPreferences:
        self.transcripts.append(transcript)
        found = []

        def add(m, field, value, kind, polarity="want", confidence=0.9):
            found.append(
                ExtractedConstraint(
                    pid=m.pid,
                    field=field,
                    value=value,
                    polarity=polarity,
                    kind=kind,
                    confidence=confidence,
                    evidence_msg_ids=[m.message_id],
                )
            )

        for m in transcript:
            text = m.text.lower()
            if "starving" in text:
                add(m, ConstraintField.CATEGORY, "food", ConstraintKind.INFERRED)
            if "no sushi" in text:
                add(m, ConstraintField.CUISINE, "sushi", ConstraintKind.VETO, polarity="avoid")
            if "back by 9" in text:
                add(m, ConstraintField.AVAILABLE_UNTIL, "21:00", ConstraintKind.HARD)
            if "haven't tried" in text:
                add(m, ConstraintField.NOVELTY, 1.0, ConstraintKind.SOFT, confidence=0.6)
            if "love korean" in text:
                add(m, ConstraintField.CUISINE, "korean", ConstraintKind.SOFT, confidence=0.6)
            if "1 minute away" in text:
                add(m, ConstraintField.MAX_TRAVEL_MIN, 1, ConstraintKind.HARD)
        return GroupPreferences(constraints=found, group_intent="food")

    async def phrase_explanations(self, facts: list[dict]) -> list[str]:
        return ["" for _ in facts]


@pytest.fixture
def harness(tmp_path):
    settings = Settings(
        _env_file=None,
        # Pin these so shell variables (e.g. PROVIDER_MESSAGING=photon) can't leak in.
        provider_messaging="sim",
        nessie_api_key="",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'e2e.db'}",
        poll_timeout_sec=3600,
    )
    llm = StubLLM()
    app = create_app(build_deps(settings, llm=llm, clock=lambda: FIXED_NOW, places=StubPlaces()))
    with TestClient(app) as client:
        yield client, llm


def outbox(client: TestClient, handle: str) -> list[dict]:
    resp = client.get(f"/sim/outbox/{handle}")
    assert resp.status_code == 200
    return resp.json()


def dm(client: TestClient, handle: str, text: str) -> str:
    """Send a DM as `handle`; return everything the bot sent back (one message per line)."""
    before = len(outbox(client, handle))
    resp = client.post("/sim/message", json={"sender_handle": handle, "text": text})
    assert resp.status_code == 200
    new = [m["text"] for m in outbox(client, handle)[before:]]
    return "\n".join(new)


def answer_modes(client: TestClient, persona: tuple) -> None:
    """Per-plan questions, one at a time: how (modes), then where from (nobody shares in
    the sim, so "same" keeps the starting point from setup)."""
    assert "where are you starting from?" in dm(client, persona[1], persona[-1])
    assert "what are you in the mood for" in dm(client, persona[1], "same")


def onboard(client: TestClient, persona: tuple) -> None:
    name, handle, code, limit, start, label, modes = persona
    assert "what should I call you?" in dm(client, handle, "start")
    assert "bank code" in dm(client, handle, name)
    assert f"${limit}" in dm(client, handle, code)
    assert "where are you starting" in dm(client, handle, "yes")
    reply = dm(client, handle, start)
    assert label in reply
    if reply.endswith("right?"):  # ambiguous match: confirm it
        reply = dm(client, handle, "yes")
    assert "join <code>" in reply  # obvious matches skip "right?"; modes come per plan


def without_venue_names(text: str) -> str:
    for name in sorted(VENUE_NAMES, key=len, reverse=True):
        text = text.replace(name, " ")
    return text


def leaks_limit(text: str, limit: int) -> bool:
    patterns = [
        rf"\$\s?{limit}(\.\d{{2}})?\b",
        rf"\b{limit}(\.00)?\s*(dollars|bucks|usd)\b",
        rf"\b{limit}\.00\b",
    ]
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def finished_session(client: TestClient) -> tuple[Plan, dict[str, str]]:
    """The delivered plan and its pid → handle map, read from the finished session."""

    async def load() -> tuple[Plan, dict[str, str]]:
        async with db.session_factory()() as s:
            row = (await s.scalars(select(SessionRow))).one()
            assert row.state == "done"
            plans = [Plan.model_validate(p) for p in json.loads(row.plans_json)]
            plan = next(p for p in plans if p.plan_id == row.winner_plan_id)
            pid_map = json.loads(row.pid_map_json)
            users = {str(u.id): u.handle for u in await s.scalars(select(UserRow))}
            return plan, {pid: users[uid] for pid, uid in pid_map.items()}

    return client.portal.call(load)


def test_demo_scenario(harness, caplog: pytest.LogCaptureFixture) -> None:
    client, llm = harness
    caplog.set_level(logging.WARNING)
    handles = [p[1] for p in PERSONAS]
    for persona in PERSONAS:
        onboard(client, persona)
    # Onboarding prompts are fixed copy (they name example landmarks); skip them below.
    after_onboarding = {handle: len(outbox(client, handle)) for handle in handles}

    # Form the virtual group.
    started = dm(client, MAYA[1], "@plan")
    assert "how're you getting there?" in started
    code = re.search(r"join ([A-Z0-9]{4})", started).group(1)
    assert not re.search(r"[01OI]", code)
    assert "you're in!" in dm(client, SAM[1], f"join {code.lower()}")
    assert "you're in!" in dm(client, JORDAN[1], f"ok join {code}")
    for persona in PERSONAS:
        answer_modes(client, persona)
    for handle in handles:
        assert any(m["text"] == "Jordan is in ✅ (3 of you)" for m in outbox(client, handle))

    # Preferences, privately. Each gets a 👍 tapback on that message, not a "got it" text.
    sim = client.app.state.deps.messaging
    for handle, texts in PREFERENCES.items():
        before = len(sim.reactions)
        for text in texts:
            dm(client, handle, text)
        added = sim.reactions[before:]
        assert [(h, e) for h, _, e in added] == [(handle, "👍")]  # first message only
        assert not any(m["text"].startswith("got it 👍") for m in outbox(client, handle))

    # More than half must say go (2 of 3). Everyone hears who's ready.
    dm(client, SAM[1], "go")
    for handle in handles:
        assert outbox(client, handle)[-1]["text"] == (
            "Sam's ready ✅ (1/2 needed). say go when you're in too"
        )
    dm(client, MAYA[1], "@go")

    # The LLM saw only pseudonymous text: no names, handles, or limits.
    (transcript,) = llm.transcripts
    assert len(transcript) == 4
    assert {m.pid for m in transcript} <= {"p1", "p2", "p3"}
    for name, handle, *_ in PERSONAS:
        assert all(name not in m.text and handle not in m.text for m in transcript)

    # Every member gets "Looking…" and the same poll with ≤3 options.
    polls = []
    for handle in handles:
        msgs = outbox(client, handle)
        # Typing dots while planning (on, then off), instead of a "looking…" message.
        events = [on for h, on in client.app.state.deps.messaging.typing_events if h == handle]
        assert events[:2] == [True, False]
        (poll_msg,) = [m for m in msgs if m.get("poll")]
        assert poll_msg["kind"] == "group"
        polls.append(poll_msg)
    assert all(p == polls[0] for p in polls)
    options = polls[0]["poll"]
    assert 1 <= len(options) <= 3
    assert all("Plum Tree" not in o["title"] for o in options)  # sushi veto respected
    assert polls[0]["text"].endswith("reply A, B, or C")

    # Votes are DM replies; 2 of 3 decide.
    assert dm(client, MAYA[1], "A") == "🗳️ 1/3 voted"
    dm(client, SAM[1], "a")

    # Each member: group confirmation, then their own itinerary right under it.
    itineraries = {}
    for handle in handles:
        *_, confirmation, itinerary, link = outbox(client, handle)
        assert confirmation["kind"] == "group" and confirmation["effect"] == "confetti"
        assert confirmation["text"].startswith("🎉 it's A:")
        assert confirmation["text"].endswith("your route's coming 👇")
        assert itinerary["kind"] == "private"
        assert itinerary["text"].startswith("your plan tonight:")
        assert link["kind"] == "link" and link["text"].startswith("https://www.google.com/maps")
        itineraries[handle] = itinerary["text"]
    assert len(set(itineraries.values())) == len(PERSONAS)
    # Different modes, same arrival: Sam (far, $50) rides, Maya bikes, Jordan ($15) walks.
    assert "request a ride by" in itineraries[SAM[1]]
    assert "on the bike" in itineraries[MAYA[1]]
    assert "min walk" in itineraries[JORDAN[1]]
    arrivals = {re.search(r"get there ~(\d+:\d\d)", t).group(1) for t in itineraries.values()}
    assert len(arrivals) == 1
    assert arrivals.pop() in confirmation["text"]

    # No private values in any group-safe message.
    for handle in handles:
        for msg in outbox(client, handle):
            if msg["kind"] != "group":
                continue
            text = without_venue_names(msg["text"] + json.dumps(msg["poll"] or []))
            for _, other_handle, _, limit, _, label, _ in PERSONAS:
                assert not leaks_limit(text, limit), text
                assert other_handle not in text and other_handle[-4:] not in text
                assert label not in text

    # No member's DMs contain another member's private values or preferences.
    for name, handle, *_ in PERSONAS:
        received = outbox(client, handle)[after_onboarding[handle] :]
        everything = without_venue_names("\n".join(m["text"] for m in received))
        for other in PERSONAS:
            if other[1] == handle:
                continue
            _, other_handle, _, other_limit, _, other_label, _ = other
            assert not leaks_limit(everything, other_limit), (name, other[0])
            assert other_label not in everything
            assert other_handle not in everything
            assert all(text not in everything for text in PREFERENCES[other_handle])

    # The guard checked every message and had nothing to block in a normal run.
    assert "privacy_block" not in caplog.text

    # Every leave_by + travel time reaches the same T_target, within each budget.
    plan, pid_to_handle = finished_session(client)
    limit_by_handle = {p[1]: p[3] for p in PERSONAS}
    assert len(plan.assignments) == len(PERSONAS)
    for a in plan.assignments:
        assert a.arrive_at == plan.target_arrival
        reached = a.leave_by + timedelta(minutes=a.travel_min)
        assert abs((reached - plan.target_arrival).total_seconds()) < 1
        assert a.total_cost_usd <= limit_by_handle[pid_to_handle[a.pid]]

    # The session is over: the code is retired and a new @plan gets a new one.
    assert dm(client, JORDAN[1], f"join {code}") == "hmm I don't know that code. double check it?"
    assert code not in dm(client, MAYA[1], "@plan")


def test_join_rules(harness) -> None:
    client, _ = harness
    onboard(client, MAYA)
    onboard(client, SAM)

    assert "not in a plan" in dm(client, MAYA[1], "@go")
    code = re.search(r"join ([A-Z0-9]{4})", dm(client, MAYA[1], "@plan")).group(1)
    assert code in dm(client, MAYA[1], "@plan")  # already in a plan
    assert dm(client, MAYA[1], "@go") == f"need at least 2 people! share code {code} first"
    assert "don't know that code" in dm(client, SAM[1], "join ZZZZ")

    # A join from someone not set up yet starts onboarding instead.
    newcomer = "+16075550199"
    client.post("/sim/message", json={"sender_handle": newcomer, "text": f"join {code}"})
    first, second = (m["text"] for m in outbox(client, newcomer))
    assert first == f"quick setup first, then send join {code} again"
    assert "what should I call you?" in second

    # Any member can cancel; everyone hears about it and the code is retired.
    dm(client, SAM[1], f"join {code}")
    answer_modes(client, SAM)
    dm(client, SAM[1], "@cancel")
    for handle in (MAYA[1], SAM[1]):
        assert outbox(client, handle)[-1]["text"].startswith("plan cancelled")
    assert "don't know that code" in dm(client, SAM[1], f"join {code}")


def test_nothing_fits_returns_to_collecting_with_a_safe_hint(harness) -> None:
    client, _ = harness
    onboard(client, MAYA)
    onboard(client, JORDAN)
    code = re.search(r"join ([A-Z0-9]{4})", dm(client, MAYA[1], "@plan")).group(1)
    dm(client, JORDAN[1], f"join {code}")
    answer_modes(client, MAYA)
    answer_modes(client, JORDAN)
    dm(client, JORDAN[1], "it has to be 1 minute away")  # HARD: nothing qualifies
    dm(client, MAYA[1], "I'd love korean")  # SOFT: named in the hint
    dm(client, JORDAN[1], "go")
    dm(client, MAYA[1], "@go")

    expected = (
        "nothing works for everyone rn. being open to more than korean could help. "
        "then say go again"
    )
    for handle in (MAYA[1], JORDAN[1]):
        assert outbox(client, handle)[-1] == {"kind": "group", "text": expected, "poll": None}
    # Back to COLLECTING, readiness reset: both say go again (Jordan's HARD limit still
    # rules everything out).
    dm(client, JORDAN[1], "go")
    assert dm(client, MAYA[1], "@go").endswith(expected)
    typing = [on for h, on in client.app.state.deps.messaging.typing_events if h == MAYA[1]]
    assert typing == [True, False, True, False]  # planned twice
