"""Application configuration.

Every value comes from the environment. Nothing here has a production-safe
default that would let a misconfigured deployment start up pretending to be
secure - the dev-only auth secret is refused outright when ENVIRONMENT is
production.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "get_settings"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = Field(default="development", alias="ENVIRONMENT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # -- database ----------------------------------------------------------
    database_url: str = Field(default="sqlite+pysqlite:///./recon.sqlite3", alias="DATABASE_URL")
    database_echo: bool = Field(default=False, alias="DATABASE_ECHO")
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    # -- object storage ----------------------------------------------------
    object_storage_endpoint: str = Field(default="", alias="OBJECT_STORAGE_ENDPOINT")
    object_storage_bucket: str = Field(default="recon-local", alias="OBJECT_STORAGE_BUCKET")
    object_storage_access_key: str = Field(default="", alias="OBJECT_STORAGE_ACCESS_KEY")
    object_storage_secret_key: str = Field(default="", alias="OBJECT_STORAGE_SECRET_KEY")
    object_storage_region: str = Field(default="us-east-1", alias="OBJECT_STORAGE_REGION")
    local_storage_path: str = Field(default="./var/storage", alias="LOCAL_STORAGE_PATH")

    # -- authentication ----------------------------------------------------
    # Identity is bought, not built (spec section 4). These point at whichever
    # IdP the deployment uses; no user store lives in this application.
    auth_issuer: str = Field(default="", alias="AUTH_ISSUER")
    auth_audience: str = Field(default="", alias="AUTH_AUDIENCE")
    auth_jwks_url: str = Field(default="", alias="AUTH_JWKS_URL")
    auth_dev_secret: str = Field(default="", alias="AUTH_DEV_SECRET")

    # -- AI ----------------------------------------------------------------
    ai_enabled: bool = Field(default=False, alias="AI_ENABLED")
    ai_provider: str = Field(default="deterministic", alias="AI_PROVIDER")
    ai_model: str = Field(default="", alias="AI_MODEL")
    ai_api_key: str = Field(default="", alias="AI_API_KEY")
    ai_latency_budget_ms: int = Field(default=8000, alias="AI_LATENCY_BUDGET_MS")

    # -- connectors --------------------------------------------------------
    stripe_client_id: str = Field(default="", alias="STRIPE_CLIENT_ID")
    stripe_client_secret: str = Field(default="", alias="STRIPE_CLIENT_SECRET")
    stripe_webhook_secret: str = Field(default="", alias="STRIPE_WEBHOOK_SECRET")

    # -- secrets -----------------------------------------------------------
    kms_key_id: str = Field(default="", alias="KMS_KEY_ID")
    credential_encryption_key: str = Field(default="", alias="CREDENTIAL_ENCRYPTION_KEY")

    # -- limits ------------------------------------------------------------
    max_upload_bytes: int = Field(default=200 * 1024 * 1024, alias="MAX_UPLOAD_BYTES")
    rate_limit_per_minute: int = Field(default=600, alias="RATE_LIMIT_PER_MINUTE")

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @model_validator(mode="after")
    def _production_safety(self) -> Settings:
        if not self.is_production:
            return self

        problems: list[str] = []
        if self.auth_dev_secret:
            problems.append(
                "AUTH_DEV_SECRET is set; the local development signer must never "
                "be enabled in production"
            )
        if not (self.auth_issuer and self.auth_audience and self.auth_jwks_url):
            problems.append(
                "AUTH_ISSUER, AUTH_AUDIENCE and AUTH_JWKS_URL are all required in production"
            )
        if self.is_sqlite:
            problems.append("SQLite is not a supported production database")
        if not self.credential_encryption_key:
            problems.append(
                "CREDENTIAL_ENCRYPTION_KEY is required: connector secrets are "
                "never stored unencrypted"
            )
        if problems:
            raise ValueError("Refusing to start in production: " + "; ".join(problems) + ".")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
