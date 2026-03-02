"""
app/config.py

Application configuration loaded from environment variables via pydantic-settings.
All secrets are read from .env — never hardcoded.
"""
from functools import lru_cache
from pydantic import Field, HttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Central settings object. Values are sourced (in priority order) from:
      1. Environment variables
      2. .env file
      3. Default values defined here
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ────────────────────────────────────────────────
    app_env: str = Field(default="production", description="development | production")
    app_host: str = Field(default="0.0.0.0")
    app_port: int = Field(default=8000)
    log_level: str = Field(default="INFO")

    # ── RingCentral ────────────────────────────────────────────────
    rc_webhook_verification_token: str = Field(
        ...,
        description="Token set in RC Developer Console. Validated on every inbound push.",
    )
    rc_server_url: str = Field(
        default="https://platform.ringcentral.com",
        description="RingCentral API server URL.",
    )
    rc_client_id: str = Field(
        ...,
        description="RingCentral OAuth2 client ID (app key).",
    )
    rc_client_secret: str = Field(
        ...,
        description="RingCentral OAuth2 client secret.",
    )
    rc_jwt_token: str = Field(
        ...,
        description="RingCentral JWT token for server-to-server auth.",
    )
    rc_webhook_delivery_url: str = Field(
        default="",
        description=(
            "Public URL where RC should deliver webhook notifications. "
            "e.g. https://your-app.ondigitalocean.app/api/v1/rc/webhook  "
            "If empty, auto-subscription is disabled."
        ),
    )
    rc_account_scope: str = Field(
        default="company_wide",
        description=(
            "Subscription scope. "
            "'company_wide' = monitor ALL users/phone numbers in the account. "
            "'single' = monitor only the JWT token owner's extension."
        ),
    )

    # ── Zapier ───────────────────────────────────────────────
    # Legacy single URL — optional now that inbound/outbound are used
    zapier_webhook_url: str = Field(
        default="",
        description="Legacy default URL (optional when inbound/outbound are configured).",
    )
    zapier_inbound_webhook_url: str = Field(
        ...,
        description="Zapier URL for INBOUND SMS. Required.",
    )
    zapier_outbound_webhook_url: str = Field(
        ...,
        description="Zapier URL for OUTBOUND SMS. Required.",
    )

    # ── Retry / Reliability ────────────────────────────────────────
    zapier_max_retries: int = Field(default=3, ge=1, le=10)
    zapier_retry_base_delay_seconds: float = Field(default=1.0, ge=0.1)

    # ── Idempotency Cache ──────────────────────────────────────────
    idempotency_cache_max_size: int = Field(default=10_000, ge=100)
    idempotency_cache_ttl_seconds: int = Field(default=86_400, ge=60)

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {allowed}")
        return upper


    @property
    def is_development(self) -> bool:
        return self.app_env.lower() == "development"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Returns a cached Settings instance.
    Use FastAPI's Depends(get_settings) for dependency injection.
    """
    return Settings()
