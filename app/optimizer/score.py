"""Group objective (§13.5), phase 1: J = λ·max B + (1 − λ)·mean B. Lower is better."""

from app.models.plans import PlanScore
from app.optimizer.params import OptimizerParams


def group_score(burdens: list[float], params: OptimizerParams) -> PlanScore:
    worst = max(burdens)
    mean = sum(burdens) / len(burdens)
    return PlanScore(
        J=params.lam * worst + (1 - params.lam) * mean,
        max_burden=worst,
        mean_burden=mean,
        burden_spread=worst - min(burdens),
        arrival_spread_min=0.0,  # v2: deterministic modes, everyone arrives at T_target
    )
