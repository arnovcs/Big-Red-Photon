"""§13.7 optimizer tests, the other hard filters, facts, and the demo outcome."""

import asyncio
import json
import random
from datetime import timedelta
from decimal import Decimal

from app.models.candidates import Uncertain
from app.models.conversation import ConstraintField as F
from app.models.conversation import ConstraintKind as K
from app.models.private import TravelModes
from app.models.routing import Mode
from app.optimizer import OptimizerParams, rank, select
from app.optimizer.facts import nothing_fits_hint, plan_facts
from app.optimizer.feasibility import allowed_modes
from app.providers.mock.routing import MockRouting
from app.settings import Settings
from tests.optimizer_helpers import HERE, NOW, index, person, pref, prefs, run, trip, venue
from tests.places_stub import StubPlaces

WALK, BIKE, DRIVE, RIDE = Mode.WALK, Mode.BIKE, Mode.DRIVE, Mode.RIDESHARE


def ids(plans) -> list[str]:
    return [p.plan_id for p in plans]


def modes_of(plan) -> dict[str, Mode]:
    return {a.pid: a.mode for a in plan.assignments}


# --- 1. Budget ---------------------------------------------------------------------


def test_budget_filter_drops_plan_one_person_cannot_afford() -> None:
    fancy = venue("fancy", tier="$$$")  # high end $60
    cheap = venue("cheap", tier="$")  # high end $15
    people = [person("p1", 100), person("p2", 100), person("p3", 20)]
    estimates = index(
        *(trip(p.pid, "fancy", WALK, 5) for p in people),  # closest for everyone
        *(trip(p.pid, "cheap", WALK, 25) for p in people),
    )
    plans = run([fancy, cheap], estimates, people)
    assert ids(plans) == ["cheap"]
    assert all(a.total_cost_usd <= 20 for a in plans[0].assignments if a.pid == "p3")


def test_unknown_price_must_fit_within_a_safety_margin() -> None:
    unknown = Uncertain[Decimal](value=Decimal(25), status="unknown", source="google")
    mystery = venue("mystery", cost=unknown)
    estimates = index(trip("p1", "mystery", WALK, 5), trip("p2", "mystery", WALK, 5))

    # 25 > 0.8 × 30: rejected even though 25 ≤ 30.
    assert run([mystery], estimates, [person("p1", 30), person("p2", 100)]) == []
    # 25 ≤ 0.8 × 40: allowed, and flagged.
    (plan,) = run([mystery], estimates, [person("p1", 40), person("p2", 100)])
    assert plan.risk_flags == ["price unknown"]
    assert all(a.cost_uncertain for a in plan.assignments)

    # No typical value at all: nothing to check budgets against, so it's dropped.
    blank = venue("blank", cost=Uncertain[Decimal](value=None, status="unknown", source="x"))
    estimates = index(trip("p1", "blank", WALK, 5), trip("p2", "blank", WALK, 5))
    assert run([blank], estimates, [person("p1", 100), person("p2", 100)]) == []


# --- 2. Veto -------------------------------------------------------------------


def test_veto_removes_matching_cuisine_and_category() -> None:
    sushi = venue("sushi", cuisines=("japanese", "sushi"))
    bar = venue("bar", category="bar")
    tacos = venue("tacos", cuisines=("mexican",))
    people = [person("p1"), person("p2")]
    estimates = index(
        *(trip(p.pid, v, WALK, 10) for p in people for v in ("sushi", "bar", "tacos"))
    )
    vetoes = prefs(
        pref("p1", F.CUISINE, "sushi", K.VETO, polarity="avoid"),
        pref("p2", F.CATEGORY, "bar", K.VETO, polarity="avoid"),
    )
    assert ids(run([sushi, bar, tacos], estimates, people, vetoes)) == ["tacos"]


# --- 3. Hours ------------------------------------------------------------------


def test_hours_known_closed_or_closing_too_soon() -> None:
    closed = venue("closed", open_at_target="closed")
    closing = venue("closing", closes_at=NOW + timedelta(minutes=45))  # visit runs to ~19:15
    late = venue("late", closes_at=NOW + timedelta(hours=4))
    people = [person("p1"), person("p2")]
    estimates = index(
        *(trip(p.pid, v, WALK, 10) for p in people for v in ("closed", "closing", "late"))
    )
    assert ids(run([closed, closing, late], estimates, people)) == ["late"]


# --- 4. Available until (HARD) -----------------------------------------------------


def test_hard_available_until_removes_plan_that_ends_too_late() -> None:
    far = venue("far")  # 60 min each way: 18:05 + 60 → 19:05, eat until 20:05, home 21:05
    near = venue("near")  # 10 min: arrive 18:15, home by 19:25
    people = [person("p1"), person("p2")]
    estimates = index(
        *(trip(p.pid, "far", WALK, 60) for p in people),
        *(trip(p.pid, "near", WALK, 10) for p in people),
    )
    back_by_8 = prefs(pref("p2", F.AVAILABLE_UNTIL, "20:00", K.HARD))
    assert ids(run([far, near], estimates, people, back_by_8)) == ["near"]
    # The same wish as SOFT doesn't filter.
    soft = prefs(pref("p2", F.AVAILABLE_UNTIL, "20:00", K.SOFT))
    assert set(ids(run([far, near], estimates, people, soft))) == {"far", "near"}


def test_available_until_earlier_than_now_means_tomorrow() -> None:
    v = venue("v")
    people = [person("p1"), person("p2")]
    estimates = index(*(trip(p.pid, "v", WALK, 10) for p in people))
    back_by_1am = prefs(pref("p1", F.AVAILABLE_UNTIL, "01:00", K.HARD))
    assert ids(run([v], estimates, people, back_by_1am)) == ["v"]


# --- 5–6. Max walk / max travel (HARD) ---------------------------------------------


def test_hard_max_walk_switches_mode_or_drops_venue() -> None:
    v = venue("v")
    biker = person("p1", bike=True)
    walker = person("p2")  # walk + ride-share only
    estimates = index(
        trip("p1", "v", WALK, 25),
        trip("p1", "v", BIKE, 8),
        trip("p2", "v", WALK, 12),
    )
    no_long_walks = prefs(pref("p1", F.MAX_WALK_MIN, 15, K.HARD))
    (plan,) = run([v], estimates, [biker, walker], no_long_walks)
    assert modes_of(plan)["p1"] == BIKE

    walker_limit = prefs(pref("p2", F.MAX_WALK_MIN, 10, K.HARD))
    assert run([v], estimates, [biker, walker], walker_limit) == []


def test_hard_max_travel_filters_and_soft_does_not() -> None:
    v = venue("v")
    people = [person("p1"), person("p2")]
    estimates = index(trip("p1", "v", WALK, 40), trip("p2", "v", WALK, 10))
    assert run([v], estimates, people, prefs(pref("p1", F.MAX_TRAVEL_MIN, 30, K.HARD))) == []
    assert ids(run([v], estimates, people, prefs(pref("p1", F.MAX_TRAVEL_MIN, 30, K.SOFT)))) == [
        "v"
    ]


# --- 7. Mode -------------------------------------------------------------------


def test_mode_must_be_allowed_and_not_refused() -> None:
    v = venue("v")
    people = [person("p1", 100, drive=True), person("p2", 100)]
    estimates = index(
        trip("p1", "v", WALK, 40),
        trip("p1", "v", DRIVE, 8, fare=4),
        trip("p1", "v", RIDE, 12, fare=9),
        trip("p2", "v", WALK, 10),
        trip("p2", "v", DRIVE, 3, fare=4),  # p2 has no car: never used
    )
    (plan,) = run([v], estimates, people)
    assert modes_of(plan) == {"p1": DRIVE, "p2": WALK}

    no_driving = prefs(pref("p1", F.MODE_PREFERENCE, "drive", K.HARD, polarity="avoid"))
    assert modes_of(run([v], estimates, people, no_driving)[0])["p1"] == RIDE

    walk_only = prefs(pref("p1", F.MODE_PREFERENCE, "walk", K.HARD))
    assert modes_of(run([v], estimates, people, walk_only)[0])["p1"] == WALK


# --- Fairness (§13.7) ----------------------------------------------------------


def test_fairness_lambda_trades_worst_off_against_average() -> None:
    """X: less total travel but one person walks 45 min. Y: a bit more total, max 20."""
    x, y = venue("x"), venue("y")
    people = [person("p1"), person("p2"), person("p3")]
    estimates = index(
        trip("p1", "x", WALK, 45),
        trip("p2", "x", WALK, 5),
        trip("p3", "x", WALK, 5),  # total 55
        trip("p1", "y", WALK, 20),
        trip("p2", "y", WALK, 19),
        trip("p3", "y", WALK, 19),  # total 58
    )
    assert ids(run([x, y], estimates, people, lam=0.5)) == ["y", "x"]
    assert ids(run([x, y], estimates, people, lam=0.0)) == ["x", "y"]


# --- Ride-share budget case (§13.7) --------------------------------------------------


def test_rideshare_only_for_those_it_helps_and_who_can_afford_it() -> None:
    v = venue("v", tier="$")  # high end $15
    tight = person("tight", 20)  # $15 + $9 ride = $24 > $20
    roomy = person("roomy", 60)
    estimates = index(
        trip("tight", "v", WALK, 30),
        trip("tight", "v", RIDE, 8, fare=9),
        trip("roomy", "v", WALK, 30),
        trip("roomy", "v", RIDE, 8, fare=9),
    )
    (plan,) = run([v], estimates, [tight, roomy])
    assert modes_of(plan) == {"tight": WALK, "roomy": RIDE}

    # Ride-share genuinely lowers J for "roomy": without it, the plan scores worse.
    no_ride = person("roomy", 60, rideshare=False)
    (walk_plan,) = run([v], estimates, [tight, no_ride])
    assert walk_plan.score.J > plan.score.J

    # If walking breaks the tight person's HARD time limit, nothing is feasible.
    hurry = prefs(pref("tight", F.MAX_TRAVEL_MIN, 20, K.HARD))
    assert run([v], estimates, [tight, roomy], hurry) == []


# --- Diversity (§13.6) ---------------------------------------------------------------


def _ranked_for_diversity():
    sushi = [venue(f"sushi{i}", cuisines=("sushi",)) for i in (1, 2, 3)]
    korean = venue("korean", cuisines=("korean",))
    cafe = venue("cafe", category="cafe", cuisines=("coffee",))
    people = [person("p1"), person("p2")]
    minutes = {"sushi1": 5, "sushi2": 6, "sushi3": 7, "korean": 15, "cafe": 20}
    estimates = index(*(trip(p.pid, c, WALK, m) for p in people for c, m in minutes.items()))
    return run([*sushi, korean, cafe], estimates, people)


def test_diversity_three_sushi_places_do_not_fill_the_poll() -> None:
    ranked = _ranked_for_diversity()
    assert ids(ranked)[:3] == ["sushi1", "sushi2", "sushi3"]
    assert ids(select(ranked, k=3)) == ["sushi1", "korean", "cafe"]


def test_diversity_falls_back_to_next_best_when_nothing_differs() -> None:
    ranked = [p for p in _ranked_for_diversity() if p.plan_id.startswith("sushi")]
    assert ids(select(ranked, k=3)) == ["sushi1", "sushi2", "sushi3"]


# --- Determinism ---------------------------------------------------------------------


def test_same_inputs_same_order_regardless_of_input_order() -> None:
    venues = [venue(f"v{i}", cuisines=(c,)) for i, c in enumerate(["a", "b", "c", "d", "e"])]
    people = [person("p1", bike=True), person("p2"), person("p3", 100, drive=True)]
    rng = random.Random(7)
    estimates = index(
        *(
            trip(p.pid, v.candidate_id, m, rng.uniform(5, 30), fare=rng.choice([0, 0, 6, 9]))
            for p in people
            for v in venues
            for m in allowed_modes(p.modes)
        )
    )
    first = run(venues, estimates, people)
    assert ids(run(list(reversed(venues)), estimates, people)) == ids(first)
    assert [p.model_dump() for p in run(venues, estimates, people)] == [
        p.model_dump() for p in first
    ]


# --- Facts and the "nothing fits" hint (§13.8) ----------------------------------------


def test_facts_are_group_safe_aggregates() -> None:
    a = venue("koko", cuisines=("korean",))
    b = venue("viva", cuisines=("mexican",))
    people = [person("p1", 30), person("p2", 50)]
    estimates = index(
        trip("p1", "koko", WALK, 22),
        trip("p2", "koko", WALK, 9.2),
        trip("p1", "viva", WALK, 30.5),
        trip("p2", "viva", WALK, 12),
    )
    preferences = prefs(
        pref("p1", F.CUISINE, "korean", K.SOFT),
        pref("p2", F.CUISINE, "sushi", K.VETO, polarity="avoid"),
        pref("p2", F.NOVELTY, 1.0, K.SOFT),  # "something new": not scored (no data)
        pref("p1", F.AVAILABLE_UNTIL, "21:00", K.HARD),
    )
    plans = select(run([a, b], estimates, people, preferences), k=3)
    facts = plan_facts(plans, preferences)
    assert [f["label"] for f in facts] == ["A", "B"]
    koko = next(f for f in facts if f["venue"] == "Koko")
    assert koko["max_travel_min"] == 22
    assert koko["fits_all_budgets"] is True
    assert koko["vetoes_respected"] == ["sushi"]
    assert koko["matches_group_wants"] == ["korean"]
    viva = next(f for f in facts if f["venue"] == "Viva")
    assert viva["matches_group_wants"] == []
    assert facts[0]["next_best_max_travel_min"] == facts[1]["max_travel_min"]
    assert facts[-1]["next_best_max_travel_min"] is None
    # No pseudonyms, limits, or personal times anywhere in the facts.
    dumped = json.dumps(facts)
    for secret in ("p1", "p2", "30.00", "50", "21:00"):
        assert secret not in dumped


def test_nothing_fits_hint_names_the_narrowest_soft_venue_preference() -> None:
    venues = [
        venue("a", cuisines=("korean",)),
        venue("b", cuisines=("korean",)),
        venue("c", cuisines=("thai",)),
        venue("d", category="cafe", cuisines=("coffee",)),
    ]
    preferences = prefs(
        pref("p1", F.CUISINE, "korean", K.SOFT),  # 2 of 4 venues
        pref("p2", F.CUISINE, "thai", K.SOFT, confidence=0.5),  # 1 of 4: most binding
        pref("p3", F.AVAILABLE_UNTIL, "21:00", K.SOFT),  # personal: never named
        pref("p3", F.MAX_TRAVEL_MIN, 10, K.SOFT),  # personal: never named
        pref("p1", F.CUISINE, "sushi", K.VETO, polarity="avoid"),  # not SOFT
    )
    hint = nothing_fits_hint(preferences, venues)
    assert hint == "Being open to more than thai could help."
    assert nothing_fits_hint(prefs(pref("p1", F.MAX_TRAVEL_MIN, 10, K.SOFT)), venues) is None
    # Values that aren't plain words (numbers, money) are never echoed.
    odd = prefs(pref("p1", F.CUISINE, "$30 max", K.SOFT))
    assert nothing_fits_hint(odd, venues) is None


# --- Demo realism ------------------------------------------------------------------------


def test_demo_origins_sam_rides_and_jordan_walks_with_default_weights() -> None:
    """§18 with realistic starts. Default OptimizerParams: nothing tuned for this outcome."""
    settings = Settings(_env_file=None)
    places = StubPlaces()
    starts = {"maya": "collegetown", "sam": "north campus", "jordan": "downtown"}
    people = {
        "maya": person("maya", 30, bike=True),
        "sam": person("sam", 50),
        "jordan": person("jordan", 15),
    }
    for pid, text in starts.items():
        people[pid] = people[pid].model_copy(
            update={"origin": asyncio.run(places.geocode(text, HERE)).location}
        )
    preferences = prefs(
        pref("maya", F.CATEGORY, "food", K.INFERRED),
        pref("sam", F.CUISINE, "sushi", K.VETO, polarity="avoid"),
        pref("jordan", F.AVAILABLE_UNTIL, "21:00", K.HARD),
        pref("sam", F.NOVELTY, 1.0, K.SOFT, confidence=0.6),
    )
    origins = {pid: p.origin for pid, p in people.items()}

    async def plan(constraints):
        center_lat = sum(o.lat for o in origins.values()) / len(origins)
        center_lng = sum(o.lng for o in origins.values()) / len(origins)
        center = origins["maya"].model_copy(update={"lat": center_lat, "lng": center_lng})
        candidates = await places.search_nearby(center, 2500, ["food", "cafe", "dessert"], NOW)
        estimates = await MockRouting(settings).matrix(
            origins,
            {c.candidate_id: c.location for c in candidates},
            {pid: set(allowed_modes(c.modes)) for pid, c in constraints.items()},
            NOW,
        )
        index_ = {(e.origin_pid, e.candidate_id, e.mode): e for e in estimates}
        return rank(candidates, index_, constraints, preferences, NOW, OptimizerParams())

    best = asyncio.run(plan(people))[0]
    assert modes_of(best) == {"maya": BIKE, "sam": RIDE, "jordan": WALK}

    # Sam's ride-share genuinely lowers J at this venue...
    no_ride = dict(people)
    no_ride["sam"] = people["sam"].model_copy(update={"modes": TravelModes(rideshare=False)})
    same_venue = next(p for p in asyncio.run(plan(no_ride)) if p.plan_id == best.plan_id)
    assert same_venue.score.J > best.score.J
    # ...and Jordan can't take one: any fare on top of a $15 meal breaks a $15 limit.
    jordan = next(a for a in best.assignments if a.pid == "jordan")
    assert jordan.venue_cost_usd + settings.rideshare_min_fare_usd > Decimal(15)


# --- Unknown prices: no guessing by category ------------------------------------------------


def test_unknown_food_price_uses_nearby_median_with_the_safety_margin() -> None:
    from app.optimizer.enumerate import unknown_price_stand_in

    unknown = Uncertain[Decimal](value=None, status="unknown", source="google")
    unpriced = venue("diner", cost=unknown)
    cheap, pricey = venue("cheap", tier="$"), venue("pricey", tier="$$$")  # highs 15 and 60
    assert unknown_price_stand_in([unpriced, cheap, pricey]) == Decimal("37.5")
    assert unknown_price_stand_in([unpriced]) is None

    estimates = index(*(trip("p1", c, WALK, 5) for c in ("diner", "cheap", "pricey")))
    # $50 limit: stand-in 37.5 ≤ 80% × 50, so the unpriced diner is a real option, flagged.
    plans = {p.plan_id: p for p in run([unpriced, cheap, pricey], estimates, [person("p1", 50)])}
    assert "price unknown" in plans["diner"].risk_flags
    # $40 limit: 37.5 > 80% × 40, so it's left out rather than risk the budget.
    plans = {p.plan_id: p for p in run([unpriced, cheap, pricey], estimates, [person("p1", 40)])}
    assert "diner" not in plans
    # Nothing nearby has a price: nothing to check a budget against, so it's left out.
    assert run([unpriced], index(trip("p1", "diner", WALK, 5)), [person("p1", 50)]) == []


def test_unpriced_activities_count_as_free_and_are_flagged() -> None:
    unknown = Uncertain[Decimal](value=None, status="unknown", source="google")
    court = venue("court", category="sports", cost=unknown)
    museum = venue("museum", category="activity", cost=unknown)
    estimates = index(trip("p1", "court", WALK, 5), trip("p1", "museum", WALK, 5))
    plans = {p.plan_id: p for p in run([court, museum], estimates, [person("p1", 5)])}
    assert set(plans) == {"court", "museum"}  # even a $5 budget: no price to break it
    assert all("price unknown" in p.risk_flags for p in plans.values())


def test_poll_and_itinerary_say_price_unknown() -> None:
    from app.conversation import copy
    from app.decision.poll import _price_tier

    unpriced = venue(
        "park",
        category="activity",
        cost=Uncertain[Decimal](value=None, status="unknown", source="google"),
    )
    cheap = venue("cheap", tier="$")
    estimates = index(trip("p1", "park", WALK, 5), trip("p1", "cheap", WALK, 5))
    plan = next(
        p for p in run([unpriced, cheap], estimates, [person("p1", 50)]) if p.plan_id == "park"
    )
    assert _price_tier(plan) == "?"
    line = copy.cost_line(NOW, None, Decimal(0), "walk")
    assert "price unknown" in line and "$" not in line
