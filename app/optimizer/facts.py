"""Group-safe facts for explanations (§13.8) and the "nothing fits" hint (§10 step 5).

Facts are aggregates over the whole group: no pids, names, per-person costs or times,
limits, or origins. They are the only input the explanation LLM sees.
"""

import re

from app.conversation import copy
from app.models.candidates import Candidate
from app.models.conversation import (
    ConstraintField,
    ConstraintKind,
    ExtractedConstraint,
    GroupPreferences,
)
from app.models.plans import Plan
from app.optimizer.arrival import whole_minutes
from app.optimizer.burden import satisfies
from app.optimizer.feasibility import values_of

OPTION_LABELS = ("A", "B", "C")

# Preference fields that describe venues, so they can be named to the whole group
# without pointing at anyone. Times, walking/travel limits and modes are personal.
# Novelty ("something new") isn't a venue fact we have (Google has no such data).
_VENUE_FIELDS = (ConstraintField.CUISINE, ConstraintField.CATEGORY, ConstraintField.ACTIVITY)
_SAFE_VALUE = re.compile(r"^[a-z][a-z _-]{0,29}$")


def _safe_value(value: str) -> str | None:
    """A short plain word or phrase ("korean", "ice cream"), or None."""
    value = value.strip().lower().replace("_", " ")
    return value if _SAFE_VALUE.match(value) else None


def max_travel_min(plan: Plan) -> int:
    return whole_minutes(max(a.travel_min for a in plan.assignments))


def _vetoes(preferences: GroupPreferences) -> list[str]:
    vetoed = {
        safe
        for c in preferences.constraints
        if c.kind == ConstraintKind.VETO
        and c.field in (ConstraintField.CUISINE, ConstraintField.CATEGORY)
        for v in values_of(c)
        if (safe := _safe_value(v))
    }
    return sorted(vetoed)


def _wants_matched(candidate: Candidate, preferences: GroupPreferences) -> list[str]:
    matched: set[str] = set()
    for c in preferences.constraints:
        if c.kind not in (ConstraintKind.SOFT, ConstraintKind.INFERRED):
            continue
        if c.field not in _VENUE_FIELDS or c.polarity != "want":
            continue
        if not satisfies(c, candidate):
            continue
        matched.update(safe for v in values_of(c) if (safe := _safe_value(v)))
    return sorted(matched)


def plan_facts(plans: list[Plan], preferences: GroupPreferences) -> list[dict]:
    """One fact dict per plan, in poll order (A, B, C)."""
    vetoes = _vetoes(preferences)
    travel = [max_travel_min(p) for p in plans]
    facts = []
    for i, (label, plan) in enumerate(zip(OPTION_LABELS, plans, strict=False)):
        cand = plan.candidate
        facts.append(
            {
                "label": label,
                "venue": cand.name,
                "category": cand.category,
                "cuisine": cand.cuisines[0] if cand.cuisines else None,
                "fits_all_budgets": True,  # infeasible plans never reach the poll
                "max_travel_min": travel[i],
                "next_best_max_travel_min": travel[i + 1] if i + 1 < len(travel) else None,
                "arrival_window_min": round(plan.score.arrival_spread_min),
                "vetoes_respected": vetoes,
                "matches_group_wants": _wants_matched(cand, preferences),
            }
        )
    return facts


def _share_satisfied(c: ExtractedConstraint, candidates: list[Candidate]) -> float:
    hits = sum(1 for cand in candidates if satisfies(c, cand) == (c.polarity == "want"))
    return hits / len(candidates)


def nothing_fits_hint(preferences: GroupPreferences, candidates: list[Candidate]) -> str | None:
    """Name the most binding SOFT venue preference: the one the fewest nearby venues meet.

    Ties go to the higher-confidence preference. Never names a person, a time, a limit,
    or a place, and returns None if no SOFT venue preference was stated.
    """
    if not candidates:
        return None
    scored = []
    for c in preferences.constraints:
        if c.kind != ConstraintKind.SOFT or c.field not in _VENUE_FIELDS:
            continue
        value = next((s for v in values_of(c) if (s := _safe_value(v))), None)
        if value is None:
            continue
        scored.append((_share_satisfied(c, candidates), -c.confidence, value, c, value))
    if not scored:
        return None
    _, _, _, constraint, value = min(scored, key=lambda row: row[:3])
    return copy.loosen_hint(constraint.field.value, constraint.polarity, value)
