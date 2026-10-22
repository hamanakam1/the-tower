"""A decision that cannot be re-derived is not trustworthy.

Every clearance decision - cleared, queued or denied - must be replayable
from the Tower's own record, without the caller having to reconstruct the
flight by hand.
"""
import asyncio

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


@pytest.fixture
def client(tmp_path):
    tower = Tower(db_path=str(tmp_path / "tower.sqlite"))
    asyncio.run(tower.start())
    _quiet(tower)
    c = TestClient(TowerServer(tower).app)
    yield c
    asyncio.run(tower.stop())


def test_a_cleared_decision_can_be_replayed(client):
    r = client.post("/clearance", json={
        "task": "train the adapter", "scope": ["model/lora"], "priority": 1,
        "weight": {"cpu_cores": 1, "memory_gb": 1, "duration_est_s": 600}})
    assert r.status_code == 200
    flight_id = r.json()["flight_id"]

    replay = client.get(f"/replay/flight/{flight_id}")
    assert replay.status_code == 200, replay.text
    body = replay.json()
    assert body["replayed"]["decision"] == r.json()["decision"]
    assert body["comparison"]["matches"] is True, body["comparison"]


def test_a_queued_decision_can_be_replayed(client):
    # take every node out of service so nothing can be scheduled
    for node_id in ("m3max", "pi-cluster", "trailer-hpc"):
        client.post(f"/nodes/{node_id}/state", json={"state": "maintenance"})
    r = client.post("/clearance", json={
        "task": "cannot run anywhere",
        "weight": {"cpu_cores": 64, "memory_gb": 512, "duration_est_s": 600}})
    assert r.json()["decision"] == "queued"
    flight_id = r.json()["flight_id"]

    replay = client.get(f"/replay/flight/{flight_id}")
    assert replay.status_code == 200, replay.text
    body = replay.json()
    assert body["replayed"]["decision"] == "queued"
    assert body["comparison"]["matches"] is True, body["comparison"]


def test_the_record_carries_the_flight_and_the_snapshot(client):
    r = client.post("/clearance", json={
        "task": "recorded", "scope": ["x"], "priority": 0,
        "weight": {"cpu_cores": 2, "memory_gb": 4, "duration_est_s": 600}})
    flight_id = r.json()["flight_id"]
    decisions = client.get("/decisions?snapshot=1").json()["decisions"]
    record = [d for d in decisions if d.get("flight_id") == flight_id]
    assert record, "the decision was not recorded"
    entry = record[0]
    assert entry["flight"]["task"] == "recorded"
    assert entry["flight"]["weight"]["cpu_cores"] == 2
    assert entry["flight"]["scope"] == ["x"]
    assert entry["flight"]["priority"] == 0
    assert entry["snapshot"]["nodes"]


def test_an_unknown_flight_is_a_404(client):
    assert client.get("/replay/flight/flt-does-not-exist").status_code == 404
