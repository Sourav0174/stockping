from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_database_url(raw_url: str | None) -> str:
    """
    Normalizes database connection URLs:
    - Defaults absent or empty URLs to the local SQLite fallback.
    - Converts Neon/PostgreSQL schemes ('postgres://', 'postgresql://') to 'postgresql+asyncpg://'.
    - Converts SQLite schemes without driver to 'sqlite+aiosqlite://'.
    - Translates query parameter 'sslmode' to 'ssl' as required by asyncpg.
    """
    if not raw_url or not str(raw_url).strip():
        return "sqlite+aiosqlite:///./stockping.db"

    raw_url = str(raw_url).strip()

    if raw_url.startswith("postgres://"):
        raw_url = "postgresql+asyncpg://" + raw_url[len("postgres://"):]
    elif raw_url.startswith("postgresql://"):
        raw_url = "postgresql+asyncpg://" + raw_url[len("postgresql://"):]
    elif raw_url.startswith("sqlite://") and not raw_url.startswith("sqlite+"):
        raw_url = "sqlite+aiosqlite://" + raw_url[len("sqlite://"):]

    if raw_url.startswith("postgresql+asyncpg://"):
        if "?" in raw_url:
            base_url, query_str = raw_url.split("?", 1)
            params = query_str.split("&")
            new_params = []
            has_ssl = False
            sslmode_val = None
            for p in params:
                if not p:
                    continue
                k, *v = p.split("=", 1)
                val = v[0] if v else ""
                if k == "sslmode":
                    sslmode_val = val
                elif k == "ssl":
                    has_ssl = True
                    new_params.append(p)
                else:
                    new_params.append(p)
            if not has_ssl and sslmode_val is not None:
                new_params.append(f"ssl={sslmode_val}")
            raw_url = base_url + ("?" + "&".join(new_params) if new_params else "")

    return raw_url


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
        description="Database connection URL (PostgreSQL or SQLite fallback)"
    )

    WORKER_POLL_INTERVAL: float = Field(default=1.0, description="Worker poll interval in seconds")
    WORKER_LEASE_SECONDS: int = Field(default=60, description="Lock lease duration in seconds")
    WORKER_MAX_ATTEMPTS: int = Field(default=5, description="Maximum retry attempts before permanent failure")

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def validate_database_url(cls, v: object) -> str:
        return normalize_database_url(str(v) if v is not None else None)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
