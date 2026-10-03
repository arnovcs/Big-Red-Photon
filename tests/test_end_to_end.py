"""§18 demo scenario through the simulator: mock places/routing, stubbed LLM (tests only)."""

import json
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

# 18:00 in Ithaca (EDT) on the demo date.
FIXED_NOW = datetime(2026, 10, 3, 22, 0, tzinfo=UTC)
GROUP = "group-chat-1"

PERSONAS = [
    # name, handle, bank code, stub limit, start, modes reply
    ("Maya", "+16075550101", "MAYA1", 30, "Olin Library", "bike"),
    ("Sam", "+16075550102", "SAM1", 50, "north campus", "neither"),
    ("Jordan", "+16075550103", "JORDAN1", 15, "Willard Straight", "neither"),
]
ORIGIN_LABELS = ["Olin Library", "Robert Purcell Community Center", "Willard Straight Hall"]


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
        return GroupPreferences(constraints=found, group_intent="food")

    async def phrase_explanations(self, facts: list[dict]) -> list[str]:
        return ["" for _ in facts]


@pytest.fixture
def harness(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'e2e.db'}",
        poll_timeout_sec=3600,
    )
    llm = StubLLM()
    app = create_app(build_deps(settings, llm=llm, clock=lambda: FIXED_NOW))
    with TestClient(app) as client:
        yield client, llm


def say(client: TestClient, handle: str, name: str, text: str, chat_id: str = GROUP) -> None:
    is_group = chat_id == GROUP
    resp = client.post(
        "/sim/message",
        json={
            "chat_id": chat_id if is_group else f"dm-{handle}",
            "is_group": is_group,
            "sender_handle": handle,
            "sender_name": name,
            "text": text,
        },
    )
    assert resp.status_code == 200


def dm(client: TestClient, handle: str, name: str, text: str) -> str:
    say(client, handle, name, text, chat_id="dm")
    return outbox(client, handle)[-1]["text"]


def outbox(client: TestClient, key: str) -> list[dict]:
    resp = client.get(f"/sim/outbox/{key}")
    assert resp.status_code == 200
    return resp.json()


def leaks(text: str, limit: int) -> bool:
    patterns = [
        rf"\$\s?{limit}(\.\d{{2}})?\b",
        rf"\b{limit}(\.00)?\s*(dollars|bucks|usd)\b",
        rf"\b{limit}\.00\b",
    ]
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def winning_plan(client: TestClient) -> tuple[Plan, dict[str, str]]:
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


def test_demo_scenario(harness) -> None:
    client, llm = harness

    # Everyone has spoken in the group, so the bot knows the members.
    for name, handle, *_ in PERSONAS:
        say(client, handle, name, "hey")
    assert outbox(client, GROUP)[0]["text"].startswith("Hi! I'm")

    # Private onboarding over DM.
    for name, handle, code, limit, start, modes in PERSONAS:
        assert "bank code" in dm(client, handle, name, "start")
        assert f"${limit}" in dm(client, handle, name, code)
        assert "Where are you starting" in dm(client, handle, name, "yes")
        assert "Got it" in dm(client, handle, name, start)
        assert "car" in dm(client, handle, name, "yes")
        assert "You're set" in dm(client, handle, name, modes)
    assert outbox(client, GROUP)[-1]["text"] == "✅ Jordan is set (3/3)."

    # §18 group chat.
    maya, sam, jordan = ((p[0], p[1]) for p in PERSONAS)
    say(client, maya[1], maya[0], "@plan")
    assert outbox(client, GROUP)[-1]["text"].startswith("Listening")
    say(client, maya[1], maya[0], "I'm starving")
    say(client, sam[1], sam[0], "no sushi pls")
    say(client, jordan[1], jordan[0], "I have to be back by 9, and nothing too far")
    say(client, sam[1], sam[0], "something we haven't tried?")
    say(client, maya[1], maya[0], "@go")

    # The LLM saw only pseudonymous text: no names, handles, or limits.
    (transcript,) = llm.transcripts
    assert len(transcript) == 4
    assert {m.pid for m in transcript} <= {"p1", "p2", "p3"}
    for name, handle, *_ in PERSONAS:
        assert all(name not in m.text and handle not in m.text for m in transcript)

    group_msgs = outbox(client, GROUP)
    polls = [m for m in group_msgs if m["poll"]]
    assert len(polls) == 1
    options = polls[0]["poll"]
    assert 1 <= len(options) <= 3
    assert all("Plum Tree" not in o["title"] for o in options)  # sushi veto respected
    assert "Reply A" in polls[0]["text"]

    # Two of three vote A → winner.
    client.post("/sim/vote", json={"chat_id": GROUP, "voter_handle": maya[1], "option_label": "A"})
    say(client, sam[1], sam[0], "A")
    group_msgs = outbox(client, GROUP)
    assert group_msgs[-1]["text"].startswith("🎉 Plan A:")

    # No private values in any group message.
    for msg in group_msgs:
        text = msg["text"] + json.dumps(msg["poll"] or [])
        for _, handle, _, limit, _, _ in PERSONAS:
            assert not leaks(text, limit), text
            assert handle not in text and handle[-4:] not in text
        for label in ORIGIN_LABELS:
            assert label not in text

    # A distinct personal DM per member, all arriving at the same time.
    itineraries = {handle: outbox(client, handle)[-1]["text"] for _, handle, *_ in PERSONAS}
    assert all(t.startswith("Your plan for tonight:") for t in itineraries.values())
    assert len(set(itineraries.values())) == len(PERSONAS)
    arrivals = {re.search(r"Arrive ~(\d+:\d\d)", t).group(1) for t in itineraries.values()}
    assert len(arrivals) == 1
    assert arrivals.pop() in group_msgs[-1]["text"]

    # Every leave_by + travel time reaches the same T_target, within each budget.
    plan, pid_to_handle = winning_plan(client)
    limit_by_handle = {handle: limit for _, handle, _, limit, _, _ in PERSONAS}
    assert len(plan.assignments) == len(PERSONAS)
    for a in plan.assignments:
        assert a.arrive_at == plan.target_arrival
        reached = a.leave_by + timedelta(minutes=a.travel_min)
        assert abs((reached - plan.target_arrival).total_seconds()) < 1
        assert a.total_cost_usd <= limit_by_handle[pid_to_handle[a.pid]]
