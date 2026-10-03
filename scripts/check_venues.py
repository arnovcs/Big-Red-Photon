"""Validate the hand-curated venue fixture before committing it.

    uv run python scripts/check_venues.py                 # checks fixtures/venues.json
    uv run python scripts/check_venues.py path/to/file.json

Errors (exit 1): missing/invalid coordinates, category, or price tier; ids that
aren't unique or aren't osm:<node|way|relation>/<number>.
Warnings: opening_hours in a form the bot can't read (it will be treated as "unknown").
Prints a category and price-tier summary, and the cost each tier maps to (§9.3).
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.models.candidates import DEFAULT_DURATION_MIN  # noqa: E402
from app.providers import opening_hours  # noqa: E402
from app.providers.costs import PRICE_TIERS  # noqa: E402

CATEGORIES = set(DEFAULT_DURATION_MIN)
TIERS = set(PRICE_TIERS) | {"free"}
ID_PATTERN = re.compile(r"^osm:(node|way|relation)/\d+$")


def validate(venues: object) -> tuple[list[str], list[str]]:
    """(errors, warnings) for a parsed venues.json."""
    if not isinstance(venues, list):
        return ["venues.json must be a JSON list of venues"], []
    errors: list[str] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for i, v in enumerate(venues):
        if not isinstance(v, dict):
            errors.append(f"#{i}: not an object")
            continue
        label = f"#{i} {v.get('name') or v.get('id') or '(no name)'}"
        vid = v.get("id")
        if not isinstance(vid, str) or not ID_PATTERN.match(vid):
            errors.append(f"{label}: id {vid!r} is not osm:<node|way|relation>/<id>")
        elif vid in seen:
            errors.append(f"{label}: duplicate id {vid}")
        else:
            seen.add(vid)
        if not v.get("name"):
            errors.append(f"{label}: missing name")
        lat, lng = v.get("lat"), v.get("lng")
        if not isinstance(lat, int | float) or not -90 <= lat <= 90:
            errors.append(f"{label}: missing or invalid lat")
        if not isinstance(lng, int | float) or not -180 <= lng <= 180:
            errors.append(f"{label}: missing or invalid lng")
        if v.get("category") not in CATEGORIES:
            errors.append(f"{label}: category {v.get('category')!r} not in {sorted(CATEGORIES)}")
        if v.get("price_tier") not in TIERS:
            errors.append(f"{label}: price_tier {v.get('price_tier')!r} not in {sorted(TIERS)}")
        hours = v.get("opening_hours")
        if hours and not opening_hours.is_supported(hours):
            warnings.append(f"{label}: opening_hours {hours!r} not understood → 'unknown'")
    return errors, warnings


def summary(venues: list[dict]) -> str:
    categories = Counter(v.get("category") for v in venues)
    tiers = Counter(v.get("price_tier") for v in venues)
    lines = [f"{len(venues)} venues"]
    lines.append("  by category: " + ", ".join(f"{k} {n}" for k, n in sorted(categories.items())))
    lines.append(
        "  by price tier: " + ", ".join(f"{k} {n}" for k, n in sorted(tiers.items(), key=str))
    )
    lines.append(
        "  tier → est. cost per person: "
        + ", ".join(f"{t} ${v} (${lo}–{hi})" for t, (v, lo, hi) in PRICE_TIERS.items())
        + ", free $0"
    )
    return "\n".join(lines)


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "fixtures" / "venues.json"
    venues = json.loads(path.read_text(encoding="utf-8"))
    errors, warnings = validate(venues)
    if isinstance(venues, list):
        print(summary([v for v in venues if isinstance(v, dict)]))
    for w in warnings:
        print(f"WARNING {w}")
    for e in errors:
        print(f"ERROR   {e}")
    if errors:
        sys.exit(f"\n{len(errors)} error(s) in {path.name}")
    print(f"\n{path.name} looks good.")


if __name__ == "__main__":
    main()
