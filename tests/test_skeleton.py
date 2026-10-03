"""Stage 0 smoke tests: models import, app boots, DB tables exist."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import inspect

from app.db import session as db
from app.models.candidates import Candidate, EventFinding, Uncertain
from app.models.identity import OnboardingState
from app.models.outbound import GroupPlanOption, GroupSafeMessage
from app.models.private import LatLng, TravelModes
from app.models.routing import Mode, RouteEstimate, RouteStep
from app.settings import Settings, get_settings


def test_models_construct() -> None:
    cand = Candidate(
        candidate_id="osm:node/123",
        name="Koko",
        category="food",
        cuisines=["korean"],
        location=LatLng(lat=42.44, lng=-76.48),
        address="123 College Ave",
        est_cost_pp=Uncertain[Decimal](
            value=Decimal("25"), status="estimated", source="hand_entered"
        ),
        typical_duration_min=60,
        source="google",
    )
    est = RouteEstimate(
        origin_pid="p1",
        candidate_id=cand.candidate_id,
        mode=Mode.WALK,
        duration_min=12,
        distance_mi=0.6,
        walk_min=12,
        fare_usd=Uncertain[Decimal](value=Decimal("0"), status="known", source="ors"),
        source="mock",
    )
    msg = GroupSafeMessage(
        text="hi",
        poll=[
            GroupPlanOption(
                label="A",
                title="Koko — Korean",
                max_travel_min=12,
                walking_level="low",
                price_tier="$$",
                arrival_window_min=0,
                blurb="Close to everyone.",
            )
        ],
    )
    assert est.mode == "walk"
    assert msg.poll is not None and msg.poll[0].label == "A"
    assert datetime.now(UTC).tzinfo is not None


def test_v2_modes_and_defaults() -> None:
    assert {m.value for m in Mode} == {"walk", "bike", "drive", "rideshare"}
    assert TravelModes() == TravelModes(walk=True, bike=False, drive=False, rideshare=True)
    assert OnboardingState.AWAITING_MODES == "awaiting_modes"
    step = RouteStep(
        mode="bike", instruction="Turn left onto College Ave", duration_min=2, distance_mi=0.3
    )
    assert step.distance_mi == 0.3

    event = EventFinding(
        title="Show", venue_name="State Theatre", starts_at=None, source_url="https://x"
    )
    assert event.est_price_usd is None
    with pytest.raises(ValidationError):
        EventFinding(title="Show", venue_name="State Theatre", source_url="https://x")  # type: ignore[call-arg]


def test_settings_v2_defaults(monkeypatch) -> None:
    monkeypatch.delenv("PROVIDER_PLACES", raising=False)
    monkeypatch.delenv("PROVIDER_ROUTING", raising=False)
    settings = Settings(_env_file=None)
    assert settings.provider_places == "google"  # the only place data source
    assert settings.provider_routing == "mock"
    assert settings.rideshare_min_fare_usd == Decimal("8.00")
    assert settings.rideshare_pickup_wait_min == 6
    assert settings.llm_timeout_sec == 20
    assert settings.http_timeout_sec == 10


def test_app_boots_and_creates_tables(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    get_settings.cache_clear()
    try:
        from app.main import app

        with TestClient(app) as client:
            resp = client.get("/health")
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok"}

            async def table_names() -> list[str]:
                async with db.get_engine().connect() as conn:
                    return await conn.run_sync(lambda c: inspect(c).get_table_names())

            names = client.portal.call(table_names)
            assert set(names) == {
                "users",
                "groups",
                "group_members",
                "private_profiles",
                "sessions",
                "session_messages",
                "session_ready",
                "votes",
                "processed_messages",
                "pending_signups",
            }
    finally:
        get_settings.cache_clear()
