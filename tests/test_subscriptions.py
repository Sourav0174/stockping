import asyncio
from unittest.mock import AsyncMock, patch
import pytest
from sqlalchemy import select

from app.models.subscription import SubscriptionRequest, SubscriptionStatus
from app.services.stock_client import StockAPIConnectionError, StockAPIResponseError


@pytest.mark.asyncio
async def test_successful_subscription(client, test_db_session):
    """Scenario 1: Successful subscription with email & SKU normalization."""
    with patch("app.services.subscription.stock_client.sku_exists", new=AsyncMock(return_value=True)):
        response = await client.post(
            "/api/requests",
            json={"email": "  Customer.One@Example.COM ", "sku": "  HOOD-FLG-GRY-M  "},
        )

    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "subscribed"
    assert data["email"] == "customer.one@example.com"
    assert data["sku"] == "HOOD-FLG-GRY-M"
    assert "id" in data

    # Verify DB persistence
    async with test_db_session() as session:
        result = await session.execute(
            select(SubscriptionRequest).where(SubscriptionRequest.id == data["id"])
        )
        saved = result.scalar_one()
        assert saved.email == "customer.one@example.com"
        assert saved.sku == "HOOD-FLG-GRY-M"
        assert saved.status == SubscriptionStatus.PENDING.value


@pytest.mark.asyncio
async def test_invalid_email(client):
    """Scenario 2: Invalid email formats rejected with 422."""
    for bad_email in ["invalid-email", "test@", "@example.com", ""]:
        response = await client.post(
            "/api/requests",
            json={"email": bad_email, "sku": "HOOD-FLG-GRY-M"},
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_unknown_sku(client):
    """Scenario 3: Unknown SKU rejected with 404."""
    with patch("app.services.subscription.stock_client.sku_exists", new=AsyncMock(return_value=False)):
        response = await client.post(
            "/api/requests",
            json={"email": "user@example.com", "sku": "NONEXISTENT-SKU"},
        )

    assert response.status_code == 404
    data = response.json()
    assert "detail" in data
    assert "NONEXISTENT-SKU" in data["detail"]


@pytest.mark.asyncio
async def test_duplicate_open_subscription(client, test_db_session):
    """Scenario 4: Duplicate open subscription returns friendly 200 without creating new rows."""
    with patch("app.services.subscription.stock_client.sku_exists", new=AsyncMock(return_value=True)):
        # First submission -> 201
        res1 = await client.post(
            "/api/requests",
            json={"email": "dup@example.com", "sku": "HOOD-FLG-GRY-M"},
        )
        assert res1.status_code == 201
        first_id = res1.json()["id"]

        # Duplicate submission (varying case and whitespace) -> 200 already_subscribed
        res2 = await client.post(
            "/api/requests",
            json={"email": " DUP@example.com ", "sku": "HOOD-FLG-GRY-M"},
        )
        assert res2.status_code == 200
        data2 = res2.json()
        assert data2["status"] == "already_subscribed"
        assert data2["id"] == first_id

    # Verify only 1 row exists in DB
    async with test_db_session() as session:
        result = await session.execute(
            select(SubscriptionRequest).where(SubscriptionRequest.email == "dup@example.com")
        )
        rows = result.scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_new_subscription_after_notified(client, test_db_session):
    """Scenario 5: Allow a new open subscription after a previous request was notified."""
    with patch("app.services.subscription.stock_client.sku_exists", new=AsyncMock(return_value=True)):
        # First submission
        res1 = await client.post(
            "/api/requests",
            json={"email": "repeat@example.com", "sku": "HOOD-FLG-GRY-M"},
        )
        assert res1.status_code == 201
        first_id = res1.json()["id"]

        # Mark first request as notified (closed)
        async with test_db_session() as session:
            result = await session.execute(
                select(SubscriptionRequest).where(SubscriptionRequest.id == first_id)
            )
            sub = result.scalar_one()
            sub.status = SubscriptionStatus.NOTIFIED.value
            await session.commit()

        # Submit new request for the same email and SKU -> 201 Created
        res2 = await client.post(
            "/api/requests",
            json={"email": "repeat@example.com", "sku": "HOOD-FLG-GRY-M"},
        )
        assert res2.status_code == 201
        second_id = res2.json()["id"]
        assert second_id != first_id

    # Verify both rows exist in DB (one notified, one pending)
    async with test_db_session() as session:
        result = await session.execute(
            select(SubscriptionRequest)
            .where(SubscriptionRequest.email == "repeat@example.com")
            .order_by(SubscriptionRequest.id)
        )
        rows = result.scalars().all()
        assert len(rows) == 2
        assert rows[0].status == SubscriptionStatus.NOTIFIED.value
        assert rows[1].status == SubscriptionStatus.PENDING.value


@pytest.mark.asyncio
async def test_upstream_stock_api_failure(client):
    """Scenario 6: Upstream failure returns 502 without leaking credentials."""
    with patch(
        "app.services.subscription.stock_client.sku_exists",
        new=AsyncMock(side_effect=StockAPIConnectionError("Connection timed out")),
    ):
        response = await client.post(
            "/api/requests",
            json={"email": "test@example.com", "sku": "HOOD-FLG-GRY-M"},
        )

    assert response.status_code == 502
    data = response.json()
    assert "detail" in data
    # Ensure no credentials or raw stack traces leak
    assert "dev_stock_key" not in data["detail"]
    assert "Bearer" not in data["detail"]


@pytest.mark.asyncio
async def test_concurrent_duplicate_submissions(client, test_db_session):
    """Scenario 7: Concurrent duplicate submissions handle race condition gracefully."""
    with patch("app.services.subscription.stock_client.sku_exists", new=AsyncMock(return_value=True)):
        payload = {"email": "concurrent@example.com", "sku": "HOOD-FLG-GRY-M"}

        async def send_request():
            return await client.post("/api/requests", json=payload)

        # Dispatch 5 concurrent requests simultaneously
        responses = await asyncio.gather(*(send_request() for _ in range(5)))

    status_codes = [r.status_code for r in responses]
    created_count = status_codes.count(201)
    already_subscribed_count = status_codes.count(200)

    # Exactly one request created, remaining 4 safely received already_subscribed 200
    assert created_count == 1
    assert already_subscribed_count == 4

    # Verify database has exactly 1 row
    async with test_db_session() as session:
        result = await session.execute(
            select(SubscriptionRequest).where(
                SubscriptionRequest.email == "concurrent@example.com"
            )
        )
        rows = result.scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_embed_iframe_headers(client):
    """Verify security headers specifically permit iframe embedding on /embed."""
    response = await client.get("/embed?sku=HOOD-FLG-GRY-M")
    assert response.status_code == 200
    assert "frame-ancestors *" in response.headers.get("Content-Security-Policy", "")
    assert "x-frame-options" not in response.headers

    # Verify standard routes keep strict framing protection
    api_response = await client.get("/health")
    assert api_response.headers.get("X-Frame-Options") == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in api_response.headers.get("Content-Security-Policy", "")
