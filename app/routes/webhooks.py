import hmac
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.schemas.webhook import WebhookResponse, WebhookStockPayload
from app.services.stock_client import StockAPIConnectionError, StockAPIResponseError
from app.services.webhook_service import (
    ingest_webhook_event,
    verify_and_schedule_event_jobs,
)

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


@router.post(
    "/stock",
    response_model=WebhookResponse,
    status_code=status.HTTP_200_OK,
    summary="Receive stock update events",
)
async def handle_stock_webhook(
    payload: WebhookStockPayload,
    request: Request,
    x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
    db: AsyncSession = Depends(get_db),
):
    # Constant-time secret authentication to mitigate timing attacks
    if not x_webhook_secret or not hmac.compare_digest(x_webhook_secret, settings.WEBHOOK_SECRET):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Webhook-Secret header.",
        )

    try:
        raw_body = (await request.body()).decode("utf-8")
    except Exception:
        raw_body = payload.model_dump_json()

    event, is_duplicate = await ingest_webhook_event(
        session=db,
        event_id=payload.event_id,
        sku=payload.sku,
        quantity=payload.quantity,
        raw_payload=raw_body,
    )

    if is_duplicate:
        return WebhookResponse(
            status="duplicate_acknowledged",
            message="Event has already been recorded.",
            event_id=payload.event_id,
        )

    try:
        jobs_count = await verify_and_schedule_event_jobs(session=db, event=event)
        return WebhookResponse(
            status="processed",
            message=f"Stock verified. Scheduled {jobs_count} notification jobs.",
            event_id=payload.event_id,
        )
    except (StockAPIConnectionError, StockAPIResponseError):
        # Stock API is temporarily unreachable; event is preserved in SQLite as 'pending'
        return WebhookResponse(
            status="pending_verification",
            message="Event recorded. Stock verification queued for background worker.",
            event_id=payload.event_id,
        )
