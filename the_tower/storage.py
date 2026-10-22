"""Persistence layer for The Tower.

SQLite-backed store for flights, events, telemetry samples, and nodes.
Uses WAL mode for concurrent readers. Optional: Tower runs in-memory if
no db_path is provided.
"""
from __future__ import annotations

import functools
import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .schemas import Flight, TowerEvent, Telemetry


def _serialised(fn):
    """Serialise a storage operation against the shared SQLite connection."""

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)

    return wrapper


SCHEMA = """
CREATE TABLE IF NOT EXISTS flights (
    id TEXT PRIMARY KEY,
    callsign TEXT,
    task TEXT NOT NULL,
    requester TEXT,
    priority INTEGER,
    weight_json TEXT,
    container_id TEXT,
    preferred_node TEXT,
    status TEXT,
    submitted_at TEXT,
    cleared_at TEXT,
    takeoff_at TEXT,
    landed_at TEXT,
    shot_down_at TEXT,
    assigned_node TEXT,
    reason TEXT,
    rogue INTEGER,
    metadata_json TEXT,
    scope_json TEXT,
    retries INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 2
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    kind TEXT,
    timestamp TEXT,
    node_id TEXT,
    flight_id TEXT,
    container_id TEXT,
    message TEXT,
    severity TEXT,
    data_json TEXT
);
CREATE TABLE IF NOT EXISTS telemetry (
    node_id TEXT,
    timestamp TEXT,
    cpu_percent REAL,
    memory_percent REAL,
    memory_used_gb REAL,
    memory_total_gb REAL,
    temperature_c REAL,
    power_watts REAL,
    bandwidth_mbps REAL,
    load_avg REAL,
    gpu_percent REAL,
    thermal_state TEXT,
    battery_percent REAL,
    solar_input_watts REAL,
    source TEXT,
    source_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_tel_node_ts ON telemetry(node_id, timestamp);
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    spec_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    previous_hash TEXT,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS telemetry_rollup (
    node_id TEXT,
    bucket_start TEXT,
    bucket_seconds INTEGER,
    samples INTEGER,
    cpu_percent REAL,
    memory_percent REAL,
    temperature_c REAL,
    power_watts REAL,
    load_avg REAL
);
CREATE INDEX IF NOT EXISTS idx_rollup_node ON telemetry_rollup(node_id, bucket_start);
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    note TEXT
);
"""

# ---------------------------------------------------------------------------
# Migrations
#
# Every schema change ships as an ordered, append-only migration. The
# database records the highest applied version in `schema_version`; on
# startup the store applies every migration above the recorded version.
# Never edit an applied migration — add a new one.
# ---------------------------------------------------------------------------

MIGRATIONS = [
    # v1 — baseline schema (the tables created above).
    (1, "baseline schema", []),
    # v2 — flights gain scope (resource conflict keys) and retry counters.
    (2, "flight scope + retry counters", [
        "ALTER TABLE flights ADD COLUMN scope_json TEXT",
        "ALTER TABLE flights ADD COLUMN retries INTEGER DEFAULT 0",
        "ALTER TABLE flights ADD COLUMN max_retries INTEGER DEFAULT 2",
    ]),
    # v3 — containers are tracked so the Tower can prove it owns a process
    # before it sends a signal.
    (3, "container registry", [
        """CREATE TABLE IF NOT EXISTS containers (
               container_id TEXT PRIMARY KEY,
               flight_id TEXT,
               node_id TEXT,
               status TEXT,
               metadata_json TEXT,
               last_heartbeat TEXT
           )""",
    ]),
    # v4 — telemetry records where the sample came from, so a simulated
    # reading is never mistaken for a measured one.
    (4, "telemetry provenance", [
        "ALTER TABLE telemetry ADD COLUMN source TEXT",
        "ALTER TABLE telemetry ADD COLUMN source_note TEXT",
    ]),
    # v5 — rolled-up telemetry so a month of mesh history stays small.
    (5, "telemetry rollup", [
        """CREATE TABLE IF NOT EXISTS telemetry_rollup (
               node_id TEXT,
               bucket_start TEXT,
               bucket_seconds INTEGER,
               samples INTEGER,
               cpu_percent REAL,
               memory_percent REAL,
               temperature_c REAL,
               power_watts REAL,
               load_avg REAL
           )""",
        "CREATE INDEX IF NOT EXISTS idx_rollup_node ON telemetry_rollup(node_id, bucket_start)",
    ]),
    # v6 — a draining flight records the deadline it has to land by.
    (6, "flight drain deadline", [
        "ALTER TABLE flights ADD COLUMN drain_until TEXT",
    ]),
]

# The reverse of each migration, applied newest-first when rolling back.
# A migration without a rollback is irreversible: it must say so.
ROLLBACKS = {
    2: [
        "ALTER TABLE flights DROP COLUMN scope_json",
        "ALTER TABLE flights DROP COLUMN retries",
        "ALTER TABLE flights DROP COLUMN max_retries",
    ],
    3: ["DROP TABLE IF EXISTS containers"],
    4: [
        "ALTER TABLE telemetry DROP COLUMN source",
        "ALTER TABLE telemetry DROP COLUMN source_note",
    ],
    5: [
        "DROP INDEX IF EXISTS idx_rollup_node",
        "DROP TABLE IF EXISTS telemetry_rollup",
    ],
    6: [
        "ALTER TABLE flights DROP COLUMN drain_until",
    ],
}


def _table_columns(conn: sqlite3.Connection, table: str):
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {r[1] for r in rows}


class Storage:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # The Tower may be constructed on one thread and served on another
        # (uvicorn workers, an embedding app, the test client). SQLite forbids
        # crossing threads by default, so the connection is shared explicitly
        # and every access is serialised through one lock.
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        # NORMAL keeps the WAL crash-safe against application crashes while
        # avoiding an fsync on every commit. The Tower is an advisory control
        # plane: losing the last few telemetry samples to an OS crash is
        # acceptable, losing the audit chain is not, and the chain is
        # re-verified on startup.
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self.apply_migrations()

    # ---- migrations ----
    def apply_migrations(self) -> int:
        """Apply every migration above the recorded schema version."""
        with self._lock:
            row = self.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
            current = row[0] or 0
            applied = 0
            for version, note, statements in MIGRATIONS:
                if version <= current:
                    continue
                for stmt in statements:
                    # make ALTER TABLE idempotent for databases that already
                    # carry the column (e.g. created from a newer SCHEMA)
                    if stmt.startswith("ALTER TABLE"):
                        table, col = stmt.split()[2], stmt.split()[-1]
                        if col in _table_columns(self.conn, table):
                            continue
                    try:
                        self.conn.execute(stmt)
                    except sqlite3.OperationalError as e:
                        if "duplicate column" in str(e).lower():
                            continue
                        raise
                self.conn.execute(
                    "INSERT OR REPLACE INTO schema_version(version, applied_at, note) VALUES (?,?,?)",
                    (version, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), note),
                )
                self.conn.commit()
                applied += 1
            return applied

    def schema_version(self) -> int:
        with self._lock:
            row = self.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
            return row[0] or 0

    def rollback_migration(self, version: Optional[int] = None) -> int:
        """Roll the schema back to `version` (or one step, if None).

        Returns the number of migrations rolled back. A migration without a
        registered rollback is refused rather than half-applied.
        """
        with self._lock:
            current = self.schema_version()
            target = current - 1 if version is None else version
            if target >= current:
                return 0
            applied = 0
            for v in range(current, target, -1):
                statements = ROLLBACKS.get(v)
                if statements is None:
                    raise RuntimeError(
                        f"migration {v} has no rollback; refusing to roll back partially")
                for stmt in statements:
                    try:
                        self.conn.execute(stmt)
                    except sqlite3.OperationalError as e:
                        if "no such column" in str(e).lower() or \
                           "no such table" in str(e).lower():
                            continue
                        raise
                self.conn.execute("DELETE FROM schema_version WHERE version=?", (v,))
                self.conn.commit()
                applied += 1
            return applied

    # ---- flights ----
    @_serialised
    def upsert_flight(self, f: Flight):
        # Named columns (not positional) so migrations can add columns
        # without breaking this statement.
        self.conn.execute(
            """INSERT INTO flights
               (id, callsign, task, requester, priority, weight_json, container_id,
                preferred_node, status, submitted_at, cleared_at, takeoff_at, landed_at,
                shot_down_at, assigned_node, reason, rogue, metadata_json, scope_json,
                retries, max_retries, drain_until)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
               callsign=excluded.callsign, task=excluded.task, requester=excluded.requester,
               priority=excluded.priority, weight_json=excluded.weight_json,
               container_id=excluded.container_id, preferred_node=excluded.preferred_node,
               status=excluded.status, submitted_at=excluded.submitted_at,
               cleared_at=excluded.cleared_at, takeoff_at=excluded.takeoff_at,
               landed_at=excluded.landed_at, shot_down_at=excluded.shot_down_at,
               assigned_node=excluded.assigned_node, reason=excluded.reason,
               rogue=excluded.rogue, metadata_json=excluded.metadata_json,
               scope_json=excluded.scope_json, retries=excluded.retries,
               max_retries=excluded.max_retries, drain_until=excluded.drain_until""",
            (
                f.id, f.callsign, f.task, f.requester, int(f.priority), f.weight.model_dump_json(),
                f.container_id, f.preferred_node, f.status.value,
                f.submitted_at.isoformat(), f.cleared_at.isoformat() if f.cleared_at else None,
                f.takeoff_at.isoformat() if f.takeoff_at else None,
                f.landed_at.isoformat() if f.landed_at else None,
                f.shot_down_at.isoformat() if f.shot_down_at else None,
                f.assigned_node, f.reason, 1 if f.rogue else 0, json.dumps(f.metadata),
                json.dumps(f.scope), f.retries, f.max_retries,
                f.drain_until.isoformat() if f.drain_until else None,
            ),
        )
        self.conn.commit()
        # audit trail: record every flight state transition in the hash chain
        try:
            self.append_audit({
                "kind": "flight_state",
                "flight_id": f.id,
                "callsign": f.callsign,
                "status": f.status.value if hasattr(f.status, "value") else str(f.status),
                "assigned_node": f.assigned_node,
                "reason": f.reason,
                "priority": int(f.priority),
                "rogue": f.rogue,
            })
        except Exception:
            pass

    @_serialised
    def load_flights(self) -> List[Flight]:
        rows = self.conn.execute("SELECT * FROM flights").fetchall()
        out = []
        for r in rows:
            f = Flight(
                id=r["id"],
                callsign=r["callsign"] or "",
                task=r["task"],
                requester=r["requester"] or "api",
                priority=r["priority"] if r["priority"] is not None else 2,
                weight=__import__("json").loads(r["weight_json"]) if r["weight_json"] else {},
                container_id=r["container_id"],
                preferred_node=r["preferred_node"],
                status=r["status"],
                submitted_at=r["submitted_at"],
                cleared_at=r["cleared_at"],
                takeoff_at=r["takeoff_at"],
                landed_at=r["landed_at"],
                shot_down_at=r["shot_down_at"],
                assigned_node=r["assigned_node"],
                reason=r["reason"] or "",
                rogue=bool(r["rogue"]),
                metadata=json.loads(r["metadata_json"]) if r["metadata_json"] else {},
                scope=json.loads(r["scope_json"]) if r["scope_json"] else [],
                retries=r["retries"] or 0,
                max_retries=r["max_retries"] if r["max_retries"] is not None else 2,
                drain_until=r["drain_until"] if "drain_until" in r.keys() else None,
            )
            out.append(f)
        return out

    @_serialised
    def delete_flight(self, flight_id: str):
        self.conn.execute("DELETE FROM flights WHERE id=?", (flight_id,))
        self.conn.commit()

    # ---- events ----
    @_serialised
    def append_event(self, e: TowerEvent):
        self.conn.execute(
            "INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?,?)",
            (e.id, e.kind.value, e.timestamp.isoformat(), e.node_id, e.flight_id,
             e.container_id, e.message, e.severity, json.dumps(e.data)),
        )
        self.conn.commit()

    @_serialised
    def load_events(self, limit: int = 500) -> List[TowerEvent]:
        rows = self.conn.execute(
            "SELECT * FROM events ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            out.append(TowerEvent(
                id=r["id"], kind=r["kind"], timestamp=r["timestamp"],
                node_id=r["node_id"], flight_id=r["flight_id"], container_id=r["container_id"],
                message=r["message"], severity=r["severity"],
                data=json.loads(r["data_json"]) if r["data_json"] else {},
            ))
        out.reverse()
        return out

    # ---- telemetry ----
    @_serialised
    def append_telemetry(self, t: Telemetry):
        self.conn.execute(
            """INSERT INTO telemetry
               (node_id, timestamp, cpu_percent, memory_percent, memory_used_gb,
                memory_total_gb, temperature_c, power_watts, bandwidth_mbps, load_avg,
                gpu_percent, thermal_state, battery_percent, solar_input_watts,
                source, source_note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (t.node_id, t.timestamp.isoformat(), t.cpu_percent, t.memory_percent,
             t.memory_used_gb, t.memory_total_gb, t.temperature_c, t.power_watts,
             t.bandwidth_mbps, t.load_avg, t.gpu_percent, t.thermal_state.value,
             t.battery_percent, t.solar_input_watts, t.source, t.source_note),
        )
        self.conn.commit()

    @_serialised
    def load_telemetry(self, node_id: str, limit: int = 60) -> List[Telemetry]:
        rows = self.conn.execute(
            "SELECT * FROM telemetry WHERE node_id=? ORDER BY timestamp DESC LIMIT ?",
            (node_id, limit),
        ).fetchall()
        out = []
        for r in rows:
            out.append(Telemetry(
                node_id=r["node_id"], timestamp=r["timestamp"],
                cpu_percent=r["cpu_percent"], memory_percent=r["memory_percent"],
                memory_used_gb=r["memory_used_gb"], memory_total_gb=r["memory_total_gb"],
                temperature_c=r["temperature_c"], power_watts=r["power_watts"],
                bandwidth_mbps=r["bandwidth_mbps"], load_avg=r["load_avg"],
                gpu_percent=r["gpu_percent"], thermal_state=r["thermal_state"],
                battery_percent=r["battery_percent"], solar_input_watts=r["solar_input_watts"],
                source=r["source"] or "simulated" if "source" in r.keys() else "simulated",
                source_note=r["source_note"] if "source_note" in r.keys() else None,
            ))
        out.reverse()
        return out

    # ---- retention / rollup ----
    @_serialised
    def rollup_telemetry(self, now: Optional[float] = None) -> Dict[str, int]:
        """Roll raw telemetry up into 1-minute, 1-hour and 1-day summaries.

        Raw 5-second samples are only useful for the recent past. Older
        samples are averaged into progressively coarser buckets, so a month
        of mesh history costs kilobytes instead of gigabytes.
        """
        now = now if now is not None else time.time()
        counts = {"rolled": 0, "deleted": 0}
        boundaries = [
            (60.0, 60.0, "1m"),        # keep 1 minute of raw samples
            (3600.0, 300.0, "5m"),     # keep 1 hour as 5-minute averages
            (86400.0, 3600.0, "1h"),   # keep 1 day as hourly averages
            (2592000.0, 86400.0, "1d"), # keep 1 month as daily averages
        ]
        for raw_keep, bucket, label in boundaries:
            cutoff = now - raw_keep
            rows = self.conn.execute(
                "SELECT node_id, timestamp, cpu_percent, memory_percent, temperature_c, "
                "power_watts, load_avg FROM telemetry WHERE timestamp < ?",
                (time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(cutoff)),),
            ).fetchall()
            if not rows:
                continue
            buckets: Dict[Any, List[Any]] = {}
            for r in rows:
                ts = r["timestamp"]
                try:
                    epoch = datetime.fromisoformat(ts).timestamp()
                except ValueError:
                    epoch = now
                key = (r["node_id"], int(epoch // bucket) * bucket)
                buckets.setdefault(key, []).append(r)
            for (node_id, bucket_start), group in buckets.items():
                n = len(group)
                def avg(field):
                    vals = [g[field] for g in group if g[field] is not None]
                    return sum(vals) / len(vals) if vals else None
                stamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(bucket_start))
                self.conn.execute(
                    """INSERT INTO telemetry_rollup
                       (node_id, bucket_start, bucket_seconds, samples, cpu_percent,
                        memory_percent, temperature_c, power_watts, load_avg)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (node_id, stamp, int(bucket), n, avg("cpu_percent"),
                     avg("memory_percent"), avg("temperature_c"), avg("power_watts"),
                     avg("load_avg")),
                )
                counts["rolled"] += n
            self.conn.execute(
                "DELETE FROM telemetry WHERE timestamp < ?",
                (time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(cutoff)),),
            )
            counts["deleted"] += len(rows)
        self.conn.commit()
        return counts

    @_serialised
    def load_rollup(self, node_id: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
        if node_id:
            rows = self.conn.execute(
                "SELECT * FROM telemetry_rollup WHERE node_id=? ORDER BY bucket_start DESC LIMIT ?",
                (node_id, limit)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM telemetry_rollup ORDER BY bucket_start DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---- containers ----
    @_serialised
    def upsert_container(self, info: Dict[str, Any]):
        self.conn.execute(
            """INSERT INTO containers
               (container_id, flight_id, node_id, status, metadata_json, last_heartbeat)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(container_id) DO UPDATE SET
               flight_id=excluded.flight_id, node_id=excluded.node_id,
               status=excluded.status, metadata_json=excluded.metadata_json,
               last_heartbeat=excluded.last_heartbeat""",
            (info["container_id"], info.get("flight_id"), info.get("node_id"),
             info.get("status", "running"), json.dumps(info.get("metadata", {})),
             info["last_heartbeat"].isoformat() if hasattr(info.get("last_heartbeat"), "isoformat")
             else info.get("last_heartbeat")),
        )
        self.conn.commit()

    @_serialised
    def load_containers(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM containers").fetchall()
        out = []
        for r in rows:
            out.append({
                "container_id": r["container_id"],
                "flight_id": r["flight_id"],
                "node_id": r["node_id"],
                "status": r["status"] or "running",
                "metadata": json.loads(r["metadata_json"]) if r["metadata_json"] else {},
                "last_heartbeat": r["last_heartbeat"],
            })
        return out

    # ---- nodes ----
    @_serialised
    def upsert_node_spec(self, node_id: str, spec_json: str):
        self.conn.execute(
            "INSERT INTO nodes(id, spec_json) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET spec_json=excluded.spec_json",
            (node_id, spec_json),
        )
        self.conn.commit()

    @_serialised
    def load_node_specs(self) -> Dict[str, str]:
        rows = self.conn.execute("SELECT id, spec_json FROM nodes").fetchall()
        return {r["id"]: r["spec_json"] for r in rows}

    # ---- audit hash chain ----
    @_serialised
    def append_audit(self, payload: Dict[str, Any]) -> str:
        import hashlib
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        content = json.dumps(payload, sort_keys=True)
        h = hashlib.sha256(content.encode()).hexdigest()
        row = self.conn.execute(
            "SELECT content_hash FROM audit ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        prev = row["content_hash"] if row else None
        aid = f"audit-{time.time_ns()}"
        self.conn.execute(
            "INSERT INTO audit VALUES (?,?,?,?,?)",
            (aid, ts, h, prev, content),
        )
        self.conn.commit()
        return aid

    @_serialised
    def load_decisions(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Clearance decisions, newest first, with their recorded snapshots."""
        rows = self.conn.execute(
            "SELECT payload_json FROM audit WHERE payload_json LIKE '%\"kind\": \"clearance\"%' "
            "OR payload_json LIKE '%\"kind\":\"clearance\"%' ORDER BY rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            try:
                out.append(json.loads(r["payload_json"]))
            except Exception:
                continue
        return out

    @_serialised
    def verify_audit_chain(self) -> bool:
        rows = self.conn.execute(
            "SELECT content_hash, previous_hash FROM audit ORDER BY rowid"
        ).fetchall()
        prev = None
        for r in rows:
            if r["previous_hash"] != prev:
                return False
            prev = r["content_hash"]
        return True

    @_serialised
    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass
