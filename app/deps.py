"""Builds provider instances from settings.

The LLM and finance providers are injected (real ones arrive in Stages 3 and 5), so
tests can pass stubs. With no LLM, planning continues with empty preferences (§10).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.providers.mock.places import MockPlaces
from app.providers.mock.routing import MockRouting
from app.providers.mock.sim_messaging import SimMessaging
from app.providers.protocols import (
    FinanceProvider,
    LLMProvider,
    MessagingProvider,
    PlacesProvider,
    RoutingProvider,
)
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
) -> Deps:
    if settings.provider_messaging != "sim":
        raise NotImplementedError("Photon messaging arrives in Stage 6; use PROVIDER_MESSAGING=sim")
    if settings.provider_places != "mock":
        raise NotImplementedError("OSM places arrive in Stage 4; use PROVIDER_PLACES=mock")
    if settings.provider_routing != "mock":
        raise NotImplementedError("ORS routing arrives in Stage 4; use PROVIDER_ROUTING=mock")
    return Deps(
        settings=settings,
        messaging=SimMessaging(),
        places=MockPlaces(),
        routing=MockRouting(settings),
        llm=llm,
        finance=finance,
        clock=clock or utcnow,
    )
