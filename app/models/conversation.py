"""Conversation and preference models (§6.3)."""

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.private import LatLng


class ChatMessage(BaseModel):
    message_id: str
    chat_id: str
    sender_user_id: UUID
    text: str
    ts: datetime
    is_group: bool


class PseudonymousMessage(BaseModel):
    """What the LLM sees."""

    message_id: str
    pid: str
    text: str
    ts_local: str  # "18:02"


class ConstraintKind(StrEnum):
    HARD = "hard"  # "I have to be back by 9"
    SOFT = "soft"  # "I'd prefer Korean"
    VETO = "veto"  # "no sushi"
    INFERRED = "inferred"  # "I'm starving" → food, soon


class ConstraintField(StrEnum):
    MAX_WALK_MIN = "max_walking_minutes"
    MAX_TRAVEL_MIN = "max_travel_minutes"
    AVAILABLE_UNTIL = "available_until"  # "HH:MM" local
    AVAILABLE_FROM = "available_from"  # "HH:MM" local
    CUISINE = "cuisine"
    CATEGORY = "category"  # food | bar | cafe | dessert | activity | event
    NOVELTY = "novelty"  # 0..1
    MODE_PREFERENCE = "mode_preference"  # "walk" | "bike" | "drive" | "rideshare" with polarity


class ExtractedConstraint(BaseModel):
    pid: str
    field: ConstraintField
    value: str | float | list[str]
    polarity: Literal["want", "avoid"] = "want"
    kind: ConstraintKind
    confidence: float = Field(ge=0, le=1)
    evidence_msg_ids: list[str]


class GroupPreferences(BaseModel):
    constraints: list[ExtractedConstraint]
    group_intent: Literal["food", "activity", "either", "unknown"]
    unresolved: list[str] = []


class InboundMessage(BaseModel):
    """A message from the bridge webhook (§9.1) or the simulator (§14.3)."""

    message_id: str
    chat_id: str
    is_group: bool
    sender_handle: str
    sender_name: str | None = None
    text: str
    ts: datetime
    location: LatLng | None = None
