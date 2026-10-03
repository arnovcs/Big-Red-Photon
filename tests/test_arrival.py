"""Synchronized arrival (§13.2): T_target and leave_by for mixed modes."""

from datetime import timedelta

from app.models.conversation import ConstraintField as F
from app.models.conversation import ConstraintKind as K
from app.models.routing import Mode
from app.optimizer import OptimizerParams
from app.optimizer.arrival import ceil_minute, leave_by, ready_time, target_arrival
from tests.optimizer_helpers import NOW, index, person, pref, prefs, run, trip, venue


def test_target_is_latest_arrival_rounded_up_and_everyone_leaves_in_time() -> None:
    v = venue("v")
    people = [person("walker"), person("biker", bike=True), person("rider", 100)]
    estimates = index(
        trip("walker", "v", Mode.WALK, 12.4),
        trip("biker", "v", Mode.BIKE, 4.2),
        trip("rider", "v", Mode.RIDESHARE, 9.5, fare=8),
        trip("rider", "v", Mode.WALK, 40),
    )
    (plan,) = run([v], estimates, people)
    by_pid = {a.pid: a for a in plan.assignments}
    assert by_pid["biker"].mode == Mode.BIKE
    assert by_pid["rider"].mode == Mode.RIDESHARE

    # Everyone is ready at 18:05; the walker is slowest (12.4 min) → 18:17:24 → 18:18.
    assert plan.target_arrival == NOW.replace(hour=18, minute=18)
    for a in plan.assignments:
        assert a.arrive_at == plan.target_arrival
        assert a.leave_by + timedelta(minutes=a.travel_min) == plan.target_arrival
    # The fastest traveller leaves last.
    assert by_pid["biker"].leave_by > by_pid["rider"].leave_by > by_pid["walker"].leave_by


def test_available_from_delays_that_person_and_so_the_group() -> None:
    params = OptimizerParams()
    later = prefs(pref("p2", F.AVAILABLE_FROM, "18:30", K.SOFT))
    ready = {pid: ready_time(pid, later, NOW, params) for pid in ("p1", "p2")}
    assert ready["p1"] == NOW + timedelta(minutes=5)
    assert ready["p2"] == NOW.replace(minute=30)
    target = target_arrival(ready, {"p1": 20, "p2": 5})
    assert target == NOW.replace(minute=35)
    assert leave_by(target, 20) == NOW.replace(minute=15)


def test_ceil_minute_only_rounds_partial_minutes() -> None:
    assert ceil_minute(NOW) == NOW
    assert ceil_minute(NOW + timedelta(seconds=1)) == NOW + timedelta(minutes=1)
