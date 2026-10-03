"""Fixture-backed places (§14.2): venues from fixtures/venues.json, geocoding from
fixtures/demo_locations.json only."""

import json
from datetime import datetime
from pathlib import Path

from app.models.candidates import DEFAULT_DURATION_MIN, Candidate
from app.models.private import LatLng
from app.providers.costs import tier_cost
from app.providers.mock.routing import haversine_mi

FIXTURES_DIR = Path(__file__).resolve().parents[3] / "fixtures"
METERS_PER_MILE = 1609.34


def load_venues(path: Path) -> list[Candidate]:
    venues = []
    for raw in json.loads(path.read_text(encoding="utf-8")):
        venues.append(
            Candidate(
                candidate_id=raw["id"],
                name=raw["name"],
                category=raw["category"],
                cuisines=raw.get("cuisines", []),
                location=LatLng(lat=raw["lat"], lng=raw["lng"]),
                address=raw["address"],
                est_cost_pp=tier_cost(raw.get("price_tier")),
                typical_duration_min=DEFAULT_DURATION_MIN.get(raw["category"], 60),
                rating=raw.get("rating"),
                source="osm_fixture",
                novelty_tags=raw.get("novelty_tags", []),
            )
        )
    return venues


def _normalize(text: str) -> str:
    return " ".join("".join(ch for ch in text.lower() if ch.isalnum() or ch.isspace()).split())


def match_demo_location(text: str, locations: list[dict]) -> tuple[LatLng, str] | None:
    """Exact name/alias match first, then the longest alias contained in the text."""
    query = _normalize(text)
    if not query:
        return None
    best: tuple[int, dict] | None = None
    for loc in locations:
        for alias in [loc["name"], *loc.get("aliases", [])]:
            alias_n = _normalize(alias)
            if alias_n == query:
                return LatLng(lat=loc["lat"], lng=loc["lng"]), loc["name"]
            if alias_n in query and (best is None or len(alias_n) > best[0]):
                best = (len(alias_n), loc)
    if best is None:
        return None
    loc = best[1]
    return LatLng(lat=loc["lat"], lng=loc["lng"]), loc["name"]


NEAR_LANDMARK_MAX_M = 400


def load_demo_locations(fixtures_dir: Path = FIXTURES_DIR) -> list[dict]:
    return json.loads((fixtures_dir / "demo_locations.json").read_text(encoding="utf-8"))


def near_label(point: LatLng, locations: list[dict]) -> str:
    """A friendly label for raw coordinates: "near <landmark>" if one is close, else a
    generic phrase. Never includes the coordinates themselves."""
    best: tuple[float, str] | None = None
    for loc in locations:
        meters = haversine_mi(point, LatLng(lat=loc["lat"], lng=loc["lng"])) * METERS_PER_MILE
        if meters <= NEAR_LANDMARK_MAX_M and (best is None or meters < best[0]):
            best = (meters, loc["name"])
    return f"near {best[1]}" if best else "your shared location"


class MockPlaces:
    def __init__(self, fixtures_dir: Path = FIXTURES_DIR, venues_path: Path | None = None) -> None:
        self.venues = load_venues(venues_path or fixtures_dir / "venues.json")
        self.locations: list[dict] = json.loads(
            (fixtures_dir / "demo_locations.json").read_text(encoding="utf-8")
        )

    async def search_nearby(
        self, center: LatLng, radius_m: int, categories: list[str], open_at: datetime
    ) -> list[Candidate]:
        radius_mi = radius_m / METERS_PER_MILE
        return [
            v
            for v in self.venues
            if (not categories or v.category in categories)
            and haversine_mi(center, v.location) <= radius_mi
        ]

    async def text_search(self, query: str, near: LatLng) -> list[Candidate]:
        q = _normalize(query)
        return [v for v in self.venues if q and q in _normalize(v.name)]

    async def geocode(self, text: str, near: LatLng) -> tuple[LatLng, str] | None:
        return match_demo_location(text, self.locations)
