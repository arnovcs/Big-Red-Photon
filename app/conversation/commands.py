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

# How people actually text yes / no ("ya", "yesss", "yeahh", "nahh", "nope"). Stretched
# letters are allowed. Kept for anything that imports the sets.
_YES = {"yes", "y", "yep", "yeah", "yup", "sure", "ok", "okay", "correct", "right"}
_NO = {"no", "n", "nope", "nah", "wrong"}
_YES_WORD = re.compile(
    r"^(y+|ye+s*|yes+|ya+h*|yah+|yea+h*|yeh+|yep+|yup+|yu+h|ok+|okay+|k+|kk+|sure+|bet|yas|word|"
    r"correct|right|mhm+|mhmm+|uh huh|ofc|yessir|absolutely|exactly|perfect|yeppers|"
    r"affirmative|true|totally|definitely)$"
)
_NO_WORD = re.compile(r"^(n+|no+|nope+|nah+|naw+|nay|wrong|incorrect|negative)$")


def yes_no(text: str) -> str | None:
    """ "yes", "no", or None, from how people really text: "Ya", "yesss", "nahh", even
    "bro what no ya means yes" (a yes word anywhere wins unless they lead with no)."""
    if any(e in text for e in ("👍", "👌", "✅", "💯")):
        return "yes"
    if any(e in text for e in ("👎", "❌", "🙅")):
        return "no"
    words = _words(text)
    if not words:
        return None
    if _NO_WORD.match(words[0]):
        return "no"
    if "not" in words:  # "not sure", "not right" aren't a yes
        return "no" if any(w in ("right", "correct", "it") for w in words) else None
    if any(_YES_WORD.match(w) for w in words):
        return "yes"
    if any(_NO_WORD.match(w) for w in words):
        return "no"
    return None


# Plain-language versions, matched only as the WHOLE message (so "let's go bowling" is a
# preference, not @go). Apostrophes and punctuation are ignored.
_PLAIN = {
    SessionCommand.PLAN: {
        "plan", "lets plan", "new plan", "start a plan", "plan something",
        "lets plan something", "make a plan", "lets make a plan",
    },
    SessionCommand.GO: {
        "go", "ok go", "okay go", "lets go", "ok lets go", "go time", "ready",
        "were ready", "everyones in", "show options", "find options", "options",
        "show me options", "whats the plan",
    },
    SessionCommand.CANCEL: {
        "cancel", "nvm", "never mind", "nevermind", "cancel it", "cancel plan",
        "cancel this", "nvm cancel", "cancel the plan",
    },
}  # fmt: skip


# "I'm done, show me options" said inside a sentence ("ok thats all can you tell me where
# to go now"). Matched on the plain text (lowercase, no apostrophes or punctuation).
# Deliberately narrow: "lets go bowling" and "where should we go for food" stay
# preferences; only clear "done / what now / show us" signals count.
GO_IN_SENTENCE = "sentence"  # ParsedCommand.arg for a go found inside a longer message
_GO_SENTENCE = [
    re.compile(p)
    for p in (
        r"\b(thats|that is) (all|it|everything)\b",
        r"\b(were|we are|im|i am) (all )?(done|ready|set)$",
        r"\b(tell|show|give|send) (me|us) (where|what|the options|options|the plan|a plan|"
        r"something)\b",
        r"\bwhere (to|do we|do i|should we|should i|can we) go( now| then)?$",
        r"\bwhat (are )?(the |our )?(options|plan)\b",
        r"\bwhats (the |our )?(plan|options)\b",
        r"^(ok |okay |so |alright |k )?(so )?what now$",
        r"\b(everyones|everybody is|everyone is|were all) (in|here)\b",
        r"\b(lets|let us) (see|hear) (the )?(options|it|what you got)\b",
    )
]


def _plain(text: str) -> str:
    return " ".join(re.findall(r"[a-z]+", text.lower().replace("'", "").replace("’", "")))


def is_go_sentence(text: str) -> bool:
    plain = _plain(text)
    return any(p.search(plain) for p in _GO_SENTENCE)


def parse_session_command(text: str) -> ParsedCommand | None:
    """Case-insensitive; "@go" may appear inside other text ("ok @go"); plain words
    ("ok go", "nvm") only as the whole message. Join codes are uppercased."""
    plain = _plain(text)
    for command, phrases in _PLAIN.items():
        if plain in phrases:
            return ParsedCommand(command)
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
    if is_go_sentence(text):
        return ParsedCommand(SessionCommand.GO, GO_IN_SENTENCE)
    return None


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z]+", text.lower())


def is_yes(text: str) -> bool:
    return yes_no(text) == "yes"


def is_no(text: str) -> bool:
    return yes_no(text) == "no"


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
