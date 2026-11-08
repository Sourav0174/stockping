from datetime import datetime, timezone
import enum
from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class WebhookStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSED = "processed"
    OUT_OF_STOCK = "out_of_stock"
    FAILED_VERIFICATION = "failed_verification"


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    event_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    sku: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default=WebhookStatus.PENDING.value,
        index=True,
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
