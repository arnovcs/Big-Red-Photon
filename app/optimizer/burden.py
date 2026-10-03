"""Per-person burden (§13.4), phase 1: money, time, preference."""

from decimal import Decimal

from app.models.candidates import Candidate
from app.models.conversation import (
    ConstraintField,
    ConstraintKind,
    ExtractedConstraint,
    GroupPreferences,
)
from app.models.plans import BurdenBreakdown
from app.models.routing import Mode
from app.optimizer.feasibility import values_of
from app.optimizer.params import OptimizerParams

_SCORED_KINDS = (ConstraintKind.SOFT, ConstraintKind.INFERRED)


def travel_tolerance(pid: str, preferences: GroupPreferences, params: OptimizerParams) -> float:
    """τ_i = stated max_travel (any kind), else the default."""
    for c in preferences.constraints:
        if c.pid == pid and c.field == ConstraintField.MAX_TRAVEL_MIN:
            try:
                value = float(values_of(c)[0])
            except (ValueError, IndexError):
                continue
            if value > 0:
                return value
    return params.default_travel_tolerance_min


def _satisfied(c: ExtractedConstraint, candidate: Candidate, mode: Mode) -> bool | None:
    """Does the candidate satisfy this preference? None = not a scored preference."""
    values = values_of(c)
    if c.field == ConstraintField.CUISINE:
        return any(v in {x.lower() for x in candidate.cuisines} for v in values)
    if c.field == ConstraintField.CATEGORY:
        return candidate.category.lower() in values
    if c.field == ConstraintField.NOVELTY:
        return bool(candidate.novelty_tags)
    if c.field == ConstraintField.MODE_PREFERENCE:
        return mode.value in values
    return None


def preference_satisfaction(
    pid: str,
    candidate: Candidate,
    mode: Mode,
    preferences: GroupPreferences,
    params: OptimizerParams,
) -> float:
    """sat_i = Σ(conf × match) / Σ conf over i's SOFT + INFERRED prefs; neutral if none."""
    weighted, total = 0.0, 0.0
    for c in preferences.constraints:
        if c.pid != pid or c.kind not in _SCORED_KINDS:
            continue
        satisfied = _satisfied(c, candidate, mode)
        if satisfied is None:
            continue
        match = satisfied if c.polarity == "want" else not satisfied
        weighted += c.confidence * (1.0 if match else 0.0)
        total += c.confidence
    if total == 0:
        return params.neutral_pref_satisfaction
    return weighted / total


def person_burden(
    total_cost: Decimal,
    limit: Decimal,
    duration_min: float,
    tolerance_min: float,
    satisfaction: float,
    params: OptimizerParams,
) -> BurdenBreakdown:
    money = float(total_cost / limit) if limit > 0 else 1.0
    time = duration_min / tolerance_min
    pref = 1.0 - satisfaction
    total = params.w_money * money + params.w_time * time + params.w_pref * pref
    return BurdenBreakdown(money=money, time=time, pref=pref, total=total)
