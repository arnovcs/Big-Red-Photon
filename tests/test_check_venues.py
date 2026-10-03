"""scripts/check_venues.py validation of the hand-curated venue fixture."""

from scripts.check_venues import summary, validate


def good(**overrides) -> dict:
    venue = {
        "id": "osm:node/123",
        "name": "Koko",
        "category": "food",
        "lat": 42.442,
        "lng": -76.4852,
        "price_tier": "$$",
    }
    return {**venue, **overrides}


def test_valid_file_has_no_errors() -> None:
    venues = [good(), good(id="osm:way/456", name="Cinemapolis", category="activity")]
    assert validate(venues) == ([], [])


def test_missing_fields_are_errors() -> None:
    errors, _ = validate([good(lat=None), good(id="osm:node/2", category="spa", price_tier=None)])
    text = "\n".join(errors)
    assert "invalid lat" in text
    assert "category 'spa'" in text
    assert "price_tier None" in text


def test_ids_must_be_unique_and_osm_shaped() -> None:
    errors, _ = validate([good(), good(), good(id="fx:koko")])
    assert any("duplicate id osm:node/123" in e for e in errors)
    assert any("'fx:koko'" in e for e in errors)


def test_free_is_an_allowed_tier() -> None:
    assert validate([good(price_tier="free", category="activity")])[0] == []


def test_unreadable_opening_hours_is_only_a_warning() -> None:
    errors, warnings = validate([good(opening_hours="Mo-Fr 10:00-14:00; PH off")])
    assert errors == []
    assert len(warnings) == 1


def test_not_a_list_is_an_error() -> None:
    assert validate({"oops": True})[0]


def test_summary_counts_categories_and_tiers() -> None:
    text = summary([good(), good(id="osm:node/2", category="cafe", price_tier="$")])
    assert "cafe 1" in text and "food 1" in text
    assert "$ 1" in text and "$$ 1" in text
