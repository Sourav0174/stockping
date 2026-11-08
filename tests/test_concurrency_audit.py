import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models.outbox import EmailOutbox, OutboxStatus
from app.models.subscription import SubscriptionRequest, SubscriptionStatus
from app.models.webhook import WebhookEvent, WebhookStatus
from app.services.email_client import EmailPermanentError, EmailRetryableError
from app.services.worker import worker


@pytest.mark.asyncio
async def test_different_event_ids_while_job_pending(client, test_db_session):
    """
    Scenario: Event B arrives for the same SKU while Event A's notification job is still pending.
    Expected: Subscription was atomically transitioned to 'processing' by Event A,
    so Event B does NOT create a duplicate outbox job.
    """
    # 1. Create a pending subscription
    async with test_db_session() as session:
        sub = SubscriptionRequest(email="audit1@example.com", sku="SKU-AUDIT-1", status=SubscriptionStatus.PENDING.value)
        session.add(sub)
        await session.commit()
        sub_id = sub.id

    # 2. Ingest Event A (in stock)
    with patch("app.services.webhook_service.stock_client.get_sku_stock", new=AsyncMock(return_value={"in_stock": True, "quantity": 5})):
        res_a = await client.post(
            "/webhooks/stock",
            headers={"X-Webhook-Secret": settings.WEBHOOK_SECRET},
            json={"event_id": "evt_audit_1_A", "sku": "SKU-AUDIT-1", "quantity": 5},
        )
        assert res_a.status_code == 200

    # Verify subscription is now 'processing' and 1 job exists
    async with test_db_session() as session:
        sub_check = await session.get(SubscriptionRequest, sub_id)
        assert sub_check.status == SubscriptionStatus.PROCESSING.value

        jobs = (await session.execute(
            select(EmailOutbox).where(EmailOutbox.subscription_id == sub_id)
        )).scalars().all()
        assert len(jobs) == 1
        assert jobs[0].event_id == "evt_audit_1_A"
        assert jobs[0].status == OutboxStatus.PENDING.value

    # 3. Ingest Event B for the same SKU while Job A is pending
    with patch("app.services.webhook_service.stock_client.get_sku_stock", new=AsyncMock(return_value={"in_stock": True, "quantity": 8})):
        res_b = await client.post(
            "/webhooks/stock",
            headers={"X-Webhook-Secret": settings.WEBHOOK_SECRET},
            json={"event_id": "evt_audit_1_B", "sku": "SKU-AUDIT-1", "quantity": 8},
        )
        assert res_b.status_code == 200

    # Verify still exactly 1 job exists (no duplicate job created)
    async with test_db_session() as session:
        jobs = (await session.execute(
            select(EmailOutbox).where(EmailOutbox.subscription_id == sub_id)
        )).scalars().all()
        assert len(jobs) == 1
        assert jobs[0].event_id == "evt_audit_1_A"


@pytest.mark.asyncio
async def test_different_event_ids_while_job_processing(client, test_db_session):
    """
    Scenario: Event B arrives while Event A's notification job is currently 'processing' (lease claimed).
    Expected: Subscription is 'processing', so Event B does NOT create a duplicate job.
    """
    async with test_db_session() as session:
        sub = SubscriptionRequest(email="audit2@example.com", sku="SKU-AUDIT-2", status=SubscriptionStatus.PENDING.value)
        session.add(sub)
        await session.commit()
        sub_id = sub.id

    # Ingest Event A
    with patch("app.services.webhook_service.stock_client.get_sku_stock", new=AsyncMock(return_value={"in_stock": True, "quantity": 5})):
        res_a = await client.post(
            "/webhooks/stock",
            headers={"X-Webhook-Secret": settings.WEBHOOK_SECRET},
            json={"event_id": "evt_audit_2_A", "sku": "SKU-AUDIT-2", "quantity": 5},
        )
        assert res_a.status_code == 200

    # Mark job A as 'processing' (simulating lease acquisition)
    async with test_db_session() as session:
        job = (await session.execute(
            select(EmailOutbox).where(EmailOutbox.subscription_id == sub_id)
        )).scalar_one()
        job.status = OutboxStatus.PROCESSING.value
        job.lease_timeout_at = datetime.now(timezone.utc) + timedelta(seconds=60)
        await session.commit()

    # Ingest Event B for the same SKU
    with patch("app.services.webhook_service.stock_client.get_sku_stock", new=AsyncMock(return_value={"in_stock": True, "quantity": 10})):
        res_b = await client.post(
            "/webhooks/stock",
            headers={"X-Webhook-Secret": settings.WEBHOOK_SECRET},
            json={"event_id": "evt_audit_2_B", "sku": "SKU-AUDIT-2", "quantity": 10},
        )
        assert res_b.status_code == 200

    # Verify no duplicate job created
    async with test_db_session() as session:
        jobs = (await session.execute(
            select(EmailOutbox).where(EmailOutbox.subscription_id == sub_id)
        )).scalars().all()
        assert len(jobs) == 1
        assert jobs[0].event_id == "evt_audit_2_A"


@pytest.mark.asyncio
async def test_concurrent_distinct_events_attempting_same_subscription(client, test_db_session):
    """
    Scenario: 5 distinct stock events for the same SKU arrive concurrently.
    Expected: Exactly ONE event successfully claims the pending subscription;
    the other 4 events see it already claimed and create zero duplicate jobs.
    """
    async with test_db_session() as session:
        sub = SubscriptionRequest(email="conc_audit@example.com", sku="SKU-CONC-AUDIT", status=SubscriptionStatus.PENDING.value)
        session.add(sub)
        await session.commit()
        sub_id = sub.id

    with patch("app.services.webhook_service.stock_client.get_sku_stock", new=AsyncMock(return_value={"in_stock": True, "quantity": 10})):
        headers = {"X-Webhook-Secret": settings.WEBHOOK_SECRET}

        async def post_event(i: int):
            return await client.post(
                "/webhooks/stock",
                headers=headers,
                json={"event_id": f"evt_conc_{i}", "sku": "SKU-CONC-AUDIT", "quantity": 10},
            )

        responses = await asyncio.gather(*(post_event(i) for i in range(5)))

    assert all(r.status_code == 200 for r in responses)

    # Verify exactly 1 outbox job was created for this subscription
    async with test_db_session() as session:
        jobs = (await session.execute(
            select(EmailOutbox).where(EmailOutbox.subscription_id == sub_id)
        )).scalars().all()
        assert len(jobs) == 1

        sub_final = await session.get(SubscriptionRequest, sub_id)
        assert sub_final.status == SubscriptionStatus.PROCESSING.value


@pytest.mark.asyncio
async def test_retryable_email_failure_preserves_claim(client, test_db_session):
    """
    Scenario: Job A encounters a retryable failure (e.g. 500 error). While waiting for backoff,
    Event B arrives for the same SKU.
    Expected: Subscription remains 'processing' during retries. Event B does NOT create another job.
    When Job A is retried and succeeds, subscription transitions to 'notified'.
    """
    async with test_db_session() as session:
        sub = SubscriptionRequest(email="retry_claim@example.com", sku="SKU-RETRY-CLAIM", status=SubscriptionStatus.PENDING.value)
        session.add(sub)
        await session.commit()
        sub_id = sub.id

    # Event A arrives
    with patch("app.services.webhook_service.stock_client.get_sku_stock", new=AsyncMock(return_value={"in_stock": True, "quantity": 5})):
        res_a = await client.post(
            "/webhooks/stock",
            headers={"X-Webhook-Secret": settings.WEBHOOK_SECRET},
            json={"event_id": "evt_rc_A", "sku": "SKU-RETRY-CLAIM", "quantity": 5},
        )
        assert res_a.status_code == 200

    # Worker attempts delivery, fails with retryable 500
    with patch("app.services.worker.email_client.send_restock_email", new=AsyncMock(side_effect=EmailRetryableError("Resend 500 timeout"))):
        await worker.run_once()

    # Subscription remains in 'processing' state during retry backoff
    async with test_db_session() as session:
        sub_check = await session.get(SubscriptionRequest, sub_id)
        assert sub_check.status == SubscriptionStatus.PROCESSING.value

    # Event B arrives while Job A is pending retry
    with patch("app.services.webhook_service.stock_client.get_sku_stock", new=AsyncMock(return_value={"in_stock": True, "quantity": 10})):
        res_b = await client.post(
            "/webhooks/stock",
            headers={"X-Webhook-Secret": settings.WEBHOOK_SECRET},
            json={"event_id": "evt_rc_B", "sku": "SKU-RETRY-CLAIM", "quantity": 10},
        )
        assert res_b.status_code == 200

    # Verify no duplicate job was created
    async with test_db_session() as session:
        jobs = (await session.execute(
            select(EmailOutbox).where(EmailOutbox.subscription_id == sub_id)
        )).scalars().all()
        assert len(jobs) == 1
        assert jobs[0].event_id == "evt_rc_A"

        # Reset next_retry_at so retry can run now
        job = jobs[0]
        job.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()

    # Worker runs retry pass and succeeds
    with patch("app.services.worker.email_client.send_restock_email", new=AsyncMock(return_value={"id": "msg_success"})):
        await worker.run_once()

    # Verified notified!
    async with test_db_session() as session:
        sub_final = await session.get(SubscriptionRequest, sub_id)
        assert sub_final.status == SubscriptionStatus.NOTIFIED.value
        assert sub_final.notified_at is not None


@pytest.mark.asyncio
async def test_permanent_email_failure_recovery_policy(client, test_db_session):
    """
    Scenario: Job A encounters a permanent failure (e.g. 422 invalid sender).
    Expected: Subscription transitions to 'failed'. Subsequent stock events will NOT silently
    create duplicate jobs for an invalid/unsendable email address.
    """
    async with test_db_session() as session:
        sub = SubscriptionRequest(email="perm_fail@example.com", sku="SKU-PERM-FAIL", status=SubscriptionStatus.PENDING.value)
        session.add(sub)
        await session.commit()
        sub_id = sub.id

    # Event A arrives
    with patch("app.services.webhook_service.stock_client.get_sku_stock", new=AsyncMock(return_value={"in_stock": True, "quantity": 5})):
        await client.post(
            "/webhooks/stock",
            headers={"X-Webhook-Secret": settings.WEBHOOK_SECRET},
            json={"event_id": "evt_pf_A", "sku": "SKU-PERM-FAIL", "quantity": 5},
        )

    # Worker attempts delivery, encounters permanent 422
    with patch("app.services.worker.email_client.send_restock_email", new=AsyncMock(side_effect=EmailPermanentError(422, "Domain unverified"))):
        await worker.run_once()

    # Subscription is marked 'failed'
    async with test_db_session() as session:
        sub_check = await session.get(SubscriptionRequest, sub_id)
        assert sub_check.status == SubscriptionStatus.FAILED.value
        assert "Domain unverified" in sub_check.last_error

    # Event B arrives
    with patch("app.services.webhook_service.stock_client.get_sku_stock", new=AsyncMock(return_value={"in_stock": True, "quantity": 10})):
        await client.post(
            "/webhooks/stock",
            headers={"X-Webhook-Secret": settings.WEBHOOK_SECRET},
            json={"event_id": "evt_pf_B", "sku": "SKU-PERM-FAIL", "quantity": 10},
        )

    # Verify no new job created
    async with test_db_session() as session:
        jobs = (await session.execute(
            select(EmailOutbox).where(EmailOutbox.subscription_id == sub_id)
        )).scalars().all()
        assert len(jobs) == 1
        assert jobs[0].event_id == "evt_pf_A"


@pytest.mark.asyncio
async def test_new_subscription_after_notified_and_subsequent_restock(client, test_db_session):
    """
    Scenario: A subscription is completed (notified). Customer submits a new subscription
    for the same SKU, which is later restocked by a subsequent event.
    Expected: Preserves existing functionality allowing re-subscription after completion.
    """
    # 1. Initial subscription
    with patch("app.services.subscription.stock_client.sku_exists", new=AsyncMock(return_value=True)):
        res1 = await client.post(
            "/api/requests",
            json={"email": "re_sub@example.com", "sku": "SKU-RESUB"},
        )
        assert res1.status_code == 201
        sub1_id = res1.json()["id"]

    # 2. First restock event arrives and notifies
    with patch("app.services.webhook_service.stock_client.get_sku_stock", new=AsyncMock(return_value={"in_stock": True, "quantity": 5})):
        await client.post(
            "/webhooks/stock",
            headers={"X-Webhook-Secret": settings.WEBHOOK_SECRET},
            json={"event_id": "evt_resub_1", "sku": "SKU-RESUB", "quantity": 5},
        )

    with patch("app.services.worker.email_client.send_restock_email", new=AsyncMock(return_value={"id": "msg_1"})):
        await worker.run_once()

    async with test_db_session() as session:
        sub1 = await session.get(SubscriptionRequest, sub1_id)
        assert sub1.status == SubscriptionStatus.NOTIFIED.value

    # 3. Customer submits a new subscription for the same SKU -> 201 Created
    with patch("app.services.subscription.stock_client.sku_exists", new=AsyncMock(return_value=True)):
        res2 = await client.post(
            "/api/requests",
            json={"email": "re_sub@example.com", "sku": "SKU-RESUB"},
        )
        assert res2.status_code == 201
        sub2_id = res2.json()["id"]
        assert sub2_id != sub1_id

    # 4. Subsequent restock event arrives -> creates job for subscription 2
    with patch("app.services.webhook_service.stock_client.get_sku_stock", new=AsyncMock(return_value={"in_stock": True, "quantity": 3})):
        await client.post(
            "/webhooks/stock",
            headers={"X-Webhook-Secret": settings.WEBHOOK_SECRET},
            json={"event_id": "evt_resub_2", "sku": "SKU-RESUB", "quantity": 3},
        )

    with patch("app.services.worker.email_client.send_restock_email", new=AsyncMock(return_value={"id": "msg_2"})):
        await worker.run_once()

    # 5. Verify both subscriptions are now completed
    async with test_db_session() as session:
        sub2 = await session.get(SubscriptionRequest, sub2_id)
        assert sub2.status == SubscriptionStatus.NOTIFIED.value
        assert sub2.notified_at is not None


@pytest.mark.asyncio
async def test_db_constraint_rejects_multiple_active_jobs_per_subscription(test_db_session):
    """
    Directly verify database engine constraint:
    Index 'uq_active_outbox_per_sub' forbids multiple 'pending' or 'processing' jobs
    for the same subscription_id.
    """
    async with test_db_session() as session:
        evt1 = WebhookEvent(event_id="evt_c1", sku="SKU-C", quantity=1, payload="{}")
        evt2 = WebhookEvent(event_id="evt_c2", sku="SKU-C", quantity=1, payload="{}")
        sub = SubscriptionRequest(email="c@example.com", sku="SKU-C", status=SubscriptionStatus.PROCESSING.value)
        session.add_all([evt1, evt2, sub])
        await session.commit()

        # Job 1 active (pending)
        job1 = EmailOutbox(subscription_id=sub.id, event_id="evt_c1", email=sub.email, sku=sub.sku, status=OutboxStatus.PENDING.value)
        session.add(job1)
        await session.commit()

        # Attempt Job 2 active (pending) for the same subscription
        job2 = EmailOutbox(subscription_id=sub.id, event_id="evt_c2", email=sub.email, sku=sub.sku, status=OutboxStatus.PENDING.value)
        session.add(job2)
        with pytest.raises(IntegrityError):
            await session.commit()
