"""Seed the Nessie sandbox with the demo personas (§12.3). Safe to re-run.

    uv run python scripts/seed_nessie.py

Creates merchants, then per persona: customer → checking account → bills (due in
5 days) → purchases (spread over the last few weeks). Writes the created ids back to
fixtures/personas.json; anything that already has an id is reused, not recreated.
Finally reads each persona back through the real FinanceProvider and checks the
estimated limit against `expected_limit`.
"""

import asyncio
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.private.budget import estimate_limit  # noqa: E402
from app.providers.real.nessie import NessieFinance  # noqa: E402
from app.settings import get_settings  # noqa: E402

PERSONAS_PATH = ROOT / "fixtures" / "personas.json"
ITHACA_ADDRESS = {
    "street_number": "130",
    "street_name": "College Ave",
    "city": "Ithaca",
    "state": "NY",
    "zip": "14850",
}
ITHACA_GEOCODE = {"lat": 42.4430, "lng": -76.4850}
BILL_DUE_IN_DAYS = 5


class Nessie:
    def __init__(self, base_url: str, api_key: str, timeout: float) -> None:
        self.client = httpx.Client(base_url=base_url, params={"key": api_key}, timeout=timeout)

    def exists(self, path: str) -> bool:
        return self.client.get(path).status_code == 200

    def create(self, path: str, body: dict) -> str:
        """POST and return the new object's id. Exits with Nessie's message on failure."""
        response = self.client.post(path, json=body)
        if response.status_code not in (200, 201):
            sys.exit(f"Nessie rejected POST {path.split('/')[1]}: {response.text[:300]}")
        created = response.json().get("objectCreated") or {}
        if "_id" not in created:
            sys.exit(f"Unexpected Nessie response for {path.split('/')[1]}: {response.text[:300]}")
        return created["_id"]


def seed_merchant(nessie: Nessie, merchant: dict) -> None:
    existing = merchant["nessie_merchant_id"]
    if existing and nessie.exists(f"/merchants/{existing}"):
        print(f"  merchant {merchant['name']}: already seeded")
        return
    body = {
        "name": merchant["name"],
        "category": [merchant["category"]],
        "address": ITHACA_ADDRESS,
        "geocode": ITHACA_GEOCODE,
    }
    response = nessie.client.post("/merchants", json=body)
    if response.status_code == 400:  # some Nessie versions want category as a string
        body["category"] = merchant["category"]
        response = nessie.client.post("/merchants", json=body)
    if response.status_code not in (200, 201):
        sys.exit(f"Nessie rejected POST merchants: {response.text[:300]}")
    merchant["nessie_merchant_id"] = response.json()["objectCreated"]["_id"]
    print(f"  merchant {merchant['name']}: created")


def seed_persona(nessie: Nessie, persona: dict, merchant_ids: dict[str, str]) -> None:
    existing = persona["nessie_customer_id"]
    if existing and nessie.exists(f"/customers/{existing}"):
        print(f"  {persona['name']}: already seeded")
        return
    today = date.today()
    customer_id = nessie.create(
        "/customers",
        {"first_name": persona["name"], "last_name": "Demo", "address": ITHACA_ADDRESS},
    )
    account_id = nessie.create(
        f"/customers/{customer_id}/accounts",
        {
            "type": "Checking",
            "nickname": "Checking",
            "rewards": 0,
            "balance": persona["checking_balance"],
        },
    )
    due = today + timedelta(days=BILL_DUE_IN_DAYS)
    for bill in persona["bills"]:
        nessie.create(
            f"/accounts/{account_id}/bills",
            {
                "status": "pending",
                "payee": bill["payee"],
                "nickname": bill["payee"],
                "payment_date": due.isoformat(),
                "recurring_date": due.day,
                "payment_amount": bill["amount"],
            },
        )
    for i, purchase in enumerate(persona["purchases"]):
        nessie.create(
            f"/accounts/{account_id}/purchases",
            {
                "merchant_id": merchant_ids[purchase["merchant"]],
                "medium": "balance",
                "purchase_date": (today - timedelta(days=7 * (i + 1))).isoformat(),
                "amount": purchase["amount"],
                "status": "pending",
                "description": "demo purchase",
            },
        )
    persona["nessie_customer_id"] = customer_id
    print(f"  {persona['name']}: created customer, account, bills, purchases")


async def verify(personas: list[dict]) -> bool:
    finance = NessieFinance(get_settings())
    ok = True
    for persona in personas:
        snapshot = await finance.get_financial_snapshot(persona["nessie_customer_id"])
        limit = estimate_limit(snapshot)
        match = limit == persona["expected_limit"]
        ok = ok and match
        status = "OK" if match else f"MISMATCH (expected {persona['expected_limit']})"
        print(f"  {persona['name']} ({persona['bank_code']}): limit {limit} {status}")
    return ok


def main() -> None:
    settings = get_settings()
    if not settings.nessie_api_key:
        sys.exit("NESSIE_API_KEY is not set. Add it to .env in the repo root.")
    data = json.loads(PERSONAS_PATH.read_text(encoding="utf-8"))
    nessie = Nessie(settings.nessie_base_url, settings.nessie_api_key, settings.http_timeout_sec)
    try:
        print("Merchants:")
        for merchant in data["merchants"]:
            seed_merchant(nessie, merchant)
        merchant_ids = {m["key"]: m["nessie_merchant_id"] for m in data["merchants"]}
        print("Personas:")
        for persona in data["personas"]:
            seed_persona(nessie, persona, merchant_ids)
    finally:
        # Save whatever was created so a re-run after an error doesn't duplicate it.
        PERSONAS_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        nessie.client.close()
    print("Verifying limits through the real FinanceProvider:")
    if not asyncio.run(verify(data["personas"])):
        sys.exit("Some limits don't match. Adjust amounts in fixtures/personas.json (§12.3).")
    print("Done. Bank codes MAYA1, SAM1, JORDAN1 now link to live Nessie data.")


if __name__ == "__main__":
    main()
