"""All user-facing strings and message templates (§7.4). Short, warm, no jargon."""

from datetime import datetime
from decimal import Decimal

from app.models.outbound import GroupPlanOption

BOT_NAME = "[NAME]"  # replace with the product name (§19 open decision 4)

# --- Group chat ---------------------------------------------------------------

GROUP_INTRO = (
    f"Hi! I'm {BOT_NAME}. I help this chat pick a plan everyone can afford and reach, "
    'and I get you there at the same time. Each of you: DM me "start" to set up '
    "privately. I never share anyone's money or location here."
)
LISTENING = "Listening — tell me what you're feeling. Say @go when ready."
ALREADY_PLANNING = "Already planning — say @cancel to start over."
NEED_MORE_PEOPLE = 'I need at least 2 people set up to plan. DM me "start" to join in.'
SAY_PLAN_FIRST = "Say @plan first, then tell me what you're feeling."
LOOKING = "🔎 Looking at options…"
PIPELINE_FAILED = "Couldn't finish — say @go to retry."
NOTHING_FITS = "Nothing fits everyone right now. Try loosening a preference, then say @go again."
CANCELLED = "Cancelled. Say @plan whenever you want to start again."


def _join_names(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def planning_with(ready_names: list[str], missing_names: list[str]) -> str:
    return (
        f"Planning with {_join_names(ready_names)}. "
        f"{_join_names(missing_names)}, DM me 'start' to be included."
    )


def member_ready(name: str, ready: int, total: int) -> str:
    return f"✅ {name} is set ({ready}/{total})."


def plan_blurb(max_travel_min: int) -> str:
    return f"Fits everyone's budget; longest trip {max_travel_min} min."


def _option_line(option: GroupPlanOption) -> str:
    arrival = (
        f"arrive within {option.arrival_window_min} min"
        if option.arrival_window_min > 0
        else "arrive together"
    )
    return (
        f"{option.label}: {option.title} · ≤{option.max_travel_min} min for everyone · "
        f"{option.price_tier} · {arrival}"
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
        "Check your DMs for your route."
    )


# --- DM onboarding ------------------------------------------------------------

ASK_NAME = f"Hi! I'm {BOT_NAME}. What's your first name?"


def ask_bank_code(name: str) -> str:
    greeting = f"Nice to meet you, {name}!" if name else "Welcome!"
    return (
        f"{greeting} To keep plans affordable, link your (sandbox) bank: reply with your "
        "bank code, like MAYA1. I never share money details with the group."
    )


BANK_CODE_INVALID = "I don't recognize that code. Try again (it looks like MAYA1)."


def confirm_limit(amount: Decimal) -> str:
    return f"About {usd(amount)} looks comfortable tonight. Use that? (yes / or type a number)"


LIMIT_INVALID = "Reply yes, or type a number like 25."
ASK_LOCATION = (
    'Where are you starting from? A landmark works, like "Olin Library" or "Collegetown".'
)
LOCATION_NOT_FOUND = 'I couldn\'t find that. Try a nearby landmark, like "Olin Library".'
SHARED_LOCATION_LABEL = "your shared location"


def confirm_location(label: str) -> str:
    return f"Got it: {label}. Right? (yes/no)"


ASK_MODES = (
    "Last one: do you have a car or a bike with you? Reply car, bike, both, or neither. "
    '(Add "no rideshare" if you\'d rather not take one.)'
)
MODES_INVALID = "Reply car, bike, both, or neither."
YOU_ARE_SET = (
    "You're set. Say @plan in your group chat whenever you're ready. Text help for options."
)
ALREADY_SET = "You're already set. Text help for options."
UPDATED = "Updated."
HELP = (
    "Text: budget <amount> to change your limit, location to change where you start, "
    "car yes / car no, or help."
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


def cost_line(arrive_local: datetime, food: Decimal, fare: Decimal, mode: str) -> str:
    total = approx_usd(food + fare)
    if fare > 0:
        fare_name = "ride" if mode == "rideshare" else "gas + parking"
        parts = f"food {approx_usd(food)} + {fare_name} {approx_usd(fare)}"
    else:
        parts = "food"
    return f"Arrive ~{clock_time(arrive_local)}. Estimated total: {total} ({parts})."
