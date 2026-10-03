"""send_group / send_private wrappers. A failed send is logged, never raised.

v3: a "group" message goes to every member's DM via send_group(handles, ...), never a
loop of send_private, so the GroupSafeMessage type rule (and, in Stage 2, the
PrivacyGuard) stays in one place.
"""

from app.logging import get_logger, kv, mask_handle
from app.models.outbound import GroupSafeMessage, PrivateMessage
from app.providers.protocols import MessagingProvider

log = get_logger(__name__)


async def send_group(
    messaging: MessagingProvider, handles: list[str], msg: GroupSafeMessage
) -> None:
    try:
        await messaging.send_group(handles, msg)
    except Exception:
        log.exception(kv("send_group_failed", members=len(handles)))


async def send_private(messaging: MessagingProvider, handle: str, msg: PrivateMessage) -> None:
    try:
        await messaging.send_private(handle, msg)
    except Exception:
        log.exception(kv("send_private_failed", handle=mask_handle(handle)))
