"""Candidate venue models (§6.4)."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from app.models.private import LatLng

# §6.4 category defaults for Candidate.typical_duration_min
DEFAULT_DURATION_MIN: dict[str, int] = {
    "food": 60,
    "cafe": 45,
    "dessert": 30,
    "bar": 90,
    "activity": 90,
    "event": 90,
}


class Uncertain[T](BaseModel):
    value: T | None
    low: T | None = None
    high: T | None = None
    status: Literal["known", "estimated", "unknown"]
    source: str  # "osm_fixture" | "hand_entered" | "formula" | "ors" | "gemini:<url>"


class Candidate(BaseModel):
    candidate_id: str  # "osm:<node|way>/<id>" | "google:<place id>" | "ev:<hash>"
    name: str
    category: str  # food | bar | cafe | dessert | activity | event
    cuisines: list[str] = []
    location: LatLng  # REQUIRED, from the OSM fixture or Nominatim — never from the LLM
    address: str
    est_cost_pp: Uncertain[Decimal]
    open_at_target: Literal["open", "closed", "unknown"] = "unknown"
    closes_at: datetime | None = None
    typical_duration_min: int  # category default: food 60, cafe 45, dessert 30, bar 90, activity 90
    rating: float | None = None
    source: Literal["osm_fixture", "google", "event"]
    novelty_tags: list[str] = []


class EventFinding(BaseModel):
    """A live event found by ContextProvider (Stage 7 only).

    No coordinates by design: events must be resolved via PlacesProvider.geocode
    before becoming a Candidate.
    """

    title: str
    venue_name: str
    starts_at: datetime | None
    est_price_usd: Decimal | None = None
    source_url: str
