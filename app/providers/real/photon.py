"""MessagingProvider backed by the Photon bridge (§9.1). v3: everything is a DM.

Both methods call the bridge's `POST /send_dm`. Sends are not record/replay cached:
replaying a send would mean a message never reaches the phone.
"""

import time

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

    async def send_group(self, handles: list[str], msg: GroupSafeMessage) -> None:
        # msg.text already contains the rendered poll options (copy.poll_message).
        for handle in handles:
            try:
                await self._send_dm(handle, msg.text)
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
            response = await self._post("/location", {"handle": handle})
            if response.status_code != 200:
                return None
            data = response.json()
            return LatLng(lat=float(data["lat"]), lng=float(data["lng"]))
        except Exception:
            log.warning(kv("photon_shared_location_failed", handle=mask_handle(handle)))
            return None

    async def _post(self, path: str, body: dict) -> httpx.Response:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            return await client.post(f"{self.bridge_url}{path}", json=body)

    async def _send_dm(self, handle: str, text: str) -> None:
        start = time.monotonic()
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(2),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        ):
            with attempt:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(
                        f"{self.bridge_url}/send_dm", json={"handle": handle, "text": text}
                    )
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
