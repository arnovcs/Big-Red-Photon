"""All user-facing strings and message templates (§7.4). Short, warm, no jargon."""

from datetime import datetime
from decimal import Decimal

from app.models.outbound import GroupPlanOption

BOT_NAME = "Huddle"  # product name (§19 open decision 4)

# --- Virtual group (v3: every message is a DM) --------------------------------

WELCOME = (
    f"Hi! I'm {BOT_NAME}. I help friends pick a plan everyone can afford and reach, and I "
    "get you there at the same time. Let's set you up. It's private: I never share your "
    "money or location with anyone."
)


def plan_started(code: str) -> str:
    return (
        f"Plan started! 🎉 Tell your friends to text me: join {code}\n"
        "Meanwhile, tell me what you're in the mood for. Only I see it. "
        "Say @go when everyone's in."
    )


def already_in_plan(code: str) -> str:
    return f"You're already in a plan (code {code}). Say @cancel to start over."


def setup_first(code: str) -> str:
    return f"Let's get you set up first, then send join {code} again."


def joined(name: str, member_count: int) -> str:
    return f"✅ {name} joined ({member_count} people)."


YOU_JOINED = (
    "You're in! Tell me what you're in the mood for. Only I see it. Say @go when everyone's in."
)
UNKNOWN_CODE = "I don't know that code. Check it and try again."
PLAN_ALREADY_STARTED = "That plan already started. Ask them to @plan again."


def plan_full(max_members: int) -> str:
    return f"That plan is full ({max_members} people max)."


NOTED = "Got it 👍 Keep going, or say @go when everyone's ready."


def need_two(code: str) -> str:
    return f"I need at least 2 people. Share code {code} first."


NOT_IN_PLAN = "You're not in a plan yet. Say @plan to start one, or join <code> to join a friend's."
LOOKING = "🔎 Looking at options…"
PIPELINE_FAILED = "Couldn't finish — say @go to retry."


def loosen_hint(field: str, polarity: str, value: str | None) -> str:
    """Suggest relaxing one venue preference. `value` is a plain word, never a number."""
    if field == "novelty":
        target = "somewhere familiar" if polarity == "want" else "somewhere new"
    elif value is None:
        return "Loosening a preference could help."
    else:
        target = f"more than {value}" if polarity == "want" else value
    return f"Being open to {target} could help."


NO_PLACES = (
    "I couldn't find any places for that nearby. Try something a bit broader "
    '(like "something sporty" or "food"), then say @go again.'
)


def nothing_fits(hint: str | None) -> str:
    middle = hint or "Try loosening a preference."
    return f"Nothing fits everyone right now. {middle} Then say @go again."


NEED_MORE_PEOPLE = (
    "I need at least 2 people who are fully set up. Share the code and try @go again."
)
CANCELLED = "Plan cancelled. Say @plan whenever you want to start again."

PRIVACY_HELD_BACK = (
    "I held back an update to protect someone's privacy. Say @go to try again, or @cancel."
)
PRIVACY_HELD_BACK_PRIVATE = (
    "I held back your details because they touched someone else's private info. "
    "Text help if you need anything."
)


def votes_progress(voted: int, total: int) -> str:
    return f"🗳️ {voted} of {total} voted"


def plan_blurb(max_travel_min: int) -> str:
    return f"Fits everyone's budget; longest trip {max_travel_min} min."


def _price_text(option: GroupPlanOption) -> str:
    if option.price_tier == "?":
        return "price ?"
    return f"{option.price_tier} est." if option.price_estimated else option.price_tier


def _option_line(option: GroupPlanOption) -> str:
    arrival = (
        f"arrive within {option.arrival_window_min} min"
        if option.arrival_window_min > 0
        else "arrive together"
    )
    return (
        f"{option.label}: {option.title} · ≤{option.max_travel_min} min for everyone · "
        f"{_price_text(option)} · {arrival}"
    )


def poll_message(options: list[GroupPlanOption]) -> str:
    count = len(options)
    header = (
        "Here's the plan that fits everyone's constraints:"
        if count == 1
        else f"Here are {count} plans that fit everyone's constraints:"
    )
    lines = [header]
    for option in options:
        lines.append(_option_line(option))
        lines.append(option.blurb)
    labels = [o.label for o in options]
    lines.append(f"Reply {_join_names_or(labels)}.")
    return "\n".join(lines)


def _join_names_or(labels: list[str]) -> str:
    if len(labels) <= 2:
        return " or ".join(labels)
    return ", ".join(labels[:-1]) + ", or " + labels[-1]


def clock_time(dt: datetime) -> str:
    """6:42 style, already in local time."""
    return f"{dt.hour % 12 or 12}:{dt.minute:02d}"


def confirmation(label: str, venue: str, arrive_local: datetime) -> str:
    return (
        f"🎉 Plan {label}: {venue}. Everyone arrives around {clock_time(arrive_local)}. "
        "Your route is below 👇"
    )


# --- DM onboarding ------------------------------------------------------------

ASK_NAME = "What's your first name?"


def ask_bank_code(name: str) -> str:
    greeting = f"Nice to meet you, {name}!" if name else "Welcome!"
    return (
        f"{greeting} To keep plans affordable, link your (sandbox) bank: reply with your "
        "bank code, like MAYA1. I never share money details with anyone."
    )


BANK_CODE_INVALID = "I don't recognize that code. Try again (it looks like MAYA1)."


def confirm_limit(amount: Decimal) -> str:
    return f"About {usd(amount)} looks comfortable tonight. Use that? (yes / or type a number)"


LIMIT_INVALID = "Reply yes, or type a number like 25."
ASK_LOCATION = (
    "Where are you starting from? Share your location with me (tap the card, then say "
    '"done"), or type a landmark like "Olin Library" or "Collegetown".'
)
LOCATION_NOT_FOUND = (
    "I couldn't find that place. Try the name of a business or building near you, "
    'like "Collegetown Bagels", or share your location with me and say "done".'
)
LIVE_LOCATION_LABEL = "your live location"
LOCATION_FIRST = (
    'Almost done! First, where are you starting from? Share your location and say "done", '
    'or type a landmark like "Olin Library".'
)
SHARE_NOT_SEEN = (
    'I can\'t see your location yet. Give it a few seconds and say "done" again, '
    'or type a landmark like "Olin Library".'
)


def confirm_location(label: str) -> str:
    return f"Got it: {label}. Right? (yes/no)"


ASK_MODES = (
    "Last one: do you have a car or a bike with you? Reply car, bike, both, or neither. "
    '(Add "no rideshare" if you\'d rather not take one.)'
)
ASK_TRIP_MODES = (
    "How are you getting there this time? Reply car, bike, walk, uber, or neither "
    "(walking, and I'll suggest a ride if that gets you there with everyone)."
)
MODES_INVALID = "Reply car, bike, walk, uber, or neither."
MODES_FIRST = "First, how are you getting there this time? Reply car, bike, walk, uber, or neither."
NO_BUS_YET = "I can't plan bus trips yet. Reply car, bike, walk, uber, or neither."
DEFAULT_WALK = (
    "You didn't say how you're getting there, so I'm planning you as walking. "
    "Tell me if you're driving (e.g. \"I'm driving\")."
)


def mode_changed(modes_text: str) -> str:
    return f"Got it, {modes_text}. I'll use that for this plan."


TRIP_MODES_SET = "Got it. Now tell me what you're in the mood for!"
TRIP_LIVE_LOCATION = (
    "Got it. I'll plan from your live location "
    '(text "location" to use a different spot). Now tell me what you\'re in the mood for!'
)
ASK_TRIP_LOCATION = (
    "Where are you starting from this time? Share your location with me (tap the card, "
    'then say "done"), type a place like "Olin Library", or say "same" to start where '
    "you did last time."
)


def waiting_on(names: list[str]) -> str:
    return f"Still waiting on {', '.join(names)} to answer my questions."


YOU_ARE_SET = "You're set! Start a plan with @plan, or join a friend's with join <code>."

# --- Web signup, finished by text (app/onboarding/web_claim.py) -----------------


def claim_linked(name: str, limit: Decimal) -> str:
    return (
        f"Thanks, {name}! Your demo bank is linked. About {usd(limit)} looks comfortable "
        "tonight. Use that? (yes / or type a number)"
    )


def web_intro(name: str) -> str:
    return (
        f"Hi {name}! You're set up. Start a plan with @plan, or join a friend's with join <code>."
    )


def web_signup_nudge(name: str, token: str) -> str:
    return f"Hi {name}! To finish signing up, reply: start {token}"


CLAIM_UNKNOWN = "I couldn't find that signup code. Check it, or sign up again on the website."
CLAIM_USED = "That signup code was already used."
CLAIM_EXPIRED = "That signup code expired. Please sign up again on the website."
CLAIM_WRONG_PHONE = (
    "That code was made for a different phone number. Text it from the phone you signed up with."
)
CLAIM_ALREADY_SET = "You're already set up. Text help for options."
CLAIM_BANK_GONE = "I couldn't link that demo bank. Please sign up again on the website."

ALREADY_SET = "You're already set. Text help for options."
UPDATED = "Updated."
HELP = (
    "Plans: @plan to start one, join <code> to join a friend's, @go when everyone's in, "
    "A/B/C to vote, @cancel to stop.\n"
    "Settings: budget <amount>, location, car yes / car no."
)

# --- Personal itinerary DM ----------------------------------------------------


def usd(amount: Decimal) -> str:
    if amount == amount.to_integral_value():
        return f"${amount:.0f}"
    return f"${amount:.2f}"


def approx_usd(amount: Decimal) -> str:
    return f"~${amount.quantize(Decimal(1)):.0f}"


def itinerary_header(venue: str, address: str) -> str:
    return f"Your plan for tonight: {venue}, {address}."


def leave_line(
    mode: str, leave_local: datetime, travel_min: int, pickup_min: int, ride_min: int
) -> str:
    t = clock_time(leave_local)
    if mode == "walk":
        return f"🚶 Leave by {t} and walk about {travel_min} min."
    if mode == "bike":
        return f"🚲 Leave by {t} and bike about {travel_min} min."
    if mode == "drive":
        return f"🚗 Leave by {t} and drive about {travel_min} min (including parking)."
    return f"🚗 Request a ride by {t} (about {pickup_min} min pickup + {ride_min} min drive)."


def maps_line(url: str) -> str:
    return f"🗺️ Directions: {url}"


def cost_line(
    arrive_local: datetime, food: Decimal | None, fare: Decimal, mode: str, what: str = "food"
) -> str:
    """`what`: "food" for places to eat or drink, "entry" for activities."""
    if food is None:  # Google has no price for this venue
        fare_name = "ride" if mode == "rideshare" else "gas + parking"
        travel = f"{fare_name} {approx_usd(fare)} + " if fare > 0 else ""
        return f"Arrive ~{clock_time(arrive_local)}. Cost: {travel}{what} (price unknown)."
    total = approx_usd(food + fare)
    if fare > 0:
        fare_name = "ride" if mode == "rideshare" else "gas + parking"
        parts = f"{what} {approx_usd(food)} + {fare_name} {approx_usd(fare)}"
    else:
        parts = what
    return f"Arrive ~{clock_time(arrive_local)}. Estimated total: {total} ({parts})."
