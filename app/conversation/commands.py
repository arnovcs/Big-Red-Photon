"""Parse group commands (@plan, @go, @cancel, @pick, poll replies) and DM replies."""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from app.models.private import TravelModes


class GroupCommand(StrEnum):
    PLAN = "plan"
    GO = "go"
    CANCEL = "cancel"
    PICK = "pick"
    VOTE = "vote"  # bare "A" / "B" / "C"; only meaningful while POLLING


@dataclass(frozen=True)
class ParsedCommand:
    command: GroupCommand
    option: str | None = None


_PICK = re.compile(r"@pick\s+([abc])\b", re.IGNORECASE)
_VOTE = re.compile(r"^\s*([abc])\s*[.!]?\s*$", re.IGNORECASE)
_AMOUNT = re.compile(r"\$?\s*(\d{1,5}(?:\.\d{1,2})?)")

_YES = {"yes", "y", "yep", "yeah", "yup", "sure", "ok", "okay", "correct", "right"}
_NO = {"no", "n", "nope", "nah", "wrong"}


def parse_group(text: str) -> ParsedCommand | None:
    """Commands are case-insensitive and may appear inside other text ("ok @go")."""
    lowered = text.lower()
    if "@cancel" in lowered:
        return ParsedCommand(GroupCommand.CANCEL)
    if pick := _PICK.search(text):
        return ParsedCommand(GroupCommand.PICK, pick.group(1).upper())
    if "@go" in lowered:
        return ParsedCommand(GroupCommand.GO)
    if "@plan" in lowered:
        return ParsedCommand(GroupCommand.PLAN)
    if vote := _VOTE.match(text):
        return ParsedCommand(GroupCommand.VOTE, vote.group(1).upper())
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


def parse_modes(text: str) -> TravelModes | None:
    """ "car" | "bike" | "both" | "neither", optionally with "no rideshare".

    Walk and ride-share are on unless the user says "no rideshare".
    """
    lowered = text.lower()
    no_rideshare = bool(re.search(r"no\s*(ride\s*-?\s*share|uber|lyft)", lowered))
    words = set(_words(lowered))
    if "both" in words:
        car, bike = True, True
    elif "neither" in words or "none" in words:
        car, bike = False, False
    elif "car" in words or "bike" in words:
        car, bike = "car" in words, "bike" in words
    elif no_rideshare:
        car, bike = False, False
    else:
        return None
    return TravelModes(walk=True, bike=bike, drive=car, rideshare=not no_rideshare)


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
