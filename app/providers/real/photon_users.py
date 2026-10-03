"""UserDirectoryProvider backed by Photon's Spectrum API (spectrum.photon.codes/openapi/json).

- GET  /projects/{projectId}/users/?search=<phone>  → users list
- POST /projects/{projectId}/users/  {"type": "shared", "phoneNumber", "firstName"} → user
Live responses come wrapped: {"succeed": true, "data": {"users": [...], "total": n}}
(checked against the API on 2026-10-03); bare bodies are accepted too.
Auth: HTTP Basic, base64(projectId:projectSecret). A user's `assignedPhoneNumber` is the
iMessage line they text (the dashboard's "TEXTS ON" column).

Not record/replay cached: registering is a write, and replaying it would skip it.
"""

import re
import time

import httpx

from app.logging import get_logger, kv
from app.settings import Settings

log = get_logger(__name__)


def _digits_plus(phone: str) -> str:
    return re.sub(r"[^\d+]", "", phone or "")


def _payload(response: httpx.Response) -> dict:
    """The body without Photon's {"succeed", "data"} envelope. Raises if succeed is false."""
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("unexpected Photon response")
    if body.get("succeed") is False:
        raise ValueError("Photon reported failure")
    inner = body.get("data", body)
    if not isinstance(inner, dict):
        raise ValueError("unexpected Photon response")
    return inner


class PhotonUsers:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.project_id = settings.photon_project_id
        self.secret = settings.photon_project_secret
        self.base = settings.photon_api_url.rstrip("/")
        self.timeout = settings.http_timeout_sec
        self.transport = transport  # tests pass httpx.MockTransport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base,
            auth=(self.project_id, self.secret),  # HTTP Basic
            timeout=self.timeout,
            transport=self.transport,
        )

    async def register(self, phone: str, first_name: str) -> str | None:
        if not self.project_id or not self.secret:
            log.warning(kv("photon_users_not_configured"))
            return None
        start = time.monotonic()
        path = f"/projects/{self.project_id}/users/"
        try:
            async with self._client() as client:
                found = await client.get(path, params={"search": phone})
                found.raise_for_status()
                user = next(
                    (
                        u
                        for u in _payload(found).get("users", [])
                        if _digits_plus(u.get("phoneNumber", "")) == _digits_plus(phone)
                    ),
                    None,
                )
                created = user is None
                if created:
                    body = {"type": "shared", "phoneNumber": phone, "firstName": first_name}
                    response = await client.post(path, json=body)
                    response.raise_for_status()
                    data = _payload(response)
                    user = data.get("user", data)
        except (httpx.HTTPError, ValueError) as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            log.warning(kv("photon_register_failed", error=type(exc).__name__, status=status))
            return None
        assigned = (user or {}).get("assignedPhoneNumber") or None
        log.info(
            kv(
                "provider_call",
                provider="photon",
                method="register_user",
                created=created,
                has_line=assigned is not None,
                latency_ms=round((time.monotonic() - start) * 1000),
            )
        )
        return assigned
