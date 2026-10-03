"""Deterministic cost formulas shared by mock and real providers (§5, §9.3, §9.4)."""

from decimal import ROUND_HALF_UP, Decimal

from app.models.candidates import Uncertain
from app.settings import Settings

CENTS = Decimal("0.01")

# Minutes added to own-car trips for parking (§9.4, §14.2).
DRIVE_PARKING_MIN = 5

# §9.3 price tier → (typical, low, high) per person, USD.
PRICE_TIERS: dict[str, tuple[int, int, int]] = {
    "$": (12, 8, 15),
    "$$": (25, 15, 35),
    "$$$": (45, 35, 60),
    "$$$$": (75, 60, 100),
}


def money(amount: Decimal) -> Decimal:
    return amount.quantize(CENTS, rounding=ROUND_HALF_UP)


def _dec(x: float) -> Decimal:
    return Decimal(str(round(x, 4)))


def drive_cost(distance_mi: float, settings: Settings) -> Decimal:
    """Own car: gas estimate plus parking."""
    return money(_dec(distance_mi) * settings.drive_cost_per_mile_usd + settings.drive_parking_usd)


def rideshare_fare(distance_mi: float, drive_min: float, settings: Settings) -> Decimal:
    """max(min_fare, base + booking + per_mile × mi + per_min × min)."""
    fare = (
        settings.rideshare_base_usd
        + settings.rideshare_booking_usd
        + settings.rideshare_per_mile_usd * _dec(distance_mi)
        + settings.rideshare_per_min_usd * _dec(drive_min)
    )
    return money(max(settings.rideshare_min_fare_usd, fare))


def tier_cost(tier: str | None, source: str = "hand_entered") -> Uncertain[Decimal]:
    """Price tier → per-person cost estimate. "free" → $0. Missing tier → unknown."""
    if tier == "free":
        return Uncertain[Decimal](
            value=Decimal(0), low=Decimal(0), high=Decimal(0), status="known", source=source
        )
    if tier not in PRICE_TIERS:
        return Uncertain[Decimal](value=None, status="unknown", source=source)
    value, low, high = PRICE_TIERS[tier]
    return Uncertain[Decimal](
        value=Decimal(value),
        low=Decimal(low),
        high=Decimal(high),
        status="estimated",
        source=source,
    )


def tier_for_cost(cost: Decimal) -> str:
    """Per-person cost → group-facing price tier."""
    for tier, (_, _, high) in PRICE_TIERS.items():
        if cost <= high:
            return tier
    return "$$$$"
