"""Hard filters (§13.3). Stage 1: budget, veto, allowed/refused modes.

Stage 2 adds hours, available_until, max walk, and max travel.
"""

from decimal import Decimal

from app.models.candidates import Candidate, Uncertain
from app.models.conversation import (
    ConstraintField,
    ConstraintKind,
    ExtractedConstraint,
    GroupPreferences,
)
from app.models.private import TravelModes
from app.models.routing import Mode
from app.optimizer.params import OptimizerParams


def values_of(c: ExtractedConstraint) -> list[str]:
    raw = c.value if isinstance(c.value, list) else [c.value]
    return [str(v).strip().lower() for v in raw]


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


def allowed_modes(modes: TravelModes) -> list[Mode]:
    return [m for m in Mode if getattr(modes, m.value)]


def mode_refused(pid: str, mode: Mode, preferences: GroupPreferences) -> bool:
    return any(
        c.pid == pid
        and c.kind == ConstraintKind.HARD
        and c.field == ConstraintField.MODE_PREFERENCE
        and c.polarity == "avoid"
        and mode.value in values_of(c)
        for c in preferences.constraints
    )


def venue_cost(candidate: Candidate) -> tuple[Decimal, bool]:
    """(cost used for budget checks, price unknown?). Uses `high` when known/estimated."""
    est = candidate.est_cost_pp
    if est.status == "unknown":
        return (est.value or Decimal(0)), True
    if est.high is not None:
        return est.high, False
    return (est.value or Decimal(0)), False


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
