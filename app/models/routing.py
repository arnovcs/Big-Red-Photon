"""Routing models (§6.5)."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel

from app.models.candidates import Uncertain


class Mode(StrEnum):
    WALK = "walk"
    TRANSIT = "transit"
    DRIVE = "drive"


class RouteEstimate(BaseModel):
    origin_pid: str
    candidate_id: str
    mode: Mode
    duration_min: float
    distance_mi: float
    walk_min: float  # walking portion; equals duration for WALK
    fare_usd: Uncertain[Decimal]  # transit: Routes fare or config; drive: miles × rate + parking
    source: Literal["google_routes", "mock"]


class RouteStep(BaseModel):
    mode: Literal["walk", "transit", "drive"]
    instruction: str
    duration_min: float
    line_name: str | None = None
    depart_stop: str | None = None
    arrive_stop: str | None = None
    depart_time: datetime | None = None


class RouteDetail(RouteEstimate):
    depart_at: datetime
    arrive_at: datetime
    steps: list[RouteStep]
    polyline: str | None = None
