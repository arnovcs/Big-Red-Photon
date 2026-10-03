"""Inbound dispatch (§7.1), v3: every message is a DM.

Handling is serialized with one global asyncio.Lock: members of one virtual group write
from different DMs, and traffic is tiny. Long work (pipeline, delivery) runs as tracked
background tasks outside the lock.
"""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from app.conversation import copy
from app.conversation.commands import SessionCommand, parse_session_command
from app.db import queries
from app.db.session import session_factory
from app.db.tables import ProcessedMessageRow
from app.deps import Deps
from app.logging import get_logger, kv
from app.messaging.outbound import send_private
from app.models.conversation import InboundMessage
from app.models.identity import OnboardingState
from app.models.outbound import PrivateMessage
from app.onboarding.fsm import Onboarding
from app.planning.session import PlanningSessions

log = get_logger(__name__)

PLAN_QUESTION_STATES = {OnboardingState.AWAITING_MODES, OnboardingState.AWAITING_LOCATION}


class Router:
    def __init__(self, deps: Deps) -> None:
        self.deps = deps
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self.onboarding = Onboarding(deps)
        self.sessions = PlanningSessions(deps, self.run_locked, self.spawn)

    async def run_locked(self, fn: Callable[[], Awaitable[None]]) -> None:
        async with self._lock:
            await fn()

    def spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        """Run long work in the background; failures are logged, never lost."""

        async def guarded() -> None:
            try:
                await coro
            except Exception:
                log.exception(kv("background_task_failed"))

        task = asyncio.create_task(guarded())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for background work (pipeline, delivery), not poll timers. Simulator/tests."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def handle_message(self, msg: InboundMessage) -> None:
        """Never raises: a failure is logged so the bot keeps running."""
        try:
            await self.run_locked(lambda: self._handle_message(msg))
        except Exception:
            log.exception(kv("handle_message_failed"))

    async def _handle_message(self, msg: InboundMessage) -> None:
        async with session_factory()() as db:
            if await db.get(ProcessedMessageRow, msg.message_id) is not None:
                return
            db.add(ProcessedMessageRow(message_id=msg.message_id))
            if msg.is_group:
                log.info(kv("group_message_ignored"))  # v3: DMs only
                await db.commit()
                return

            user = await queries.upsert_user(db, msg.sender_handle, msg.sender_name)
            first_dm = user.dm_chat_id is None
            user.dm_chat_id = msg.chat_id

            parsed = parse_session_command(msg.text)
            if (
                parsed
                and user.onboarding_state in PLAN_QUESTION_STATES
                and await queries.active_group_for_user(db, user.id) is not None
            ):
                # Mid-question in a plan ("how are you getting there?", "where from?") but
                # sent @go / @cancel: plan commands still work (@go waits for answers).
                await self.sessions.handle_command(db, user, msg)
            elif user.onboarding_state != OnboardingState.READY:
                if parsed and parsed.command == SessionCommand.JOIN and parsed.arg:
                    await self.onboarding.join_before_ready(user, parsed.arg, first_dm)
                else:
                    await self.onboarding.handle_dm(db, user, msg.text, first_dm)
            elif await self.sessions.handle_command(db, user, msg):
                pass
            elif await self.onboarding.handle_settings(db, user, msg.text):
                pass
            elif await self.sessions.store_message(db, user, msg):
                pass
            else:
                await send_private(self.deps.messaging, user.handle, PrivateMessage(text=copy.HELP))
            await db.commit()

    def shutdown(self) -> None:
        self.sessions.shutdown()
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
