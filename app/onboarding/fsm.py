"""DM onboarding state machine (§7.2) and READY-state DM commands (§7.1)."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.conversation import copy
from app.conversation.commands import (
    is_no,
    is_yes,
    parse_amount,
    parse_dm_command,
    parse_modes,
)
from app.db import queries
from app.db.tables import UserRow
from app.deps import Deps
from app.messaging.outbound import send_group, send_private
from app.models.conversation import InboundMessage
from app.models.identity import OnboardingState
from app.models.outbound import GroupSafeMessage, PrivateMessage
from app.models.private import LatLng
from app.private import vault

MIN_LIMIT_USD = 1
MAX_LIMIT_USD = 1000


class Onboarding:
    def __init__(self, deps: Deps) -> None:
        self.deps = deps

    async def _reply(self, user: UserRow, text: str) -> None:
        await send_private(self.deps.messaging, user.handle, PrivateMessage(text=text))

    async def handle_dm(
        self, db: AsyncSession, user: UserRow, msg: InboundMessage, first_dm: bool
    ) -> None:
        state = OnboardingState(user.onboarding_state)
        text = msg.text.strip()

        if state == OnboardingState.NEW:
            await self._new(user, text, first_dm)
        elif state == OnboardingState.AWAITING_BANK_CODE:
            await self._bank_code(db, user, text)
        elif state == OnboardingState.AWAITING_LIMIT_CONFIRM:
            await self._limit_confirm(db, user, text)
        elif state == OnboardingState.AWAITING_LOCATION:
            await self._location(db, user, text, msg.location)
        elif state == OnboardingState.AWAITING_MODES:
            await self._modes(db, user, text)
        else:
            await self._ready_command(db, user, text)

    async def _new(self, user: UserRow, text: str, first_dm: bool) -> None:
        if not user.display_name:
            if first_dm:
                await self._reply(user, copy.ASK_NAME)
                return
            user.display_name = text.split()[0][:30] if text else ""
            if not user.display_name:
                await self._reply(user, copy.ASK_NAME)
                return
        user.onboarding_state = OnboardingState.AWAITING_BANK_CODE
        await self._reply(user, copy.ask_bank_code(user.display_name))

    async def _bank_code(self, db: AsyncSession, user: UserRow, text: str) -> None:
        limit = await vault.link_customer(db, user.id, text)
        if limit is None:
            await self._reply(user, copy.BANK_CODE_INVALID)
            return
        user.onboarding_state = OnboardingState.AWAITING_LIMIT_CONFIRM
        await self._reply(user, copy.confirm_limit(limit))

    async def _limit_confirm(self, db: AsyncSession, user: UserRow, text: str) -> None:
        if not is_yes(text):
            amount = parse_amount(text)
            if amount is None or not MIN_LIMIT_USD <= amount <= MAX_LIMIT_USD:
                await self._reply(user, copy.LIMIT_INVALID)
                return
            await vault.set_limit(db, user.id, amount, "user_override")
        user.onboarding_state = OnboardingState.AWAITING_LOCATION
        await self._reply(user, copy.ASK_LOCATION)

    async def _location(
        self, db: AsyncSession, user: UserRow, text: str, shared: LatLng | None
    ) -> None:
        if shared is not None:
            await vault.set_origin(db, user.id, shared, copy.SHARED_LOCATION_LABEL)
            await self._location_confirmed(db, user)
            return

        pending = await vault.origin_label(db, user.id)
        if pending and is_yes(text):
            await self._location_confirmed(db, user)
            return
        if pending and is_no(text):
            await vault.clear_origin(db, user.id)
            await self._reply(user, copy.ASK_LOCATION)
            return

        settings = self.deps.settings
        center = LatLng(lat=settings.demo_center_lat, lng=settings.demo_center_lng)
        try:
            found = await self.deps.places.geocode(text, center)
        except Exception:
            found = None
        if found is None:
            await self._reply(user, copy.LOCATION_NOT_FOUND)
            return
        coords, label = found
        await vault.set_origin(db, user.id, coords, label)
        await self._reply(user, copy.confirm_location(label))

    async def _location_confirmed(self, db: AsyncSession, user: UserRow) -> None:
        if await vault.get_modes(db, user.id) is not None:
            # Re-entry from the READY "location" command: modes are already known.
            user.onboarding_state = OnboardingState.READY
            await self._reply(user, copy.UPDATED)
            return
        user.onboarding_state = OnboardingState.AWAITING_MODES
        await self._reply(user, copy.ASK_MODES)

    async def _modes(self, db: AsyncSession, user: UserRow, text: str) -> None:
        modes = parse_modes(text)
        if modes is None:
            await self._reply(user, copy.MODES_INVALID)
            return
        await vault.set_modes(db, user.id, modes)
        user.onboarding_state = OnboardingState.READY
        await db.flush()
        await self._reply(user, copy.YOU_ARE_SET)
        await self._announce_ready(db, user)

    async def _announce_ready(self, db: AsyncSession, user: UserRow) -> None:
        for group in await queries.groups_for_user(db, user.id):
            members = await queries.group_members(db, group.id)
            ready = sum(1 for m in members if m.onboarding_state == OnboardingState.READY)
            text = copy.member_ready(user.display_name or "Someone", ready, len(members))
            await send_group(self.deps.messaging, group.chat_id, GroupSafeMessage(text=text))

    async def _ready_command(self, db: AsyncSession, user: UserRow, text: str) -> None:
        command = parse_dm_command(text)
        if command is None:
            await self._reply(user, copy.HELP)
            return
        if command.name == "budget":
            amount = parse_amount(command.arg)
            if amount is None or not MIN_LIMIT_USD <= amount <= MAX_LIMIT_USD:
                await self._reply(user, copy.LIMIT_INVALID)
                return
            await vault.set_limit(db, user.id, amount, "user_override")
            await self._reply(user, copy.UPDATED)
        elif command.name == "location":
            await vault.clear_origin(db, user.id)
            user.onboarding_state = OnboardingState.AWAITING_LOCATION
            await self._reply(user, copy.ASK_LOCATION)
        elif command.name == "car":
            modes = await vault.get_modes(db, user.id)
            if modes is not None:
                modes.drive = is_yes(command.arg)
                await vault.set_modes(db, user.id, modes)
            await self._reply(user, copy.UPDATED)
        elif command.name == "start":
            await self._reply(user, copy.ALREADY_SET)
        else:
            await self._reply(user, copy.HELP)
