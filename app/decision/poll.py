"""Text poll (§7.3): build the group-safe poll, record votes, pick a winner."""

import math
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversation import copy
from app.db.tables import VoteRow
from app.models.outbound import GroupPlanOption, GroupSafeMessage
from app.models.plans import Plan
from app.optimizer.facts import OPTION_LABELS
from app.providers.costs import tier_for_cost

LABELS = OPTION_LABELS
# Group-facing walking level from the longest walk in the plan (minutes).
LOW_WALK_MAX_MIN = 10
MODERATE_WALK_MAX_MIN = 20


def _title(plan: Plan) -> str:
    cand = plan.candidate
    if cand.cuisines:
        return f"{cand.name} — {cand.cuisines[0].replace('_', ' ').title()}"
    return cand.name


def _walking_level(plan: Plan) -> str:
    longest = max(a.walk_min for a in plan.assignments)
    if longest <= LOW_WALK_MAX_MIN:
        return "low"
    if longest <= MODERATE_WALK_MAX_MIN:
        return "moderate"
    return "high"


def _price_tier(plan: Plan) -> str:
    value = plan.candidate.est_cost_pp.value
    return "?" if value is None else tier_for_cost(value)


def build_options(
    plans: list[Plan], facts: list[dict], blurbs: list[str] | None = None
) -> list[GroupPlanOption]:
    """Aggregate, group-safe view of each plan: no per-person values.

    `blurbs` are the number-checked LLM explanations (§13.8); without them, each plan
    gets the fact-based template.
    """
    if not blurbs or len(blurbs) != len(plans):
        blurbs = [copy.plan_blurb(f["max_travel_min"]) for f in facts]
    options = []
    for plan, fact, blurb in zip(plans, facts, blurbs, strict=True):
        options.append(
            GroupPlanOption(
                label=fact["label"],
                title=_title(plan),
                max_travel_min=fact["max_travel_min"],
                walking_level=_walking_level(plan),
                price_tier=_price_tier(plan),
                arrival_window_min=fact["arrival_window_min"],
                blurb=blurb,
            )
        )
    return options


def build_poll_message(
    plans: list[Plan], facts: list[dict], blurbs: list[str] | None = None
) -> GroupSafeMessage:
    options = build_options(plans, facts, blurbs)
    return GroupSafeMessage(text=copy.poll_message(options), poll=options)


async def record_vote(
    db: AsyncSession, session_id: uuid.UUID, user_id: uuid.UUID, label: str
) -> None:
    vote = await db.get(VoteRow, (session_id, user_id))
    if vote is None:
        db.add(VoteRow(session_id=session_id, user_id=user_id, option_label=label))
    else:
        vote.option_label = label
        vote.ts = datetime.now(UTC)
    await db.flush()


async def tally(db: AsyncSession, session_id: uuid.UUID) -> dict[str, int]:
    counts: dict[str, int] = {}
    for vote in await db.scalars(select(VoteRow).where(VoteRow.session_id == session_id)):
        counts[vote.option_label] = counts.get(vote.option_label, 0) + 1
    return counts


def decided_winner(counts: dict[str, int], n_members: int) -> str | None:
    """First option to reach ⌈N/2⌉ votes. Earlier labels have better scores."""
    needed = max(1, math.ceil(n_members / 2))
    for label in LABELS:
        if counts.get(label, 0) >= needed:
            return label
    return None


def leader(counts: dict[str, int], n_options: int) -> str:
    """Most votes; ties (and no votes) go to the better-scored, earlier label."""
    labels = LABELS[:n_options]
    return max(labels, key=lambda label: (counts.get(label, 0), -labels.index(label)))
