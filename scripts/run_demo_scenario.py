"""Run the §18 demo (v3, DM-only) through the simulator and print every outbox.

    uv run python scripts/run_demo_scenario.py           # mock places + mock routing
    uv run python scripts/run_demo_scenario.py --live    # OSM venues + OpenRouteService

With --live, CACHE_MODE from .env decides the network use:
    CACHE_MODE=record  → real ORS (and Nominatim, if needed) calls, saved to fixtures/recorded/
    CACHE_MODE=replay  → the saved responses, no network

The clock is pinned (--now) so opening hours, venues, and therefore the ORS
requests are identical between runs; that's what makes replay hit every time.
Messaging is always the simulator and Nessie is off (personas use their fixture
limits). Gemini reads the preferences when GEMINI_API_KEY is set (and is
recorded/replayed like routing); without a key, planning uses no preferences.
Also the backup demo if iMessage fails.
"""

import argparse
import json
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import session as db  # noqa: E402
from app.db.tables import SessionRow, UserRow  # noqa: E402
from app.deps import build_deps  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models.plans import Plan  # noqa: E402
from app.models.routing import Mode  # noqa: E402
from app.settings import get_settings  # noqa: E402

# (name, handle, bank code, starting point, "car/bike/both/neither")
PERSONAS = [
    ("Maya", "+16075550101", "MAYA1", "collegetown", "bike"),
    ("Sam", "+16075550102", "SAM1", "north campus", "neither"),
    ("Jordan", "+16075550103", "JORDAN1", "downtown", "neither"),
]
PREFERENCES = [
    ("Maya", "I'm starving"),
    ("Sam", "no sushi pls"),
    ("Jordan", "I have to be back by 9, and nothing too far"),
    ("Sam", "something we haven't tried?"),
]
DEFAULT_NOW = "2026-10-04T09:30:00-04:00"  # Sunday morning, during judging


def dm(client: TestClient, handle: str, text: str) -> None:
    response = client.post("/sim/message", json={"sender_handle": handle, "text": text})
    response.raise_for_status()


def outbox(client: TestClient, handle: str) -> list[dict]:
    return client.get(f"/sim/outbox/{handle}").json()


def winning_plan(client: TestClient) -> tuple[Plan, dict[str, str]] | None:
    async def load() -> tuple[Plan, dict[str, str]] | None:
        async with db.session_factory()() as s:
            row = (await s.scalars(select(SessionRow))).first()
            if row is None or not row.winner_plan_id:
                return None
            plans = [Plan.model_validate(p) for p in json.loads(row.plans_json)]
            plan = next(p for p in plans if p.plan_id == row.winner_plan_id)
            names = {str(u.id): u.display_name for u in await s.scalars(select(UserRow))}
            return plan, {pid: names[uid] for pid, uid in json.loads(row.pid_map_json).items()}

    return client.portal.call(load)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the §18 demo through the simulator.")
    parser.add_argument("--live", action="store_true", help="use OSM places + ORS routing")
    parser.add_argument("--now", default=DEFAULT_NOW, help="pinned local time (ISO 8601)")
    args = parser.parse_args()

    now = datetime.fromisoformat(args.now)
    tmp = Path(tempfile.mkdtemp(prefix="demo-"))
    settings = get_settings().model_copy(
        update={
            "provider_messaging": "sim",
            "provider_places": "osm" if args.live else "mock",
            "provider_routing": "ors" if args.live else "mock",
            "nessie_api_key": "",
            "database_url": f"sqlite+aiosqlite:///{tmp / 'demo.db'}",
            "poll_timeout_sec": 3600,
        }
    )
    print(
        f"places={settings.provider_places} routing={settings.provider_routing} "
        f"cache={settings.cache_mode} now={now.isoformat()}"
    )
    handles = {name: handle for name, handle, *_ in PERSONAS}

    app = create_app(build_deps(settings, clock=lambda: now))
    with TestClient(app) as client:
        for name, handle, code, start, modes in PERSONAS:
            for text in ["start", name, code, "yes", start, "yes", modes]:
                dm(client, handle, text)

        dm(client, handles["Maya"], "@plan")
        started = outbox(client, handles["Maya"])[-1]["text"]
        code = re.search(r"join ([A-Z0-9]{4})", started).group(1)
        dm(client, handles["Sam"], f"join {code}")
        dm(client, handles["Jordan"], f"join {code}")
        for name, text in PREFERENCES:
            dm(client, handles[name], text)
        dm(client, handles["Maya"], "@go")
        dm(client, handles["Maya"], "A")
        dm(client, handles["Sam"], "A")

        for name, handle in handles.items():
            print(f"\n===== {name}'s DMs =====")
            for message in outbox(client, handle):
                print(f"[{message['kind']}] {message['text']}\n")

        result = winning_plan(client)
        if result is None:
            print("\nNo plan was chosen.")
            return
        plan, names = result
        print(f"===== Winner: {plan.candidate.name} (arrive {plan.target_arrival}) =====")
        for a in plan.assignments:
            print(
                f"  {names[a.pid]:<7} {a.mode.value:<10} {a.travel_min:5.1f} min  "
                f"fare ${a.fare_usd}  total ${a.total_cost_usd}  leave {a.leave_by:%H:%M}"
            )

        # Every mode from every start to the winner, so all four modes are visible.
        deps = app.state.deps
        center = plan.candidate.location

        async def all_modes() -> list:
            origins = {}
            for name, _, _, start, _ in PERSONAS:
                found = await deps.places.geocode(start, center)
                origins[name] = found[0]
            return await deps.routing.matrix(
                origins,
                {plan.candidate.candidate_id: center},
                {name: set(Mode) for name in origins},
                now,
            )

        print(f"\n===== Every mode to {plan.candidate.name} =====")
        rows = sorted(client.portal.call(all_modes), key=lambda e: (e.origin_pid, e.mode))
        for e in rows:
            print(
                f"  {e.origin_pid:<7} {e.mode.value:<10} {e.duration_min:5.1f} min  "
                f"{e.distance_mi:4.2f} mi  fare ${e.fare_usd.value}  source={e.source}"
            )


if __name__ == "__main__":
    main()
