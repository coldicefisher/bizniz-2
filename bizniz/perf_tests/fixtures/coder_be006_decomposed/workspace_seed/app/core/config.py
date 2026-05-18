from pydantic_settings import BaseSettings
from functools import lru_cache
from typing import Optional


class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql+asyncpg://app:app_dev@db:5432/app"

    # FusionAuth
    fusionauth_url: str = "http://fusionauth:9011"
    fusionauth_api_key: str = ""
    fusionauth_application_id: str = ""
    fusionauth_issuer: Optional[str] = None  # defaults to fusionauth_url
    fusionauth_google_idp_id: Optional[str] = None

    # App
    environment: str = "development"
    log_level: str = "debug"
    app_name: str = "App API"
    api_v1_prefix: str = "/api/v1"
    app_base_url: str = "http://localhost"

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
