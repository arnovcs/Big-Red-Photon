"""An offline PlacesProvider for tests: a few Ithaca venues and landmarks as Python data.

The app itself has no local place data (everything comes from Google Places); this
only stands in for Google so tests run without a network or an API key.
"""

from datetime import datetime

from app.models.candidates import DEFAULT_DURATION_MIN, Candidate, ResolvedPlace
from app.models.private import LatLng
from app.providers.costs import tier_cost
from app.providers.mock.routing import haversine_mi

METERS_PER_MILE = 1609.34

# (id, name, category, cuisines, lat, lng, address, price tier)
VENUES = [
    ("koko", "Koko", "food", ["korean"], 42.442, -76.4852, "405 College Ave", "$$"),
    (
        "collegetown-bagels",
        "Collegetown Bagels",
        "cafe",
        ["bagels"],
        42.4417,
        -76.4853,
        "415 College Ave",
        "$",
    ),
    (
        "louies-lunch",
        "Louie's Lunch",
        "food",
        ["american"],
        42.4524,
        -76.4808,
        "Thurston Ave, North Campus",
        "$",
    ),
    (
        "viva-taqueria",
        "Viva Taqueria",
        "food",
        ["mexican"],
        42.4398,
        -76.4958,
        "101 N Aurora St",
        "$",
    ),
    (
        "plum-tree",
        "Plum Tree Japanese Restaurant",
        "food",
        ["japanese", "sushi"],
        42.44,
        -76.4958,
        "113 N Aurora St",
        "$$",
    ),
    ("moosewood", "Moosewood", "food", ["vegetarian"], 42.441, -76.4985, "215 N Cayuga St", "$$"),
    (
        "saigon-kitchen",
        "Saigon Kitchen",
        "food",
        ["vietnamese"],
        42.4391,
        -76.5056,
        "526 W State St",
        "$$",
    ),
    (
        "gola-osteria",
        "Gola Osteria",
        "food",
        ["italian"],
        42.4385,
        -76.489,
        "115 S Quarry St",
        "$$$",
    ),
    (
        "purity",
        "Purity Ice Cream",
        "dessert",
        ["ice_cream"],
        42.4459,
        -76.5068,
        "700 Cascadilla St",
        "$",
    ),
    ("cinemapolis", "Cinemapolis", "activity", [], 42.4386, -76.497, "120 E Green St", "$"),
]
VENUE_NAMES = [v[1] for v in VENUES]

# What people type → (name, lat, lng). Real Google returns a name and address; here the
# address is left empty so confirmations read "Got it: Olin Library. Right?".
LANDMARKS = {
    "olin": ("Olin Library", 42.4479, -76.4843),
    "olin library": ("Olin Library", 42.4479, -76.4843),
    "collegetown": ("Collegetown", 42.4422, -76.4852),
    "north campus": ("Robert Purcell Community Center", 42.4560, -76.4777),
    "downtown": ("Ithaca Commons", 42.4393, -76.4977),
    "the commons": ("Ithaca Commons", 42.4393, -76.4977),
    "commons": ("Ithaca Commons", 42.4393, -76.4977),
}
FILLER = ("i'm at ", "im at ", "i am at ", "meet me at ", "near ", "at ")


def _candidate(v: tuple) -> Candidate:
    vid, name, category, cuisines, lat, lng, address, tier = v
    return Candidate(
        candidate_id=f"google:{vid}",
        name=name,
        category=category,
        cuisines=cuisines,
        location=LatLng(lat=lat, lng=lng),
        address=address,
        est_cost_pp=tier_cost(tier, source="google"),
        typical_duration_min=DEFAULT_DURATION_MIN[category],
        source="google",
    )


class StubPlaces:
    def __init__(self) -> None:
        self.lookups: list[str] = []  # what was typed, in order

    async def search_nearby(
        self,
        center: LatLng,
        radius_m: int,
        categories: list[str],
        open_at: datetime,
        cuisines: list[str] | None = None,
    ) -> list[Candidate]:
        radius_mi = radius_m / METERS_PER_MILE
        return [
            _candidate(v)
            for v in VENUES
            if (not categories or v[2] in categories)
            and haversine_mi(center, LatLng(lat=v[4], lng=v[5])) <= radius_mi
        ]

    async def text_search(self, query: str, near: LatLng) -> list[Candidate]:
        q = query.lower()
        return [_candidate(v) for v in VENUES if v[1].lower() in q or q in v[1].lower()]

    async def geocode(self, text: str, near: LatLng) -> ResolvedPlace | None:
        self.lookups.append(text)
        q = " ".join(text.lower().split()).strip(".!?")
        for prefix in FILLER:
            q = q.removeprefix(prefix)
        if q not in LANDMARKS:
            return None
        name, lat, lng = LANDMARKS[q]
        return ResolvedPlace(
            location=LatLng(lat=lat, lng=lng), name=name, address="", place_id=f"stub:{q}"
        )
