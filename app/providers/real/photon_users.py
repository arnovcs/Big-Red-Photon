"""UserDirectoryProvider backed by Photon.

Two Photon APIs, two credentials:
- Spectrum API (spectrum.photon.codes, HTTP Basic projectId:projectSecret), per
  spectrum.photon.codes/openapi/json:
    GET  /projects/{projectId}/users/?search=<phone>  → users (id, assignedPhoneNumber)
    POST /projects/{projectId}/users/  {"type": "shared", "phoneNumber", "firstName", "email"}
         Creates the user but sends no invite (tested 2026-10-04).
    GET  /users/{userId}/redirect?msg=<text>  (public) → 302 to sms:<their line>&body=<text>
- Dashboard API (app.photon.codes, Bearer account token), the call `photon spectrum users
  add --invite` makes (read from @photon-ai/cli 2.2.0):
    POST /api/projects/{projectId}/spectrum/users
         {"firstName", "lastName", "email", "phoneNumber", "sendInvite": true}
         Creates the user and emails them Photon's onboarding (opt-in) invite. All four
         fields are required (a missing lastName gives a 422, checked live). For a phone
         that's already a user it updates that user instead of adding a duplicate, so it
         also re-sends the invite to someone who never opted in (meta.opt_in not true).
Live responses may come wrapped: {"succeed": true, "data": {...}}; bare bodies work too.

Shared-line recipients must opt in before the bot can text them: the invite, or sending
through the redirect link. Not record/replay cached: these are writes.
"""

import re
import time
from urllib.parse import quote

import httpx

from app.logging import get_logger, kv
from app.models.identity import DirectoryUser
from app.settings import Settings

log = get_logger(__name__)


def _digits_plus(phone: str) -> str:
    return re.sub(r"[^\d+]", "", phone or "")


def _payload(response: httpx.Response) -> dict:
    """The body without Photon's {"succeed", "data"} envelope. Raises if succeed is false."""
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("unexpected Photon response")
    if body.get("succeed") is False or ("error" in body and "user" not in body):
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
        self.token = settings.photon_token
        self.dashboard = settings.photon_dashboard_url.rstrip("/")
        self.timeout = settings.http_timeout_sec
        self.transport = transport  # tests pass httpx.MockTransport

    def _spectrum(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base,
            auth=(self.project_id, self.secret),  # HTTP Basic
            timeout=self.timeout,
            transport=self.transport,
        )

    async def _find(self, client: httpx.AsyncClient, phone: str) -> dict | None:
        found = await client.get(f"/projects/{self.project_id}/users/", params={"search": phone})
        found.raise_for_status()
        return next(
            (
                u
                for u in _payload(found).get("users", [])
                if _digits_plus(u.get("phoneNumber", "")) == _digits_plus(phone)
            ),
            None,
        )

    async def _create_with_invite(
        self, phone: str, first_name: str, email: str | None, last_name: str | None
    ) -> bool:
        """Dashboard API with sendInvite. False if there's no token, a required field is
        missing (it needs first and last name, email, and phone), or it didn't work."""
        if not self.token:
            return False
        if not (first_name and last_name and email):
            log.warning(kv("photon_invite_skipped", reason="missing_fields"))
            return False
        body = {
            "firstName": first_name,
            "lastName": last_name,
            "email": email,
            "phoneNumber": phone,
            "sendInvite": True,
        }
        try:
            async with httpx.AsyncClient(
                base_url=self.dashboard,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=self.timeout,
                transport=self.transport,
            ) as client:
                response = await client.post(
                    f"/api/projects/{self.project_id}/spectrum/users", json=body
                )
                response.raise_for_status()
                _payload(response)
        except (httpx.HTTPError, ValueError) as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            # 401: the token expired. Re-run `npx @photon-ai/cli login` and update PHOTON_TOKEN.
            log.warning(kv("photon_invite_failed", error=type(exc).__name__, status=status))
            return False
        return True

    async def register(
        self,
        phone: str,
        first_name: str,
        email: str | None = None,
        last_name: str | None = None,
    ) -> DirectoryUser | None:
        if not self.project_id or not self.secret:
            log.warning(kv("photon_users_not_configured"))
            return None
        start = time.monotonic()
        created = invited = False
        try:
            async with self._spectrum() as client:
                user = await self._find(client, phone)
                if user is not None and not (user.get("meta") or {}).get("opt_in"):
                    # Registered but never opted in: (re)send the invite.
                    if await self._create_with_invite(phone, first_name, email, last_name):
                        invited = True
                        user = await self._find(client, phone) or user
                if user is None:
                    invited = await self._create_with_invite(phone, first_name, email, last_name)
                    if invited:
                        user = await self._find(client, phone)  # id + assigned line
                    else:  # no token / it failed: create without an invite
                        body = {"type": "shared", "phoneNumber": phone, "firstName": first_name}
                        if last_name:
                            body["lastName"] = last_name
                        if email:
                            body["email"] = email
                        response = await client.post(
                            f"/projects/{self.project_id}/users/", json=body
                        )
                        response.raise_for_status()
                        data = _payload(response)
                        user = data.get("user", data)
                    created = True
        except (httpx.HTTPError, ValueError) as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            log.warning(kv("photon_register_failed", error=type(exc).__name__, status=status))
            return None
        user = user or {}
        assigned = user.get("assignedPhoneNumber") or None
        log.info(
            kv(
                "provider_call",
                provider="photon",
                method="register_user",
                created=created,
                invited=invited,
                has_line=assigned is not None,
                latency_ms=round((time.monotonic() - start) * 1000),
            )
        )
        if not user.get("id"):
            log.warning(kv("photon_register_no_user_id"))
            return None
        return DirectoryUser(user_id=str(user["id"]), line=assigned)

    def opt_in_link(self, user_id: str, message: str) -> str | None:
        """Photon's public per-user link: opens Messages to their line, message typed in."""
        return f"{self.base}/users/{quote(user_id, safe='')}/redirect?msg={quote(message)}"
