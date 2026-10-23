"""Scope preemption must lead through ordinary admission and lease accounting."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from the_tower.core import Tower
from the_tower.replay import replay
from the_tower.schemas import (
    ClearanceDecision, ClearanceRequest, Flight, FlightPriority, FlightStatus,
    KillRequest, LeaseStatus, NodeSpec, NodeStatus, ResourceWeight,
)
from the_tower.server import TowerServer
from the_tower.storage import Storage


def make_tower(enforcement="enforce"):
    return Tower(nodes=[NodeSpec(
        id="n1", name="Node 1", role="test", specs="test", constraints="",
        cpu_cores=8, memory_gb=16, redline_temp_c=90, max_power_watts=100,
        network_capacity_mbps=1000, enforcement=enforcement,
    )])


async def admit_incumbent(tower):
    flight = Flight(task="incumbent", scope=["shared"], priority=FlightPriority.P3,
                    weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=600))
    response = await tower.request_clearance(ClearanceRequest(flight=flight))
    assert response.decision == ClearanceDecision.CLEARED
    return flight


def challenger(**kwargs):
    return Flight(task="challenger", scope=["shared"], priority=FlightPriority.P0,
                  **kwargs)


@pytest.mark.asyncio
async def test_enforced_preemption_assigns_a_node_and_replaces_lease():
    tower = make_tower()
    incumbent = await admit_incumbent(tower)
    old_lease = tower.leases[incumbent.metadata["lease_id"]]
    replacement = challenger()

    response = await tower.request_clearance(ClearanceRequest(flight=replacement))

    assert response.decision == ClearanceDecision.CLEARED
    assert response.assigned_node == "n1"
    assert response.admission["scored"]["n1"] > 0
    assert replacement.status == FlightStatus.IN_FLIGHT
    assert incumbent.status == FlightStatus.SHOT_DOWN
    assert old_lease.status == LeaseStatus.RELEASED
    new_lease = tower.leases[replacement.metadata["lease_id"]]
    assert new_lease.status == LeaseStatus.ACTIVE
    assert new_lease.node_id == response.assigned_node
    assert tower.nodes["n1"].active_flights == [replacement.id]
    assert set(tower.nodes["n1"].active_weights) == {replacement.id}


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", ["oversized", "redline", "missing_preferred"])
async def test_preemption_never_clears_without_a_safe_destination(unavailable):
    tower = make_tower()
    await admit_incumbent(tower)
    options = {}
    if unavailable == "oversized":
        options["weight"] = ResourceWeight(cpu_cores=999, memory_gb=999)
    elif unavailable == "redline":
        tower.nodes["n1"].status = NodeStatus.REDLINE
    else:
        options["preferred_node"] = "missing"
    replacement = challenger(**options)

    response = await tower.request_clearance(ClearanceRequest(flight=replacement))

    assert response.decision == ClearanceDecision.QUEUED
    assert replacement.status == FlightStatus.QUEUED
    assert replacement.id in tower._queued_ids
    assert "lease_id" not in replacement.metadata
    assert all(replacement.id not in node.active_weights for node in tower.nodes.values())


@pytest.mark.asyncio
async def test_advisory_preemption_preserves_incumbent_and_queues_challenger():
    tower = make_tower(enforcement="advisory")
    incumbent = await admit_incumbent(tower)
    incumbent_lease = tower.leases[incumbent.metadata["lease_id"]]
    replacement = challenger()

    response = await tower.request_clearance(ClearanceRequest(flight=replacement))

    assert response.decision == ClearanceDecision.QUEUED
    assert replacement.status == FlightStatus.QUEUED
    assert incumbent.status == FlightStatus.IN_FLIGHT
    assert incumbent_lease.status == LeaseStatus.ACTIVE
    assert tower.nodes["n1"].active_flights == [incumbent.id]
    assert "lease_id" not in replacement.metadata
    recommendation = next(e for e in tower.events if e.data.get("recommendation") == "preempt")
    assert recommendation.node_id == "n1"


@pytest.mark.asyncio
async def test_preemption_rechecks_other_scope_owners_before_admitting():
    tower = make_tower()
    await admit_incumbent(tower)
    blocker = Flight(task="other owner", scope=["other"], priority=FlightPriority.P0)
    await tower.request_clearance(ClearanceRequest(flight=blocker))
    replacement = challenger()
    replacement.scope.append("other")

    response = await tower.request_clearance(ClearanceRequest(flight=replacement))

    assert response.decision == ClearanceDecision.QUEUED
    assert response.admission["scope_conflict"] == blocker.id
    assert blocker.status == FlightStatus.IN_FLIGHT
    assert replacement.status == FlightStatus.QUEUED
    assert "lease_id" not in replacement.metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("enforcement", ["enforce", "advisory"])
async def test_preemption_clearance_record_replays_the_final_decision(enforcement, monkeypatch):
    tower = make_tower(enforcement=enforcement)
    await admit_incumbent(tower)
    audit = AsyncMock()
    monkeypatch.setattr(tower, "log_audit_async", audit)
    replacement = challenger()

    response = await tower.request_clearance(ClearanceRequest(flight=replacement))

    record = next(call.args[0] for call in audit.call_args_list
                  if call.args[0].get("kind") == "clearance")
    replayed = replay(tower.flight_from_record(record["flight"]), record["snapshot"])
    assert replayed.decision == response.decision
    assert replayed.assigned_node == response.assigned_node
    assert replayed.to_dict() == response.admission


@pytest.mark.asyncio
@pytest.mark.parametrize("priority", [FlightPriority.P0, FlightPriority.P3])
@pytest.mark.parametrize("completion", ["landing", "deadline"])
async def test_draining_owner_keeps_scope_until_completion(priority, completion):
    tower = make_tower()
    spare = tower.nodes["n1"].spec.model_copy(update={"id": "n2", "name": "Node 2"})
    tower.register_node(spare)
    incumbent = await admit_incumbent(tower)
    incumbent.container_id = "draining-owner"
    replacement = Flight(task="wait for shared scope", scope=["shared"], priority=priority,
                         preferred_node="n2")

    await tower.kill(KillRequest(container_id=incumbent.container_id, reason="maintenance",
                                 drain_timeout_s=60))
    response = await tower.request_clearance(ClearanceRequest(flight=replacement))

    assert response.decision == ClearanceDecision.QUEUED
    assert incumbent.status == FlightStatus.DRAINING
    assert tower.scope_conflict(replacement) == incumbent.id
    assert "lease_id" not in replacement.metadata
    transport = httpx.ASGITransport(app=TowerServer(tower).app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        conflicts = await client.get("/conflicts", params={"scope": "shared,unrelated"})
        assert conflicts.json()["conflicting_flights"] == [incumbent.id]

        if completion == "landing":
            incumbent.takeoff_at = datetime.now(timezone.utc) - timedelta(seconds=601)
        else:
            incumbent.drain_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        await tower._progress_flights()
        await tower._dispatch_queued()

        assert incumbent.status == (FlightStatus.LANDED if completion == "landing"
                                    else FlightStatus.SHOT_DOWN)
        assert replacement.status == FlightStatus.IN_FLIGHT
        assert replacement.assigned_node == "n2"
        assert incumbent.id not in tower._scope_index["shared"]
        conflicts = await client.get("/conflicts", params={"scope": "shared"})
        assert conflicts.json()["conflicting_flights"] == [replacement.id]


@pytest.mark.asyncio
async def test_restart_restores_draining_scope_index(tmp_path, monkeypatch):
    original = make_tower()
    incumbent = await admit_incumbent(original)
    incumbent.status = FlightStatus.DRAINING
    incumbent.drain_until = datetime.now(timezone.utc) + timedelta(seconds=60)
    db_path = str(tmp_path / "scope.sqlite")
    storage = Storage(db_path)
    storage.upsert_flight(incumbent)
    storage.close()
    restored = Tower(nodes=[original.nodes["n1"].spec], db_path=db_path)
    monkeypatch.setattr(restored.radar, "sweep_once", AsyncMock())
    monkeypatch.setattr(restored.radar, "start", AsyncMock())
    monkeypatch.setattr(restored, "_emit", Mock())
    try:
        await restored.start()
        assert restored.scope_conflict(challenger()) == incumbent.id
        assert restored.snapshot().in_flight[0].flight_id == incumbent.id
        response = await restored.request_clearance(ClearanceRequest(flight=challenger()))
        assert response.decision == ClearanceDecision.QUEUED
        assert restored.flights[incumbent.id].status == FlightStatus.DRAINING
    finally:
        await restored.stop()
        restored.storage.close()
