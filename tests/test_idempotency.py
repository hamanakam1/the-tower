"""The v2 clearance contract promises idempotency. That has to survive a
restart, not just a retry in the same process.
"""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from the_tower.core import Tower
from the_tower.schemas import Flight
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


def _request(body):
    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode()}

    return Request({"type": "http", "method": "POST", "path": "/clearance/v2"}, receive)


def _endpoint(server):
    return next(route.endpoint for route in server.app.routes if route.path == "/clearance/v2")


async def test_concurrent_retries_admit_one_flight(monkeypatch):
    tower = _quiet(Tower())
    endpoint = _endpoint(TowerServer(tower))
    original = tower.request_clearance
    calls = 0

    async def yield_before_admission(req):
        nonlocal calls
        calls += 1
        # Let every concurrent handler reach the lookup before admission.
        # No wall-clock timing or actual process execution is involved.
        await asyncio.sleep(0)
        return await original(req)

    monkeypatch.setattr(tower, "request_clearance", yield_before_admission)
    responses = await asyncio.gather(*[
        endpoint(_request({"idempotency_key": "concurrent", "task": "synthetic"}))
        for _ in range(5)
    ])
    payloads = [json.loads(response.body) for response in responses]
    assert calls == 1
    assert len(tower.flights) == 1
    assert len({payload["flight_id"] for payload in payloads}) == 1
    assert [response.status_code for response in responses] == [202, 200, 200, 200, 200]
    assert all(payload == payloads[0] for payload in payloads)


@pytest.mark.parametrize("stored", [None, {}, "invalid", []])
async def test_incomplete_persisted_key_refuses_duplicate_after_restart(tmp_path, monkeypatch, stored):
    db = str(tmp_path / "interrupted.sqlite")
    original = Tower(db_path=db)
    metadata = {"idempotency_key": "interrupted"}
    if stored is not None:
        metadata["idempotency_response"] = stored
    flight = Flight(task="synthetic interrupted admission", metadata=metadata)
    original.storage.upsert_flight(flight)

    restarted = Tower(db_path=db)
    monkeypatch.setattr(restarted.radar, "sweep_once", AsyncMock())
    monkeypatch.setattr(restarted.radar, "start", AsyncMock())
    await restarted.start()
    try:
        endpoint = _endpoint(TowerServer(restarted))
        response = await endpoint(_request({"idempotency_key": "interrupted", "task": "retry"}))
        assert response.status_code == 409
        payload = json.loads(response.body)
        assert payload["flight_id"] == flight.id
        assert payload["idempotency_key"] == "interrupted"
        assert "incomplete" in payload["error"]
        assert set(restarted.flights) == {flight.id}
    finally:
        await restarted.stop()


async def test_cancelled_admission_refuses_duplicate_and_releases_lock(monkeypatch):
    tower = _quiet(Tower())
    endpoint = _endpoint(TowerServer(tower))
    original = tower.request_clearance
    admitted = asyncio.Event()
    hold = asyncio.Event()

    async def interrupted(req):
        response = await original(req)
        admitted.set()
        await hold.wait()
        return response

    monkeypatch.setattr(tower, "request_clearance", interrupted)
    body = {"idempotency_key": "cancelled", "task": "synthetic"}
    pending = asyncio.create_task(endpoint(_request(body)))
    await asyncio.wait_for(admitted.wait(), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    monkeypatch.setattr(tower, "request_clearance", original)
    retry = await asyncio.wait_for(endpoint(_request(body)), 1)
    assert retry.status_code == 409
    assert len(tower.flights) == 1
    another = await asyncio.wait_for(endpoint(_request({"idempotency_key": "another"})), 1)
    assert another.status_code == 202
    assert len(tower.flights) == 2


@pytest.mark.parametrize("key", [None, "", " \t", False, True, 0, 1, [], ["key"], {}, {"key": "value"}])
def test_invalid_explicit_key_is_rejected_before_admission(key):
    tower = Tower()
    with TestClient(TowerServer(tower).app, raise_server_exceptions=False) as client:
        response = _post(client, key)
    assert response.status_code == 400
    assert tower.flights == {}


@pytest.mark.parametrize("raw", ["{", "[]", "null", '"text"', "true", "1"])
def test_invalid_json_body_is_rejected_before_admission(raw):
    tower = Tower()
    with TestClient(TowerServer(tower).app, raise_server_exceptions=False) as client:
        response = client.post("/clearance/v2", content=raw, headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert tower.flights == {}


@pytest.mark.parametrize("body", [
    {"constraints": []}, {"constraints": "node"},
    {"weight": []}, {"weight": "heavy"},
    {"priority": "urgent"}, {"priority": 99},
    {"task": {}}, {"scope": "resource"},
])
def test_invalid_clearance_fields_are_rejected_before_admission(body):
    tower = Tower()
    with TestClient(TowerServer(tower).app, raise_server_exceptions=False) as client:
        response = client.post("/clearance/v2", json={"idempotency_key": "invalid", **body})
    assert response.status_code == 400
    assert tower.flights == {}


def test_omitted_key_still_allows_independent_requests():
    tower = _quiet(Tower())
    with TestClient(TowerServer(tower).app) as client:
        first = client.post("/clearance/v2", json={"task": "synthetic"})
        second = client.post("/clearance/v2", json={"task": "synthetic"})
    assert first.status_code == second.status_code == 202
    assert first.json()["flight_id"] != second.json()["flight_id"]
    assert len(tower.flights) == 2


def test_evicted_cache_recovers_original_answer_even_if_retry_payload_changes():
    tower = _quiet(Tower())
    with TestClient(TowerServer(tower).app) as client:
        first = _post(client, "evicted", "original task")
        tower._idempotency.clear()
        retry = _post(client, "evicted", "different retry task")
    assert retry.status_code == 200
    assert retry.json() == first.json()
    assert len(tower.flights) == 1
    assert tower.flights[first.json()["flight_id"]].task == "original task"


@pytest.mark.parametrize("field,value", [
    ("decision", "invalid"),
    ("flight_id", "different-flight"),
    ("idempotency_key", "different-key"),
])
def test_invalid_saved_response_cannot_be_replayed(field, value):
    tower = _quiet(Tower())
    with TestClient(TowerServer(tower).app) as client:
        first = _post(client, "corrupt-answer")
        flight = tower.flights[first.json()["flight_id"]]
        flight.metadata["idempotency_response"] = {**first.json(), field: value}
        tower._idempotency.clear()
        retry = _post(client, "corrupt-answer")
    assert retry.status_code == 409
    assert retry.json()["flight_id"] == flight.id
    assert len(tower.flights) == 1
