"""Planning pipeline (§10): snapshot → extract → discover → route → optimize → poll."""

import asyncio
import json
import random
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import optimizer
from app.conversation import copy
from app.db import queries
from app.db.tables import GroupRow, SessionMessageRow, SessionRow, UserRow
from app.decision import poll
from app.deps import Deps
from app.logging import get_logger, kv
from app.messaging.outbound import send_group
from app.models.candidates import Candidate
from app.models.conversation import (
    ConstraintField,
    ConstraintKind,
    GroupPreferences,
    PseudonymousMessage,
)
from app.models.identity import OnboardingState
from app.models.outbound import GroupSafeMessage
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


async def ready_members(db: AsyncSession, group: GroupRow) -> list[UserRow]:
    members = await queries.group_members(db, group.id)
    return [m for m in members if m.onboarding_state == OnboardingState.READY]


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


async def run(deps: Deps, db: AsyncSession, group: GroupRow, session: SessionRow) -> list[Plan]:
    """Run planning and send the poll. Returns the polled plans ([] if nothing was sent).

    Raises on unexpected errors; the caller sends the retry message.
    """
    settings = deps.settings
    tz = ZoneInfo(settings.demo_timezone)
    now_local = deps.clock().astimezone(tz)

    # 1. Snapshot
    members = await ready_members(db, group)
    pid_map = make_pid_map([m.id for m in members])
    constraints = await vault.constraints_for(db, pid_map)
    pid_map = {pid: uid for pid, uid in pid_map.items() if pid in constraints}
    if len(pid_map) < 2:
        await send_group(
            deps.messaging, group.chat_id, GroupSafeMessage(text=copy.NEED_MORE_PEOPLE)
        )
        return []
    user_to_pid = {uid: pid for pid, uid in pid_map.items()}
    transcript = await _transcript(db, session, user_to_pid, tz)
    session.pid_map_json = json.dumps({pid: str(uid) for pid, uid in pid_map.items()})
    await db.commit()  # release the SQLite read lock before slow provider calls

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
    top = optimizer.select(ranked, k=3)
    log.info(kv("pipeline_ranked", candidates=len(candidates), feasible=len(ranked)))

    session.preferences_json = preferences.model_dump_json()
    if not top:
        await send_group(deps.messaging, group.chat_id, GroupSafeMessage(text=copy.NOTHING_FITS))
        return []

    # 6–7. Explain (template blurbs in Stage 1) + send the poll
    message = poll.build_poll_message(top)
    session.poll_id = await send_group(deps.messaging, group.chat_id, message)
    session.plans_json = json.dumps([p.model_dump(mode="json") for p in top])
    return top
