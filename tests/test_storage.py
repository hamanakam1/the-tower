import time

import pytest

from the_tower.core import Tower
from the_tower.storage import Storage


@pytest.fixture
def storage(tmp_path):
    s = Storage(str(tmp_path / "audit.sqlite"))
    yield s
    s.close()


def test_audit_ids_are_unique_within_the_same_millisecond(storage):
    # Regression: audit ids were built from epoch milliseconds, so two entries
    # written in the same millisecond collided on the primary key and the
    # endpoint answered 500.
    ids = {storage.append_audit({"n": i}) for i in range(25)}
    assert len(ids) == 25


def test_audit_chain_verifies_after_rapid_writes(storage):
    for i in range(25):
        storage.append_audit({"n": i, "when": time.time()})
    assert storage.verify_audit_chain() is True


def test_audit_chain_detects_tampering(storage):
    storage.append_audit({"n": 1})
    storage.append_audit({"n": 2})
    rows = storage.conn.execute("SELECT id FROM audit ORDER BY rowid").fetchall()
    storage.conn.execute("UPDATE audit SET previous_hash='tampered' WHERE id=?", (rows[1]["id"],))
    storage.conn.commit()
    assert storage.verify_audit_chain() is False


def test_storage_round_trips_flights(tmp_path):
    from the_tower.schemas import Flight, ResourceWeight

    s = Storage(str(tmp_path / "flights.sqlite"))
    s.upsert_flight(Flight(task="persist me", weight=ResourceWeight(cpu_cores=2, memory_gb=4)))
    loaded = s.load_flights()
    s.close()
    assert any(f.task == "persist me" for f in loaded)


def test_migrations_are_recorded_and_idempotent(tmp_path):
    s = Storage(str(tmp_path / "m.sqlite"))
    v1 = s.schema_version()
    assert v1 >= 1
    # applying again is a no-op
    assert s.apply_migrations() == 0
    assert s.schema_version() == v1
    s.close()


def test_migration_rollback(tmp_path):
    s = Storage(str(tmp_path / "rb.sqlite"))
    top = s.schema_version()
    assert top >= 3
    rolled = s.rollback_migration(top - 1)
    assert rolled == 1
    assert s.schema_version() == top - 1
    s.close()


def test_rollback_removes_the_rollup_table(tmp_path):
    s = Storage(str(tmp_path / "rb-rollup.sqlite"))
    assert s.schema_version() >= 5
    s.rollback_migration(4)
    with pytest.raises(Exception):
        s.conn.execute("SELECT * FROM telemetry_rollup LIMIT 1").fetchall()
    s.close()


def test_drain_deadline_column_is_added_and_rolled_back(tmp_path):
    s = Storage(str(tmp_path / "drain.sqlite"))
    assert s.schema_version() >= 6
    cols = {r[1] for r in s.conn.execute("PRAGMA table_info(flights)").fetchall()}
    assert "drain_until" in cols
    s.rollback_migration(5)
    cols = {r[1] for r in s.conn.execute("PRAGMA table_info(flights)").fetchall()}
    assert "drain_until" not in cols
    # and forward again
    s.apply_migrations()
    cols = {r[1] for r in s.conn.execute("PRAGMA table_info(flights)").fetchall()}
    assert "drain_until" in cols
    s.close()


def test_drain_deadline_round_trips(tmp_path):
    from datetime import datetime, timedelta, timezone

    from the_tower.schemas import Flight, ResourceWeight

    s = Storage(str(tmp_path / "drain-rt.sqlite"))
    deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
    f = Flight(task="draining", weight=ResourceWeight(cpu_cores=1, memory_gb=1))
    f.drain_until = deadline
    s.upsert_flight(f)
    loaded = [x for x in s.load_flights() if x.task == "draining"]
    assert loaded
    assert loaded[0].drain_until is not None
    s.close()


def test_rollback_refuses_an_irreversible_migration(tmp_path):
    s = Storage(str(tmp_path / "rb2.sqlite"))
    with pytest.raises(RuntimeError):
        s.rollback_migration(0)
    s.close()


def test_rollback_then_forward_again(tmp_path):
    s = Storage(str(tmp_path / "rb3.sqlite"))
    top = s.schema_version()
    s.rollback_migration(top - 1)
    assert s.apply_migrations() == 1
    assert s.schema_version() == top
    s.close()


def test_flights_persist_scope_and_retries(tmp_path):
    from the_tower.schemas import Flight, ResourceWeight

    s = Storage(str(tmp_path / "f.sqlite"))
    f = Flight(task="scoped", scope=["a", "b"], weight=ResourceWeight(cpu_cores=1, memory_gb=1))
    s.upsert_flight(f)
    loaded = s.load_flights()
    s.close()
    match = [x for x in loaded if x.task == "scoped"]
    assert match and match[0].scope == ["a", "b"]


def test_containers_persist(tmp_path):
    from datetime import datetime, timezone

    s = Storage(str(tmp_path / "c.sqlite"))
    s.upsert_container({
        "container_id": "ctr-1", "flight_id": "flt-1", "node_id": "m3max",
        "status": "running", "metadata": {"pid": 123},
        "last_heartbeat": datetime.now(timezone.utc),
    })
    loaded = s.load_containers()
    s.close()
    assert len(loaded) == 1
    assert loaded[0]["container_id"] == "ctr-1"
    assert loaded[0]["metadata"]["pid"] == 123


def test_telemetry_rollup_keeps_history_small(tmp_path):
    from datetime import datetime, timedelta, timezone
    from the_tower.schemas import Telemetry

    s = Storage(str(tmp_path / "r.sqlite"))
    now = datetime.now(timezone.utc)
    # two hours of 5-second samples
    for i in range(1440):
        ts = now - timedelta(seconds=5 * i)
        s.append_telemetry(Telemetry(
            node_id="n1", timestamp=ts, cpu_percent=10.0, memory_percent=20.0,
            memory_used_gb=1.0, memory_total_gb=8.0, temperature_c=40.0,
            power_watts=20.0, bandwidth_mbps=1.0, load_avg=0.5,
        ))
    result = s.rollup_telemetry()
    assert result["rolled"] > 0
    rollups = s.load_rollup("n1")
    assert rollups
    # the raw table no longer holds the old samples
    remaining = s.conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0]
    assert remaining < 1440
    s.close()


@pytest.mark.asyncio
async def test_tower_audit_endpoint_accepts_rapid_writes(tmp_path):
    # Regression: the storage connection was bound to the thread that built it,
    # so serving the Tower from another thread raised inside SQLite.
    from starlette.testclient import TestClient

    from the_tower.server import TowerServer

    t = Tower(db_path=str(tmp_path / "tower.sqlite"))
    await t.start()
    app = TowerServer(t).app
    with TestClient(app) as client:
        for i in range(10):
            r = client.post("/audit", json={"n": i})
            assert r.status_code == 200, r.text
        assert client.get("/audit/verify").json() == {"valid": True}
    await t.stop()
