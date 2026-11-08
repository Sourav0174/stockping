import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.config import normalize_database_url
from app.database import Base, create_engine_and_session, init_db
from app.models.outbox import EmailOutbox, OutboxStatus
from app.models.subscription import SubscriptionRequest, SubscriptionStatus
from app.models.webhook import WebhookEvent, WebhookStatus
from app.services.subscription import register_subscription
from app.services.webhook_service import ingest_webhook_event, verify_and_schedule_event_jobs
from app.services.worker import OutboxWorker


def test_normalize_database_url():
    """Verifies standard Neon and cloud PostgreSQL URLs are correctly converted for asyncpg."""
    neon_postgres = "postgres://alex:secret123@ep-cool-fog-123456.us-east-2.aws.neon.tech/neondb?sslmode=require"
    assert normalize_database_url(neon_postgres) == (
        "postgresql+asyncpg://alex:secret123@ep-cool-fog-123456.us-east-2.aws.neon.tech/neondb?ssl=require"
    )

    neon_postgresql = "postgresql://alex:secret123@ep-cool-fog-123456.us-east-2.aws.neon.tech/neondb?sslmode=require"
    assert normalize_database_url(neon_postgresql) == (
        "postgresql+asyncpg://alex:secret123@ep-cool-fog-123456.us-east-2.aws.neon.tech/neondb?ssl=require"
    )

    already_asyncpg = "postgresql+asyncpg://alex:secret123@ep-cool-fog-123456.us-east-2.aws.neon.tech/neondb?sslmode=require"
    assert normalize_database_url(already_asyncpg) == (
        "postgresql+asyncpg://alex:secret123@ep-cool-fog-123456.us-east-2.aws.neon.tech/neondb?ssl=require"
    )

    local_pg = "postgresql://localhost:5432/stockping"
    assert normalize_database_url(local_pg) == "postgresql+asyncpg://localhost:5432/stockping"

    # SQLite fallback cases
    assert normalize_database_url(None) == "sqlite+aiosqlite:///./stockping.db"
    assert normalize_database_url("") == "sqlite+aiosqlite:///./stockping.db"
    assert normalize_database_url("   ") == "sqlite+aiosqlite:///./stockping.db"
    assert normalize_database_url("sqlite:///./custom.db") == "sqlite+aiosqlite:///./custom.db"


@pytest.fixture(scope="module")
def postgres_service():
    """
    Provisions an isolated, disposable local PostgreSQL cluster for integration testing.
    Shuts down and cleans up completely after the test module finishes.
    """
    pg_dir = tempfile.mkdtemp(prefix="stockping_pg_test_")
    port = "54388"

    init_res = subprocess.run(
        ["initdb", "-D", pg_dir, "-A", "trust", "-U", "postgres", "--no-instructions"],
        capture_output=True,
        text=True,
    )
    if init_res.returncode != 0:
        shutil.rmtree(pg_dir, ignore_errors=True)
        pytest.skip(f"initdb not available or failed: {init_res.stderr}")

    start_res = subprocess.run(
        ["pg_ctl", "-D", pg_dir, "-o", f"-p {port} -k {pg_dir} -h 127.0.0.1", "-l", f"{pg_dir}/server.log", "start"],
        capture_output=True,
        text=True,
    )
    if start_res.returncode != 0:
        shutil.rmtree(pg_dir, ignore_errors=True)
        pytest.skip(f"pg_ctl start failed: {start_res.stderr}")

    ready = False
    for _ in range(25):
        chk = subprocess.run(["pg_isready", "-h", "127.0.0.1", "-p", port], capture_output=True)
        if chk.returncode == 0:
            ready = True
            break
        time.sleep(0.2)

    if not ready:
        subprocess.run(["pg_ctl", "-D", pg_dir, "stop"], capture_output=True)
        shutil.rmtree(pg_dir, ignore_errors=True)
        pytest.skip("PostgreSQL failed to become ready in time.")

    createdb_res = subprocess.run(
        ["createdb", "-h", "127.0.0.1", "-p", port, "-U", "postgres", "test_stockping_pg"],
        capture_output=True,
        text=True,
    )
    if createdb_res.returncode != 0:
        subprocess.run(["pg_ctl", "-D", pg_dir, "stop"], capture_output=True)
        shutil.rmtree(pg_dir, ignore_errors=True)
        pytest.skip(f"createdb failed: {createdb_res.stderr}")

    raw_pg_url = f"postgresql://postgres@127.0.0.1:{port}/test_stockping_pg"

    try:
        yield raw_pg_url
    finally:
        subprocess.run(["pg_ctl", "-D", pg_dir, "stop"], capture_output=True)
        shutil.rmtree(pg_dir, ignore_errors=True)


@pytest_asyncio.fixture
async def pg_session(postgres_service):
    """Provides a fresh PostgreSQL database session with schema recreated for each test."""
    pg_url = normalize_database_url(postgres_service)
    engine, session_maker = create_engine_and_session(pg_url)

    # Recreate all tables cleanly
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as session:
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_schema_and_partial_indexes(pg_session):
    """Verifies that PostgreSQL successfully creates tables and partial unique indexes."""
    query = text("""
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE tablename IN ('subscription_requests', 'email_outbox')
        ORDER BY indexname;
    """)
    result = await pg_session.execute(query)
    indexes = {row[0]: row[1] for row in result.all()}

    # Verify uq_open_subscription partial index exists with WHERE clause in PostgreSQL
    assert "uq_open_subscription" in indexes
    open_sub_def = indexes["uq_open_subscription"].lower()
    assert "unique index" in open_sub_def
    assert "where" in open_sub_def
    assert "pending" in open_sub_def
    assert "processing" in open_sub_def

    # Verify uq_active_outbox_per_sub partial index exists with WHERE clause in PostgreSQL
    assert "uq_active_outbox_per_sub" in indexes
    active_outbox_def = indexes["uq_active_outbox_per_sub"].lower()
    assert "unique index" in active_outbox_def
    assert "where" in active_outbox_def


@pytest.mark.asyncio
async def test_postgres_open_subscription_uniqueness_and_resubscribe(pg_session):
    """
    Verifies that PostgreSQL partial unique index blocks duplicate open subscriptions
    while permitting a new subscription after a prior one is completed ('notified').
    """
    sub1 = SubscriptionRequest(
        email="customer@example.com",
        sku="SKU-POSTGRES-1",
        status=SubscriptionStatus.PENDING.value,
    )
    pg_session.add(sub1)
    await pg_session.commit()
    sub1_id = sub1.id

    # Attempt inserting duplicate open subscription
    sub2 = SubscriptionRequest(
        email="customer@example.com",
        sku="SKU-POSTGRES-1",
        status=SubscriptionStatus.PENDING.value,
    )
    pg_session.add(sub2)
    with pytest.raises(IntegrityError):
        await pg_session.commit()
    await pg_session.rollback()

    # Mark first subscription as notified (closed)
    sub1_refreshed = await pg_session.get(SubscriptionRequest, sub1_id)
    assert sub1_refreshed is not None
    sub1_refreshed.status = SubscriptionStatus.NOTIFIED.value
    sub1_refreshed.notified_at = datetime.now(timezone.utc)
    await pg_session.commit()

    # Now a new open subscription for the same email + SKU must succeed
    sub3 = SubscriptionRequest(
        email="customer@example.com",
        sku="SKU-POSTGRES-1",
        status=SubscriptionStatus.PENDING.value,
    )
    pg_session.add(sub3)
    await pg_session.commit()
    assert sub3.id is not None
    assert sub3.id != sub1_id


@pytest.mark.asyncio
async def test_postgres_webhook_deduplication(pg_session):
    """Verifies that webhook events are idempotently recorded and deduplicated in PostgreSQL."""
    event1, is_dup1 = await ingest_webhook_event(
        session=pg_session,
        event_id="evt_pg_001",
        sku="SKU-PG-RESTOCK",
        quantity=25,
        raw_payload='{"event_id": "evt_pg_001", "sku": "SKU-PG-RESTOCK", "quantity": 25}',
    )
    assert not is_dup1
    assert event1.event_id == "evt_pg_001"

    # Ingesting the same event ID again
    event2, is_dup2 = await ingest_webhook_event(
        session=pg_session,
        event_id="evt_pg_001",
        sku="SKU-PG-RESTOCK",
        quantity=25,
        raw_payload='{"event_id": "evt_pg_001", "sku": "SKU-PG-RESTOCK", "quantity": 25}',
    )
    assert is_dup2
    assert event2.event_id == "evt_pg_001"


@pytest.mark.asyncio
async def test_postgres_atomic_subscription_claim_and_outbox_uniqueness(pg_session):
    """
    Verifies atomic subscription claiming ('pending' -> 'processing') and outbox job creation
    under PostgreSQL. Tests that uq_active_outbox_per_sub blocks duplicate active jobs.
    """
    class MockStockClient:
        async def get_sku_stock(self, sku: str):
            return {"sku": sku, "in_stock": True, "quantity": 15}

    sub = SubscriptionRequest(
        email="user_pg@example.com",
        sku="SKU-CLAIM-TEST",
        status=SubscriptionStatus.PENDING.value,
    )
    pg_session.add(sub)
    await pg_session.commit()

    event = WebhookEvent(
        event_id="evt_claim_001",
        sku="SKU-CLAIM-TEST",
        quantity=15,
        payload="{}",
        status=WebhookStatus.PENDING.value,
    )
    pg_session.add(event)
    await pg_session.commit()

    # Schedule event jobs
    jobs_created = await verify_and_schedule_event_jobs(
        session=pg_session,
        event=event,
        client=MockStockClient(),
    )
    assert jobs_created == 1

    # Verify subscription was atomically transitioned to 'processing'
    refreshed_sub = await pg_session.get(SubscriptionRequest, sub.id)
    assert refreshed_sub.status == SubscriptionStatus.PROCESSING.value

    # Verify outbox job was created
    stmt = select(EmailOutbox).where(EmailOutbox.subscription_id == sub.id)
    job = (await pg_session.execute(stmt)).scalar_one()
    assert job.status == OutboxStatus.PENDING.value
    assert job.event_id == "evt_claim_001"

    # Verify PostgreSQL database constraint prevents a second active job for the same subscription
    duplicate_job = EmailOutbox(
        subscription_id=sub.id,
        event_id="evt_claim_002",
        email=sub.email,
        sku=sub.sku,
        status=OutboxStatus.PENDING.value,
        attempts=0,
    )
    pg_session.add(duplicate_job)
    with pytest.raises(IntegrityError):
        await pg_session.commit()
    await pg_session.rollback()


@pytest.mark.asyncio
async def test_postgres_transaction_rollback_preserves_subscription_state(pg_session):
    """
    Verifies that if an error occurs during outbox creation, the entire PostgreSQL transaction
    is rolled back, returning the subscription to 'pending' without corrupting state.
    """
    sub = SubscriptionRequest(
        email="rollback_user@example.com",
        sku="SKU-ROLLBACK",
        status=SubscriptionStatus.PENDING.value,
    )
    pg_session.add(sub)
    await pg_session.commit()
    sub_id = sub.id

    event = WebhookEvent(
        event_id="evt_rollback_001",
        sku="SKU-ROLLBACK",
        quantity=10,
        payload="{}",
        status=WebhookStatus.PENDING.value,
    )
    pg_session.add(event)
    await pg_session.commit()

    class FaultyStockClient:
        async def get_sku_stock(self, sku: str):
            return {"sku": sku, "in_stock": True, "quantity": 10}

    # Simulate an error inside the transaction by monkey-patching or invalid DB operation
    # Here we intentionally violate a foreign key in outbox creation
    class BrokenEvent:
        event_id = "non_existent_event_fk"
        sku = "SKU-ROLLBACK"

    with pytest.raises(Exception):
        await verify_and_schedule_event_jobs(
            session=pg_session,
            event=BrokenEvent(),
            client=FaultyStockClient(),
        )

    # Subscription must still be pending after rollback
    refreshed_sub = await pg_session.get(SubscriptionRequest, sub_id)
    assert refreshed_sub is not None
    assert refreshed_sub.status == SubscriptionStatus.PENDING.value


@pytest.mark.asyncio
async def test_postgres_worker_processing_and_lease_recovery(pg_session, postgres_service):
    """
    Verifies that the outbox worker processes jobs, transitions subscriptions to 'notified',
    and recovers expired leases on PostgreSQL.
    """
    pg_url = normalize_database_url(postgres_service)
    _, session_maker = create_engine_and_session(pg_url)

    sub = SubscriptionRequest(
        email="worker_pg@example.com",
        sku="SKU-WORKER-PG",
        status=SubscriptionStatus.PROCESSING.value,
    )
    pg_session.add(sub)
    await pg_session.commit()

    event = WebhookEvent(
        event_id="evt_worker_pg_001",
        sku="SKU-WORKER-PG",
        quantity=5,
        payload="{}",
        status=WebhookStatus.PROCESSED.value,
    )
    pg_session.add(event)
    await pg_session.commit()

    now = datetime.now(timezone.utc)
    job = EmailOutbox(
        subscription_id=sub.id,
        event_id="evt_worker_pg_001",
        email=sub.email,
        sku=sub.sku,
        status=OutboxStatus.PENDING.value,
        attempts=0,
        next_retry_at=now - timedelta(seconds=10),
    )
    pg_session.add(job)
    await pg_session.commit()

    # Mock email delivery
    import app.services.worker as worker_module
    orig_client = worker_module.email_client

    class MockEmailClient:
        async def send_restock_email(self, email: str, sku: str):
            return {"id": "mock_email_123"}

    worker_module.email_client = MockEmailClient()
    test_worker = OutboxWorker(session_maker=session_maker, poll_interval=0.1)

    try:
        metrics = await test_worker.run_once(session=pg_session)
        assert metrics["jobs_processed"] == 1

        # Check job is sent and subscription is notified
        refreshed_job = await pg_session.get(EmailOutbox, job.id)
        assert refreshed_job.status == OutboxStatus.SENT.value
        assert refreshed_job.sent_at is not None

        refreshed_sub = await pg_session.get(SubscriptionRequest, sub.id)
        assert refreshed_sub.status == SubscriptionStatus.NOTIFIED.value
        assert refreshed_sub.notified_at is not None

        # Now test lease recovery: create a job stuck in 'processing' with expired lease
        stuck_sub = SubscriptionRequest(
            email="stuck@example.com",
            sku="SKU-STUCK",
            status=SubscriptionStatus.PROCESSING.value,
        )
        pg_session.add(stuck_sub)
        await pg_session.commit()

        stuck_job = EmailOutbox(
            subscription_id=stuck_sub.id,
            event_id="evt_worker_pg_001",
            email="stuck@example.com",
            sku="SKU-STUCK",
            status=OutboxStatus.PROCESSING.value,
            lease_timeout_at=now - timedelta(seconds=120),
            next_retry_at=now,
        )
        pg_session.add(stuck_job)
        await pg_session.commit()

        recovered_count = await test_worker.recover_expired_leases(session=pg_session)
        assert recovered_count == 1

        refreshed_stuck = await pg_session.get(EmailOutbox, stuck_job.id)
        assert refreshed_stuck.status == OutboxStatus.PENDING.value
        assert refreshed_stuck.lease_timeout_at is None

    finally:
        worker_module.email_client = orig_client
