"""Operator intent survives observations, configuration changes, and restart."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import pytest
from starlette.testclient import TestClient

from the_tower.core import Tower
from the_tower.schemas import (
    ClearanceDecision, ClearanceRequest, Flight, FlightStatus, KillRequest,
    NodeSpec, NodeStatus, ResourceWeight, Telemetry, ThermalState,
)
from the_tower.server import TowerServer
from the_tower.storage import Storage


HOLDS = [NodeStatus.DRAINING, NodeStatus.CORDONED, NodeStatus.MAINTENANCE, NodeStatus.OFFLINE]


def sample(**updates):
    return Telemetry(
        node_id="n1", cpu_percent=5, memory_percent=5, memory_used_gb=1,
        memory_total_gb=16, temperature_c=40, power_watts=5,
        bandwidth_mbps=0, load_avg=0,
    ).model_copy(update=updates)


def make_tower(monkeypatch, tmp_path=None, sampler="simulated"):
    tower = Tower(nodes=[NodeSpec(
        id="n1", name="Synthetic", role="test", specs="test", constraints="",
        cpu_cores=8, memory_gb=16, redline_temp_c=90, max_power_watts=100,
        network_capacity_mbps=1000, sampler=sampler,
    )], db_path=str(tmp_path / "tower.sqlite") if tmp_path else None)
    monkeypatch.setattr(tower, "_emit", Mock())
    monkeypatch.setattr(tower, "log_audit", Mock())
    monkeypatch.setattr(tower.radar, "start", AsyncMock())
    monkeypatch.setattr(tower.radar, "_sample", Mock(side_effect=lambda *args: sample()))
    tower.radar._subscribers.clear()
    return tower


def flight():
    return Flight(task="synthetic", container_id="synthetic-container", preferred_node="n1",
                  weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=3600))


@pytest.mark.parametrize("state", HOLDS)
@pytest.mark.parametrize("sampler", ["simulated", "local", "agent"])
async def test_observations_never_clear_operator_hold(monkeypatch, state, sampler):
    tower = make_tower(monkeypatch, sampler=sampler)
    assert tower.set_node_state("n1", state.value)
    node = tower.nodes["n1"]
    node.last_seen = datetime.now(timezone.utc) - timedelta(hours=1)
    await tower.radar.sweep_once()
    assert node.status == state
    assert tower.ingest_telemetry("n1", sample())
    assert node.status == state
    assert node.operator_state == state
    result = await tower.request_clearance(ClearanceRequest(flight=flight()))
    assert result.decision == ClearanceDecision.QUEUED


@pytest.mark.parametrize("state", HOLDS)
async def test_hold_survives_restart_with_explicit_configuration(tmp_path, monkeypatch, state):
    original = make_tower(monkeypatch, tmp_path)
    original.set_node_state("n1", state.value)
    original.storage.close()
    restored = make_tower(monkeypatch, tmp_path)
    await restored.start()
    assert restored.nodes["n1"].operator_state == state
    assert restored.nodes["n1"].status == state
    result = await restored.request_clearance(ClearanceRequest(flight=flight()))
    assert result.decision == ClearanceDecision.QUEUED
    restored.storage.close()


@pytest.mark.parametrize("state", list(NodeStatus))
def test_existing_explicit_state_values_remain_supported(monkeypatch, state):
    tower = make_tower(monkeypatch)
    assert tower.set_node_state("n1", state.value)
    assert tower.nodes["n1"].operator_state == state
    assert tower.nodes["n1"].status == state


@pytest.mark.parametrize("unsafe", ["stale", "missing", "thermal", "temperature"])
def test_explicit_online_does_not_override_unsafe_telemetry(monkeypatch, unsafe):
    tower = make_tower(monkeypatch, sampler="agent")
    node = tower.nodes["n1"]
    tower.set_node_state("n1", "maintenance")
    if unsafe != "missing":
        reading = sample()
        if unsafe == "thermal":
            reading.thermal_state = ThermalState.CRITICAL
        if unsafe == "temperature":
            reading.temperature_c = 95
        tower.ingest_telemetry("n1", reading)
    if unsafe == "stale":
        node.last_seen = datetime.now(timezone.utc) - timedelta(hours=1)
    assert tower.set_node_state("n1", "online")
    assert node.operator_state == NodeStatus.ONLINE
    assert node.status == (NodeStatus.UNKNOWN if unsafe in ("stale", "missing") else NodeStatus.REDLINE)
    assert not node.can_accept(ResourceWeight())
    tower.ingest_telemetry("n1", sample())
    assert node.status == NodeStatus.ONLINE


@pytest.mark.parametrize("explicit_resume", [False, True])
async def test_kill_drain_and_explicit_resume_survive_restart(tmp_path, monkeypatch, explicit_resume):
    original = make_tower(monkeypatch, tmp_path)
    draining = flight()
    await original.request_clearance(ClearanceRequest(flight=draining))
    await original.kill(KillRequest(container_id=draining.container_id, reason="maintenance",
                                    drain_first=True, drain_timeout_s=120))
    assert original.nodes["n1"].operator_state == NodeStatus.DRAINING
    if explicit_resume:
        original.set_node_state("n1", "online")
    original.storage.close()
    restored = make_tower(monkeypatch, tmp_path)
    await restored.start()
    assert restored.flights[draining.id].status == FlightStatus.DRAINING
    assert restored.flights[draining.id].drain_until == draining.drain_until
    assert restored.nodes["n1"].operator_state == (
        NodeStatus.ONLINE if explicit_resume else NodeStatus.DRAINING)
    result = await restored.request_clearance(ClearanceRequest(flight=flight()))
    assert result.decision == (ClearanceDecision.CLEARED if explicit_resume else ClearanceDecision.QUEUED)
    restored.storage.close()


async def test_legacy_draining_flight_gets_persistent_conservative_hold(tmp_path, monkeypatch):
    original = make_tower(monkeypatch, tmp_path)
    draining = flight()
    draining.status = FlightStatus.DRAINING
    draining.assigned_node = "n1"
    draining.takeoff_at = datetime.now(timezone.utc)
    draining.drain_until = datetime.now(timezone.utc) + timedelta(minutes=5)
    original.storage.upsert_flight(draining)
    original.storage.close()
    restored = make_tower(monkeypatch, tmp_path)
    await restored.start()
    assert restored.nodes["n1"].status == NodeStatus.DRAINING
    assert restored.storage.load_node_states() == {"n1": NodeStatus.DRAINING}
    await restored.radar.sweep_once()
    assert restored.nodes["n1"].status == NodeStatus.DRAINING
    restored.storage.close()


@pytest.mark.parametrize("prior", [NodeStatus.OVERLOADED, NodeStatus.MAINTENANCE])
async def test_drain_sets_hold_without_weakening_stronger_operator_hold(tmp_path, monkeypatch, prior):
    tower = make_tower(monkeypatch, tmp_path)
    running = flight()
    await tower.request_clearance(ClearanceRequest(flight=running))
    tower.set_node_state("n1", prior.value)
    await tower.kill(KillRequest(container_id=running.container_id, reason="maintenance",
                                drain_first=True, drain_timeout_s=120))
    assert tower.nodes["n1"].operator_state == (
        NodeStatus.MAINTENANCE if prior == NodeStatus.MAINTENANCE else NodeStatus.DRAINING)
    tower.storage.close()


@pytest.mark.parametrize("initial,requested", [(None, "maintenance"), ("cordoned", "online")])
def test_failed_persistence_does_not_publish_state(tmp_path, monkeypatch, initial, requested):
    tower = make_tower(monkeypatch, tmp_path)
    if initial:
        tower.set_node_state("n1", initial)
    before = tower.nodes["n1"].model_dump()
    stored = tower.storage.load_node_states()
    monkeypatch.setattr(tower.storage, "upsert_node_state", Mock(side_effect=RuntimeError("write failed")))
    with pytest.raises(RuntimeError, match="write failed"):
        tower.set_node_state("n1", requested)
    assert tower.nodes["n1"].model_dump() == before
    assert tower.storage.load_node_states() == stored
    tower.storage.close()


@pytest.mark.parametrize("raw", ["{}", "[]", "null", "{", '{"state":null}', '{"state":[]}', '{"state":false}'])
def test_missing_or_malformed_state_never_resumes_a_node(monkeypatch, raw):
    tower = make_tower(monkeypatch)
    tower.set_node_state("n1", "cordoned")
    with TestClient(TowerServer(tower).app, raise_server_exceptions=False) as client:
        response = client.post("/nodes/n1/state", content=raw,
                               headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert tower.nodes["n1"].operator_state == NodeStatus.CORDONED
    assert tower.nodes["n1"].status == NodeStatus.CORDONED


def test_registration_preserves_previous_hold(tmp_path, monkeypatch):
    tower = make_tower(monkeypatch, tmp_path)
    spec = tower.nodes["n1"].spec
    tower.set_node_state("n1", "offline")
    assert tower.unregister_node("n1")
    node = tower.register_node(spec)
    assert node.operator_state == NodeStatus.OFFLINE
    assert node.status == NodeStatus.OFFLINE
    tower.storage.close()


async def test_online_tombstone_does_not_revive_stale_agent_after_restart(tmp_path, monkeypatch):
    original = make_tower(monkeypatch, tmp_path, sampler="agent")
    stale = sample(timestamp=datetime.now(timezone.utc) - timedelta(hours=1))
    original.ingest_telemetry("n1", stale)
    original.storage.append_telemetry(stale)
    original.set_node_state("n1", "online")
    original.storage.close()
    restored = make_tower(monkeypatch, tmp_path, sampler="agent")
    await restored.start()
    node = restored.nodes["n1"]
    assert node.operator_state == NodeStatus.ONLINE
    assert node.last_seen == stale.timestamp
    assert node.status == NodeStatus.UNKNOWN
    denied = await restored.request_clearance(ClearanceRequest(flight=flight()))
    assert denied.decision == ClearanceDecision.QUEUED
    restored.ingest_telemetry("n1", sample())
    assert node.status == NodeStatus.ONLINE
    restored.storage.close()


async def test_invalid_persisted_state_aborts_before_radar(tmp_path, monkeypatch):
    tower = make_tower(monkeypatch, tmp_path)
    tower.storage.conn.execute("INSERT INTO node_states VALUES ('n1', 'invalid')")
    tower.storage.conn.commit()
    with pytest.raises(ValueError):
        await tower.start()
    tower.radar._sample.assert_not_called()
    tower.radar.start.assert_not_awaited()
    tower.storage.close()


def test_node_state_migration_round_trip_and_spec_independence(tmp_path):
    storage = Storage(str(tmp_path / "tower.sqlite"))
    storage.upsert_node_state("n1", NodeStatus.MAINTENANCE)
    storage.upsert_node_spec("n1", '{}')
    assert storage.load_node_states() == {"n1": NodeStatus.MAINTENANCE}
    storage.upsert_node_state("n1", NodeStatus.ONLINE)
    assert storage.load_node_states() == {"n1": NodeStatus.ONLINE}
    assert storage.schema_version() == 8
    assert storage.rollback_migration(7) == 1
    assert storage.apply_migrations() == 1
    assert storage.load_node_states() == {}
    storage.close()
