"""PlacesProvider on OpenStreetMap data (§9.3).

- search_nearby: reads the hand-curated venue fixture (no live Overpass at runtime).
- geocode: fuzzy match fixtures/demo_locations.json first, then Nominatim
  (/search, format=jsonv2, limit=1, bounded viewbox around the demo area),
  through the record/replay cache. Nominatim's usage policy: a descriptive
  User-Agent, at most 1 request/second, cache results.
- text_search: a venue in the fixture by name, else a geocoded place as an
  event-style Candidate (Stage 7).
"""

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt

from app.logging import get_logger, kv
from app.models.candidates import DEFAULT_DURATION_MIN, Candidate, Uncertain
from app.models.private import LatLng
from app.providers import opening_hours
from app.providers.cache import RecordReplayCache
from app.providers.costs import tier_cost
from app.providers.mock.places import _normalize, match_demo_location
from app.providers.mock.routing import haversine_mi
from app.settings import Settings

log = get_logger(__name__)

# httpx logs request URLs at INFO; Nominatim URLs contain the user's typed starting
# point, which is private (§11, CLAUDE.md rule 7).
logging.getLogger("httpx").setLevel(logging.WARNING)

FIXTURES_DIR = Path(__file__).resolve().parents[3] / "fixtures"
METERS_PER_MILE = 1609.34
# Bounded search box around the demo center: about ±5.5 km north-south, ±5.7 km east-west.
VIEWBOX_DLAT = 0.05
VIEWBOX_DLNG = 0.07
NOMINATIM_MIN_INTERVAL_SEC = 1.0


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


class _Throttle:
    """At most one call per `interval` seconds, process-wide."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self.lock = asyncio.Lock()
        self.last = 0.0

    async def wait(self) -> None:
        async with self.lock:
            delay = self.last + self.interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self.last = time.monotonic()


_nominatim_throttle = _Throttle(NOMINATIM_MIN_INTERVAL_SEC)


def resolve_path(path: str) -> Path:
    """Relative paths in settings are relative to the repo root."""
    p = Path(path)
    return p if p.is_absolute() else FIXTURES_DIR.parent / p


class OsmPlaces:
    def __init__(self, settings: Settings, cache: RecordReplayCache) -> None:
        self.settings = settings
        self.cache = cache
        self.tz = ZoneInfo(settings.demo_timezone)
        self.venues: list[dict] = json.loads(
            resolve_path(settings.venues_path).read_text(encoding="utf-8")
        )
        self.locations: list[dict] = json.loads(
            (FIXTURES_DIR / "demo_locations.json").read_text(encoding="utf-8")
        )

    def _candidate(self, raw: dict, at: datetime) -> Candidate:
        status, closes_at = opening_hours.status_at(
            raw.get("opening_hours"), at.astimezone(self.tz)
        )
        return Candidate(
            candidate_id=raw["id"],
            name=raw["name"],
            category=raw["category"],
            cuisines=raw.get("cuisines", []),
            location=LatLng(lat=raw["lat"], lng=raw["lng"]),
            address=raw.get("address", ""),
            est_cost_pp=tier_cost(raw.get("price_tier")),
            open_at_target=status,
            closes_at=closes_at,
            typical_duration_min=DEFAULT_DURATION_MIN.get(raw["category"], 60),
            rating=raw.get("rating"),
            source="osm_fixture",
            novelty_tags=raw.get("novelty_tags", []),
        )

    async def search_nearby(
        self, center: LatLng, radius_m: int, categories: list[str], open_at: datetime
    ) -> list[Candidate]:
        radius_mi = radius_m / METERS_PER_MILE
        return [
            self._candidate(raw, open_at)
            for raw in self.venues
            if (not categories or raw["category"] in categories)
            and haversine_mi(center, LatLng(lat=raw["lat"], lng=raw["lng"])) <= radius_mi
        ]

    async def text_search(self, query: str, near: LatLng) -> list[Candidate]:
        q = _normalize(query)
        if not q:
            return []
        now = datetime.now(self.tz)
        venues = [self._candidate(raw, now) for raw in self.venues if q in _normalize(raw["name"])]
        if venues:
            return venues
        found = await self.geocode(query, near)
        if found is None:
            return []
        coords, label = found
        return [
            Candidate(
                candidate_id="ev:" + hashlib.sha1(label.encode()).hexdigest()[:10],
                name=label,
                category="event",
                location=coords,
                address=label,
                est_cost_pp=Uncertain[Decimal](value=None, status="unknown", source="nominatim"),
                typical_duration_min=DEFAULT_DURATION_MIN["event"],
                source="event",
            )
        ]

    async def geocode(self, text: str, near: LatLng) -> tuple[LatLng, str] | None:
        found = match_demo_location(text, self.locations)
        if found is not None:
            return found
        return await self._nominatim(text)

    async def _nominatim(self, text: str) -> tuple[LatLng, str] | None:
        query = " ".join(text.split())
        if not query:
            return None
        if not self.settings.nominatim_user_agent:
            log.warning(kv("nominatim_skipped", reason="NOMINATIM_USER_AGENT not set"))
            return None
        lat, lng = self.settings.demo_center_lat, self.settings.demo_center_lng
        params = {
            "q": query,
            "format": "jsonv2",
            "limit": 1,
            # x1,y1,x2,y2 = lon/lat corners
            "viewbox": f"{lng - VIEWBOX_DLNG},{lat + VIEWBOX_DLAT},"
            f"{lng + VIEWBOX_DLNG},{lat - VIEWBOX_DLAT}",
            "bounded": 1,
        }

        async def live() -> list[dict]:
            await _nominatim_throttle.wait()
            start = time.monotonic()
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(2),
                retry=retry_if_exception(_is_retryable),
                reraise=True,
            ):
                with attempt:
                    async with httpx.AsyncClient(timeout=self.settings.http_timeout_sec) as c:
                        response = await c.get(
                            f"{self.settings.nominatim_base_url.rstrip('/')}/search",
                            params=params,
                            headers={"User-Agent": self.settings.nominatim_user_agent},
                        )
                        response.raise_for_status()
            log.info(
                kv(
                    "provider_call",
                    provider="nominatim",
                    method="search",
                    status=response.status_code,
                    latency_ms=round((time.monotonic() - start) * 1000),
                )
            )
            return response.json()

        try:
            results = await self.cache.call("nominatim", "search", params, live)
        except Exception as exc:
            log.warning(kv("nominatim_failed", error=type(exc).__name__))
            return None
        if not results:
            return None
        top = results[0]
        try:
            coords = LatLng(lat=float(top["lat"]), lng=float(top["lon"]))
        except (KeyError, TypeError, ValueError):
            return None
        label = top.get("name") or str(top.get("display_name", "")).split(",")[0].strip()
        return coords, label or query
