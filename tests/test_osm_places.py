"""OSM places provider (§9.3): fixture search, opening hours, geocode fallback."""

from datetime import datetime
from pathlib import Path

from app.models.private import LatLng
from app.providers.real.osm_places import OsmPlaces
from app.settings import Settings

TEST_VENUES = str(Path(__file__).parent / "fixtures" / "venues_test.json")
CENTER = LatLng(lat=42.4440, lng=-76.4830)
SAT_6PM = datetime.fromisoformat("2026-10-03T18:00:00-04:00")


class FakeCache:
    """Stands in for the record/replay cache: returns a canned Nominatim response."""

    def __init__(self, response) -> None:
        self.response = response
        self.requests: list[tuple[str, str, dict]] = []

    async def call(self, provider, method, request, fn):
        self.requests.append((provider, method, request))
        return self.response


def places(response=None, user_agent="bigredhacks-test/0.1 (test@example.com)") -> OsmPlaces:
    settings = Settings(_env_file=None, venues_path=TEST_VENUES, nominatim_user_agent=user_agent)
    return OsmPlaces(settings, FakeCache(response))


async def test_geocode_uses_demo_locations_without_nominatim() -> None:
    p = places(response=[{"lat": "0", "lon": "0", "name": "WRONG"}])
    coords, label = await p.geocode("meet me at olin", CENTER)
    assert label == "Olin Library"
    assert p.cache.requests == []  # no Nominatim call


async def test_geocode_falls_back_to_nominatim_through_cache() -> None:
    p = places(response=[{"lat": "42.4396", "lon": "-76.4970", "name": "Ithaca Bakery"}])
    coords, label = await p.geocode("ithaca bakery", CENTER)
    assert (coords.lat, coords.lng, label) == (42.4396, -76.4970, "Ithaca Bakery")
    provider, method, params = p.cache.requests[0]
    assert (provider, method) == ("nominatim", "search")
    assert params["format"] == "jsonv2"
    assert params["limit"] == 1
    assert params["bounded"] == 1
    x1, y1, x2, y2 = (float(v) for v in params["viewbox"].split(","))
    assert x1 < CENTER.lng < x2 and y2 < CENTER.lat < y1  # lon/lat box around the demo


async def test_geocode_returns_none_when_nominatim_finds_nothing() -> None:
    assert await places(response=[]).geocode("nowhere at all", CENTER) is None


async def test_geocode_skips_nominatim_without_a_user_agent() -> None:
    p = places(response=[{"lat": "1", "lon": "1"}], user_agent="")
    assert await p.geocode("somewhere new", CENTER) is None
    assert p.cache.requests == []


async def test_search_nearby_filters_category_and_distance() -> None:
    p = places()
    cafes = await p.search_nearby(CENTER, 2500, ["cafe"], SAT_6PM)
    assert cafes and all(c.category == "cafe" for c in cafes)
    assert await p.search_nearby(LatLng(lat=40.0, lng=-74.0), 2500, [], SAT_6PM) == []


async def test_search_nearby_sets_opening_status_and_tier_cost() -> None:
    p = places()
    p.venues = [
        {
            "id": "osm:node/1",
            "name": "Late Spot",
            "category": "food",
            "lat": CENTER.lat,
            "lng": CENTER.lng,
            "price_tier": "$$",
            "opening_hours": "Mo-Su 11:00-22:00",
        },
        {
            "id": "osm:node/2",
            "name": "Lunch Only",
            "category": "food",
            "lat": CENTER.lat,
            "lng": CENTER.lng,
            "price_tier": "$",
            "opening_hours": "Mo-Fr 11:00-14:00",
        },
        {
            "id": "osm:node/3",
            "name": "Weird Hours",
            "category": "food",
            "lat": CENTER.lat,
            "lng": CENTER.lng,
            "price_tier": "$",
            "opening_hours": "Mo-Fr 11:00-14:00; PH off",
        },
    ]
    found = {c.name: c for c in await p.search_nearby(CENTER, 500, ["food"], SAT_6PM)}
    assert found["Late Spot"].open_at_target == "open"
    assert found["Late Spot"].closes_at.hour == 22
    assert found["Late Spot"].est_cost_pp.value == 25
    assert found["Lunch Only"].open_at_target == "closed"
    assert found["Weird Hours"].open_at_target == "unknown"


async def test_text_search_prefers_fixture_venues() -> None:
    p = places(response=[{"lat": "1", "lon": "1", "name": "x"}])
    results = await p.text_search("koko", CENTER)
    assert [c.name for c in results] == ["Koko"]
    assert p.cache.requests == []
