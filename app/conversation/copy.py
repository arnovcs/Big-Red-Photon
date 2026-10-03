"""All user-facing strings and message templates (§7.4).

Voice: a friend texting. Lowercase-ish, short, one idea per message, emoji sparingly.
Plain words work for commands ("go", "nvm"); the @ versions still work too.
"""

from datetime import datetime
from decimal import Decimal

from app.models.outbound import GroupPlanOption

BOT_NAME = "Huddle"

# Tapbacks (iMessage reactions) the bot uses instead of "got it" messages.
REACT_OK = "👍"
REACT_LOVE = "❤️"
REACT_HUH = "❓"
REACT_STRONG = "‼️"
REACT_LAUGH = "😂"

# --- Virtual group (v3: every message is a DM) --------------------------------

WELCOME = (
    f"hey! I'm {BOT_NAME} 👋 I find a spot your whole group can afford and reach, "
    "and get everyone there at the same time. your money + location stay private, always."
)


def plan_started(code: str) -> str:
    return f"let's do it 🎉 tell your friends to text me: join {code} (say go when everyone's in)"


def already_in_plan(code: str) -> str:
    return f"you're already in a plan (code {code}). say nvm to start over"


def setup_first(code: str) -> str:
    return f"quick setup first, then send join {code} again"


def joined(name: str, member_count: int) -> str:
    return f"{name} is in ✅ ({member_count} of you)"


YOU_JOINED = "you're in!"
UNKNOWN_CODE = "hmm I don't know that code. double check it?"
PLAN_ALREADY_STARTED = "that plan already started. ask them to start a new one"


def plan_full(max_members: int) -> str:
    return f"that plan's full ({max_members} max)"


# Fallback when a tapback can't be sent (the reaction is the usual acknowledgment).
NOTED = "got it 👍"
GO_HINT = "say go when everyone's in"


def ready_progress(name: str, ready: int, needed: int) -> str:
    """To the whole group when someone says go, before enough people have."""
    return f"{name}'s ready ✅ ({ready}/{needed} needed). say go when you're in too"


def already_ready(more: int) -> str:
    return f"you're already in ✅ waiting on {more} more to say go"


def ready_enough(name: str) -> str:
    return f"{name}'s ready too ✅ finding spots 👀"


def need_two(code: str) -> str:
    return f"need at least 2 people! share code {code} first"


NOT_IN_PLAN = "you're not in a plan yet. say plan to start one, or join <code> for a friend's"
LOOKING = "on it 🔎"  # fallback when the typing indicator can't be shown
PIPELINE_FAILED = "ugh something broke on my end. say go to try again"


def loosen_hint(field: str, polarity: str, value: str | None) -> str:
    """Suggest relaxing one venue preference. `value` is a plain word, never a number."""
    if field == "novelty":
        target = "somewhere familiar" if polarity == "want" else "somewhere new"
    elif value is None:
        return "maybe loosen up one ask?"
    else:
        target = f"more than {value}" if polarity == "want" else value
    return f"being open to {target} could help."


NO_PLACES = (
    "couldn't find anywhere for that nearby 😕 try something broader "
    '(like "something sporty" or "food") and say go again'
)


def nothing_fits(hint: str | None) -> str:
    middle = hint or "maybe loosen up one ask?"
    return f"nothing works for everyone rn. {middle} then say go again"


NEED_MORE_PEOPLE = "need at least 2 people who are all set up. share the code and say go again"
CANCELLED = "plan cancelled 👋 say plan whenever"

PRIVACY_HELD_BACK = "held back an update to keep someone's info private. say go to retry, or nvm"
PRIVACY_HELD_BACK_PRIVATE = (
    "held back your details bc they touched someone else's private info. text help if stuck"
)


def votes_progress(voted: int, total: int) -> str:
    return f"🗳️ {voted}/{total} voted"


def plan_blurb(max_travel_min: int) -> str:
    return f"fits everyone's budget, longest trip {max_travel_min} min"


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
        "ok here's what works for everyone 👇" if count > 1 else "ok this one works for everyone 👇"
    )
    lines = [header]
    for option in options:
        lines.append(_option_line(option))
        lines.append(option.blurb)
    labels = [o.label for o in options]
    lines.append(f"reply {_join_names_or(labels)}")
    return "\n".join(lines)


def _join_names_or(labels: list[str]) -> str:
    if len(labels) <= 2:
        return " or ".join(labels)
    return ", ".join(labels[:-1]) + ", or " + labels[-1]


def clock_time(dt: datetime) -> str:
    """6:42 style, already in local time."""
    return f"{dt.hour % 12 or 12}:{dt.minute:02d}"


def day_word(local: datetime) -> str:
    """ "tonight" from 5 PM, otherwise "today"."""
    return "tonight" if local.hour >= 17 else "today"


def confirmation(label: str, venue: str, arrive_local: datetime) -> str:
    return (
        f"🎉 it's {label}: {venue}! everyone gets there around {clock_time(arrive_local)}. "
        "your route's coming 👇"
    )


# --- DM onboarding ------------------------------------------------------------

ASK_NAME = "what should I call you?"


def ask_bank_code(name: str) -> str:
    greeting = f"nice to meet you {name}!" if name else "nice to meet you!"
    return (
        f"{greeting} to keep plans affordable, send your (sandbox) bank code, like MAYA1. "
        "I never share money stuff with anyone"
    )


BANK_CODE_INVALID = "hmm don't recognize that one. it looks like MAYA1"


def confirm_limit(amount: Decimal, when: str = "today") -> str:
    return f"looks like ~{usd(amount)} is comfy {when}. cool? (or send a number)"


LIMIT_INVALID = "say yes, or send a number like 25"
ASK_LOCATION = (
    "where are you starting from? share your location with me (tap the card, then say "
    '"done") or type a place like "Collegetown Bagels"'
)
LOCATION_NOT_FOUND = (
    "couldn't find that one 🤔 try a business or building near you, "
    'like "Collegetown Bagels", or share your location and say "done"'
)
LIVE_LOCATION_LABEL = "your live location"
LIVE_LOCATION_SET = "got it, using your live location 📍"


def live_location_offer(place: str | None) -> str:
    """They're already sharing: offer it instead of asking where they're starting."""
    where = f"near {place} " if place else ""
    return f"I have your live location {where}📍 use that, or text a different spot?"


LOCATION_UNCLEAR = (
    "is that a yes? if not, send me the place you're starting from (or share your location "
    'and say "done")'
)
LOCATION_FIRST = (
    'almost done! where are you starting from? share your location and say "done", '
    'or type a place like "Collegetown Bagels"'
)
SHARE_NOT_SEEN = (
    'can\'t see your location yet. give it a sec and say "done" again, '
    'or type a place like "Collegetown Bagels"'
)


def confirm_location(label: str) -> str:
    return f"{label}, right?"


def location_set(label: str) -> str:
    """When Google's match is clearly what they typed: no yes/no needed."""
    return f"got it, {label} 📍"


ASK_MODES = (
    "last one: do you have a car or a bike with you? car, bike, both, or neither. "
    '(add "no rideshare" if you\'d rather not take one)'
)
ASK_TRIP_MODES = "how're you getting there? 🚗 car, 🚲 bike, 🚶 walk, 🚕 uber, or neither"
MODES_INVALID = "car, bike, walk, uber, or neither?"
MODES_FIRST = "first, how're you getting there? car, bike, walk, uber, or neither"
NO_BUS_YET = "can't do bus routes yet 😅 car, bike, walk, uber, or neither?"
DEFAULT_WALK = (
    "you didn't say how you're getting there so I've got you walking 🚶 text me if you're driving"
)


def mode_changed(modes_text: str) -> str:
    return f"got it, {modes_text} 👍"


TRIP_MODES_SET = "bet. what are you in the mood for?"
ASK_TRIP_LOCATION = (
    'where are you starting from? share your location (tap the card, then say "done"), '
    'type a place, or say "same" for last time\'s spot'
)


def waiting_on(names: list[str]) -> str:
    return f"still waiting on {', '.join(names)} ⏳"


YOU_ARE_SET = "you're all set! say plan to start one, or join <code> for a friend's"
ALREADY_SET = "you're already set! text help if you need anything"
UPDATED = "updated 👍"
HELP = (
    "say plan to start one, join <code> for a friend's, go when everyone's in, "
    "A/B/C to vote, nvm to cancel.\n"
    "settings: budget <amount>, location, car yes / car no"
)


def hello_idle(name: str) -> str:
    return (
        f"hey {name}! 👋 wanna plan something? just say plan"
        if name
        else ("hey! 👋 wanna plan something? just say plan")
    )


HELLO_COLLECTING = "hey! 👋 tell me what you're in the mood for, or say go when everyone's in"
HELLO_VOTING = "hey! 👋 vote with A, B, or C"
HELLO_BUSY = "hey! 👋 hang tight, I'm on it"

# --- Web signup claim (app/onboarding/web_claim.py) ----------------------------


def claim_linked(name: str, limit: Decimal) -> str:
    return (
        f"thanks {name}! your demo bank's linked. looks like ~{usd(limit)} is comfy. "
        "cool? (or send a number)"
    )


def web_intro(name: str) -> str:
    return f"hey {name}! you're all set. say plan to start one, or join <code> for a friend's"


def web_signup_nudge(name: str, token: str) -> str:
    return f"hey {name}! to finish signing up, reply: start {token}"


CLAIM_UNKNOWN = "hmm can't find that signup code. double check it, or sign up again on the website"
CLAIM_USED = "that signup code was already used"
CLAIM_EXPIRED = "that signup code expired 😕 sign up again on the website"
CLAIM_WRONG_PHONE = (
    "that code was made for a different number. text it from the phone you signed up with"
)
CLAIM_ALREADY_SET = "you're already set up! text help if you need anything"
CLAIM_BANK_GONE = "couldn't link that demo bank. sign up again on the website"

# --- Personal itinerary DM ----------------------------------------------------


def usd(amount: Decimal) -> str:
    if amount == amount.to_integral_value():
        return f"${amount:.0f}"
    return f"${amount:.2f}"


def approx_usd(amount: Decimal) -> str:
    return f"~${amount.quantize(Decimal(1)):.0f}"


def itinerary_header(venue: str, address: str, when: str = "today") -> str:
    return f"your plan {when}: {venue}, {address}"


def leave_line(
    mode: str, leave_local: datetime, travel_min: int, pickup_min: int, ride_min: int
) -> str:
    t = clock_time(leave_local)
    if mode == "walk":
        return f"🚶 leave by {t}, it's about a {travel_min} min walk"
    if mode == "bike":
        return f"🚲 leave by {t}, about {travel_min} min on the bike"
    if mode == "drive":
        return f"🚗 leave by {t}, about {travel_min} min drive (incl. parking)"
    return f"🚕 request a ride by {t} (~{pickup_min} min pickup + {ride_min} min drive)"


def next_stop_line(name: str, address: str, walk_min: int) -> str:
    return f"then 🚶 ~{walk_min} min walk together to {name}, {address}"


def maps_line(url: str) -> str:
    return f"🗺️ {url}"


def leave_nudge(mode: str, minutes: int) -> str:
    verb = {"walk": "walking", "bike": "biking", "drive": "driving", "rideshare": "getting a ride"}
    return f"heads up, leave in {minutes} if you're {verb.get(mode, 'heading over')} 👀"


def cost_line(
    arrive_local: datetime, food: Decimal | None, fare: Decimal, mode: str, what: str = "food"
) -> str:
    """`what`: "food" for places to eat or drink, "entry" for activities."""
    if food is None:  # Google has no price for this venue
        fare_name = "ride" if mode == "rideshare" else "gas + parking"
        travel = f"{fare_name} {approx_usd(fare)} + " if fare > 0 else ""
        return f"you'll get there ~{clock_time(arrive_local)}. cost: {travel}{what} (price unknown)"
    total = approx_usd(food + fare)
    if fare > 0:
        fare_name = "ride" if mode == "rideshare" else "gas + parking"
        parts = f"{what} {approx_usd(food)} + {fare_name} {approx_usd(fare)}"
    else:
        parts = what
    return f"you'll get there ~{clock_time(arrive_local)}. about {total} total ({parts})"
