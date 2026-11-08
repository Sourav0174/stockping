from pydantic import BaseModel, Field, field_validator


class WebhookStockPayload(BaseModel):
    event_id: str = Field(..., min_length=1, description="Unique identifier for the stock event")
    sku: str = Field(..., min_length=1, description="Product SKU")
    quantity: int = Field(..., ge=0, description="Reported quantity in event")

    @field_validator("event_id", mode="before")
    @classmethod
    def validate_event_id(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("event_id must be a non-empty string")
        return v.strip()

    @field_validator("sku", mode="before")
    @classmethod
    def validate_sku(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("sku must be a non-empty string")
        return v.strip()


class WebhookResponse(BaseModel):
    status: str
    message: str
    event_id: str
