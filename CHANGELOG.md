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

- Restore active and draining reservations, queued node membership, and durable
  leases on restart. Migration 7 adds lease storage with a rollback; paired
  flight/lease transitions are atomic and unreadable admission state aborts startup.
- Preserve drain resource claims through lease expiry, reject expired lease
  renewals, and keep obsolete leases from evicting newer attempts.
- Serialize concurrent v2 idempotency requests within one server instance;
  reject malformed inputs with HTTP 400 and incomplete persisted answers with
  HTTP 409 instead of admitting duplicate work.
- Reject malformed kill commands before mutation, require an explicit reason,
  and validate boolean flags and finite drain grace periods of up to 24 hours.
- Keep owned processes alive during a drain grace period and terminate them
  through the normal ownership/audit path when the deadline expires.
- Recheck node capacity and remaining scopes after enforced preemption, release
  the incumbent lease, and keep advisory or draining scope owners protected.
- Fix audit id collision: `audit-<epoch-ms>` raised `UNIQUE constraint failed` on two entries in the same millisecond; now `audit-<epoch-ns>`
- Fix audit chain ordering: second-resolution timestamps were ambiguous; the chain now follows insertion order
- Fix storage thread affinity: the SQLite connection was bound to the constructing thread and raised `ProgrammingError` on any write from another thread; the connection is now shared under a lock
- Add regression tests for the audit chain and storage thread safety
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
