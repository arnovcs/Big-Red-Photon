# [NAME] — Architecture & Implementation Plan

> An iMessage agent that turns group-chat debates into a plan everyone can afford and reach, and gets the whole group there at the same time.

**Status:** approved plan, ready to build. No application code yet.
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
A bot that joins an iMessage group chat. Each member privately links a (sandbox) bank account and shares where they're starting from. When the group says `@plan`, the bot listens to the discussion, extracts preferences, finds nearby options, routes every person to every option by walking, transit, or driving, and picks the plans that are fairest to the worst-off person while staying inside **everyone's** private budget. The group votes in a poll. Then each person gets a **private** DM with their own travel mode, cost, and leave-by time, calculated so the whole group **arrives together**.

### Why it fits "Navigation"
Navigation is the core, not a side feature: multi-origin, multi-modal routing to a shared destination, with departure scheduling so arrivals converge. Money and preferences are constraints on that navigation problem.

### What makes it technically interesting (pitch points)
- **Fair group optimization:** exact enumeration with a min-max + mean objective, so no one person carries the cost of the plan.
- **Synchronized arrival:** work backward from a shared target arrival time to compute each person's leave-by time.
- **Privacy by construction:** budgets never reach the group chat or the LLM. Enforced by module boundaries, types, and an output scanner.
- **Grounded LLM use:** the LLM extracts preferences and phrases explanations, but every number (time, distance, cost, hours) comes from real APIs, and explanations are checked against computed facts.

---

## 2. Guiding decisions

| Decision | Choice | Reason |
|---|---|---|
| Backend | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, `uv` | Team preference. One async process. |
| iMessage | Photon `spectrum-ts` in a **thin TypeScript bridge** (`bridge/`) | Photon's SDKs are TypeScript. The bridge only relays; all logic stays in Python. |
| Bridge ↔ backend | Plain JSON over HTTP on **localhost** | Both run on one laptop. No HMAC, no auth. |
| Database | SQLite via SQLAlchemy | Zero setup. No Postgres or Supabase. |
| Routing | Google Routes API (`computeRouteMatrix`, `computeRoutes`) | Only mainstream API with walk + drive + transit with schedules and arrival-time targeting. |
| Places | Google Places API (New) | Same key as Routes. Price level, hours, types, ratings. Also geocodes text. |
| LLM | xAI Grok (structured output) for extraction and explanation | Team choice. Model name is config. |
| Live events | Grok `web_search` | **Stretch only** (Stage 7). |
| Finance | Capital One Nessie sandbox + deterministic estimator | LLM never sees balances. |
| Optimizer | Exact enumeration, hard filters, min-max + mean burden | Small search space; fully explainable; no solver dependency. |
| Demo reliability | Record/replay cache + simulator + two small mocks | Real data on stage without live-API risk. |
| Hosting | One team laptop + phone hotspot backup | Spectrum holds an outgoing connection; no public URL needed. |
| ML | None | No fake ML. |

---

## 3. System architecture

```
          ┌──────────────────────────── iMessage ────────────────────────────┐
          │  Group chat (A, B, C, bot)          DMs (A↔bot, B↔bot, C↔bot)      │
          └──────────────┬─────────────────────────────────▲─────────────────┘
                         │ Photon Spectrum (gRPC stream)   │
          ┌──────────────▼─────────────────────────────────┴─────────────────┐
          │ bridge/ (TypeScript, Bun, ~150 LOC, port 3001)                    │
          │  • on message / poll vote  → POST http://localhost:8000/webhooks  │
          │  • POST /send, /send_dm, /send_poll, /send_image ← from backend   │
          └──────────────┬─────────────────────────────────▲─────────────────┘
                         │ JSON over localhost HTTP        │
┌────────────────────────▼─────────────────────────────────┴──────────────────────────┐
│ app/ (FastAPI, port 8000)                                                            │
│                                                                                      │
│  api/webhooks.py ─► conversation/router.py (group vs DM, commands, session lookup)   │
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
5. **Webhooks return immediately.** All processing runs as background tasks, serialized per chat with an `asyncio.Lock` so messages in one chat are handled in order.

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
│   │   ├── webhooks.py           # POST /webhooks/photon/message, /webhooks/photon/poll_vote
│   │   ├── sim.py                # Simulator endpoints (§14.3)
│   │   └── health.py             # GET /health
│   │
│   ├── conversation/
│   │   ├── router.py             # Dispatch: group vs DM, command vs free text, state
│   │   ├── commands.py           # Parse @plan, @go, @cancel, @pick, start, yes/no, numbers
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
│   │   └── poll.py               # send poll (native or text), tally votes, pick winner
│   │
│   ├── delivery/
│   │   └── itinerary.py          # detailed routes for winner; per-person DMs; map image (stretch)
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
│   │   │   ├── grok.py           # LLMProvider (+ ContextProvider in Stage 7)
│   │   │   ├── places.py         # Google Places (New)
│   │   │   ├── routes.py         # Google Routes
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
│   ├── venues.json               # ~25 real venues around the demo area (mock Places + replay seed)
│   ├── demo_locations.json       # named starting points with lat/lng
│   ├── personas.json             # Nessie seed definitions (§12.3)
│   ├── transcripts/              # golden group-chat transcripts + expected extraction
│   └── recorded/                 # record/replay cache files (committed)
│
├── scripts/
│   ├── seed_nessie.py            # creates demo customers/accounts/purchases/bills
│   ├── fetch_venues.py           # pulls venues from Places into fixtures/venues.json
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
| `XAI_API_KEY` | | Grok |
| `XAI_MODEL` | (fill from current xAI docs) | Model id for extraction + explanation |
| `GOOGLE_MAPS_API_KEY` | | Places + Routes (+ Static Maps if used) |
| `NESSIE_API_KEY` | | Nessie |
| `NESSIE_BASE_URL` | `http://api.nessieisreal.com` | Verify scheme/host against Nessie docs |
| `BRIDGE_URL` | `http://localhost:3001` | Backend → bridge |
| `BACKEND_URL` | `http://localhost:8000` | Bridge → backend (bridge's own env) |
| `PHOTON_PROJECT_ID`, `PHOTON_PROJECT_SECRET` | | Bridge only |
| `DATABASE_URL` | `sqlite+aiosqlite:///./app.db` | |
| `PROVIDER_MESSAGING` | `photon` \| `sim` | |
| `PROVIDER_PLACES` | `google` \| `mock` | |
| `PROVIDER_ROUTING` | `google` \| `mock` | |
| `PROVIDER_FINANCE` | `nessie` | (no mock; seeded customers serve that role) |
| `CACHE_MODE` | `off` \| `record` \| `replay` | §14.1 |
| `DEMO_TIMEZONE` | `America/New_York` | All "by 9" style times are local |
| `DEMO_AREA_LABEL` | `Ithaca, NY` | Used in prompts and Places queries |
| `DEMO_CENTER_LAT`, `DEMO_CENTER_LNG` | | Fallback search center |
| `TRANSIT_FARE_USD` | `1.50` | Verify local fare; used when Routes gives no fare |
| `DRIVE_COST_PER_MILE_USD` | `0.20` | |
| `DRIVE_PARKING_USD` | `3.00` | |
| `OPTIMIZER_LAMBDA` | `0.5` | Worst-off vs mean weight |
| `POLL_TIMEOUT_SEC` | `300` | Auto-pick the leader after this |
| `GROK_TIMEOUT_SEC` | `20` | |

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
    AWAITING_CAR = "awaiting_car"
    READY = "ready"

class User(BaseModel):
    id: UUID
    handle: str                     # phone/email from iMessage; never shown to others
    display_name: str               # first name; asked in onboarding if unknown
    dm_chat_id: str | None          # Photon chat id of the DM with this user
    onboarding_state: OnboardingState

class Group(BaseModel):
    id: UUID
    chat_id: str                    # Photon group chat id
    member_ids: list[UUID]          # users seen speaking in this group (+ membership events if available)
    active_session_id: UUID | None
```

### 6.2 Private data (vault only)

```python
class TravelModes(BaseModel):
    walk: bool = True
    transit: bool = True
    drive: bool = False

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
    MODE_PREFERENCE = "mode_preference"     # "walk" | "transit" | "drive" with polarity

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
    source: str                     # "google_places" | "fixture" | "config" | "grok:<url>"

class Candidate(BaseModel):
    candidate_id: str               # "gp:<place_id>" | "fx:<slug>" | "ev:<hash>"
    name: str
    category: str                   # food | bar | cafe | dessert | activity | event
    cuisines: list[str] = []
    location: LatLng                # REQUIRED, from Places or fixture — never from the LLM
    address: str
    est_cost_pp: Uncertain[Decimal]
    open_at_target: Literal["open", "closed", "unknown"] = "unknown"
    closes_at: datetime | None = None
    typical_duration_min: int       # category default: food 60, cafe 45, dessert 30, bar 90, activity 90
    rating: float | None = None
    source: Literal["google_places", "fixture", "grok_event"]
    novelty_tags: list[str] = []
```

### 6.5 Routing

```python
class Mode(StrEnum):
    WALK = "walk"
    TRANSIT = "transit"
    DRIVE = "drive"

class RouteEstimate(BaseModel):
    origin_pid: str
    candidate_id: str
    mode: Mode
    duration_min: float
    distance_mi: float
    walk_min: float                 # walking portion; equals duration for WALK
    fare_usd: Uncertain[Decimal]    # transit: Routes fare or config; drive: miles × rate + parking; walk: 0
    source: Literal["google_routes", "mock"]

class RouteStep(BaseModel):
    mode: Literal["walk", "transit", "drive"]
    instruction: str
    duration_min: float
    line_name: str | None = None
    depart_stop: str | None = None
    arrive_stop: str | None = None
    depart_time: datetime | None = None

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
    arrival_spread_min: float

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

### 6.8 Database tables (SQLAlchemy)

| Table | Columns | Notes |
|---|---|---|
| `users` | id, handle (unique), display_name, dm_chat_id, onboarding_state, created_at | |
| `groups` | id, chat_id (unique), active_session_id, created_at | |
| `group_members` | group_id, user_id | |
| `private_profiles` | user_id (PK), nessie_customer_id, spend_limit_usd, limit_source, origin_lat, origin_lng, origin_label, modes_json, updated_at | **Only `app/private/` may query this table.** |
| `sessions` | id, group_id, state, started_at, target_time, pid_map_json, preferences_json, plans_json, poll_id, winner_plan_id | `plans_json` stores full plans; `pid_map_json` maps pid → user_id |
| `session_messages` | id, session_id, message_id (unique), sender_user_id, text, ts | Only messages during COLLECTING. Deleted when session reaches DONE or CANCELLED. |
| `votes` | session_id, user_id, option_label, ts | One row per user per session (upsert) |
| `processed_messages` | message_id (PK) | Idempotency for webhook retries |

Use `create_all()` on startup. No migrations.

---

## 7. Conversation flows and state machines

### 7.1 Message routing (`conversation/router.py`)

For every inbound message:
1. If `message_id` is in `processed_messages`, ignore. Otherwise insert it.
2. Upsert the `User` by handle. If the chat is a group, upsert the `Group` and membership.
3. If the message is from a **DM** → onboarding FSM (§7.2), unless the user is READY, in which case handle DM commands (`budget <n>`, `location`, `car yes|no`, `help`).
4. If the message is from a **group**:
   - Parse commands: `@plan`, `@go`, `@cancel`, `@pick A|B|C`, poll replies `A`/`B`/`C` (only while POLLING).
   - Otherwise, if the group's session is COLLECTING, store it in `session_messages`.
   - Otherwise ignore silently.
5. First time the bot sees a group → send the intro message (§7.4).

Commands are case-insensitive and may appear with surrounding text ("ok @go"). The wake word should also accept the bot's display name if Photon exposes mentions; otherwise plain text matching is enough.

### 7.2 Onboarding FSM (DM only, `onboarding/fsm.py`)

```
NEW ──"start" (or any first DM)──► ask name if unknown, then:
AWAITING_BANK_CODE ──valid code──► vault.link_customer() → budget.estimate()
                                   → "About $25 looks comfortable tonight. Use that? (yes / or type a number)"
AWAITING_LIMIT_CONFIRM ──"yes" | number──► save limit (source = estimate | override)
AWAITING_LOCATION ──text──► places.geocode(text, near=demo center) → "Got it: Olin Library. Right? (yes/no)"
                    ──"no"──► ask again
AWAITING_CAR ──"yes"|"no"──► TravelModes(drive=…)
READY ──► DM: "You're set." Group: "✅ Maya is set (2/3)."  (no details)
```

- **Bank code:** a short code per seeded persona (e.g. `MAYA1`), mapped to a Nessie customer id in `fixtures/personas.json` after seeding. This simulates "linking a bank account."
- If a shared-location attachment arrives and the bridge can parse coordinates, accept it directly. Otherwise the typed-landmark path is the default.
- Any invalid input re-asks with a short hint. Never echo dollar amounts in group chats.

### 7.3 Planning session FSM (`planning/session.py`)

```
IDLE ──@plan──► COLLECTING ──@go──► RUNNING ──pipeline ok──► POLLING
                    │                  │                         │
                 @cancel          pipeline error              votes / @pick / timeout
                    ▼                  ▼                         ▼
                  IDLE        "Couldn't finish — say @go     CONFIRMED ──► DELIVERING ──► DONE
                              to retry" → COLLECTING
any state ──@cancel──► CANCELLED → IDLE
```

- **COLLECTING:** the bot only reads messages sent **after** `@plan`. (Photon's adapter cannot fetch chat history, so there is no "read the last 30 messages" behavior.) On entry: "Listening — tell me what you're feeling. Say @go when ready."
- On `@plan`, if some members aren't READY: "Planning with Maya and Sam. Jordan, DM me 'start' to be included." Proceed with READY members only. Require at least 2.
- **RUNNING:** send "🔎 Looking at options…" then run the pipeline (§10) as a background task.
- **POLLING:** native poll if supported, otherwise text poll ("Reply A, B, or C"). Winner = first option to reach ⌈N/2⌉ votes, or `@pick X` by anyone, or the leader after `POLL_TIMEOUT_SEC` (ties → better score).
- **DELIVERING:** compute detailed routes for the winner, send each person's DM, then the group confirmation.
- One active session per group. `@plan` during an active session replies "Already planning — say @cancel to start over."

### 7.4 Message copy (all in `conversation/copy.py`)

Group intro:
> Hi! I'm [NAME]. I help this chat pick a plan everyone can afford and reach, and I get you there at the same time. Each of you: DM me "start" to set up privately. I never share anyone's money or location here.

Poll message:
> Here are 3 plans that fit everyone's constraints:
> **A** — Koko (Korean) · ≤22 min for everyone · $$ · arrive within 4 min
> Keeps everyone within budget and the longest trip is 22 min.
> **B** — …
> Vote A, B, or C.

Confirmation:
> 🎉 Plan A: Koko. Everyone arrives around 6:42. Check your DMs for your route.

Personal DM:
> Your plan for tonight: **Koko**, 123 College Ave.
> 🚌 Leave by **6:24**. Walk 4 min to Schwartz Center, Route 10 bus (3 stops), walk 3 min.
> Arrive ~6:41. Estimated total: $27.50 (food + fare).

Keep copy short, warm, and free of jargon.

---

## 8. Provider interfaces (`app/providers/protocols.py`)

Do not change these signatures without asking. Implementations live in `providers/real/` and `providers/mock/`.

```python
class MessagingProvider(Protocol):
    async def send_group(self, chat_id: str, msg: GroupSafeMessage) -> str | None: ...  # returns poll_id if poll sent
    async def send_private(self, handle: str, msg: PrivateMessage) -> None: ...

class FinanceProvider(Protocol):
    async def get_customer(self, customer_id: str) -> dict: ...
    async def get_financial_snapshot(self, customer_id: str) -> FinancialSnapshot: ...

class PlacesProvider(Protocol):
    async def search_nearby(self, center: LatLng, radius_m: int, categories: list[str],
                            open_at: datetime) -> list[Candidate]: ...
    async def text_search(self, query: str, near: LatLng) -> list[Candidate]: ...
    async def geocode(self, text: str, near: LatLng) -> tuple[LatLng, str] | None: ...  # (coords, clean label)

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

class ContextProvider(Protocol):       # Stage 7 stretch
    async def find_events(self, area_label: str, when: datetime,
                          intent: str) -> list[EventFinding]: ...
```

`FinancialSnapshot`: `checking_balance: Decimal`, `upcoming_bills_14d: Decimal`, `recent_outing_amounts: list[Decimal]` (dining/entertainment purchases in the last 60 days).

Every real provider:
- Uses `httpx.AsyncClient` with an explicit timeout (10 s default; Grok 20 s).
- Retries once on network errors or 5xx (`tenacity`, 2 attempts total).
- Goes through `providers/cache.py` (§14.1).
- Logs provider, method, latency, status. Never logs request bodies containing PII.

---

## 9. External service details

> These shapes are best-known as of planning. **Verify each against current official docs before implementing**, and adapt the provider internals, not the interfaces.

### 9.1 Photon bridge (`bridge/src/index.ts`)

Responsibilities, and nothing else:
1. Connect to Photon Spectrum Cloud with `spectrum-ts` and the iMessage provider (`PHOTON_PROJECT_ID`, `PHOTON_PROJECT_SECRET`). Read the `spectrum-ts` README for the exact init and message-handler API.
2. On every inbound message (group or DM), POST to `BACKEND_URL/webhooks/photon/message`:
   ```json
   {"message_id": "...", "chat_id": "...", "is_group": true,
    "sender_handle": "+16075551234", "sender_name": "Maya" , "text": "no sushi pls",
    "ts": "2026-10-03T18:02:11-04:00",
    "location": {"lat": 42.44, "lng": -76.48} }
   ```
   `sender_name` and `location` are optional; include only if the SDK provides them.
3. On poll votes (if native polls work), POST to `/webhooks/photon/poll_vote`: `{"poll_id", "chat_id", "voter_handle", "option_index"}`.
4. Expose a tiny HTTP server (Bun's built-in `Bun.serve`) on port 3001:
   - `POST /send` `{chat_id, text}`
   - `POST /send_dm` `{handle, text}` — sends to a handle, creating/resolving the DM chat
   - `POST /send_poll` `{chat_id, title, options: string[]}` → `{poll_id}`; return HTTP 501 if unsupported
   - `POST /send_image` `{chat_id | handle, path}` (stretch)
   - `GET /health`
5. Log every in/out event to the console (handle truncated to last 4 digits).

The bridge holds no state and makes no decisions. If the backend is down, log and drop.

**Spike questions** (answer in Stage 0b, record answers in `README.md`):
- Do group messages and DMs arrive through the same handler, and how is group vs DM distinguished?
- Can the bot **initiate** a DM to a handle that has only spoken in a group? If not, onboarding requires users to DM first (already the design: "DM me 'start'").
- Do native polls work in group chats, and do votes arrive as events?
- What does a shared location look like in the inbound payload, if anything?
- Are there rate or allowlist limits on the hackathon tier?

### 9.2 xAI Grok (`providers/real/grok.py`)

- Use xAI's OpenAI-compatible API (base URL `https://api.x.ai/v1`) with the `openai` Python SDK, or the official `xai-sdk`. Check current docs for structured-output support and model names.
- **Extraction:** system prompt + pseudonymous transcript; require JSON matching the `GroupPreferences` schema (pass the Pydantic JSON schema as the structured-output schema). Temperature 0. Then run the validation rules in §6.3.
- **Extraction prompt must include:**
  - Current local time and timezone, so "back by 9" → `21:00`.
  - Definitions of HARD / SOFT / VETO / INFERRED with one example each.
  - The allowed `ConstraintField` and `CATEGORY` values.
  - "Only attribute a constraint to the person who said it. Cite message ids. Do not invent constraints. Do not output any prices, distances, or times other than those stated by a person."
- **Explanation:** input is a list of group-safe fact dicts (§13.8). Output: one sentence (≤ 25 words) per plan. Then the number check (§13.8).
- **Stage 7 only:** `find_events` uses the Responses API with the `web_search` tool, 20 s hard timeout, run in parallel with Places. Results must be resolved to coordinates via `PlacesProvider.text_search`; unresolved events are dropped.

### 9.3 Google Places API (New) (`providers/real/places.py`)

- `POST https://places.googleapis.com/v1/places:searchNearby` and `.../places:searchText`.
- Headers: `X-Goog-Api-Key`, `X-Goog-FieldMask` (request only needed fields: `places.id, places.displayName, places.location, places.formattedAddress, places.types, places.priceLevel, places.priceRange, places.currentOpeningHours, places.rating`).
- Map Google `types` → our `category`. Map cost:
  - `priceRange` present → `low/high` from it, `value` = midpoint, `status="known"`.
  - Else `priceLevel`: FREE → 0; INEXPENSIVE → $12 (8–15); MODERATE → $25 (15–35); EXPENSIVE → $45 (35–60); VERY_EXPENSIVE → $75 (60–100); `status="estimated"`.
  - Else category default with `status="unknown"`.
- `geocode()` = `searchText` with location bias around the demo center; take the top result.
- **For the demo, prefer the fixture:** `scripts/fetch_venues.py` pulls ~25 venues once into `fixtures/venues.json`, which is then hand-checked (fix obvious cost errors, remove closed places).

### 9.4 Google Routes API (`providers/real/routes.py`)

- **Screening:** `POST https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix`, field mask `originIndex,destinationIndex,duration,distanceMeters,status,condition,travelAdvisory`.
  - One call per mode. Only include origins whose `TravelModes` allow that mode.
  - TRANSIT matrices are limited in size (≈100 elements; verify). Batch destinations so `origins × destinations` stays under the limit.
  - Walk portion for TRANSIT is not in the matrix: estimate `walk_min = min(duration, 8)` for screening; the detailed route gives the real value.
- **Detail:** `POST https://routes.googleapis.com/directions/v2:computeRoutes` only for the winning plan's legs. For TRANSIT, set `arrivalTime = T_target`. For WALK/DRIVE, set `departureTime = leave_by`. Parse steps into `RouteStep`s (transit line names, stops, departure times).
- Use transit fare from the response if present; otherwise `TRANSIT_FARE_USD`.
- Driving cost = `distance_mi × DRIVE_COST_PER_MILE_USD + DRIVE_PARKING_USD`.
- If Routes returns no transit route for a pair, that mode is unavailable for that pair. Do not fake it.

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
   - members = READY users in the group
   - pid_map = {"p1": user_id, ...}  (random order per session; saved in session)
   - constraints = vault.constraints_for(members, pid_map) -> dict[pid, PrivateConstraints]
   - transcript = session_messages → PseudonymousMessage list

2. Extract   (LLMProvider.extract_preferences + validation)
   - On failure or timeout: retry once; then continue with empty preferences and intent "either".

3. Discover  (PlacesProvider)
   - center = centroid of origins (computed inside the pipeline from PrivateConstraints; never sent to the LLM)
   - categories from group_intent and CATEGORY constraints
   - search_nearby(center, radius 2500 m, categories, open_at = now + 30 min)
   - Pre-filter: vetoed cuisines/categories, known-closed; keep top ~20 by rating with category variety
   - [Stage 7] ContextProvider.find_events in parallel; resolved events added as candidates

4. Route     (RoutingProvider.matrix)
   - origins = {pid: origin}, destinations = {candidate_id: location}
   - modes = {pid: allowed modes}
   - depart_at = now + 5 min

5. Optimize  (optimizer, pure)
   - plans = optimizer.rank(candidates, estimates, constraints, preferences, now, settings)
   - top3 = optimizer.select(plans, k=3)
   - If 0 feasible: tell the group "Nothing fits everyone right now" + the most binding SOFT constraint suggestion; return to COLLECTING.

6. Explain   (facts → LLMProvider.phrase_explanations → number check → template fallback)

7. Guard + send
   - Build GroupSafeMessage with GroupPlanOption list
   - PrivacyGuard.check(message, members' private values) → send or substitute safe template
   - MessagingProvider.send_group(...) → poll_id
   - session.state = POLLING; schedule timeout task
```

After the winner is chosen (`delivery/itinerary.py`):
```
for each person in winner.assignments:
    detail = routing.route(origin, venue, mode, arrive_by=T_target if transit else None,
                           depart_at=leave_by if not transit else None)
    leave_by = detail.depart_at (transit) or recomputed
    send_private(handle, PrivateMessage(text=itinerary text))
group: confirmation message with the shared arrival time (rounded to the minute)
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
| **Aggregation** | Group output never shows per-person cost, per-person travel time, limits, or counts of who is constrained. Infeasible plans are hidden. Prices appear as tiers. Travel appears as "≤N min for everyone." |
| **Output guard** | `messaging/guard.py` scans every outbound group message for: any member's limit (±$1, formats `$25`, `25 dollars`, `25.00`), origin labels, street addresses from profiles, Nessie ids, phone numbers. On match: block, log `privacy_block` (without the value), send a safe template. |
| **Group input** | If someone types money info in the group, the bot never repeats it. |
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
For each candidate `v` (≤20) and each person `i` (N ≤ 6), the feasible modes are `M_i(v) ⊆ {walk, transit, drive}` (allowed by `TravelModes`, present in estimates, not refused by a HARD mode preference). Enumerate the Cartesian product: at most 20 × 3⁶ = 14,580 combinations. Brute force is fine.

### 13.2 Synchronized arrival (`arrival.py`)
```
ready_i            = AVAILABLE_FROM_i if stated else now + 5 min
earliest_arrival_i = ready_i + duration_i(v, m_i)
T_target           = max_i earliest_arrival_i, rounded up to the next minute
leave_by_i         = T_target − duration_i
arrival_spread     = 0 at screening (everyone scheduled to T_target);
                     after detailed transit routing: T_target − min_i actual_arrival_i
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
  + μ · arrival_spread_min / 10                        μ = 0.15  (phase 2)
  + ρ · uncertainty_fraction                           ρ = 0.10  (phase 2)
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
- `mock/sim_messaging.py`: appends to an in-memory outbox per `chat_id`/handle; used by the simulator and tests.
- `mock/routing.py`: haversine × 1.3 detour. Walk 3 mph. Drive 18 mph + 5 min parking. Transit 12 mph + 7 min wait + 6 min total walking; fare from config. Used in tests and as fallback.
- `mock/places.py`: returns `fixtures/venues.json`, filtered by category and distance.

There is no mock LLM or mock finance. Tests that need them use recorded responses in replay mode, or stub the provider directly in the test.

### 14.3 Simulator (`api/sim.py`, enabled only when `PROVIDER_MESSAGING=sim`)
- `POST /sim/message` `{chat_id, is_group, sender_handle, sender_name, text}` → runs the same router as the real webhook (generates `message_id` and `ts`).
- `POST /sim/vote` `{chat_id, voter_handle, option_label}`
- `GET /sim/outbox/{chat_id_or_handle}` → list of sent messages.
- `POST /sim/reset` → clears DB and outbox.

`scripts/run_demo_scenario.py` drives the full §18 script through these endpoints and prints each chat's outbox. It doubles as the backup demo if iMessage fails.

---

## 15. Staged build plan

Work assignment for 4 people (all code against the Stage 0 models and protocols, so work runs in parallel):
- **A — Core:** Stages 0, 1, 2 (router, FSMs, optimizer).
- **B — Photon:** Stage 0b, then 6.
- **C — LLM:** Stage 3, then 7 (stretch).
- **D — Data:** Stages 4 and 5 (Google, Nessie, fixtures, record/replay).

Suggested clock targets assume Saturday daytime start; adjust to actual time but **keep the 3 AM freeze**.

| Stage | Deliverable | Depends on | Exit criterion | Target |
|---|---|---|---|---|
| **0. Skeleton** | `pyproject.toml`, settings, logging, all models (§6), protocols (§8), DB tables, `GET /health`, ruff + pytest set up | — | App boots; models import; empty test suite passes | +1.5 h |
| **0b. Photon spike** (parallel) | Bridge receives a group message and a DM, sends text to both, tries a poll. Spike answers written in README | — | All §9.1 spike questions answered | +3 h |
| **1. Mock vertical slice** | Simulator, sim messaging, mock routing + places, router, onboarding FSM (bank code can be stubbed to a fixed limit), session FSM, phase-1 optimizer, text poll, personal DMs | 0 | `test_end_to_end.py` runs §18 through the simulator with mocks and a stubbed LLM | +6 h |
| **2. Optimizer + privacy** | Full feasibility filters, arrival logic, diversity selection, facts, PrivacyGuard, all §13.7 and privacy tests | 1 | All optimizer and guard tests pass | +8 h |
| **3. Grok extraction** | Real extraction + validation; explanation + number check; 3–5 golden transcripts | 1 | ≥ 90% of expected constraints extracted on golden transcripts; template fallback on failure | +8 h |
| **4. Google Places + Routes** | Real providers, field masks, batching, detailed routes, record/replay cache, `fetch_venues.py` | 1 | Real 3-person scenario returns real durations and transit steps | +9 h |
| **5. Nessie** | `seed_nessie.py`, real FinanceProvider, budget estimator + tests, link-by-bank-code onboarding | 1 | Three personas produce expected limits; override works | +9 h |
| **6. Photon integration** | Production bridge, real messaging provider, text poll (native poll if spike says it works), DM delivery | 0b, 1 | Full §18 demo on real phones with mock data providers | +12 h |
| **— Full-loop checkpoint** | Everything real, end to end on phones | 2–6 | One complete live run | +13 h |
| **Phase 2 scoring** | walk/sched burden terms, arrival spread, uncertainty penalty | checkpoint | Tests still pass; demo outcome still sensible | +15 h |
| **7. Grok events (stretch)** | `find_events` with timeout + Places resolution | 3, 4 | At least one live event appears as a candidate with a source URL | +16 h |
| **8. Demo hardening** | Record the rehearsal, replay mode, progress messages, error copy, README, Devpost text, backup video | all | Two clean full rehearsals in a row | **freeze by 3 AM** |

**Feature freeze at 3 AM Sunday.** After that: bug fixes, rehearsal, Devpost (draft by 6 AM, submit by 8:00 AM).

---

## 16. Out of scope (do not build)

- Reading chat history from before `@plan`.
- Supabase/Postgres, deployment, Docker, CI pipelines.
- HMAC/auth between bridge and backend.
- Live re-planning when someone is late; live location tracking.
- Multi-stop plans.
- Learned weights / any ML model.
- Ride-share as a travel mode.
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
4. `test_end_to_end.py` — simulator + mocks + stubbed LLM; asserts: poll sent with ≤3 options, no private values in any group message, each member gets a distinct DM, all `leave_by` times produce the same `T_target`.
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

**Cast:** Maya, Sam, Jordan on three real phones. A fourth teammate presents with the laptop (running backend + bridge, `CACHE_MODE=replay`) and the simulator ready as backup.

**Before judging:** all three already onboarded (shows faster); onboarding is shown live only if judges ask, or via a 20-second pre-recorded clip.

1. **Setup (10 s):** "This group chat can never decide on dinner. Our bot is in it."
2. **Group chat:**
   - Maya: `@plan`
   - Bot: "Listening — tell me what you're feeling. Say @go when ready."
   - Maya: "I'm starving"
   - Sam: "no sushi pls"
   - Jordan: "I have to be back by 9, and nothing too far"
   - Sam: "something we haven't tried?"
   - Maya: `@go`
3. **Bot:** "🔎 Looking at options…" then the 3-option poll with one-line explanations.
4. **Vote:** two people reply/vote A → bot confirms: "🎉 Plan A: Koko. Everyone arrives around 6:42. Check your DMs."
5. **Reveal:** hold up the three phones. Maya walks 12 min, Sam takes the bus, Jordan drives — different leave-by times, same arrival. Jordan's plan respects the lowest budget, but nobody in the group chat ever saw it.
6. **Explain the tech (60 s):** fairness objective (show the λ trade-off), synchronized arrival, privacy guard (show that the LLM prompt contains no money), Nessie-derived limits.

**Fallbacks:** if iMessage fails, run `scripts/run_demo_scenario.py` and show outboxes; if that fails, play the backup video.

---

## 19. Risks and verification checklist

| Risk | Mitigation | Verify by |
|---|---|---|
| Photon behavior differs from assumptions (DMs, polls, payloads) | Stage 0b spike; text poll fallback; "DM me start" onboarding | End of 0b |
| Bot cannot see messages before `@plan` | Designed in: collection starts at `@plan` | — |
| Sparse evening transit in the demo area | Check Routes returns transit for the demo origin/venue pairs at the planned demo time; if not, demo with walk + drive and note transit support | Stage 4 |
| LLM latency or bad JSON | Temperature 0, schema, validation, one retry, empty-preferences fallback | Stage 3 |
| Nessie slow/flaky | Record/replay; snapshot at onboarding | Stage 5 |
| Google billing not enabled | Set up billing first thing in Stage 0 | Stage 0 |
| Venue wifi drops during judging | Replay mode; phone hotspot; simulator backup; video | Stage 8 |
| Privacy leak through explanation text | Number check + PrivacyGuard + tests | Stage 2 |

**Open decisions (team to confirm, defaults in bold):**
1. Demo area: **Ithaca (Collegetown/campus)**, switching only if transit coverage fails the Stage 4 check.
2. Group size: **3 in the demo**, cap at 6.
3. Driving cost: **miles × $0.20 + $3 parking**.
4. Product name: replace `[NAME]` everywhere (`copy.py`, README, Devpost).

---

## 20. Coding conventions

- **Python deps (pyproject):** `fastapi`, `uvicorn[standard]`, `pydantic>=2`, `pydantic-settings`, `sqlalchemy>=2`, `aiosqlite`, `httpx`, `tenacity`, `openai` (or `xai-sdk`), `python-dateutil`; dev: `pytest`, `pytest-asyncio`, `ruff`. Nothing else without asking.
- **Bridge deps:** `spectrum-ts` (or the scoped `@spectrum-ts/core` + `@spectrum-ts/imessage` packages, per its README). Use Bun's built-in HTTP server and `fetch`.
- Type hints everywhere; Pydantic models at every module boundary.
- `async` for all I/O. No blocking calls in request paths.
- All user-facing strings in `conversation/copy.py`.
- All tunable numbers (weights, defaults, timeouts) in `settings.py` or `OptimizerParams`, never inline.
- Times: store UTC, convert to `DEMO_TIMEZONE` only for display and LLM prompts.
- Money: `Decimal`, rounded to cents for display; `$` tiers for the group.
- Errors: catch at the pipeline/router boundary, log, and send a friendly message. Never let an exception kill the background task silently.
- Commit after each stage with a message naming the stage.
