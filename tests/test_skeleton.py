"""Stage 0 smoke tests: models import, app boots, DB tables exist."""

from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import inspect

from app.db import session as db
from app.models.candidates import Candidate, Uncertain
from app.models.outbound import GroupPlanOption, GroupSafeMessage
from app.models.private import LatLng
from app.models.routing import Mode, RouteEstimate
from app.settings import get_settings


def test_models_construct() -> None:
    cand = Candidate(
        candidate_id="fx:koko",
        name="Koko",
        category="food",
        cuisines=["korean"],
        location=LatLng(lat=42.44, lng=-76.48),
        address="123 College Ave",
        est_cost_pp=Uncertain[Decimal](value=Decimal("25"), status="estimated", source="fixture"),
        typical_duration_min=60,
        source="fixture",
    )
    est = RouteEstimate(
        origin_pid="p1",
        candidate_id=cand.candidate_id,
        mode=Mode.WALK,
        duration_min=12,
        distance_mi=0.6,
        walk_min=12,
        fare_usd=Uncertain[Decimal](value=Decimal("0"), status="known", source="config"),
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
                "votes",
                "processed_messages",
            }
    finally:
        get_settings.cache_clear()
