"""FinanceProvider backed by the Capital One Nessie sandbox (§9.5).

Field names were checked against Nessie's official SDK (github.com/nessieisreal):
accounts have `type`/`balance`, bills `payment_date`/`payment_amount`, purchases
`merchant_id`/`purchase_date`/`amount`. Only `app/private/` should call this.
"""

import logging
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt

from app.logging import get_logger, kv
from app.models.private import FinancialSnapshot
from app.settings import Settings

log = get_logger(__name__)

# httpx logs every request URL at INFO, and Nessie URLs carry the API key and
# customer/account ids, which must never be logged at INFO (§11, CLAUDE.md rule 7).
logging.getLogger("httpx").setLevel(logging.WARNING)

BILLS_WINDOW_DAYS = 14
OUTINGS_WINDOW_DAYS = 60
# A purchase counts as an "outing" if its merchant has one of these categories.
OUTING_CATEGORIES = {
    "food",
    "restaurant",
    "dining",
    "bar",
    "cafe",
    "entertainment",
    "night_club",
    "movie_theater",
    "bowling_alley",
}


def _is_retryable(exc: BaseException) -> bool:
    """Retry network errors and 5xx once. GETs are safe to repeat."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _categories(merchant: dict) -> set[str]:
    """Nessie merchants carry `category` as a string or a list of strings."""
    raw = merchant.get("category") or []
    values = [raw] if isinstance(raw, str) else raw
    return {str(v).strip().lower() for v in values}


class NessieFinance:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.nessie_base_url.rstrip("/")
        self.api_key = settings.nessie_api_key
        self.timeout = settings.http_timeout_sec

    async def _get(self, client: httpx.AsyncClient, path: str) -> Any:
        start = time.monotonic()
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(2),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        ):
            with attempt:
                response = await client.get(f"{self.base_url}{path}", params={"key": self.api_key})
                response.raise_for_status()
        # The path holds Nessie ids, so log only its first segment.
        log.info(
            kv(
                "provider_call",
                provider="nessie",
                method=path.strip("/").split("/")[0],
                status=response.status_code,
                latency_ms=round((time.monotonic() - start) * 1000),
            )
        )
        return response.json()

    async def get_customer(self, customer_id: str) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            return await self._get(client, f"/customers/{customer_id}")

    async def get_financial_snapshot(self, customer_id: str) -> FinancialSnapshot:
        today = datetime.now(UTC).date()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            accounts = await self._get(client, f"/customers/{customer_id}/accounts")
            checking = [a for a in accounts if str(a.get("type", "")).lower() == "checking"]

            balance = sum((Decimal(str(a.get("balance", 0))) for a in checking), Decimal(0))
            bills_due = Decimal(0)
            outings: list[Decimal] = []
            merchants: dict[str, set[str]] = {}

            for account in checking:
                account_id = account["_id"]
                for bill in await self._get(client, f"/accounts/{account_id}/bills"):
                    if str(bill.get("status", "")).lower() in {"completed", "cancelled"}:
                        continue  # already paid (so already out of the balance) or void
                    due = _parse_date(bill.get("payment_date"))
                    if due and today <= due <= today + timedelta(days=BILLS_WINDOW_DAYS):
                        bills_due += Decimal(str(bill.get("payment_amount", 0)))

                for purchase in await self._get(client, f"/accounts/{account_id}/purchases"):
                    bought = _parse_date(purchase.get("purchase_date"))
                    if not bought or bought < today - timedelta(days=OUTINGS_WINDOW_DAYS):
                        continue
                    merchant_id = purchase.get("merchant_id")
                    if not merchant_id:
                        continue
                    if merchant_id not in merchants:
                        merchant = await self._get(client, f"/merchants/{merchant_id}")
                        merchants[merchant_id] = _categories(merchant)
                    if merchants[merchant_id] & OUTING_CATEGORIES:
                        outings.append(Decimal(str(purchase.get("amount", 0))))

        return FinancialSnapshot(
            checking_balance=balance,
            upcoming_bills_14d=bills_due,
            recent_outing_amounts=outings,
        )
