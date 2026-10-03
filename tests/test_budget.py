"""Budget estimator (§12), Nessie snapshot parsing, and bank-code linking (Stage 5)."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.db import session as db
from app.models.private import FinancialSnapshot, LatLng, TravelModes
from app.private import vault
from app.private.budget import estimate_limit
from app.providers.real.nessie import NessieFinance
from app.settings import Settings

PERSONAS = json.loads(vault.PERSONAS_PATH.read_text(encoding="utf-8"))
MERCHANT_CATEGORY = {m["key"]: m["category"] for m in PERSONAS["merchants"]}
OUTING_CATEGORIES = {"restaurant", "entertainment"}


def snapshot_for(persona: dict) -> FinancialSnapshot:
    """What Nessie should report for a freshly seeded persona."""
    return FinancialSnapshot(
        checking_balance=Decimal(persona["checking_balance"]),
        upcoming_bills_14d=sum((Decimal(b["amount"]) for b in persona["bills"]), Decimal(0)),
        recent_outing_amounts=[
            Decimal(p["amount"])
            for p in persona["purchases"]
            if MERCHANT_CATEGORY[p["merchant"]] in OUTING_CATEGORIES
        ],
    )


@pytest.mark.parametrize("persona", PERSONAS["personas"], ids=lambda p: p["name"])
def test_persona_limits_match_expected(persona: dict) -> None:
    assert estimate_limit(snapshot_for(persona)) == persona["expected_limit"]


def test_zero_balance_gives_minimum() -> None:
    snapshot = FinancialSnapshot(
        checking_balance=Decimal(0), upcoming_bills_14d=Decimal(0), recent_outing_amounts=[]
    )
    assert estimate_limit(snapshot) == 10


def test_no_purchases_uses_default_typical_outing() -> None:
    # Plenty of room, so the 1.25 × default 20 = 25 term is the binding one.
    snapshot = FinancialSnapshot(
        checking_balance=Decimal(5000), upcoming_bills_14d=Decimal(0), recent_outing_amounts=[]
    )
    assert estimate_limit(snapshot) == 25


def test_limit_capped_at_maximum() -> None:
    snapshot = FinancialSnapshot(
        checking_balance=Decimal(100000),
        upcoming_bills_14d=Decimal(0),
        recent_outing_amounts=[Decimal(500)],
    )
    assert estimate_limit(snapshot) == 150


async def test_snapshot_counts_only_checking_upcoming_bills_and_outings(monkeypatch) -> None:
    today = datetime.now(UTC).date()
    days = lambda n: (today + timedelta(days=n)).isoformat()  # noqa: E731
    responses = {
        "/customers/c1/accounts": [
            {"_id": "a1", "type": "Checking", "balance": 1000},
            {"_id": "a2", "type": "Savings", "balance": 9999},
        ],
        "/accounts/a1/bills": [
            {"payment_date": days(5), "payment_amount": 100, "status": "pending"},
            {"payment_date": days(30), "payment_amount": 999, "status": "pending"},  # too far
            {"payment_date": days(3), "payment_amount": 999, "status": "completed"},  # paid
        ],
        "/accounts/a1/purchases": [
            {"merchant_id": "food", "purchase_date": days(-10), "amount": 20},
            {"merchant_id": "fun", "purchase_date": days(-20), "amount": 30},
            {"merchant_id": "groc", "purchase_date": days(-5), "amount": 120},  # not an outing
            {"merchant_id": "food", "purchase_date": days(-90), "amount": 999},  # too old
        ],
        "/merchants/food": {"category": ["restaurant"]},
        "/merchants/fun": {"category": "entertainment"},
        "/merchants/groc": {"category": ["grocery"]},
    }

    async def fake_get(self, client, path):
        return responses[path]

    monkeypatch.setattr(NessieFinance, "_get", fake_get)
    finance = NessieFinance(Settings(_env_file=None, nessie_api_key="test"))
    snapshot = await finance.get_financial_snapshot("c1")

    assert snapshot.checking_balance == 1000
    assert snapshot.upcoming_bills_14d == 100
    assert sorted(snapshot.recent_outing_amounts) == [20, 30]


class FakeFinance:
    def __init__(self, snapshot: FinancialSnapshot | None) -> None:
        self.snapshot = snapshot

    async def get_customer(self, customer_id: str) -> dict:
        return {}

    async def get_financial_snapshot(self, customer_id: str) -> FinancialSnapshot:
        if self.snapshot is None:
            raise ConnectionError("nessie down")
        return self.snapshot


@pytest.fixture
async def database(tmp_path, monkeypatch):
    db.configure(f"sqlite+aiosqlite:///{tmp_path / 'budget.db'}")
    await db.init_db()
    # Pretend MAYA1 has been seeded so the live path is used.
    personas = {p["bank_code"]: dict(p) for p in PERSONAS["personas"]}
    personas["MAYA1"]["nessie_customer_id"] = "seeded-maya"
    monkeypatch.setattr(vault, "_personas_by_code", lambda: personas)
    yield
    await db.dispose()


async def _link(code: str, finance) -> tuple[uuid.UUID, Decimal | None]:
    user_id = uuid.uuid4()
    async with db.session_factory()() as s:
        limit = await vault.link_customer(s, user_id, code, finance)
        await s.commit()
    return user_id, limit


async def test_link_uses_live_nessie_data(database) -> None:
    live = FinancialSnapshot(
        checking_balance=Decimal(5000), upcoming_bills_14d=Decimal(0), recent_outing_amounts=[]
    )
    _, limit = await _link("maya1", FakeFinance(live))
    assert limit == 25  # from the live snapshot, not the fixture's expected_limit


async def test_link_falls_back_when_nessie_is_down(database) -> None:
    _, limit = await _link("MAYA1", FakeFinance(None))
    assert limit == 30


async def test_unknown_bank_code_is_rejected(database) -> None:
    _, limit = await _link("NOPE1", FakeFinance(None))
    assert limit is None


async def test_override_wins(database) -> None:
    user_id, limit = await _link("SAM1", None)
    assert limit == 50
    async with db.session_factory()() as s:
        await vault.set_limit(s, user_id, Decimal(80), "user_override")
        await vault.set_origin(s, user_id, LatLng(lat=42.44, lng=-76.48), "Olin Library")
        await vault.set_modes(s, user_id, TravelModes())
        await s.commit()
        constraints = await vault.constraints_for(s, {"p1": user_id})
    assert constraints["p1"].spend_limit_usd == 80
