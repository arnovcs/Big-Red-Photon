"""Cuisine families, so "asian" matches a sushi place and "japanese" matches ramen.

Venue cuisines come from Google place types with "_restaurant" dropped (e.g.
"sushi_restaurant" → "sushi", "korean_barbecue_restaurant" → "korean_barbecue"), or
from the curated fixture. Pure data + lookups: no I/O.
"""

JAPANESE = {
    "japanese",
    "sushi",
    "ramen",
    "japanese_curry",
    "japanese_izakaya",
    "tonkatsu",
    "yakiniku",
    "yakitori",
}
CHINESE = {"chinese", "cantonese", "dim_sum", "dumpling", "hot_pot", "sichuan", "szechuan"}
KOREAN = {"korean", "korean_barbecue"}
SOUTHEAST_ASIAN = {
    "thai",
    "vietnamese",
    "pho",
    "filipino",
    "malaysian",
    "indonesian",
    "burmese",
    "cambodian",
}
INDIAN = {"indian", "north_indian", "south_indian"}
# "Asian food" in the US usually means East / Southeast Asian.
ASIAN = (
    JAPANESE
    | CHINESE
    | KOREAN
    | SOUTHEAST_ASIAN
    | {"asian", "asian_fusion", "taiwanese", "mongolian_barbecue", "tibetan", "noodles"}
)

FAMILIES: dict[str, set[str]] = {
    "asian": ASIAN,
    "japanese": JAPANESE,
    "chinese": CHINESE,
    "korean": KOREAN,
    "korean_bbq": {"korean", "korean_barbecue"},
    "indian": INDIAN,
    "southeast_asian": SOUTHEAST_ASIAN,
    "noodles": {"noodles", "ramen", "pho"},
    "bbq": {"barbecue", "korean_barbecue", "mongolian_barbecue"},
    "barbecue": {"barbecue", "korean_barbecue", "mongolian_barbecue"},
    "mexican": {"mexican", "taco", "burrito", "tex_mex"},
    "mediterranean": {"mediterranean", "greek", "lebanese", "middle_eastern", "turkish"},
    "middle_eastern": {"middle_eastern", "lebanese", "falafel", "shawarma", "turkish"},
    "burgers": {"hamburger", "burgers"},
    "seafood": {"seafood", "sushi", "oyster_bar"},
}


def normalize(cuisine: str) -> str:
    """ "Korean BBQ" → "korean_bbq"; "dim-sum" → "dim_sum"."""
    return "_".join(cuisine.lower().replace("-", " ").split())


def expand(cuisine: str) -> set[str]:
    """The cuisine plus everything in its family."""
    c = normalize(cuisine)
    return {c} | FAMILIES.get(c, set())


def matches(wanted: list[str], venue_cuisines: list[str]) -> bool:
    """Does any venue cuisine fall in any wanted cuisine's family?"""
    have = {normalize(x) for x in venue_cuisines}
    return any(expand(w) & have for w in wanted)
