"""Shared lookups on the non-private tables (users, groups, membership, sessions)."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import GroupMemberRow, GroupRow, SessionRow, UserRow
from app.models.identity import OnboardingState


async def upsert_user(db: AsyncSession, handle: str, name: str | None) -> tuple[UserRow, bool]:
    user = await db.scalar(select(UserRow).where(UserRow.handle == handle))
    created = user is None
    if user is None:
        user = UserRow(handle=handle, display_name="", onboarding_state=OnboardingState.NEW)
        db.add(user)
    if name and not user.display_name:
        user.display_name = name.strip().split()[0][:30]
    await db.flush()
    return user, created


async def user_by_handle(db: AsyncSession, handle: str) -> UserRow | None:
    return await db.scalar(select(UserRow).where(UserRow.handle == handle))


async def users_by_ids(db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, UserRow]:
    rows = await db.scalars(select(UserRow).where(UserRow.id.in_(ids)))
    return {row.id: row for row in rows}


async def upsert_group(db: AsyncSession, chat_id: str) -> tuple[GroupRow, bool]:
    group = await db.scalar(select(GroupRow).where(GroupRow.chat_id == chat_id))
    if group is not None:
        return group, False
    group = GroupRow(chat_id=chat_id)
    db.add(group)
    await db.flush()
    return group, True


async def group_by_chat(db: AsyncSession, chat_id: str) -> GroupRow | None:
    return await db.scalar(select(GroupRow).where(GroupRow.chat_id == chat_id))


async def ensure_member(db: AsyncSession, group_id: uuid.UUID, user_id: uuid.UUID) -> None:
    if await db.get(GroupMemberRow, (group_id, user_id)) is None:
        db.add(GroupMemberRow(group_id=group_id, user_id=user_id))
        await db.flush()


async def group_members(db: AsyncSession, group_id: uuid.UUID) -> list[UserRow]:
    rows = await db.scalars(
        select(UserRow)
        .join(GroupMemberRow, GroupMemberRow.user_id == UserRow.id)
        .where(GroupMemberRow.group_id == group_id)
        .order_by(UserRow.created_at, UserRow.handle)
    )
    return list(rows)


async def groups_for_user(db: AsyncSession, user_id: uuid.UUID) -> list[GroupRow]:
    rows = await db.scalars(
        select(GroupRow)
        .join(GroupMemberRow, GroupMemberRow.group_id == GroupRow.id)
        .where(GroupMemberRow.user_id == user_id)
    )
    return list(rows)


async def active_session(db: AsyncSession, group: GroupRow) -> SessionRow | None:
    if group.active_session_id is None:
        return None
    return await db.get(SessionRow, group.active_session_id)
