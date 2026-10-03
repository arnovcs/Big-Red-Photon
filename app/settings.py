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
    # 3.8/3.7 returned 503 "high demand" on 2026-10-03; 3.5 Flash hit its free-tier
    # 20 requests/day. Flash-Lite has its own per-model quota.
    gemini_model: str = "gemini-3.5-flash-lite"

    # OpenRouteService
    ors_api_key: str = ""
    ors_base_url: str = "https://api.openrouteservice.org"

    # Google Places API (New): worldwide venues (PROVIDER_PLACES=google)
    google_places_api_key: str = ""
    # Routes API; empty = use the Places key (same Google Cloud project).
    google_routes_api_key: str = ""

    # Capital One Nessie
    nessie_api_key: str = ""
    nessie_base_url: str = "http://api.nessieisreal.com"

    # Photon Spectrum project (same values as bridge/.env). The backend uses them only to
    # register web signups as project users and read the line each one should text.
    photon_project_id: str = ""
    photon_project_secret: str = ""
    photon_api_url: str = "https://spectrum.photon.codes"

    # Bridge <-> backend
    bridge_url: str = "http://localhost:3001"
    backend_url: str = "http://localhost:8000"

    # Database
    database_url: str = "sqlite+aiosqlite:///./app.db"

    # Provider selection
    provider_messaging: Literal["photon", "sim"] = "sim"
    # All place data comes from Google Places (no local venue/landmark files).
    provider_places: Literal["google"] = "google"
    # google = Google Routes for every mode (live traffic for driving), ORS as fallback.
    provider_routing: Literal["google", "ors", "mock"] = "mock"
    provider_finance: Literal["nessie"] = "nessie"

    # Record/replay cache (§14.1)
    cache_mode: Literal["off", "record", "replay"] = "off"

    # Demo area
    demo_timezone: str = "America/New_York"
    demo_area_label: str = "Ithaca, NY"
    # Typed places are searched near here (a 10 km bias, not a limit): central Ithaca.
    demo_center_lat: float = 42.44
    demo_center_lng: float = -76.50

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
    # A Find My location older than this counts as "not sharing" (they likely stopped).
    shared_location_max_age_min: int = 120
    # "heads up, leave in N" DM before each person's leave time (0 = off).
    leave_nudge_min: int = 5

    # Web signup (app/web/). Who signed up is visible in Photon's dashboard (Users tab).
    app_name: str = "Huddle"  # product name shown on the web pages
    bot_phone_number: str = ""  # the bot's iMessage number, for the "text to finish" link
    # Free shared Photon lines can't text a number first. Only if true, nudge new signups
    # by text right after the form is submitted.
    photon_can_initiate: bool = False
    signup_token_ttl_hours: int = 24


@lru_cache
def get_settings() -> Settings:
    return Settings()
