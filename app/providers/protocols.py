"""Provider interfaces (§8). Do not change these signatures without asking the team."""

from datetime import datetime
from typing import Protocol

from app.models.candidates import Candidate, EventFinding, ResolvedPlace
from app.models.conversation import GroupPreferences, PseudonymousMessage
from app.models.identity import DirectoryUser
from app.models.outbound import GroupSafeMessage, PrivateMessage
from app.models.private import FinancialSnapshot, LatLng
from app.models.routing import Mode, RouteDetail, RouteEstimate


class MessagingProvider(Protocol):
    async def send_group(self, handles: list[str], msg: GroupSafeMessage) -> None:
        """Send the same group-safe message to each member's DM (v3: no group chats)."""
        ...

    async def send_private(self, handle: str, msg: PrivateMessage) -> None: ...

    async def request_location(self, handle: str) -> bool:
        """Ask this person to share their location (iMessage: a Find My card). True if sent."""
        ...

    async def shared_location(self, handle: str) -> LatLng | None:
        """Their current shared location, or None if they aren't sharing with the bot.

        Private: only onboarding may store it, through the vault (§11)."""
        ...

    async def react(self, handle: str, message_id: str, emoji: str) -> bool:
        """A tapback (❤️ 👍 👎 😂 ‼️ ❓) on one of their messages. True if it landed;
        callers fall back to a short text when it didn't. Never raises."""
        ...

    async def typing(self, handle: str, on: bool) -> None:
        """Show / clear the typing indicator in their DM. Best effort, never raises."""
        ...

    async def send_link(self, handle: str, url: str) -> bool:
        """A link preview card (e.g. Google Maps directions). True if sent. Never raises."""
        ...


class UserDirectoryProvider(Protocol):
    """Who may text the bot. On Photon's shared line pool, a number must be registered as a
    user of the project before the bot can talk to it, and each user is assigned the line
    (bot number) they text."""

    async def register(
        self,
        phone: str,
        first_name: str,
        email: str | None = None,
        last_name: str | None = None,
    ) -> DirectoryUser | None:
        """Make sure `phone` (E.164) is a user; return their id and the line they text.

        `email` is stored on the platform (the API doesn't send invites). Idempotent.
        None if the call failed: never raises.
        """
        ...

    def opt_in_link(self, user_id: str, message: str) -> str | None:
        """A link that opens Messages to this user's line with `message` typed in. On
        Photon's shared lines, sending through it is how a person opts in to being
        texted. None if the platform has no such link (then use a plain sms: link)."""
        ...


class FinanceProvider(Protocol):
    async def get_customer(self, customer_id: str) -> dict: ...

    async def get_financial_snapshot(self, customer_id: str) -> FinancialSnapshot: ...


class PlacesProvider(Protocol):
    async def search_nearby(
        self,
        center: LatLng,
        radius_m: int,
        categories: list[str],
        open_at: datetime,
        cuisines: list[str] | None = None,
        activities: list[str] | None = None,
    ) -> list[Candidate]:
        """`cuisines` (e.g. ["japanese"]) and `activities` (e.g. ["pickleball"]): what
        people asked for, searched specifically so matching venues are among the results
        (tagged with the activity). Optional to honour."""
        ...

    async def text_search(self, query: str, near: LatLng) -> list[Candidate]: ...

    async def nearest_place_name(self, location: LatLng) -> str | None:
        """The closest named place to a point ("Collegetown Bagels"), to describe someone's
        live location back to them. None if nothing's close or the lookup failed."""
        ...

    async def geocode(self, text: str, near: LatLng) -> ResolvedPlace | None:
        """The place someone typed, searched near `near`. None if nothing matched or the
        lookup failed: callers ask the person to rephrase, never substitute a default."""
        ...


class RoutingProvider(Protocol):
    async def matrix(
        self,
        origins: dict[str, LatLng],
        destinations: dict[str, LatLng],
        modes: dict[str, set[Mode]],
        depart_at: datetime,
    ) -> list[RouteEstimate]: ...

    async def route(
        self,
        origin: LatLng,
        destination: LatLng,
        mode: Mode,
        arrive_by: datetime | None = None,
        depart_at: datetime | None = None,
    ) -> RouteDetail: ...


class LLMProvider(Protocol):
    async def extract_preferences(
        self, transcript: list[PseudonymousMessage], now_local: datetime
    ) -> GroupPreferences: ...

    async def phrase_explanations(self, facts: list[dict]) -> list[str]: ...


class ContextProvider(Protocol):
    """Stage 7 stretch (Gemini + Search grounding, if tier allows)."""

    async def find_events(
        self, area_label: str, when: datetime, intent: str
    ) -> list[EventFinding]: ...
