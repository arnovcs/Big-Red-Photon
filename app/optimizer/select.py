"""Top-k selection with variety (§13.6)."""

from app.models.plans import Plan


def _kind(plan: Plan) -> tuple[str, str | None]:
    """Category plus primary cuisine, e.g. ("food", "sushi"). For a combo plan (court +
    boba) every option has the same kinds, so its first stop is what varies."""
    cand = plan.candidate
    if cand.extra_stops:
        return "combo", cand.candidate_id.split("+")[0]
    return cand.category.lower(), (cand.cuisines[0].lower() if cand.cuisines else None)


def select(plans: list[Plan], k: int = 3) -> list[Plan]:
    """`plans` must be sorted best first (as `rank` returns them).

    Take the best. Each next pick is the best remaining plan whose category or primary
    cuisine differs from every pick so far; if none differs, the next best overall.
    """
    picked: list[Plan] = []
    remaining = list(plans)
    while remaining and len(picked) < k:
        kinds = {_kind(p) for p in picked}
        choice = next((p for p in remaining if _kind(p) not in kinds), remaining[0])
        picked.append(choice)
        remaining.remove(choice)
    return picked
