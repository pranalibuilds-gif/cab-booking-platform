from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = Field(
        default="postgresql+psycopg://postgres:postgres@127.0.0.1:5433/cab_booking",
        min_length=1,
    )
    jwt_secret_key: str = Field(min_length=32)
    access_token_minutes: int = Field(default=15, ge=1, le=60)
    refresh_token_days: int = Field(default=14, ge=1, le=90)
    refresh_cookie_secure: bool = False
    cors_origins: str = "http://localhost:3000"
    driver_location_freshness_seconds: int = Field(default=30, ge=1, le=300)
    driver_search_radius_meters: int = Field(default=10_000, ge=100, le=100_000)
    ride_offer_timeout_seconds: int = Field(default=20, ge=1, le=300)
    ride_max_offer_attempts: int = Field(default=5, ge=1, le=5)
    ride_request_lifetime_seconds: int = Field(default=300, ge=30, le=3600)
    fare_base_paise: int = Field(default=5_000, ge=0)
    fare_per_km_paise: int = Field(default=1_500, ge=0)
    environment: Literal["development", "test", "production"] = "development"

    @field_validator("jwt_secret_key")
    @classmethod
    def reject_placeholder_secret(cls, value: str) -> str:
        if value.startswith("generate-") or value.startswith("replace-"):
            raise ValueError("Set JWT_SECRET_KEY to a generated secret before starting the API")
        return value

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


def get_settings() -> Settings:
    return Settings()
