"""Pull candidate venues from OpenStreetMap (Overpass) into fixtures/venues_raw.json.

    uv run python scripts/fetch_venues.py            # one Overpass request, then cached
    uv run python scripts/fetch_venues.py --refresh  # ask Overpass again

Run by a human, once. One polite request (descriptive User-Agent from settings),
through the record/replay cache so re-runs don't hit Overpass again.

Never touches fixtures/venues.json (the hand-curated file). Instead it reports which
raw venues aren't in venues.json yet, so a human can copy the good ones over and
enter a price_tier for each. Then run scripts/check_venues.py.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.providers.cache import RecordReplayCache  # noqa: E402
from app.settings import get_settings  # noqa: E402

RAW_PATH = ROOT / "fixtures" / "venues_raw.json"
CURATED_PATH = ROOT / "fixtures" / "venues.json"
RADIUS_M = 2500

AMENITY_CATEGORY = {
    "restaurant": "food",
    "fast_food": "food",
    "cafe": "cafe",
    "bar": "bar",
    "pub": "bar",
    "ice_cream": "dessert",
    "cinema": "activity",
}
LEISURE_CATEGORY = {
    "bowling_alley": "activity",
    "escape_game": "activity",
    "miniature_golf": "activity",
    "amusement_arcade": "activity",
}


def build_query(lat: float, lng: float) -> str:
    amenities = "|".join(AMENITY_CATEGORY)
    leisures = "|".join(LEISURE_CATEGORY)
    around = f"(around:{RADIUS_M},{lat},{lng})"
    return (
        "[out:json][timeout:60];\n(\n"
        f'  nwr["name"]["amenity"~"^({amenities})$"]{around};\n'
        f'  nwr["name"]["leisure"~"^({leisures})$"]{around};\n'
        ");\nout center;"
    )


def to_venue(element: dict) -> dict | None:
    """One Overpass element → the venues.json shape (price_tier left null)."""
    tags = element.get("tags", {})
    category = AMENITY_CATEGORY.get(tags.get("amenity", "")) or LEISURE_CATEGORY.get(
        tags.get("leisure", "")
    )
    # Nodes have lat/lon; ways and relations get a `center` from `out center;`.
    point = element if "lat" in element else element.get("center", {})
    if not category or "lat" not in point or not tags.get("name"):
        return None
    address = " ".join(
        part for part in (tags.get("addr:housenumber"), tags.get("addr:street")) if part
    )
    cuisines = [
        c.strip().lower().replace(" ", "_") for c in tags.get("cuisine", "").split(";") if c.strip()
    ]
    venue = {
        "id": f"osm:{element['type']}/{element['id']}",
        "name": tags["name"],
        "category": category,
        "cuisines": cuisines,
        "lat": round(point["lat"], 6),
        "lng": round(point["lon"], 6),
        "address": address,
        "price_tier": None,
    }
    if tags.get("opening_hours"):
        venue["opening_hours"] = tags["opening_hours"]
    return venue


async def fetch(refresh: bool) -> dict:
    settings = get_settings()
    if not settings.nominatim_user_agent:
        sys.exit("Set NOMINATIM_USER_AGENT in .env first (OSM services need a descriptive one).")
    query = build_query(settings.demo_center_lat, settings.demo_center_lng)

    async def live() -> dict:
        print("Asking Overpass (one request)…")
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                settings.overpass_url,
                data={"data": query},
                headers={"User-Agent": settings.nominatim_user_agent},
            )
            response.raise_for_status()
            return response.json()

    cache = RecordReplayCache("record" if refresh else "replay")
    return await cache.call("overpass", "interpreter", {"query": query}, live)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--refresh", action="store_true", help="query Overpass again")
    args = parser.parse_args()

    response = asyncio.run(fetch(args.refresh))
    venues = [v for v in (to_venue(e) for e in response.get("elements", [])) if v]
    venues.sort(key=lambda v: (v["category"], v["name"].lower()))
    RAW_PATH.write_text(json.dumps(venues, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    counts: dict[str, int] = {}
    for v in venues:
        counts[v["category"]] = counts.get(v["category"], 0) + 1
    print(f"Wrote {len(venues)} venues to {RAW_PATH.relative_to(ROOT)}: {counts}")

    if not CURATED_PATH.exists():
        print(f"{CURATED_PATH.relative_to(ROOT)} doesn't exist yet: create it from the raw file.")
        return
    curated_ids = {v.get("id") for v in json.loads(CURATED_PATH.read_text(encoding="utf-8"))}
    new = [v for v in venues if v["id"] not in curated_ids]
    print(f"\n{len(new)} raw venues are not in {CURATED_PATH.name} (not modified):")
    for v in new:
        print(f"  {v['id']:<22} {v['category']:<9} {v['name']}")


if __name__ == "__main__":
    main()
