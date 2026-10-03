"""FastAPI app: lifespan (DB init) and routers."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health
from app.db import session as db
from app.logging import get_logger, kv, setup_logging
from app.settings import get_settings

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging()
    db.configure(settings.database_url)
    await db.init_db()
    log.info(
        kv(
            "startup",
            messaging=settings.provider_messaging,
            places=settings.provider_places,
            routing=settings.provider_routing,
            cache=settings.cache_mode,
        )
    )
    # Provider wiring (app/deps.py) arrives in Stage 1.
    yield
    await db.dispose()


app = FastAPI(title="Big Red Photon", lifespan=lifespan)
app.include_router(health.router)
