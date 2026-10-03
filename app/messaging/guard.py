"""PrivacyGuard: the last check before anything leaves the bot (§11).

Group-safe messages (one text to every member) must not contain ANY member's spend
limit (±$1, as "$25", "25 dollars", "25.00"), origin label, handle or phone number,
Nessie id, or the text of a preference they DMed.

A personal itinerary must not contain ANOTHER member's limit, origin, handle, or
preference text. Its own computed amounts are exempt, so a total that happens to equal
someone's limit doesn't block a legitimate itinerary.

Public terms (venue names and addresses) are removed before scanning: an origin label
like "Collegetown" must not block a poll that lists "Collegetown Bagels".
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal

from app.conversation import copy
from app.models.outbound import GroupSafeMessage, PrivateMessage

# Preference DMs shorter than this ("ok", "yes", "food") are too common to match safely.
MIN_PREFERENCE_CHARS = 8
MIN_TERM_CHARS = 3
# Ten or more digits, possibly separated: a phone number.
_PHONE = re.compile(r"\+?\d(?:[\s\-.()]*\d){9,}")


@dataclass(frozen=True)
class MemberSecrets:
    handle: str
    spend_limit_usd: Decimal | None = None
    origin_label: str | None = None
    nessie_customer_id: str | None = None
    preference_texts: tuple[str, ...] = ()


@dataclass(frozen=True)
class Verdict:
    reasons: tuple[str, ...] = ()  # kinds only ("limit", "origin", ...), never values

    @property
    def blocked(self) -> bool:
        return bool(self.reasons)


def _normalize(text: str) -> str:
    return " ".join(re.sub(r"[^\w$.@+\-' ]", " ", text.lower()).split())


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _amount_patterns(amount: int) -> list[re.Pattern[str]]:
    n = str(amount)
    return [
        re.compile(rf"\$\s?{n}(?:\.\d{{1,2}})?(?!\d)"),
        re.compile(rf"(?<![\d.]){n}(?:\.00)?\s*(?:dollars?|bucks|usd)\b"),
        re.compile(rf"(?<![\d.$]){n}\.00(?!\d)"),
    ]


def _mentions_amount(text: str, amounts: set[int]) -> bool:
    return any(p.search(text) for a in amounts if a > 0 for p in _amount_patterns(a))


def _limit_amounts(limit: Decimal, spread: int) -> set[int]:
    base = int(limit.to_integral_value())
    return {base + d for d in range(-spread, spread + 1)}


def _contains_term(text: str, term: str | None) -> bool:
    if not term or len(term.strip()) < MIN_TERM_CHARS:
        return False
    return re.search(rf"(?<!\w){re.escape(term.strip().lower())}(?!\w)", text) is not None


def _contains_preference(text: str, preferences: tuple[str, ...]) -> bool:
    for pref in preferences:
        normalized = _normalize(pref).strip(" .!?")
        if len(normalized) >= MIN_PREFERENCE_CHARS and normalized in text:
            return True
    return False


@dataclass
class PrivacyGuard:
    members: list[MemberSecrets]
    public_terms: list[str] = field(default_factory=list)

    def _scrub(self, text: str) -> str:
        lowered = text.lower()
        for term in sorted(self.public_terms, key=len, reverse=True):
            if len(term) >= MIN_TERM_CHARS:
                lowered = lowered.replace(term.lower(), " ")
        return lowered

    def _member_reasons(
        self, text: str, member: MemberSecrets, limit_amounts: set[int]
    ) -> list[str]:
        reasons = []
        if limit_amounts and _mentions_amount(text, limit_amounts):
            reasons.append("limit")
        if _contains_term(text, member.origin_label):
            reasons.append("origin")
        handle_digits = _digits(member.handle)
        if _contains_term(text, member.handle) or (
            len(handle_digits) >= 10 and handle_digits[-10:] in _digits(text)
        ):
            reasons.append("handle")
        if _contains_term(text, member.nessie_customer_id):
            reasons.append("nessie_id")
        if _contains_preference(_normalize(text), member.preference_texts):
            reasons.append("preference")
        return reasons

    def check_group(self, text: str) -> Verdict:
        scanned = self._scrub(text)
        reasons: set[str] = set()
        for member in self.members:
            amounts = _limit_amounts(member.spend_limit_usd, 1) if member.spend_limit_usd else set()
            reasons.update(self._member_reasons(scanned, member, amounts))
        if _PHONE.search(scanned):
            reasons.add("phone_number")
        return Verdict(tuple(sorted(reasons)))

    def check_private(
        self, recipient_handle: str, text: str, own_amounts: frozenset[int] = frozenset()
    ) -> Verdict:
        scanned = self._scrub(text)
        recipient = next((m for m in self.members if m.handle == recipient_handle), None)
        own_origin = (
            recipient.origin_label.lower() if recipient and recipient.origin_label else None
        )
        reasons: set[str] = set()
        for member in self.members:
            if member.handle == recipient_handle:
                continue
            amounts = (
                _limit_amounts(member.spend_limit_usd, 0) - own_amounts
                if member.spend_limit_usd
                else set()
            )
            if own_origin and member.origin_label and member.origin_label.lower() == own_origin:
                member = MemberSecrets(  # same starting point as the recipient: not a leak
                    handle=member.handle,
                    nessie_customer_id=member.nessie_customer_id,
                    preference_texts=member.preference_texts,
                )
            reasons.update(self._member_reasons(scanned, member, amounts))
        return Verdict(tuple(sorted(reasons)))

    def safe_group_fallback(self, msg: GroupSafeMessage) -> GroupSafeMessage:
        """A poll keeps its options with template blurbs if that's clean; else a notice."""
        if msg.poll:
            options = [
                o.model_copy(update={"blurb": copy.plan_blurb(o.max_travel_min)}) for o in msg.poll
            ]
            retry = GroupSafeMessage(text=copy.poll_message(options), poll=options)
            if not self.check_group(retry.text + " " + _poll_text(retry)).blocked:
                return retry
        return GroupSafeMessage(text=copy.PRIVACY_HELD_BACK)


def _poll_text(msg: GroupSafeMessage) -> str:
    return " ".join(f"{o.title} {o.blurb}" for o in msg.poll or [])


def group_text(msg: GroupSafeMessage) -> str:
    """Everything a member would see, including poll fields."""
    return f"{msg.text} {_poll_text(msg)}"


SAFE_PRIVATE_FALLBACK = PrivateMessage(text=copy.PRIVACY_HELD_BACK_PRIVATE)
