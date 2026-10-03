"""Record/replay cache for real providers (§14.1).

    await cache.call(provider="ors", method="matrix", request={...}, fn=live_call)

Key = sha256(provider + method + canonical JSON of `request`), with any `depart_at`
value rounded to the nearest 15 minutes first. Files live in
fixtures/recorded/{provider}/{key}.json and are committed, so the stage demo can
run in replay mode with no network.

    off     always call live
    record  call live, write the response
    replay  return the file if present; on a miss call live, record it, log cache_miss
"""

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from app.logging import get_logger, kv

log = get_logger(__name__)

RECORDED_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "recorded"
NORMALIZE_KEYS = {"depart_at"}
ROUND_TO = timedelta(minutes=15)

CacheMode = Literal["off", "record", "replay"]


def _round_time(value: Any) -> Any:
    """Round a datetime (or ISO string) to the nearest 15 minutes; leave others alone."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if not isinstance(value, datetime):
        return value
    floor = value - (value - value.min.replace(tzinfo=value.tzinfo)) % ROUND_TO
    nearest = floor + ROUND_TO if value - floor >= ROUND_TO / 2 else floor
    return nearest.isoformat()


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _round_time(v) if k in NORMALIZE_KEYS else _normalize(v) for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [_normalize(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def canonical_json(request: Any) -> str:
    return json.dumps(_normalize(request), sort_keys=True, separators=(",", ":"), default=str)


def cache_key(provider: str, method: str, request: Any) -> str:
    return hashlib.sha256((provider + method + canonical_json(request)).encode()).hexdigest()


class RecordReplayCache:
    def __init__(self, mode: CacheMode, root: Path = RECORDED_DIR) -> None:
        self.mode = mode
        self.root = root

    def path_for(self, provider: str, key: str) -> Path:
        return self.root / provider / f"{key}.json"

    async def call(
        self,
        provider: str,
        method: str,
        request: Any,
        fn: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Run `fn` (the live call) unless replay mode has a recording for `request`.

        `fn` must return JSON-serializable data (the raw API response).
        """
        if self.mode == "off":
            return await fn()

        key = cache_key(provider, method, request)
        path = self.path_for(provider, key)
        if self.mode == "replay":
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))["response"]
            log.info(kv("cache_miss", provider=provider, method=method, key=key[:12]))

        response = await fn()
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "provider": provider,
            "method": method,
            "request": json.loads(canonical_json(request)),
            "response": response,
        }
        path.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return response
