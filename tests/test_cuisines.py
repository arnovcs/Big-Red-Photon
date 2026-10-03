"""Cuisine requests: families ("asian" ⊇ sushi), cuisine search, shortlist priority.

The bug this covers: "I want asian food" + "I want japanese food" got Collegetown
Bagels, because no Japanese place was searched for and "asian" never matched "sushi".
"""

import json
from datetime import datetime
from pathlib import Path

from app.models.conversation import ConstraintField as F
from app.models.conversation import ConstraintKind as K
from app.models.private import LatLng
from app.models.routing import Mode
from app.optimizer import cuisines
from app.optimizer.burden import satisfies
from app.optimizer.feasibility import is_vetoed
from app.planning.pipeline import _shortlist, _wanted_cuisines
from app.providers.cache import RecordReplayCache
from app.providers.real.google_places import (
    CUISINE_SEARCH_TYPES,
    MAX_TYPES_PER_REQUEST,
    GooglePlaces,
    _cuisines,
    cuisine_types,
)
from app.settings import Settings
from tests.optimizer_helpers import index, person, pref, prefs, run, trip, venue

SAMPLE = json.loads((Path(__file__).parent / "fixtures" / "google_nearby_sample.json").read_text())
SAT_6PM = datetime.fromisoformat("2026-10-03T18:00:00-04:00")
CENTER = LatLng(lat=42.444, lng=-76.483)

# --- families (B) ------------------------------------------------------------------


def test_asian_and_japanese_families() -> None:
    assert cuisines.matches(["asian"], ["sushi"])
    assert cuisines.matches(["asian"], ["thai"])
    assert cuisines.matches(["japanese"], ["ramen"])
    assert cuisines.matches(["Korean BBQ"], ["korean_barbecue"])
    assert not cuisines.matches(["japanese"], ["chinese"])
    assert not cuisines.matches(["asian"], ["bagels"])
    assert not cuisines.matches(["sushi"], ["japanese"])  # narrow asks stay narrow
    assert not cuisines.matches([], ["sushi"])


def test_preference_and_veto_use_families() -> None:
    sushi = venue("sushi_place", cuisines=("sushi",))
    bagels = venue("bagels", cuisines=("bagels",))
    wants_asian = pref("p1", F.CUISINE, "asian", K.SOFT)
    assert satisfies(wants_asian, sushi) is True
    assert satisfies(wants_asian, bagels) is False
    no_japanese = prefs(pref("p1", F.CUISINE, "japanese", K.VETO, polarity="avoid"))
    assert is_vetoed(sushi, no_japanese)
    assert not is_vetoed(bagels, no_japanese)


def test_asian_plus_japanese_picks_sushi_over_a_closer_bagel_shop() -> None:
    sushi = venue("sushi_place", cuisines=("sushi",))
    bagels = venue("bagels", cuisines=("bagels",))
    people = [person("p1"), person("p2")]
    estimates = index(
        *(trip(p, "bagels", Mode.WALK, 4) for p in ("p1", "p2")),
        *(trip(p, "sushi_place", Mode.WALK, 9) for p in ("p1", "p2")),
    )
    wants = prefs(pref("p1", F.CUISINE, "asian", K.SOFT), pref("p2", F.CUISINE, "japanese", K.SOFT))
    assert run([bagels, sushi], estimates, people, wants)[0].candidate.candidate_id == (
        "sushi_place"
    )


# --- pipeline (A) ------------------------------------------------------------------


def test_wanted_cuisines_skip_vetoes_and_duplicates() -> None:
    p = prefs(
        pref("p1", F.CUISINE, "asian", K.SOFT),
        pref("p2", F.CUISINE, ["japanese", "Asian"], K.SOFT),
        pref("p3", F.CUISINE, "sushi", K.VETO, polarity="avoid"),
        pref("p3", F.CATEGORY, "cafe", K.SOFT),
    )
    assert _wanted_cuisines(p) == ["asian", "japanese"]


def test_shortlist_keeps_wanted_cuisines_first() -> None:
    many = [venue(f"v{i}", cuisines=("pizza",), rating=4.9) for i in range(25)]
    ramen = venue("ramen", cuisines=("ramen",), rating=4.0)
    shortlist = _shortlist([*many, ramen], ["japanese"])
    assert shortlist[0].candidate_id == "ramen" and len(shortlist) == 20
    # Not asked for: no longer first, but kept for variety (one of each kind gets a turn).
    unasked = [c.candidate_id for c in _shortlist([*many, ramen], [])]
    assert unasked[0] != "ramen" and "ramen" in unasked


# --- Google cuisine search (A) -----------------------------------------------------


def test_cuisine_types_are_real_google_types() -> None:
    japanese = cuisine_types(["japanese"])
    assert {"japanese_restaurant", "sushi_restaurant", "ramen_restaurant"} <= set(japanese)
    asian = cuisine_types(["asian", "japanese"])
    assert "thai_restaurant" in asian and "noodle_shop" in asian
    assert set(asian) <= CUISINE_SEARCH_TYPES  # only names Google accepts
    assert len(cuisine_types(list(cuisines.FAMILIES))) <= MAX_TYPES_PER_REQUEST
    assert cuisine_types(["martian"]) == []


def test_place_cuisines_include_secondary_types() -> None:
    place = {"primaryType": "restaurant", "types": ["japanese_restaurant", "restaurant", "food"]}
    assert _cuisines(place) == ["japanese"]
    assert _cuisines({"primaryType": "sushi_restaurant", "types": ["ramen_restaurant"]}) == [
        "sushi",
        "ramen",
    ]


class StubGoogle(GooglePlaces):
    def __init__(self) -> None:
        super().__init__(
            Settings(_env_file=None, google_places_api_key="k"), RecordReplayCache("off")
        )
        self.bodies: list[dict] = []

    async def _post(self, body: dict, *args, **kwargs) -> dict:
        self.bodies.append(body)
        return SAMPLE


async def test_wanted_cuisine_gets_its_own_search_first() -> None:
    g = StubGoogle()
    await g.search_nearby(CENTER, 2500, ["food"], SAT_6PM, cuisines=["japanese"])
    first, second = g.bodies
    assert "sushi_restaurant" in first["includedTypes"] and first["maxResultCount"] == 20
    assert second["includedTypes"] == ["restaurant"]


async def test_no_cuisine_no_extra_search() -> None:
    g = StubGoogle()
    await g.search_nearby(CENTER, 2500, ["food"], SAT_6PM)
    assert [b["includedTypes"] for b in g.bodies] == [["restaurant"]]


async def test_a_cuisine_with_no_google_type_is_searched_by_name() -> None:
    g = StubGoogle()  # "boba" has no restaurant type: Text Search for it, then the category
    await g.search_nearby(CENTER, 2500, ["food"], SAT_6PM, cuisines=["boba"])
    assert g.bodies[0]["textQuery"] == "boba"
    assert g.bodies[1]["includedTypes"] == ["restaurant"]
