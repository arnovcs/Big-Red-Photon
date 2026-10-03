"""Provider interfaces (§8). Do not change these signatures without asking the team."""

from datetime import datetime
from typing import Protocol

from app.models.candidates import Candidate, EventFinding
from app.models.conversation import GroupPreferences, PseudonymousMessage
from app.models.outbound import GroupSafeMessage, PrivateMessage
from app.models.private import FinancialSnapshot, LatLng
from app.models.routing import Mode, RouteDetail, RouteEstimate


class MessagingProvider(Protocol):
    async def send_group(self, chat_id: str, msg: GroupSafeMessage) -> str | None:
        """Send to a group chat. Returns poll_id if a native poll was sent."""
        ...

    async def send_private(self, handle: str, msg: PrivateMessage) -> None: ...


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
    ) -> list[Candidate]: ...

    async def text_search(self, query: str, near: LatLng) -> list[Candidate]: ...

    async def geocode(self, text: str, near: LatLng) -> tuple[LatLng, str] | None:
        """Returns (coords, clean label), or None if nothing matched.

        Checks demo_locations first, then Nominatim.
        """
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
