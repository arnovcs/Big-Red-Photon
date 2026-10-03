"""Planning session state machine (§7.3), v3: a virtual group of DMs joined by code.

Command handlers run under the router's global lock. The pipeline and delivery run as
their own tasks outside the lock, then re-take it to apply their results, re-checking
the session state first (someone may have said @cancel in the meantime).
"""

import asyncio
import secrets
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from datetime import timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversation import copy
from app.conversation.commands import (
    GO_IN_SENTENCE,
    SessionCommand,
    parse_mode_change,
    parse_session_command,
)
from app.db import queries
from app.db.session import session_factory
from app.db.tables import GroupRow, SessionMessageRow, SessionRow, UserRow
from app.decision import poll
from app.delivery import itinerary
from app.deps import Deps
from app.logging import get_logger, kv
from app.messaging.guard import PrivacyGuard
from app.messaging.outbound import send_group, send_private
from app.models.conversation import InboundMessage
from app.models.identity import OnboardingState
from app.models.outbound import GroupSafeMessage, PrivateMessage
from app.models.plans import Plan
from app.onboarding.fsm import Onboarding
from app.planning import pipeline
from app.planning.pipeline import PlanningResult

log = get_logger(__name__)

JOIN_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I
JOIN_CODE_LENGTH = 4
MAX_GROUP_SIZE = 6
MIN_GROUP_SIZE = 2
# Plan questions ("how are you getting there?", "where from?") still unanswered.
QUESTION_STATES = {OnboardingState.AWAITING_MODES, OnboardingState.AWAITING_LOCATION}

RunLocked = Callable[[Callable[[], Awaitable[None]]], Awaitable[None]]
Spawn = Callable[[Coroutine[Any, Any, None]], None]


class SessionState(StrEnum):
    COLLECTING = "collecting"
    RUNNING = "running"
    POLLING = "polling"
    CONFIRMED = "confirmed"
    DELIVERING = "delivering"
    DONE = "done"
    CANCELLED = "cancelled"


class PlanningSessions:
    def __init__(self, deps: Deps, run_locked: RunLocked, spawn: Spawn) -> None:
        self.deps = deps
        self._run_locked = run_locked
        self._spawn = spawn
        self._poll_timers: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._nudges: set[asyncio.Task[None]] = set()

    # --- messaging helpers ----------------------------------------------------

    async def _reply_handle(self, handle: str, text: str) -> None:
        await send_private(self.deps.messaging, handle, PrivateMessage(text=text))

    async def _reply(self, user: UserRow, text: str) -> None:
        await send_private(self.deps.messaging, user.handle, PrivateMessage(text=text))

    async def _tell_group(
        self,
        db: AsyncSession,
        group_id: uuid.UUID,
        session_id: uuid.UUID,
        msg: GroupSafeMessage | str,
        guard: PrivacyGuard | None = None,
    ) -> None:
        """Same group-safe message to every member's DM, through the PrivacyGuard.

        Pass `guard` when the session's DMs are about to be deleted (cancel/close).
        """
        if isinstance(msg, str):
            msg = GroupSafeMessage(text=msg)
        if guard is None:
            guard = await pipeline.build_guard(db, group_id, session_id)
        await send_group(self.deps.messaging, await _handles(db, group_id), msg, guard)

    # --- entry points (called under the router lock, READY users only) --------

    async def handle_command(self, db: AsyncSession, user: UserRow, msg: InboundMessage) -> bool:
        """Session commands. Returns False if the text isn't one (so the router moves on)."""
        parsed = parse_session_command(msg.text)
        if parsed is None:
            return False
        group = await queries.active_group_for_user(db, user.id)
        session = await queries.active_session(db, group) if group else None

        if parsed.command == SessionCommand.PLAN:
            await self._plan(db, user, group)
            return True
        if parsed.command == SessionCommand.JOIN:
            assert parsed.arg is not None
            await self._join(db, user, group, parsed.arg)
            return True
        if group is None or session is None:
            if parsed.command == SessionCommand.VOTE:
                return False  # a bare "A" outside a plan is just text
            await self._reply(user, copy.NOT_IN_PLAN)
            return True

        state = SessionState(session.state)
        if parsed.command == SessionCommand.CANCEL:
            await self._cancel(db, group, session)
        elif parsed.command == SessionCommand.GO:
            if parsed.arg == GO_IN_SENTENCE and state == SessionState.COLLECTING:
                # "ok that's all, somewhere cheap pls, show us": keep what they said as a
                # preference too, then go.
                await self.store_message(db, user, msg, quiet=True)
            await self._go(db, user, group, session)
        elif parsed.command == SessionCommand.PICK:
            if state == SessionState.POLLING:
                assert parsed.arg is not None
                await self._pick(db, group, session, parsed.arg)
        elif parsed.command == SessionCommand.VOTE:
            if state != SessionState.POLLING:
                return False
            assert parsed.arg is not None
            await self._vote(db, group, session, user, parsed.arg)
        return True

    async def store_message(
        self, db: AsyncSession, user: UserRow, msg: InboundMessage, quiet: bool = False
    ) -> bool:
        """Keep a preference DM while COLLECTING. Nothing is relayed to other members."""
        group = await queries.active_group_for_user(db, user.id)
        session = await queries.active_session(db, group) if group else None
        if session is None or session.state != SessionState.COLLECTING:
            return False
        mode_change = parse_mode_change(msg.text)
        if mode_change is not None:
            # "actually I'll drive": their mode for this plan changes now. The message is
            # still kept below (it may say more, e.g. "I'm driving, let's get tacos").
            await Onboarding(self.deps).change_trip_modes(db, user, mode_change)
        earlier = await db.scalar(
            select(func.count())
            .select_from(SessionMessageRow)
            .where(
                SessionMessageRow.session_id == session.id,
                SessionMessageRow.sender_user_id == user.id,
            )
        )
        db.add(
            SessionMessageRow(
                session_id=session.id,
                message_id=msg.message_id,
                sender_user_id=user.id,
                text=msg.text,
                ts=msg.ts,
            )
        )
        # Only their first message in a plan gets a 👍 ("heard you"), like a friend would;
        # after that, quiet. No tapback when a text reply already went out (mode change).
        # If tapbacks don't work on this line, a short "got it" instead.
        if not earlier and mode_change is None and not quiet:
            if not await self.deps.messaging.react(user.handle, msg.message_id, copy.REACT_OK):
                await self._reply(user, copy.NOTED)
        return True

    # --- transitions ----------------------------------------------------------

    async def _plan(self, db: AsyncSession, user: UserRow, current: GroupRow | None) -> None:
        if current is not None:
            await self._reply(user, copy.already_in_plan(current.join_code))
            return
        group = await queries.create_group(db, await _new_join_code(db))
        await queries.add_member(db, group.id, user.id)
        session = SessionRow(group_id=group.id, state=SessionState.COLLECTING)
        db.add(session)
        await db.flush()
        group.active_session_id = session.id
        await db.commit()
        await self._reply(user, copy.plan_started(group.join_code))
        await Onboarding(self.deps).ask_trip_modes(db, user)
        await db.commit()

    async def _join(
        self, db: AsyncSession, user: UserRow, current: GroupRow | None, code: str
    ) -> None:
        if current is not None:
            await self._reply(user, copy.already_in_plan(current.join_code))
            return
        group = await queries.group_by_code(db, code)
        session = await queries.active_session(db, group) if group else None
        if group is None or session is None:
            await self._reply(user, copy.UNKNOWN_CODE)
            return
        if session.state != SessionState.COLLECTING:
            await self._reply(user, copy.PLAN_ALREADY_STARTED)
            return
        count = await queries.member_count(db, group.id)
        if count >= MAX_GROUP_SIZE:
            await self._reply(user, copy.plan_full(MAX_GROUP_SIZE))
            return
        await queries.add_member(db, group.id, user.id)
        await db.commit()
        await self._tell_group(
            db, group.id, session.id, copy.joined(user.display_name or "Someone", count + 1)
        )
        await self._reply(user, copy.YOU_JOINED)
        await Onboarding(self.deps).ask_trip_modes(db, user)
        await db.commit()

    async def _go(
        self, db: AsyncSession, user: UserRow, group: GroupRow, session: SessionRow
    ) -> None:
        if session.state != SessionState.COLLECTING:
            return
        if await queries.member_count(db, group.id) < MIN_GROUP_SIZE:
            await self._reply(user, copy.need_two(group.join_code))
            return
        members = await queries.group_members(db, group.id)
        for m in members:
            if m.onboarding_state == OnboardingState.AWAITING_MODES:
                # Never said how they're getting there: walking, and they're told so.
                await Onboarding(self.deps).default_to_walking(db, m)
        waiting = [m for m in members if m.onboarding_state in QUESTION_STATES]
        if waiting:
            await self._reply(user, copy.waiting_on([m.display_name or "someone" for m in waiting]))
            for m in waiting:
                still = m.onboarding_state == OnboardingState.AWAITING_MODES
                await self._reply(m, copy.ASK_TRIP_MODES if still else copy.ASK_TRIP_LOCATION)
            return
        session.state = SessionState.RUNNING
        await db.commit()
        # Typing dots while planning ("…"), the way a friend would look things up.
        for handle in await _handles(db, group.id):
            await self.deps.messaging.typing(handle, True)
        self._spawn(self._run_pipeline(group.id, session.id))

    async def _run_pipeline(self, group_id: uuid.UUID, session_id: uuid.UUID) -> None:
        result: PlanningResult | None
        try:
            result = await pipeline.compute(self.deps, group_id, session_id)
        except Exception:
            log.exception(kv("pipeline_failed"))
            result = None
        await self._run_locked(lambda: self._apply_pipeline(group_id, session_id, result))

    async def _apply_pipeline(
        self, group_id: uuid.UUID, session_id: uuid.UUID, result: PlanningResult | None
    ) -> None:
        async with session_factory()() as db:
            for handle in await _handles(db, group_id):
                await self.deps.messaging.typing(handle, False)
            session = await db.get(SessionRow, session_id)
            if session is None or session.state != SessionState.RUNNING:
                return  # cancelled while the pipeline ran

            if result is not None and result.missing:
                users = await queries.users_by_ids(db, result.missing)
                for member in users.values():
                    member.onboarding_state = OnboardingState.AWAITING_LOCATION
                    await self._reply(member, copy.ASK_TRIP_LOCATION)
                session.state = SessionState.COLLECTING
                await db.commit()
                names = [m.display_name or "someone" for m in users.values()]
                await self._tell_group(db, group_id, session_id, copy.waiting_on(names))
                return
            if result is None:
                notice = copy.PIPELINE_FAILED
            elif len(result.pid_map) < MIN_GROUP_SIZE:
                notice = copy.NEED_MORE_PEOPLE
            elif not result.plans:
                pipeline.save_result(session, result)
                notice = copy.NO_PLACES if result.no_places else copy.nothing_fits(result.hint)
            else:
                pipeline.save_result(session, result)
                session.state = SessionState.POLLING
                await db.commit()
                guard = await pipeline.build_guard(
                    db, group_id, session_id, pipeline.venue_terms(result.plans)
                )
                poll_message = poll.build_poll_message(result.plans, result.facts, result.blurbs)
                await self._tell_group(db, group_id, session_id, poll_message, guard)
                self._start_poll_timer(group_id, session_id)
                return

            session.state = SessionState.COLLECTING
            await db.commit()
            await self._tell_group(db, group_id, session_id, notice)

    async def _cancel(self, db: AsyncSession, group: GroupRow, session: SessionRow) -> None:
        self._stop_poll_timer(group.id)
        guard = await pipeline.build_guard(db, group.id, session.id)  # before DMs are deleted
        session.state = SessionState.CANCELLED
        await self._close(db, group, session)
        await self._tell_group(db, group.id, session.id, copy.CANCELLED, guard)

    async def _vote(
        self, db: AsyncSession, group: GroupRow, session: SessionRow, user: UserRow, label: str
    ) -> None:
        """Latest vote per user wins. Only members included in the plan can vote."""
        pid_map = pipeline.load_pid_map(session)
        n_options = len(pipeline.load_plans(session))
        if user.id not in pid_map.values() or label not in poll.LABELS[:n_options]:
            return
        await poll.record_vote(db, session.id, user.id, label)
        counts = await poll.tally(db, session.id)
        winner = poll.decided_winner(counts, len(pid_map))
        if winner is not None:
            await self._finish(db, group, session, winner)
        else:
            await db.commit()
            await self._tell_group(
                db, group.id, session.id, copy.votes_progress(sum(counts.values()), len(pid_map))
            )

    async def _pick(
        self, db: AsyncSession, group: GroupRow, session: SessionRow, label: str
    ) -> None:
        if label in poll.LABELS[: len(pipeline.load_plans(session))]:
            await self._finish(db, group, session, label)

    async def _finish(
        self, db: AsyncSession, group: GroupRow, session: SessionRow, label: str
    ) -> None:
        self._stop_poll_timer(group.id)
        plan = pipeline.load_plans(session)[poll.LABELS.index(label)]
        session.state = SessionState.CONFIRMED
        session.winner_plan_id = plan.plan_id
        session.target_time = plan.target_arrival
        await db.commit()
        session.state = SessionState.DELIVERING
        await db.commit()
        self._spawn(self._deliver(group.id, session.id, plan, label))

    async def _deliver(
        self, group_id: uuid.UUID, session_id: uuid.UUID, plan: Plan, label: str
    ) -> None:
        try:
            personal = await itinerary.prepare(self.deps, session_id, plan)
        except Exception:
            log.exception(kv("delivery_prepare_failed"))
            personal = []
        await self._run_locked(
            lambda: self._apply_delivery(group_id, session_id, plan, label, personal)
        )

    async def _apply_delivery(
        self,
        group_id: uuid.UUID,
        session_id: uuid.UUID,
        plan: Plan,
        label: str,
        personal: list[itinerary.Itinerary],
    ) -> None:
        async with session_factory()() as db:
            group = await db.get(GroupRow, group_id)
            session = await db.get(SessionRow, session_id)
            if group is None or session is None or session.state != SessionState.DELIVERING:
                return
            guard = await pipeline.build_guard(
                db, group_id, session_id, pipeline.venue_terms([plan])
            )
            confirmation = itinerary.confirmation_message(self.deps, plan, label)
            handles = await _handles(db, group_id)
            await itinerary.send(self.deps, handles, confirmation, personal, guard)
            session.state = SessionState.DONE
            await self._close(db, group, session)
            self._schedule_leave_nudges(personal)

    def _schedule_leave_nudges(self, personal: list[itinerary.Itinerary]) -> None:
        """ "heads up, leave in 5" to each person, at their own leave time."""
        if self.deps.settings.leave_nudge_min <= 0:
            return
        lead = timedelta(minutes=self.deps.settings.leave_nudge_min)
        now = self.deps.clock()
        for item in personal:
            if item.leave_by is None:
                continue
            delay = (item.leave_by - lead - now).total_seconds()
            if delay <= 0:
                continue  # leaving too soon for a nudge to help
            task = asyncio.create_task(self._leave_nudge(item, delay))
            self._nudges.add(task)
            task.add_done_callback(self._nudges.discard)

    async def _leave_nudge(self, item: itinerary.Itinerary, delay: float) -> None:
        await asyncio.sleep(delay)
        text = copy.leave_nudge(item.mode, self.deps.settings.leave_nudge_min)
        try:
            await self._reply_handle(item.handle, text)
        except Exception:
            log.warning(kv("leave_nudge_failed"))

    async def _close(self, db: AsyncSession, group: GroupRow, session: SessionRow) -> None:
        """End the session: drop collected DMs (§11 retention) and retire the join code."""
        await db.execute(
            delete(SessionMessageRow).where(SessionMessageRow.session_id == session.id)
        )
        for member in await queries.group_members(db, group.id):
            if member.onboarding_state in QUESTION_STATES:
                member.onboarding_state = OnboardingState.READY  # don't strand them
        group.active_session_id = None
        await db.commit()

    # --- poll timeout ---------------------------------------------------------

    def _start_poll_timer(self, group_id: uuid.UUID, session_id: uuid.UUID) -> None:
        self._stop_poll_timer(group_id)
        self._poll_timers[group_id] = asyncio.create_task(self._poll_timeout(group_id, session_id))

    def _stop_poll_timer(self, group_id: uuid.UUID) -> None:
        task = self._poll_timers.pop(group_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _poll_timeout(self, group_id: uuid.UUID, session_id: uuid.UUID) -> None:
        await asyncio.sleep(self.deps.settings.poll_timeout_sec)
        await self._run_locked(lambda: self._on_poll_timeout(group_id, session_id))

    async def _on_poll_timeout(self, group_id: uuid.UUID, session_id: uuid.UUID) -> None:
        async with session_factory()() as db:
            group = await db.get(GroupRow, group_id)
            session = await db.get(SessionRow, session_id)
            if group is None or session is None or session.state != SessionState.POLLING:
                return
            counts = await poll.tally(db, session_id)
            label = poll.leader(counts, len(pipeline.load_plans(session)))
            await self._finish(db, group, session, label)

    def shutdown(self) -> None:
        for task in self._nudges:
            task.cancel()
        self._nudges.clear()
        for task in self._poll_timers.values():
            task.cancel()
        self._poll_timers.clear()


async def _handles(db: AsyncSession, group_id: uuid.UUID) -> list[str]:
    return [member.handle for member in await queries.group_members(db, group_id)]


async def _new_join_code(db: AsyncSession) -> str:
    """A code no group has used before (codes are retired, never reused)."""
    while True:
        code = "".join(secrets.choice(JOIN_CODE_ALPHABET) for _ in range(JOIN_CODE_LENGTH))
        if await queries.group_by_code(db, code) is None:
            return code
