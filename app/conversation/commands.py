"""Parse session commands (@plan, join <code>, @go, @cancel, @pick, A/B/C) and settings
replies. v3: everything arrives as a DM."""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from app.models.private import TravelModes


class SessionCommand(StrEnum):
    PLAN = "plan"
    JOIN = "join"
    GO = "go"
    CANCEL = "cancel"
    PICK = "pick"
    VOTE = "vote"  # bare "A" / "B" / "C"; only a vote while POLLING


@dataclass(frozen=True)
class ParsedCommand:
    command: SessionCommand
    arg: str | None = None  # option label for PICK/VOTE, join code for JOIN


_PICK = re.compile(r"@pick\s+([abc])\b", re.IGNORECASE)
_JOIN = re.compile(r"\bjoin\s+([a-z0-9]{4})\b", re.IGNORECASE)
_VOTE = re.compile(r"^\s*([abc])\s*[.!]?\s*$", re.IGNORECASE)
_AMOUNT = re.compile(r"\$?\s*(\d{1,5}(?:\.\d{1,2})?)")

_YES = {"yes", "y", "yep", "yeah", "yup", "sure", "ok", "okay", "correct", "right"}
_NO = {"no", "n", "nope", "nah", "wrong"}


def parse_session_command(text: str) -> ParsedCommand | None:
    """Case-insensitive; may appear inside other text ("ok @go"). Join codes are uppercased."""
    lowered = text.lower()
    if "@cancel" in lowered:
        return ParsedCommand(SessionCommand.CANCEL)
    if pick := _PICK.search(text):
        return ParsedCommand(SessionCommand.PICK, pick.group(1).upper())
    if "@go" in lowered:
        return ParsedCommand(SessionCommand.GO)
    if "@plan" in lowered:
        return ParsedCommand(SessionCommand.PLAN)
    if join := _JOIN.search(text):
        return ParsedCommand(SessionCommand.JOIN, join.group(1).upper())
    if vote := _VOTE.match(text):
        return ParsedCommand(SessionCommand.VOTE, vote.group(1).upper())
    return None


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z]+", text.lower())


def is_yes(text: str) -> bool:
    words = _words(text)
    return bool(words) and words[0] in _YES


def is_no(text: str) -> bool:
    words = _words(text)
    return bool(words) and words[0] in _NO


def parse_amount(text: str) -> Decimal | None:
    match = _AMOUNT.search(text)
    if match is None:
        return None
    try:
        return Decimal(match.group(1))
    except InvalidOperation:
        return None


RIDE_WORDS = {"uber", "lyft", "rideshare", "taxi", "ride"}
WALK_WORDS = {"walk", "walking", "foot", "walks"}


def parse_modes(text: str) -> TravelModes | None:
    """How someone is getting there THIS time: the mode they'll actually use.

    car → drive · bike → bike · both → drive or bike · walk → walk · uber → rideshare ·
    neither → walk, or a rideshare if that's what gets everyone there together.
    "no rideshare" rules the ride out. None if it isn't an answer.
    """
    lowered = text.lower()
    no_rideshare = bool(re.search(r"no\s*(ride\s*-?\s*share|uber|lyft)", lowered))
    words = set(_words(lowered))
    words |= {"car" for w in ("drive", "driving", "driven", "drives") if w in words}
    words |= {"bike" for w in ("biking", "bicycle", "cycling", "bikes") if w in words}
    car = "car" in words or "both" in words
    bike = "bike" in words or "both" in words
    if car or bike:
        return TravelModes(walk=False, bike=bike, drive=car, rideshare=False)
    if words & RIDE_WORDS and not no_rideshare:
        return TravelModes(walk=False, bike=False, drive=False, rideshare=True)
    if words & WALK_WORDS:
        return TravelModes(walk=True, bike=False, drive=False, rideshare=False)
    if "neither" in words or "none" in words or no_rideshare:
        return TravelModes(walk=True, bike=False, drive=False, rideshare=not no_rideshare)
    return None


# "actually I'll drive", "I'm biking", "gonna walk": someone telling the bot their mode
# mid-plan (not just mentioning a car).
_MODE_CHANGE = re.compile(
    r"\b(i'?ll|i will|i'?m|i am|im|gonna|going to|actually|instead|i can|we'?ll|switch)\b"
)


def parse_mode_change(text: str) -> TravelModes | None:
    """A mode statement in a chat message ("actually I'll drive"), or None."""
    if not _MODE_CHANGE.search(text.lower()):
        return None
    return parse_modes(text)


@dataclass(frozen=True)
class DmCommand:
    name: str  # "budget" | "location" | "car" | "help" | "start"
    arg: str = ""


def parse_dm_command(text: str) -> DmCommand | None:
    words = _words(text)
    if not words:
        return None
    first = words[0]
    if first == "budget":
        return DmCommand("budget", text)
    if first in {"location", "help", "start"}:
        return DmCommand(first)
    if first == "car" and len(words) > 1 and words[1] in _YES | _NO:
        return DmCommand("car", words[1])
    return None
