from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.subscription import SubscriptionCreate, SubscriptionResponse
from app.services.subscription import (
    SKUNotFoundError,
    UpstreamStockError,
    register_subscription,
)

router = APIRouter(prefix="/api", tags=["Subscriptions"])


@router.post(
    "/requests",
    response_model=SubscriptionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create or check a back-in-stock subscription",
)
async def create_subscription_request(
    payload: SubscriptionCreate,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    try:
        sub, is_duplicate = await register_subscription(
            session=db,
            email=payload.email,
            sku=payload.sku,
        )
    except SKUNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except UpstreamStockError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while processing your request. Please try again.",
        ) from exc

    if is_duplicate:
        response.status_code = status.HTTP_200_OK
        return SubscriptionResponse(
            status="already_subscribed",
            message="You are already on the notification list for this item. We will email you once it's available.",
            id=sub.id,
            email=sub.email,
            sku=sub.sku,
            created_at=sub.created_at,
        )

    response.status_code = status.HTTP_201_CREATED
    return SubscriptionResponse(
        status="subscribed",
        message="You're subscribed! We will send you an email the moment this item is back in stock.",
        id=sub.id,
        email=sub.email,
        sku=sub.sku,
        created_at=sub.created_at,
    )
