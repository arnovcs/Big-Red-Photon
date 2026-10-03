"""DM onboarding state machine (§7.2) and READY-state settings commands (§7.1)."""

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
from app.logging import get_logger, kv
from app.messaging.outbound import send_private
from app.models.identity import OnboardingState
from app.models.outbound import PrivateMessage
from app.models.private import LatLng, TravelModes
from app.private import vault

# Short replies that mean "I shared my location" rather than a place name.
log = get_logger(__name__)


def modes_text(modes: TravelModes) -> str:
    """ "driving", "walking (or a ride if needed)", ... for replies and logs."""
    names = {"drive": "driving", "bike": "biking", "walk": "walking", "rideshare": "a ride"}
    chosen = [names[m] for m in ("drive", "bike", "walk") if getattr(modes, m)]
    if modes.rideshare and chosen:
        return f"{' or '.join(chosen)} (or a ride if needed)"
    if modes.rideshare:
        return "taking a ride"
    return " or ".join(chosen)


SHARE_REPLIES = {"done", "shared", "shared it", "sent", "sent it", "here", "i shared", "ok done"}
# Any reply containing one of these ("I shared my live location", "ok") means "look at
# what I shared", not a place name.
SHARE_WORDS = {"done", "shared", "share", "sharing", "sent", "location", "ok", "okay"}


def is_share_reply(text: str) -> bool:
    normalized = " ".join(text.lower().split()).strip(".!")
    words = set(normalized.replace(",", " ").replace(".", " ").replace("!", " ").split())
    return normalized in SHARE_REPLIES or bool(words & SHARE_WORDS)


SAME_REPLIES = {"same", "same place", "same as before", "same as last time"}

MIN_LIMIT_USD = 1
MAX_LIMIT_USD = 1000


class Onboarding:
    def __init__(self, deps: Deps) -> None:
        self.deps = deps

    async def _reply(self, user: UserRow, text: str) -> None:
        await send_private(self.deps.messaging, user.handle, PrivateMessage(text=text))

    async def _ask_location(self, db: AsyncSession, user: UserRow, use_share: bool = True) -> None:
        """Already sharing their location with the bot (and `use_share`)? Use it (they
        confirm yes/no). Otherwise ask where they're starting, with the Find My card."""
        found = await self._shared_location(user) if use_share else None
        if found is not None:
            coords, label = found
            await vault.set_origin(db, user.id, coords, label)
            await self._reply(user, copy.confirm_location(label))
            return
        await self._reply(user, copy.ASK_LOCATION)
        await self._send_location_card(user)

    async def _send_location_card(self, user: UserRow) -> None:
        try:
            await self.deps.messaging.request_location(user.handle)
        except Exception:
            pass  # the typed-landmark path still works

    async def _shared_location(self, user: UserRow) -> tuple[LatLng, str] | None:
        try:
            coords = await self.deps.messaging.shared_location(user.handle)
        except Exception:
            coords = None
        if coords is None:
            return None
        return coords, copy.LIVE_LOCATION_LABEL

    async def handle_dm(self, db: AsyncSession, user: UserRow, text: str, first_dm: bool) -> None:
        """Advance a not-yet-READY user by one step."""
        state = OnboardingState(user.onboarding_state)
        text = text.strip()

        if state == OnboardingState.NEW:
            await self._new(user, text, first_dm)
        elif state == OnboardingState.AWAITING_BANK_CODE:
            await self._bank_code(db, user, text)
        elif state == OnboardingState.AWAITING_LIMIT_CONFIRM:
            await self._limit_confirm(db, user, text)
        elif state == OnboardingState.AWAITING_LOCATION:
            await self._location(db, user, text)
        elif state == OnboardingState.AWAITING_MODES:
            await self._modes(db, user, text)

    async def join_before_ready(self, user: UserRow, code: str, first_dm: bool) -> None:
        """`join <code>` from someone not set up yet: explain, then (re)ask the current step."""
        await self._reply(user, copy.setup_first(code))
        await self.reprompt(user)

    async def reprompt(self, user: UserRow) -> None:
        """(Re)ask whatever setup step this not-yet-READY user is on."""
        state = OnboardingState(user.onboarding_state)
        if state == OnboardingState.NEW:
            await self._new(user, "", first_dm=True)
        elif state == OnboardingState.AWAITING_BANK_CODE:
            await self._reply(user, copy.ask_bank_code(user.display_name))
        elif state == OnboardingState.AWAITING_LIMIT_CONFIRM:
            await self._reply(user, copy.LIMIT_INVALID)
        elif state == OnboardingState.AWAITING_LOCATION:
            await self._reply(user, copy.ASK_LOCATION)
        elif state == OnboardingState.AWAITING_MODES:
            await self._reply(user, copy.ASK_MODES)

    async def _new(self, user: UserRow, text: str, first_dm: bool) -> None:
        if not user.display_name:
            if first_dm:
                await self._reply(user, f"{copy.WELCOME}\n\n{copy.ASK_NAME}")
                return
            user.display_name = text.split()[0][:30] if text else ""
            if not user.display_name:
                await self._reply(user, copy.ASK_NAME)
                return
        elif first_dm:
            await self._reply(user, copy.WELCOME)
        user.onboarding_state = OnboardingState.AWAITING_BANK_CODE
        await self._reply(user, copy.ask_bank_code(user.display_name))

    async def _bank_code(self, db: AsyncSession, user: UserRow, text: str) -> None:
        limit = await vault.link_customer(db, user.id, text, self.deps.finance)
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
        if await queries.claimed_signup_for(db, user.id) is not None:
            # Signed up on the website: no setup location step (asked per plan instead).
            user.onboarding_state = OnboardingState.READY
            await self._reply(user, copy.web_intro(user.display_name))
            return
        user.onboarding_state = OnboardingState.AWAITING_LOCATION
        await self._ask_location(db, user)

    async def _location(self, db: AsyncSession, user: UserRow, text: str) -> None:
        """A typed landmark, or the person's shared (Find My) location. Either way the
        place is stored, then confirmed yes/no."""
        if is_yes(text) or " ".join(text.lower().split()).strip(".!") in SAME_REPLIES:
            # "yes" confirms the place just found; "same" keeps last time's starting point.
            status = await vault.confirm_origin(db, user.id)
            if status == "complete":
                # Re-entry (a plan's location question, or the "location" command).
                user.onboarding_state = OnboardingState.READY
                in_plan = await queries.active_group_for_user(db, user.id) is not None
                await self._reply(user, copy.TRIP_MODES_SET if in_plan else copy.UPDATED)
                return
            if status == "needs_modes":
                # Travel modes are asked per plan (at @plan / join), not during setup.
                user.onboarding_state = OnboardingState.READY
                await self._reply(user, copy.YOU_ARE_SET)
                return
            if not is_share_reply(text):  # "ok" may mean "I shared it": checked below
                await self._reply(user, copy.ASK_LOCATION)  # "yes" with nothing to confirm
                return
        if is_no(text):
            await vault.clear_origin(db, user.id)
            await self._reply(user, copy.ASK_LOCATION)
            return

        if text.strip().startswith("@"):
            # A command (e.g. @plan) sent mid-setup is not a place name.
            await self._reply(user, copy.LOCATION_FIRST)
            return
        if is_share_reply(text):
            found = await self._shared_location(user)
            if found is None:
                await self._reply(user, copy.SHARE_NOT_SEEN)
                return
            coords, label = found
            await vault.set_origin(db, user.id, coords, label)
            log.info(kv("origin_stored", user=user.id.hex[:8], source="shared"))
            await self._reply(user, copy.confirm_location(label))
            return

        # Typed text: Google Places only. Nothing found (or Google down) → ask them to
        # rephrase. Never substitute a default or their shared location: that was the
        # "near Olin Library" loop.
        settings = self.deps.settings
        center = LatLng(lat=settings.demo_center_lat, lng=settings.demo_center_lng)
        log.debug(kv("location_text", user=user.id.hex[:8], text=text))
        try:
            place = await self.deps.places.geocode(text, center)
        except Exception:
            place = None
        if place is None:
            log.info(kv("origin_not_found", user=user.id.hex[:8]))
            await self._reply(user, copy.LOCATION_NOT_FOUND)
            return
        await vault.set_origin(
            db, user.id, place.location, place.label, typed=True, place_id=place.place_id
        )
        log.info(kv("origin_stored", user=user.id.hex[:8], source="typed"))
        await self._reply(user, copy.confirm_location(place.label))

    async def ask_trip_modes(self, db: AsyncSession, user: UserRow) -> None:
        """At @plan / join: forget last time's answer and ask how they're getting there."""
        await vault.clear_modes(db, user.id)
        user.onboarding_state = OnboardingState.AWAITING_MODES
        await self._reply(user, copy.ASK_TRIP_MODES)

    async def change_trip_modes(self, db: AsyncSession, user: UserRow, modes: TravelModes) -> None:
        """A mode said mid-plan ("actually I'll drive"): it replaces their answer."""
        await vault.set_modes(db, user.id, modes)
        log.info(kv("trip_modes", user=user.id.hex[:8], modes=modes_text(modes), source="chat"))
        await self._reply(user, copy.mode_changed(modes_text(modes)))

    async def default_to_walking(self, db: AsyncSession, user: UserRow) -> None:
        """Never said how they're getting there by @go: plan them as walking, and say so."""
        await vault.set_modes(db, user.id, TravelModes(walk=True, rideshare=False))
        user.onboarding_state = OnboardingState.READY
        log.info(kv("trip_modes", user=user.id.hex[:8], modes="walking", source="default"))
        await self._reply(user, copy.DEFAULT_WALK)

    async def _modes(self, db: AsyncSession, user: UserRow, text: str) -> None:
        if text.strip().startswith("@"):
            await self._reply(user, copy.MODES_FIRST)  # e.g. @go before answering
            return
        modes = parse_modes(text)
        if modes is None:
            no_bus = "bus" in text.lower() or "transit" in text.lower()
            await self._reply(user, copy.NO_BUS_YET if no_bus else copy.MODES_INVALID)
            return
        await vault.set_modes(db, user.id, modes)
        log.info(kv("trip_modes", user=user.id.hex[:8], modes=modes_text(modes), source="answer"))
        # Next question, one at a time: where from? Live location answers it by itself.
        if await self._shared_location(user) is not None:
            user.onboarding_state = OnboardingState.READY
            await self._reply(user, copy.TRIP_LIVE_LOCATION)
            return
        user.onboarding_state = OnboardingState.AWAITING_LOCATION
        await self._reply(user, copy.ASK_TRIP_LOCATION)
        await self._send_location_card(user)

    async def handle_settings(self, db: AsyncSession, user: UserRow, text: str) -> bool:
        """READY-state settings commands. Returns False if `text` isn't one."""
        command = parse_dm_command(text)
        if command is None:
            return False
        if command.name == "budget":
            amount = parse_amount(command.arg)
            if amount is None or not MIN_LIMIT_USD <= amount <= MAX_LIMIT_USD:
                await self._reply(user, copy.LIMIT_INVALID)
                return True
            await vault.set_limit(db, user.id, amount, "user_override")
            await self._reply(user, copy.UPDATED)
        elif command.name == "location":
            await vault.clear_origin(db, user.id)
            user.onboarding_state = OnboardingState.AWAITING_LOCATION
            # They asked to change it: ask, even if they're sharing (to type somewhere else).
            await self._ask_location(db, user, use_share=False)
        elif command.name == "car":
            await vault.set_drive(db, user.id, is_yes(command.arg))
            await self._reply(user, copy.UPDATED)
        elif command.name == "start":
            await self._reply(user, copy.ALREADY_SET)
        else:
            await self._reply(user, copy.HELP)
        return True
