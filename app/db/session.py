"""Async engine, session factory, and `init_db()`."""

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.tables import Base

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def configure(database_url: str) -> None:
    global _engine, _session_factory
    _engine = create_async_engine(database_url)
    if database_url.startswith("sqlite"):
        # WAL lets DM handlers write while a planning run holds a read transaction.
        @event.listens_for(_engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Database not configured; call configure() first")
    return _engine


def session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Database not configured; call configure() first")
    return _session_factory


# Columns added after the first release: (table, column, SQL type). create_all() makes
# new tables but never alters existing ones, so existing app.db files get these here.
ADDED_COLUMNS = [
    ("private_profiles", "origin_typed_at", "DATETIME"),
    ("private_profiles", "origin_place_id", "VARCHAR"),
]


async def init_db() -> None:
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if conn.dialect.name == "sqlite":
            for table, column, sql_type in ADDED_COLUMNS:
                rows = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
                if column not in {row[1] for row in rows}:
                    await conn.exec_driver_sql(
                        f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"
                    )


async def reset_db() -> None:
    """Drop and recreate every table (simulator reset only)."""
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)


async def dispose() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
