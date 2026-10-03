"""Top-k selection (§13.6). Stage 1: best k by score. Stage 2 adds category diversity."""

from app.models.plans import Plan


def select(plans: list[Plan], k: int = 3) -> list[Plan]:
    return plans[:k]
