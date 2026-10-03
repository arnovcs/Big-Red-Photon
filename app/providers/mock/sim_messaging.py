"""In-memory messaging for the simulator and tests (§14.2)."""

from collections import defaultdict
from typing import Any

from app.models.outbound import GroupSafeMessage, PrivateMessage


class SimMessaging:
    def __init__(self) -> None:
        self.outbox: dict[str, list[dict[str, Any]]] = defaultdict(list)

    async def send_group(self, chat_id: str, msg: GroupSafeMessage) -> str | None:
        poll = [option.model_dump() for option in msg.poll] if msg.poll else None
        self.outbox[chat_id].append({"kind": "group", "text": msg.text, "poll": poll})
        return None  # text polls only; no native poll id

    async def send_private(self, handle: str, msg: PrivateMessage) -> None:
        self.outbox[handle].append(
            {"kind": "private", "text": msg.text, "image_path": msg.image_path}
        )

    def messages(self, chat_id_or_handle: str) -> list[dict[str, Any]]:
        return list(self.outbox.get(chat_id_or_handle, []))

    def clear(self) -> None:
        self.outbox.clear()
