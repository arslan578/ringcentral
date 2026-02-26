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

    # ── Zapier ─────────────────────────────────────────────────────
    zapier_webhook_url: str = Field(
        ...,
        description="Full Zapier catch-hook HTTPS URL.",
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

    @field_validator("zapier_webhook_url")
    @classmethod
    def validate_https(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("ZAPIER_WEBHOOK_URL must use HTTPS.")
        return v

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
