"""Simulator UserDirectoryProvider: everyone texts the same configured line."""

from app.models.identity import DirectoryUser
from app.settings import Settings


class SimUsers:
    def __init__(self, settings: Settings) -> None:
        self.line = settings.bot_phone_number or None
        self.registered: dict[str, tuple[str, str | None]] = {}  # phone → (name, email)

    async def register(
        self,
        phone: str,
        first_name: str,
        email: str | None = None,
        last_name: str | None = None,
    ) -> DirectoryUser | None:
        self.registered[phone] = (first_name, email)
        return DirectoryUser(user_id=f"sim-{phone}", line=self.line)

    def opt_in_link(self, user_id: str, message: str) -> str | None:
        return None  # no opt-in in the simulator: the page uses a plain sms: link
