"""Plan models (§6.6)."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from app.models.candidates import Candidate
from app.models.routing import Mode


class BurdenBreakdown(BaseModel):
    money: float
    time: float
    pref: float
    walk: float = 0.0  # phase 2
    sched: float = 0.0  # phase 2
    total: float


class PersonAssignment(BaseModel):
    pid: str
    mode: Mode
    leave_by: datetime
    arrive_at: datetime
    travel_min: float
    walk_min: float
    venue_cost_usd: Decimal
    fare_usd: Decimal
    total_cost_usd: Decimal
    cost_uncertain: bool
    burden: BurdenBreakdown


class PlanScore(BaseModel):
    J: float
    max_burden: float
    mean_burden: float
    burden_spread: float
    arrival_spread_min: float  # v2: always 0 (deterministic modes); kept for future use


class Plan(BaseModel):
    plan_id: str
    candidate: Candidate
    target_arrival: datetime
    assignments: list[PersonAssignment]
    score: PlanScore
    risk_flags: list[str] = []  # "price estimated", "closes within 30 min of arrival"
