import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.models.outbox import EmailOutbox, OutboxStatus
from app.models.subscription import SubscriptionRequest, SubscriptionStatus
from app.models.webhook import WebhookEvent, WebhookStatus
from app.services.email_client import EmailRateLimitError, EmailRetryableError
from app.services.stock_client import StockAPIConnectionError
from app.services.worker import worker


@pytest.mark.asyncio
async def test_01_valid_webhook_authentication(client):
    """Scenario 1: Valid webhook authentication succeeds with 200 OK."""
    with patch("app.services.webhook_service.stock_client.get_sku_stock", new=AsyncMock(return_value={"in_stock": True, "quantity": 5})):
        resp = await client.post(
            "/webhooks/stock",
            headers={"X-Webhook-Secret": settings.WEBHOOK_SECRET},
            json={"event_id": "evt_auth_1", "sku": "HOOD-01", "quantity": 5},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] in ("processed", "duplicate_acknowledged")


@pytest.mark.asyncio
async def test_02_invalid_webhook_secret(client):
    """Scenario 2: Invalid or missing webhook secret rejected with 401 Unauthorized."""
    # Missing header
    res1 = await client.post(
        "/webhooks/stock",
        json={"event_id": "evt_auth_2", "sku": "HOOD-01", "quantity": 5},
    )
    assert res1.status_code == 401

    # Bad header
    res2 = await client.post(
        "/webhooks/stock",
        headers={"X-Webhook-Secret": "wrong_secret_key"},
        json={"event_id": "evt_auth_2", "sku": "HOOD-01", "quantity": 5},
    )
    assert res2.status_code == 401


@pytest.mark.asyncio
async def test_03_malformed_payload(client):
    """Scenario 3: Malformed payload rejected with 422 Unprocessable Entity."""
    headers = {"X-Webhook-Secret": settings.WEBHOOK_SECRET}

    # Missing event_id
    res1 = await client.post("/webhooks/stock", headers=headers, json={"sku": "HOOD-01", "quantity": 5})
    assert res1.status_code == 422

    # Negative quantity
    res2 = await client.post("/webhooks/stock", headers=headers, json={"event_id": "e3", "sku": "HOOD-01", "quantity": -1})
    assert res2.status_code == 422

    # Invalid type for quantity
    res3 = await client.post("/webhooks/stock", headers=headers, json={"event_id": "e3", "sku": "HOOD-01", "quantity": "not_an_int"})
    assert res3.status_code == 422


@pytest.mark.asyncio
async def test_04_unknown_sku(client, test_db_session):
    """Scenario 4: Stock API reports SKU not found/empty -> marked out_of_stock, 0 jobs."""
    with patch("app.services.webhook_service.stock_client.get_sku_stock", new=AsyncMock(return_value={})):
        resp = await client.post(
            "/webhooks/stock",
            headers={"X-Webhook-Secret": settings.WEBHOOK_SECRET},
            json={"event_id": "evt_unknown_sku", "sku": "UNKNOWN-99", "quantity": 10},
        )
    assert resp.status_code == 200

    async with test_db_session() as session:
        event = await session.get(WebhookEvent, "evt_unknown_sku")
        assert event is not None
        assert event.status == WebhookStatus.OUT_OF_STOCK.value


@pytest.mark.asyncio
async def test_05_stock_api_reports_out_of_stock(client, test_db_session):
    """Scenario 5: Webhook claims quantity 10, but stock API confirms 0 -> no notifications."""
    async with test_db_session() as session:
        sub = SubscriptionRequest(email="wait@example.com", sku="HOOD-OOS", status="pending")
        session.add(sub)
        await session.commit()

    with patch("app.services.webhook_service.stock_client.get_sku_stock", new=AsyncMock(return_value={"in_stock": False, "quantity": 0})):
        resp = await client.post(
            "/webhooks/stock",
            headers={"X-Webhook-Secret": settings.WEBHOOK_SECRET},
            json={"event_id": "evt_oos_1", "sku": "HOOD-OOS", "quantity": 10},
        )
    assert resp.status_code == 200

    async with test_db_session() as session:
        # Event is marked out_of_stock
        event = await session.get(WebhookEvent, "evt_oos_1")
        assert event.status == WebhookStatus.OUT_OF_STOCK.value

        # Subscription remains pending
        refreshed_sub = await session.get(SubscriptionRequest, sub.id)
        assert refreshed_sub.status == SubscriptionStatus.PENDING.value

        # No jobs created in outbox
        jobs = (await session.execute(select(EmailOutbox))).scalars().all()
        assert len(jobs) == 0


@pytest.mark.asyncio
async def test_06_stock_api_confirms_in_stock(client, test_db_session):
    """Scenario 6: Stock API confirms in stock -> jobs created for eligible pending subscriptions."""
    async with test_db_session() as session:
        sub = SubscriptionRequest(email="ready@example.com", sku="HOOD-IN", status="pending")
        session.add(sub)
        await session.commit()

    with patch("app.services.webhook_service.stock_client.get_sku_stock", new=AsyncMock(return_value={"in_stock": True, "quantity": 4})):
        resp = await client.post(
            "/webhooks/stock",
            headers={"X-Webhook-Secret": settings.WEBHOOK_SECRET},
            json={"event_id": "evt_in_stock_1", "sku": "HOOD-IN", "quantity": 4},
        )
    assert resp.status_code == 200
    assert "Scheduled 1" in resp.json()["message"]

    async with test_db_session() as session:
        event = await session.get(WebhookEvent, "evt_in_stock_1")
        assert event.status == WebhookStatus.PROCESSED.value

        # Outbox job created
        jobs = (await session.execute(select(EmailOutbox).where(EmailOutbox.event_id == "evt_in_stock_1"))).scalars().all()
        assert len(jobs) == 1
        assert jobs[0].email == "ready@example.com"
        assert jobs[0].status == OutboxStatus.PENDING.value


@pytest.mark.asyncio
async def test_07_duplicate_webhook_event(client, test_db_session):
    """Scenario 7: Duplicate webhook event acknowledged with 200 without duplicate processing."""
    with patch("app.services.webhook_service.stock_client.get_sku_stock", new=AsyncMock(return_value={"in_stock": True, "quantity": 5})):
        res1 = await client.post(
            "/webhooks/stock",
            headers={"X-Webhook-Secret": settings.WEBHOOK_SECRET},
            json={"event_id": "evt_dup_1", "sku": "HOOD-DUP", "quantity": 5},
        )
        assert res1.status_code == 200
        assert res1.json()["status"] == "processed"

        # Resend exact same webhook
        res2 = await client.post(
            "/webhooks/stock",
            headers={"X-Webhook-Secret": settings.WEBHOOK_SECRET},
            json={"event_id": "evt_dup_1", "sku": "HOOD-DUP", "quantity": 5},
        )
        assert res2.status_code == 200
        assert res2.json()["status"] == "duplicate_acknowledged"


@pytest.mark.asyncio
async def test_08_concurrent_duplicate_webhook_delivery(client, test_db_session):
    """Scenario 8: Concurrent duplicate webhook deliveries handled atomically."""
    with patch("app.services.webhook_service.stock_client.get_sku_stock", new=AsyncMock(return_value={"in_stock": True, "quantity": 5})):
        headers = {"X-Webhook-Secret": settings.WEBHOOK_SECRET}
        payload = {"event_id": "evt_concurrent_dup", "sku": "HOOD-CONC", "quantity": 5}

        async def send():
            return await client.post("/webhooks/stock", headers=headers, json=payload)

        responses = await asyncio.gather(*(send() for _ in range(5)))

    assert all(r.status_code == 200 for r in responses)
    statuses = [r.json()["status"] for r in responses]
    assert "processed" in statuses
    assert "duplicate_acknowledged" in statuses

    # Exactly 1 row in webhook_events
    async with test_db_session() as session:
        events = (await session.execute(select(WebhookEvent).where(WebhookEvent.event_id == "evt_concurrent_dup"))).scalars().all()
        assert len(events) == 1


@pytest.mark.asyncio
async def test_09_stock_api_timeout_and_event_recovery(client, test_db_session):
    """Scenario 9: Stock API timeout preserves event as pending; worker recovers and processes it."""
    # 1. Create a subscription
    async with test_db_session() as session:
        sub = SubscriptionRequest(email="recover@example.com", sku="HOOD-REC", status="pending")
        session.add(sub)
        await session.commit()

    # 2. Webhook arrives while Stock API times out
    with patch("app.services.webhook_service.stock_client.get_sku_stock", new=AsyncMock(side_effect=StockAPIConnectionError("Timeout"))):
        resp = await client.post(
            "/webhooks/stock",
            headers={"X-Webhook-Secret": settings.WEBHOOK_SECRET},
            json={"event_id": "evt_timeout_1", "sku": "HOOD-REC", "quantity": 5},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending_verification"

    # Verify event stored with status=pending in DB
    async with test_db_session() as session:
        evt = await session.get(WebhookEvent, "evt_timeout_1")
        assert evt.status == WebhookStatus.PENDING.value

    # 3. Stock API recovers; background worker runs a pass
    with patch("app.services.webhook_service.stock_client.get_sku_stock", new=AsyncMock(return_value={"in_stock": True, "quantity": 5})):
        await worker.run_once()

    # Verify event now processed and outbox job created
    async with test_db_session() as session:
        evt = await session.get(WebhookEvent, "evt_timeout_1")
        assert evt.status == WebhookStatus.PROCESSED.value
        jobs = (await session.execute(select(EmailOutbox).where(EmailOutbox.event_id == "evt_timeout_1"))).scalars().all()
        assert len(jobs) == 1


@pytest.mark.asyncio
async def test_10_notification_job_uniqueness(test_db_session):
    """Scenario 10: Enforce that same subscription cannot get multiple jobs for the same stock event."""
    async with test_db_session() as session:
        # Create parent WebhookEvent and SubscriptionRequest
        evt = WebhookEvent(event_id="evt_u1", sku="HOOD-UNI", quantity=10, payload="{}")
        sub = SubscriptionRequest(email="unique@example.com", sku="HOOD-UNI", status="pending")
        session.add_all([evt, sub])
        await session.commit()

        job1 = EmailOutbox(subscription_id=sub.id, event_id="evt_u1", email=sub.email, sku=sub.sku)
        session.add(job1)
        await session.commit()

        # Attempt to insert identical subscription_id and event_id -> should fail uniqueness constraint
        from sqlalchemy.exc import IntegrityError
        job2 = EmailOutbox(subscription_id=sub.id, event_id="evt_u1", email=sub.email, sku=sub.sku)
        session.add(job2)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_11_successful_email_delivery(test_db_session):
    """Scenario 11: Worker delivers email via Resend and updates subscription to notified."""
    async with test_db_session() as session:
        evt = WebhookEvent(event_id="evt_deliv", sku="HOOD-DELIV", quantity=5, payload="{}")
        sub = SubscriptionRequest(email="customer@example.com", sku="HOOD-DELIV", status="pending")
        session.add_all([evt, sub])
        await session.commit()

        job = EmailOutbox(subscription_id=sub.id, event_id="evt_deliv", email=sub.email, sku=sub.sku)
        session.add(job)
        await session.commit()
        job_id = job.id
        sub_id = sub.id

    with patch("app.services.worker.email_client.send_restock_email", new=AsyncMock(return_value={"id": "msg_123"})):
        await worker.run_once()

    async with test_db_session() as session:
        refreshed_job = await session.get(EmailOutbox, job_id)
        assert refreshed_job.status == OutboxStatus.SENT.value
        assert refreshed_job.sent_at is not None

        refreshed_sub = await session.get(SubscriptionRequest, sub_id)
        assert refreshed_sub.status == SubscriptionStatus.NOTIFIED.value
        assert refreshed_sub.notified_at is not None


@pytest.mark.asyncio
async def test_12_email_http_500_and_retry(test_db_session):
    """Scenario 12: Resend returns HTTP 500 -> job retried with exponential backoff."""
    async with test_db_session() as session:
        evt = WebhookEvent(event_id="evt_500", sku="HOOD-500", quantity=5, payload="{}")
        sub = SubscriptionRequest(email="retry500@example.com", sku="HOOD-500", status="pending")
        session.add_all([evt, sub])
        await session.commit()

        job = EmailOutbox(subscription_id=sub.id, event_id="evt_500", email=sub.email, sku=sub.sku)
        session.add(job)
        await session.commit()
        job_id = job.id
        sub_id = sub.id

    with patch("app.services.worker.email_client.send_restock_email", new=AsyncMock(side_effect=EmailRetryableError("Resend 500 error"))):
        await worker.run_once()

    async with test_db_session() as session:
        refreshed_job = await session.get(EmailOutbox, job_id)
        assert refreshed_job.status == OutboxStatus.PENDING.value
        assert refreshed_job.attempts == 1
        assert "Resend 500" in refreshed_job.last_error

        # Subscription remains pending
        refreshed_sub = await session.get(SubscriptionRequest, sub_id)
        assert refreshed_sub.status == SubscriptionStatus.PENDING.value
        assert refreshed_sub.notified_at is None


@pytest.mark.asyncio
async def test_13_email_http_429_and_retry(test_db_session):
    """Scenario 13: Resend returns HTTP 429 -> worker respects rate limit backoff."""
    async with test_db_session() as session:
        evt = WebhookEvent(event_id="evt_429", sku="HOOD-429", quantity=5, payload="{}")
        sub = SubscriptionRequest(email="rate@example.com", sku="HOOD-429", status="pending")
        session.add_all([evt, sub])
        await session.commit()

        job = EmailOutbox(subscription_id=sub.id, event_id="evt_429", email=sub.email, sku=sub.sku)
        session.add(job)
        await session.commit()
        job_id = job.id

    with patch("app.services.worker.email_client.send_restock_email", new=AsyncMock(side_effect=EmailRateLimitError("Rate limited", retry_after=45))):
        await worker.run_once()

    async with test_db_session() as session:
        refreshed_job = await session.get(EmailOutbox, job_id)
        assert refreshed_job.status == OutboxStatus.PENDING.value
        assert refreshed_job.attempts == 1
        # next_retry_at is in future (~45s)
        retry_at = refreshed_job.next_retry_at
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        assert retry_at > datetime.now(timezone.utc) + timedelta(seconds=30)


@pytest.mark.asyncio
async def test_14_permanent_email_api_failure(test_db_session):
    """Scenario 14: Non-retryable 4xx client error -> marked failed without endless retries."""
    from app.services.email_client import EmailPermanentError

    async with test_db_session() as session:
        evt = WebhookEvent(event_id="evt_perm", sku="HOOD-PERM", quantity=5, payload="{}")
        sub = SubscriptionRequest(email="bad@example.com", sku="HOOD-PERM", status="pending")
        session.add_all([evt, sub])
        await session.commit()

        job = EmailOutbox(subscription_id=sub.id, event_id="evt_perm", email=sub.email, sku=sub.sku)
        session.add(job)
        await session.commit()
        job_id = job.id

    with patch("app.services.worker.email_client.send_restock_email", new=AsyncMock(side_effect=EmailPermanentError(422, "Unverified sender domain"))):
        await worker.run_once()

    async with test_db_session() as session:
        refreshed_job = await session.get(EmailOutbox, job_id)
        assert refreshed_job.status == OutboxStatus.FAILED.value
        assert "422" in refreshed_job.last_error
        assert "Unverified sender domain" in refreshed_job.last_error


@pytest.mark.asyncio
async def test_15_worker_recovery_after_simulated_restart(test_db_session):
    """Scenario 15: Worker recovers jobs stuck in processing whose lease expired."""
    expired_time = datetime.now(timezone.utc) - timedelta(seconds=120)

    async with test_db_session() as session:
        evt = WebhookEvent(event_id="evt_stuck", sku="HOOD-STUCK", quantity=5, payload="{}")
        sub = SubscriptionRequest(email="stuck@example.com", sku="HOOD-STUCK", status="pending")
        session.add_all([evt, sub])
        await session.commit()

        stuck_job = EmailOutbox(
            subscription_id=sub.id,
            event_id="evt_stuck",
            email=sub.email,
            sku=sub.sku,
            status=OutboxStatus.PROCESSING.value,
            lease_timeout_at=expired_time,
        )
        session.add(stuck_job)
        await session.commit()
        job_id = stuck_job.id

    # Running worker cycle recovers lease to pending and successfully dispatches
    with patch("app.services.worker.email_client.send_restock_email", new=AsyncMock(return_value={"id": "ok"})):
        await worker.run_once()

    async with test_db_session() as session:
        refreshed_job = await session.get(EmailOutbox, job_id)
        assert refreshed_job.status == OutboxStatus.SENT.value


@pytest.mark.asyncio
async def test_16_subscription_notified_only_after_successful_delivery(test_db_session):
    """Scenario 16: Subscription remains pending during delivery failures and updates only on success."""
    async with test_db_session() as session:
        evt = WebhookEvent(event_id="evt_stage", sku="HOOD-STAGE", quantity=5, payload="{}")
        sub = SubscriptionRequest(email="stage@example.com", sku="HOOD-STAGE", status="pending")
        session.add_all([evt, sub])
        await session.commit()

        job = EmailOutbox(subscription_id=sub.id, event_id="evt_stage", email=sub.email, sku=sub.sku)
        session.add(job)
        await session.commit()
        sub_id = sub.id

    # Pass 1: Delivery fails
    with patch("app.services.worker.email_client.send_restock_email", new=AsyncMock(side_effect=EmailRetryableError("Timeout"))):
        await worker.run_once()

    async with test_db_session() as session:
        sub_check = await session.get(SubscriptionRequest, sub_id)
        assert sub_check.status == SubscriptionStatus.PENDING.value
        assert sub_check.notified_at is None

        # Reset next_retry_at to now for test pass 2
        job_check = (await session.execute(select(EmailOutbox).where(EmailOutbox.subscription_id == sub_id))).scalar_one()
        job_check.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()

    # Pass 2: Delivery succeeds
    with patch("app.services.worker.email_client.send_restock_email", new=AsyncMock(return_value={"id": "msg_success"})):
        await worker.run_once()

    async with test_db_session() as session:
        sub_final = await session.get(SubscriptionRequest, sub_id)
        assert sub_final.status == SubscriptionStatus.NOTIFIED.value
        assert sub_final.notified_at is not None


@pytest.mark.asyncio
async def test_17_later_restock_notifies_previously_pending_customer(client, test_db_session):
    """Scenario 17: A customer who remained pending during an initial out-of-stock event gets notified on a subsequent restock."""
    # 1. User subscribes
    with patch("app.services.subscription.stock_client.sku_exists", new=AsyncMock(return_value=True)):
        sub_res = await client.post(
            "/api/requests",
            json={"email": "persistent@example.com", "sku": "HOOD-PERSIST"},
        )
        assert sub_res.status_code == 201
        sub_id = sub_res.json()["id"]

    # 2. First webhook arrives, but Stock API reports out of stock
    with patch("app.services.webhook_service.stock_client.get_sku_stock", new=AsyncMock(return_value={"in_stock": False, "quantity": 0})):
        evt1_res = await client.post(
            "/webhooks/stock",
            headers={"X-Webhook-Secret": settings.WEBHOOK_SECRET},
            json={"event_id": "evt_wave_1", "sku": "HOOD-PERSIST", "quantity": 1},
        )
        assert evt1_res.status_code == 200

    # Verify subscription is still pending
    async with test_db_session() as session:
        sub_state = await session.get(SubscriptionRequest, sub_id)
        assert sub_state.status == SubscriptionStatus.PENDING.value

    # 3. Second webhook arrives later, and Stock API now reports in stock
    with patch("app.services.webhook_service.stock_client.get_sku_stock", new=AsyncMock(return_value={"in_stock": True, "quantity": 10})):
        evt2_res = await client.post(
            "/webhooks/stock",
            headers={"X-Webhook-Secret": settings.WEBHOOK_SECRET},
            json={"event_id": "evt_wave_2", "sku": "HOOD-PERSIST", "quantity": 10},
        )
        assert evt2_res.status_code == 200

    # 4. Worker delivers notification
    with patch("app.services.worker.email_client.send_restock_email", new=AsyncMock(return_value={"id": "msg_sent"})):
        await worker.run_once()

    # 5. Verify customer is now successfully notified
    async with test_db_session() as session:
        sub_final = await session.get(SubscriptionRequest, sub_id)
        assert sub_final.status == SubscriptionStatus.NOTIFIED.value
        assert sub_final.notified_at is not None
