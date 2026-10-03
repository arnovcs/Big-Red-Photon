from pydantic import BaseModel


class OptimizerParams(BaseModel):
    """Tunable optimizer weights and defaults (§13.4, §13.5). Phase 1 terms only."""

    lam: float = 0.5  # worst-off (max) vs mean burden
    w_money: float = 0.40
    w_time: float = 0.35
    w_pref: float = 0.25
    default_travel_tolerance_min: float = 30.0  # τ when no max_travel is stated
    ready_buffer_min: float = 5.0  # ready = now + this, unless available_from is stated
    neutral_pref_satisfaction: float = 0.5  # sat when a person stated no preferences
    unknown_cost_limit_fraction: float = 0.8  # unknown price must be ≤ this × limit
