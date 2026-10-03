"""Provider interfaces (§8). Do not change these signatures without asking the team."""

from datetime import datetime
from typing import Protocol

from app.models.candidates import Candidate, EventFinding, ResolvedPlace
from app.models.conversation import GroupPreferences, PseudonymousMessage
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


class UserDirectoryProvider(Protocol):
    """Who may text the bot. On Photon's shared line pool, a number must be registered as a
    user of the project before the bot can talk to it, and each user is assigned the line
    (bot number) they text."""

    async def register(self, phone: str, first_name: str) -> str | None:
        """Make sure `phone` (E.164) is a user; return the number they should text.

        Idempotent. None if the number isn't known (yet) or the call failed: never raises.
        """
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
