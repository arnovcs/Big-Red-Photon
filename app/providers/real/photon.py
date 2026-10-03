"""MessagingProvider backed by the Photon bridge (§9.1). v3: everything is a DM.

Both methods call the bridge's `POST /send_dm`. Sends are not record/replay cached:
replaying a send would mean a message never reaches the phone.
"""

import time
from datetime import UTC, datetime, timedelta

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
)

from app.logging import get_logger, kv, mask_handle
from app.models.outbound import GroupSafeMessage, PrivateMessage
from app.models.private import LatLng
from app.settings import Settings

log = get_logger(__name__)

LOCATION_TIMEOUT_SEC = 25.0


def _is_retryable(exc: BaseException) -> bool:
    """Retry when the bridge was never reached, or it reported a server error.

    Read timeouts are not retried: the bridge may already have sent the message,
    and a retry would double-text the user.
    """
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status >= 500 and status != 501  # 501 = unsupported; retrying won't help
    return False


class PhotonMessaging:
    def __init__(self, settings: Settings) -> None:
        self.bridge_url = settings.bridge_url.rstrip("/")
        self.timeout = settings.http_timeout_sec
        self.max_location_age = timedelta(minutes=settings.shared_location_max_age_min)

    async def send_group(self, handles: list[str], msg: GroupSafeMessage) -> None:
        # msg.text already contains the rendered poll options (copy.poll_message).
        for handle in handles:
            try:
                await self._send_dm(handle, msg.text, msg.effect)
            except Exception:
                # One member's failure must not stop the others from getting the message.
                log.exception(kv("photon_send_group_member_failed", handle=mask_handle(handle)))

    async def send_private(self, handle: str, msg: PrivateMessage) -> None:
        await self._send_dm(handle, msg.text)

    async def request_location(self, handle: str) -> bool:
        """Sends Apple's Find My "share your location" card. Best effort: never raises."""
        try:
            response = await self._post("/request_location", {"handle": handle})
            return response.status_code == 200 and response.json().get("status") == "sent"
        except Exception:
            log.warning(kv("photon_request_location_failed", handle=mask_handle(handle)))
            return False

    async def shared_location(self, handle: str) -> LatLng | None:
        """The person's Find My location, if shared with the bot. Never logs coordinates."""
        try:
            # Photon's Find My lookups can take 15+ s; a short timeout looked like "not
            # sharing" and sent the share card to someone already sharing.
            response = await self._post("/location", {"handle": handle}, LOCATION_TIMEOUT_SEC)
            if response.status_code != 200:
                return None
            data = response.json()
            # Judge freshness by the timestamp: Photon labels even seconds-old shares
            # "legacy". Without a timestamp, "legacy" is all we have to go on.
            at = data.get("at")
            if self._is_stale(at) or (not at and data.get("type") == "legacy"):
                return None  # an old snapshot: they've probably stopped sharing
            return LatLng(lat=float(data["lat"]), lng=float(data["lng"]))
        except Exception:
            log.warning(kv("photon_shared_location_failed", handle=mask_handle(handle)))
            return None

    def _is_stale(self, at: str | None) -> bool:
        """True if the snapshot's timestamp is older than the max age. No or unreadable
        timestamp → trust it (Photon says the time "may be absent")."""
        if not at:
            return False
        try:
            taken = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
        except ValueError:
            return False
        if taken.tzinfo is None:
            taken = taken.replace(tzinfo=UTC)
        return datetime.now(UTC) - taken > self.max_location_age

    async def _post(self, path: str, body: dict, wait_sec: float | None = None) -> httpx.Response:
        async with httpx.AsyncClient(timeout=wait_sec or self.timeout) as client:
            return await client.post(f"{self.bridge_url}{path}", json=body)

    async def react(self, handle: str, message_id: str, emoji: str) -> bool:
        try:
            response = await self._post(
                "/react", {"handle": handle, "message_id": message_id, "emoji": emoji}
            )
            return response.status_code == 200
        except Exception:
            log.warning(kv("photon_react_failed", handle=mask_handle(handle)))
            return False

    async def typing(self, handle: str, on: bool) -> None:
        try:
            await self._post("/typing", {"handle": handle, "state": "start" if on else "stop"})
        except Exception:
            log.warning(kv("photon_typing_failed", handle=mask_handle(handle)))

    async def send_link(self, handle: str, url: str) -> bool:
        try:
            response = await self._post("/send_link", {"handle": handle, "url": url})
            return response.status_code == 200
        except Exception:
            log.warning(kv("photon_send_link_failed", handle=mask_handle(handle)))
            return False

    async def _send_dm(self, handle: str, text: str, effect: str | None = None) -> None:
        start = time.monotonic()
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(2),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        ):
            with attempt:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    body = {"handle": handle, "text": text}
                    if effect:
                        body["effect"] = effect
                    response = await client.post(f"{self.bridge_url}/send_dm", json=body)
                    response.raise_for_status()
        log.info(
            kv(
                "provider_call",
                provider="photon",
                method="send_dm",
                status=response.status_code,
                latency_ms=round((time.monotonic() - start) * 1000),
            )
        )
