from datetime import datetime
from pydantic import BaseModel, ConfigDict, EmailStr, field_validator


class SubscriptionCreate(BaseModel):
    email: EmailStr
    sku: str

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @field_validator("sku", mode="before")
    @classmethod
    def validate_sku(cls, v: str) -> str:
        if not isinstance(v, str):
            raise ValueError("SKU must be a string")
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("SKU cannot be empty")
        if len(cleaned) > 100:
            raise ValueError("SKU exceeds maximum length of 100 characters")
        return cleaned


class SubscriptionResponse(BaseModel):
    status: str
    message: str
    id: int
    email: str
    sku: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
