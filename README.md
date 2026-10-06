# Disruption Management & Recovery Orchestrator

Multi-agent airline disruption recovery: detect a disruption → assess who's
affected → propose recovery options → **a human approves** → execute → notify
passengers.

**Status: Phase 0 and Phase 1 complete.** One scenario (Fog at DEL), five agent
functions called in sequence, one approval gate, a Streamlit control room with
manual refresh. See `Capstone_1/Disruption Orchestrator - 4-Phase Build
Roadmap.md` for what each later phase adds.

---

## Quick start

Three commands. Nothing here needs an API key.

```bash
# 1. Install (Python 3.11+)
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt

# 2. Seed the database (~20 flights, 6 tails, 15 crew, 90 PNRs)
python -m db.seed

# 3. Run the two processes, in two terminals
uvicorn services.api.main:app --reload
streamlit run apps/dashboard/app.py
```

Then open **http://localhost:8501** and press **🌫️ Inject Fog at DEL**.

With no `.env` at all this runs on a local SQLite file with rule-based agent
stubs. Copy `.env.example` to `.env` to point at Neon Postgres and/or turn on
real Claude calls — see [Configuration](#configuration).

### The demo, start to finish

1. Press **🌫️ Inject Fog at DEL**. A disruption appears in `DETECTED`.
2. Press **🔄 Refresh** a couple of times. It moves
   `DETECTED → ASSESSED → PLANNED → PENDING_APPROVAL`, filling in impact
   numbers and 2-3 recovery options as it goes.
3. The **⏸️ Awaiting your approval** panel appears. Pick an option and press
   **✅ Approve** (or **⛔ Reject** to send it back for a new plan).
4. Press **🔄 Refresh**. It moves `APPROVED → EXECUTING → COMPLETED`.
5. Open the **✉️ Notifications** tab and read a drafted message.

**Refresh is manual in Phase 1, by design.** Streamlit reruns the script on
every interaction, so the button *is* the poll. Phase 2 swaps it for
`streamlit-autorefresh`; nothing else changes, because both poll the same REST
endpoints.

To run the demo again, use **Reset demo** in the sidebar — it clears the
disruptions and puts the schedule, fleet, roster and bookings back.

---

## What's real and what isn't

Being precise about this matters more than it sounds: the point of the project
is the orchestration, and overstating the data would undercut it.

| Data | Phase 1 | Why |
| --- | --- | --- |
| **Weather** (METAR/TAF, forecast) | 🟢 **Real** — aviationweather.gov + Open-Meteo | Genuinely free, no key. The one live external call. |
| **Airport reference data** | 🟢 **Real** — OurAirports CSV, downloaded once | Free, no key. Cached in `db/cache/`. |
| Flights, tails, crew, PNRs, passengers, gates | 🔶 Synthetic (Faker) | AviationStack needs a key; PSS/crew/AODB have no free API at all. |
| MEL / maintenance status | 🔶 Synthetic, seeded column | No free API, in any phase. |
| Recovery options & passenger messages | 🟢 Claude *if* `ANTHROPIC_API_KEY` is set, else rule-based stubs | Same function signatures either way. |
| Email / Telegram delivery | 🔴 **Logged, not sent** | No key wired. Rows land in `notifications` with `status='logged'`. |

The sidebar tells you which mode you're in at runtime, and the **🔍 Evidence**
tab shows exactly what the Detection agent read, including whether the fog was
live or injected.

### About the injected fog

Delhi usually isn't fogged in when you run the demo, so `POST /demo/inject`
installs a CAT III fog observation (150 m visibility) for `VIDP`. This is
**labelled** — that evidence row reads `source: demo_override`. The agent still
makes the real METAR, TAF and Open-Meteo calls alongside it, and you can see
the genuine readings in the same evidence table. Nothing pretends injected
weather is real.

---

## Architecture (as built)

```
Streamlit (apps/dashboard)          one page, manual refresh, httpx → REST
        │
        ▼
FastAPI (services/api)              /api/v1, no auth in Phase 1
        │
        ├── orchestrator.py         the state machine, as plain `if` guards
        │
        ├── services/agents/        5 agent functions
        │     detection impact recovery coordination comms
        │        │
        │        ▼
        └── services/mcp/           6 logical domains as plain Python modules
              weather flightops aodb pss crew notify
                 │
                 ▼
            Postgres (Neon) or local SQLite
```

Two Phase 0 decisions do the heavy lifting for later phases:

- **`services/mcp/` modules already have the function names, arguments and
  return shapes the MCP tools will have.** Promoting one to a real FastMCP
  server in Phase 3 is a move, not a redesign.
- **`services/mcp/registry.py` holds the tool table and the agent-to-tool
  permission matrix** from architecture doc section 10, and every agent call
  goes through `check_permission`. In Phase 3 that same table becomes the
  gateway's allowlist — so an agent reaching for a tool it shouldn't have
  fails *now*, rather than working now and breaking later.

### State machine

```
DETECTED → ASSESSED → PLANNED → PENDING_APPROVAL → APPROVED → EXECUTING → COMPLETED
                         ↑              │
                         └──────────────┘  reject + replan
                   any step ──→ NEEDS_HUMAN        any step ──→ CLOSED
```

Enforced in one place (`services/core/state.py`); an illegal transition raises
rather than silently corrupting the row. A failed run lands in `NEEDS_HUMAN`
with the reason attached instead of getting stuck mid-state.

### Where the LLM is, and isn't

Deliberately narrow. Claude writes **recovery options** (Sonnet) and
**passenger messages** (Haiku). Everything else is Python:

- **Detection** is rule-based — a hallucinated disruption is the one failure
  mode nothing downstream can recover from.
- **Impact** counts from the database, because section 8 requires the numbers
  to reconcile with it.
- **Coordination** dispatches an already-approved plan. An `H`-tagged write is
  the last place you want improvisation.
- **Hard constraints** — crew FTL, curfew, seat counts, tail availability,
  voucher entitlements — are checked in Python *after* Claude answers. An
  option that fails its regulatory check is discarded, never ranked.
- **Both score formulas** (priority, option score) are the architecture doc's
  formulas, computed in code.

Claude output is parsed straight into a Pydantic schema, retried once with the
validation error on failure, then `NEEDS_HUMAN`.

---

## Configuration

Everything is optional. Copy `.env.example` → `.env`.

| Variable | Default | Effect |
| --- | --- | --- |
| `DATABASE_URL` | local SQLite | Set to a Neon `postgresql+psycopg://...` URL to use Postgres. |
| `ANTHROPIC_API_KEY` | unset | Unset → rule-based stubs. Set → real Claude calls. |
| `AGENT_MODEL` | `claude-sonnet-5-5` | Recovery reasoning. |
| `COMMS_MODEL` | `claude-haiku-4-5` | Passenger message drafts. |
| `API_BASE_URL` | `http://127.0.0.1:8000/api/v1` | Where Streamlit looks for the API (also editable in the sidebar). |
| `DEMO_STEP_DELAY_SECONDS` | `1.2` | Pause between orchestrator steps, so a Refresh mid-run lands on an intermediate state. Set `0` for speed. |
| `HUB_ICAO` | `VIDP` | The hub the fog scenario hits. |

### Using Neon Postgres

```bash
# .env
DATABASE_URL=postgresql+psycopg://user:pass@ep-xxx.aws.neon.tech/neondb?sslmode=require
```

Then `python -m db.seed --reset`. The driver (`psycopg[binary]`) is already in
`requirements.txt`. Timestamps are normalised to tz-aware UTC by a SQLAlchemy
type decorator, so SQLite and Postgres behave identically.

---

## API

Base path `/api/v1`. Interactive docs at http://localhost:8000/docs.

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness, which DB, LLM mode, weather reachability, seed counts |
| `GET` | `/disruptions` | List (filters: `status`, `severity`, `airport`) |
| `GET` | `/disruptions/{id}` | Full record: impact, options, notifications, evidence, timeline |
| `POST` | `/disruptions/{id}/approve` | `{option_id?, comment?, approved_by}` — the HITL gate |
| `POST` | `/disruptions/{id}/reject` | `{reason?, request_replan, rejected_by}` |
| `POST` | `/demo/inject` | `{scenario: "fog_delhi"}` — run the scenario |
| `POST` | `/demo/reset` | Clear disruptions, restore schedule/fleet/roster/bookings |

Routes are mounted under `/api/v1` to match architecture doc section 9 even
though Phase 1 implements a slice of it — the prefix is free now and saves a
client rewrite in Phase 2.

---

## Repo layout

```
apps/dashboard/        Streamlit app (app.py + api_client.py)
services/
  api/                 FastAPI app, routes, orchestrator
  core/                config, db, models, schemas, state machine
  agents/              the 5 agent functions + the Claude client
  mcp/                 6 logical tool domains + the permission registry
db/
  seed.py              Faker seeder (run with `python -m db.seed`)
  cache/               downloaded OurAirports CSV
requirements.txt       one file — the whole project is Python
```

Two deviations from the architecture doc's layout, both deliberate:

- **`services/core/`** holds what `api`, `agents`, `mcp` and `db/seed.py` all
  need (config, models, state machine). The doc's layout has no shared
  package, which would have meant `db/seed.py` importing from
  `services/api/`.
- **`apps/dashboard/`** instead of `apps/web/`, since the frontend is
  Streamlit. Run it as `streamlit run apps/dashboard/app.py`; Phase 2's
  multipage views go in `apps/dashboard/pages/`.

---

## Seeded dataset

`python -m db.seed` — add `--reset` to drop and rebuild, or
`--flights/--pnrs/--crew/--tails` to change the counts.

20 flights (10 hub departures in the next ~5 hours, 10 return legs), 6 tails,
15 crew, 90 PNRs / 112 passengers, 9 airports.

Four things are seeded *specifically* so the scenario has something real to
reason about:

- **one tail flagged AOG** (`VT-FJP`) — feeds the Phase 2 AOG scenario;
- **one spare tail parked at the hub** — gives Recovery a genuine swap option;
- **one crew member near their duty limit** — so the fog delay produces a real
  FTL breach, which forces a standby call-out and makes an option without one
  *illegal*;
- **~8 passengers without email consent** — the Comms agent must skip them,
  and the run shows that happening (13 notifications for 14 booked passengers).

The schedule is anchored on "now" rather than a fixed date, so the demo works
whenever you run it.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Sidebar: "Backend unreachable" | Start the API: `uvicorn services.api.main:app --reload` |
| Sidebar: "Database is not seeded" | `python -m db.seed` |
| `409 database is not seeded` on inject | Same |
| `409 detection found nothing` | The seeded departures have aged past the 6-hour window. Reseed: `python -m db.seed --reset` |
| Stuck in `DETECTED`/`PLANNED` | Press **🔄 Refresh**. If it stays, check the API console for the traceback. |
| `NEEDS_HUMAN` | The run failed; the reason is on the disruption and in the API log. |
| Weather feed "unreachable" | Fine — detection falls back to the injected override and says so in the evidence. |

---

## Phase 1 definition of done

> Click "Inject Fog at DEL" → a disruption appears → impact numbers show up →
> 2-3 recovery options show up → click Approve → status moves to EXECUTING then
> COMPLETED → a notification row with drafted text exists. All in the browser,
> no manual DB poking.

Met. All three recovery kinds (retime, swap aircraft, cancel) execute real DB
writes, the reject-and-replan path works, and the run is repeatable via
**Reset demo**.
