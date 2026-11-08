from datetime import datetime, timezone
import enum
from sqlalchemy import DateTime, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SubscriptionStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    NOTIFIED = "notified"
    FAILED = "failed"


OPEN_STATUSES = (SubscriptionStatus.PENDING.value, SubscriptionStatus.PROCESSING.value)


class SubscriptionRequest(Base):
    __tablename__ = "subscription_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    sku: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=SubscriptionStatus.PENDING.value,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)

    __table_args__ = (
        # Enforce uniqueness only on active open requests ('pending' or 'processing')
        # This natively allows a user to resubscribe after a previous request reached 'notified'
        Index(
            "uq_open_subscription",
            "email",
            "sku",
            unique=True,
            sqlite_where=text("status IN ('pending', 'processing')"),
        ),
        Index("ix_requests_sku_status", "sku", "status"),
    )
