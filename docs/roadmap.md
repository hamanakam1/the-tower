# The Tower — Long-Term Plan

**Status:** living document · last reviewed 2026-10-22 · against `v0.4.0` (`6853d22` + the kill-authority, forecast and idempotency fixes)

This is the build plan for The Tower: what it is, how it should be engineered,
what the next three years look like, and what must be true before each phase
is called done. It is written against the source that exists today, not
against an aspiration.

Every claim about current behaviour below was read from the code at
`5b05300`. Every claim about future behaviour is explicitly labelled
*proposed*.

---

## 1. What The Tower is

The Tower is an **admission-control plane for physical compute**. It is the
component of the autonomous mesh that decides whether a task is allowed to run
on a real machine, and what it is allowed to do to that machine while it runs.

It is not a scheduler in the Kubernetes sense (it does not own the workload
lifecycle), and it is not a monitoring system (it does not merely observe). It
sits between the two:

```text
   requester            The Tower                 node
  (El Hidratador)   ┌────────────────────┐   ┌──────────────┐
        │          │ admission control   │   │  the work    │
        └─flight──▶│ policy · scoring ·  │──▶│  actually    │
                   │ leases · stop       │   │  executes    │
                   └─────────┬──────────┘   └──────┬───────┘
                             │                     │
                             ▼                     ▼
                     decision ledger        thermal · power ·
                     (hash-chained)          battery · memory
```

### The job to be done

> "I want to run this job on the mesh. Tell me where it can safely run — and
> stop it if the hardware is about to cook."

### Three properties that define the product

1. **It decides, it does not execute.** The Tower grants clearance and can
   revoke it. The worker still does the work. This keeps the Tower small and
   keeps the blast radius of a Tower bug inside the Tower.
2. **Every decision is explainable.** Clearance and kill decisions carry a
   reason, the inputs that produced it, and land in a hash-chained ledger.
   "Why did this run on the Pi instead of the M3?" must have an answer.
3. **Unknown means no.** If telemetry is stale, missing, or untrustworthy, the
   Tower does not schedule onto that node. Failing open onto a hot machine is
   the one failure mode the whole product exists to prevent.

### Non-goals (things The Tower will never do)

- Execute user workloads, or become a container orchestrator.
- Replace El Hidratador (DAG construction) or Taskrivet (execution).
- Read the private Ticket Board. The Tower sees requests, never the roadmap.
- Call paid cloud models. Inference for anomaly detection is local-only.
- Pretend to be a general-purpose observability stack.

---

## 2. Where we are: an honest read of `v0.4.0`

The Tower started as a working prototype with a genuinely good skeleton: a
clear domain model (`schemas.py`), a clear engine (`core.py`), a thin HTTP
layer (`server.py`), and a persistence boundary (`storage.py`). Horizon 0 is
now closed and the shape has changed underneath it — the admission decision
is pure, the safety ladder is graded, nodes have leases and lifecycle
states, and the ledger records what was decided.

What has not changed is the shape of the problem: **most of the mesh is still
fiction.** The default fleet is three synthetic nodes, and the only node that
reports anything real reports a temperature it guessed.

### What is real today

| Area | Reality | Evidence |
|---|---|---|
| Decision core | A pure `decide(flight, snapshot)` over a frozen mesh snapshot, separate from the effects that follow | `the_tower/policy.py` |
| Safety | A graded ladder — admit, warn, throttle, drain, kill — with per-node advisory or enforce mode | `the_tower/safety.py` |
| Engine | Capacity checks, node scoring, priority queue, scope preemption, leases with renewal and expiry, redline handling | `the_tower/core.py` |
| Persistence | SQLite in WAL mode with numbered migrations and rollbacks: flights, events, telemetry, containers, nodes, hash-chained audit | `the_tower/storage.py` |
| Explainability | Every decision carries the scores it considered and the node state it decided against, and can be replayed later | `the_tower/replay.py`, `POST /replay` |
| Local telemetry | Real `psutil` CPU/memory/load on the host, with a labelled estimate where no sensor exists | `the_tower/sensors.py` |
| Transport | Starlette HTTP + WebSocket; optional bearer token; optional per-IP rate limit; CORS is an allowlist | `the_tower/server.py` |
| Dashboard | Single-file HTML/CSS/JS; canvas radar; live flight board; leases panel; safety ladder; kill console | `the_tower/static/index.html` |
| Ops | Dockerfile, Makefile, CI on push/PR, `.env.example`, Prometheus `/metrics` | repo root |

### What is still simulated or unreachable

| Area | Reality | Impact |
|---|---|---|
| Multi-node telemetry | Nodes declare a sampler. `simulated` is still the default for anything that is not this machine, and `agent` only works once an agent is actually running on the node. | The Pi cluster and trailer are decorative until real hardware is attached. |
| Temperature | macOS exposes no CPU thermal sensor to an unprivileged process, so the local node's temperature is an estimate from CPU load. It is labelled `estimated` wherever it is shown. | "Thermal ATC" is still "CPU-load ATC" on the local machine. |
| Enforcement | Advisory is the default and per-node enforcement is an opt-in flag. | Correct for now, but it means the Tower will not stop a runaway on its own until someone opts in. |
| Scale | The scope index and the queued-flight index keep the common paths proportional, but the mesh is one process on one machine with one SQLite file. | Horizon 4 is a rewrite, not a tuning exercise. |
| Rollups | Telemetry rolls up into buckets, but there is no cross-node query layer. | Answering "what did the whole mesh do last Tuesday" means reading the store by hand. |

### Defects found and fixed while writing this plan

- `storage.py` — audit IDs were `audit-<epoch-ms>`, so two audit entries in the
  same millisecond raised `UNIQUE constraint failed` and returned HTTP 500.
  Now `audit-<epoch-ns>`. Chain ordering also used second-resolution
  timestamps, which is ambiguous; it now uses insertion order (`rowid`).
- `pyproject.toml` — the WebSocket endpoint was advertised in the README and
  wired into the dashboard, but `websockets` was not a declared dependency, so
  on a clean install uvicorn answered the handshake with **404** and live push
  was silently dead. Fixed in the `v0.3.0` tag commit (`websockets`, `httpx`
  and `pyyaml` are now runtime dependencies — all three were already imported
  by the code).
- `storage.py` — the SQLite connection was created on the constructing thread
  and used from whatever thread served the request, which raised
  `ProgrammingError` on the first write in a multi-threaded deployment. The
  connection is now explicitly shared (`check_same_thread=False`) and every
  storage operation is serialised through one lock.
- `core.py` — the kill path treated a registered container whose id happened
  to be all digits as a process id and sent it `SIGKILL`. The Tower now keeps
  an explicit registry of the PIDs it owns and signals nothing else; PID 0
  and 1 are never claimed, and ownership survives a restart.
- `core.py` — `drain_timeout_s` was accepted by the API and then ignored:
  "drain before kill" stopped new flights being admitted and killed the
  flight immediately, so nothing ever got the chance to land. A kill with a
  grace period now puts the flight into a DRAINING state with a persisted
  deadline.
- `anomaly.py` — the thermal regression assumed five seconds between
  samples, but the sweep interval is configurable. On a tower running with
  `--interval 2` the time-to-redline prediction was two and a half times too
  optimistic, and that prediction is what decides whether a node is drained.
  The regression now runs against the real sample timestamps.
- `server.py` — the v2 clearance contract promised that a retried request
  never schedules the work twice, but the answer was kept only in an
  in-memory cache. A retry that arrived after a restart scheduled a second
  flight. The key and the answer are now persisted with the flight.
- `anomaly.py` — the spike detector compared the newest temperature against
  the mean of everything before it, so a machine that was warming up fired a
  "thermal spike" on every sweep: nine false alarms in forty seconds. A
  spike is now a sharp departure from the local level, the same condition
  is held down for a minute, the simulated telemetry moves with thermal
  inertia, and the time-to-redline forecast is never negative.
- `agent.py` — the agent pushed telemetry at a node id the Tower had never
  heard of and printed 404 forever. It can now register itself, describing
  the machine it is running on.
- `schemas.py` / `config.example.yaml` — `NodeSpec.enforcement` defaulted to
  `enforce`, and every node in the example config was `enforce`, while the
  README, roadmap and CHANGELOG all promised that advisory was the default
  and acting was an opt-in. The default is now `advisory` in the schema, in
  `default_nodes()` and in `config.example.yaml`; a regression test forces a
  redline on the default fleet and asserts nothing is killed.
- `core.py` — with advisory as the real default, an advisory preemption left
  the newcomer `cleared` into capacity the incumbent still held, which
  overcommitted the node. The request is now queued and explained, the
  recommendation is published once instead of on every sweep, and it resumes
  by itself when the incumbent lands or the node is enforced.
- `server.py` — `POST /nodes/:id/enforcement` defaulted a missing `mode` to
  `enforce`. An omitted field now returns 400: switching a node to
  enforcement must be explicit.
- `storage.py` — the telemetry rollup rolled *everything* older than a minute
  into 1-minute buckets and then deleted the raw rows, so the 5-minute,
  hourly and daily bands never saw a sample and never existed, while the
  1-minute table grew without bound. The age bands are now disjoint, one row
  per bucket is enforced by a unique index (migration 7), and rolled history
  older than the retention window is pruned.
- `server.py` — the v2 idempotency key was persisted before its answer, so a
  crash in between left a keyed flight with no stored response and the next
  retry scheduled the work again. A missing answer is now reconstructed from
  the flight's own state.
- `core.py` — `DELETE /leases/:id` flipped a lease to released without
  touching the node's capacity, so the resources of a running flight came
  back while the work still ran; and an expired lease could be renewed back
  to life after the sweep had already handed its resources back. A live lease
  now refuses release (409), and only an active lease can be renewed.
- `schemas.py` — a lease was emitted as a second `takeoff` event. It has its
  own `lease` kind now.
- `core.py` — `nodes_online` counted every node that was not `offline`,
  including `unknown`. A node whose telemetry had gone stale therefore showed
  as online, and a dashboard or pager watching 3/3 could not see a dead
  machine. Reachability is now its own set of states.
- `metrics.py` — the only series were CPU, memory and temperature, so nothing
  could alert on "this node is armed and at redline" or "this node has stopped
  reporting". Added per-node enforcement, thermal margin, telemetry age,
  status (one-hot), safety-ladder rung, active leases and landed flights.
  Label values are escaped: node ids are operator input.
- `core.py` — `DELETE /nodes/:id` only forgot the node in memory. Its spec
  stayed on disk and the node came back on the next start, and its leases and
  queued flights were never released. Unregistering now deletes the spec and
  closes every claim the node held.
- `radar.py` — every sweep recomputed a node's status from telemetry, so a
  `cordoned`, `maintenance` or `draining` node silently went back into service
  within five seconds. That is exactly the machine an operator had taken out
  of the airspace on purpose, and it included the drain that precedes a kill.
  Lifecycle states are now pinned against telemetry; a drain clears itself
  only when the work in the air is gone, and `cordoned` never clears itself.
- `policy.py` — a preempting flight was cleared with `assigned_node=None`,
  so it flew with no node, no reservation and no lease while the incumbent
  was shot down. Preemption now places the newcomer on the incumbent's node
  and only fires when the newcomer actually fits there; otherwise the request
  is queued and the incumbent keeps running.
- `core.py` — the stale-container watchdog killed a silent container on every
  node, including advisory ones, and closed its flight. If the Tower did not
  own the process, that freed capacity while the process was still running.
  A silent container on an advisory node is now a recommendation (published
  once a minute); a kill needs either an enforced node or an owned pid.
- `safety.py` — an `offline` or `unknown` node was shown at the `kill` rung of
  the ladder, implying the Tower was about to terminate work it could not
  even see. Those states are `drain`: stop adding work, touch nothing.

> These fixes are in this branch (except the dependency declaration, which
> landed in the `v0.3.0` tag), with regression tests in
> [`tests/test_storage.py`](../tests/test_storage.py),
> [`tests/test_kill_authority.py`](../tests/test_kill_authority.py),
> [`tests/test_anomaly.py`](../tests/test_anomaly.py),
> [`tests/test_false_positives.py`](../tests/test_false_positives.py),
> [`tests/test_idempotency.py`](../tests/test_idempotency.py),
> [`tests/test_replayability.py`](../tests/test_replayability.py) and
> [`tests/test_onboarding.py`](../tests/test_onboarding.py). The rest are
> scheduled in Horizon 0.

### Horizon 0 progress

Horizon 0 is the gate between "a prototype" and "something you may point at a
real machine". Most of it is now closed. The table records what was done, not
what was intended.

| # | Work | State | Where |
|---|---|---|---|
| 0.1 | Single-source the version | **done** | `the_tower/__init__.py` → `/health`, dashboard footer, `pyproject` |
| 0.2 | Config parity | **done** | `the_tower/config.py`; every documented key has a test in `tests/test_config.py` |
| 0.3 | Shorthand clearance keeps `scope`, `weight.gpu_units`, `constraints` | **done** | `server.py`; covered by `tests/test_server.py` |
| 0.4 | Pluggable sampler per node | **done** | `the_tower/sensors.py`, `NodeSpec.sampler` (`local` / `simulated` / `agent`) |
| 0.5 | Kill guard: owned PIDs only, reason required, drain first | **done** | `core.py::kill`; `NodeStatus.DRAINING`; an explicit PID ownership registry |
| 0.6 | Audit every decision | **done** | clearance, queue, preempt, kill, drain, node state, enforcement change |
| 0.7 | SQLite migrations (numbered, forward, with rollback) | **done** | `the_tower/storage.py::MIGRATIONS` and `ROLLBACKS` |
| 0.8 | Telemetry retention and rollup | **done** | `storage.py::rollup_telemetry`; 1m → 5m → 1h → 1d buckets |
| 0.9 | Storage off the event loop | **done** | `core.py::_store` runs every write on a worker thread |
| 0.10 | Telemetry freshness, `unknown` state | **done** | `NodeStatus.UNKNOWN`, `staleness_budget_s` |
| 0.11 | Bounded rate-limit buckets, CORS allowlist | **done** | `server.py`; `TOWER_CORS_ORIGINS`, no wildcard default |
| 0.12 | Fill the test gaps | **done** | 184 tests across policy, safety, replay, config, sensors, storage, server, anomaly, kill authority, idempotency, replayability, metrics and onboarding |
| — | Graded safety ladder (§3.2) | **done** | `the_tower/safety.py`: admit → warn → throttle → drain → kill |
| — | Pure decision core (§3.1) | **done** | `the_tower/policy.py`; the Tower drives it with a frozen snapshot |
| — | Advisory vs enforce (§3.5) | **done** | `NodeSpec.enforcement`, advisory by default; the Tower recommends instead of acting |
| — | Decision replay (§3.1) | **done** | `the_tower/replay.py`; `POST /replay`, `GET /decisions` |
| — | Node lifecycle states | **done** | draining / cordoned / maintenance / offline / unknown |
| — | Leases | **done** | `core.py::issue_lease`; renewal, expiry and automatic rescheduling |

### The guarantees that now hold

These are the properties a reader should be able to check the code against.
Each one has a test.

| Guarantee | Where it is enforced | Test |
|---|---|---|
| The Tower never signals a process it was not given | `core.py::_owned_pid_for` | `tests/test_kill_authority.py` |
| A numeric container id is never a licence to kill that pid | `core.py::_owned_pid_for` | `tests/test_kill_authority.py` |
| A kill needs a reason | `core.py::kill` | `tests/test_kill_authority.py` |
| A flight given a grace period lands, or is terminated at the deadline | `core.py::_progress_flights` | `tests/test_kill_authority.py` |
| A node with stale telemetry is `unknown` and is never scheduled onto | `policy.py::NodeView.accepts` | `tests/test_server.py` |
| The time-to-redline forecast does not depend on the sweep interval | `anomaly.py::time_axis` | `tests/test_anomaly.py` |
| A retried clearance request never schedules the work twice, even across a restart | `server.py::clearance_v2` | `tests/test_idempotency.py` |
| Every clearance decision can be replayed from the Tower's own record | `core.py::replay_flight` | `tests/test_replayability.py` |
| A fresh Tower advises; it never kills or preempts without an explicit opt-in | `schemas.py::NodeSpec.enforcement` | `tests/test_kill_authority.py` |
| An advisory preemption never clears a flight into capacity the incumbent still holds | `core.py::_act_preempt` | `tests/test_kill_authority.py` |
| A preemption only happens when the newcomer fits the incumbent's node afterwards | `policy.py::decide` | `tests/test_policy.py` |
| A silent container on an advisory node is reported, not killed | `core.py::_on_sweep` | `tests/test_core.py` |
| The telemetry rollup produces every age band and prunes past a month | `storage.py::rollup_telemetry` | `tests/test_storage.py` |
| A retry whose stored answer was lost still cannot schedule the work twice | `server.py::clearance_v2` | `tests/test_idempotency.py` |
| A live lease cannot be released out from under a running flight | `core.py::release_lease` | `tests/test_leases.py` |
| A node with stale telemetry is not reported as online | `core.py::airspace` | `tests/test_metrics.py` |
| A node id cannot forge a metric series | `metrics.py::_label` | `tests/test_metrics.py` |
| Unregistering a node forgets it permanently and releases its claims | `core.py::unregister_node` | `tests/test_onboarding.py` |
| A cordoned, draining or maintained node is never returned to service by telemetry | `radar.py::_status_for` | `tests/test_core.py` |
| A fresh drain returns the node to service once the work in the air is gone | `core.py::_progress_flights` | `tests/test_core.py` |
| A warming machine is not a thermal spike | `anomaly.py::detect_spike` | `tests/test_false_positives.py` |
| The same condition is not reported on every sweep | `anomaly.py::_should_report` | `tests/test_false_positives.py` |
| A node can join the mesh in one command | `agent.py::node_spec` | `tests/test_onboarding.py` |
| Every decision is in the hash-chained ledger | `core.py::_emit`, `storage.py::append_audit` | `tests/test_storage.py` |
| A schema change ships with a forward migration and a rollback | `storage.py::MIGRATIONS`, `ROLLBACKS` | `tests/test_storage.py` |

**Still open in Horizon 0:** a real macOS CPU thermal sensor (the platform
exposes none to unprivileged processes, so the local node's temperature is a
labelled estimate), and rolling the `simplify` pass further into
`core.py`.

---

## 3. How this software should be built
Engineering rules that survive every phase. When two designs conflict, these
win.

### 3.1 The decision core is pure; the effects live at the edges

Admission must be a **function of observed state**, not a function of history:

```python
# pure: same inputs, same decision, no I/O, no clock
def decide(request: ClearanceRequest, snapshot: MeshSnapshot) -> Decision: ...

# impure: effects, ordered and logged
async def act(decision: Decision) -> Outcome: ...
```

`request_clearance` in `core.py` currently mixes scoring, state mutation,
event emission, persistence, and dispatch. Splitting it is the single highest
leverage refactor in the project: it makes the decision testable without a
database, replayable against recorded telemetry, and reviewable by a human.

### 3.2 Safety is graded, never binary

Today the Tower has two outcomes: allow, or `SIGKILL`. Real hardware needs a
ladder:

```text
   admit ──▶ warn ──▶ throttle ──▶ drain ──▶ kill
              │         │            │         │
          log only  cap CPU/MEM   reschedule   revoke
                                         flight
```

Every rung is a policy decision, recorded with its inputs. `kill` is the last
rung, not the first, and it requires a reason, a target the Tower actually
owns, and a drain attempt first.

### 3.3 Unknown state is a first-class value

`Telemetry` today has no notion of freshness. Add it: every sample carries a
timestamp, every node carries `last_seen`, and a node whose telemetry is
older than `staleness_budget` is **`unknown`**, not `online`. Unknown nodes
are never scheduled onto. This single change converts most of the safety
story from "clever heuristics" into "explicit state".

### 3.4 Nothing destructive happens without a recorded reason

Every mutation to the physical world — a kill, a throttle, a drain — carries
an actor, a reason code, the inputs that triggered it, and a hash-chain
link. If the ledger cannot accept the record, the action does not happen.

### 3.5 The Tower never blocks the mesh

A Tower that is down must not stop work that is already safe. The Tower is
**advisory by default**: it denies only what it can prove is unsafe.
Enforcement mode is an explicit, per-node, auditable opt-in. This is the
difference between an ATC and a bottleneck.

### 3.6 Boring technology, small reversible changes

- Python 3.11+, Starlette, Pydantic v2, SQLite. That is the stack.
- No message broker, no Kubernetes, no service mesh, no cloud dependency.
- No new runtime dependency without a written reason in the PR.
- Every schema change ships with a migration and a rollback.

### 3.7 Local-first, and honest about it

Anomaly detection and forecasting run on local models or on classical
statistics. No paid cloud inference is ever a dependency of the Tower's
control path. When a model is used, its version and inputs are recorded with
the decision.

---

## 4. Target architecture

![Proposed target architecture](architecture/tower-target.png)

The typed specification for this diagram is
[`architecture/tower-target.architecture.json`](architecture/tower-target.architecture.json).
It was rendered and validated at 9/9 showcase checks with zero composition
errors, and browser-checked for containment at 1440×900, 1600×1000, 1920×1080
and 2048×1320.

### The four planes

| Plane | Responsibility | Horizon |
|---|---|---|
| **Control** | `Admission API` → `Policy Engine` → `Scheduler` → `Digital Twin`. Decide where a flight runs and whether it may run. | 1–2 |
| **Data plane** | `Node Agents`, one per machine. Report real telemetry, hold leases, execute stop orders, never decide anything. | 1 |
| **State** | `Telemetry Store` (observations, rolled up) and `Decision Ledger` (immutable, hash-chained, exported to EdgeRecord). | 0–2 |
| **Authority** | `Anti-Air Battery`. The only component allowed to stop a process. Graded. | 2 |

### The clearance contract (v2)

```jsonc
POST /clearance/v2
{
  "idempotency_key": "el-hidratador-7f3a91",   // retried requests never double-schedule
  "task": "distill-lora-adapter-run",
  "scope": ["model/lora", "data/train"],        // conflict keys — always honoured
  "priority": 1,                                 // P0..P3
  "weight": { "cpu_cores": 6, "memory_gb": 24, "gpu_units": 0,
              "duration_est_s": 900, "network_mbps": 40,
              "thermal_budget_c": 8, "energy_budget_wh": 180 },
  "constraints": { "preferred_node": null, "not_before": null,
                   "deadline": null, "drainable": true }
}
```

```jsonc
202 Accepted
{
  "decision": "cleared",
  "flight_id": "flt-9c2d04",
  "assigned_node": "pi-01",
  "lease": { "id": "lease-4417", "expires_at": "2026-10-22T08:14:22Z",
             "ttl_s": 900, "renewable": true },
  "admission": {
    "policy_version": "p-2026.10.1",
    "node_state_at_decision": { "temp_c": 58.2, "thermal_state": "nominal",
                                "predicted_redline_in_s": 3100,
                                "telemetry_age_s": 4 },
    "scored": { "m3max": 0.41, "pi-01": 0.88, "trailer-hpc": 0.22 },
    "reason": "lowest predicted thermal headroom penalty; scope free"
  }
}
```

The `admission` block is the product. It is what makes the decision
explainable, replayable, and diffable between versions.

---

## 5. Roadmap

Horizons are sequenced by **risk retired**, not by feature count. Each horizon
ends with a version tag, an exit checklist, and an explicit "what we are
willing to break" note.

### Horizon 0 — Harden (`v0.4.x`, weeks 1–4)

**Goal:** make what exists trustworthy enough to point at a real machine.
Nothing new is added; everything that exists is made honest.

| # | Work | Done when |
|---|---|---|
| 0.1 | Single-source the version (`__version__` → `/health`, dashboard footer, `User-Agent`, `pyproject` build metadata) | One version string, every consumer reads it, no drift |
| 0.2 | Config parity: `config.example.yaml` fields actually load, or are removed | Every documented key has a test proving it takes effect |
| 0.3 | Fix the shorthand clearance path so `scope`, `weight.gpu_units`, `constraints` survive | A scope-conflict test passes through the HTTP API |
| 0.4 | Replace the simulated-node branch with a pluggable sampler per node | A node registered with a real id reports real telemetry |
| 0.5 | Guard the kill path: only kill PIDs the Tower owns, require a reason, require a drain attempt first | A kill of an unowned PID is refused and logged |
| 0.6 | Audit **every** decision, not just explicit `POST /audit` | Clearance, deny, queue, preempt, kill all appear in the chain |
| 0.7 | SQLite migrations (numbered, forward, with rollback) | A schema change ships with a migration and a test |
| 0.8 | Retention and rollup for `telemetry` (raw → 1m → 1h → 1d) | 30 days of 5-second samples on one node stays under 50 MB |
| 0.9 | Move storage off the event loop (writer thread or `aiosqlite`) | A 1,000-request burst does not stall the radar sweep |
| 0.10 | Telemetry freshness: `last_seen`, `staleness_budget`, `unknown` state | A silent node is `unknown` within one sweep and is not scheduled onto |
| 0.11 | Bound the rate-limit buckets; move CORS to an allowlist | Memory is flat under sustained load; `*` is gone by default |
| 0.12 | Fill the test gaps: scope conflict via HTTP, kill guard, audit chain continuity, migrations, freshness | Coverage of the decision core and the kill path is the highest in the repo |

**Exit criteria:** every row in §2 "simulated or unreachable" is either fixed
or has an issue tracking it; the test suite passes on a clean install; the
README describes only behaviour that exists.

**What we are willing to break:** the `POST /clearance` shorthand body (v2
lands alongside it, not instead of it, but the shorthand is deprecated in
docs and frozen).

---

### Horizon 1 — Real mesh (`v0.5–v0.6`, months 2–4)

**Goal:** the Tower controls real machines, not a simulation of them.

**1.1 Node agents become first class.**
The current `the-tower agent` is a 40-line psutil pusher. It becomes a
supervised service with: node identity and capability discovery, a stable
per-node `NodeSpec` (cores, RAM, GPU, redline, thermal mass, power envelope),
heartbeat and lease renewal, agent-side enforcement of the Tower's stop
orders, graceful shutdown that releases leases, and an on-disk buffer so a
network blip does not lose telemetry.

**1.2 Node lifecycle states.**
`online → draining → cordoned → maintenance → offline → unknown`. Draining
stops new flights but lets in-flight work land. Cordoned is an operator
decision. This is what makes "shut down this node for firmware update" a
safe operation instead of a kill-everything event.

**1.3 Leases replace implicit reservations.**
Today `active_weights` is a dict that only the in-process Tower understands.
A lease is a first-class object with a TTL, a renewal path, an owner, and a
history. If an agent dies, its lease expires and its resources are released.
Nothing is stranded.

**1.4 Idempotent clearance and retry safety.**
`idempotency_key` on every request. A retried request returns the original
decision instead of scheduling twice.

**1.5 Real temperature.**
On Apple Silicon use `powermetrics`/`ioreg` or a purpose-built helper; on
Linux use the hwmon/coretemp sysfs path; on the Pi use the SoC sensor. The
Tower must distinguish **measured** from **estimated** in the telemetry
schema, and a node with no real sensor reports `sensor: estimated` and gets
a stricter policy.

**1.6 The scope model gets real.**
Scope keys become hierarchical resource paths (`data/train`, `model/lora`) with
read/write/intent modes, so two flights that both read `data/train` are not
conflicted while two that write it are.

**Exit criteria:** three physically distinct machines (M3 Max, two Pi 5s) run
real workloads under Tower control; an agent can be killed mid-flight and the
Tower releases its lease and reschedules without operator action; every
temperature reading on screen is either a real sensor or explicitly labelled
estimated.

**Risks:** sensor access is platform-specific and will consume more time than
expected. Mitigation: ship the agent with a "sensor backend" interface and
one implementation per platform, accepting an estimate for platforms without
one — but never silently.

---

### Horizon 2 — Policy and authority (`v0.7–v1.0`, months 5–9)

**Goal:** the Tower's decisions are governed by declared rules, and its
authority over hardware is explicit, graded, and auditable.

**2.1 Declarative policy.**
Policy moves from scattered Python constants into versioned YAML with a
schema, a linter, and a unit-testable evaluator:

```yaml
policy_version: p-2026.10.1
rules:
  - id: thermal-headroom
    when: { metric: predicted_redline_in_s, lt: 120 }
    then: { deny: "insufficient thermal headroom" }
  - id: off-grid-battery
    when: { node_class: off-grid, metric: battery_percent, lt: 25 }
    then: { deny: "battery below reserve" }
  - id: power-envelope
    when: { metric: projected_power_w, gt: "node.max_power_watts * 0.9" }
    then: { throttle: { cpu_cores: 0.5 } }
```

A policy version is recorded on every decision, so "why was this allowed last
Tuesday?" is answerable.

**2.2 The Digital Twin.**
Before a clearance is transmitted, the Tower replays the placement against a
reconstructed model of the node: its thermal mass, recent thermal slope,
active flights, and current draw. The twin answers "if this flight lands here,
where does the node peak, and does it cross the redline in the next ten
minutes?" A placement that the twin cannot vouch for is not sent.

The twin is also the regression harness: every historical clearance is
replayed against every new policy version and the differences are reported.
**This is how policy changes get validated without a molten Pi.**

**2.3 Graded stop authority.**
The Anti-Air Battery becomes a ladder — warn, throttle, drain, kill — with
each rung requiring a policy rule to authorise it. Kill additionally requires
the target to be a process the Tower owns. Advisory mode is the default;
enforcement is enabled per node with an explicit, audited flag.

**2.4 The ledger becomes the spine.**
Every decision — admit, deny, queue, preempt, throttle, kill — becomes a
ledger entry with actor, reason, inputs, policy version, and a chain link.
The ledger is append-only, exported to EdgeRecord, and independently
verifiable (`GET /audit/verify` already exists; it becomes a first-class
contract with a signed export and a periodic external attestation).

**2.5 Read-only by default.**
The Tower's own control API separates read and write surfaces with distinct
credentials. Killing hardware should be a deliberate, separately-authorised
action.

**Exit criteria:** every decision carries a policy version and a ledger
entry; the twin replays 30 days of history against a new policy with no
unexplained diffs; an operator can put a node into enforcement mode and take
it out without a restart; the ledger verifies end-to-end into EdgeRecord.

**Risks:** the twin is only as good as its model of the hardware. Mitigation:
the twin is advisory — it informs the decision and never becomes the sole
basis for a kill.

---

### Horizon 3 — Predict, don't react (`v1.1`, months 10–15)

**Goal:** stop running on the edge of the redline. Run on the far side of it.

**3.1 A real thermal model.**
Per-node first-order thermal response: thermal mass, ambient, dissipation, and
a measured cooling constant. Time-to-redline becomes a function of *projected*
load, not just current slope.

**3.2 Forecast-based admission.**
Instead of "is the node hot now?", admission asks "if this flight lands here,
does the node stay under redline for the flight's whole duration, with margin?"
The current `AnomalyRadar` linear regression (`anomaly.py`) is the seed of this
and should be replaced by the per-node model rather than extended.

**3.3 Energy awareness for off-grid nodes.**
The trailer node is solar and battery-constrained, and `battery_percent` and
`solar_input_watts` are already in the telemetry schema but unused by the
scheduler. Energy-aware admission schedules compute when the sun is out and
reserves battery for the night.

**3.4 Predictive maintenance.**
Thermal slope, fan curve, and battery degradation trend become inputs to
node-level maintenance advice ("this Pi's cooling is degrading; plan a swap").

**3.5 Learned models, locally.**
Any learned component is local-only, versioned, evaluated against the
recorded history before it is enabled, and can be turned off without
redeploying. The classical model remains the fallback and the baseline.

**Exit criteria:** the Tower clears a flight on a node predicted to cross
redline in 90 seconds — it must refuse, and the twin must agree; the
battery-constrained node completes its night schedule without a manual
override; every model decision records the model version and its inputs.

**Risks:** forecasting is easy to over-trust. Mitigation: forecasts inform
conservative admission margins, and the Tower keeps a hard redline guard
that no model can override.

---

### Horizon 4 — Scale the mesh (`v1.2–v1.3`, months 16–24)

**Goal:** run a Tower per site and a Tower of Towers across sites.

**4.1 Hierarchy.**
An **edge Tower** governs one site (a room, a trailer, a Pi cluster). A
**regional Tower** governs edge Towers and arbitrates between them. A global
Tower holds the fleet-wide policy and the fleet-wide ledger. This mirrors the
physical reality of the mesh: the trailer is solar-constrained, the rack is
not, and they should not share one scheduler.

**4.2 Federation and isolation.**
Sites can be disconnected. An isolated site keeps its own policy and ledger and
reconciles later; it does not lose history. Quorum and conflict rules for
cross-site scope are explicit.

**4.3 Multi-tenancy.**
Several operators or tenants share a Tower. Namespaces, quotas, per-tenant
policy overrides, and per-tenant audit trails.

**4.4 High availability.**
Leader election for the scheduler, hot failover for the API, and the audit
chain designed so two leaders cannot both write a conflicting chain.

**4.5 Storage at scale.**
SQLite stays the edge store. Regional/global tiers move to Postgres with
Timescale (or an equivalent) for telemetry retention and range queries.
Storage becomes a pluggable interface so the choice is per tier.

**Exit criteria:** two sites operate independently and reconcile; a site
loses its uplink for 24 hours and keeps its decisions and its ledger; a
regional Tower survives the loss of one edge Tower without losing the other.

---

### Horizon 5 — Ecosystem (`v2.0`, months 25+)

- **A published protocol.** The clearance contract, the telemetry schema, and
  the ledger format become a documented, versioned protocol so other
  orchestrators can ask the Tower for clearance.
- **Node policy packs.** A node class declares its own rules ("this is a
  solar trailer, never run above 70% battery"), so hardware-specific policy
  ships with the hardware, not in the Tower.
- **Plugin model for analysers.** Thermal, power, and audio analysers plug
  in behind a stable interface.
- **Integration depth with the family.** El Destilador supplies intent and
  resource estimates; El Hidratador requests clearance for a DAG; Taskrivet
  holds the lease and executes; El Verificador reports outcome; El Cronista
  learns from the ledger; EdgeRecord stores the signed chain. The Tower
  becomes the one place that knows whether the hardware can take it.
- **Community.** An RFC process for protocol changes, a public issue tracker
  with good templates (already started), a contributor guide, and a monthly
  demo of a real mesh under control.

---

## 6. Cross-cutting workstreams

These run across every horizon. They are not "phase 6".

### 6.1 Safety and security

| Area | Horizon 0 | Horizon 2 | Horizon 4 |
|---|---|---|---|
| AuthN | Token, hashed, rotatable, scoped | Per-surface credentials (read/write/authority) | Workload identity, mutual TLS for agents |
| AuthZ | Owner/observer roles | Per-node enforcement flags | Tenant namespaces and quotas |
| Transport | Loopback-only default | TLS termination documented | mTLS across the hierarchy |
| Kill path | Owned-process guard, reason required | Graded ladder, drain first | Two-person rule for fleet-wide kills |
| Secrets | Never in config files | Secret store or file with 0600 | Rotated, scoped, audited |
| Supply chain | Pinned CI actions, SBOM | Signed images, signed releases | Provenance attestation |
| Audit | Decision chain | Signed export to EdgeRecord | External periodic attestation |

A written **threat model** for the Tower is a Horizon 0 deliverable. The
dangerous surface is not the HTTP API — it is the ability to stop processes on
a machine.

### 6.2 Testing

The current 26 tests cover the happy paths well. The gaps are where the
danger is.

- **Decision-core unit tests** — the pure `decide()` function, property-based
  over random mesh states and requests.
- **Golden replay tests** — recorded telemetry + recorded requests → recorded
  decisions. Any change to the scheduler that alters a decision is a visible
  diff, reviewed on purpose.
- **Simulation tests** — a synthetic mesh of 100 nodes under a random workload
  against the twin and the policy engine. Chaos: kill agents, drop the
  network, spike temperature.
- **Hardware-in-the-loop tests** — a rack of Pi 5s running a soak suite with
  real thermal stress.
- **Contract tests** — the API contract, the ledger format, and the telemetry
  schema are all versioned and tested independently of the implementation.

### 6.3 Observability

- **Structured logs** with `flight_id`, `node_id`, `policy_version`, and
  `decision_reason` on every line. Already partially there
  (`core.py:117`).
- **Metrics** — extend the current `/metrics` to cover admission outcomes,
  queue depth, lease count, decision latency, kill counts by cause, thermal
  headroom, and agent liveness.
- **Traces** — OpenTelemetry around the clearance path, with the decision
  inputs attached.
- **SLOs** — clearance latency, false-deny rate, unexpected-kill count (target:
  zero), and node-state freshness.
- **Dashboards** — Grafana or equivalent for the operator view, separate from
  the in-product console.

### 6.4 Data and API evolution

- **Versioned API.** `/clearance/v2` with the old path deprecated on a stated
  schedule. `Accept`-style versioning is unnecessary; the path is clearer.
- **Versioned schemas.** Telemetry, clearance, and ledger records each carry a
  schema version, and the Tower can read the last three.
- **Migrations.** Numbered, forward-only, reversible, tested against a copy of
  a real database.
- **Retention.** Raw telemetry (7 days), rolled up (90 days), long-horizon
  aggregates (2 years). Events and the ledger are retained indefinitely.
- **One canonical decision record.** The same struct is used in the API
  response, the ledger, the event log, and the dashboard. Today these are
  four different shapes.

### 6.5 Delivery

- **CI**: tests, lint, type check, security scan, and a build of the Docker
  image on every push. Currently only tests run.
- **Releases**: semantic versioning, an automated changelog from conventional
  commits, a signed tag, and a release build with an SBOM.
- **One-command run**: `docker compose up` brings up the Tower with a
  simulated mesh for demos, and a real mesh for operators.
- **Migration path**: every release notes how to upgrade, including the
  migration command and the rollback.

### 6.6 Documentation and community

- **ADRs** for decisions that are expensive to reverse (kill semantics,
  lease model, ledger format, protocol version). One file per decision, in
  `docs/adr/`.
- **Runbooks** for the three things that will actually page someone: a node
  at redline, a Tower that will not start, a corrupted ledger.
- **Architecture docs** that match the code, updated in the same PR.
- **Examples** directory with a working simulated mesh, a real Pi join guide,
  and a policy pack.

---

## 7. Version and release plan

| Version | Theme | Breaking? | Content |
|---|---|---|---|
| `v0.4.x` | Honesty | No | Horizon 0. Version parity, config parity, kill guard, real audit, migrations, freshness, scope fix |
| `v0.5.x` | Real telemetry | No | Node agents v1, sampler backends, node lifecycle states |
| `v0.6.x` | Leases | **Yes** — internal state format | Leases, idempotency, drain semantics |
| `v0.7.x` | Policy | No | Declarative policy engine, policy version on every decision |
| `v0.8.x` | Twin | No | Digital Twin, replay harness |
| `v0.9.x` | Authority | **Yes** — kill semantics | Graded stop ladder, enforcement flags, read/write surfaces |
| `v1.0.0` | Stable control plane | No | Frozen v2 clearance contract, stable ledger format, supported upgrade path |
| `v1.1.x` | Prediction | No | Thermal model, energy-aware admission, maintenance signals |
| `v1.2.x` | Hierarchy | **Yes** — topology | Edge/regional/global Tower, federation, multi-tenancy |
| `v2.0.0` | Protocol | **Yes** — protocol | Published clearance and ledger protocol, node policy packs |

`v1.0.0` is the first version other systems may depend on. Before it, the
Tower's own README says *prototype* and nothing else should integrate
against it.

---

## 8. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| The Tower blocks the mesh when it is wrong | Medium | **Severe** | Advisory by default; enforcement opt-in per node; kill requires an owned target |
| A bad kill destroys in-flight work | Medium | High | Drain before kill; reason required; ledger entry before action; dry-run mode |
| Thermal model is wrong for a new board | High | High | Per-node sensor backends; measured vs estimated is explicit; conservative margins |
| Policy engine becomes too clever to review | Medium | Medium | Policies are small, declarative, versioned, and diffable; no arbitrary code in rules |
| Storage blocks the event loop at scale | Medium | Medium | Move to async I/O in Horizon 0; Postgres/Timescale in Horizon 4 |
| Scope grows into a full orchestrator | Low | High | Non-goals are written down; the Tower never executes workloads |
| Single author bottleneck | High | Medium | ADRs, RFC process, a public issue tracker, a contributor guide |
| Telemetry floods storage | Medium | Low | Rollup and retention from Horizon 0; bounded in-memory history |
| Cross-site policy conflicts | Medium | Medium | Explicit federation rules; per-site policy; reconcile, never merge silently |

---

## 9. Decisions I need from you

These are the points where the plan forks and I should not pick unilaterally.

1. **Enforcement default.** Should a fresh install ship in advisory mode
   (deny only what it can prove is unsafe) or enforcement mode (act on every
   policy decision)? *This plan assumes advisory.* It is the single biggest
   safety default in the system.
2. **Who holds kill authority.** Operator only, or can El Verificador request
   a stop for a failing flight? The second is powerful and would need the
   two-person rule.
3. **Agent trust model.** Is a Tower trusted to tell a Pi to stop a process,
   or does the agent independently enforce limits and the Tower only
   *requests*? The second is safer and slower.
4. **Storage tier.** Keep SQLite everywhere for simplicity, or introduce
   Postgres for the regional/global tiers in Horizon 4? Postgres adds a
   deployment burden but buys retention and concurrency.
5. **Scope of the protocol.** Should the clearance contract be published as
   an open standard in Horizon 5, or stay an internal contract between the
   family components? Publishing is a long-term commitment.
6. **Physical hardware access.** Do I get hands-on time with the real Pi
   cluster and the trailer for hardware-in-the-loop testing, or does the plan
   assume simulation only until then? This changes Horizon 1 and 2 estimates
   substantially.
7. **Naming and identity.** The Tower, the Tower of Towers, the Anti-Air
   Battery, and the Digital Twin are the names in this document. Keep them,
   or align with the family naming (`El …` / `the-…`)?

---

## 10. How this plan stays true

- Every horizon review re-reads the code and updates §2.
- The "simulated or unreachable" table is the scoreboard. It should shrink
  every horizon and be empty before `v1.0.0`.
- A decision that contradicts this document is either an ADR that supersedes
  a section, or a mistake in the document. Neither is "just a small change".
- `docs/architecture/tower-target.architecture.json` is the machine-readable
  form of §4 and is re-rendered when the architecture changes.
