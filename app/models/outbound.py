"""Outbound message models (§6.7) — the only types messages are built from."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from app.models.routing import Mode, RouteStep


class GroupPlanOption(BaseModel):
    """Group-safe. No per-person fields."""

    label: Literal["A", "B", "C"]
    title: str  # "Koko — Korean"
    max_travel_min: int  # "≤22 min for everyone"
    walking_level: Literal["low", "moderate", "high"]
    price_tier: Literal["$", "$$", "$$$", "$$$$", "?"]  # "?" = Google has no price
    price_estimated: bool = False  # typical cost for this kind of place, not its own price
    arrival_window_min: int
    blurb: str  # explanation from group-safe facts only


class GroupSafeMessage(BaseModel):
    text: str
    poll: list[GroupPlanOption] | None = None


class PersonalItinerary(BaseModel):
    """DM only."""

    user_id: UUID
    venue_name: str
    venue_address: str
    mode: Mode
    leave_by: datetime
    arrive_at: datetime
    steps: list[RouteStep]
    est_cost_usd: Decimal


class PrivateMessage(BaseModel):
    text: str
    image_path: str | None = None
