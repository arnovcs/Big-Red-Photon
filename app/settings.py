"""Application settings, loaded from environment variables and `.env`."""

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    # xAI Grok
    xai_api_key: str = ""
    xai_model: str = ""  # fill from current xAI docs
    grok_timeout_sec: float = 20.0

    # Google Places + Routes
    google_maps_api_key: str = ""

    # Capital One Nessie
    nessie_api_key: str = ""
    nessie_base_url: str = "http://api.nessieisreal.com"

    # Bridge <-> backend
    bridge_url: str = "http://localhost:3001"
    backend_url: str = "http://localhost:8000"

    # Database
    database_url: str = "sqlite+aiosqlite:///./app.db"

    # Provider selection
    provider_messaging: Literal["photon", "sim"] = "sim"
    provider_places: Literal["google", "mock"] = "mock"
    provider_routing: Literal["google", "mock"] = "mock"
    provider_finance: Literal["nessie"] = "nessie"

    # Record/replay cache (§14.1)
    cache_mode: Literal["off", "record", "replay"] = "off"

    # Demo area
    demo_timezone: str = "America/New_York"
    demo_area_label: str = "Ithaca, NY"
    demo_center_lat: float = 42.4440
    demo_center_lng: float = -76.4830

    # Costs
    transit_fare_usd: Decimal = Decimal("1.50")
    drive_cost_per_mile_usd: Decimal = Decimal("0.20")
    drive_parking_usd: Decimal = Decimal("3.00")

    # Tuning
    optimizer_lambda: float = 0.5
    poll_timeout_sec: int = 300

    # Default timeout for real providers (§8)
    http_timeout_sec: float = 10.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
