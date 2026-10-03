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

    # Gemini
    gemini_api_key: str = ""
    # Stable Flash (ai.google.dev). 3.8/3.7 returned 503 "high demand" on 2026-10-03; 3.5 works.
    gemini_model: str = "gemini-3.5-flash"

    # OpenRouteService
    ors_api_key: str = ""
    ors_base_url: str = "https://api.openrouteservice.org"

    # OpenStreetMap community services
    nominatim_base_url: str = "https://nominatim.openstreetmap.org"
    nominatim_user_agent: str = ""  # required by Nominatim's usage policy
    overpass_url: str = "https://overpass-api.de/api/interpreter"  # scripts/fetch_venues.py only

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
    provider_places: Literal["osm", "mock"] = "mock"
    provider_routing: Literal["ors", "mock"] = "mock"
    provider_finance: Literal["nessie"] = "nessie"

    # Record/replay cache (§14.1)
    cache_mode: Literal["off", "record", "replay"] = "off"

    # Hand-curated venue fixture (§9.3). Tests point this at tests/fixtures/.
    venues_path: str = "fixtures/venues.json"

    # Demo area
    demo_timezone: str = "America/New_York"
    demo_area_label: str = "Ithaca, NY"
    demo_center_lat: float = 42.4440
    demo_center_lng: float = -76.4830

    # Own car
    drive_cost_per_mile_usd: Decimal = Decimal("0.20")
    drive_parking_usd: Decimal = Decimal("3.00")

    # Ride-share: max(min_fare, base + booking + per_mile × mi + per_min × min)
    rideshare_base_usd: Decimal = Decimal("2.50")
    rideshare_booking_usd: Decimal = Decimal("2.50")
    rideshare_per_mile_usd: Decimal = Decimal("1.20")
    rideshare_per_min_usd: Decimal = Decimal("0.30")
    rideshare_min_fare_usd: Decimal = Decimal("8.00")
    rideshare_pickup_wait_min: int = 6

    # Tuning
    optimizer_lambda: float = 0.5
    poll_timeout_sec: int = 300

    # Timeouts (§8)
    llm_timeout_sec: float = 20.0
    http_timeout_sec: float = 10.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
