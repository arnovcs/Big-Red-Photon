"""Routing models (§6.5)."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel

from app.models.candidates import Uncertain


class Mode(StrEnum):
    WALK = "walk"
    BIKE = "bike"
    DRIVE = "drive"
    RIDESHARE = "rideshare"


class RouteEstimate(BaseModel):
    origin_pid: str
    candidate_id: str
    mode: Mode
    duration_min: float
    distance_mi: float
    walk_min: float  # equals duration for WALK; 0 for other modes
    # walk/bike: 0; drive: miles × rate + parking; rideshare: formula (status="estimated")
    fare_usd: Uncertain[Decimal]
    source: Literal["ors", "mock"]


class RouteStep(BaseModel):
    mode: Literal["walk", "bike", "drive", "rideshare"]
    instruction: str  # from ORS directions, e.g. "Turn left onto College Ave"
    duration_min: float
    distance_mi: float


class RouteDetail(RouteEstimate):
    depart_at: datetime
    arrive_at: datetime
    steps: list[RouteStep]
    polyline: str | None = None
