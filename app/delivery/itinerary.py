"""Winner delivery (§10): the group confirmation, then one private itinerary DM each."""

import uuid
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from app.conversation import copy
from app.db import queries
from app.db.session import session_factory
from app.db.tables import SessionRow
from app.deps import Deps
from app.logging import get_logger, kv
from app.messaging.guard import PrivacyGuard
from app.messaging.outbound import send_group, send_private
from app.models.outbound import GroupSafeMessage, PrivateMessage
from app.models.plans import PersonAssignment, Plan
from app.models.private import LatLng
from app.models.routing import Mode, RouteStep
from app.optimizer.arrival import whole_minutes
from app.planning.pipeline import load_pid_map
from app.private import vault
from app.providers.costs import DRIVE_PARKING_MIN

log = get_logger(__name__)

MAX_STEPS_SHOWN = 5

# Google Maps URLs (no API key; developers.google.com/maps/documentation/urls).
# Ride-share gets driving directions: Uber deep links need a registered client_id.
GOOGLE_TRAVEL_MODE = {
    Mode.WALK: "walking",
    Mode.BIKE: "bicycling",
    Mode.DRIVE: "driving",
    Mode.RIDESHARE: "driving",
}


def directions_url(destination: LatLng, mode: Mode, origin: LatLng | None = None) -> str:
    """Google Maps directions to the venue, from this person's own starting point (the
    link only ever goes into their own DM). No origin: Maps uses the phone's location."""
    query = {"api": "1"}
    if origin is not None:
        query["origin"] = f"{origin.lat:.5f},{origin.lng:.5f}"
    query["destination"] = f"{destination.lat:.5f},{destination.lng:.5f}"
    query["travelmode"] = GOOGLE_TRAVEL_MODE[mode]
    return "https://www.google.com/maps/dir/?" + urlencode(query)


def itinerary_text(
    plan: Plan,
    assignment: PersonAssignment,
    steps: list[RouteStep],
    tz: ZoneInfo,
    pickup_wait_min: int,
    origin: LatLng | None = None,
) -> str:
    venue = plan.candidate
    leave_local = assignment.leave_by.astimezone(tz)
    arrive_local = assignment.arrive_at.astimezone(tz)
    travel = whole_minutes(assignment.travel_min)
    if assignment.mode == Mode.RIDESHARE:
        ride = whole_minutes(assignment.travel_min - pickup_wait_min)
    elif assignment.mode == Mode.DRIVE:
        ride = whole_minutes(assignment.travel_min - DRIVE_PARKING_MIN)
    else:
        ride = travel

    lines = [
        copy.itinerary_header(venue.name, venue.address),
        copy.leave_line(assignment.mode.value, leave_local, travel, pickup_wait_min, ride),
    ]
    if assignment.mode != Mode.RIDESHARE:
        lines.extend(f"• {step.instruction}" for step in steps[:MAX_STEPS_SHOWN])
    lines.append(copy.maps_line(directions_url(venue.location, assignment.mode, origin)))
    food = _food_cost(plan, assignment)
    lines.append(copy.cost_line(arrive_local, food, assignment.fare_usd, assignment.mode.value))
    return "\n".join(lines)


def _food_cost(plan: Plan, assignment: PersonAssignment) -> Decimal | None:
    """The venue's own estimate; None when Google has no price (never the stand-in)."""
    return plan.candidate.est_cost_pp.value


def own_amounts(plan: Plan, assignment: PersonAssignment) -> frozenset[int]:
    """The whole-dollar amounts this person's own itinerary shows (food, fare, total)."""
    food = _food_cost(plan, assignment)
    amounts = (
        [assignment.fare_usd]
        if food is None
        else [food, assignment.fare_usd, food + assignment.fare_usd]
    )
    return frozenset(int(x.quantize(Decimal(1))) for x in amounts)


@dataclass(frozen=True)
class Itinerary:
    handle: str
    message: PrivateMessage
    own_amounts: frozenset[int]  # exempt from the guard's "other member's limit" check


async def prepare(deps: Deps, session_id: uuid.UUID, plan: Plan) -> list[Itinerary]:
    """Build each member's itinerary DM. Runs outside the router lock.

    If detailed routing fails for someone, their DM falls back to the screening estimate.
    """
    tz = ZoneInfo(deps.settings.demo_timezone)
    async with session_factory()() as db:
        session = await db.get(SessionRow, session_id)
        if session is None:
            return []
        pid_map = load_pid_map(session)
        users = await queries.users_by_ids(db, list(pid_map.values()))
        origins = await vault.itinerary_context_for(db, list(pid_map.values()))

    personal = []
    for assignment in plan.assignments:
        user_id = pid_map.get(assignment.pid)
        user = users.get(user_id) if user_id else None
        if user is None:
            continue
        steps: list[RouteStep] = []
        # This member's own starting point → the venue: one Routes call per person.
        origin = origins.get(user.id)
        source = "none"
        if origin is not None:
            try:
                detail = await deps.routing.route(
                    origin, plan.candidate.location, assignment.mode, depart_at=assignment.leave_by
                )
                steps, source = detail.steps, detail.source
            except Exception:
                log.warning(kv("route_detail_failed", mode=assignment.mode.value), exc_info=True)
        log.info(
            kv(
                "member_route",
                user=user.id.hex[:8],
                mode=assignment.mode.value,
                has_origin=origin is not None,
                source=source,
                steps=len(steps),
            )
        )
        if origin is not None:  # coordinates: DEBUG only (CLAUDE.md rule 7)
            log.debug(
                kv("member_route_origin", user=user.id.hex[:8], lat=origin.lat, lng=origin.lng)
            )
        text = itinerary_text(
            plan, assignment, steps, tz, deps.settings.rideshare_pickup_wait_min, origin
        )
        personal.append(
            Itinerary(user.handle, PrivateMessage(text=text), own_amounts(plan, assignment))
        )
    return personal


def confirmation_message(deps: Deps, plan: Plan, label: str) -> GroupSafeMessage:
    arrive_local = plan.target_arrival.astimezone(ZoneInfo(deps.settings.demo_timezone))
    return GroupSafeMessage(text=copy.confirmation(label, plan.candidate.name, arrive_local))


async def send(
    deps: Deps,
    handles: list[str],
    confirmation: GroupSafeMessage,
    personal: list[Itinerary],
    guard: PrivacyGuard,
) -> None:
    """Group confirmation first, so each person's route lands right under it (§7.3).

    Every itinerary is checked for other members' private values before it goes out.
    """
    await send_group(deps.messaging, handles, confirmation, guard)
    for item in personal:
        await send_private(deps.messaging, item.handle, item.message, guard, item.own_amounts)
