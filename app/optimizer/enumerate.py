"""Enumerate venue × per-person mode combinations and rank venues (§13.1, §13.5)."""

import itertools
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.models.candidates import Candidate
from app.models.conversation import GroupPreferences
from app.models.plans import BurdenBreakdown, PersonAssignment, Plan, PlanScore
from app.models.private import PrivateConstraints
from app.models.routing import Mode, RouteEstimate
from app.optimizer import arrival
from app.optimizer.burden import person_burden, preference_satisfaction, travel_tolerance
from app.optimizer.feasibility import (
    allowed_modes,
    fare_amount,
    is_vetoed,
    mode_refused,
    venue_cost,
    within_budget,
)
from app.optimizer.params import OptimizerParams
from app.optimizer.score import group_score

EstimateIndex = dict[tuple[str, str, Mode], RouteEstimate]


@dataclass(frozen=True)
class _Option:
    """One feasible mode for one person at one venue."""

    pid: str
    estimate: RouteEstimate
    fare: Decimal
    total_cost: Decimal
    burden: BurdenBreakdown


def _person_options(
    pid: str,
    candidate: Candidate,
    constraint: PrivateConstraints,
    estimates: EstimateIndex,
    preferences: GroupPreferences,
    tolerance: float,
    params: OptimizerParams,
) -> list[_Option]:
    cost, price_unknown = venue_cost(candidate)
    options = []
    for mode in allowed_modes(constraint.modes):
        est = estimates.get((pid, candidate.candidate_id, mode))
        if est is None or mode_refused(pid, mode, preferences):
            continue
        fare = fare_amount(est.fare_usd)
        total = cost + fare
        if not within_budget(total, cost, price_unknown, constraint.spend_limit_usd, params):
            continue
        sat = preference_satisfaction(pid, candidate, mode, preferences, params)
        burden = person_burden(
            total, constraint.spend_limit_usd, est.duration_min, tolerance, sat, params
        )
        options.append(_Option(pid, est, fare, total, burden))
    return options


def _build_plan(
    candidate: Candidate,
    combo: tuple[_Option, ...],
    score: PlanScore,
    ready: dict[str, datetime],
) -> Plan:
    durations = {o.pid: o.estimate.duration_min for o in combo}
    target = arrival.target_arrival(ready, durations)
    cost, price_unknown = venue_cost(candidate)
    assignments = [
        PersonAssignment(
            pid=o.pid,
            mode=o.estimate.mode,
            leave_by=arrival.leave_by(target, o.estimate.duration_min),
            arrive_at=target,
            travel_min=o.estimate.duration_min,
            walk_min=o.estimate.walk_min,
            venue_cost_usd=cost,
            fare_usd=o.fare,
            total_cost_usd=o.total_cost,
            cost_uncertain=price_unknown
            or candidate.est_cost_pp.status != "known"
            or o.estimate.fare_usd.status != "known",
            burden=o.burden,
        )
        for o in combo
    ]
    risk_flags = []
    if price_unknown:
        risk_flags.append("price unknown")
    elif candidate.est_cost_pp.status == "estimated":
        risk_flags.append("price estimated")
    return Plan(
        plan_id=candidate.candidate_id,
        candidate=candidate,
        target_arrival=target,
        assignments=assignments,
        score=score,
        risk_flags=risk_flags,
    )


def rank(
    candidates: list[Candidate],
    estimates: EstimateIndex,
    constraints: dict[str, PrivateConstraints],
    preferences: GroupPreferences,
    now: datetime,
    params: OptimizerParams,
) -> list[Plan]:
    """At most one plan per venue (its best mode assignment), sorted best first.

    `now` must be timezone-aware local time (used for "HH:MM" constraints).
    """
    pids = sorted(constraints)
    if not pids:
        return []
    tolerance = {pid: travel_tolerance(pid, preferences, params) for pid in pids}
    ready = {pid: arrival.ready_time(pid, preferences, now, params) for pid in pids}

    plans = []
    for candidate in candidates:
        if is_vetoed(candidate, preferences):
            continue
        per_person = [
            _person_options(
                pid, candidate, constraints[pid], estimates, preferences, tolerance[pid], params
            )
            for pid in pids
        ]
        if any(not options for options in per_person):
            continue

        best: tuple[float, float, int, tuple[_Option, ...], PlanScore] | None = None
        for i, combo in enumerate(itertools.product(*per_person)):
            score = group_score([o.burden.total for o in combo], params)
            key = (score.J, score.burden_spread, i)
            if best is None or key < best[:3]:
                best = (*key, combo, score)
        assert best is not None
        plans.append(_build_plan(candidate, best[3], best[4], ready))

    plans.sort(key=lambda p: (p.score.J, p.score.burden_spread, p.candidate.candidate_id))
    return plans
