"""Google Places provider: mapping, opening hours, request shape, fallback. No network.

tests/fixtures/google_nearby_sample.json follows the Places API (New) response shape.
"""

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from app.models.private import LatLng
from app.providers.cache import RecordReplayCache
from app.providers.real.google_places import (
    CATEGORY_TYPES,
    EXCLUDED_PRIMARY_TYPES,
    FIELD_MASK,
    GooglePlaces,
    hours_status,
    place_cost,
    to_candidate,
)
from app.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = json.loads((FIXTURES / "google_nearby_sample.json").read_text())
PLACES = {p["id"]: p for p in SAMPLE["places"]}
SAT_6PM = datetime.fromisoformat("2026-10-03T18:00:00-04:00")
SUN_1AM = datetime.fromisoformat("2026-10-04T01:00:00-04:00")
SUN_930 = datetime.fromisoformat("2026-10-04T09:30:00-04:00")
CENTER = LatLng(lat=42.444049, lng=-76.483012)
SETTINGS = Settings(
    _env_file=None,
    google_places_api_key="k",
    venues_path=str(FIXTURES / "venues_test.json"),
)


# --- mapping -------------------------------------------------------------------------


def test_priced_restaurant_maps_to_candidate() -> None:
    c = to_candidate(PLACES["ChIJmoosewood"], "food", SAT_6PM)
    assert c.candidate_id == "google:ChIJmoosewood"
    assert (c.name, c.category, c.cuisines) == ("Moosewood", "food", ["vegetarian"])
    assert c.address == "215 N Cayuga St, Ithaca"
    assert c.est_cost_pp.value == 25 and c.est_cost_pp.source == "google"
    assert c.rating == 4.4
    assert c.source == "google"


def test_missing_price_is_estimated_from_category() -> None:
    c = to_candidate(PLACES["ChIJgimme"], "cafe", SAT_6PM)
    assert c.est_cost_pp.value == 12
    assert c.est_cost_pp.status == "estimated"
    assert c.est_cost_pp.source == "category_estimate"
    assert c.cuisines == ["coffee"]


def test_place_without_name_or_location_is_skipped() -> None:
    real = {"businessStatus": "OPERATIONAL", "userRatingCount": 50}
    assert to_candidate({**real, "displayName": {"text": "x"}}, "food", SAT_6PM) is None
    no_name = {**real, "location": {"latitude": 1, "longitude": 2}}
    assert to_candidate(no_name, "food", SAT_6PM) is None


def test_ghost_listings_service_businesses_and_closed_places_are_dropped() -> None:
    assert to_candidate(PLACES["ChIJghost"], "cafe", SAT_6PM) is None  # no reviews
    assert to_candidate(PLACES["ChIJcaterer"], "food", SAT_6PM) is None  # no storefront
    assert to_candidate(PLACES["ChIJclosed"], "cafe", SAT_6PM) is None  # temporarily closed
    no_status = {k: v for k, v in PLACES["ChIJmoosewood"].items() if k != "businessStatus"}
    assert to_candidate(no_status, "food", SAT_6PM) is None


def test_field_mask_asks_for_the_trust_fields() -> None:
    for field in ("businessStatus", "pureServiceAreaBusiness", "userRatingCount", "priceRange"):
        assert f"places.{field}" in FIELD_MASK


# --- opening hours (venue's own timezone) --------------------------------------------------


def test_open_and_closing_time() -> None:
    status, closes = hours_status(PLACES["ChIJmoosewood"], SAT_6PM)
    assert status == "open"
    assert closes == datetime.fromisoformat("2026-10-03T21:00:00-04:00")


def test_closed_outside_periods() -> None:
    assert hours_status(PLACES["ChIJmoosewood"], SUN_930)[0] == "closed"  # opens at 11


def test_evaluated_in_venue_time_whatever_timezone_now_is() -> None:
    utc = datetime.fromisoformat("2026-10-03T22:00:00+00:00")  # = Sat 6 PM in Ithaca
    assert hours_status(PLACES["ChIJmoosewood"], utc)[0] == "open"


def test_saturday_night_period_wraps_into_sunday() -> None:
    status, closes = hours_status(PLACES["ChIJlatebar"], SUN_1AM)
    assert status == "open"
    assert closes == datetime.fromisoformat("2026-10-04T02:00:00-04:00")


def test_always_open_and_unknown() -> None:
    assert hours_status(PLACES["ChIJdiner"], SUN_930) == ("open", None)
    assert hours_status(PLACES["ChIJgimme"], SUN_930) == ("unknown", None)  # no hours


# --- requests, de-duplication, fallback ---------------------------------------------------


class StubGoogle(GooglePlaces):
    def __init__(self, response: dict | Exception) -> None:
        super().__init__(SETTINGS, RecordReplayCache("off"))
        self.response = response
        self.bodies: list[dict] = []

    async def _post(self, body: dict) -> dict:
        self.bodies.append(body)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


async def test_one_call_per_category_with_the_right_filters() -> None:
    g = StubGoogle(SAMPLE)
    await g.search_nearby(CENTER, 2500, [], SAT_6PM)

    assert len(g.bodies) == len(CATEGORY_TYPES)
    food, cafe = g.bodies[0], g.bodies[1]
    assert food["includedTypes"] == ["restaurant"]
    assert cafe["includedPrimaryTypes"] == CATEGORY_TYPES["cafe"]
    assert all(b["excludedPrimaryTypes"] == EXCLUDED_PRIMARY_TYPES for b in g.bodies)
    counts = {
        b.get("includedTypes", b.get("includedPrimaryTypes"))[0]: b["maxResultCount"]
        for b in g.bodies
    }
    assert counts.pop("movie_theater") == 20  # activities: always the full 20
    assert set(counts.values()) == {10}
    circle = food["locationRestriction"]["circle"]
    assert circle["center"] == {"latitude": 42.444, "longitude": -76.483}  # rounded
    assert circle["radius"] == 2500.0


async def test_single_category_asks_for_more_results() -> None:
    g = StubGoogle(SAMPLE)
    await g.search_nearby(CENTER, 2500, ["food"], SAT_6PM)
    assert [b["maxResultCount"] for b in g.bodies] == [20]


async def test_same_place_in_two_categories_appears_once() -> None:
    results = await StubGoogle(SAMPLE).search_nearby(CENTER, 2500, ["food", "cafe"], SAT_6PM)
    ids = [c.candidate_id for c in results]
    assert len(ids) == len(set(ids)) == 4
    assert {c.category for c in results} == {"food"}  # first category wins


async def test_google_failure_falls_back_to_curated_fixture() -> None:
    results = await StubGoogle(ConnectionError("down")).search_nearby(CENTER, 2500, [], SAT_6PM)
    assert results
    assert {c.source for c in results} == {"osm_fixture"}


async def test_geocoding_is_not_limited_to_the_demo_area() -> None:
    class FakeCache:
        requests: list = []

        async def call(self, provider, method, request, fn):
            self.requests.append(request)
            return [{"lat": "40.7580", "lon": "-73.9855", "name": "Times Square"}]

    settings = SETTINGS.model_copy(update={"nominatim_user_agent": "test/0.1 (t@example.com)"})
    g = GooglePlaces(settings, FakeCache())
    near_nyc = LatLng(lat=40.75, lng=-73.99)
    coords, label = await g.geocode("times square", near_nyc)
    assert label == "Times Square"
    params = g.fallback.cache.requests[0]
    assert params["bounded"] == 0
    x1, y1, x2, y2 = (float(v) for v in params["viewbox"].split(","))
    assert x1 < near_nyc.lng < x2 and y2 < near_nyc.lat < y1  # prefers results near them


# --- real prices (priceRange) -------------------------------------------------------


def test_price_range_beats_the_price_level_tier() -> None:
    sushi = {
        **PLACES["ChIJmoosewood"],
        "priceLevel": "PRICE_LEVEL_MODERATE",  # tier says up to $35
        "priceRange": {  # Google's real range, as sent: units is a string
            "startPrice": {"currencyCode": "USD", "units": "10"},
            "endPrice": {"currencyCode": "USD", "units": "20"},
        },
    }
    cost = to_candidate(sushi, "food", SAT_6PM).est_cost_pp
    assert (cost.value, cost.low, cost.high) == (15, 10, 20)
    assert cost.source == "google_price_range" and cost.status == "estimated"


def test_open_ended_and_foreign_price_ranges() -> None:
    open_ended = {"priceRange": {"startPrice": {"currencyCode": "USD", "units": "50"}}}
    cost = place_cost(open_ended, "food")
    assert (cost.value, cost.high) == (50, None)  # "More than $50"
    euros = {
        "priceLevel": "PRICE_LEVEL_INEXPENSIVE",
        "priceRange": {"startPrice": {"currencyCode": "EUR", "units": "10"}},
    }
    assert place_cost(euros, "food").source == "google"  # USD-only: falls back to the tier
    cents = {
        "priceRange": {"startPrice": {"currencyCode": "USD", "units": "9", "nanos": 500000000}}
    }
    assert place_cost(cents, "food").value == Decimal("9.5")


def test_parks_and_animals_are_activities_and_parks_are_free() -> None:
    for t in ("park", "hiking_area", "zoo", "aquarium", "museum"):
        assert t in CATEGORY_TYPES["activity"]
    assert place_cost({"primaryType": "state_park"}, "activity").value == 0
    assert place_cost({"primaryType": "museum"}, "activity").value == 12  # $ estimate


def test_activities_get_their_kind_for_variety_and_the_poll() -> None:
    real = {"businessStatus": "OPERATIONAL", "userRatingCount": 50}
    place = {
        **real,
        "displayName": {"text": "Buttermilk Falls"},
        "location": {"latitude": 42.4, "longitude": -76.5},
        "primaryType": "state_park",
        "types": ["state_park", "park"],
    }
    assert to_candidate(place, "activity", SAT_6PM).cuisines == ["state_park"]
