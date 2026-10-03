"""Candidate venue models (§6.4)."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from app.models.private import LatLng


class Uncertain[T](BaseModel):
    value: T | None
    low: T | None = None
    high: T | None = None
    status: Literal["known", "estimated", "unknown"]
    source: str  # "google_places" | "fixture" | "config" | "grok:<url>"


class Candidate(BaseModel):
    candidate_id: str  # "gp:<place_id>" | "fx:<slug>" | "ev:<hash>"
    name: str
    category: str  # food | bar | cafe | dessert | activity | event
    cuisines: list[str] = []
    location: LatLng  # REQUIRED, from Places or fixture — never from the LLM
    address: str
    est_cost_pp: Uncertain[Decimal]
    open_at_target: Literal["open", "closed", "unknown"] = "unknown"
    closes_at: datetime | None = None
    typical_duration_min: int  # category default: food 60, cafe 45, dessert 30, bar 90, activity 90
    rating: float | None = None
    source: Literal["google_places", "fixture", "grok_event"]
    novelty_tags: list[str] = []


class EventFinding(BaseModel):
    """A live event found by ContextProvider (Stage 7 stretch).

    Placeholder shape: no coordinates by design — events must be resolved to a
    location via PlacesProvider.text_search before becoming a Candidate.
    """

    title: str
    venue_name: str
    starts_at: datetime | None = None
    source_url: str
