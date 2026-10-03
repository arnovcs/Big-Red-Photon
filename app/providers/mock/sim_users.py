"""Simulator UserDirectoryProvider: everyone texts the same configured line."""

from app.settings import Settings


class SimUsers:
    def __init__(self, settings: Settings) -> None:
        self.line = settings.bot_phone_number or None
        self.registered: dict[str, str] = {}  # phone → first name, for tests

    async def register(self, phone: str, first_name: str) -> str | None:
        self.registered[phone] = first_name
        return self.line
