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
    metadata_json TEXT
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
    solar_input_watts REAL
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
"""


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
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- flights ----
    @_serialised
    def upsert_flight(self, f: Flight):
        self.conn.execute(
            """INSERT INTO flights VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
               callsign=excluded.callsign, task=excluded.task, requester=excluded.requester,
               priority=excluded.priority, weight_json=excluded.weight_json,
               container_id=excluded.container_id, preferred_node=excluded.preferred_node,
               status=excluded.status, submitted_at=excluded.submitted_at,
               cleared_at=excluded.cleared_at, takeoff_at=excluded.takeoff_at,
               landed_at=excluded.landed_at, shot_down_at=excluded.shot_down_at,
               assigned_node=excluded.assigned_node, reason=excluded.reason,
               rogue=excluded.rogue, metadata_json=excluded.metadata_json""",
            (
                f.id, f.callsign, f.task, f.requester, int(f.priority), f.weight.model_dump_json(),
                f.container_id, f.preferred_node, f.status.value,
                f.submitted_at.isoformat(), f.cleared_at.isoformat() if f.cleared_at else None,
                f.takeoff_at.isoformat() if f.takeoff_at else None,
                f.landed_at.isoformat() if f.landed_at else None,
                f.shot_down_at.isoformat() if f.shot_down_at else None,
                f.assigned_node, f.reason, 1 if f.rogue else 0, json.dumps(f.metadata),
            ),
        )
        self.conn.commit()

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
            """INSERT INTO telemetry VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (t.node_id, t.timestamp.isoformat(), t.cpu_percent, t.memory_percent,
             t.memory_used_gb, t.memory_total_gb, t.temperature_c, t.power_watts,
             t.bandwidth_mbps, t.load_avg, t.gpu_percent, t.thermal_state.value,
             t.battery_percent, t.solar_input_watts),
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
            ))
        out.reverse()
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
