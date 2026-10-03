"""Planning pipeline (§10): snapshot → extract → discover → route → optimize.

The session FSM sends the poll (steps 6–7) with the result.
"""

import asyncio
import json
import random
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
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
from app.messaging.guard import MemberSecrets, PrivacyGuard
from app.models.candidates import Candidate
from app.models.conversation import (
    ConstraintField,
    ConstraintKind,
    GroupPreferences,
    PseudonymousMessage,
)
from app.models.plans import Plan
from app.models.private import LatLng, PrivateConstraints
from app.optimizer import OptimizerParams, facts
from app.optimizer import cuisines as cuisine_families
from app.optimizer.enumerate import EstimateIndex
from app.optimizer.feasibility import allowed_modes
from app.planning import combos
from app.private import vault
from app.providers.mock.routing import haversine_mi

log = get_logger(__name__)

SEARCH_RADIUS_M = 50_000  # effectively none: Google returns the nearest matches
OPEN_AT_OFFSET_MIN = 30
DEPART_OFFSET_MIN = 5
MAX_CANDIDATES = 20
LLM_ATTEMPTS = 2

_INTENT_CATEGORIES = {
    "food": ["food", "cafe", "dessert"],
    # Bars only when someone asks for drinks or a bar (then "bar" is added below).
    "activity": ["activity", "sports"],
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
    """What kinds of place to search. A specific kind someone asked for ("sporty" →
    sports, "drinks" → bar, "coffee" → cafe) is searched on its own; the broad words
    ("activity", "food") and the overall intent give the wider mix."""
    asked: list[str] = []
    for c in preferences.constraints:
        if (
            c.field == ConstraintField.CATEGORY
            and c.polarity == "want"
            and c.kind != ConstraintKind.VETO
        ):
            values = c.value if isinstance(c.value, list) else [c.value]
            asked.extend(str(v).lower() for v in values if str(v).lower() not in asked)
    specific = [c for c in asked if c not in _INTENT_CATEGORIES]
    if specific:
        return specific
    categories = list(_INTENT_CATEGORIES.get(preferences.group_intent, []))
    for broad in asked:  # e.g. "food" said during an "activity" plan: both
        categories.extend(c for c in _INTENT_CATEGORIES[broad] if c not in categories)
    return categories  # [] ("either" / "unknown", nothing asked) → all categories


def _centroid(constraints: dict[str, PrivateConstraints]) -> LatLng:
    origins = [c.origin for c in constraints.values()]
    return LatLng(
        lat=sum(o.lat for o in origins) / len(origins),
        lng=sum(o.lng for o in origins) / len(origins),
    )


def _wanted_cuisines(preferences: GroupPreferences) -> list[str]:
    """Cuisines someone asked for ("I want japanese"), not ones they ruled out."""
    wanted: list[str] = []
    for c in preferences.constraints:
        if (
            c.field == ConstraintField.CUISINE
            and c.polarity == "want"
            and c.kind != ConstraintKind.VETO
        ):
            values = c.value if isinstance(c.value, list) else [c.value]
            wanted.extend(str(v).lower() for v in values if str(v).lower() not in wanted)
    return wanted


def _wanted_activities(preferences: GroupPreferences) -> list[str]:
    """Specific things someone asked to do ("pickleball"), not ones they ruled out."""
    wanted: list[str] = []
    for c in preferences.constraints:
        if (
            c.field == ConstraintField.ACTIVITY
            and c.polarity == "want"
            and c.kind != ConstraintKind.VETO
        ):
            values = c.value if isinstance(c.value, list) else [c.value]
            wanted.extend(str(v).lower() for v in values if str(v).lower() not in wanted)
    return wanted


def _shortlist(
    candidates: list[Candidate],
    wanted_cuisines: list[str],
    wanted_activities: list[str] | None = None,
    center: LatLng | None = None,
) -> list[Candidate]:
    """Drop known-closed; keep the top MAX_CANDIDATES.

    1. Venues matching a wanted cuisine or activity, in the search's own order (Google
       ranks "pickleball" results by relevance: courts before a gym that mentions it).
       If a specific activity was asked for and has matches, only those are kept.
    2. The rest, closest first, taking turns between kinds of place (parks, theaters,
       courts...) so a vague ask like "something fun" gets a mix, not 20 of one kind.
    There's no search radius, so distance is kept in mind here; the optimizer then
    weighs each person's actual travel time.
    """
    activities = set(wanted_activities or [])
    open_ = [c for c in candidates if c.open_at_target != "closed"]

    def matches(c: Candidate) -> bool:
        return cuisine_families.matches(wanted_cuisines, c.cuisines) or bool(
            activities & set(c.cuisines)
        )

    def distance(c: Candidate) -> float:
        return haversine_mi(center, c.location) if center else 0.0

    matched = [c for c in open_ if matches(c)]
    if activities and any(activities & set(c.cuisines) for c in matched):
        # A specific activity ("pickleball") was asked for and Google found places for it:
        # only those. The wider search is only a fallback when it finds none.
        return matched[:MAX_CANDIDATES]
    rest = sorted(
        (c for c in open_ if not matches(c)), key=lambda c: (distance(c), -(c.rating or 0))
    )
    # Take turns between kinds: each kind's nearest, then each kind's second nearest...
    turn: dict[str, int] = {}
    ranked_rest = []
    for c in rest:
        kind = c.cuisines[0] if c.cuisines else c.category
        ranked_rest.append((turn.get(kind, 0), distance(c), c))
        turn[kind] = turn.get(kind, 0) + 1
    ranked_rest.sort(key=lambda row: row[:2])
    return (matched + [c for _, _, c in ranked_rest])[:MAX_CANDIDATES]


@dataclass
class PlanningResult:
    pid_map: dict[str, uuid.UUID]
    preferences: GroupPreferences
    plans: list[Plan]  # top 3, best first; [] if nothing fits
    facts: list[dict] = field(default_factory=list)  # group-safe, one per plan (§13.8)
    hint: str | None = None  # "nothing fits" suggestion, when plans is []
    blurbs: list[str] = field(default_factory=list)  # checked explanations (§13.8)
    no_places: bool = False  # Google found nothing for what they asked (vs. nothing fits)
    # Members with no starting point (or travel answer): asked, never given a default.
    missing: list[uuid.UUID] = field(default_factory=list)


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
        # Plan from where people are now: anyone sharing their location gets it refreshed.
        await vault.refresh_shared_origins(
            db,
            {m.id: m.handle for m in members},
            deps.messaging,
            keep_typed_since=as_utc(session.started_at),  # a place typed this plan wins
        )
        await db.commit()
        pid_map = make_pid_map([m.id for m in members])
        constraints = await vault.constraints_for(db, pid_map)
        missing = [uid for pid, uid in pid_map.items() if pid not in constraints]
        pid_map = {pid: uid for pid, uid in pid_map.items() if pid in constraints}
        user_to_pid = {uid: pid for pid, uid in pid_map.items()}
        transcript = await _transcript(db, session, user_to_pid, tz)

    empty = GroupPreferences(constraints=[], group_intent="either")
    log.info(kv("planning_members", ready=len(pid_map), missing=len(missing)))
    if missing:
        # Someone has no starting point: ask them rather than plan without (or for) them.
        return PlanningResult(pid_map=pid_map, preferences=empty, plans=[], missing=missing)
    if len(pid_map) < 2:
        return PlanningResult(pid_map=pid_map, preferences=empty, plans=[])

    # 2. Extract
    preferences = await _extract(deps, transcript, now_local)
    # Each person's mode is their own answer ("How are you getting there?" or a later
    # "actually I'll drive"), so modes the LLM read from chat aren't applied twice
    # (an old "I'm walking" plus a new "I'll drive" would rule out every way there).
    preferences = preferences.model_copy(
        update={
            "constraints": [
                c for c in preferences.constraints if c.field != ConstraintField.MODE_PREFERENCE
            ]
        }
    )

    # 3. Discover
    wanted_cuisines = _wanted_cuisines(preferences)
    wanted_activities = _wanted_activities(preferences)
    categories = _categories(preferences)
    log.info(
        kv(
            "extracted",
            intent=preferences.group_intent,
            categories=",".join(categories) or "all",
            activities=",".join(wanted_activities),
            cuisines=",".join(wanted_cuisines),
            constraints=len(preferences.constraints),
        )
    )
    center = _centroid(constraints)
    candidates = await deps.places.search_nearby(
        center,
        SEARCH_RADIUS_M,
        categories,
        open_at=now_local + timedelta(minutes=OPEN_AT_OFFSET_MIN),
        cuisines=wanted_cuisines,
        activities=wanted_activities,
    )
    found = len(candidates)
    mixed_wants = None  # set when the poll must take turns between very different asks
    wants = combos.asks_by_person(preferences)
    if combos.needs_combos(candidates, wants):
        # Very different asks ("pickleball" + "boba"), nothing covers everyone: chain a
        # stop for each, walkable; else take turns between them. Fair to every ask.
        chained = combos.build(candidates, wants)
        mixed_wants = wants if not chained else None
        candidates = chained or combos.mixed(candidates, wants, MAX_CANDIDATES)
        log.info(kv("combo_plans", chained=len(chained), options=len(candidates)))
    else:
        candidates = _shortlist(candidates, wanted_cuisines, wanted_activities, center)
    log.info(kv("venues_filtered", found=found, shortlisted=len(candidates)))

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
    top = combos.pick_mixed(ranked, mixed_wants) if mixed_wants else optimizer.select(ranked, k=3)
    log.info(kv("pipeline_ranked", candidates=len(candidates), feasible=len(ranked)))

    # 6. Explain (LLM phrasing → number check → template fallback)
    plan_facts = facts.plan_facts(top, preferences)
    return PlanningResult(
        pid_map=pid_map,
        preferences=preferences,
        plans=top,
        facts=plan_facts,
        hint=None if top else facts.nothing_fits_hint(preferences, candidates),
        no_places=not candidates,
        # The poll is just the options (no one-line pitch each), so no Gemini call here.
        blurbs=["" for _ in plan_facts],
    )


def save_result(session: SessionRow, result: PlanningResult) -> None:
    session.pid_map_json = json.dumps({pid: str(uid) for pid, uid in result.pid_map.items()})
    session.preferences_json = result.preferences.model_dump_json()
    session.plans_json = json.dumps([p.model_dump(mode="json") for p in result.plans])


async def build_guard(
    db: AsyncSession,
    group_id: uuid.UUID,
    session_id: uuid.UUID | None,
    public_terms: list[str] | None = None,
) -> PrivacyGuard:
    """PrivacyGuard for one virtual group: every member's secrets plus the preference DMs
    collected in this session. Build it before the session's messages are deleted."""
    members = await queries.group_members(db, group_id)
    secrets = await vault.guard_secrets_for(db, [m.id for m in members])
    texts: dict[uuid.UUID, list[str]] = defaultdict(list)
    if session_id is not None:
        rows = await db.scalars(
            select(SessionMessageRow).where(SessionMessageRow.session_id == session_id)
        )
        for row in rows:
            texts[row.sender_user_id].append(row.text)
    return PrivacyGuard(
        members=[
            MemberSecrets(
                handle=m.handle,
                spend_limit_usd=secrets[m.id].spend_limit_usd if m.id in secrets else None,
                origin_label=secrets[m.id].origin_label if m.id in secrets else None,
                nessie_customer_id=secrets[m.id].nessie_customer_id if m.id in secrets else None,
                preference_texts=tuple(texts[m.id]),
            )
            for m in members
        ],
        public_terms=public_terms or [],
    )


def venue_terms(plans: list[Plan]) -> list[str]:
    """Public venue names and addresses, exempt from the guard's scan."""
    stops = [s for p in plans for s in (p.candidate, *p.candidate.extra_stops)]
    return [t for s in stops for t in (s.name, s.address)]
