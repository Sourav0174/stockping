import os
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401 - ensure all models are registered on Base.metadata
from app.database import Base, get_db
from app.main import app
from app.services.worker import worker


@pytest_asyncio.fixture
async def test_db_session(tmp_path):
    """
    Creates an isolated file-based SQLite database with WAL mode and pragmas
    for realistic testing of concurrency and constraints.
    """
    db_file = tmp_path / "test_stockping.db"
    test_db_url = f"sqlite+aiosqlite:///{db_file}"

    test_engine = create_async_engine(
        test_db_url,
        echo=False,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(test_engine.sync_engine, "connect")
    def configure_pragmas(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA busy_timeout=5000;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.close()

    async_session = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    # Initialize tables and unique partial indexes
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Configure background worker to use the test database
    orig_worker_session_maker = worker.session_maker
    worker.session_maker = async_session

    async def override_get_db():
        async with async_session() as session:
            try:
                yield session
            finally:
                await session.close()

    app.dependency_overrides[get_db] = override_get_db

    yield async_session

    app.dependency_overrides.clear()
    worker.session_maker = orig_worker_session_maker
    await test_engine.dispose()


@pytest_asyncio.fixture
async def client(test_db_session):
    """
    Async HTTP client for testing FastAPI endpoints.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
