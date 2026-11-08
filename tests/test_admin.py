import base64
import pytest

from app.config import settings
from app.models.subscription import SubscriptionRequest, SubscriptionStatus


def make_basic_auth(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("utf-8")
    return f"Basic {token}"


@pytest.mark.asyncio
async def test_admin_requires_authentication(client):
    """GET /admin without authentication returns 401 with WWW-Authenticate header."""
    response = await client.get("/admin")
    assert response.status_code == 401
    assert "Basic" in response.headers.get("WWW-Authenticate", "")


@pytest.mark.asyncio
async def test_admin_rejects_invalid_password(client):
    """GET /admin with incorrect password returns 401."""
    auth_header = make_basic_auth("admin", "wrong_password")
    response = await client.get("/admin", headers={"Authorization": auth_header})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_admin_succeeds_with_correct_password(client, test_db_session):
    """GET /admin with correct password returns 200 OK and strict anti-framing headers."""
    auth_header = make_basic_auth("admin", settings.ADMIN_PASSWORD)
    response = await client.get("/admin", headers={"Authorization": auth_header})
    assert response.status_code == 200
    assert "StockPing Management" in response.text

    # Strict clickjacking protection for admin pages
    assert response.headers.get("X-Frame-Options") == "DENY"
    assert "frame-ancestors 'none'" in response.headers.get("Content-Security-Policy", "")


@pytest.mark.asyncio
async def test_admin_empty_state(client, test_db_session):
    """GET /admin renders friendly empty state when no subscriptions exist."""
    auth_header = make_basic_auth("admin", settings.ADMIN_PASSWORD)
    response = await client.get("/admin", headers={"Authorization": auth_header})
    assert response.status_code == 200
    assert "No customer subscription requests recorded yet" in response.text


@pytest.mark.asyncio
async def test_admin_displays_subscription_details_and_escapes_html(client, test_db_session):
    """GET /admin lists subscriptions, delivery attempts, and escapes user content to prevent XSS."""
    async with test_db_session() as session:
        # User input with potentially malicious HTML tags
        sub = SubscriptionRequest(
            email="<script>alert('xss')</script>@example.com",
            sku="HOOD-<img src=x onerror=alert(1)>",
            status=SubscriptionStatus.NOTIFIED.value,
            attempts=1,
            last_error="<b>Transient Resend drop</b>",
        )
        session.add(sub)
        await session.commit()

    auth_header = make_basic_auth("admin", settings.ADMIN_PASSWORD)
    response = await client.get("/admin", headers={"Authorization": auth_header})
    assert response.status_code == 200

    body = response.text
    # Raw scripts and unescaped HTML tags MUST NOT appear
    assert "<script>alert('xss')</script>" not in body
    assert "<img src=x onerror=alert(1)>" not in body
    assert "<b>Transient Resend drop</b>" not in body

    # Escaped safe entities MUST appear
    assert "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;@example.com" in body
    assert "&lt;img src=x onerror=alert(1)&gt;" in body
    assert "&lt;b&gt;Transient Resend drop&lt;/b&gt;" in body
    assert "badge-success" in body


@pytest.mark.asyncio
async def test_admin_api_endpoint_protected(client, test_db_session):
    """GET /admin/api/subscriptions is also protected by basic auth."""
    # 401 without auth
    res_unauth = await client.get("/admin/api/subscriptions")
    assert res_unauth.status_code == 401

    # 200 with auth
    auth_header = make_basic_auth("admin", settings.ADMIN_PASSWORD)
    res_auth = await client.get("/admin/api/subscriptions", headers={"Authorization": auth_header})
    assert res_auth.status_code == 200
    assert isinstance(res_auth.json(), list)
