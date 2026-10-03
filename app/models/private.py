"""Private data models (§6.2). Read/written only through `app/private/`."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class TravelModes(BaseModel):
    walk: bool = True
    bike: bool = False
    drive: bool = False  # own car
    rideshare: bool = True


class LatLng(BaseModel):
    lat: float
    lng: float


class PrivateProfile(BaseModel):
    user_id: UUID
    nessie_customer_id: str | None
    spend_limit_usd: Decimal
    limit_source: Literal["nessie_estimate", "user_override"]
    origin: LatLng
    origin_label: str  # "Olin Library" — DM only
    modes: TravelModes
    updated_at: datetime


class PrivateConstraints(BaseModel):
    """The only private data the optimizer sees."""

    pid: str  # "p1".."pN", per-session pseudonym
    spend_limit_usd: Decimal
    origin: LatLng
    modes: TravelModes


class FinancialSnapshot(BaseModel):
    """Returned by FinanceProvider (§8). Consumed only by `app/private/budget.py`."""

    checking_balance: Decimal
    upcoming_bills_14d: Decimal
    recent_outing_amounts: list[Decimal]  # dining/entertainment purchases, last 60 days
