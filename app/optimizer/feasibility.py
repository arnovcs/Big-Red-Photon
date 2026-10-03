"""Hard filters (§13.3). A combination is infeasible if ANY of these holds:

1. Budget: venue cost (high end) + fare > the person's limit; unknown venue prices must
   also be ≤ unknown_cost_limit_fraction × limit.
2. Veto: the venue's cuisine or category matches any VETO.
3. Hours: known closed, or closes before T_target + typical duration.
4. Available until (HARD): T_target + typical duration + trip home > the deadline.
5. Max walk (HARD): walking minutes > the stated maximum.
6. Max travel (HARD): trip minutes > the stated maximum.
7. Mode: not allowed by TravelModes, or refused by a HARD mode preference.

Only HARD constraints (and VETOs) filter. SOFT and INFERRED ones only affect the score.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.models.candidates import Candidate, Uncertain
from app.models.conversation import (
    ConstraintField,
    ConstraintKind,
    ExtractedConstraint,
    GroupPreferences,
)
from app.models.private import TravelModes
from app.models.routing import Mode, RouteEstimate
from app.optimizer.arrival import parse_hhmm
from app.optimizer.params import OptimizerParams


def values_of(c: ExtractedConstraint) -> list[str]:
    raw = c.value if isinstance(c.value, list) else [c.value]
    return [str(v).strip().lower() for v in raw]


def _number(c: ExtractedConstraint) -> float | None:
    try:
        value = float(values_of(c)[0])
    except (ValueError, IndexError):
        return None
    return value if value > 0 else None


@dataclass(frozen=True)
class HardLimits:
    """One person's HARD constraints, resolved once per planning run."""

    max_walk_min: float | None = None
    max_travel_min: float | None = None
    deadline: datetime | None = None  # must be back by this time (local)
    only_modes: frozenset[Mode] | None = None  # HARD "want" mode preferences
    refused_modes: frozenset[Mode] = frozenset()  # HARD "avoid" mode preferences


def local_deadline(value: object, now: datetime) -> datetime | None:
    """ "HH:MM" → that time today, or tomorrow if it has already passed ("back by 1")."""
    t = parse_hhmm(value)
    if t is None:
        return None
    deadline = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    return deadline if deadline > now else deadline + timedelta(days=1)


def _modes(values: list[str]) -> set[Mode]:
    return {Mode(v) for v in values if v in Mode._value2member_map_}


def hard_limits(pid: str, preferences: GroupPreferences, now: datetime) -> HardLimits:
    """The strictest value wins when a person states the same HARD limit twice."""
    max_walk = max_travel = None
    deadline = None
    only: set[Mode] | None = None
    refused: set[Mode] = set()
    for c in preferences.constraints:
        if c.pid != pid or c.kind != ConstraintKind.HARD:
            continue
        if c.field == ConstraintField.MAX_WALK_MIN and (n := _number(c)) is not None:
            max_walk = n if max_walk is None else min(max_walk, n)
        elif c.field == ConstraintField.MAX_TRAVEL_MIN and (n := _number(c)) is not None:
            max_travel = n if max_travel is None else min(max_travel, n)
        elif c.field == ConstraintField.AVAILABLE_UNTIL and c.polarity == "want":
            d = local_deadline(c.value, now)
            if d is not None:
                deadline = d if deadline is None else min(deadline, d)
        elif c.field == ConstraintField.MODE_PREFERENCE:
            modes = _modes(values_of(c))
            if c.polarity == "avoid":
                refused |= modes
            elif modes:
                only = modes if only is None else only & modes
    return HardLimits(
        max_walk_min=max_walk,
        max_travel_min=max_travel,
        deadline=deadline,
        only_modes=frozenset(only) if only is not None else None,
        refused_modes=frozenset(refused),
    )


# --- venue-level -----------------------------------------------------------------


def is_vetoed(candidate: Candidate, preferences: GroupPreferences) -> bool:
    cuisines = {x.lower() for x in candidate.cuisines}
    for c in preferences.constraints:
        if c.kind != ConstraintKind.VETO:
            continue
        if c.field not in (ConstraintField.CUISINE, ConstraintField.CATEGORY):
            continue
        for value in values_of(c):
            if value in cuisines or value == candidate.category.lower():
                return True
    return False


def is_known_closed(candidate: Candidate) -> bool:
    return candidate.open_at_target == "closed"


def venue_cost(candidate: Candidate) -> tuple[Decimal | None, bool]:
    """(cost used for budget checks, price unknown?). Uses `high` when known/estimated.

    None means the price is unknown and there isn't even a typical value to check.
    """
    est = candidate.est_cost_pp
    if est.status == "unknown":
        return est.value, True
    if est.high is not None:
        return est.high, False
    return est.value, False


# --- person-level ----------------------------------------------------------------


def allowed_modes(modes: TravelModes) -> list[Mode]:
    return [m for m in Mode if getattr(modes, m.value)]


def mode_ok(mode: Mode, limits: HardLimits) -> bool:
    if mode in limits.refused_modes:
        return False
    return limits.only_modes is None or mode in limits.only_modes


def trip_ok(estimate: RouteEstimate, limits: HardLimits) -> bool:
    if limits.max_walk_min is not None and estimate.walk_min > limits.max_walk_min:
        return False
    return limits.max_travel_min is None or estimate.duration_min <= limits.max_travel_min


def fare_amount(fare: Uncertain[Decimal]) -> Decimal:
    if fare.high is not None:
        return fare.high
    return fare.value or Decimal(0)


def within_budget(
    total_cost: Decimal,
    venue_cost_usd: Decimal,
    price_unknown: bool,
    limit: Decimal,
    params: OptimizerParams,
) -> bool:
    if price_unknown and venue_cost_usd > limit * Decimal(str(params.unknown_cost_limit_fraction)):
        return False
    return total_cost <= limit


# --- combination-level (needs T_target) ------------------------------------------


def schedule_ok(
    candidate: Candidate,
    target: datetime,
    trips: dict[str, float],
    limits: dict[str, HardLimits],
) -> bool:
    """Hours and HARD available_until, given the shared arrival time and each trip length.

    The trip home is estimated as the same length as the trip there.
    """
    visit_end = target + timedelta(minutes=candidate.typical_duration_min)
    if candidate.closes_at is not None and candidate.closes_at < visit_end:
        return False
    for pid, duration_min in trips.items():
        deadline = limits[pid].deadline
        if deadline is not None and visit_end + timedelta(minutes=duration_min) > deadline:
            return False
    return True
