"""Planning pipeline (§10): snapshot → extract → discover → route → optimize.

The session FSM sends the poll (steps 6–7) with the result.
"""

import asyncio
import json
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import optimizer
from app.db import queries
from app.db.session import session_factory
from app.db.tables import SessionMessageRow, SessionRow
from app.deps import Deps
from app.logging import get_logger, kv
from app.models.candidates import Candidate
from app.models.conversation import (
    ConstraintField,
    ConstraintKind,
    GroupPreferences,
    PseudonymousMessage,
)
from app.models.plans import Plan
from app.models.private import LatLng, PrivateConstraints
from app.optimizer import OptimizerParams
from app.optimizer.enumerate import EstimateIndex
from app.optimizer.feasibility import allowed_modes
from app.private import vault

log = get_logger(__name__)

SEARCH_RADIUS_M = 2500
OPEN_AT_OFFSET_MIN = 30
DEPART_OFFSET_MIN = 5
MAX_CANDIDATES = 20
LLM_ATTEMPTS = 2

_INTENT_CATEGORIES = {
    "food": ["food", "cafe", "dessert"],
    "activity": ["activity", "event", "bar"],
}


def as_utc(dt: datetime) -> datetime:
    """SQLite drops tzinfo; stored times are UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def make_pid_map(user_ids: list[uuid.UUID]) -> dict[str, uuid.UUID]:
    shuffled = list(user_ids)
    random.shuffle(shuffled)
    return {f"p{i}": user_id for i, user_id in enumerate(shuffled, start=1)}


def load_pid_map(session: SessionRow) -> dict[str, uuid.UUID]:
    raw = json.loads(session.pid_map_json or "{}")
    return {pid: uuid.UUID(user_id) for pid, user_id in raw.items()}


def load_plans(session: SessionRow) -> list[Plan]:
    return [Plan.model_validate(p) for p in json.loads(session.plans_json or "[]")]


async def _transcript(
    db: AsyncSession, session: SessionRow, user_to_pid: dict[uuid.UUID, str], tz: ZoneInfo
) -> list[PseudonymousMessage]:
    rows = await db.scalars(
        select(SessionMessageRow)
        .where(SessionMessageRow.session_id == session.id)
        .order_by(SessionMessageRow.ts, SessionMessageRow.id)
    )
    return [
        PseudonymousMessage(
            message_id=row.message_id,
            pid=user_to_pid[row.sender_user_id],
            text=row.text,
            ts_local=as_utc(row.ts).astimezone(tz).strftime("%H:%M"),
        )
        for row in rows
        if row.sender_user_id in user_to_pid
    ]


async def _extract(
    deps: Deps, transcript: list[PseudonymousMessage], now_local: datetime
) -> GroupPreferences:
    empty = GroupPreferences(constraints=[], group_intent="either")
    if deps.llm is None or not transcript:
        return empty
    for attempt in range(1, LLM_ATTEMPTS + 1):
        try:
            return await asyncio.wait_for(
                deps.llm.extract_preferences(transcript, now_local),
                timeout=deps.settings.llm_timeout_sec,
            )
        except Exception:
            log.warning(kv("extract_failed", attempt=attempt), exc_info=True)
    return empty


def _categories(preferences: GroupPreferences) -> list[str]:
    categories = list(_INTENT_CATEGORIES.get(preferences.group_intent, []))
    if not categories:
        return []  # "either" / "unknown" → all categories
    for c in preferences.constraints:
        if (
            c.field == ConstraintField.CATEGORY
            and c.polarity == "want"
            and c.kind != ConstraintKind.VETO
        ):
            values = c.value if isinstance(c.value, list) else [c.value]
            categories.extend(str(v).lower() for v in values if str(v).lower() not in categories)
    return categories


def _centroid(constraints: dict[str, PrivateConstraints]) -> LatLng:
    origins = [c.origin for c in constraints.values()]
    return LatLng(
        lat=sum(o.lat for o in origins) / len(origins),
        lng=sum(o.lng for o in origins) / len(origins),
    )


def _shortlist(candidates: list[Candidate]) -> list[Candidate]:
    """Drop known-closed; keep the top MAX_CANDIDATES by rating (stable order)."""
    open_ = [c for c in candidates if c.open_at_target != "closed"]
    open_.sort(key=lambda c: -(c.rating or 0))
    return open_[:MAX_CANDIDATES]


@dataclass
class PlanningResult:
    pid_map: dict[str, uuid.UUID]
    preferences: GroupPreferences
    plans: list[Plan]  # top 3, best first; [] if nothing fits


async def compute(deps: Deps, group_id: uuid.UUID, session_id: uuid.UUID) -> PlanningResult:
    """Steps 1–5 of §10: no sends and no session state changes (the caller does those).

    Runs outside the router lock, so it reads its snapshot in its own short DB session.
    If fewer than 2 members have complete profiles, returns early with that pid_map.
    """
    settings = deps.settings
    tz = ZoneInfo(settings.demo_timezone)
    now_local = deps.clock().astimezone(tz)

    # 1. Snapshot
    async with session_factory()() as db:
        session = await db.get(SessionRow, session_id)
        if session is None:
            raise LookupError("session not found")
        members = await queries.group_members(db, group_id)
        pid_map = make_pid_map([m.id for m in members])
        constraints = await vault.constraints_for(db, pid_map)
        pid_map = {pid: uid for pid, uid in pid_map.items() if pid in constraints}
        user_to_pid = {uid: pid for pid, uid in pid_map.items()}
        transcript = await _transcript(db, session, user_to_pid, tz)

    empty = GroupPreferences(constraints=[], group_intent="either")
    if len(pid_map) < 2:
        return PlanningResult(pid_map=pid_map, preferences=empty, plans=[])

    # 2. Extract
    preferences = await _extract(deps, transcript, now_local)

    # 3. Discover
    candidates = await deps.places.search_nearby(
        _centroid(constraints),
        SEARCH_RADIUS_M,
        _categories(preferences),
        open_at=now_local + timedelta(minutes=OPEN_AT_OFFSET_MIN),
    )
    candidates = _shortlist(candidates)

    # 4. Route
    estimates = await deps.routing.matrix(
        origins={pid: c.origin for pid, c in constraints.items()},
        destinations={cand.candidate_id: cand.location for cand in candidates},
        modes={pid: set(allowed_modes(c.modes)) for pid, c in constraints.items()},
        depart_at=now_local + timedelta(minutes=DEPART_OFFSET_MIN),
    )
    index: EstimateIndex = {(e.origin_pid, e.candidate_id, e.mode): e for e in estimates}

    # 5. Optimize
    params = OptimizerParams(lam=settings.optimizer_lambda)
    ranked = optimizer.rank(candidates, index, constraints, preferences, now_local, params)
    log.info(kv("pipeline_ranked", candidates=len(candidates), feasible=len(ranked)))
    return PlanningResult(
        pid_map=pid_map, preferences=preferences, plans=optimizer.select(ranked, k=3)
    )


def save_result(session: SessionRow, result: PlanningResult) -> None:
    session.pid_map_json = json.dumps({pid: str(uid) for pid, uid in result.pid_map.items()})
    session.preferences_json = result.preferences.model_dump_json()
    session.plans_json = json.dumps([p.model_dump(mode="json") for p in result.plans])
