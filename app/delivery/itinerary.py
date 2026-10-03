"""Winner delivery (§10): one private itinerary DM per person, then the group confirmation."""

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.conversation import copy
from app.db import queries
from app.db.tables import GroupRow, SessionRow
from app.deps import Deps
from app.logging import get_logger, kv
from app.messaging.outbound import send_group, send_private
from app.models.outbound import GroupSafeMessage, PrivateMessage
from app.models.plans import PersonAssignment, Plan
from app.models.routing import Mode, RouteStep
from app.optimizer.arrival import whole_minutes
from app.planning.pipeline import load_pid_map
from app.private import vault
from app.providers.costs import DRIVE_PARKING_MIN

log = get_logger(__name__)

MAX_STEPS_SHOWN = 5


def itinerary_text(
    plan: Plan,
    assignment: PersonAssignment,
    steps: list[RouteStep],
    tz: ZoneInfo,
    pickup_wait_min: int,
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
    food = (
        venue.est_cost_pp.value
        if venue.est_cost_pp.value is not None
        else assignment.venue_cost_usd
    )
    lines.append(copy.cost_line(arrive_local, food, assignment.fare_usd, assignment.mode.value))
    return "\n".join(lines)


async def deliver(
    deps: Deps, db: AsyncSession, group: GroupRow, session: SessionRow, plan: Plan, label: str
) -> None:
    tz = ZoneInfo(deps.settings.demo_timezone)
    pid_map = load_pid_map(session)
    users = await queries.users_by_ids(db, list(pid_map.values()))
    origins = await vault.itinerary_context_for(db, list(pid_map.values()))
    await db.commit()  # release the SQLite read lock before routing calls

    for assignment in plan.assignments:
        user_id = pid_map.get(assignment.pid)
        user = users.get(user_id) if user_id else None
        if user is None:
            continue
        steps: list[RouteStep] = []
        origin = origins.get(user.id)
        if origin is not None:
            try:
                detail = await deps.routing.route(
                    origin, plan.candidate.location, assignment.mode, depart_at=assignment.leave_by
                )
                steps = detail.steps
            except Exception:
                log.warning(kv("route_detail_failed", mode=assignment.mode.value), exc_info=True)
        text = itinerary_text(plan, assignment, steps, tz, deps.settings.rideshare_pickup_wait_min)
        await send_private(deps.messaging, user.handle, PrivateMessage(text=text))

    arrive_local: datetime = plan.target_arrival.astimezone(tz)
    confirmation = copy.confirmation(label, plan.candidate.name, arrive_local)
    await send_group(deps.messaging, group.chat_id, GroupSafeMessage(text=confirmation))
