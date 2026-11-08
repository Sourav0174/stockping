from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    STOCK_API_URL: str = Field(default="http://localhost:8001/stock", description="Base URL of upstream stock API")
    STOCK_API_KEY: str = Field(default="dev_stock_key", description="Bearer token for stock API")
    RESEND_BASE_URL: str = Field(default="https://api.resend.com", description="Base URL of Resend API")
    RESEND_API_KEY: str = Field(default="dev_resend_key", description="API key for Resend")
    WEBHOOK_SECRET: str = Field(default="dev_webhook_secret", description="Shared secret for incoming webhooks")
    ADMIN_PASSWORD: str = Field(default="dev_admin_password", description="Password for admin dashboard")

    FROM_EMAIL: str = Field(
        default="StockPing <onboarding@resend.dev>",
        description="Sender email address for notifications"
    )

    PORT: int = Field(default=8000, description="HTTP listening port")
    DATABASE_URL: str = Field(
        default="sqlite+aiosqlite:///./stockping.db",
        description="Async SQLite database connection string"
    )

    WORKER_POLL_INTERVAL: float = Field(default=1.0, description="Worker poll interval in seconds")
    WORKER_LEASE_SECONDS: int = Field(default=60, description="Lock lease duration in seconds")
    WORKER_MAX_ATTEMPTS: int = Field(default=5, description="Maximum retry attempts before permanent failure")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
