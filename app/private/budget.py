"""Deterministic spend-limit estimator (§12.1). Pure: no I/O, no LLM."""

from decimal import ROUND_HALF_UP, Decimal
from statistics import median

from app.models.private import FinancialSnapshot

MIN_BUFFER_USD = Decimal(50)
BUFFER_SHARE = Decimal("0.10")
ROOM_SHARE = Decimal("0.15")
OUTING_MULTIPLIER = Decimal("1.25")
DEFAULT_TYPICAL_OUTING_USD = Decimal(20)
MIN_LIMIT_USD = Decimal(10)
MAX_LIMIT_USD = Decimal(150)


def _round_to_nearest_5(amount: Decimal) -> Decimal:
    return (amount / 5).quantize(Decimal(1), rounding=ROUND_HALF_UP) * 5


def estimate_limit(snapshot: FinancialSnapshot) -> Decimal:
    """A comfortable spend limit for tonight, from balance, upcoming bills, and habits."""
    balance = snapshot.checking_balance
    buffer = max(MIN_BUFFER_USD, BUFFER_SHARE * balance)
    discretionary_room = max(Decimal(0), balance - snapshot.upcoming_bills_14d - buffer)
    if snapshot.recent_outing_amounts:
        typical_outing = Decimal(median(snapshot.recent_outing_amounts))
    else:
        typical_outing = DEFAULT_TYPICAL_OUTING_USD
    raw = min(ROOM_SHARE * discretionary_room, OUTING_MULTIPLIER * typical_outing)
    clamped = min(max(raw, MIN_LIMIT_USD), MAX_LIMIT_USD)
    return _round_to_nearest_5(clamped)
