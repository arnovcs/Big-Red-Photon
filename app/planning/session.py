"""Group planning session state machine (§7.3)."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from enum import StrEnum

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversation import copy
from app.conversation.commands import GroupCommand, parse_group
from app.db import queries
from app.db.session import session_factory
from app.db.tables import GroupRow, SessionMessageRow, SessionRow, UserRow
from app.decision import poll
from app.delivery import itinerary
from app.deps import Deps
from app.logging import get_logger, kv
from app.messaging.outbound import send_group
from app.models.conversation import InboundMessage
from app.models.outbound import GroupSafeMessage
from app.planning import pipeline

log = get_logger(__name__)

RunLocked = Callable[[str, Callable[[], Awaitable[None]]], Awaitable[None]]


class SessionState(StrEnum):
    COLLECTING = "collecting"
    RUNNING = "running"
    POLLING = "polling"
    CONFIRMED = "confirmed"
    DELIVERING = "delivering"
    DONE = "done"
    CANCELLED = "cancelled"


class PlanningSessions:
    """Handles group commands. Callers hold the chat's lock (see Router)."""

    def __init__(self, deps: Deps, run_locked: RunLocked) -> None:
        self.deps = deps
        self._run_locked = run_locked
        self._poll_timers: dict[uuid.UUID, asyncio.Task[None]] = {}

    async def _say(self, group: GroupRow, text: str) -> None:
        await send_group(self.deps.messaging, group.chat_id, GroupSafeMessage(text=text))

    # --- entry points ---------------------------------------------------------

    async def on_group_message(
        self, db: AsyncSession, group: GroupRow, user: UserRow, msg: InboundMessage
    ) -> None:
        parsed = parse_group(msg.text)
        session = await queries.active_session(db, group)
        state = SessionState(session.state) if session else None

        if parsed and parsed.command == GroupCommand.PLAN:
            await self._plan(db, group, session)
        elif parsed and parsed.command == GroupCommand.CANCEL:
            await self._cancel(db, group, session)
        elif parsed and parsed.command == GroupCommand.GO:
            await self._go(db, group, session)
        elif parsed and parsed.command == GroupCommand.PICK and state == SessionState.POLLING:
            assert session is not None and parsed.option is not None
            await self._pick(db, group, session, parsed.option)
        elif parsed and parsed.command == GroupCommand.VOTE and state == SessionState.POLLING:
            assert session is not None and parsed.option is not None
            await self._vote(db, group, session, user, parsed.option)
        elif session is not None and state == SessionState.COLLECTING:
            db.add(
                SessionMessageRow(
                    session_id=session.id,
                    message_id=msg.message_id,
                    sender_user_id=user.id,
                    text=msg.text,
                    ts=msg.ts,
                )
            )

    async def on_vote(self, db: AsyncSession, group: GroupRow, user: UserRow, label: str) -> None:
        session = await queries.active_session(db, group)
        if session is not None and session.state == SessionState.POLLING:
            await self._vote(db, group, session, user, label)

    # --- transitions ----------------------------------------------------------

    async def _plan(self, db: AsyncSession, group: GroupRow, session: SessionRow | None) -> None:
        if session is not None:
            await self._say(group, copy.ALREADY_PLANNING)
            return
        members = await queries.group_members(db, group.id)
        ready = await pipeline.ready_members(db, group)
        if len(ready) < 2:
            await self._say(group, copy.NEED_MORE_PEOPLE)
            return
        session = SessionRow(group_id=group.id, state=SessionState.COLLECTING)
        db.add(session)
        await db.flush()
        group.active_session_id = session.id
        await db.commit()

        ready_ids = {m.id for m in ready}
        missing = [m.display_name or "Someone" for m in members if m.id not in ready_ids]
        if missing:
            await self._say(
                group, copy.planning_with([m.display_name or "Someone" for m in ready], missing)
            )
        await self._say(group, copy.LISTENING)

    async def _go(self, db: AsyncSession, group: GroupRow, session: SessionRow | None) -> None:
        if session is None:
            await self._say(group, copy.SAY_PLAN_FIRST)
            return
        if session.state != SessionState.COLLECTING:
            return
        session.state = SessionState.RUNNING
        await db.commit()
        await self._say(group, copy.LOOKING)

        try:
            plans = await pipeline.run(self.deps, db, group, session)
        except Exception:
            log.exception(kv("pipeline_failed"))
            await db.rollback()
            await db.refresh(group)  # rollback expires rows; reload before touching them
            await db.refresh(session)
            plans = []
            await self._say(group, copy.PIPELINE_FAILED)

        session.state = SessionState.POLLING if plans else SessionState.COLLECTING
        await db.commit()
        if plans:
            self._start_poll_timer(group.chat_id, session.id)

    async def _cancel(self, db: AsyncSession, group: GroupRow, session: SessionRow | None) -> None:
        if session is None:
            return
        self._stop_poll_timer(session.id)
        session.state = SessionState.CANCELLED
        await self._close(db, group, session)
        await self._say(group, copy.CANCELLED)

    async def _vote(
        self, db: AsyncSession, group: GroupRow, session: SessionRow, user: UserRow, label: str
    ) -> None:
        pid_map = pipeline.load_pid_map(session)
        plans = pipeline.load_plans(session)
        if user.id not in pid_map.values() or label not in poll.LABELS[: len(plans)]:
            return
        await poll.record_vote(db, session.id, user.id, label)
        winner = poll.decided_winner(await poll.tally(db, session.id), len(pid_map))
        if winner is not None:
            await self._finish(db, group, session, winner)

    async def _pick(
        self, db: AsyncSession, group: GroupRow, session: SessionRow, label: str
    ) -> None:
        if label in poll.LABELS[: len(pipeline.load_plans(session))]:
            await self._finish(db, group, session, label)

    async def _finish(
        self, db: AsyncSession, group: GroupRow, session: SessionRow, label: str
    ) -> None:
        self._stop_poll_timer(session.id)
        plans = pipeline.load_plans(session)
        plan = plans[poll.LABELS.index(label)]
        session.state = SessionState.CONFIRMED
        session.winner_plan_id = plan.plan_id
        session.target_time = plan.target_arrival
        await db.commit()

        session.state = SessionState.DELIVERING
        await db.commit()
        try:
            await itinerary.deliver(self.deps, db, group, session, plan, label)
        except Exception:
            log.exception(kv("delivery_failed"))
        session.state = SessionState.DONE
        await self._close(db, group, session)

    async def _close(self, db: AsyncSession, group: GroupRow, session: SessionRow) -> None:
        """End the session: drop collected messages (§11 retention) and free the group."""
        await db.execute(
            delete(SessionMessageRow).where(SessionMessageRow.session_id == session.id)
        )
        group.active_session_id = None
        await db.commit()

    # --- poll timeout ---------------------------------------------------------

    def _start_poll_timer(self, chat_id: str, session_id: uuid.UUID) -> None:
        self._stop_poll_timer(session_id)
        self._poll_timers[session_id] = asyncio.create_task(self._poll_timeout(chat_id, session_id))

    def _stop_poll_timer(self, session_id: uuid.UUID) -> None:
        task = self._poll_timers.pop(session_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _poll_timeout(self, chat_id: str, session_id: uuid.UUID) -> None:
        await asyncio.sleep(self.deps.settings.poll_timeout_sec)
        await self._run_locked(chat_id, lambda: self._on_poll_timeout(chat_id, session_id))

    async def _on_poll_timeout(self, chat_id: str, session_id: uuid.UUID) -> None:
        async with session_factory()() as db:
            group = await queries.group_by_chat(db, chat_id)
            session = await db.get(SessionRow, session_id)
            if group is None or session is None or session.state != SessionState.POLLING:
                return
            counts = await poll.tally(db, session_id)
            label = poll.leader(counts, len(pipeline.load_plans(session)))
            await self._finish(db, group, session, label)

    def shutdown(self) -> None:
        for task in self._poll_timers.values():
            task.cancel()
        self._poll_timers.clear()
