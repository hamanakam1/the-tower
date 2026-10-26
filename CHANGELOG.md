# Changelog

## 0.4.0

### Admission
- **Pure policy core** (`the_tower/policy.py`) — admission is now a pure
  function of a frozen mesh snapshot: no I/O, no clock, replayable
- **Clearance contract v2** (`POST /clearance/v2`) — idempotency keys, 202 on
  accept, and an `admission` block carrying the policy version, the node
  state at decision time, and the score of every candidate
- **Decision replay** (`the_tower/replay.py`) — every clearance records the
  snapshot it decided against, so any decision can be re-derived later
  (`GET /decisions`, `POST /replay`)

### Safety
- **Graded safety ladder** (`the_tower/safety.py`) — admit → warn → throttle →
  drain → kill, instead of allow-or-`SIGKILL`
- **Advisory vs enforce** — per-node opt-in; the Tower recommends instead of
  acting, and records the recommendation
- **Node lifecycle** — `draining`, `cordoned`, `maintenance`, `offline`,
  `unknown`
- **Kill guard** — a kill requires a reason, only signals processes the Tower
  owns, and drains the node first
- **Freshness** — stale telemetry makes a node `unknown`; unknown nodes are
  never scheduled onto

### Operations
- **Schema migrations** — numbered, forward, recorded in `schema_version`
- **Telemetry rollup** — raw samples roll into 5-minute, hourly and daily
  buckets, so a month of mesh history stays small
- **Storage off the event loop** — every write runs on a worker thread
- **CORS allowlist** — `TOWER_CORS_ORIGINS`; no wildcard default
- **Config module** — every documented key loads, and every key has a test
  proving it takes effect
- **Pluggable sensors** — per-node temperature backends; a reading is always
  labelled `measured` or `estimated`
- **Per-node samplers** — `local`, `simulated` or `agent`; an agent-driven
  node is never given fabricated telemetry

### Scale
- Scope index and queued-flight index replace the per-sweep linear scans
- Rate-limit buckets are bounded

### Tests
- 101 tests, up from 31

## Unreleased

### Safety
- **Advisory is finally the real default.** `NodeSpec.enforcement` defaulted to
  `enforce`, and so did `default_nodes()` and `config.example.yaml`, while the
  README and roadmap promised advisory. A fresh Tower now recommends kills and
  preemptions and never acts until a node is explicitly put into `enforce`.
- **Advisory preemption no longer overcommits a node.** When the Tower
  recommends preempting an incumbent it cannot displace, the newcomer is
  queued (and told why) instead of being cleared into capacity that is still
  held. The recommendation is published once, not every sweep, and the flight
  resumes when the incumbent lands.
- **`POST /nodes/:id/enforcement` requires an explicit `mode`.** A missing
  field used to mean `enforce`.
- **Kill authority.** A kill only signals PIDs the Tower was explicitly given
  ownership of; a numeric container id is never treated as a pid, PID 0/1 are
  never claimed, and ownership survives a restart.
- **`drain_timeout_s` is real.** A kill with a grace period puts the flight
  into `DRAINING` with a persisted deadline; it lands if it finishes, and is
  terminated at the deadline.
- **The event log stops crying wolf.** Thermal spikes are measured against the
  local level instead of all history, the same condition is held down for 60s,
  the simulated telemetry moves with thermal inertia, and the time-to-redline
  forecast is never negative.
- **A lifecycle state is operator intent, not telemetry.** A `cordoned`,
  `maintenance` or `draining` node is no longer returned to service by the
  next sweep — including the drain that precedes a kill. A drain clears
  itself once the work in the air is gone; `cordoned` never does.
- **Preemption places the newcomer.** A preempting flight used to be cleared
  with no node, no reservation and no lease while the incumbent was shot
  down. It now takes the incumbent's node, and the preemption only happens
  when the newcomer actually fits there once the incumbent's weight is
  released; otherwise the request is queued and the incumbent keeps running.

### Reliability
- **Telemetry rollup actually rolls up.** Every age band (1m, 5m, 1h, 1d) is
  produced, one row per bucket is enforced by a unique index, and history
  older than the retention window is pruned. The old code rolled everything
  into 1-minute buckets and deleted it, so the coarser bands never existed.
- **A queued flight is not re-announced every sweep.** The dispatcher retries
  queued work quietly; a retry that reaches the same answer does not write a
  second event or audit row.
- **An idempotency key whose answer was lost still cannot reschedule.**
- **A live lease cannot be released while its flight is still airborne**, and
  an expired lease cannot be renewed back to life.
- **Unregistering a node forgets it.** The spec is deleted from the store, so
  the node does not come back on restart, and the flights, leases and queue
  entries it was holding are closed with it.
- **Decisions are replayable.** Every clearance records the flight and the
  mesh snapshot it was decided against (`GET /replay/flight/{id}`), including
  queued decisions.
- **Idempotency survives a restart.** The key and its answer are persisted
  with the flight, so a retry after a restart cannot schedule the work twice.
- **The forecast uses real timestamps.** The thermal regression no longer
  assumes a five-second sweep interval.
- **The agent can register itself.** `the-tower agent --register` describes
  the machine it runs on and stops the Tower simulating that node.

### Dashboard
- **Every node shows whether it is armed.** The node panel carries an
  `advisory` / `enforce` badge and an arm/disarm control; arming asks for
  confirmation first. A lease is its own kind of event, so the log no longer
  reports one as a second takeoff.

### Operations
- **`/metrics` answers operational questions.** Per-node enforcement mode,
  thermal margin to redline, telemetry age, one-hot lifecycle status, safety
  ladder rung, and the number of active leases. Label values are escaped.
- **`nodes_online` means reachable.** A node whose telemetry went stale is
  `unknown` and is no longer counted as online.

### Fixed
- Fix audit id collision: `audit-<epoch-ms>` raised `UNIQUE constraint failed` on two entries in the same millisecond; now `audit-<epoch-ns>`
- Fix audit chain ordering: second-resolution timestamps were ambiguous; the chain now follows insertion order
- Fix storage thread affinity: the SQLite connection was bound to the constructing thread and raised `ProgrammingError` on any write from another thread; the connection is now shared under a lock
- Update the dashboard screenshots and add regression tests for the audit chain, storage thread safety, kill authority, idempotency, replayability, false positives and onboarding

### Docs
- Add the long-term build plan (`docs/roadmap.md`), the proposed target architecture diagram (`docs/architecture/`) and dashboard screenshots (`docs/screenshots/`)

## 0.3.0
- Prometheus `/metrics` endpoint
- Flight detail endpoint with related events
- Telemetry agent (`the-tower agent`)
- Dockerfile + .env.example
- Structured logging
- Detailed `/health` with uptime
- CONTRIBUTING.md
- Declare `websockets`, `httpx` and `pyyaml` as runtime dependencies — a clean install previously answered the `/ws` handshake with 404 and live push was silently dead

## 0.2.0
- SQLite persistence, WebSocket live push
- Node registration, scope conflicts, priority dispatch
- Container registry + heartbeat kill-switch
- Auth middleware, rate limiting, anomaly z-scores
- Demo seeder, CI workflow, config example

## 0.1.0
- Initial hardware ATC with dashboard and visualization
