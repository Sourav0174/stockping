from datetime import datetime, timezone
import json
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outbox import EmailOutbox, OutboxStatus
from app.models.subscription import SubscriptionRequest, SubscriptionStatus
from app.models.webhook import WebhookEvent, WebhookStatus
from app.services.stock_client import (
    StockAPIConnectionError,
    StockAPIResponseError,
    stock_client,
)


def is_product_in_stock(stock_data: dict) -> bool:
    """
    Evaluates authoritative stock response: { quantity, in_stock, ... }.
    Confirms product is in stock if in_stock is True or quantity > 0.
    """
    if not stock_data:
        return False
    in_stock_flag = stock_data.get("in_stock")
    if in_stock_flag is not None:
        if isinstance(in_stock_flag, bool):
            return in_stock_flag
        if str(in_stock_flag).lower() in ("false", "0"):
            return False
        if str(in_stock_flag).lower() in ("true", "1"):
            return True

    qty = stock_data.get("quantity")
    if qty is not None:
        try:
            return int(qty) > 0
        except (ValueError, TypeError):
            pass
    return False


async def ingest_webhook_event(
    session: AsyncSession,
    event_id: str,
    sku: str,
    quantity: int,
    raw_payload: str,
) -> tuple[WebhookEvent, bool]:
    """
    Persists the incoming webhook event atomically.
    Returns (WebhookEvent, is_duplicate: bool).
    """
    existing = await session.get(WebhookEvent, event_id)
    if existing is not None:
        return existing, True

    new_event = WebhookEvent(
        event_id=event_id,
        sku=sku,
        quantity=quantity,
        payload=raw_payload,
        status=WebhookStatus.PENDING.value,
    )
    session.add(new_event)

    try:
        await session.commit()
        await session.refresh(new_event)
        return new_event, False
    except IntegrityError:
        await session.rollback()
        existing = await session.get(WebhookEvent, event_id)
        if existing:
            return existing, True
        raise


async def verify_and_schedule_event_jobs(
    session: AsyncSession,
    event: WebhookEvent,
    client=None,
) -> int:
    """
    Performs authoritative stock verification and creates outbox notification jobs.
    Returns the number of created notification jobs.
    """
    target_client = client or stock_client

    # 1. Authoritative verification via GET {STOCK_API_URL}/{sku}
    try:
        stock_data = await target_client.get_sku_stock(event.sku)
    except (StockAPIConnectionError, StockAPIResponseError) as exc:
        # Preserve event as pending for worker retry
        event.status = WebhookStatus.PENDING.value
        event.last_error = f"Stock verification failed: {exc}"
        await session.commit()
        raise

    in_stock = is_product_in_stock(stock_data)

    if not in_stock:
        # Product is still out of stock; do not notify customers
        event.status = WebhookStatus.OUT_OF_STOCK.value
        event.processed_at = datetime.now(timezone.utc)
        event.last_error = None
        await session.commit()
        return 0

    # 2. Product is confirmed in stock: identify eligible pending subscriptions
    stmt = select(SubscriptionRequest).where(
        SubscriptionRequest.sku == event.sku,
        SubscriptionRequest.status == SubscriptionStatus.PENDING.value,
    )
    result = await session.execute(stmt)
    pending_subs = result.scalars().all()

    jobs_created = 0
    now = datetime.now(timezone.utc)

    for sub in pending_subs:
        # Check if outbox job already exists (idempotency guard)
        check_stmt = select(EmailOutbox).where(
            EmailOutbox.subscription_id == sub.id,
            EmailOutbox.event_id == event.event_id,
        )
        existing_job = (await session.execute(check_stmt)).scalar_one_or_none()
        if existing_job is not None:
            continue

        job = EmailOutbox(
            subscription_id=sub.id,
            event_id=event.event_id,
            email=sub.email,
            sku=sub.sku,
            status=OutboxStatus.PENDING.value,
            attempts=0,
            created_at=now,
            next_retry_at=now,
        )
        session.add(job)
        jobs_created += 1

    event.status = WebhookStatus.PROCESSED.value
    event.processed_at = now
    event.last_error = None

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()

    return jobs_created
