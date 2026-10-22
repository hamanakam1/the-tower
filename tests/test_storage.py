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
