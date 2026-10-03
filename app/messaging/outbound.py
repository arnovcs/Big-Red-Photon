"""send_group / send_private wrappers. A failed send is logged, never raised.

Stage 2 adds the PrivacyGuard check to send_group.
"""

from app.logging import get_logger, kv, mask_handle
from app.models.outbound import GroupSafeMessage, PrivateMessage
from app.providers.protocols import MessagingProvider

log = get_logger(__name__)


async def send_group(
    messaging: MessagingProvider, chat_id: str, msg: GroupSafeMessage
) -> str | None:
    try:
        return await messaging.send_group(chat_id, msg)
    except Exception:
        log.exception(kv("send_group_failed"))
        return None


async def send_private(messaging: MessagingProvider, handle: str, msg: PrivateMessage) -> None:
    try:
        await messaging.send_private(handle, msg)
    except Exception:
        log.exception(kv("send_private_failed", handle=mask_handle(handle)))
