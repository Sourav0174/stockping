import base64
from unittest.mock import AsyncMock, patch
import pytest

from app.config import settings
from app.services.worker import worker


def make_basic_auth(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("utf-8")
    return f"Basic {token}"


@pytest.mark.asyncio
async def test_full_end_to_end_lifecycle(client, test_db_session):
    """
    Comprehensive End-to-End smoke test verifying the complete user & admin lifecycle:
    1. GET /health
    2. GET /embed?sku=HOOD-E2E (iframe security headers)
    3. POST /api/requests (new subscription)
    4. POST /api/requests (duplicate open request acknowledgment)
    5. POST /webhooks/stock (webhook authentication, stock verification, outbox job creation)
    6. Background worker delivery (Resend API email dispatch, transition to notified)
    7. GET /admin (authenticated dashboard verification of notified state)
    """
    # 1. Health check
    health_resp = await client.get("/health")
    assert health_resp.status_code == 200
    assert health_resp.json() == {"status": "ok", "service": "StockPing"}

    # 2. Embed UI iframe check
    embed_resp = await client.get("/embed?sku=HOOD-E2E")
    assert embed_resp.status_code == 200
    assert "frame-ancestors *" in embed_resp.headers.get("Content-Security-Policy", "")
    assert "x-frame-options" not in embed_resp.headers

    # 3. New subscription registration
    with patch("app.services.subscription.stock_client.sku_exists", new=AsyncMock(return_value=True)):
        sub_resp = await client.post(
            "/api/requests",
            json={"email": "e2e_customer@example.com", "sku": "HOOD-E2E"},
        )
    assert sub_resp.status_code == 201
    assert sub_resp.json()["status"] == "subscribed"

    # 4. Duplicate subscription submission
    with patch("app.services.subscription.stock_client.sku_exists", new=AsyncMock(return_value=True)):
        dup_resp = await client.post(
            "/api/requests",
            json={"email": "e2e_customer@example.com", "sku": "HOOD-E2E"},
        )
    assert dup_resp.status_code == 200
    assert dup_resp.json()["status"] == "already_subscribed"

    # 5. Webhook event ingestion and authoritative stock check
    with patch("app.services.webhook_service.stock_client.get_sku_stock", new=AsyncMock(return_value={"in_stock": True, "quantity": 7})):
        hook_resp = await client.post(
            "/webhooks/stock",
            headers={"X-Webhook-Secret": settings.WEBHOOK_SECRET},
            json={"event_id": "evt_e2e_99", "sku": "HOOD-E2E", "quantity": 7},
        )
    assert hook_resp.status_code == 200
    assert hook_resp.json()["status"] == "processed"

    # 6. Background worker email delivery
    with patch("app.services.worker.email_client.send_restock_email", new=AsyncMock(return_value={"id": "msg_e2e"})):
        work_res = await worker.run_once()
        assert work_res["jobs_processed"] == 1

    # 7. Authenticated Admin Dashboard check
    auth_header = make_basic_auth("admin", settings.ADMIN_PASSWORD)
    admin_resp = await client.get("/admin", headers={"Authorization": auth_header})
    assert admin_resp.status_code == 200
    assert "e2e_customer@example.com" in admin_resp.text
    assert "HOOD-E2E" in admin_resp.text
    assert "badge-success" in admin_resp.text
    assert "notified" in admin_resp.text
