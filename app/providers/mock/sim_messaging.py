"""In-memory messaging for the simulator and tests (§14.2). One outbox per handle."""

from collections import defaultdict
from typing import Any

from app.models.outbound import GroupSafeMessage, PrivateMessage
from app.models.private import LatLng


class SimMessaging:
    def __init__(self) -> None:
        self.outbox: dict[str, list[dict[str, Any]]] = defaultdict(list)
        # Simulated Find My: tests set shared[handle] to "share" a location.
        self.shared: dict[str, LatLng] = {}
        self.location_requests: list[str] = []
        # Tapbacks / typing as (handle, message_id, emoji) / (handle, on). Tests can set
        # reactions_work = False to simulate a line without tapbacks (text fallback).
        self.reactions: list[tuple[str, str, str]] = []
        self.typing_events: list[tuple[str, bool]] = []
        self.reactions_work = True

    async def send_group(self, handles: list[str], msg: GroupSafeMessage) -> None:
        poll = [option.model_dump() for option in msg.poll] if msg.poll else None
        for handle in handles:
            entry = {"kind": "group", "text": msg.text, "poll": poll}
            if msg.effect:
                entry["effect"] = msg.effect
            self.outbox[handle].append(entry)

    async def send_private(self, handle: str, msg: PrivateMessage) -> None:
        self.outbox[handle].append(
            {"kind": "private", "text": msg.text, "image_path": msg.image_path}
        )

    async def request_location(self, handle: str) -> bool:
        self.location_requests.append(handle)
        return True

    async def shared_location(self, handle: str) -> LatLng | None:
        return self.shared.get(handle)

    async def react(self, handle: str, message_id: str, emoji: str) -> bool:
        if not self.reactions_work:
            return False
        self.reactions.append((handle, message_id, emoji))
        return True

    async def typing(self, handle: str, on: bool) -> None:
        self.typing_events.append((handle, on))

    async def send_link(self, handle: str, url: str) -> bool:
        self.outbox[handle].append({"kind": "link", "text": url})
        return True

    def messages(self, handle: str) -> list[dict[str, Any]]:
        return list(self.outbox.get(handle, []))

    def clear(self) -> None:
        self.outbox.clear()
        self.shared.clear()
        self.location_requests.clear()
        self.reactions.clear()
        self.typing_events.clear()
