"""Inbound dispatch (§7.1): idempotency, user/group upsert, group vs DM routing.

Messages in one chat are handled in order under a per-chat asyncio.Lock.
"""

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable

from app.conversation import copy
from app.db import queries
from app.db.session import session_factory
from app.db.tables import ProcessedMessageRow
from app.deps import Deps
from app.logging import get_logger, kv
from app.messaging.outbound import send_group
from app.models.conversation import InboundMessage
from app.models.outbound import GroupSafeMessage
from app.onboarding.fsm import Onboarding
from app.planning.session import PlanningSessions

log = get_logger(__name__)


class Router:
    def __init__(self, deps: Deps) -> None:
        self.deps = deps
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.onboarding = Onboarding(deps)
        self.sessions = PlanningSessions(deps, self.run_locked)

    async def run_locked(self, chat_id: str, fn: Callable[[], Awaitable[None]]) -> None:
        async with self._locks[chat_id]:
            await fn()

    async def handle_message(self, msg: InboundMessage) -> None:
        """Never raises: a failure is logged so the bot keeps running."""
        try:
            await self.run_locked(msg.chat_id, lambda: self._handle_message(msg))
        except Exception:
            log.exception(kv("handle_message_failed", is_group=msg.is_group))

    async def handle_vote(self, chat_id: str, voter_handle: str, label: str) -> None:
        try:
            await self.run_locked(chat_id, lambda: self._handle_vote(chat_id, voter_handle, label))
        except Exception:
            log.exception(kv("handle_vote_failed"))

    async def _handle_message(self, msg: InboundMessage) -> None:
        async with session_factory()() as db:
            if await db.get(ProcessedMessageRow, msg.message_id) is not None:
                return
            db.add(ProcessedMessageRow(message_id=msg.message_id))
            user, _ = await queries.upsert_user(db, msg.sender_handle, msg.sender_name)

            if msg.is_group:
                group, is_new = await queries.upsert_group(db, msg.chat_id)
                await queries.ensure_member(db, group.id, user.id)
                await db.commit()
                if is_new:
                    await send_group(
                        self.deps.messaging, group.chat_id, GroupSafeMessage(text=copy.GROUP_INTRO)
                    )
                await self.sessions.on_group_message(db, group, user, msg)
            else:
                first_dm = user.dm_chat_id is None
                user.dm_chat_id = msg.chat_id
                await self.onboarding.handle_dm(db, user, msg, first_dm)
            await db.commit()

    async def _handle_vote(self, chat_id: str, voter_handle: str, label: str) -> None:
        async with session_factory()() as db:
            group = await queries.group_by_chat(db, chat_id)
            user = await queries.user_by_handle(db, voter_handle)
            if group is None or user is None:
                return
            await self.sessions.on_vote(db, group, user, label)
            await db.commit()

    def shutdown(self) -> None:
        self.sessions.shutdown()
        self._locks.clear()
