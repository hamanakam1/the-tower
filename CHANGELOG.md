# Changelog

## Unreleased
- Fix audit id collision: `audit-<epoch-ms>` raised `UNIQUE constraint failed` on two entries in the same millisecond; now `audit-<epoch-ns>`
- Fix audit chain ordering: second-resolution timestamps were ambiguous; the chain now follows insertion order
- Fix storage thread affinity: the SQLite connection was bound to the constructing thread and raised `ProgrammingError` on any write from another thread; the connection is now shared under a lock
- Add regression tests for the audit chain and storage thread safety
- Add the long-term build plan (`docs/roadmap.md`), the proposed target architecture diagram (`docs/architecture/`) and dashboard screenshots (`docs/screenshots/`)

## 0.3.0
- Declare `websockets`, `httpx` and `pyyaml` as runtime dependencies — a clean install previously answered the `/ws` handshake with 404 and live push was silently dead

## 0.3.0
- Prometheus `/metrics` endpoint
- Flight detail endpoint with related events
- Telemetry agent (`the-tower agent`)
- Dockerfile + .env.example
- Structured logging
- Detailed `/health` with uptime
- CONTRIBUTING.md

## 0.2.0
- SQLite persistence, WebSocket live push
- Node registration, scope conflicts, priority dispatch
- Container registry + heartbeat kill-switch
- Auth middleware, rate limiting, anomaly z-scores
- Demo seeder, CI workflow, config example

## 0.1.0
- Initial hardware ATC with dashboard and visualization