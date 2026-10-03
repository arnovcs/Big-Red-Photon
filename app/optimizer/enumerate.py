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
    HardLimits,
    allowed_modes,
    fare_amount,
    hard_limits,
    is_known_closed,
    is_vetoed,
    mode_ok,
    schedule_ok,
    trip_ok,
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
    cost: Decimal,
    price_unknown: bool,
    constraint: PrivateConstraints,
    limits: HardLimits,
    estimates: EstimateIndex,
    preferences: GroupPreferences,
    tolerance: float,
    params: OptimizerParams,
) -> list[_Option]:
    """Modes that pass every person-level hard filter (mode, walk, travel, budget)."""
    options = []
    for mode in allowed_modes(constraint.modes):
        est = estimates.get((pid, candidate.candidate_id, mode))
        if est is None or not mode_ok(mode, limits) or not trip_ok(est, limits):
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
    cost: Decimal,
    price_unknown: bool,
    combo: tuple[_Option, ...],
    score: PlanScore,
    target: datetime,
) -> Plan:
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


# Activities with no price at all (Google has none and it's not a usually-paid kind of
# place) count as free, flagged "price unknown"; budgets only bite where cost matters.
FREE_UNLESS_PRICED = {"activity", "sports"}


def unknown_price_stand_in(candidates: list[Candidate]) -> Decimal | None:
    """Median budget-check cost of the venues that do have a price (None if none do)."""
    costs = sorted(c for c, unknown in map(venue_cost, candidates) if c is not None and not unknown)
    if not costs:
        return None
    mid = len(costs) // 2
    return costs[mid] if len(costs) % 2 else (costs[mid - 1] + costs[mid]) / 2


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
    limits = {pid: hard_limits(pid, preferences, now) for pid in pids}

    stand_in = unknown_price_stand_in(candidates)
    plans = []
    for candidate in candidates:
        cost, price_unknown = venue_cost(candidate)
        if cost is None and price_unknown:
            if candidate.category in FREE_UNLESS_PRICED:
                cost = Decimal(0)  # parks, courts, museums...: counted free, flagged unknown
            else:
                # Food/drink with no price: what nearby venues actually cost (never a
                # category guess), under the unknown-price safety margin.
                cost = stand_in
        if cost is None or is_vetoed(candidate, preferences) or is_known_closed(candidate):
            continue  # no price at all nearby to check budgets against, vetoed, or closed
        per_person = [
            _person_options(
                pid,
                candidate,
                cost,
                price_unknown,
                constraints[pid],
                limits[pid],
                estimates,
                preferences,
                tolerance[pid],
                params,
            )
            for pid in pids
        ]
        if any(not options for options in per_person):
            continue

        best: tuple[float, float, int, tuple[_Option, ...], PlanScore, datetime] | None = None
        for i, combo in enumerate(itertools.product(*per_person)):
            trips = {o.pid: o.estimate.duration_min for o in combo}
            target = arrival.target_arrival(ready, trips)
            if not schedule_ok(candidate, target, trips, limits):
                continue
            score = group_score([o.burden.total for o in combo], params)
            key = (score.J, score.burden_spread, i)
            if best is None or key < best[:3]:
                best = (*key, combo, score, target)
        if best is not None:
            _, _, _, combo, score, target = best
            plans.append(_build_plan(candidate, cost, price_unknown, combo, score, target))

    plans.sort(key=lambda p: (p.score.J, p.score.burden_spread, p.candidate.candidate_id))
    return plans
