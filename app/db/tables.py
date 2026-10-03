"""SQLAlchemy tables (§6.8). Created with `create_all()` on startup; no migrations.

`private_profiles` may only be queried from `app/private/`.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Numeric, String, Text, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    handle: Mapped[str] = mapped_column(String, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String, default="")
    dm_chat_id: Mapped[str | None] = mapped_column(String, nullable=True)
    onboarding_state: Mapped[str] = mapped_column(String, default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class GroupRow(Base):
    __tablename__ = "groups"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    join_code: Mapped[str] = mapped_column(String, unique=True, index=True)
    active_session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class GroupMemberRow(Base):
    __tablename__ = "group_members"

    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("groups.id"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)


class PrivateProfileRow(Base):
    """ONLY `app/private/` may query this table."""

    __tablename__ = "private_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    nessie_customer_id: Mapped[str | None] = mapped_column(String, nullable=True)
    spend_limit_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    limit_source: Mapped[str] = mapped_column(String)
    # Origin and modes are filled in during onboarding, after the limit.
    origin_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    origin_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    origin_label: Mapped[str | None] = mapped_column(String, nullable=True)
    # When the person last TYPED their origin (None = it came from location sharing).
    # A typed origin wins over live location for the plan it was typed in.
    origin_typed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    origin_place_id: Mapped[str | None] = mapped_column(String, nullable=True)  # Google's id
    modes_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SessionRow(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("groups.id"), index=True)
    state: Mapped[str] = mapped_column(String)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    target_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pid_map_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # pid -> user_id
    preferences_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    plans_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # full plans
    poll_id: Mapped[str | None] = mapped_column(String, nullable=True)
    winner_plan_id: Mapped[str | None] = mapped_column(String, nullable=True)


class SessionMessageRow(Base):
    """Only messages during COLLECTING. Deleted when the session reaches DONE or CANCELLED."""

    __tablename__ = "session_messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"), index=True)
    message_id: Mapped[str] = mapped_column(String, unique=True)
    sender_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    text: Mapped[str] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class VoteRow(Base):
    """One row per user per session (upsert)."""

    __tablename__ = "votes"

    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    option_label: Mapped[str] = mapped_column(String(1))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class LateRow(Base):
    """ "running 10 min late" after a plan was sent: their latest delay (None = unsaid)."""

    __tablename__ = "late_updates"

    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    minutes: Mapped[int | None] = mapped_column(nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SessionReadyRow(Base):
    """Who has said "go" in this planning session. The search starts at > half."""

    __tablename__ = "session_ready"

    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ProcessedMessageRow(Base):
    """Idempotency for webhook retries."""

    __tablename__ = "processed_messages"

    message_id: Mapped[str] = mapped_column(String, primary_key=True)


class PendingSignupRow(Base):
    """A web signup waiting for "start <token>" from the same phone (app/web, app/onboarding).

    Holds no budget, balance, or location: those are written to the vault at claim time.
    """

    __tablename__ = "pending_signups"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(8), unique=True, index=True)
    first_name: Mapped[str] = mapped_column(String)
    phone: Mapped[str] = mapped_column(String, index=True)  # canonical form, e.g. +16075551234
    bank_code: Mapped[str] = mapped_column(String)  # demo persona code, e.g. MAYA1
    # The bot line Photon assigned this person (their "TEXTS ON" number); None until known.
    bot_phone: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    claimed: Mapped[bool] = mapped_column(Boolean, default=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
