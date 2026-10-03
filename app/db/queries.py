"""Shared lookups on the non-private tables (users, groups, membership, sessions)."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import GroupMemberRow, GroupRow, PendingSignupRow, SessionRow, UserRow
from app.models.identity import OnboardingState


async def upsert_user(db: AsyncSession, handle: str, name: str | None) -> UserRow:
    user = await db.scalar(select(UserRow).where(UserRow.handle == handle))
    if user is None:
        user = UserRow(handle=handle, display_name="", onboarding_state=OnboardingState.NEW)
        db.add(user)
    if name and not user.display_name:
        user.display_name = name.strip().split()[0][:30]
    await db.flush()
    return user


async def users_by_ids(db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, UserRow]:
    rows = await db.scalars(select(UserRow).where(UserRow.id.in_(ids)))
    return {row.id: row for row in rows}


async def create_group(db: AsyncSession, join_code: str) -> GroupRow:
    group = GroupRow(join_code=join_code)
    db.add(group)
    await db.flush()
    return group


async def group_by_code(db: AsyncSession, join_code: str) -> GroupRow | None:
    return await db.scalar(select(GroupRow).where(GroupRow.join_code == join_code.upper()))


async def active_group_for_user(db: AsyncSession, user_id: uuid.UUID) -> GroupRow | None:
    """The one group with an active session that this user belongs to (§6.1)."""
    return await db.scalar(
        select(GroupRow)
        .join(GroupMemberRow, GroupMemberRow.group_id == GroupRow.id)
        .where(GroupMemberRow.user_id == user_id, GroupRow.active_session_id.is_not(None))
        .order_by(GroupRow.created_at.desc())
        .limit(1)
    )


async def add_member(db: AsyncSession, group_id: uuid.UUID, user_id: uuid.UUID) -> None:
    if await db.get(GroupMemberRow, (group_id, user_id)) is None:
        db.add(GroupMemberRow(group_id=group_id, user_id=user_id))
        await db.flush()


async def group_members(db: AsyncSession, group_id: uuid.UUID) -> list[UserRow]:
    """Members, oldest user first."""
    rows = await db.scalars(
        select(UserRow)
        .join(GroupMemberRow, GroupMemberRow.user_id == UserRow.id)
        .where(GroupMemberRow.group_id == group_id)
        .order_by(UserRow.created_at, UserRow.handle)
    )
    return list(rows)


async def member_count(db: AsyncSession, group_id: uuid.UUID) -> int:
    count = await db.scalar(
        select(func.count()).select_from(GroupMemberRow).where(GroupMemberRow.group_id == group_id)
    )
    return count or 0


async def active_session(db: AsyncSession, group: GroupRow) -> SessionRow | None:
    if group.active_session_id is None:
        return None
    return await db.get(SessionRow, group.active_session_id)


async def user_by_handle(db: AsyncSession, handle: str) -> UserRow | None:
    return await db.scalar(select(UserRow).where(UserRow.handle == handle))


async def signup_by_token(db: AsyncSession, token: str) -> PendingSignupRow | None:
    return await db.scalar(select(PendingSignupRow).where(PendingSignupRow.token == token.upper()))


async def claimed_signup_for(db: AsyncSession, user_id: uuid.UUID) -> PendingSignupRow | None:
    """The web signup this user finished by text, if they came through the website."""
    return await db.scalar(
        select(PendingSignupRow)
        .where(PendingSignupRow.claimed_user_id == user_id)
        .order_by(PendingSignupRow.claimed_at.desc())
        .limit(1)
    )
