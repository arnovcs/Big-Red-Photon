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

    async def send_group(self, handles: list[str], msg: GroupSafeMessage) -> None:
        poll = [option.model_dump() for option in msg.poll] if msg.poll else None
        for handle in handles:
            self.outbox[handle].append({"kind": "group", "text": msg.text, "poll": poll})

    async def send_private(self, handle: str, msg: PrivateMessage) -> None:
        self.outbox[handle].append(
            {"kind": "private", "text": msg.text, "image_path": msg.image_path}
        )

    async def request_location(self, handle: str) -> bool:
        self.location_requests.append(handle)
        return True

    async def shared_location(self, handle: str) -> LatLng | None:
        return self.shared.get(handle)

    def messages(self, handle: str) -> list[dict[str, Any]]:
        return list(self.outbox.get(handle, []))

    def clear(self) -> None:
        self.outbox.clear()
        self.shared.clear()
        self.location_requests.clear()
