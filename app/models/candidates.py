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
    source: str  # "google" | "google_price_range" | "formula" | "ors" | "gemini:<url>"


class Candidate(BaseModel):
    candidate_id: str  # "google:<place id>"
    name: str
    category: str  # food | bar | cafe | dessert | activity | event
    cuisines: list[str] = []
    location: LatLng  # REQUIRED, from Google Places — never from the LLM
    address: str
    est_cost_pp: Uncertain[Decimal]
    open_at_target: Literal["open", "closed", "unknown"] = "unknown"
    closes_at: datetime | None = None
    typical_duration_min: int  # category default: food 60, cafe 45, dessert 30, bar 90, activity 90
    rating: float | None = None
    source: Literal["google", "event"]


class ResolvedPlace(BaseModel):
    """A place someone typed ("Young Boys Barbershop"), resolved by Google Places."""

    location: LatLng
    name: str
    address: str  # short address, e.g. "111 Dryden Rd Apt D, Ithaca"; may be ""
    place_id: str | None = None

    @property
    def label(self) -> str:
        """What we confirm back: "Young Boys barbershop, 111 Dryden Rd Apt D, Ithaca"."""
        if not self.address or self.address.lower().startswith(self.name.lower()):
            return self.address or self.name
        return f"{self.name}, {self.address}"


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
