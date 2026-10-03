"""The only code that reads or writes `private_profiles` (§11).

Readers outside onboarding may use only `constraints_for()` (planning pipeline) and
`itinerary_context_for()` (delivery). The write helpers are for onboarding only.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import PrivateProfileRow
from app.models.private import LatLng, PrivateConstraints, TravelModes

# Stage 1 stub: bank code → fixed limit (the §12.3 expected limits).
# Stage 5 replaces this with Nessie lookup + budget.estimate().
_STUB_BANK_LIMITS: dict[str, Decimal] = {
    "MAYA1": Decimal(30),
    "SAM1": Decimal(50),
    "JORDAN1": Decimal(15),
}


def _now() -> datetime:
    return datetime.now(UTC)


async def _profile(db: AsyncSession, user_id: uuid.UUID) -> PrivateProfileRow | None:
    return await db.get(PrivateProfileRow, user_id)


async def link_customer(db: AsyncSession, user_id: uuid.UUID, bank_code: str) -> Decimal | None:
    """Link a (sandbox) bank by code and store the estimated limit. None if unknown code."""
    limit = _STUB_BANK_LIMITS.get(bank_code.strip().upper())
    if limit is None:
        return None
    profile = await _profile(db, user_id)
    if profile is None:
        profile = PrivateProfileRow(user_id=user_id)
        db.add(profile)
    profile.nessie_customer_id = None
    profile.spend_limit_usd = limit
    profile.limit_source = "nessie_estimate"
    profile.updated_at = _now()
    await db.flush()
    return limit


async def set_limit(
    db: AsyncSession,
    user_id: uuid.UUID,
    amount: Decimal,
    source: Literal["nessie_estimate", "user_override"],
) -> bool:
    profile = await _profile(db, user_id)
    if profile is None:
        return False
    profile.spend_limit_usd = amount
    profile.limit_source = source
    profile.updated_at = _now()
    await db.flush()
    return True


async def set_origin(db: AsyncSession, user_id: uuid.UUID, origin: LatLng, label: str) -> None:
    profile = await _profile(db, user_id)
    if profile is None:
        return
    profile.origin_lat, profile.origin_lng, profile.origin_label = origin.lat, origin.lng, label
    profile.updated_at = _now()
    await db.flush()


async def clear_origin(db: AsyncSession, user_id: uuid.UUID) -> None:
    profile = await _profile(db, user_id)
    if profile is None:
        return
    profile.origin_lat = profile.origin_lng = profile.origin_label = None
    await db.flush()


async def origin_label(db: AsyncSession, user_id: uuid.UUID) -> str | None:
    """The user's own origin label, for their DM only."""
    profile = await _profile(db, user_id)
    return profile.origin_label if profile else None


async def get_modes(db: AsyncSession, user_id: uuid.UUID) -> TravelModes | None:
    profile = await _profile(db, user_id)
    if profile is None or profile.modes_json is None:
        return None
    return TravelModes.model_validate_json(profile.modes_json)


async def set_modes(db: AsyncSession, user_id: uuid.UUID, modes: TravelModes) -> None:
    profile = await _profile(db, user_id)
    if profile is None:
        return
    profile.modes_json = modes.model_dump_json()
    profile.updated_at = _now()
    await db.flush()


async def constraints_for(
    db: AsyncSession, pid_map: dict[str, uuid.UUID]
) -> dict[str, PrivateConstraints]:
    """Pseudonymous constraints for complete profiles. Incomplete profiles are skipped."""
    rows = await db.scalars(
        select(PrivateProfileRow).where(PrivateProfileRow.user_id.in_(pid_map.values()))
    )
    by_user = {row.user_id: row for row in rows}
    result: dict[str, PrivateConstraints] = {}
    for pid, user_id in pid_map.items():
        row = by_user.get(user_id)
        if row is None or row.origin_lat is None or row.origin_lng is None or not row.modes_json:
            continue
        result[pid] = PrivateConstraints(
            pid=pid,
            spend_limit_usd=row.spend_limit_usd,
            origin=LatLng(lat=row.origin_lat, lng=row.origin_lng),
            modes=TravelModes.model_validate_json(row.modes_json),
        )
    return result


async def itinerary_context_for(
    db: AsyncSession, user_ids: list[uuid.UUID]
) -> dict[uuid.UUID, LatLng]:
    """Origins for delivery routing. Each value goes only into that user's own DM route."""
    rows = await db.scalars(
        select(PrivateProfileRow).where(PrivateProfileRow.user_id.in_(user_ids))
    )
    return {
        row.user_id: LatLng(lat=row.origin_lat, lng=row.origin_lng)
        for row in rows
        if row.origin_lat is not None and row.origin_lng is not None
    }
