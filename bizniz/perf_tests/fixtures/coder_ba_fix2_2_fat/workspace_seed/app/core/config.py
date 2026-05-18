from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings sourced from environment variables (and optional .env file).

    Environment variables map to field names case-insensitively. The six
    FusionAuth fields below are the canonical reference for auth-related
    configuration consumed by signup, login, and JWT validation flows.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    # Database
    database_url: str = "postgresql+asyncpg://app:app_dev@db:5432/app"

    # FusionAuth — canonical six fields (see AUTH_CONTRACT.md)
    fusionauth_url: str = "http://auth:9011"
    fusionauth_application_id: str
    fusionauth_tenant_id: str
    fusionauth_issuer: str = "acme.com"
    fusionauth_api_key: str
    jwt_leeway_seconds: int = 60

    # Optional extras preserved from the skeleton
    fusionauth_google_idp_id: Optional[str] = None

    # App
    environment: str = "development"
    log_level: str = "debug"
    app_name: str = "App API"
    api_v1_prefix: str = "/api/v1"
    app_base_url: str = "http://localhost"


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance built from process environment."""
    return Settings()


settings: Settings = get_settings()
"""Module-level Settings singleton — import with ``from app.core.config import settings``."""
