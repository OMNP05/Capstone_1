# Disruption Orchestrator — 4-Phase Build Roadmap

Companion to `Disruption Orchestrator - Architecture & Build Plan.md`. That doc is the **full target architecture** (5 agents, MCP gateway, 6 MCP servers, Redis, SSE, HITL tokens, etc). This doc exists because building all of that on day one is how projects stall.

**Rule for every phase: it must run, end-to-end, on a real browser, before you add the next layer.** Each phase is a strict superset of the previous one — nothing built in Phase 1 gets thrown away, it just gets a thin layer wrapped around it.

---

## 0. Guiding simplifications (apply to the whole project, not just Phase 1)

| Full-architecture idea | What we actually do until it's needed |
| --- | --- |
| MCP Gateway + 6 standalone MCP servers (network boundary, FastMCP) | Plain Python modules/functions inside the one FastAPI app, with the **same function names and input/output shapes** the MCP tools will have later. Promoting a module to a real MCP server later is a copy-paste, not a redesign. |
| LangGraph state machine | A single Python function per disruption that calls Detection → Impact → Recovery → (approve) → Coordination → Comms in order, writing `disruptions.state` after each step. It's a state machine, just not a graph library yet. |
| Redis (cache + event stream) | Nothing. Postgres is small enough; the frontend polls. |
| SSE / WebSocket live push | Frontend polls `GET /disruptions` every few seconds. |
| JWT + RBAC (`controller`/`approver`/`admin`) | No login wall. One implicit "ops" role for everyone. |
| HMAC approval tokens, 15-min TTL | A `pending` boolean + an `approved_by` text field on the disruption row. |
| 6 MCP servers across weather / flightops / aodb / pss / crew / notify | Same 6 **logical** domains, just as service modules, not servers. |
| Multiple frontend stacks / separate dashboards for controller vs approver vs passenger | One Next.js app, one page style, everything on it. No separate passenger UI — passenger comms are just rows in a table (plus optional real email/Telegram once keys exist). |

Tech stack for the **entire project**, all 4 phases — this does not change phase to phase, only what's wired up changes:

- **Frontend:** Next.js + Tailwind (plain fetch/polling in Phase 1-2, nothing fancier)
- **Backend:** Python FastAPI (monolith through Phase 1-2; splits into gateway + servers in Phase 3)
- **DB:** Postgres on **Neon** (free tier) — the only datastore until Phase 3
- **LLM:** Claude API (Sonnet for agent reasoning, Haiku for comms drafts once that stage is wired)
- **Deploy:** local only (`uvicorn` + `next dev`) until Phase 4

---

## Phase 1 — Make It Work (one disruption, start to finish)

**Goal:** prove the core loop — detect something wrong → assess who's affected → propose a fix → a human clicks approve → it "executes" → someone gets "notified". One scenario, one straight line, no polish.

### What we build
- FastAPI backend, single service, single repo folder (`/services/api`), talking to one Neon Postgres database.
- Minimal schema: `flights, pnrs, passengers, crew, disruptions, impacts, recovery_options, notifications`. (`audit_log`, `gates`, `crew_assignments` deferred to Phase 2.)
- 5 agent **functions** (not agents-as-a-framework), called in sequence by one orchestrator function:
  1. **Detection** — reads weather + the seeded flight list, decides if a flight is disrupted.
  2. **Impact** — counts affected passengers/connections from seeded PNR data.
  3. **Recovery** — asks Claude for 2-3 recovery options (retime / swap aircraft / cancel), each with a cost/delay/pax-impact guess.
  4. **Coordination** — "executes" the approved option by updating rows (flight retimed, pax marked rebooked). No real external system to call yet, so this is just DB writes.
  5. **Passenger Comms** — drafts a notification message with Claude and writes it to the `notifications` table (not actually sent — see dummy data below).
  - Each agent call: Claude returns JSON → validate against a Pydantic schema → **one retry on failure** → else mark the disruption `NEEDS_HUMAN` and stop.
- State machine (manual, in code): `DETECTED → ASSESSED → PLANNED → PENDING_APPROVAL → APPROVED → EXECUTING → COMPLETED`. Enforced with simple `if` guards, not a library.
- HITL: dead simple. One button in the UI: **Approve** / **Reject**. No token, no HMAC, no TTL — just sets `disruptions.pending = false, approved_by = 'demo-user'` and lets the orchestrator continue.
- One API route group: `GET /disruptions`, `GET /disruptions/{id}`, `POST /disruptions/{id}/approve`, `POST /disruptions/{id}/reject`, `POST /demo/inject` (triggers the one seeded scenario), `GET /health`.
- One Next.js page: a table of disruptions + a detail view with impact numbers, recovery options, an approve/reject button, and the notification text that got drafted. **Polling only** (refetch every 5s), no SSE.
- **One demo scenario only:** Fog at DEL (a weather-driven delay). Hard-code it well, make it reliable, don't try to support all four scenarios yet.
- No auth, no RBAC, no roles.

### Dummy data in Phase 1 (be explicit about this in the demo/README)
No free API gives you PSS, crew rosters, AODB gate data, or MEL/maintenance status, key or no key — these are **always synthetic**, in every phase:
- **Flights, tails, gates, crew roster, PNRs/passengers** — seeded with Faker directly into Neon. Phase 1 target: ~20 flights, ~5 tails, ~15 crew, ~80-100 PNRs across those flights. A `db/seed.py` script, run once.
- **Maintenance/MEL status** — a static seeded column on `flights`/`aircraft` (e.g. one tail flagged AOG for the demo), not computed.

Things that *could* be real but are mocked in Phase 1 **only because we're not wiring the key/integration yet** (promoted to real in later phases, see Phase 4):
- **Flight schedule/status (AviationStack)** — needs a key → use the seeded Faker schedule instead of calling AviationStack at all.
- **Email (Resend) / Telegram send** — need keys → `send_email`/`send_telegram` just `INSERT INTO notifications (... , status='logged')` and print to console. No message actually leaves the app.
- **Live aircraft positions (OpenSky)** — optional even with a key → skip entirely, not used by any Phase 1 scenario.

Things that are real in Phase 1 because they're genuinely free and keyless:
- **Weather** — Open-Meteo (forecast) and aviationweather.gov (METAR/TAF) are called for real, no key required. This is the one live external call in Phase 1, and it's what drives the Fog-at-DEL scenario.
- **Airport reference data** — OurAirports CSV, downloaded once and loaded into a lookup table, no key.

If `ANTHROPIC_API_KEY` itself isn't available yet: fall back to a deterministic rule-based stub for whichever agent function needs it (e.g. Recovery just returns 2 hard-coded option templates with the real numbers plugged in) so the loop still runs end-to-end. Swap in the real Claude call as soon as the key exists — same function signature.

### Definition of done
Click "Inject Fog at DEL" → a disruption appears → impact numbers show up → 2-3 recovery options show up → click Approve → status moves to EXECUTING then COMPLETED → a notification row with drafted text exists. All in the browser, no manual DB poking.

---

## Phase 2 — Fill Out the Feature Set

**Goal:** same architecture, but it's a real tool instead of one fragile path. All 4 demo scenarios work, more than one disruption can be open at a time, there's a basic audit trail, and the UI updates itself.

### What we add
- **Remaining 3 demo scenarios**, generalizing Detection/Impact/Recovery so they're not hard-coded to fog: AOG on a tail, crew timeout (FTL breach), hub congestion. `/demo/inject` takes a `scenario` param for all four.
- **Audit log** — add the `audit_log` table; every agent call and state transition writes a row (what tool/function, inputs, outputs, timestamp). Still just a Postgres table, nothing fancier.
- **Full data model** from the architecture doc: add `gates`, `crew_assignments`, `segments`, `approvals`, `actions` so the schema matches section 11 of the architecture doc properly instead of the trimmed Phase 1 version.
- **SSE for live updates** (`GET /stream`) replacing polling — this is the first "real-time" piece, and it's cheap to add once the REST shape is stable.
- **Basic auth + RBAC** — simple JWT login (`controller`, `approver`, `admin`), gating the approve/reject and audit routes. Still no Supabase Auth, just FastAPI + `python-jose`.
- **Proper HITL rule** from the architecture doc (section 8): auto-approve low-risk options, require explicit approval when a flight is cancelled, pax_impacted > 100, cost > threshold, or a crew legality override — instead of Phase 1's "always ask a human."
- **Priority score and option score formulas** (architecture doc section 8) computed for real, instead of Phase 1's rough guesses.
- Dashboard KPIs (`/dashboard/summary`): active disruptions, pax affected, cost exposure, rough OTP — simple aggregate queries, no new infra.

### Still not doing yet
Redis, MCP protocol separation, HMAC approval tokens, What-if simulator, real email/Telegram sends, deployment — all Phase 3/4.

### Definition of done
All 4 scenarios run reliably, two can be open simultaneously without interfering, there's a login, the dashboard updates without a manual refresh, and `/audit` shows a believable trail for a run.

---

## Phase 3 — Make the Architecture Honest

**Goal:** this is where the project catches up to the architecture doc structurally. Someone reading the doc and someone reading the code should recognize the same system.

### What we add
- **MCP Gateway + 6 real MCP servers** (`weather-mcp`, `flightops-mcp`, `aodb-mcp`, `pss-mcp`, `crew-mcp`, `notify-mcp`), using `FastMCP`, one process each (or at least one importable package each) — this is the promotion of the Phase 1 "plain Python modules" into the real network-boundary tools the architecture doc describes, with the agent-to-tool permission matrix (doc section 10) actually enforced at the gateway instead of trusted by convention.
- **Redis (Upstash free tier)** for: caching external API responses (60s TTL, so a quota outage doesn't break the demo) and as the event stream backing `/stream`, replacing the Phase 2 in-process SSE.
- **Real HITL approval tokens** — HMAC-signed, 15-minute TTL, bound to `option_id`, required by any `H`-tagged tool call at the gateway. Replaces Phase 1/2's boolean flag.
- **LangGraph orchestrator** replacing the Phase 1 hand-written sequential function — same state machine, now an explicit graph with proper interrupt-on-HITL semantics, idempotency keys per event/action, and optimistic locking on the disruption row.
- **What-if Simulator** (`POST /disruptions/{id}/simulate`) — dry-run mode that runs the Recovery agent with all `A`/`W` tools blocked, so a controller can preview an option without side effects.
- **Circuit breaker + timeouts** at the gateway (10s timeout per tool call, breaker trips after repeated failures) — this is also where the "free API quota runs out" risk from the architecture doc gets a real mitigation instead of just a note.
- **Trace IDs** threaded through API → agent → gateway → MCP server, surfaced as an "agent activity" panel in the UI.

### Definition of done
Pulling the plug on one MCP server doesn't crash the app (breaker trips, that domain shows "degraded"); the audit log and trace panel show a tool-call-by-tool-call story of a run; a controller can run a what-if without it touching any data.

---

## Phase 4 — Go Live

**Goal:** swap in whatever real integrations you actually have keys for, deploy it somewhere reachable, and polish it for a demo audience.

### What we add
- **Real external integrations**, each promoted independently as keys become available (none of these are required to finish the project — promote only what you actually have a key for):
  - AviationStack key → real flight schedule/status instead of the seeded Faker schedule (seeded data stays as the fallback if the quota runs out, per the Phase 3 cache/breaker work).
  - Resend key → real emails for passenger comms, replacing the "logged, not sent" stub.
  - Telegram bot token → real chat delivery for the demo "passenger" channel.
  - OpenSky credentials (optional) → live aircraft positions layered onto the map, if you want it.
  - PSS, crew rosters, AODB gate data, and MEL/maintenance status **stay synthetic permanently** — there is no free real-world API for these, in any phase. The seeded dataset is the product's permanent "data layer," not a placeholder.
- **Deployment**: Docker Compose for local parity, then Render/Fly.io for the API + MCP servers and Vercel for the Next.js frontend (all free tiers, per the architecture doc).
- **Observability polish**: the Phase 3 trace panel becomes a proper dashboard view; add the layer-by-layer checks from architecture doc section 10 that weren't load-bearing earlier (PII masking in logs, rate limiting at the edge, CORS allowlist for the real deployed domain).
- **Permission matrix tests** — automated checks that each agent can only reach the tools section 10's table says it can.
- **Demo rehearsal pass**: pre-recorded fallback for `demo/inject` scenarios in case live weather/flight data is uncooperative during the actual demo, per the architecture doc's risk table.

### Definition of done
The thing is reachable at a public URL, at least one external integration is live (not mocked), and you could run the demo cold in front of someone without touching a terminal.

---

## One-page summary

| | Phase 1 | Phase 2 | Phase 3 | Phase 4 |
| --- | --- | --- | --- | --- |
| Scenarios | 1 (fog) | all 4 | all 4 | all 4, demo-polished |
| Tool "servers" | plain Python functions | plain Python functions | real MCP servers + gateway | same, with real integrations behind them |
| Orchestration | hand-written sequential function | same, generalized | LangGraph graph | same |
| Realtime | polling | SSE | SSE over Redis streams | same |
| Cache | none | none | Redis | Redis |
| Auth | none | JWT + RBAC | same | same + hardened |
| HITL | boolean flag | risk-based auto/manual split | signed token, TTL, dry-run simulator | same |
| Notifications | logged only | logged only | logged only | real email/Telegram if keys exist |
| Flight schedule data | seeded (Faker) | seeded (Faker) | seeded (Faker) | real (AviationStack) if key exists, else seeded |
| PSS / crew / AODB gates / MEL | seeded (Faker) — permanent | seeded — permanent | seeded — permanent | seeded — permanent |
| Weather | real (Open-Meteo, METAR/TAF — no key) | real | real | real |
| Deploy | local only | local only | local only | Render/Fly + Vercel |
