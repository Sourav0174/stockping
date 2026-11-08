from collections.abc import AsyncGenerator
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import normalize_database_url, settings


def create_engine_and_session(
    database_url: str | None = None,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """
    Builds an async database engine and session maker:
    - Automatically detects PostgreSQL vs SQLite.
    - Enables pool pre-ping on PostgreSQL for reliable connection recovery on cloud serverless hosts (Neon).
    - Enforces SQLite-only pragmas (WAL mode, busy timeout, foreign keys) exclusively on SQLite connections.
    """
    url = normalize_database_url(database_url or settings.DATABASE_URL)
    is_sqlite = url.startswith("sqlite")
    connect_args = {"check_same_thread": False} if is_sqlite else {}

    eng = create_async_engine(
        url,
        echo=False,
        connect_args=connect_args,
        pool_pre_ping=not is_sqlite,
    )

    if is_sqlite:
        @event.listens_for(eng.sync_engine, "connect")
        def configure_sqlite_pragmas(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA busy_timeout=5000;")
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.close()

    maker = async_sessionmaker(
        bind=eng,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    return eng, maker


engine, async_session_maker = create_engine_and_session()


class Base(DeclarativeBase):
    pass


# Register all models on Base.metadata
from app import models  # noqa: E402, F401


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db(target_engine: AsyncEngine | None = None) -> None:
    eng = target_engine or engine
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
