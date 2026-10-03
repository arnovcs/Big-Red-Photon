"""Very different asks ("pickleball" + "boba"): plans that cover everyone.

Pure: no I/O. Used by the pipeline only when no single venue satisfies every person who
asked for something specific. Then each option is a short walkable chain of venues, one
per distinct ask (a court, then a boba shop around the corner), so every option covers
everyone. Stop order is by kind (things to do before food and drink, then alphabetical),
never by who asked first or most often. If nothing can be chained within walking
distance, `mixed` takes turns between the asks instead.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.models.candidates import Candidate, Uncertain
from app.models.conversation import ConstraintField, ConstraintKind, GroupPreferences
from app.optimizer import cuisines as cuisine_families
from app.providers.mock.routing import haversine_mi

# Broad words that aren't a specific ask ("food", "activity" match too much).
GENERIC_CATEGORIES = {"food", "activity", "event"}
NON_FOOD = {"activity", "sports", "event"}
# Category words: a stop's tag prefers what was actually asked ("pickleball" over "sports").
KIND_WORDS = {"sports", "cafe", "bar", "dessert"}
MAX_WALK_BETWEEN_MI = 0.5  # ~10 min on foot between stops
WALK_MPH = 3.0
DETOUR = 1.3  # straight line → street distance
PER_ASK = 20  # candidates per ask considered (all of a search's results)
MAX_COMBOS = 20


@dataclass(frozen=True)
class Ask:
    """One specific thing someone asked for."""

    field: ConstraintField  # ACTIVITY, CUISINE, or CATEGORY
    value: str

    def covers(self, c: Candidate) -> bool:
        if self.field == ConstraintField.ACTIVITY:
            return self.value in c.cuisines
        if self.field == ConstraintField.CUISINE:
            return cuisine_families.matches([self.value], c.cuisines)
        return c.category == self.value

    @property
    def label(self) -> str:
        return self.value.replace("_", " ")


def asks_by_person(preferences: GroupPreferences) -> dict[str, set[Ask]]:
    """Each person's specific wants: activities, cuisines, and non-generic categories."""
    fields = (ConstraintField.ACTIVITY, ConstraintField.CUISINE, ConstraintField.CATEGORY)
    out: dict[str, set[Ask]] = {}
    for c in preferences.constraints:
        if c.field not in fields or c.polarity != "want" or c.kind == ConstraintKind.VETO:
            continue
        values = c.value if isinstance(c.value, list) else [c.value]
        for v in values:
            value = str(v).lower()
            if c.field == ConstraintField.CATEGORY and value in GENERIC_CATEGORIES:
                continue
            out.setdefault(c.pid, set()).add(Ask(c.field, value))
    return out


def _covers_person(c: Candidate, asks: set[Ask]) -> bool:
    return any(a.covers(c) for a in asks)


def needs_combos(candidates: list[Candidate], wants: dict[str, set[Ask]]) -> bool:
    """True when 2+ people asked for specific things and no one venue covers them all."""
    if len(wants) < 2 or len(set().union(*wants.values())) < 2:
        return False
    return not any(all(_covers_person(c, asks) for asks in wants.values()) for c in candidates)


def _groups(
    candidates: list[Candidate], wants: dict[str, set[Ask]]
) -> list[tuple[frozenset[Ask], list[Candidate]]]:
    """One group per distinct person's asks (any one of them satisfies that person),
    in a fair fixed order: things to do before food and drink, then alphabetical."""
    ask_sets = {frozenset(asks) for asks in wants.values()}
    groups = []
    for asks in ask_sets:
        members = [
            c for c in candidates if c.open_at_target != "closed" and _covers_person(c, asks)
        ]
        groups.append((asks, members[:PER_ASK]))
    groups.sort(
        key=lambda g: (
            not any(c.category in NON_FOOD for c in g[1]),
            sorted(a.value for a in g[0]),
        )
    )
    return groups


def _tag(stop: Candidate, asks: frozenset[Ask]) -> str:
    """What this stop is for ("pickleball"), for the poll title."""
    covering = sorted(a.value for a in asks if a.covers(stop))
    specific = [v for v in covering if v not in KIND_WORDS]
    return (specific or covering or [stop.category])[0]


def _walk_min(a: Candidate, b: Candidate) -> float:
    return haversine_mi(a.location, b.location) * DETOUR / WALK_MPH * 60


def _sum_cost(stops: list[Candidate]) -> Uncertain[Decimal]:
    costs = [s.est_cost_pp for s in stops]
    known = [c.value for c in costs if c.value is not None]
    if not known:
        return Uncertain[Decimal](value=None, status="unknown", source="combo")
    unknown = any(c.value is None for c in costs)  # a free court is $0, not unknown
    highs = [c.high if c.high is not None else c.value for c in costs]
    return Uncertain[Decimal](
        value=sum(known, Decimal(0)),
        low=sum((c.low or c.value or Decimal(0) for c in costs), Decimal(0)),
        high=None if any(h is None for h in highs) else sum(highs, Decimal(0)),
        status="unknown" if unknown else "estimated",
        source="combo",
    )


def _combine(stops: list[Candidate], tags: list[str]) -> Candidate:
    first = stops[0]
    statuses = {s.open_at_target for s in stops}
    status = "closed" if "closed" in statuses else "unknown" if "unknown" in statuses else "open"
    closes: list[datetime] = [s.closes_at for s in stops if s.closes_at]
    walking = sum(_walk_min(a, b) for a, b in zip(stops, stops[1:], strict=False))
    return Candidate(
        candidate_id="combo:" + "+".join(s.candidate_id for s in stops),
        name=" + ".join(s.name for s in stops),
        category=first.category,
        cuisines=list(dict.fromkeys([*tags, *(t for s in stops for t in s.cuisines)])),
        location=first.location,  # everyone meets at the first stop
        address=first.address,
        est_cost_pp=_sum_cost(stops),
        open_at_target=status,
        closes_at=min(closes) if closes else None,
        typical_duration_min=sum(s.typical_duration_min for s in stops) + round(walking),
        rating=min((s.rating for s in stops if s.rating is not None), default=None),
        source=first.source,
        extra_stops=stops[1:],
    )


def build(candidates: list[Candidate], wants: dict[str, set[Ask]]) -> list[Candidate]:
    """Walkable chains with a stop for each person's ask, closest-together first. A stop
    that already covers a later person's ask covers them too. [] if none exist."""
    groups = _groups(candidates, wants)
    if any(not members for _, members in groups):
        return []  # someone's ask found nothing at all: can't cover everyone
    combos: dict[tuple[str, ...], tuple[float, Candidate]] = {}
    for anchor_index, (_, anchors) in enumerate(groups):
        for anchor in anchors:
            chosen: dict[int, Candidate] = {anchor_index: anchor}
            complete = True
            for i, (asks, members) in enumerate(groups):
                if i in chosen:
                    continue
                if any(_covers_person(c, asks) for c in chosen.values()):
                    continue  # a stop already picked does this person's thing too
                options = [
                    m
                    for m in members
                    if haversine_mi(anchor.location, m.location) <= MAX_WALK_BETWEEN_MI
                ]
                if not options:
                    complete = False
                    break
                chosen[i] = min(options, key=lambda m: haversine_mi(anchor.location, m.location))
            if not complete or len({c.candidate_id for c in chosen.values()}) < 2:
                continue  # incomplete, or one place covers all (not a combo)
            order = sorted(chosen)  # fixed fair order by kind, not by who asked
            stops = [chosen[i] for i in order]
            tags = [_tag(chosen[i], groups[i][0]) for i in order]
            key = tuple(s.candidate_id for s in stops)
            walking = sum(_walk_min(a, b) for a, b in zip(stops, stops[1:], strict=False))
            if key not in combos or walking < combos[key][0]:
                combos[key] = (walking, _combine(stops, tags))
    ranked = [c for _, c in sorted(combos.values(), key=lambda row: row[0])]
    # Variety: a different first stop before reusing one (3 courts, not 1 court x3).
    turn: dict[str, int] = {}
    order = []
    for rank, combo in enumerate(ranked):
        first = combo.candidate_id.split("+")[0]
        order.append((turn.get(first, 0), rank, combo))
        turn[first] = turn.get(first, 0) + 1
    return [c for *_, c in sorted(order, key=lambda row: row[:2])][:MAX_COMBOS]


def mixed(candidates: list[Candidate], wants: dict[str, set[Ask]], limit: int) -> list[Candidate]:
    """No walkable combos: take turns between the asks (a court, a boba shop, a court...)."""
    groups = [members for _, members in _groups(candidates, wants) if members]
    out: list[Candidate] = []
    seen: set[str] = set()
    for i in range(max((len(g) for g in groups), default=0)):
        for members in groups:
            if i < len(members) and members[i].candidate_id not in seen:
                out.append(members[i])
                seen.add(members[i].candidate_id)
    return out[:limit]


def pick_mixed(ranked: list, wants: dict[str, set[Ask]], k: int = 3) -> list:
    """Top-k for a mixed list (no walkable combos): take turns between people's asks
    (best pickleball plan, best boba plan, ...), so no one's ask is crowded out.
    `ranked` is the optimizer's plans, best first."""
    ask_sets = sorted(
        {frozenset(a) for a in wants.values()}, key=lambda s: sorted(a.value for a in s)
    )
    picked: list = []
    while len(picked) < k:
        added = False
        for asks in ask_sets:
            nxt = next(
                (p for p in ranked if p not in picked and _covers_person(p.candidate, asks)), None
            )
            if nxt is not None and len(picked) < k:
                picked.append(nxt)
                added = True
        if not added:
            break
    for p in ranked:  # top up with whatever's best if an ask ran out
        if len(picked) >= k:
            break
        if p not in picked:
            picked.append(p)
    return picked


def walk_minutes_between(a: Candidate, b: Candidate) -> int:
    return max(1, round(_walk_min(a, b)))
