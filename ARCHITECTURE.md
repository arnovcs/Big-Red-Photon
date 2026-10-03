# [NAME] — Architecture & Implementation Plan

> An iMessage agent that turns a group's "where should we go?" into a plan everyone can afford and reach, and gets the whole group there at the same time.

**Status:** v3 — DM-only pivot. Stages 0, 0.5, and 0b are complete. Stage 1 was built against v2; apply the Stage 1.5 migration (§15) next.

### v3 changes (summary) — DM-only "virtual group"
The Stage 0b spike showed that Photon's free plan (the only plan available to us) uses a **shared line pool**: each person is assigned their own bot number, and **group chats are not supported**. Only the paid Business plan (one dedicated number) supports groups. DMs work both ways. So in v3 there is no iMessage group chat. The bot is the hub of a **virtual group** made of DMs:
- **Start / join:** a READY user DMs `@plan`; the bot replies with a short **join code**. Friends DM `join <code>` to their own bot line. (§7.1, §7.3)
- **Collect:** members DM their preferences privately. The bot stores them; nothing is relayed to other members.
- **"Group" messages fan out:** every group-safe message (join notices, poll, confirmation) is sent to **each member's DM**. They are still built only from `GroupSafe*` types and still pass the PrivacyGuard. (§8, §11)
- **Voting:** a text poll in each DM; members reply `A`, `B`, or `C`. No native polls. (§7.3)
- **Interface change:** `MessagingProvider.send_group(chat_id, msg)` → `send_group(handles, msg)` (§8).
- **Model change:** `Group.chat_id` → `Group.join_code` (§6.1, §6.8).
- **Location:** typed landmarks only (in the spike, a shared location arrived as `custom` content with no usable coordinates).
- Unchanged: optimizer, budgets, Nessie, routing, places, Gemini, record/replay, privacy rules.

### v2 changes (summary)
v1 used paid Google Maps and xAI Grok APIs. v2 uses only free services plus the team's Gemini key:
- **LLM:** Grok → **Gemini** (structured output).
- **Venues:** Google Places → **OpenStreetMap** (Overpass API), pulled once into a hand-checked fixture with hand-entered price tiers.
- **Geocoding:** Google → **`demo_locations.json` first, then Nominatim**.
- **Routing:** Google Routes → **OpenRouteService** (walking, cycling, driving profiles).
- **Transit removed** (no free schedule-based transit API for Ithaca). **Ride-share added** as a mode, with time from ORS driving + pickup wait and cost from a configurable formula. Modes are now `walk | bike | drive | rideshare`.
- Arrival-spread scoring term removed (all modes are deterministic, so everyone arrives exactly at `T_target`).
- New definitions: `FinancialSnapshot`, `EventFinding` (§6.9).
**Event:** BigRed//Hacks 2026 (Cornell), theme **Navigation**.
**Hard deadline:** Devpost submission **Sunday 8:30 AM ET**. Judging starts 9:00 AM.
**Prize tracks targeted:** Photon (Agents in iMessage), Capital One (Best Use of Nessie), Big Red (theme), Design (UI/UX).

---

## Table of contents
1. Product summary
2. Guiding decisions
3. System architecture
4. Repository layout
5. Configuration
6. Data models
7. Conversation flows and state machines
8. Provider interfaces
9. External service details
10. Planning pipeline
11. Privacy isolation
12. Budget estimation
13. Optimizer and fairness scoring
14. Record/replay cache, mocks, and the simulator
15. Staged build plan
16. Out of scope
17. Testing strategy
18. Demo script
19. Risks and verification checklist
20. Coding conventions

---

## 1. Product summary

### Problem
Group chats waste time deciding where to go. Every person has different preferences, a different starting location, and a different budget they may not want to share. Existing tools either ignore money entirely or expose it.

### Solution
A bot you text on iMessage. Each person privately links a (sandbox) bank account and shares where they're starting from. One person DMs `@plan` and gets a join code; friends DM `join <code>` to form a virtual group. Everyone tells the bot what they want **privately**, in their own DM, so nobody has to say "I'm broke this week" in front of friends. On `@go`, the bot extracts preferences, finds nearby options, routes every person to every option by walking, biking, driving, or ride-share, and picks the plans that are fairest to the worst-off person while staying inside **everyone's** private budget. Everyone gets the same 3 options and votes by replying A, B, or C. Then each person gets a **private** DM with their own travel mode, cost, and leave-by time, calculated so the whole group **arrives together**.

### Why it fits "Navigation"
Navigation is the core, not a side feature: multi-origin, multi-modal routing to a shared destination, with departure scheduling so arrivals converge. Money and preferences are constraints on that navigation problem. Ride-share creates a real money-versus-time trade-off per person: fast but costly versus free but slow.

### What makes it technically interesting (pitch points)
- **Fair group optimization:** exact enumeration with a min-max + mean objective, so no one person carries the cost of the plan.
- **Synchronized arrival:** work backward from a shared target arrival time to compute each person's leave-by time.
- **Privacy by construction:** budgets never reach other members or the LLM, and preferences are shared privately, one DM per person. Enforced by module boundaries, types, and an output scanner.
- **Grounded LLM use:** the LLM extracts preferences and phrases explanations, but every number (time, distance, cost, hours) comes from routing data or deterministic formulas, and explanations are checked against computed facts.
- **Built on open data:** venues and routing come from OpenStreetMap (Overpass, Nominatim, OpenRouteService).

---

## 2. Guiding decisions

| Decision | Choice | Reason |
|---|---|---|
| Backend | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, `uv` | Team preference. One async process. |
| iMessage | Photon `spectrum-ts` in a **thin TypeScript bridge** (`bridge/`) | Photon's SDKs are TypeScript. The bridge only relays; all logic stays in Python. |
| Group model (v3) | **DMs only**; a virtual group joined by code | Free plan = shared line pool: a different bot number per person and no group chats. |
| Bridge ↔ backend | Plain JSON over HTTP on **localhost** | Both run on one laptop. No HMAC, no auth. |
| Database | SQLite via SQLAlchemy | Zero setup. No Postgres or Supabase. |
| Routing | **OpenRouteService** (matrix + directions; `foot-walking`, `cycling-regular`, `driving-car`) | Free key, no card. Free plan: 2,000 directions/day, 500 matrix/day. |
| Modes | `walk`, `bike`, `drive` (own car), `rideshare` | No free transit API for Ithaca. Ride-share = ORS driving time + pickup wait, cost from formula. |
| Venues | **OpenStreetMap via Overpass**, fetched once into `fixtures/venues.json` | Free, no key. No prices in OSM, so price tiers are hand-entered. |
| Geocoding | `fixtures/demo_locations.json`, then **Nominatim** | Free. Max ~1 request/second; requires a descriptive User-Agent. |
| LLM | **Gemini** (structured output) for extraction and explanation | Team already has a key. Model name is config. Use a Flash model. |
| Live events | Gemini + Google Search grounding **only if the key's tier supports it**; otherwise a hand-checked list | **Stretch only** (Stage 7). |
| Finance | Capital One Nessie sandbox + deterministic estimator | LLM never sees balances. |
| Optimizer | Exact enumeration, hard filters, min-max + mean burden | Small search space; fully explainable; no solver dependency. |
| Demo reliability | Record/replay cache + simulator + two small mocks | Real data on stage without live-API risk. |
| Hosting | One team laptop + phone hotspot backup | Spectrum holds an outgoing connection; no public URL needed. |
| ML | None | No fake ML. |

---

## 3. System architecture

```
          ┌──────────────────────────── iMessage ────────────────────────────┐
          │  DMs only: A↔bot line 1, B↔bot line 2, C↔bot line 3 (shared pool) │
          │  Virtual group = members who joined the same join code (v3)       │
          └──────────────┬─────────────────────────────────▲─────────────────┘
                         │ Photon Spectrum (gRPC stream)   │
          ┌──────────────▼─────────────────────────────────┴─────────────────┐
          │ bridge/ (TypeScript, Bun, ~150 LOC, port 3001)                    │
          │  • on DM message           → POST http://localhost:8000/webhooks  │
          │  • POST /send_dm ← from backend (/send, /send_poll unused in v3)  │
          └──────────────┬─────────────────────────────────▲─────────────────┘
                         │ JSON over localhost HTTP        │
┌────────────────────────▼─────────────────────────────────┴──────────────────────────┐
│ app/ (FastAPI, port 8000)                                                            │
│                                                                                      │
│  api/webhooks.py ─► conversation/router.py (DM commands, join codes, session lookup) │
│        │                     │                    │                    │             │
│        ▼                     ▼                    ▼                    ▼             │
│  onboarding/fsm.py   planning/session.py   decision/poll.py   delivery/itinerary.py │
│        │                     │                                                       │
│        ▼                     ▼                                                       │
│  private/vault.py    planning/pipeline.py:                                           │
│  private/budget.py     extract → discover → route → optimize → explain → guard → poll│
│        │                  │         │          │         │          │                │
│  FinanceProvider     LLMProvider PlacesProv RoutingProv optimizer/ LLMProvider       │
│                                                         (pure Python)                │
│                                                                                      │
│  messaging/outbound.py → messaging/guard.py (PrivacyGuard) → MessagingProvider       │
│  providers/{real,mock}  •  providers/cache.py (record/replay)  •  db/ (SQLite)       │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

### Structural rules (must hold everywhere)
1. **Providers are the only code that talks to the outside world** (HTTP, SDKs). Everything else calls provider interfaces.
2. **`app/private/` is the only package that reads budgets, balances, Nessie IDs, or origins.** The optimizer receives opaque, pseudonymous `PrivateConstraints`. Group-facing code never imports `app.private`.
3. **The optimizer is pure Python with no I/O.** Input: candidates, route estimates, constraints, preferences. Output: ranked plans.
4. **The LLM never produces physical-world numbers.** Durations, distances, costs, hours, and coordinates come only from providers. Unknown values are explicitly `None` / `status="unknown"`.
5. **Webhooks return immediately.** All processing runs as background tasks. v3: messages from different members of one virtual group arrive on different DMs, so router handling is serialized with **one global `asyncio.Lock`** (traffic is tiny). Long work (pipeline, delivery) runs as its own task, outside the lock.

---

## 4. Repository layout

```
.
├── CLAUDE.md
├── ARCHITECTURE.md
├── README.md                     # Setup, run commands, pitch summary (written in Stage 8)
├── pyproject.toml                # uv-managed; deps listed in §20
├── .env.example                  # Every variable from §5, no values
├── .gitignore                    # .env, *.db, __pycache__, node_modules, demo video
│
├── app/
│   ├── main.py                   # FastAPI app, lifespan (DB init, provider wiring)
│   ├── settings.py               # pydantic-settings; reads .env
│   ├── logging.py                # basic structured logging; no PII at INFO+
│   ├── deps.py                   # builds provider instances from settings
│   │
│   ├── api/
│   │   ├── webhooks.py           # POST /webhooks/photon/message (v3: poll_vote unused)
│   │   ├── sim.py                # Simulator endpoints (§14.3)
│   │   └── health.py             # GET /health
│   │
│   ├── conversation/
│   │   ├── router.py             # Dispatch (v3: DMs only): commands, join codes, free text, state
│   │   ├── commands.py           # Parse @plan, join <code>, @go, @cancel, @pick, A/B/C, start, yes/no, numbers
│   │   └── copy.py               # ALL user-facing strings and message templates
│   │
│   ├── onboarding/
│   │   └── fsm.py                # DM onboarding state machine (§7.2)
│   │
│   ├── planning/
│   │   ├── session.py            # Group planning state machine (§7.3)
│   │   └── pipeline.py           # Orchestrates §10
│   │
│   ├── optimizer/                # PURE PYTHON. No I/O.
│   │   ├── enumerate.py          # venue × per-person mode combinations
│   │   ├── arrival.py            # T_target, leave_by, arrival spread
│   │   ├── feasibility.py        # hard filters
│   │   ├── burden.py             # per-person burden terms
│   │   ├── score.py              # group objective J
│   │   ├── select.py             # top-3 with diversity
│   │   └── facts.py              # group-safe fact extraction for explanations
│   │
│   ├── private/                  # ONLY place that touches money/location
│   │   ├── vault.py              # read/write PrivateProfile; issue PrivateConstraints
│   │   └── budget.py             # deterministic limit estimator (§12)
│   │
│   ├── decision/
│   │   └── poll.py               # send text poll to all members, tally DM votes, pick winner
│   │
│   ├── delivery/
│   │   └── itinerary.py          # directions for winner; per-person DMs; map image (stretch, `staticmap` + OSM tiles)
│   │
│   ├── messaging/
│   │   ├── outbound.py           # send_group / send_private wrappers
│   │   └── guard.py              # PrivacyGuard output scanner
│   │
│   ├── providers/
│   │   ├── protocols.py          # Protocol classes (§8)
│   │   ├── cache.py              # record/replay cache (§14.1)
│   │   ├── real/
│   │   │   ├── photon.py         # calls bridge HTTP endpoints
│   │   │   ├── gemini.py         # LLMProvider (+ ContextProvider in Stage 7)
│   │   │   ├── osm_places.py     # PlacesProvider: venue fixture + demo_locations + Nominatim geocoding
│   │   │   ├── ors.py            # RoutingProvider: OpenRouteService (+ ride-share derivation)
│   │   │   └── nessie.py         # FinanceProvider
│   │   └── mock/
│   │       ├── sim_messaging.py  # writes to in-memory outbox (simulator)
│   │       ├── routing.py        # haversine-based estimates
│   │       └── places.py         # loads fixtures/venues.json
│   │
│   ├── models/
│   │   ├── identity.py           # User, Group
│   │   ├── private.py            # PrivateProfile, PrivateConstraints, TravelModes
│   │   ├── conversation.py       # ChatMessage, ExtractedConstraint, GroupPreferences
│   │   ├── candidates.py         # Candidate, Uncertain
│   │   ├── routing.py            # Mode, RouteEstimate, RouteDetail, RouteStep
│   │   ├── plans.py              # PersonAssignment, Plan, PlanScore, BurdenBreakdown
│   │   └── outbound.py           # GroupSafeMessage, PrivateMessage, GroupPlanOption, PersonalItinerary
│   │
│   └── db/
│       ├── tables.py             # SQLAlchemy models (§6.8)
│       └── session.py            # engine, async session factory, init_db()
│
├── bridge/
│   ├── package.json
│   ├── tsconfig.json
│   └── src/index.ts              # Photon relay (§9.1)
│
├── fixtures/
│   ├── venues.json               # ~25 real OSM venues around the demo area, hand-checked, with price tiers
│   ├── demo_locations.json       # named starting points with lat/lng
│   ├── personas.json             # Nessie seed definitions (§12.3)
│   ├── events_fallback.json      # hand-checked events for the demo weekend (Stage 7 fallback)
│   ├── transcripts/              # golden group-chat transcripts + expected extraction
│   └── recorded/                 # record/replay cache files (committed)
│
├── scripts/
│   ├── seed_nessie.py            # creates demo customers/accounts/purchases/bills
│   ├── fetch_venues.py           # pulls venues from Overpass into fixtures/venues.json (prices added by hand)
│   └── run_demo_scenario.py      # drives the full §18 scenario through the simulator
│
└── tests/
    ├── test_optimizer.py
    ├── test_arrival.py
    ├── test_budget.py
    ├── test_privacy_guard.py
    ├── test_extraction.py        # golden transcripts (uses replay cache)
    └── test_end_to_end.py        # full scenario via simulator, all mocks
```

---

## 5. Configuration

All config through `app/settings.py` (pydantic-settings), loaded from `.env`.

| Variable | Example | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | | Gemini |
| `GEMINI_MODEL` | (fill from current Gemini docs; a Flash model) | Model id for extraction + explanation |
| `ORS_API_KEY` | | OpenRouteService |
| `ORS_BASE_URL` | `https://api.openrouteservice.org` | Verify against ORS docs |
| `NOMINATIM_BASE_URL` | `https://nominatim.openstreetmap.org` | |
| `NOMINATIM_USER_AGENT` | `bigredhacks-<name>/0.1 (team email)` | Required by Nominatim's usage policy |
| `OVERPASS_URL` | `https://overpass-api.de/api/interpreter` | Used only by `scripts/fetch_venues.py` |
| `NESSIE_API_KEY` | | Nessie |
| `NESSIE_BASE_URL` | `http://api.nessieisreal.com` | Verify scheme/host against Nessie docs |
| `BRIDGE_URL` | `http://localhost:3001` | Backend → bridge |
| `BACKEND_URL` | `http://localhost:8000` | Bridge → backend (bridge's own env) |
| `PHOTON_PROJECT_ID`, `PHOTON_PROJECT_SECRET` | | Bridge only |
| `DATABASE_URL` | `sqlite+aiosqlite:///./app.db` | |
| `PROVIDER_MESSAGING` | `photon` \| `sim` | |
| `PROVIDER_PLACES` | `osm` \| `mock` | |
| `PROVIDER_ROUTING` | `ors` \| `mock` | |
| `PROVIDER_FINANCE` | `nessie` | (no mock; seeded customers serve that role) |
| `CACHE_MODE` | `off` \| `record` \| `replay` | §14.1 |
| `DEMO_TIMEZONE` | `America/New_York` | All "by 9" style times are local |
| `DEMO_AREA_LABEL` | `Ithaca, NY` | Used in prompts and Places queries |
| `DEMO_CENTER_LAT`, `DEMO_CENTER_LNG` | | Fallback search center |
| `DRIVE_COST_PER_MILE_USD` | `0.20` | Own car: gas estimate |
| `DRIVE_PARKING_USD` | `3.00` | Own car |
| `RIDESHARE_BASE_USD` | `2.50` | Ride-share cost = base + booking + per_mile × mi + per_min × min, then max(min_fare) |
| `RIDESHARE_BOOKING_USD` | `2.50` | |
| `RIDESHARE_PER_MILE_USD` | `1.20` | |
| `RIDESHARE_PER_MIN_USD` | `0.30` | |
| `RIDESHARE_MIN_FARE_USD` | `8.00` | |
| `RIDESHARE_PICKUP_WAIT_MIN` | `6` | Added to ride-share duration |
| `OPTIMIZER_LAMBDA` | `0.5` | Worst-off vs mean weight |
| `POLL_TIMEOUT_SEC` | `300` | Auto-pick the leader after this |
| `LLM_TIMEOUT_SEC` | `20` | |
| `HTTP_TIMEOUT_SEC` | `10` | Default provider timeout |

---

## 6. Data models

Pydantic v2. Use `Decimal` for money, `datetime` with tzinfo for times. All models live in `app/models/`.

### 6.1 Identity

```python
class OnboardingState(StrEnum):
    NEW = "new"
    AWAITING_BANK_CODE = "awaiting_bank_code"
    AWAITING_LIMIT_CONFIRM = "awaiting_limit_confirm"
    AWAITING_LOCATION = "awaiting_location"
    AWAITING_MODES = "awaiting_modes"      # v2: was AWAITING_CAR
    READY = "ready"

class User(BaseModel):
    id: UUID
    handle: str                     # phone/email from iMessage; never shown to others
    display_name: str               # first name; asked in onboarding if unknown
    dm_chat_id: str | None          # Photon chat id of the DM with this user
    onboarding_state: OnboardingState

class Group(BaseModel):              # v3: a virtual group, created by one @plan
    id: UUID
    join_code: str                  # v3 (was chat_id): e.g. "K7QP"; 4 chars, uppercase, no 0/O/1/I
    member_ids: list[UUID]          # creator + users who DMed "join <code>"
    active_session_id: UUID | None
```

v3: one `Group` per `@plan`. A user may be in **at most one group with an active session** at a time; the router finds a user's group through `group_members` + `groups.active_session_id`.

### 6.2 Private data (vault only)

```python
class TravelModes(BaseModel):          # v2: bike + rideshare added
    walk: bool = True
    bike: bool = False
    drive: bool = False                   # own car
    rideshare: bool = True

class LatLng(BaseModel):
    lat: float
    lng: float

class PrivateProfile(BaseModel):
    user_id: UUID
    nessie_customer_id: str | None
    spend_limit_usd: Decimal
    limit_source: Literal["nessie_estimate", "user_override"]
    origin: LatLng
    origin_label: str               # "Olin Library" — DM only
    modes: TravelModes
    updated_at: datetime

class PrivateConstraints(BaseModel):   # the only private data the optimizer sees
    pid: str                        # "p1".."pN", per-session pseudonym
    spend_limit_usd: Decimal
    origin: LatLng
    modes: TravelModes
```

### 6.3 Conversation and preferences

```python
class ChatMessage(BaseModel):
    message_id: str
    chat_id: str
    sender_user_id: UUID
    text: str
    ts: datetime
    is_group: bool

class PseudonymousMessage(BaseModel):  # what the LLM sees
    message_id: str
    pid: str
    text: str
    ts_local: str                   # "18:02"

class ConstraintKind(StrEnum):
    HARD = "hard"          # "I have to be back by 9"
    SOFT = "soft"          # "I'd prefer Korean"
    VETO = "veto"          # "no sushi"
    INFERRED = "inferred"  # "I'm starving" → food, soon

class ConstraintField(StrEnum):
    MAX_WALK_MIN = "max_walking_minutes"
    MAX_TRAVEL_MIN = "max_travel_minutes"
    AVAILABLE_UNTIL = "available_until"     # "HH:MM" local
    AVAILABLE_FROM = "available_from"       # "HH:MM" local
    CUISINE = "cuisine"
    CATEGORY = "category"                   # food | bar | cafe | dessert | activity | event
    NOVELTY = "novelty"                     # 0..1
    MODE_PREFERENCE = "mode_preference"     # "walk" | "bike" | "drive" | "rideshare" with polarity

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
```

**Validation rules (enforced after the LLM call, in code):**
- Every `evidence_msg_ids` entry must exist in the transcript, and the message's sender must equal `pid`. Drop constraints that fail.
- A `HARD` constraint with confidence < 0.7 is downgraded to `SOFT`.
- `AVAILABLE_UNTIL`/`AVAILABLE_FROM` must parse as `HH:MM`; otherwise drop.
- Unknown `CATEGORY` values map to the nearest allowed value or are dropped.

Only `HARD` and `VETO` become filters. `SOFT` and `INFERRED` affect the score, weighted by confidence.

### 6.4 Candidates

```python
class Uncertain(BaseModel, Generic[T]):
    value: T | None
    low: T | None = None
    high: T | None = None
    status: Literal["known", "estimated", "unknown"]
    source: str                     # "osm_fixture" | "hand_entered" | "formula" | "ors" | "gemini:<url>"

class Candidate(BaseModel):
    candidate_id: str               # "osm:<node|way>/<id>" | "ev:<hash>"
    name: str
    category: str                   # food | bar | cafe | dessert | activity | event
    cuisines: list[str] = []
    location: LatLng                # REQUIRED, from the OSM fixture or Nominatim — never from the LLM
    address: str
    est_cost_pp: Uncertain[Decimal]
    open_at_target: Literal["open", "closed", "unknown"] = "unknown"
    closes_at: datetime | None = None
    typical_duration_min: int       # category default: food 60, cafe 45, dessert 30, bar 90, activity 90
    rating: float | None = None
    source: Literal["osm_fixture", "event"]
    novelty_tags: list[str] = []
```

### 6.5 Routing

```python
class Mode(StrEnum):                  # v2
    WALK = "walk"
    BIKE = "bike"
    DRIVE = "drive"
    RIDESHARE = "rideshare"

class RouteEstimate(BaseModel):
    origin_pid: str
    candidate_id: str
    mode: Mode
    duration_min: float
    distance_mi: float
    walk_min: float                 # equals duration for WALK; 0 for other modes
    fare_usd: Uncertain[Decimal]    # walk/bike: 0; drive: miles × rate + parking; rideshare: formula (status="estimated")
    source: Literal["ors", "mock"]

class RouteStep(BaseModel):            # v2: simplified
    mode: Literal["walk", "bike", "drive", "rideshare"]
    instruction: str                # from ORS directions, e.g. "Turn left onto College Ave"
    duration_min: float
    distance_mi: float

class RouteDetail(RouteEstimate):
    depart_at: datetime
    arrive_at: datetime
    steps: list[RouteStep]
    polyline: str | None = None
```

### 6.6 Plans

```python
class BurdenBreakdown(BaseModel):
    money: float
    time: float
    pref: float
    walk: float = 0.0      # phase 2
    sched: float = 0.0     # phase 2
    total: float

class PersonAssignment(BaseModel):
    pid: str
    mode: Mode
    leave_by: datetime
    arrive_at: datetime
    travel_min: float
    walk_min: float
    venue_cost_usd: Decimal
    fare_usd: Decimal
    total_cost_usd: Decimal
    cost_uncertain: bool
    burden: BurdenBreakdown

class PlanScore(BaseModel):
    J: float
    max_burden: float
    mean_burden: float
    burden_spread: float
    arrival_spread_min: float       # v2: always 0 (deterministic modes); kept for future use

class Plan(BaseModel):
    plan_id: str
    candidate: Candidate
    target_arrival: datetime
    assignments: list[PersonAssignment]
    score: PlanScore
    risk_flags: list[str] = []      # "price estimated", "closes within 30 min of arrival"
```

### 6.7 Outbound (the only types messages are built from)

```python
class GroupPlanOption(BaseModel):    # group-safe
    label: Literal["A", "B", "C"]
    title: str                       # "Koko — Korean"
    max_travel_min: int              # "≤22 min for everyone"
    walking_level: Literal["low", "moderate", "high"]
    price_tier: Literal["$", "$$", "$$$", "$$$$"]
    arrival_window_min: int
    blurb: str                       # explanation from group-safe facts only

class GroupSafeMessage(BaseModel):
    text: str
    poll: list[GroupPlanOption] | None = None

class PersonalItinerary(BaseModel):  # DM only
    user_id: UUID
    venue_name: str
    venue_address: str
    mode: Mode
    leave_by: datetime
    arrive_at: datetime
    steps: list[RouteStep]
    est_cost_usd: Decimal

class PrivateMessage(BaseModel):
    text: str
    image_path: str | None = None
```

`send_group()` accepts only `GroupSafeMessage`. `send_private()` accepts only `PrivateMessage` and a `user_id`. The type checker must catch passing the wrong one.

v3: "group" messages are delivered to each member's DM (§8), but the rule is unchanged: anything every member sees must be a `GroupSafeMessage`. Use `PrivateMessage` only for content meant for one person.

### 6.8 Database tables (SQLAlchemy)

| Table | Columns | Notes |
|---|---|---|
| `users` | id, handle (unique), display_name, dm_chat_id, onboarding_state, created_at | |
| `groups` | id, join_code (unique), active_session_id, created_at | v3: `join_code` replaces `chat_id` |
| `group_members` | group_id, user_id | |
| `private_profiles` | user_id (PK), nessie_customer_id, spend_limit_usd, limit_source, origin_lat, origin_lng, origin_label, modes_json, updated_at | **Only `app/private/` may query this table.** |
| `sessions` | id, group_id, state, started_at, target_time, pid_map_json, preferences_json, plans_json, poll_id, winner_plan_id | `plans_json` stores full plans; `pid_map_json` maps pid → user_id. v3: `poll_id` stays null (text polls only) |
| `session_messages` | id, session_id, message_id (unique), sender_user_id, text, ts | Only messages during COLLECTING. Deleted when session reaches DONE or CANCELLED. |
| `votes` | session_id, user_id, option_label, ts | One row per user per session (upsert) |
| `processed_messages` | message_id (PK) | Idempotency for webhook retries |

Use `create_all()` on startup. No migrations.

### 6.9 Supporting models (v2: previously undefined)

```python
class FinancialSnapshot(BaseModel):     # app/models/private.py — vault-only
    checking_balance: Decimal
    upcoming_bills_14d: Decimal
    recent_outing_amounts: list[Decimal]   # dining/entertainment purchases, last 60 days

class EventFinding(BaseModel):          # app/models/candidates.py — Stage 7 only
    title: str
    venue_name: str
    starts_at: datetime | None
    est_price_usd: Decimal | None = None
    source_url: str
```

---

## 7. Conversation flows and state machines

### 7.1 Message routing (`conversation/router.py`)

v3: **every inbound message is a DM.** (`is_group` is always false; if a group message ever arrives, ignore it.) For every inbound message:
1. If `message_id` is in `processed_messages`, ignore. Otherwise insert it.
2. Upsert the `User` by handle.
3. If the user is **not READY** → onboarding FSM (§7.2). If they sent `join <code>`, reply "Let's get you set up first, then send `join <code>` again." and start onboarding.
4. If the user is READY, look up their **active group** (the one group with an active session they belong to, if any) and handle, in this order:
   - **Session commands:** `@plan`, `join <code>`, `@go`, `@cancel`, `@pick A|B|C`, and vote replies `A`/`B`/`C` (only while POLLING). See §7.3.
   - **Settings commands:** `budget <n>`, `location`, `car yes|no`, `help`.
   - **Otherwise,** if their group's session is COLLECTING, store the text in `session_messages` (sender = this user). Reply `copy.NOTED` only to that member's **first** stored message in the session, to keep the DM quiet.
   - **Otherwise** reply with `copy.HELP` (a short list of commands).

Commands are case-insensitive and may appear with surrounding text ("ok @go"). Join codes are matched case-insensitively.

### 7.2 Onboarding FSM (DM only, `onboarding/fsm.py`)

```
NEW ──"start" (or any first DM)──► ask name if unknown, then:
AWAITING_BANK_CODE ──valid code──► vault.link_customer() → budget.estimate()
                                   → "About $25 looks comfortable tonight. Use that? (yes / or type a number)"
AWAITING_LIMIT_CONFIRM ──"yes" | number──► save limit (source = estimate | override)
AWAITING_LOCATION ──text──► places.geocode(text, near=demo center) → "Got it: Olin Library. Right? (yes/no)"
                    ──"no"──► ask again
AWAITING_MODES ──"car" | "bike" | "both" | "neither"──► TravelModes(drive=…, bike=…)  (walk + rideshare always on;
                    user may reply "no rideshare" to turn it off)
READY ──► DM: "You're set! Start a plan with @plan, or join a friend's with join <code>."
```

- **Bank code:** a short code per seeded persona (e.g. `MAYA1`), mapped to a Nessie customer id in `fixtures/personas.json` after seeding. This simulates "linking a bank account."
- v3: **typed landmarks only.** In the spike, a shared location arrived as `custom` content with no usable coordinates.
- Any invalid input re-asks with a short hint. Never echo dollar amounts in any group-safe message.

### 7.3 Planning session FSM (`planning/session.py`)

v3: a session lives in a virtual group created by `@plan`. "Tell the group" means `send_group(member handles, GroupSafeMessage)`, which sends the same message to each member's DM.

```
(no group) ──@plan──► COLLECTING ──@go──► RUNNING ──pipeline ok──► POLLING
                        │  ▲  join <code>      │                         │
                     @cancel             pipeline error         A/B/C replies / @pick / timeout
                        ▼                      ▼                         ▼
                    CANCELLED      "Couldn't finish — say @go     CONFIRMED ──► DELIVERING ──► DONE
                                    to retry" → COLLECTING
any state ──@cancel (any member)──► CANCELLED
```

- **`@plan`** (READY user, not in an active group): create a `Group` with a fresh join code and the sender as its first member, create a session in COLLECTING, and reply with `copy.PLAN_STARTED` (includes the code). If the user is already in an active group: "You're already in a plan (code K7QP). Say @cancel to start over."
- **`join <code>`** (READY user): if the code matches a group whose session is COLLECTING, add the user and tell the group "✅ Sam joined (2 people)." If the session is past COLLECTING: "That plan already started. Ask them to @plan again." Unknown code: "I don't know that code. Check it and try again." Already in another active group: same reply as for `@plan`. Cap at 6 members.
- **COLLECTING:** the bot only stores DMs sent **after** the member joined. (Photon cannot fetch chat history.) Nothing is relayed to other members.
- **`@go`** (any member): require at least 2 members, else "I need at least 2 people. Share code K7QP first." Then tell the group "🔎 Looking at options…" and run the pipeline (§10) as a background task.
- **POLLING:** text poll only; the same poll message goes to every member. Votes are DM replies `A`, `B`, or `C` (latest vote per user wins; upsert into `votes`). Winner = first option to reach ⌈N/2⌉ votes, or `@pick X` by any member, or the leader after `POLL_TIMEOUT_SEC` (ties → better score). Optionally tell the group "🗳️ 2 of 3 voted" (counts only, never who voted for what).
- **DELIVERING:** compute detailed routes for the winner, send the group confirmation, then each person's itinerary DM (so the route lands right under the confirmation).
- **DONE / CANCELLED:** set `groups.active_session_id = null` and delete the session's `session_messages`. The join code is retired. A new `@plan` creates a new group and code.

### 7.4 Message copy (all in `conversation/copy.py`)

Welcome (first DM from a new user, before onboarding):
> Hi! I'm [NAME]. I help friends pick a plan everyone can afford and reach, and I get you there at the same time. Let's set you up. It's private: I never share your money or location with anyone.

`PLAN_STARTED` (reply to `@plan`):
> Plan started! 🎉 Tell your friends to text me: **join K7QP**
> Meanwhile, tell me what you're in the mood for. Only I see it. Say @go when everyone's in.

Join notice (to the group):
> ✅ Sam joined (2 people).

`NOTED` (first stored message from a member):
> Got it 👍 Keep going, or say @go when everyone's ready.

Poll message (to the group):
> Here are 3 plans that fit everyone's constraints:
> **A** — Koko (Korean) · ≤22 min for everyone · $$ · arrive within 4 min
> Keeps everyone within budget and the longest trip is 22 min.
> **B** — …
> Reply A, B, or C.

Confirmation (to the group):
> 🎉 Plan A: Koko. Everyone arrives around 6:42. Your route is below 👇

Personal DM:
> Your plan for tonight: **Koko**, 123 College Ave.
> 🚗 Request a ride by **6:29** (about 6 min pickup + 7 min drive).
> Arrive ~6:42. Estimated total: $34 (food ~$25 + ride ~$9).

Keep copy short, warm, and free of jargon.

---

## 8. Provider interfaces (`app/providers/protocols.py`)

Do not change these signatures without asking. Implementations live in `providers/real/` and `providers/mock/`.

**v3 interface change:** `send_group` now takes the members' handles instead of a group chat id, and returns nothing (text polls only, so there is no poll_id).

```python
class MessagingProvider(Protocol):
    async def send_group(self, handles: list[str], msg: GroupSafeMessage) -> None: ...  # v3: same message to each member's DM
    async def send_private(self, handle: str, msg: PrivateMessage) -> None: ...

class FinanceProvider(Protocol):
    async def get_customer(self, customer_id: str) -> dict: ...
    async def get_financial_snapshot(self, customer_id: str) -> FinancialSnapshot: ...

class PlacesProvider(Protocol):
    async def search_nearby(self, center: LatLng, radius_m: int, categories: list[str],
                            open_at: datetime) -> list[Candidate]: ...
    async def text_search(self, query: str, near: LatLng) -> list[Candidate]: ...
    async def geocode(self, text: str, near: LatLng) -> tuple[LatLng, str] | None: ...  # (coords, clean label); demo_locations first, then Nominatim

class RoutingProvider(Protocol):
    async def matrix(self, origins: dict[str, LatLng], destinations: dict[str, LatLng],
                     modes: dict[str, set[Mode]], depart_at: datetime) -> list[RouteEstimate]: ...
    async def route(self, origin: LatLng, destination: LatLng, mode: Mode,
                    arrive_by: datetime | None = None,
                    depart_at: datetime | None = None) -> RouteDetail: ...

class LLMProvider(Protocol):
    async def extract_preferences(self, transcript: list[PseudonymousMessage],
                                  now_local: datetime) -> GroupPreferences: ...
    async def phrase_explanations(self, facts: list[dict]) -> list[str]: ...

class ContextProvider(Protocol):       # Stage 7 stretch (Gemini + Search grounding, if tier allows)
    async def find_events(self, area_label: str, when: datetime,
                          intent: str) -> list[EventFinding]: ...
```

`FinancialSnapshot`: `checking_balance: Decimal`, `upcoming_bills_14d: Decimal`, `recent_outing_amounts: list[Decimal]` (dining/entertainment purchases in the last 60 days).

v3 `send_group` implementations: Photon calls the bridge's `POST /send_dm` once per handle (rendering `poll` options into the text); sim appends the message to each handle's outbox. If one handle fails, log it and keep sending to the others.

Every real provider:
- Uses `httpx.AsyncClient` (or the provider's official SDK) with an explicit timeout (`HTTP_TIMEOUT_SEC`; LLM uses `LLM_TIMEOUT_SEC`).
- Retries once on network errors or 5xx (`tenacity`, 2 attempts total).
- Goes through `providers/cache.py` (§14.1).
- Logs provider, method, latency, status. Never logs request bodies containing PII.

---

## 9. External service details

> These shapes are best-known as of planning. **Verify each against current official docs before implementing**, and adapt the provider internals, not the interfaces.
>
> **Free-tier etiquette:** Nominatim and Overpass are community-run. Send a descriptive User-Agent, stay under ~1 request/second, and never call them in loops. All live calls go through the record/replay cache (§14.1).

### 9.1 Photon bridge (`bridge/src/index.ts`)

**Status: built in Stage 0b** (`spectrum-ts` 12.10.1). Run it with `cd bridge && bun run src/index.ts`; credentials go in `bridge/.env`.

Responsibilities, and nothing else:
1. Connect to Photon Spectrum Cloud with `spectrum-ts` and the iMessage provider (`PHOTON_PROJECT_ID`, `PHOTON_PROJECT_SECRET`).
2. On every inbound text message, POST to `BACKEND_URL/webhooks/photon/message`:
   ```json
   {"message_id": "...", "chat_id": "any;-;+16075551234", "is_group": false,
    "sender_handle": "+16075551234", "text": "no sushi pls",
    "ts": "2026-10-03T22:02:11.000Z"}
   ```
   The SDK provides no sender display name (onboarding asks for it) and no location. `ts` is UTC ISO-8601.
3. The bridge also forwards native poll votes to `/webhooks/photon/poll_vote`. **Unused in v3** (text polls only); the backend doesn't need this endpoint.
4. HTTP server (`Bun.serve`) on port 3001:
   - `POST /send_dm` `{handle, text}`: the only send endpoint v3 uses. **Verified working.**
   - `POST /send` `{chat_id, text}` and `POST /send_poll`: group-only, **unused in v3**.
   - `POST /send_image` returns 501 (stretch).
   - `GET /health`
5. Log every in/out event to the console (handle truncated to last 4 digits; never log locations).

The bridge holds no state and makes no decisions. If the backend is down, log and drop.

**Spike results (Stage 0b), full notes in `README.md`:**
- DMs: inbound ✅ and outbound ✅ on real phones.
- Group chats: ❌ on the free plan. Shared line pool, so each person gets a different bot number; Photon's docs say group chats need the paid Business plan. Our live test received nothing from a group. This is why v3 is DM-only.
- Allowlist: the bot only talks to phones registered as users in the Photon project, and each person must text their bot line first. Register every demo phone.
- Shared location: arrived as `custom` content (most likely the SDK's "unsupported message"; not yet confirmed) with no usable coordinates, so typed landmarks only.

Original spike questions (answered above):
- Do group messages and DMs arrive through the same handler, and how is group vs DM distinguished?
- Can the bot **initiate** a DM to a handle that has only spoken in a group? If not, onboarding requires users to DM first (already the design: "DM me 'start'").
- Do native polls work in group chats, and do votes arrive as events?
- What does a shared location look like in the inbound payload, if anything?
- Are there rate or allowlist limits on the hackathon tier?
- **Do group chats work on the free tier, or does the promo code (HACKWITHPHOTON) unlock them?** Free shared lines also cannot message a number that hasn't texted the line first, which the "DM me start" onboarding already handles.

### 9.2 Gemini (`providers/real/gemini.py`)

- Use the official `google-genai` Python SDK. Check current docs for model names and structured-output parameters.
- **Extraction:** system instruction + pseudonymous transcript; request JSON output constrained to the `GroupPreferences` schema (pass the Pydantic model or its JSON schema as the response schema). Temperature 0. Then run the validation rules in §6.3. If the SDK rejects the schema (e.g. unsupported union types), simplify the *wire* schema (e.g. `value` as string) and convert to the Pydantic model in code.
- **Extraction prompt must include:**
  - Current local time and timezone, so "back by 9" → `21:00`.
  - Definitions of HARD / SOFT / VETO / INFERRED with one example each.
  - The allowed `ConstraintField` and `CATEGORY` values.
  - "Only attribute a constraint to the person who said it. Cite message ids. Do not invent constraints. Do not output any prices, distances, or times other than those stated by a person."
- **Explanation:** input is a list of group-safe fact dicts (§13.8). Output: one sentence (≤ 25 words) per plan. Then the number check (§13.8).
- **Rate limits:** free-tier request limits can be low. Record/replay (§14.1) is mandatory during testing so repeated runs don't burn quota. One `@go` should cost exactly 2 Gemini calls (extract + explain).
- **Stage 7 only:** `find_events` uses Gemini with Google Search grounding **if the key's tier supports it** (check the Gemini pricing/rate-limit page for your key). 20 s hard timeout. Results must be resolved to coordinates via `PlacesProvider.geocode`; unresolved events are dropped. If grounding is unavailable, use `fixtures/events_fallback.json`.

### 9.3 OpenStreetMap venues + geocoding (`providers/real/osm_places.py`, `scripts/fetch_venues.py`)

**Venue fixture (run once, by `scripts/fetch_venues.py`):**
- Query Overpass for `amenity` in (`restaurant`, `cafe`, `fast_food`, `bar`, `pub`, `ice_cream`) and `leisure`/`amenity` activity tags (e.g. `bowling_alley`, `cinema`, `escape_game` if present) within ~2.5 km of the demo center. Use `out center;` so ways get a center point.
- Map OSM tags → `Candidate`: `name`, `cuisine` (split on `;`), category from `amenity`, `opening_hours` kept as the raw string, address from `addr:*` tags.
- Write `fixtures/venues.json`. Then a teammate **hand-checks** it: remove closed/irrelevant places, keep ~25 with category variety, and **enter a price tier per venue** (`$`, `$$`, `$$$`, `$$$$`).
- Tier → cost: `$` → $12 (8–15), `$$` → $25 (15–35), `$$$` → $45 (35–60), `$$$$` → $75 (60–100); `status="estimated"`, `source="hand_entered"`. Free activities → $0.

**Opening hours:** parse OSM `opening_hours` only for the simple common forms (e.g. `Mo-Su 11:00-22:00`, `Mo-Fr 07:00-20:00; Sa-Su 09:00-20:00`). Anything else → `open_at_target="unknown"` (no crash, no guess). Do not add a heavy parsing dependency.

**Runtime `search_nearby`:** reads the fixture, filters by category and distance from `center`, and returns `Candidate`s. No live Overpass calls at runtime.

**Runtime `geocode`:**
1. Fuzzy-match the text against `fixtures/demo_locations.json` (names + aliases like "Olin", "Olin Library", "Collegetown").
2. Otherwise call Nominatim `/search?q=<text>&format=jsonv2&limit=1&viewbox=<demo bbox>&bounded=1` with the configured User-Agent, through the cache.
3. Otherwise return `None` (onboarding re-asks).

`text_search` = same as `geocode` but returns a `Candidate`-shaped result (used only by Stage 7).

### 9.4 OpenRouteService (`providers/real/ors.py`)

- Auth: `Authorization: <ORS_API_KEY>` header. Coordinates are **`[lng, lat]`** order. Verify endpoint shapes in the ORS API docs.
- **Profiles:** `foot-walking` (WALK), `cycling-regular` (BIKE), `driving-car` (DRIVE and RIDESHARE).
- **Screening:** `POST /v2/matrix/{profile}` with `locations` = origins + destinations, `sources` = origin indices, `destinations` = destination indices, `metrics = ["duration", "distance"]`, `units = "mi"`. One call per profile actually needed (skip profiles nobody has). A typical `@go` = 2–3 matrix calls.
- **Derived modes (in code, not extra API calls):**
  - DRIVE (own car): `duration = ors_duration + 5` (parking); `fare = mi × DRIVE_COST_PER_MILE_USD + DRIVE_PARKING_USD`.
  - RIDESHARE: `duration = ors_duration + RIDESHARE_PICKUP_WAIT_MIN`; `fare = max(MIN_FARE, BASE + BOOKING + PER_MILE × mi + PER_MIN × ors_duration)`, `status="estimated"`, `source="formula"`.
  - WALK, BIKE: fare 0; `walk_min = duration` for WALK, 0 for BIKE.
- **Detail:** `POST /v2/directions/{profile}` with `instructions=true` only for the winning plan's legs, to produce `RouteStep`s (turn-by-turn text). For RIDESHARE, a single step: "Request a ride to <venue>" plus the ORS drive duration.
- If ORS returns no route for a pair (null in the matrix), that mode is unavailable for that pair. Do not fake it.
- Free-plan limits (verify): ~500 matrix requests/day, ~2,000 directions/day, ~40 requests/minute. Record/replay keeps testing well under this.

### 9.5 Capital One Nessie (`providers/real/nessie.py`)

- REST API, key passed as `?key=` query parameter. Verify base URL and endpoint shapes at the Nessie docs.
- Endpoints used:
  - `GET /customers/{id}`
  - `GET /customers/{id}/accounts`
  - `GET /accounts/{id}/purchases`
  - `GET /accounts/{id}/bills`
  - `GET /merchants/{id}` (to classify purchases as dining/entertainment)
  - Seeding: `POST /customers`, `POST /customers/{id}/accounts`, `POST /merchants`, `POST /accounts/{id}/purchases`, `POST /accounts/{id}/bills`.
- Cache the `FinancialSnapshot` at onboarding time (the vault stores only the resulting limit).
- Nessie can be slow or flaky: always go through the record/replay cache.

---

## 10. Planning pipeline (`planning/pipeline.py`)

Triggered by `@go`. Target end-to-end latency: ≤ 15 s live, ≤ 3 s in replay.

```
1. Snapshot
   - members = the virtual group's members (all READY: enforced at join)
   - handles = members' handles, used for every send_group(handles, ...) below
   - pid_map = {"p1": user_id, ...}  (random order per session; saved in session)
   - constraints = vault.constraints_for(members, pid_map) -> dict[pid, PrivateConstraints]
   - transcript = session_messages → PseudonymousMessage list

2. Extract   (LLMProvider.extract_preferences + validation)
   - On failure or timeout: retry once; then continue with empty preferences and intent "either".

3. Discover  (PlacesProvider — reads the OSM venue fixture)
   - center = centroid of origins (computed inside the pipeline from PrivateConstraints; never sent to the LLM)
   - categories from group_intent and CATEGORY constraints
   - search_nearby(center, radius 2500 m, categories, open_at = now + 30 min)
   - Pre-filter: vetoed cuisines/categories, known-closed; keep top ~20 by rating with category variety
   - [Stage 7] ContextProvider.find_events in parallel (Gemini grounding or fallback list); resolved events added as candidates

4. Route     (RoutingProvider.matrix — ORS, one call per needed profile; drive/rideshare derived)
   - origins = {pid: origin}, destinations = {candidate_id: location}
   - modes = {pid: allowed modes}
   - depart_at = now + 5 min

5. Optimize  (optimizer, pure)
   - plans = optimizer.rank(candidates, estimates, constraints, preferences, now, settings)
   - top3 = optimizer.select(plans, k=3)
   - If 0 feasible: send_group "Nothing fits everyone right now" + the most binding SOFT constraint suggestion; return to COLLECTING.

6. Explain   (facts → LLMProvider.phrase_explanations → number check → template fallback)

7. Guard + send
   - Build GroupSafeMessage with GroupPlanOption list
   - PrivacyGuard.check(message, members' private values) → send or substitute safe template
   - MessagingProvider.send_group(handles, message)   # v3: each member's DM, text poll, no poll_id
   - session.state = POLLING; schedule timeout task
```

After the winner is chosen (`delivery/itinerary.py`):
```
details = {pid: routing.route(origin, venue, mode, depart_at=leave_by)}   # ORS directions for steps
send_group(handles, confirmation with the shared arrival time, rounded to the minute)   # v3: sent first
for each person in winner.assignments:
    send_private(handle, PrivateMessage(text=itinerary text))       # leave_by from the optimizer is exact
```
If `route()` fails for someone, fall back to the screening estimate and a generic instruction ("Walk ~12 min via College Ave").

---

## 11. Privacy isolation

| Layer | Mechanism |
|---|---|
| **Storage** | `private_profiles` is read/written only by `app/private/vault.py`. Group and session tables hold no money or location fields. |
| **Module boundary** | Only `planning/pipeline.py` and `delivery/itinerary.py` may import `app.private.vault`, and only the functions `constraints_for()` and `itinerary_context_for()`. Add a test that greps the codebase for other imports of `app.private` and fails if found. |
| **Pseudonyms** | Optimizer and LLM see `p1..pN` only. The mapping lives in the session row and is resolved only in delivery. |
| **LLM minimization** | Extraction receives transcript text + pids, with no budgets, balances, locations, or names. Explanation receives aggregate group-safe facts only. |
| **Types** | Group messages are built only from `GroupSafeMessage` / `GroupPlanOption`, which have no per-person fields. |
| **Group-safe fan-out (v3)** | A message sent to every member goes through `send_group(handles, GroupSafeMessage)`, never through a loop of `send_private`. That keeps the type rule and the guard in one place. |
| **Aggregation** | Group output never shows per-person cost, per-person travel time, limits, or counts of who is constrained. Infeasible plans are hidden. Prices appear as tiers. Travel appears as "≤N min for everyone." |
| **Output guard** | `messaging/guard.py` scans every outbound group message for: any member's limit (±$1, formats `$25`, `25 dollars`, `25.00`), origin labels, street addresses from profiles, Nessie ids, phone numbers. On match: block, log `privacy_block` (without the value), send a safe template. |
| **Member input** | Preferences DMed during COLLECTING are stored only in `session_messages`. They are never relayed to other members, and the bot never repeats money info from them. Join notices show only display names. |
| **Data retention** | `session_messages` are deleted when a session ends. |

Unit tests must try to leak each private value through the explanation path and assert the guard blocks it.

---

## 12. Budget estimation (`private/budget.py`)

### 12.1 Formula (deterministic)
```
balance            = Σ checking account balances
bills_14d          = Σ bills with payment_date within the next 14 days
buffer             = max(50, 0.10 × balance)
discretionary_room = max(0, balance − bills_14d − buffer)
typical_outing     = median of dining/entertainment purchases in the last 60 days (default 20 if none)
limit_estimate     = round_to_nearest_5( clamp( min(0.15 × discretionary_room, 1.25 × typical_outing), 10, 150 ) )
```
The user confirms or overrides in their DM. **The override always wins.** Only `spend_limit_usd` ever leaves the vault.

### 12.2 Tests
Each persona in §12.3 must produce its expected limit. Zero-balance → 10. No purchases → uses default 20.

### 12.3 Seeded personas (`fixtures/personas.json`, created by `scripts/seed_nessie.py`)

| Persona | Bank code | Checking | Bills (14d) | Recent outings | Expected limit |
|---|---|---|---|---|---|
| Maya | `MAYA1` | $1,200 | $400 rent share | $18, $22, $25, $30 | ~$30 |
| Sam | `SAM1` | $3,500 | $150 phone | $35, $40, $50 | ~$50 |
| Jordan | `JORDAN1` | $420 | $300 tuition payment | $12, $15 | ~$15 |

Adjust amounts so the expected limits come out as listed (verify by running the estimator). Jordan is deliberately tight so the demo shows the budget constraint changing the outcome. The seed script writes the created Nessie ids back into `personas.json` and is idempotent (re-running reuses existing customers).

---

## 13. Optimizer and fairness scoring (`app/optimizer/`)

Pure functions. Inputs: `candidates`, `estimates` (indexed by `(pid, candidate_id, mode)`), `constraints: dict[pid, PrivateConstraints]`, `preferences: GroupPreferences`, `now: datetime`, `params: OptimizerParams`.

### 13.1 Search space
For each candidate `v` (≤20) and each person `i` (N ≤ 6), the feasible modes are `M_i(v) ⊆ {walk, bike, drive, rideshare}` (allowed by `TravelModes`, present in estimates, not refused by a HARD mode preference). Enumerate the Cartesian product: at most 20 × 4⁶ = 81,920 combinations. Brute force in pure Python is still well under a second; if profiling shows otherwise, prune per person to modes not dominated on both duration and cost.

### 13.2 Synchronized arrival (`arrival.py`)
```
ready_i            = AVAILABLE_FROM_i if stated else now + 5 min
earliest_arrival_i = ready_i + duration_i(v, m_i)
T_target           = max_i earliest_arrival_i, rounded up to the next minute
leave_by_i         = T_target − duration_i
arrival_spread     = 0 (v2: all modes are deterministic, so everyone is scheduled to arrive at T_target)
```
The only coupling between people is `T_target`: one slow mode pushes everyone's start later.

### 13.3 Hard filters (`feasibility.py`) — a combination is infeasible if ANY holds
1. **Budget:** `total_cost_i = venue_cost + fare_i` where venue cost uses `high` if known/estimated. Infeasible if `total_cost_i > limit_i`. If cost status is `unknown`, require `value ≤ 0.8 × limit_i` and add risk flag "price unknown."
2. **Veto:** candidate cuisine or category matches any VETO.
3. **Hours:** known closed at `T_target`, or `closes_at < T_target + typical_duration`.
4. **Available until (HARD):** `T_target + typical_duration + duration_i (return estimate) > available_until_i`.
5. **Max walk (HARD):** `walk_min_i > max_walk_i`.
6. **Max travel (HARD):** `duration_i > max_travel_i`.
7. **Mode:** mode not allowed for that person.

### 13.4 Per-person burden (`burden.py`), each term normalized by that person's own tolerance

**Phase 1 (build first):**
```
money_i = total_cost_i / limit_i
time_i  = duration_i / τ_i          τ_i = stated max_travel (any kind) or 30
pref_i  = 1 − sat_i                 sat_i = Σ(conf × match) / Σ conf over i's SOFT + INFERRED prefs; 0.5 if none
          match = 1 if the candidate satisfies the preference (cuisine/category/novelty/mode), 0 otherwise
          ("avoid" polarity: match = 1 if NOT satisfied)
B_i = 0.40·money_i + 0.35·time_i + 0.25·pref_i
```

**Phase 2 (only after the full loop works end to end):**
```
walk_i  = walk_min_i / ω_i          ω_i = stated max_walk or 20
sched_i = (T_target − ready_i) / window_i   if available_until stated, else 0
B_i = 0.30·money + 0.25·time + 0.15·walk + 0.20·pref + 0.10·sched
```
Weights live in `OptimizerParams` so they can be tuned without code changes.

### 13.5 Group objective (`score.py`), lower is better
```
J = λ · max_i B_i + (1 − λ) · mean_i B_i              λ = 0.5   (phase 1)
  + ρ · uncertainty_fraction                           ρ = 0.10  (phase 2)
                                                       (v2: the arrival-spread term is removed)
```
- The **max term** protects the worst-off person (no plan wins by being great for three and miserable for one).
- The **mean term** keeps the plan good overall.
- `λ` is the dial between the two.
- Tie-breaker: smaller `burden_spread = max B − min B`.

For each venue, the best mode assignment is the feasible combination with minimum `J`. Each venue therefore yields at most one plan.

### 13.6 Top-3 selection (`select.py`)
1. Sort venue-plans by `J`.
2. Pick the best.
3. Each next pick: the best remaining plan whose category or primary cuisine differs from all picked plans. If none, take the next best.
4. If fewer than 3 feasible plans exist, return what exists. The pipeline tells the group how many fit.

### 13.7 Required unit tests (`tests/test_optimizer.py`, `tests/test_arrival.py`)
- Budget filter removes a plan that exceeds one person's limit even if it's best for everyone else.
- A VETO removes matching candidates.
- A HARD `available_until` removes a plan whose end time is too late.
- **Fairness case:** construct two venues where venue X has lower total travel time but one person travels 45 min, and venue Y has slightly higher total but max 20 min. Assert Y ranks above X with λ = 0.5, and X wins with λ = 0.
- `T_target` and `leave_by` are correct for mixed modes.
- **Ride-share budget case:** a person whose limit can't cover venue + ride-share is assigned WALK (or the plan is infeasible if walking breaks a HARD time limit); a person with a larger limit gets RIDESHARE when it lowers `J`.
- Diversity: three sushi places with the best scores do not fill all three slots if other categories are feasible.
- Determinism: same inputs → same output order.

### 13.8 Explanations (`facts.py` + LLM)
Facts per plan (group-safe only):
```json
{"label": "A", "venue": "Koko", "category": "food", "cuisine": "korean",
 "fits_all_budgets": true, "max_travel_min": 22, "next_best_max_travel_min": 31,
 "arrival_window_min": 4, "vetoes_respected": ["sushi"],
 "matches_group_wants": ["korean", "food soon"]}
```
After the LLM phrases each explanation, extract every number from the text; if any number isn't in that plan's facts, replace the explanation with a template: "Fits everyone's budget; longest trip {max_travel_min} min."
Do not include `binding_constraint` facts that name a person.

---

## 14. Record/replay cache, mocks, and the simulator

### 14.1 Record/replay (`providers/cache.py`)
- Wrapper used by every real provider: `await cache.call(provider="routes", method="matrix", request=<json-serializable>, fn=<async callable>)`.
- Key = `sha256(provider + method + canonical_json(request))`. Exclude volatile fields (e.g. exact timestamps) by normalizing `depart_at` to the nearest 15 min before hashing.
- File: `fixtures/recorded/{provider}/{key}.json`.
- `CACHE_MODE=off`: always call live.
- `CACHE_MODE=record`: call live, write the response.
- `CACHE_MODE=replay`: return the file if present; on a miss, call live and record it (log `cache_miss`).
- Rehearse the exact demo in `record` mode, commit the recordings, and run the stage demo in `replay` mode.

### 14.2 Mocks (only these)
- `mock/sim_messaging.py`: appends to an in-memory outbox per handle (v3); used by the simulator and tests.
- `mock/routing.py`: haversine × 1.3 detour. Walk 3 mph. Bike 10 mph. Drive 18 mph + 5 min parking. Ride-share = drive speed + pickup wait, fare from the same formula as `ors.py` (share the formula function). Used in tests and as fallback.
- `mock/places.py`: returns `fixtures/venues.json`, filtered by category and distance; geocodes only from `demo_locations.json`.

There is no mock LLM or mock finance. Tests that need them use recorded responses in replay mode, or stub the provider directly in the test.

### 14.3 Simulator (`api/sim.py`, enabled only when `PROVIDER_MESSAGING=sim`)
- `POST /sim/message` `{sender_handle, text}` → runs the same router as the real webhook as a DM (generates `message_id`, `chat_id`, and `ts`; `is_group=false`). v3: votes are just DMs (`text: "A"`), so there is no `/sim/vote`.
- `GET /sim/outbox/{handle}` → list of messages sent to that handle (group-safe and private).
- `POST /sim/reset` → clears DB and outbox.

`scripts/run_demo_scenario.py` drives the full §18 script through these endpoints and prints each person's outbox. It doubles as the backup demo if iMessage fails.

---

## 15. Staged build plan

Work assignment for 4 people (all code against the Stage 0 models and protocols, so work runs in parallel):
- **A — Core:** Stages 0, 1, 2 (router, FSMs, optimizer).
- **B — Photon:** Stage 0b, then 6.
- **C — LLM:** Stage 3 (Gemini), then 7 (stretch).
- **D — Data:** Stages 4 and 5 (OSM fixture, ORS, Nominatim, Nessie, record/replay).

Suggested clock targets assume Saturday daytime start; adjust to actual time but **keep the 3 AM freeze**.

| Stage | Deliverable | Depends on | Exit criterion | Target |
|---|---|---|---|---|
| **0. Skeleton** ✅ done | `pyproject.toml`, settings, logging, all models (§6), protocols (§8), DB tables, `GET /health`, ruff + pytest set up | — | App boots; models import; empty test suite passes | done |
| **0.5. v2 migration** | Apply the v2 changes to Stage 0 code: settings (§5), `Mode`, `TravelModes`, `OnboardingState`, `RouteStep`, `RouteEstimate.source`, `Candidate.source`, `Uncertain` source comment, `FinancialSnapshot`/`EventFinding` (§6.9), deps (§20), `.env.example`. Update `test_skeleton.py` | 0 | Tests + ruff pass; no references to `xai`, `grok`, `google_maps`, `transit` remain in `app/` | +0.5 h |
| **0b. Photon spike** ✅ done | Bridge built (`bridge/`); DMs verified both ways; groups not available on the free plan, which led to the v3 pivot. Answers in README / §9.1 | — | All §9.1 spike questions answered | done |
| **1. Mock vertical slice** | Simulator, sim messaging, mock routing + places, router, onboarding FSM (bank code can be stubbed to a fixed limit), session FSM, phase-1 optimizer, text poll, personal DMs | 0 | `test_end_to_end.py` runs §18 through the simulator with mocks and a stubbed LLM | +6 h |
| **1.5. v3 DM-only migration** | Apply the Stage 1.5 checklist below to the Stage 1 code | 1 | `test_end_to_end.py` runs the **v3** §18 script (DMs + join code) through the simulator; tests + ruff pass | +1.5 h |
| **2. Optimizer + privacy** | Full feasibility filters, arrival logic, diversity selection, facts, PrivacyGuard, all §13.7 and privacy tests | 1 | All optimizer and guard tests pass | +8 h |
| **3. Gemini extraction** | Real extraction + validation; explanation + number check; 3–5 golden transcripts | 1 | ≥ 90% of expected constraints extracted on golden transcripts; template fallback on failure | +8 h |
| **4. OSM + ORS** | `fetch_venues.py` (Overpass) + hand-checked fixture with price tiers; `osm_places.py` with Nominatim geocoding; `ors.py` matrix + directions + ride-share derivation; record/replay cache | 1 | Real 3-person scenario returns ORS durations for walk/bike/drive and correct ride-share costs | +9 h |
| **5. Nessie** | `seed_nessie.py`, real FinanceProvider, budget estimator + tests, link-by-bank-code onboarding | 1 | Three personas produce expected limits; override works | +9 h |
| **6. Photon integration** | `providers/real/photon.py` (`send_group` = `/send_dm` per handle; `send_private` = `/send_dm`), webhook wired to the bridge, text poll | 0b, 1.5 | Full v3 §18 demo on real phones with mock data providers | +12 h |
| **— Full-loop checkpoint** | Everything real, end to end on phones | 2–6 | One complete live run | +13 h |
| **Phase 2 scoring** | walk/sched burden terms, arrival spread, uncertainty penalty | checkpoint | Tests still pass; demo outcome still sensible | +15 h |
| **7. Events (stretch)** | `find_events` via Gemini grounding (if tier allows) or `events_fallback.json`; geocode resolution | 3, 4 | At least one event appears as a candidate with a source URL | +16 h |
| **8. Demo hardening** | Record the rehearsal, replay mode, progress messages, error copy, README, Devpost text, backup video | all | Two clean full rehearsals in a row | **freeze by 3 AM** |

### Stage 1.5 checklist (v3 DM-only migration of the Stage 1 code)
Where the Stage 1 code depends on group chats (from a scan of `main`): `providers/protocols.py` + `mock/sim_messaging.py` + `messaging/outbound.py` (`send_group(chat_id)`); `db/tables.py` + `db/queries.py` (`chat_id`, `upsert_group`, `group_by_chat` → add `group_by_code` and `active_group_for_user`); `planning/session.py` (poll timer keyed by `chat_id` → key by group id); `planning/pipeline.py`, `delivery/itinerary.py`, `onboarding/fsm.py:138` (each `send_group(group.chat_id, …)`); `conversation/router.py` (per-chat locks, `handle_vote`); `api/sim.py` (`chat_id`, `is_group`, `/vote`); `models/identity.py`. `api/webhooks.py`'s `/poll_vote` can stay; it's harmless and unused.

1. **Protocol (§8):** `MessagingProvider.send_group(self, handles: list[str], msg: GroupSafeMessage) -> None`. Update `providers/mock/sim_messaging.py` to append to each handle's outbox.
2. **Models + tables (§6.1, §6.8):** `Group.chat_id` → `Group.join_code`; `groups.chat_id` → `groups.join_code` (unique). Delete `app.db` (no migrations; `create_all()` rebuilds it).
3. **Commands (`conversation/commands.py`):** add `join <code>`. `@plan`, `@go`, `@cancel`, `@pick X`, and `A`/`B`/`C` are now parsed from **DMs**.
4. **Router (§7.1):** remove the group branch and the group intro. Route READY users' DMs: session commands → settings commands → store in `session_messages` if COLLECTING → help. Use one global `asyncio.Lock`.
5. **Session FSM (§7.3):** `@plan` creates the group + join code; `join` adds members while COLLECTING (cap 6); `@go` needs ≥ 2 members; any member can `@cancel`; votes are DM replies. Every "tell the group" becomes `send_group(handles, ...)`. On DONE/CANCELLED, clear `active_session_id` and delete `session_messages`.
6. **Onboarding (§7.2):** the READY message points to `@plan` / `join <code>`; no group announcement. A `join` from a non-READY user starts onboarding.
7. **Copy (§7.4):** add `PLAN_STARTED`, join notice, `NOTED`, `HELP`; poll ends with "Reply A, B, or C"; confirmation ends with "Your route is below 👇".
8. **Delivery (§10):** send the group confirmation **before** the personal itineraries.
9. **Simulator (§14.3):** `/sim/message` takes `{sender_handle, text}`; remove `/sim/vote`; `/sim/outbox/{handle}`.
10. **Tests:** rewrite `test_end_to_end.py` for the v3 §18 script. Also assert the poll and confirmation reach every member and that no member's DMs contain another member's private values.

**Feature freeze at 3 AM Sunday.** After that: bug fixes, rehearsal, Devpost (draft by 6 AM, submit by 8:00 AM).

---

## 16. Out of scope (do not build)

- Reading chat history from before `@plan` (or from before a member joined).
- iMessage group chats (need Photon's paid Business plan), relaying members' messages to each other, and native polls (v3).
- Find My location sharing via Photon's Advanced iMessage kit (`im.locations`). Real, but outside the plan; typed landmarks instead.
- Supabase/Postgres, deployment, Docker, CI pipelines.
- HMAC/auth between bridge and backend.
- Live re-planning when someone is late; live location tracking.
- Multi-stop plans.
- Learned weights / any ML model.
- Public transit. (Stretch only, after Stage 8: direct-trip routing from TCAT's GTFS schedule files.)
- Any Google Maps Platform or xAI API (paid; replaced in v2).
- Real ride-share APIs or booking (cost is a formula estimate).
- Real bank account linking (Nessie sandbox only).
- A web frontend (a judge-facing debug page only if everything else is done).
- Mock LLM or rule-based extractor.
- Bill splitting via Nessie transfers (mention as future work; build only if every stage above is done before freeze).

---

## 17. Testing strategy

Priority order (write in this order if time is short):
1. `test_optimizer.py`, `test_arrival.py` — correctness of the showpiece.
2. `test_privacy_guard.py` — attempted leaks via explanation, poll title, and error messages; plus the import-boundary test.
3. `test_budget.py` — persona limits, edge cases.
4. `test_end_to_end.py` — simulator + mocks + stubbed LLM, v3 flow (`@plan` → `join <code>` → private DMs → `@go` → `A` replies); asserts: every member receives the same poll with ≤3 options, no private values in any group-safe message, no member's DMs contain another member's private values, each member gets a distinct itinerary, all `leave_by` times produce the same `T_target`.
5. `test_extraction.py` — golden transcripts in replay mode.

Golden transcript format (`fixtures/transcripts/*.json`):
```json
{"now_local": "2026-10-03T18:00:00-04:00",
 "messages": [{"message_id": "m1", "pid": "p1", "text": "I'm starving"},
              {"message_id": "m2", "pid": "p2", "text": "no sushi pls"},
              {"message_id": "m3", "pid": "p3", "text": "I have to be back by 9"}],
 "expected": [{"pid": "p1", "field": "category", "value": "food", "kind": "inferred"},
              {"pid": "p2", "field": "cuisine", "value": "sushi", "polarity": "avoid", "kind": "veto"},
              {"pid": "p3", "field": "available_until", "value": "21:00", "kind": "hard"}]}
```
Match on `(pid, field, value, kind)`; ignore confidence.

---

## 18. Demo script

**Cast:** Maya, Sam, Jordan on three real phones, each in a DM with their own bot line. A fourth teammate presents with the laptop (running backend + bridge, `CACHE_MODE=replay`) and the simulator ready as backup. Mirror the three phones on screen if possible so judges can follow all three DMs.

**Before judging:** all three already onboarded and texted their bot line at least once (shows faster, and the shared lines require it). Onboarding is shown live only if judges ask, or via a 20-second pre-recorded clip.

1. **Setup (10 s):** "Our group chat can never decide on dinner, and nobody wants to say 'I'm broke' in front of everyone. So you tell our bot privately."
2. **Form the group:**
   - Maya → bot: `@plan`
   - Bot → Maya: "Plan started! 🎉 Tell your friends to text me: join K7QP …"
   - Sam → bot: `join K7QP`; Jordan → bot: `join K7QP`
   - Everyone sees: "✅ Sam joined (2 people)." / "✅ Jordan joined (3 people)."
3. **Say what you want, privately (each in their own DM):**
   - Maya: "I'm starving"
   - Sam: "no sushi pls" … later: "something we haven't tried?"
   - Jordan: "I have to be back by 9, and nothing too far"
   - Maya: `@go`
4. **Bot → all three:** "🔎 Looking at options…" then the same 3-option poll with one-line explanations.
5. **Vote:** two people reply `A` → all three get "🎉 Plan A: Koko. Everyone arrives around 6:42. Your route is below 👇" followed by their own itinerary.
6. **Reveal:** hold up the three phones. Sam takes a ~$9 ride-share and leaves last. Maya bikes. Jordan walks, because a ride-share would break his budget, so he leaves first. Different modes and leave-by times, same arrival. Nobody ever saw anyone else's budget, location, or what they asked for.
7. **Explain the tech (60 s):** fairness objective (show the λ trade-off), synchronized arrival, money-vs-time mode choice per person, privacy guard (show that the LLM prompt contains no money), Nessie-derived limits, built on OpenStreetMap.

**Fallbacks:** if iMessage fails, run `scripts/run_demo_scenario.py` and show outboxes; if that fails, play the backup video.

---

## 19. Risks and verification checklist

| Risk | Mitigation | Verify by |
|---|---|---|
| Photon free plan: no group chats, a different bot number per person (confirmed in 0b) | v3 DM-only virtual group with join codes; text polls | Done (0b) |
| Photon allowlist: bot only talks to registered phones that texted it first | Register all demo phones in the Photon project; each texts its bot line before judging | Stage 6, Stage 8 |
| Inbound DMs stop arriving after a bridge restart (seen once in 0b, not yet explained) | Verify inbound with a "ping" DM after every bridge start; don't restart the bridge during judging | Stage 6 |
| Bot cannot see messages before `@plan` | Designed in: collection starts at `@plan` | — |
| No transit mode | Ride-share replaces it in the story; GTFS transit is a post-freeze stretch | — |
| OSM data gaps (missing hours, odd names) | Hand-checked fixture; unknown hours treated as unknown, not guessed | Stage 4 |
| Free-tier rate limits (Gemini, ORS, Nominatim) | Record/replay during all testing; 2 Gemini calls and 2–3 ORS calls per `@go` | Stages 3–4 |
| LLM latency or bad JSON | Temperature 0, schema, validation, one retry, empty-preferences fallback | Stage 3 |
| Nessie slow/flaky | Record/replay; snapshot at onboarding | Stage 5 |
| API keys missing | Get Gemini, ORS, and Nessie keys during Stage 0.5 | Stage 0.5 |
| Venue wifi drops during judging | Replay mode; phone hotspot; simulator backup; video | Stage 8 |
| Privacy leak through explanation text | Number check + PrivacyGuard + tests | Stage 2 |

**Open decisions (team to confirm, defaults in bold):**
1. Demo area: **Ithaca (Collegetown/campus)**.
2. Group size: **3 in the demo**, cap at 6.
3. Driving cost: **miles × $0.20 + $3 parking**. Ride-share formula defaults in §5 (estimates; tune so a typical Collegetown → downtown ride is ~$9–12).
4. Product name: replace `[NAME]` everywhere (`copy.py`, README, Devpost).

---

## 20. Coding conventions

- **Python deps (pyproject):** `fastapi`, `uvicorn[standard]`, `pydantic>=2`, `pydantic-settings`, `sqlalchemy[asyncio]>=2`, `aiosqlite`, `httpx`, `tenacity`, `google-genai`, `python-dateutil` (plus `staticmap` only if the map-image stretch is built); dev: `pytest`, `pytest-asyncio`, `ruff`. Nothing else without asking.
- **Bridge deps:** `spectrum-ts` (or the scoped `@spectrum-ts/core` + `@spectrum-ts/imessage` packages, per its README). Use Bun's built-in HTTP server and `fetch`.
- Type hints everywhere; Pydantic models at every module boundary.
- `async` for all I/O. No blocking calls in request paths.
- All user-facing strings in `conversation/copy.py`.
- All tunable numbers (weights, defaults, timeouts) in `settings.py` or `OptimizerParams`, never inline.
- Times: store UTC, convert to `DEMO_TIMEZONE` only for display and LLM prompts.
- Money: `Decimal`, rounded to cents for display; `$` tiers for the group.
- Errors: catch at the pipeline/router boundary, log, and send a friendly message. Never let an exception kill the background task silently.
- Commit after each stage with a message naming the stage.
