"""The v2 clearance contract promises idempotency. That has to survive a
restart, not just a retry in the same process.
"""
import pytest
from starlette.testclient import TestClient

from the_tower.core import Tower
from the_tower.server import TowerServer


def _quiet(tower):
    from datetime import datetime, timezone

    for node in tower.nodes.values():
        node.telemetry = node.telemetry.model_copy(update={
            "cpu_percent": 5.0, "memory_percent": 10.0, "temperature_c": 40.0,
            "power_watts": 10.0, "timestamp": datetime.now(timezone.utc)})
    return tower


async def _tower(tmp_path, name="tower.sqlite"):
    t = Tower(db_path=str(tmp_path / name))
    await t.start()
    _quiet(t)
    return t


def _post(client, key, task="train the model"):
    return client.post("/clearance/v2", json={
        "idempotency_key": key,
        "task": task,
        "weight": {"cpu_cores": 1, "memory_gb": 1, "duration_est_s": 600},
    })


def test_a_retry_returns_the_original_answer(tmp_path):
    import asyncio

    async def run():
        t = await _tower(tmp_path)
        app = TowerServer(t).app
        with TestClient(app) as client:
            first = _post(client, "key-1")
            assert first.status_code == 202
            second = _post(client, "key-1")
            assert second.status_code == 200
            assert second.json()["flight_id"] == first.json()["flight_id"]
            assert len(t.flights) == 1
        await t.stop()

    asyncio.run(run())


def test_idempotency_survives_a_restart(tmp_path):
    import asyncio

    async def run():
        db = str(tmp_path / "tower.sqlite")
        t = Tower(db_path=db)
        await t.start()
        _quiet(t)
        app = TowerServer(t).app
        with TestClient(app) as client:
            first = _post(client, "key-restart")
            assert first.status_code == 202
            first_flight = first.json()["flight_id"]
        await t.stop()

        # a fresh process, same database
        t2 = Tower(db_path=db)
        await t2.start()
        app2 = TowerServer(t2).app
        with TestClient(app2) as client:
            again = _post(client, "key-restart")
            assert again.status_code == 200
            assert again.json()["flight_id"] == first_flight
            # and it really is the same flight, not a second one
            assert len(t2.flights) == 1
        await t2.stop()

    asyncio.run(run())


def test_a_different_key_is_a_different_flight(tmp_path):
    import asyncio

    async def run():
        t = await _tower(tmp_path)
        app = TowerServer(t).app
        with TestClient(app) as client:
            a = _post(client, "key-a", "task a")
            b = _post(client, "key-b", "task b")
            assert a.json()["flight_id"] != b.json()["flight_id"]
            assert len(t.flights) == 2
        await t.stop()

    asyncio.run(run())


def test_the_answer_records_the_key(tmp_path):
    import asyncio

    async def run():
        t = await _tower(tmp_path)
        app = TowerServer(t).app
        with TestClient(app) as client:
            r = _post(client, "key-echo")
            assert r.json()["idempotency_key"] == "key-echo"
            flight = t.flights[r.json()["flight_id"]]
            assert flight.metadata["idempotency_key"] == "key-echo"
            assert flight.metadata["idempotency_response"]["flight_id"] == flight.id
        await t.stop()

    asyncio.run(run())


def test_a_key_whose_answer_was_lost_still_does_not_reschedule(tmp_path):
    """Regression: the key is persisted before the answer is, so a crash in
    between left a flight with the key and no stored response. A retry then
    fell through and scheduled the work a second time."""
    import asyncio

    from the_tower.schemas import Flight, FlightStatus, ResourceWeight

    async def run():
        db = str(tmp_path / "tower.sqlite")
        t = Tower(db_path=db)
        await t.start()
        _quiet(t)
        orphan = Flight(task="answered but not recorded", metadata={"idempotency_key": "key-crash"},
                        weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=60))
        orphan.status = FlightStatus.IN_FLIGHT
        t.flights[orphan.id] = orphan
        await t.persist_flight(orphan)
        await t.stop()

        t2 = Tower(db_path=db)
        await t2.start()
        app = TowerServer(t2).app
        with TestClient(app) as client:
            again = _post(client, "key-crash")
            assert again.status_code == 200
            assert again.json()["flight_id"] == orphan.id
            assert len(t2.flights) == 1
        await t2.stop()

    asyncio.run(run())
