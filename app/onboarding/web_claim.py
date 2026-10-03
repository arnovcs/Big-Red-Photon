"""Finish a web signup by text: "start <TOKEN>" from the phone that signed up.

Texting the bot first is required anyway (free Photon lines can't message a number that
hasn't texted them), and it proves the person owns the number: the sender's handle must
match the phone on the signup. On success the bank is linked through the vault and the
person confirms or overrides their budget by text (onboarding/fsm.py), then gets the
intro. Any problem gets a short reply, then normal onboarding carries on.
"""

import re
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.conversation import copy
from app.db import queries
from app.db.tables import UserRow
from app.deps import Deps
from app.logging import get_logger, kv
from app.messaging.outbound import send_private
from app.models.identity import OnboardingState
from app.models.outbound import PrivateMessage
from app.onboarding.fsm import Onboarding
from app.private import vault

log = get_logger(__name__)

_START_TOKEN = re.compile(r"^\s*start\s+([a-z0-9]{4})\s*[.!]?\s*$", re.IGNORECASE)
_NANP_LENGTH = 10  # US/Canada numbers without the country code
_E164_MIN_DIGITS, _E164_MAX_DIGITS = 8, 15


def parse_start_token(text: str) -> str | None:
    """ "start K7QP" → "K7QP". A bare "start" (or anything else) → None."""
    match = _START_TOKEN.match(text)
    return match.group(1).upper() if match else None


def normalize_phone(raw: str) -> str | None:
    """A phone number in the form iMessage handles use: "+" and digits (E.164).

    10 digits are taken as US/Canada (+1). Returns None for anything that isn't a
    plausible phone number, including email handles.
    """
    text = raw.strip()
    if not re.fullmatch(r"\+?[\d\s().\-]+", text):
        return None
    digits = re.sub(r"\D", "", text)
    if text.startswith("+"):
        ok = _E164_MIN_DIGITS <= len(digits) <= _E164_MAX_DIGITS
        return f"+{digits}" if ok else None
    if len(digits) == _NANP_LENGTH:
        return f"+1{digits}"
    if len(digits) == _NANP_LENGTH + 1 and digits.startswith("1"):
        return f"+{digits}"
    return None


def _as_utc(dt: datetime) -> datetime:
    """SQLite drops tzinfo; stored times are UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


class WebClaim:
    def __init__(self, deps: Deps, onboarding: Onboarding) -> None:
        self.deps = deps
        self.onboarding = onboarding

    async def _reply(self, user: UserRow, text: str) -> None:
        await send_private(self.deps.messaging, user.handle, PrivateMessage(text=text))

    async def handle(self, db: AsyncSession, user: UserRow, token: str) -> None:
        signup = await queries.signup_by_token(db, token)
        now = self.deps.clock()
        if user.onboarding_state == OnboardingState.READY:
            problem, result = copy.CLAIM_ALREADY_SET, "already_ready"
        elif signup is None:
            problem, result = copy.CLAIM_UNKNOWN, "unknown"
        elif signup.claimed:
            problem, result = copy.CLAIM_USED, "used"
        elif _as_utc(signup.expires_at) <= now:
            problem, result = copy.CLAIM_EXPIRED, "expired"
        elif normalize_phone(user.handle) != signup.phone:
            problem, result = copy.CLAIM_WRONG_PHONE, "wrong_phone"
        else:
            problem, result = None, "ok"

        if problem is None and signup is not None:
            limit = await vault.link_customer(db, user.id, signup.bank_code, self.deps.finance)
            if limit is None:
                problem, result = copy.CLAIM_BANK_GONE, "bank_gone"
            else:
                user.display_name = signup.first_name
                user.onboarding_state = OnboardingState.AWAITING_LIMIT_CONFIRM
                signup.claimed = True
                signup.claimed_at = now
                signup.claimed_user_id = user.id
                await db.flush()
                log.info(kv("web_claim", result=result, user=user.id.hex[:8]))
                await self._reply(user, copy.claim_linked(signup.first_name, limit))
                return

        log.info(kv("web_claim", result=result))
        await self._reply(user, problem)
        if user.onboarding_state != OnboardingState.READY:
            await self.onboarding.reprompt(user)  # carry on with normal setup
