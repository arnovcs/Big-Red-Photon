"""Identity models (§6.1)."""

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel


class OnboardingState(StrEnum):
    NEW = "new"
    AWAITING_BANK_CODE = "awaiting_bank_code"
    AWAITING_LIMIT_CONFIRM = "awaiting_limit_confirm"
    AWAITING_LOCATION = "awaiting_location"
    AWAITING_MODES = "awaiting_modes"
    READY = "ready"


class User(BaseModel):
    id: UUID
    handle: str  # phone/email from iMessage; never shown to others
    display_name: str  # first name; asked in onboarding if unknown
    dm_chat_id: str | None  # Photon chat id of the DM with this user
    onboarding_state: OnboardingState


class Group(BaseModel):
    """A virtual group, created by one @plan (v3: DMs only)."""

    id: UUID
    join_code: str  # e.g. "K7QP"; 4 chars, uppercase, no 0/O/1/I
    member_ids: list[UUID]  # creator + users who DMed "join <code>"
    active_session_id: UUID | None
