"""Small builders for optimizer tests: candidates, estimates, people, constraints."""

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.models.candidates import DEFAULT_DURATION_MIN, Candidate, Uncertain
from app.models.conversation import (
    ConstraintField,
    ConstraintKind,
    ExtractedConstraint,
    GroupPreferences,
)
from app.models.private import LatLng, PrivateConstraints, TravelModes
from app.models.routing import Mode, RouteEstimate
from app.optimizer import OptimizerParams, rank
from app.optimizer.enumerate import EstimateIndex
from app.providers.costs import tier_cost

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 3, 18, 0, tzinfo=TZ)
HERE = LatLng(lat=42.44, lng=-76.48)


def venue(
    cid: str,
    category: str = "food",
    cuisines: tuple[str, ...] = (),
    tier: str | None = "$",
    cost: Uncertain[Decimal] | None = None,
    **extra: object,
) -> Candidate:
    return Candidate(
        candidate_id=cid,
        name=cid.title(),
        category=category,
        cuisines=list(cuisines),
        location=HERE,
        address="1 Test St",
        est_cost_pp=cost if cost is not None else tier_cost(tier),
        typical_duration_min=DEFAULT_DURATION_MIN[category],
        source="osm_fixture",
        **extra,
    )


def trip(
    pid: str, cid: str, mode: Mode, minutes: float, fare: float = 0, walk: float | None = None
) -> RouteEstimate:
    return RouteEstimate(
        origin_pid=pid,
        candidate_id=cid,
        mode=mode,
        duration_min=minutes,
        distance_mi=1.0,
        walk_min=minutes if mode == Mode.WALK else (walk or 0.0),
        fare_usd=Uncertain[Decimal](
            value=Decimal(str(fare)),
            status="known" if fare == 0 else "estimated",
            source="formula",
        ),
        source="mock",
    )


def person(pid: str, limit: float = 50, **modes: bool) -> PrivateConstraints:
    return PrivateConstraints(
        pid=pid, spend_limit_usd=Decimal(str(limit)), origin=HERE, modes=TravelModes(**modes)
    )


def pref(
    pid: str,
    field: ConstraintField,
    value: str | float | list[str],
    kind: ConstraintKind,
    polarity: str = "want",
    confidence: float = 0.9,
) -> ExtractedConstraint:
    return ExtractedConstraint(
        pid=pid,
        field=field,
        value=value,
        polarity=polarity,
        kind=kind,
        confidence=confidence,
        evidence_msg_ids=["m1"],
    )


def prefs(*constraints: ExtractedConstraint) -> GroupPreferences:
    return GroupPreferences(constraints=list(constraints), group_intent="food")


def index(*estimates: RouteEstimate) -> EstimateIndex:
    return {(e.origin_pid, e.candidate_id, e.mode): e for e in estimates}


def run(
    candidates: list[Candidate],
    estimates: EstimateIndex,
    people: list[PrivateConstraints],
    preferences: GroupPreferences | None = None,
    lam: float = 0.5,
):
    return rank(
        candidates,
        estimates,
        {p.pid: p for p in people},
        preferences or prefs(),
        NOW,
        OptimizerParams(lam=lam),
    )
