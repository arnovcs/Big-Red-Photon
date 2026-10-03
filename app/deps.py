"""Builds provider instances from settings.

The LLM and finance providers can be injected, so tests can pass stubs. Finance
defaults to Nessie when NESSIE_API_KEY is set (Stage 5). With no LLM, planning
continues with empty preferences (§10).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.providers.cache import RecordReplayCache
from app.providers.mock.routing import MockRouting
from app.providers.mock.sim_messaging import SimMessaging
from app.providers.protocols import (
    FinanceProvider,
    LLMProvider,
    MessagingProvider,
    PlacesProvider,
    RoutingProvider,
)
from app.providers.real.gemini import GeminiLLM
from app.providers.real.google_places import GooglePlaces
from app.providers.real.google_routes import GoogleRoutes
from app.providers.real.nessie import NessieFinance
from app.providers.real.ors import OrsRouting
from app.providers.real.photon import PhotonMessaging
from app.settings import Settings


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Deps:
    settings: Settings
    messaging: MessagingProvider
    places: PlacesProvider
    routing: RoutingProvider
    llm: LLMProvider | None = None
    finance: FinanceProvider | None = None
    clock: Callable[[], datetime] = field(default=utcnow)


def build_deps(
    settings: Settings,
    *,
    llm: LLMProvider | None = None,
    finance: FinanceProvider | None = None,
    clock: Callable[[], datetime] | None = None,
    places: PlacesProvider | None = None,
) -> Deps:
    """`places` lets tests plug in a fake; otherwise it's Google Places."""
    messaging: MessagingProvider
    if settings.provider_messaging == "photon":
        messaging = PhotonMessaging(settings)
    else:
        messaging = SimMessaging()
    cache = RecordReplayCache(settings.cache_mode)
    places = places or GooglePlaces(settings, cache)
    routing: RoutingProvider
    if settings.provider_routing == "google":
        routing = GoogleRoutes(settings, cache)
    elif settings.provider_routing == "ors":
        routing = OrsRouting(settings, cache)
    else:
        routing = MockRouting(settings)
    return Deps(
        settings=settings,
        messaging=messaging,
        places=places,
        routing=routing,
        llm=llm or (GeminiLLM(settings, cache) if settings.gemini_api_key else None),
        finance=finance or (NessieFinance(settings) if settings.nessie_api_key else None),
        clock=clock or utcnow,
    )
