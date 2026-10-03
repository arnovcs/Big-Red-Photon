"""The only code that reads or writes `private_profiles` (§11).

Read API (planning/pipeline.py and delivery/itinerary.py only): `constraints_for()`,
`itinerary_context_for()`, `guard_secrets_for()`.
Write API (onboarding/fsm.py only): `link_customer()`, `set_limit()`, `set_origin()`,
`clear_origin()`, `confirm_origin()`, `set_modes()`, `clear_modes()`, `set_drive()`.
Writes return at most a status or the value just written, never another stored value.
Refresh API (planning/pipeline.py only): `refresh_shared_origins()` swaps in each
member's current Find My location at @go; it returns a count, never a location.
tests/test_privacy_boundary.py enforces both lists.
"""

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversation import copy
from app.db.tables import PrivateProfileRow
from app.logging import get_logger, kv
from app.models.private import LatLng, PrivateConstraints, TravelModes
from app.private import budget
from app.providers.protocols import FinanceProvider, MessagingProvider

log = get_logger(__name__)

PERSONAS_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "personas.json"


@lru_cache
def _personas_by_code() -> dict[str, dict]:
    """Bank code → persona from fixtures/personas.json (written by scripts/seed_nessie.py)."""
    data = json.loads(PERSONAS_PATH.read_text(encoding="utf-8"))
    return {p["bank_code"].upper(): p for p in data["personas"]}


def _as_utc(dt: datetime) -> datetime:
    """SQLite drops tzinfo; stored times are UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _now() -> datetime:
    return datetime.now(UTC)


async def _profile(db: AsyncSession, user_id: uuid.UUID) -> PrivateProfileRow | None:
    return await db.get(PrivateProfileRow, user_id)


async def _estimate(persona: dict, finance: FinanceProvider | None) -> Decimal:
    """Limit from the persona's live Nessie data.

    Degrades to the persona's precomputed limit (the same §12 formula run on the
    seeded data) when Nessie is not configured, not seeded, or not reachable.
    """
    customer_id = persona.get("nessie_customer_id")
    if finance is not None and customer_id:
        try:
            snapshot = await finance.get_financial_snapshot(customer_id)
            return budget.estimate_limit(snapshot)
        except Exception as exc:
            log.warning(kv("nessie_unavailable_using_fallback", error=type(exc).__name__))
    else:
        log.info(kv("nessie_offline_using_fallback", seeded=bool(customer_id)))
    return Decimal(persona["expected_limit"])


async def link_customer(
    db: AsyncSession,
    user_id: uuid.UUID,
    bank_code: str,
    finance: FinanceProvider | None,
) -> Decimal | None:
    """Link a (sandbox) bank by code and store the estimated limit. None if unknown code."""
    persona = _personas_by_code().get(bank_code.strip().upper())
    if persona is None:
        return None
    limit = await _estimate(persona, finance)
    profile = await _profile(db, user_id)
    if profile is None:
        profile = PrivateProfileRow(user_id=user_id)
        db.add(profile)
    profile.nessie_customer_id = persona.get("nessie_customer_id")
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


async def set_origin(
    db: AsyncSession,
    user_id: uuid.UUID,
    origin: LatLng,
    label: str,
    typed: bool = False,
    place_id: str | None = None,
) -> None:
    """Stored per person (their own profile row). `typed`: they typed this place (it
    then wins over live location for the plan it was typed in); False when it came from
    location sharing. `place_id`: Google's id for a typed place."""
    profile = await _profile(db, user_id)
    if profile is None:
        return
    profile.origin_lat, profile.origin_lng, profile.origin_label = origin.lat, origin.lng, label
    profile.origin_place_id = place_id
    profile.origin_typed_at = _now() if typed else None
    profile.updated_at = _now()
    await db.flush()


async def refresh_shared_origins(
    db: AsyncSession,
    handles: dict[uuid.UUID, str],
    messaging: MessagingProvider,
    keep_typed_since: datetime | None = None,
) -> int:
    """Replace each member's origin with where they are now, for everyone sharing their
    location with the bot (Find My). Members who don't share keep their stored origin,
    and so does anyone who typed a place since `keep_typed_since` (this plan's start).
    Lookups run in parallel and never raise. Returns how many were refreshed."""
    typed_recently = set()
    if keep_typed_since is not None:
        since = _as_utc(keep_typed_since)
        for user_id in handles:
            profile = await _profile(db, user_id)
            typed_at = profile.origin_typed_at if profile else None
            if typed_at is not None and _as_utc(typed_at) >= since:
                typed_recently.add(user_id)
    user_ids = [u for u in handles if u not in typed_recently]
    results = await asyncio.gather(
        *(messaging.shared_location(handles[u]) for u in user_ids), return_exceptions=True
    )
    refreshed = 0
    for user_id, coords in zip(user_ids, results, strict=True):
        if isinstance(coords, LatLng):
            await set_origin(db, user_id, coords, copy.LIVE_LOCATION_LABEL)
            refreshed += 1
    log.info(
        kv(
            "shared_origins_refreshed",
            refreshed=refreshed,
            members=len(handles),
            kept_typed=len(typed_recently),
        )
    )
    return refreshed


async def clear_origin(db: AsyncSession, user_id: uuid.UUID) -> None:
    profile = await _profile(db, user_id)
    if profile is None:
        return
    profile.origin_lat = profile.origin_lng = profile.origin_label = None
    profile.origin_place_id = None
    profile.origin_typed_at = None
    await db.flush()


async def confirm_origin(
    db: AsyncSession, user_id: uuid.UUID
) -> Literal["missing", "needs_modes", "complete"]:
    """Confirm the stored (geocoded) origin. Reports what onboarding should do next."""
    profile = await _profile(db, user_id)
    if profile is None or profile.origin_lat is None:
        return "missing"
    profile.updated_at = _now()
    await db.flush()
    return "complete" if profile.modes_json else "needs_modes"


async def set_modes(db: AsyncSession, user_id: uuid.UUID, modes: TravelModes) -> None:
    profile = await _profile(db, user_id)
    if profile is None:
        return
    profile.modes_json = modes.model_dump_json()
    profile.updated_at = _now()
    await db.flush()


async def clear_modes(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Travel modes are per plan: forget the last answer before asking again."""
    profile = await _profile(db, user_id)
    if profile is None:
        return
    profile.modes_json = None
    await db.flush()


async def set_drive(db: AsyncSession, user_id: uuid.UUID, drive: bool) -> bool:
    """Turn own-car driving on or off. False if modes were never set."""
    profile = await _profile(db, user_id)
    if profile is None or profile.modes_json is None:
        return False
    modes = TravelModes.model_validate_json(profile.modes_json)
    modes.drive = drive
    profile.modes_json = modes.model_dump_json()
    profile.updated_at = _now()
    await db.flush()
    return True


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


@dataclass(frozen=True)
class ProfileSecrets:
    """The values the PrivacyGuard must keep out of anyone else's messages."""

    spend_limit_usd: Decimal | None
    origin_label: str | None
    nessie_customer_id: str | None


async def guard_secrets_for(
    db: AsyncSession, user_ids: list[uuid.UUID]
) -> dict[uuid.UUID, ProfileSecrets]:
    """For PrivacyGuard only: these values are matched against, never sent."""
    rows = await db.scalars(
        select(PrivateProfileRow).where(PrivateProfileRow.user_id.in_(user_ids))
    )
    return {
        row.user_id: ProfileSecrets(
            spend_limit_usd=row.spend_limit_usd,
            origin_label=row.origin_label,
            nessie_customer_id=row.nessie_customer_id,
        )
        for row in rows
    }
