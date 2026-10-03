"""send_group / send_private wrappers: PrivacyGuard first, then send. Never raise.

v3: a "group" message goes to every member's DM via send_group(handles, ...), never a
loop of send_private, so the GroupSafeMessage type rule and the guard stay in one place.
On a block, the message is not sent: "privacy_block" is logged (kinds only, never the
value) and the safe template goes out instead.
"""

from app.logging import get_logger, kv, mask_handle
from app.messaging.guard import SAFE_PRIVATE_FALLBACK, PrivacyGuard, group_text
from app.models.outbound import GroupSafeMessage, PrivateMessage
from app.providers.protocols import MessagingProvider

log = get_logger(__name__)


async def send_group(
    messaging: MessagingProvider,
    handles: list[str],
    msg: GroupSafeMessage,
    guard: PrivacyGuard,
) -> None:
    verdict = guard.check_group(group_text(msg))
    if verdict.blocked:
        log.warning(kv("privacy_block", channel="group", reasons=",".join(verdict.reasons)))
        msg = guard.safe_group_fallback(msg)
    try:
        await messaging.send_group(handles, msg)
    except Exception:
        log.exception(kv("send_group_failed", members=len(handles)))


async def send_private(
    messaging: MessagingProvider,
    handle: str,
    msg: PrivateMessage,
    guard: PrivacyGuard | None = None,
    own_amounts: frozenset[int] = frozenset(),
) -> None:
    """`guard` is required for anything built from group data (itineraries); replies
    that only echo the recipient's own input may skip it."""
    if guard is not None:
        verdict = guard.check_private(handle, msg.text, own_amounts)
        if verdict.blocked:
            log.warning(kv("privacy_block", channel="private", reasons=",".join(verdict.reasons)))
            msg = SAFE_PRIVATE_FALLBACK
    try:
        await messaging.send_private(handle, msg)
    except Exception:
        log.exception(kv("send_private_failed", handle=mask_handle(handle)))
