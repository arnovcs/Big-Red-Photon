"""FastAPI app: lifespan (DB init, provider wiring) and routers."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health, sim, webhooks
from app.conversation.router import Router
from app.db import session as db
from app.deps import Deps, build_deps
from app.logging import get_logger, kv, setup_logging
from app.settings import get_settings

log = get_logger(__name__)


def create_app(deps: Deps | None = None) -> FastAPI:
    """Pass `deps` to inject providers (tests); otherwise they're built from settings."""
    settings = deps.settings if deps is not None else get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        setup_logging()
        db.configure(settings.database_url)
        await db.init_db()
        app.state.deps = deps or build_deps(settings)
        app.state.chat_router = Router(app.state.deps)
        log.info(
            kv(
                "startup",
                messaging=settings.provider_messaging,
                places=settings.provider_places,
                routing=settings.provider_routing,
                llm="on" if app.state.deps.llm else "off",
                cache=settings.cache_mode,
            )
        )
        yield
        app.state.chat_router.shutdown()
        await db.dispose()

    app = FastAPI(title="Big Red Photon", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(webhooks.router)
    if settings.provider_messaging == "sim":
        app.include_router(sim.router)
    return app


app = create_app()
