"""Preference extraction: the LLM wire schema and the §6.3 validation rules.

Pure: no I/O. The LLM provider sends `WIRE_SCHEMA`, then `validate()` turns the raw
JSON into a trusted `GroupPreferences`. Every rule here runs in code, after the
LLM, so a bad or invented constraint never reaches the optimizer.

The wire schema keeps `value` a plain string (no unions, which structured output
handles poorly); `validate()` converts it to the type each field needs.
"""

import re
from typing import Any

from app.models.conversation import (
    ConstraintField,
    ConstraintKind,
    ExtractedConstraint,
    GroupPreferences,
    PseudonymousMessage,
)
from app.models.routing import Mode

HARD_MIN_CONFIDENCE = 0.7

CATEGORIES = ["food", "bar", "cafe", "dessert", "activity", "event"]
CATEGORY_SYNONYMS = {
    "restaurant": "food",
    "dinner": "food",
    "lunch": "food",
    "breakfast": "food",
    "brunch": "food",
    "meal": "food",
    "eat": "food",
    "eating": "food",
    "coffee": "cafe",
    "café": "cafe",
    "tea": "cafe",
    "drinks": "bar",
    "pub": "bar",
    "beer": "bar",
    "ice cream": "dessert",
    "sweets": "dessert",
    "dessert place": "dessert",
    "movie": "activity",
    "movies": "activity",
    "cinema": "activity",
    "bowling": "activity",
    "games": "activity",
    "concert": "event",
    "show": "event",
}
MODE_SYNONYMS = {
    "walking": "walk",
    "on foot": "walk",
    "biking": "bike",
    "bicycle": "bike",
    "cycling": "bike",
    "car": "drive",
    "driving": "drive",
    "uber": "rideshare",
    "lyft": "rideshare",
    "ride": "rideshare",
    "taxi": "rideshare",
}
NUMERIC_FIELDS = {ConstraintField.MAX_WALK_MIN, ConstraintField.MAX_TRAVEL_MIN}
TIME_FIELDS = {ConstraintField.AVAILABLE_UNTIL, ConstraintField.AVAILABLE_FROM}
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

WIRE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "constraints": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pid": {"type": "string"},
                    "field": {"type": "string", "enum": [f.value for f in ConstraintField]},
                    "value": {"type": "string"},
                    "polarity": {"type": "string", "enum": ["want", "avoid"]},
                    "kind": {"type": "string", "enum": [k.value for k in ConstraintKind]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "evidence_msg_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "pid",
                    "field",
                    "value",
                    "polarity",
                    "kind",
                    "confidence",
                    "evidence_msg_ids",
                ],
            },
        },
        "group_intent": {"type": "string", "enum": ["food", "activity", "either", "unknown"]},
        "unresolved": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["constraints", "group_intent"],
}


def _category(value: str) -> str | None:
    v = value.strip().lower()
    if v in CATEGORIES:
        return v
    return CATEGORY_SYNONYMS.get(v)


def _mode(value: str) -> str | None:
    v = value.strip().lower()
    if v in Mode._value2member_map_:
        return v
    return MODE_SYNONYMS.get(v)


def _hhmm(value: str) -> str | None:
    match = _HHMM.match(value.strip())
    if match is None:
        return None
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def _value(field: ConstraintField, raw: Any) -> str | float | list[str] | None:
    """The field's value in the type the optimizer reads, or None to drop it."""
    text = str(raw).strip()
    if not text:
        return None
    if field in NUMERIC_FIELDS:
        try:
            number = float(text)
        except ValueError:
            return None
        return number if number > 0 else None
    if field == ConstraintField.NOVELTY:
        try:
            return min(max(float(text), 0.0), 1.0)
        except ValueError:
            return None
    if field in TIME_FIELDS:
        return _hhmm(text)
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if field == ConstraintField.CATEGORY:
        mapped = [c for p in parts if (c := _category(p))]
    elif field == ConstraintField.MODE_PREFERENCE:
        mapped = [m for p in parts if (m := _mode(p))]
    else:  # cuisine
        mapped = [p.lower() for p in parts]
    if not mapped:
        return None
    return mapped[0] if len(mapped) == 1 else mapped


def validate(raw: dict, transcript: list[PseudonymousMessage]) -> GroupPreferences:
    """Apply §6.3: cited messages must exist and come from that person; weak HARDs
    become SOFT; times must be HH:MM; categories map to the allowed set. Anything
    that fails is dropped, never guessed."""
    sender_of = {m.message_id: m.pid for m in transcript}
    constraints: list[ExtractedConstraint] = []
    for item in raw.get("constraints") or []:
        if not isinstance(item, dict):
            continue
        try:
            field = ConstraintField(item.get("field"))
            kind = ConstraintKind(item.get("kind"))
            confidence = min(max(float(item.get("confidence", 0)), 0.0), 1.0)
        except (ValueError, TypeError):
            continue
        pid = item.get("pid")
        evidence = [str(e) for e in item.get("evidence_msg_ids") or []]
        if not evidence or any(sender_of.get(e) != pid for e in evidence):
            continue  # invented, missing, or attributed to the wrong person
        value = _value(field, item.get("value", ""))
        if value is None:
            continue
        if kind == ConstraintKind.HARD and confidence < HARD_MIN_CONFIDENCE:
            kind = ConstraintKind.SOFT
        polarity = item.get("polarity") if item.get("polarity") in ("want", "avoid") else "want"
        constraints.append(
            ExtractedConstraint(
                pid=pid,
                field=field,
                value=value,
                polarity=polarity,
                kind=kind,
                confidence=confidence,
                evidence_msg_ids=evidence,
            )
        )
    intent = raw.get("group_intent")
    if intent not in ("food", "activity", "either", "unknown"):
        intent = "unknown"
    unresolved = [str(u) for u in raw.get("unresolved") or [] if str(u).strip()]
    return GroupPreferences(constraints=constraints, group_intent=intent, unresolved=unresolved)
