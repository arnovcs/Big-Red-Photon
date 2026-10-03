# CLAUDE.md — working rules for this repo

Read `ARCHITECTURE.md` fully before writing any code. It is the source of truth for scope, design, and build order. It is now **v2 (free-API stack)**: Gemini, OpenStreetMap (Overpass, Nominatim), OpenRouteService, Nessie, Photon. No Google Maps Platform or xAI APIs.

## Context
- This is a 24-hour hackathon project (BigRed//Hacks 2026, theme: Navigation). Hard deadline: **Sunday 8:30 AM ET** (Devpost submission).
- Target prize tracks: Photon (iMessage agents), Capital One (Nessie API), Big Red (theme), Design.
- Reliability of the live demo matters more than feature count.

## How to work
1. Build **stage by stage** in the order in `ARCHITECTURE.md` §15. Do not start a stage until the previous stage's exit criterion passes, unless the plan marks it as parallel.
2. Keep `main` runnable at all times. After each stage, run `uv run pytest` and `uv run ruff check .`.
3. **Do not add features, dependencies, or abstractions that are not in the plan.** If something seems missing, ask before adding it. Items listed in §16 "Out of scope" must not be built.
4. **Never invent external API shapes.** Photon (`spectrum-ts`), Gemini (`google-genai`), OpenRouteService, Nominatim, Overpass, and Nessie request/response formats in this plan are best-known shapes, not guarantees. Before implementing a real provider, check the current official docs or SDK README, then adapt the provider internals. Provider *interfaces* (§8) must not change without asking.
5. The optimizer (`app/optimizer/`) is pure Python: no I/O, no network, no database, no LLM calls.
6. Privacy rules in §11 are hard requirements. Only `app/private/` may read budgets, balances, or origins. Group messages may only be built from `GroupSafe*` models.
7. Never log phone numbers, dollar amounts, coordinates, or Nessie IDs at INFO level or above.
8. When an external call fails, degrade (cache → mock → friendly message). The bot must never crash or go silent mid-demo.
9. Free community services (Nominatim, Overpass) must get a descriptive User-Agent, ≤ 1 request/second, and must go through the record/replay cache. Never call them in loops.
10. Prefer simple, readable code over clever code. Teammates will be debugging this at 3 AM.

## Commands
- Backend: `uv run uvicorn app.main:app --reload --port 8000`
- Bridge: `cd bridge && bun run src/index.ts`
- Tests: `uv run pytest`
- Lint: `uv run ruff check . && uv run ruff format .`
- Demo scenario (simulator): `uv run python scripts/run_demo_scenario.py`
- Seed Nessie: `uv run python scripts/seed_nessie.py`
- Fetch venues (once): `uv run python scripts/fetch_venues.py`

## Accepted Stage 0 decisions
`EventFinding` and `FinancialSnapshot` are now defined in §6.9. `sqlalchemy[asyncio]`, `HTTP_TIMEOUT_SEC`, sim/mock provider defaults, the PEP 695 `Uncertain[T]` syntax, and excluding `*.md` from ruff are all accepted.
