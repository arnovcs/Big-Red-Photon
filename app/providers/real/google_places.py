"""PlacesProvider on Google Places API (New): worldwide venues with price, hours, rating.

Checked against developers.google.com/maps/documentation/places/web-service:
POST https://places.googleapis.com/v1/places:searchNearby, headers X-Goog-Api-Key and
X-Goog-FieldMask (required; there are no default fields). maxResultCount is 1–20.
priceLevel / rating / regularOpeningHours put the call in the "Nearby Search
Enterprise" SKU. Opening-hours periods use day 0 = Sunday; a 24-hour place has an
open point and no close. utcOffsetMinutes gives the venue's own timezone, so
"open at T" works anywhere without knowing the user's timezone.

One call per category the group wants, through the record/replay cache, plus one
call for the cuisines people asked for (e.g. "japanese" → japanese, sushi, ramen...
restaurant types), so matching places are among the options. Google is the only
source of place data: if it fails or finds nothing, the result is empty and the bot
says so (no local fallback list).

Typed places ("Young Boys Barbershop", "I'm at Collegetown Bagels") are resolved
with Text Search: POST https://places.googleapis.com/v1/places:searchText,
{textQuery, pageSize, locationBias: {circle: {center, radius}}} — biased to, not
restricted to, the area around `near` (LOOKUP_BIAS_RADIUS_M).

Prices: priceRange (real "$10–20" per person, USD) when Google has it, else the
priceLevel tier, else unknown (never guessed; the optimizer decides what to do).

Only real, open-for-customers venues are kept: businessStatus OPERATIONAL, not a
"pure service area business" (caterers, home kitchens: no storefront), and at least
MIN_REVIEWS reviews (ghost listings have none). All three fields are in the same
Enterprise SKU we already pay for via rating / price / hours.
"""

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt

from app.logging import get_logger, kv
from app.models.candidates import DEFAULT_DURATION_MIN, Candidate, ResolvedPlace, Uncertain
from app.models.private import LatLng
from app.optimizer import cuisines as cuisine_families
from app.providers.cache import RecordReplayCache
from app.providers.costs import tier_cost
from app.providers.opening_hours import Status
from app.settings import Settings

log = get_logger(__name__)

NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"
TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
# Typed-place lookups need only these (Text Search "Pro" fields: no price/rating).
LOOKUP_FIELD_MASK = (
    "places.id,places.displayName,places.location,"
    "places.shortFormattedAddress,places.formattedAddress"
)
LOOKUP_BIAS_RADIUS_M = 10_000.0
FIELDS = [
    "id",
    "displayName",
    "location",
    "shortFormattedAddress",
    "formattedAddress",
    "types",
    "primaryType",
    "priceLevel",
    "priceRange",
    "rating",
    "userRatingCount",
    "businessStatus",
    "pureServiceAreaBusiness",
    "regularOpeningHours",
    "utcOffsetMinutes",
]
FIELD_MASK = ",".join(f"places.{f}" for f in FIELDS)

# Our categories → Google place types (Places API (New) "Table A" types).
CATEGORY_TYPES: dict[str, list[str]] = {
    "food": ["restaurant"],
    "cafe": ["cafe", "coffee_shop", "bakery"],
    "bar": ["bar", "pub"],
    "dessert": ["ice_cream_shop", "dessert_shop"],
    "activity": [
        "movie_theater",
        "bowling_alley",
        "amusement_center",
        "amusement_park",
        # Outdoors and animals ("let's go to a park", "somewhere with animals").
        "park",
        "state_park",
        "national_park",
        "hiking_area",
        "botanical_garden",
        "garden",
        "zoo",
        "aquarium",
        "wildlife_park",
        "wildlife_refuge",
        "museum",
        "art_gallery",
        "planetarium",
        # Entertainment ("something fun").
        "video_arcade",
        "karaoke",
        "live_music_venue",
        "performing_arts_theater",
        "comedy_club",
    ],
    # "something sporty". All checked against Google: pickleball_court, escape_room and
    # rock_climbing are NOT valid types (those go through Text Search as activities).
    "sports": [
        "sports_complex",
        "sports_activity_location",
        "athletic_field",
        "sports_club",
        "tennis_court",
        "fitness_center",
        "gym",
        "swimming_pool",
        "golf_course",
        "skateboard_park",
        "ice_skating_rink",
        "sports_coaching",
    ],
}
# Categories that aren't food or drink: restaurants and bars are never results here.
NON_FOOD_CATEGORIES = {"activity", "sports"}
FOOD_AND_DRINK_TYPES = ["restaurant", "bar", "pub", "cafe", "fast_food_restaurant"]
# Spectator venues: somewhere to watch a game on game day, not somewhere to go do something.
SPECTATOR_TYPES = ["stadium", "arena"]
# "restaurant" matches every kind of restaurant (italian_restaurant, ...), so food
# filters on any type. The other categories must be the place's *primary* type, or
# Google returns e.g. a 7-Eleven as a cafe.
# Sports must be the place's main type too (secondary types pull in stadiums and halls);
# courts inside parks are found by the activity's own Text Search ("pickleball").
MATCH_ANY_TYPE = {"food"}
# Never venues for a group outing, whatever else they're tagged with.
EXCLUDED_PRIMARY_TYPES = [
    "hotel",
    "lodging",
    "convenience_store",
    "grocery_store",
    "supermarket",
    "gas_station",
]
PRICE_TIERS = {
    "PRICE_LEVEL_FREE": "free",
    "PRICE_LEVEL_INEXPENSIVE": "$",
    "PRICE_LEVEL_MODERATE": "$$",
    "PRICE_LEVEL_EXPENSIVE": "$$$",
    "PRICE_LEVEL_VERY_EXPENSIVE": "$$$$",
}
CUISINE_FROM_TYPE = {
    "coffee_shop": "coffee",
    "bagel_shop": "bagels",
    "ice_cream_shop": "ice_cream",
    "dessert_shop": "dessert",
    "bakery": "bakery",
    "pizza_restaurant": "pizza",
    "noodle_shop": "noodles",
}
# Restaurant types we search by cuisine, from Places API (New) Table A.
# A type Google doesn't know makes the whole request fail, so only listed names.
CUISINE_SEARCH_TYPES = {
    "noodle_shop",
    *(
        f"{name}_restaurant"
        for name in (
            "afghani african american asian asian_fusion barbecue brazilian breakfast "
            "brunch burmese burrito cambodian cantonese caribbean chinese cuban "
            "dim_sum dumpling ethiopian filipino french german greek hawaiian hot_pot "
            "indian indonesian italian japanese japanese_curry japanese_izakaya "
            "korean korean_barbecue lebanese malaysian mediterranean mexican "
            "middle_eastern mongolian_barbecue north_indian pakistani peruvian pizza "
            "ramen seafood south_indian spanish sushi taco taiwanese thai tibetan "
            "tonkatsu turkish vegan vegetarian vietnamese yakiniku yakitori"
        ).split()
    ),
}
# Fewer reviews than this → probably not a real venue (or too unknown to recommend).
MIN_REVIEWS = 10
# Public courts, fields and parks get few reviews but are real: a lower bar for them.
MIN_REVIEWS_NON_FOOD = 3
# No search radius (team decision): Nearby Search's maximum circle, ranked nearest first,
# so the results are the closest matching places; the optimizer weighs travel time.
SEARCH_RADIUS_MAX_M = 50_000.0
# Where Google has no price, a typical cost by kind of place, marked as an estimate.
# (low, high) per person in USD. Free-to-visit places count as $0 with price unknown.
TYPE_COST_ESTIMATES: dict[str, tuple[int, int]] = {
    "gym": (10, 20),
    "fitness_center": (10, 20),
    "bowling_alley": (15, 25),
    "video_arcade": (15, 25),
    "karaoke": (15, 25),
    "ice_skating_rink": (15, 25),
    "amusement_center": (15, 25),
    "golf_course": (30, 60),
    "sports_club": (20, 40),
    "sports_coaching": (20, 40),
}
FREE_TO_VISIT_TYPES = {
    "park",
    "state_park",
    "national_park",
    "hiking_area",
    "botanical_garden",
    "garden",
    "playground",
    "athletic_field",
    "tennis_court",
    "skateboard_park",
    "wildlife_refuge",
}
MAX_RESULTS = 20
MAX_TYPES_PER_REQUEST = 50  # Google rejects more ("Too many types in included_types")
MAX_RESULTS_PER_CATEGORY_WHEN_MANY = 10
MINUTES_PER_WEEK = 7 * 24 * 60


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


def _week_minute(point: dict) -> int:
    return point.get("day", 0) * 1440 + point.get("hour", 0) * 60 + point.get("minute", 0)


def hours_status(place: dict, at: datetime) -> tuple[Status, datetime | None]:
    """Open/closed at `at` (any timezone) from the place's weekly periods, evaluated in
    the venue's own local time. No hours or no offset → "unknown"."""
    periods = (place.get("regularOpeningHours") or {}).get("periods") or []
    offset = place.get("utcOffsetMinutes")
    if not periods or offset is None:
        return "unknown", None
    local = at.astimezone(UTC) + timedelta(minutes=offset)
    google_day = (local.weekday() + 1) % 7  # Python Monday=0 → Google Sunday=0
    now = google_day * 1440 + local.hour * 60 + local.minute
    for period in periods:
        start = _week_minute(period.get("open") or {})
        if "close" not in period:
            return "open", None  # always open
        end = _week_minute(period["close"])
        if end <= start:
            end += MINUTES_PER_WEEK  # wraps past Saturday night
        for t in (now, now + MINUTES_PER_WEEK):
            if start <= t < end:
                return "open", at + timedelta(minutes=end - t)
    return "closed", None


def _cuisines(place: dict) -> list[str]:
    primary = place.get("primaryType") or ""
    if primary in CUISINE_FROM_TYPE:
        return [CUISINE_FROM_TYPE[primary]]
    # The primary type first, then any other cuisine types the place has (a place
    # whose primary type is just "restaurant" can still be a japanese_restaurant).
    out: list[str] = []
    for t in [primary, *(place.get("types") or [])]:
        cuisine = CUISINE_FROM_TYPE.get(t) or (
            t.removesuffix("_restaurant") if t.endswith("_restaurant") else None
        )
        if cuisine and cuisine not in out:
            out.append(cuisine)
    return out


def _activity_kind(place: dict, category: str) -> list[str]:
    """Activities and sports get their kind ("park", "museum", "tennis_court") where food
    gets a cuisine, so the poll shows it and picks a variety rather than three alike."""
    primary = place.get("primaryType") or ""
    return [primary] if category in NON_FOOD_CATEGORIES and primary else []


def _is_food_or_drink(place: dict) -> bool:
    """Also spectator venues: neither belongs in an activity search."""
    primary = place.get("primaryType") or ""
    return (
        primary in FOOD_AND_DRINK_TYPES
        or primary in SPECTATOR_TYPES
        or primary.endswith("_restaurant")
    )


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def category_of(place: dict) -> str:
    """For a place found by a free-text activity search: sports or activity."""
    types = {place.get("primaryType") or "", *(place.get("types") or [])}
    return "sports" if types & set(CATEGORY_TYPES["sports"]) else "activity"


def cuisine_types(wanted: list[str]) -> list[str]:
    """Google restaurant types for wanted cuisines and their families, e.g.
    ["japanese"] → japanese_restaurant, ramen_restaurant, sushi_restaurant, ..."""
    types = set()
    for cuisine in wanted:
        for name in cuisine_families.expand(cuisine):
            for t in (f"{name}_restaurant", "noodle_shop" if name == "noodles" else ""):
                if t in CUISINE_SEARCH_TYPES:
                    types.add(t)
    return sorted(types)[:MAX_TYPES_PER_REQUEST]


def is_real_venue(place: dict, min_reviews: int = MIN_REVIEWS) -> bool:
    """Open for business, has a storefront, and enough reviews to trust."""
    return (
        place.get("businessStatus") == "OPERATIONAL"
        and not place.get("pureServiceAreaBusiness")
        and (place.get("userRatingCount") or 0) >= min_reviews
    )


def _money(m: dict | None) -> Decimal | None:
    """A Google Money {currencyCode, units, nanos} in USD, else None (USD-only app).
    `units` is an int64, so it arrives as a JSON string."""
    if not m or m.get("currencyCode") != "USD":
        return None
    return Decimal(str(m.get("units") or 0)) + Decimal(m.get("nanos") or 0) / Decimal(10**9)


def price_from_range(place: dict) -> Uncertain[Decimal] | None:
    """Real per-person prices from priceRange, e.g. $10–20 → typical $15, high $20.
    endPrice may be unset ("More than $50"): then the start is all we know."""
    price_range = place.get("priceRange") or {}
    low = _money(price_range.get("startPrice"))
    if low is None:
        return None
    high = _money(price_range.get("endPrice"))
    typical = (low + high) / 2 if high is not None else low
    return Uncertain[Decimal](
        value=typical, low=low, high=high, status="estimated", source="google_price_range"
    )


def place_cost(place: dict) -> Uncertain[Decimal]:
    """Best price we have: Google's real range, then its $ level; else, for kinds of
    places that usually charge (gyms, bowling, golf...), a typical-cost estimate; for
    parks, courts, and trails $0 with the price marked unknown; else unknown."""
    from_range = price_from_range(place)
    if from_range is not None:
        return from_range
    tier = PRICE_TIERS.get(place.get("priceLevel", ""))
    if tier is not None:
        return tier_cost(tier, source="google")
    types = [place.get("primaryType") or "", *(place.get("types") or [])]
    for t in types:
        if t in TYPE_COST_ESTIMATES:
            low, high = (Decimal(x) for x in TYPE_COST_ESTIMATES[t])
            return Uncertain[Decimal](
                value=(low + high) / 2,
                low=low,
                high=high,
                status="estimated",
                source="type_estimate",
            )
    if types[0] in FREE_TO_VISIT_TYPES:
        zero = Decimal(0)
        return Uncertain[Decimal](
            value=zero, low=zero, high=zero, status="unknown", source="free_to_visit"
        )
    return Uncertain[Decimal](value=None, status="unknown", source="google")


def to_resolved_place(place: dict) -> ResolvedPlace | None:
    """A Text Search result → the place someone typed. None without a name or location."""
    location = place.get("location") or {}
    name = (place.get("displayName") or {}).get("text")
    if not name or "latitude" not in location or "longitude" not in location:
        return None
    return ResolvedPlace(
        location=LatLng(lat=location["latitude"], lng=location["longitude"]),
        name=name,
        address=place.get("shortFormattedAddress") or place.get("formattedAddress") or "",
        place_id=place.get("id"),
    )


def to_candidate(
    place: dict, category: str, at: datetime, tags: list[str] | None = None
) -> Candidate | None:
    """`tags`: what the person asked for that found this place (e.g. ["pickleball"]),
    kept first in `cuisines` so the optimizer can tell it matches."""
    min_reviews = MIN_REVIEWS_NON_FOOD if category in NON_FOOD_CATEGORIES else MIN_REVIEWS
    if not is_real_venue(place, min_reviews):
        return None
    if category in NON_FOOD_CATEGORIES and _is_food_or_drink(place):
        return None  # an activity search never suggests a restaurant or bar
    location = place.get("location") or {}
    name = (place.get("displayName") or {}).get("text")
    if not name or "latitude" not in location or "longitude" not in location:
        return None
    cost = place_cost(place)
    status, closes_at = hours_status(place, at)
    return Candidate(
        candidate_id=f"google:{place.get('id', name)}",
        name=name,
        category=category,
        cuisines=_dedupe([*(tags or []), *(_cuisines(place) or _activity_kind(place, category))]),
        location=LatLng(lat=location["latitude"], lng=location["longitude"]),
        address=place.get("shortFormattedAddress") or place.get("formattedAddress") or "",
        est_cost_pp=cost,
        open_at_target=status,
        closes_at=closes_at,
        typical_duration_min=DEFAULT_DURATION_MIN.get(category, 60),
        rating=place.get("rating"),
        source="google",
    )


class GooglePlaces:
    def __init__(self, settings: Settings, cache: RecordReplayCache) -> None:
        self.settings = settings
        self.cache = cache

    async def _post(
        self,
        body: dict,
        url: str = NEARBY_URL,
        field_mask: str = FIELD_MASK,
        method: str = "search_nearby",
    ) -> dict:
        """POST through the record/replay cache. Retries network errors / 5xx once."""

        async def live() -> dict:
            start = time.monotonic()
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(2),
                retry=retry_if_exception(_is_retryable),
                reraise=True,
            ):
                with attempt:
                    async with httpx.AsyncClient(timeout=self.settings.http_timeout_sec) as c:
                        response = await c.post(
                            url,
                            json=body,
                            headers={
                                "X-Goog-Api-Key": self.settings.google_places_api_key,
                                "X-Goog-FieldMask": field_mask,
                            },
                        )
                        response.raise_for_status()
            log.info(
                kv(
                    "provider_call",
                    provider="google_places",
                    method=method,
                    status=response.status_code,
                    latency_ms=round((time.monotonic() - start) * 1000),
                )
            )
            return response.json()

        request = {"body": body, "field_mask": field_mask}
        return await self.cache.call("google_places", method, request, live)

    def _nearby_body(self, center: LatLng, category: str, types: list[str], n: int) -> dict:
        type_filter = "includedTypes" if category in MATCH_ANY_TYPE else "includedPrimaryTypes"
        excluded = EXCLUDED_PRIMARY_TYPES
        if category in NON_FOOD_CATEGORIES:
            excluded = [*EXCLUDED_PRIMARY_TYPES, *FOOD_AND_DRINK_TYPES, *SPECTATOR_TYPES]
        return {
            type_filter: types,
            "excludedPrimaryTypes": excluded,
            "maxResultCount": n,
            # No search radius: the nearest matches anywhere (Google's max circle).
            "rankPreference": "DISTANCE",
            "locationRestriction": {
                "circle": {
                    # ~10 m rounding keeps the cache key stable for the same group.
                    "center": {
                        "latitude": round(center.lat, 4),
                        "longitude": round(center.lng, 4),
                    },
                    "radius": SEARCH_RADIUS_MAX_M,
                }
            },
        }

    async def search_nearby(
        self,
        center: LatLng,
        radius_m: int,
        categories: list[str],
        open_at: datetime,
        cuisines: list[str] | None = None,
        activities: list[str] | None = None,
    ) -> list[Candidate]:
        """Venues for what the group wants. `radius_m` is ignored (no search radius: the
        nearest matches, then the optimizer weighs everyone's travel time).
        `activities` ("pickleball") are searched by name with Text Search, since most
        have no Google place type; `cuisines` by restaurant type; then each category."""
        wanted = [c for c in (categories or list(CATEGORY_TYPES)) if c in CATEGORY_TYPES]
        per_call = MAX_RESULTS if len(wanted) == 1 else MAX_RESULTS_PER_CATEGORY_WHEN_MANY
        # (label, category or None = decide per place, tags, url, body).
        searches: list[tuple[str, str | None, list[str], str, dict]] = []
        for activity in activities or []:
            body = self._text_body(activity, center, MAX_RESULTS)
            searches.append((f"activity:{activity}", None, [activity], TEXT_SEARCH_URL, body))
        types = cuisine_types(cuisines or [])
        if types:
            body = self._nearby_body(center, "food", types, MAX_RESULTS)
            searches.append(("cuisine", "food", [], NEARBY_URL, body))
        for category in wanted:
            # Activities and sports span many kinds: always the full 20.
            n = MAX_RESULTS if category in NON_FOOD_CATEGORIES else per_call
            body = self._nearby_body(center, category, CATEGORY_TYPES[category], n)
            searches.append((category, category, [], NEARBY_URL, body))

        found: dict[str, Candidate] = {}
        for label, category, tags, url, body in searches:
            method = "search_text" if url == TEXT_SEARCH_URL else "search_nearby"
            try:
                response = await self._post(body, url, FIELD_MASK, method)
            except Exception as exc:
                log.warning(kv("google_places_failed", category=label, error=type(exc).__name__))
                continue
            places = response.get("places") or []
            kept = 0
            for place in places:
                candidate = to_candidate(place, category or category_of(place), open_at, tags)
                if candidate and candidate.candidate_id not in found:
                    found[candidate.candidate_id] = candidate
                    kept += 1
            log.info(kv("places_search", search=label, returned=len(places), kept=kept))
        log.info(
            kv(
                "venue_candidates",
                searches=len(searches),
                found=len(found),
                categories=",".join(wanted),
                cuisines=",".join(cuisines or []),
                activities=",".join(activities or []),
            )
        )
        if not found:
            log.warning(kv("google_places_no_venues"))
        return list(found.values())

    def _text_body(self, query: str, near: LatLng, page_size: int) -> dict:
        return {
            "textQuery": query,
            "pageSize": page_size,
            "locationBias": {
                "circle": {
                    "center": {"latitude": round(near.lat, 4), "longitude": round(near.lng, 4)},
                    "radius": LOOKUP_BIAS_RADIUS_M,
                }
            },
        }

    async def text_search(self, query: str, near: LatLng) -> list[Candidate]:
        """Venues matching free text ("cheap food near Collegetown"), as candidates."""
        query = " ".join(query.split())
        if not query:
            return []
        try:
            response = await self._post(
                self._text_body(query, near, MAX_RESULTS),
                TEXT_SEARCH_URL,
                FIELD_MASK,
                "search_text",
            )
        except Exception as exc:
            log.warning(kv("google_text_search_failed", error=type(exc).__name__))
            return []
        now = datetime.now(UTC)
        found = [to_candidate(p, "food", now) for p in response.get("places") or []]
        return [c for c in found if c is not None]

    async def geocode(self, text: str, near: LatLng) -> ResolvedPlace | None:
        """The place someone typed, via Text Search biased to `near`. None if Google finds
        nothing or fails: the caller asks them to rephrase (never a default place)."""
        query = " ".join(text.split())
        if not query:
            return None
        try:
            response = await self._post(
                self._text_body(query, near, 1), TEXT_SEARCH_URL, LOOKUP_FIELD_MASK, "lookup"
            )
        except Exception as exc:
            log.warning(kv("place_lookup_failed", error=type(exc).__name__))
            return None
        places = response.get("places") or []
        place = to_resolved_place(places[0]) if places else None
        # What they typed and where it resolved are private: DEBUG only (CLAUDE.md rule 7).
        log.info(kv("place_lookup", found=place is not None))
        log.debug(kv("place_lookup_detail", query=query, result=place.label if place else None))
        return place
