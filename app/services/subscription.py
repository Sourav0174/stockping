from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.subscription import OPEN_STATUSES, SubscriptionRequest, SubscriptionStatus
from app.services.stock_client import (
    StockAPIConnectionError,
    StockAPIResponseError,
    stock_client,
)


class SubscriptionServiceError(Exception):
    """Base exception for subscription business logic."""
    pass


class SKUNotFoundError(SubscriptionServiceError):
    """Raised when SKU does not exist in the upstream catalogue."""
    def __init__(self, sku: str):
        super().__init__(f"SKU '{sku}' was not found in the product catalogue.")
        self.sku = sku


class UpstreamStockError(SubscriptionServiceError):
    """Raised when the upstream stock service cannot be reached or errors."""
    def __init__(self, message: str):
        super().__init__(message)


async def register_subscription(
    session: AsyncSession,
    email: str,
    sku: str,
    client=None,
) -> tuple[SubscriptionRequest, bool]:
    """
    Registers a new back-in-stock subscription request.
    
    1. Verifies that the SKU exists in the upstream catalogue.
    2. Atomically checks and inserts a subscription request.
    3. If an open subscription ('pending' or 'processing') already exists,
       returns (existing_request, True) without creating a duplicate.
    4. If the SKU was previously subscribed but reached 'notified' (closed),
       allows creating a new open subscription.
    
    Returns:
        (SubscriptionRequest, is_duplicate: bool)
    """
    target_client = client or stock_client

    try:
        exists = await target_client.sku_exists(sku)
    except (StockAPIConnectionError, StockAPIResponseError) as exc:
        raise UpstreamStockError("Unable to verify product catalogue at this time. Please try again.") from exc

    if not exists:
        raise SKUNotFoundError(sku)

    stmt = select(SubscriptionRequest).where(
        SubscriptionRequest.email == email,
        SubscriptionRequest.sku == sku,
        SubscriptionRequest.status.in_(OPEN_STATUSES),
    )
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()

    if existing is not None:
        return existing, True

    new_request = SubscriptionRequest(
        email=email,
        sku=sku,
        status=SubscriptionStatus.PENDING.value,
    )
    session.add(new_request)

    try:
        await session.commit()
        await session.refresh(new_request)
        return new_request, False
    except IntegrityError:
        # Concurrent duplicate insertion caught by database unique index
        await session.rollback()
        result = await session.execute(stmt)
        race_existing = result.scalar_one_or_none()
        if race_existing:
            return race_existing, True
        raise
