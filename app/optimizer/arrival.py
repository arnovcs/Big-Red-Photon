"""Synchronized arrival (§13.2): T_target and per-person leave_by."""

import math
from datetime import datetime, time, timedelta

from app.models.conversation import ConstraintField, GroupPreferences
from app.optimizer.params import OptimizerParams


def parse_hhmm(value: object) -> time | None:
    if not isinstance(value, str):
        return None
    try:
        hour, minute = value.strip().split(":")
        return time(int(hour), int(minute))
    except ValueError:
        return None


def ready_time(
    pid: str, preferences: GroupPreferences, now: datetime, params: OptimizerParams
) -> datetime:
    """now + buffer, or the stated available_from if later. `now` must be local time."""
    ready = now + timedelta(minutes=params.ready_buffer_min)
    for c in preferences.constraints:
        if c.pid == pid and c.field == ConstraintField.AVAILABLE_FROM and c.polarity == "want":
            t = parse_hhmm(c.value)
            if t is not None:
                stated = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
                ready = max(ready, stated)
    return ready


def ceil_minute(dt: datetime) -> datetime:
    if dt.second == 0 and dt.microsecond == 0:
        return dt
    return dt.replace(second=0, microsecond=0) + timedelta(minutes=1)


def target_arrival(ready: dict[str, datetime], duration_min: dict[str, float]) -> datetime:
    """T_target = max_i (ready_i + duration_i), rounded up to the next minute."""
    latest = max(ready[pid] + timedelta(minutes=duration_min[pid]) for pid in duration_min)
    return ceil_minute(latest)


def leave_by(target: datetime, duration_min: float) -> datetime:
    return target - timedelta(minutes=duration_min)


def whole_minutes(duration_min: float) -> int:
    return math.ceil(duration_min - 1e-9)
