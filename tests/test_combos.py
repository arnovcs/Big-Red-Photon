"""Very different asks ("pickleball" + "boba"): every option covers everyone, fairly."""

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.decision.poll import _title
from app.delivery.itinerary import itinerary_text
from app.models.candidates import DEFAULT_DURATION_MIN, Candidate
from app.models.conversation import ConstraintField as F
from app.models.conversation import ConstraintKind as K
from app.models.private import LatLng
from app.models.routing import Mode
from app.planning import combos
from app.providers.costs import tier_cost
from tests.optimizer_helpers import NOW, index, person, pref, prefs, run, trip

TZ = ZoneInfo("America/New_York")


def place(cid, category, tags, lat, lng, tier="$"):
    return Candidate(
        candidate_id=cid,
        name=cid.replace("_", " ").title(),
        category=category,
        cuisines=list(tags),
        location=LatLng(lat=lat, lng=lng),
        address=f"{cid} St",
        est_cost_pp=tier_cost(tier, source="google"),
        typical_duration_min=DEFAULT_DURATION_MIN[category],
        source="google",
    )


# Collegetown: a court with a boba shop around the corner; downtown: another of each.
COURT = place("reis_courts", "sports", ["pickleball", "tennis_court"], 42.4440, -76.4800)
BOBA = place("kung_fu_tea", "cafe", ["boba"], 42.4425, -76.4851)  # ~0.3 mi from COURT
FAR_COURT = place("cass_park", "sports", ["pickleball"], 42.4530, -76.5170)
FAR_BOBA = place("downtown_boba", "cafe", ["boba"], 42.4393, -76.4977)
PICKLEBALL_AND_BOBA = prefs(
    pref("p1", F.ACTIVITY, "pickleball", K.SOFT), pref("p2", F.CUISINE, "boba", K.SOFT)
)


def test_only_when_nothing_covers_everyone() -> None:
    wants = combos.asks_by_person(PICKLEBALL_AND_BOBA)
    assert combos.needs_combos([COURT, BOBA], wants)
    both = place("courts_with_boba", "sports", ["pickleball", "boba"], 42.44, -76.48)
    assert not combos.needs_combos([COURT, BOBA, both], wants)  # one place does both
    one_person = combos.asks_by_person(prefs(pref("p1", F.ACTIVITY, "pickleball", K.SOFT)))
    assert not combos.needs_combos([COURT, BOBA], one_person)
    # Overlapping asks ("asian" + "japanese") are a normal search, not combos.
    overlap = combos.asks_by_person(
        prefs(pref("p1", F.CUISINE, "asian", K.SOFT), pref("p2", F.CUISINE, "japanese", K.SOFT))
    )
    sushi = place("sushi", "food", ["sushi"], 42.44, -76.48)
    assert not combos.needs_combos([sushi], overlap)


def test_walkable_combos_court_first_then_boba_whoever_asked_first() -> None:
    plans = combos.build(
        [BOBA, FAR_BOBA, COURT, FAR_COURT], combos.asks_by_person(PICKLEBALL_AND_BOBA)
    )
    assert plans, "a walkable court + boba pair exists"
    best = plans[0]
    assert best.name == "Reis Courts + Kung Fu Tea"  # the closest pair, activity first
    assert best.extra_stops == [BOBA]
    assert best.location == COURT.location  # everyone meets at the first stop
    assert {"pickleball", "boba"} <= set(best.cuisines)
    assert best.est_cost_pp.value == Decimal(24)  # $12 + $12
    # Swap who asked for what: same plans, same order (no first-speaker advantage).
    swapped = prefs(
        pref("p1", F.CUISINE, "boba", K.SOFT), pref("p2", F.ACTIVITY, "pickleball", K.SOFT)
    )
    again = combos.build([BOBA, FAR_BOBA, COURT, FAR_COURT], combos.asks_by_person(swapped))
    assert [c.candidate_id for c in again] == [c.candidate_id for c in plans]


def test_nothing_walkable_takes_turns_between_the_asks() -> None:
    wants = combos.asks_by_person(PICKLEBALL_AND_BOBA)
    assert combos.build([FAR_COURT, BOBA], wants) == []  # ~2 mi apart
    mixed = combos.mixed([FAR_COURT, COURT, BOBA, FAR_BOBA], wants, 20)
    kinds = ["pickleball" if "pickleball" in c.cuisines else "boba" for c in mixed]
    assert kinds == ["pickleball", "boba", "pickleball", "boba"]


def test_a_combo_satisfies_both_people_in_the_optimizer() -> None:
    combo = combos.build([COURT, BOBA], combos.asks_by_person(PICKLEBALL_AND_BOBA))[0]
    estimates = index(
        trip("p1", combo.candidate_id, Mode.WALK, 8), trip("p2", combo.candidate_id, Mode.WALK, 9)
    )
    [plan] = run([combo], estimates, [person("p1", 50), person("p2", 50)], PICKLEBALL_AND_BOBA)
    assert all(a.burden.pref == 0 for a in plan.assignments)  # both fully satisfied
    assert _title(plan) == "Reis Courts + Kung Fu Tea — Pickleball + Boba"
    text = itinerary_text(plan, plan.assignments[0], [], TZ, 6)
    assert "then 🚶 ~" in text and "walk together to Kung Fu Tea, kung_fu_tea St" in text
    assert plan.target_arrival - NOW < timedelta(hours=2)
    assert isinstance(plan.target_arrival, datetime)


def test_combo_options_vary_the_first_stop() -> None:
    from app.optimizer import select

    near_court2 = place("teagle_courts", "sports", ["pickleball"], 42.4446, -76.4790)
    boba2 = place("boba_two", "cafe", ["boba"], 42.4430, -76.4840)
    built = combos.build(
        [COURT, near_court2, BOBA, boba2], combos.asks_by_person(PICKLEBALL_AND_BOBA)
    )
    people = [person("p1", 50), person("p2", 50)]
    estimates = index(*(trip(p, c.candidate_id, Mode.WALK, 8) for c in built for p in ("p1", "p2")))
    picked = select(run(built, estimates, people, PICKLEBALL_AND_BOBA), k=2)
    assert len({p.candidate.candidate_id.split("+")[0] for p in picked}) == 2


def test_free_court_plus_priced_boba_shows_the_boba_price() -> None:
    from types import SimpleNamespace

    from app.decision.poll import _price_tier

    free_court = COURT.model_copy(
        update={
            "est_cost_pp": COURT.est_cost_pp.model_copy(
                update={
                    "value": Decimal(0),
                    "low": Decimal(0),
                    "high": Decimal(0),
                    "status": "unknown",
                }
            )
        }
    )
    combo = combos.build([free_court, BOBA], combos.asks_by_person(PICKLEBALL_AND_BOBA))[0]
    assert combo.est_cost_pp.value == Decimal(12) and combo.est_cost_pp.status == "estimated"
    assert _price_tier(SimpleNamespace(candidate=combo)) == "$"  # not "check website"
