"""Restart must retain reservations and the original lease deadlines."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from the_tower.core import Tower
from the_tower.schemas import (
    ClearanceDecision, ClearanceRequest, Flight, FlightStatus, KillRequest,
    Lease, LeaseStatus, NodeSpec, ResourceWeight,
)
from the_tower.storage import Storage


def make_tower(tmp_path, monkeypatch):
    tower = Tower(nodes=[NodeSpec(
        id="n1", name="Node 1", role="test", specs="test", constraints="",
        cpu_cores=8, memory_gb=16, redline_temp_c=90, max_power_watts=100,
        network_capacity_mbps=1000,
    )], db_path=str(tmp_path / "tower.sqlite"))
    monkeypatch.setattr(tower.radar, "sweep_once", AsyncMock())
    monkeypatch.setattr(tower.radar, "start", AsyncMock())
    monkeypatch.setattr(tower, "_emit", Mock())
    monkeypatch.setattr(tower, "log_audit", Mock())
    return tower


async def admit(tower):
    flight = Flight(task="long flight", container_id="test-container", scope=["work"],
                    weight=ResourceWeight(cpu_cores=4, memory_gb=8, duration_est_s=3600))
    response = await tower.request_clearance(ClearanceRequest(flight=flight))
    assert response.decision == ClearanceDecision.CLEARED
    return flight, tower.leases[flight.metadata["lease_id"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("draining", [False, True])
async def test_restart_restores_resource_reservations_and_lease(tmp_path, monkeypatch, draining):
    original = make_tower(tmp_path, monkeypatch)
    flight, lease = await admit(original)
    if draining:
        await original.kill(KillRequest(container_id=flight.container_id, reason="maintenance",
                                        drain_first=True, drain_timeout_s=120))
    original.storage.close()

    restored = make_tower(tmp_path, monkeypatch)
    await restored.start()

    node = restored.nodes["n1"]
    assert node.active_flights == [flight.id]
    assert node.active_weights == {flight.id: flight.weight}
    assert restored.leases[lease.id] == lease
    assert restored.flights[flight.id].status == flight.status
    assert restored.flights[flight.id].drain_until == flight.drain_until
    challenger = Flight(task="would overcommit", weight=flight.weight)
    result = await restored.request_clearance(ClearanceRequest(flight=challenger))
    assert result.decision == ClearanceDecision.QUEUED
    restored.storage.close()


@pytest.mark.asyncio
async def test_restart_preserves_renewed_absolute_deadline(tmp_path, monkeypatch):
    original = make_tower(tmp_path, monkeypatch)
    _, lease = await admit(original)
    original.renew_lease(lease.id, ttl_s=600)
    original.storage.close()

    restored = make_tower(tmp_path, monkeypatch)
    await restored.start()
    assert restored.leases[lease.id] == lease
    assert restored.leases[lease.id].renewals == 1
    restored.storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("completion", ["land", "kill"])
async def test_completed_flight_and_released_lease_stay_completed(tmp_path, monkeypatch, completion):
    original = make_tower(tmp_path, monkeypatch)
    flight, lease = await admit(original)
    if completion == "land":
        flight.takeoff_at = datetime.now(timezone.utc) - timedelta(hours=2)
        await original._progress_flights()
    else:
        await original.kill(KillRequest(container_id=flight.container_id, reason="finished"))
    assert lease.status == LeaseStatus.RELEASED
    original.storage.close()
    restored = make_tower(tmp_path, monkeypatch)
    await restored.start()
    assert restored.leases[lease.id].status == LeaseStatus.RELEASED
    assert restored.flights[flight.id].status == flight.status
    assert not restored.nodes["n1"].active_weights
    assert flight.id not in restored._queued_ids
    restored.storage.close()


@pytest.mark.asyncio
async def test_queued_flight_rejoins_node_queue_after_restart(tmp_path, monkeypatch):
    original = make_tower(tmp_path, monkeypatch)
    flight = Flight(task="waiting", status=FlightStatus.QUEUED, assigned_node="n1")
    original.storage.upsert_flight(flight)
    original.storage.close()
    restored = make_tower(tmp_path, monkeypatch)
    await restored.start()
    assert restored.nodes["n1"].queued_flights == [flight.id]
    assert flight.id in restored._queued_ids
    assert not restored.nodes["n1"].active_weights
    restored.storage.close()


@pytest.mark.asyncio
async def test_expired_lease_cannot_be_renewed_before_reaping(tmp_path, monkeypatch):
    tower = make_tower(tmp_path, monkeypatch)
    _, lease = await admit(tower)
    lease.expires_at = lease.issued_at
    assert tower.renew_lease(lease.id, ttl_s=600) is None
    assert lease.renewals == 0
    assert lease.expires_at == lease.issued_at
    tower.storage.close()


@pytest.mark.asyncio
async def test_expired_lease_requeues_once_across_restart(tmp_path, monkeypatch):
    original = make_tower(tmp_path, monkeypatch)
    flight, lease = await admit(original)
    lease.expires_at = lease.issued_at
    original.storage.upsert_lease(lease)
    original.storage.close()

    restored = make_tower(tmp_path, monkeypatch)
    await restored.start()
    assert len(restored.reap_expired_leases()) == 1
    assert restored.flights[flight.id].status == FlightStatus.QUEUED
    assert restored.flights[flight.id].retries == 1
    assert not restored.nodes["n1"].active_weights
    restored.storage.close()

    again = make_tower(tmp_path, monkeypatch)
    await again.start()
    assert again.reap_expired_leases() == []
    assert again.flights[flight.id].status == FlightStatus.QUEUED
    assert again.flights[flight.id].retries == 1
    assert flight.id in again._queued_ids
    assert again.leases[lease.id].status == LeaseStatus.EXPIRED
    again.storage.close()


@pytest.mark.asyncio
async def test_exhausted_lease_retry_remains_terminal_after_restart(tmp_path, monkeypatch):
    original = make_tower(tmp_path, monkeypatch)
    flight, lease = await admit(original)
    flight.retries = flight.max_retries
    lease.expires_at = lease.issued_at
    original.reap_expired_leases()
    original.storage.close()
    restored = make_tower(tmp_path, monkeypatch)
    await restored.start()
    assert restored.flights[flight.id].status == FlightStatus.SHOT_DOWN
    assert restored.leases[lease.id].status == LeaseStatus.EXPIRED
    assert flight.id not in restored._queued_ids
    assert not restored.nodes["n1"].active_weights
    restored.storage.close()


@pytest.mark.asyncio
async def test_legacy_active_flight_without_lease_still_reserves_capacity(tmp_path, monkeypatch):
    original = make_tower(tmp_path, monkeypatch)
    flight = Flight(task="legacy", status=FlightStatus.IN_FLIGHT, assigned_node="n1",
                    takeoff_at=datetime.now(timezone.utc), scope=["legacy"],
                    weight=ResourceWeight(cpu_cores=4, memory_gb=8, duration_est_s=3600),
                    metadata={"lease_id": "legacy-not-persisted"})
    original.storage.upsert_flight(flight)
    original.storage.close()
    restored = make_tower(tmp_path, monkeypatch)
    await restored.start()
    assert restored.nodes["n1"].active_weights == {flight.id: flight.weight}
    assert restored._scope_index["legacy"] == [flight.id]
    assert restored.leases == {}
    restored.storage.close()


@pytest.mark.asyncio
async def test_expired_draining_lease_keeps_reservations_until_drain_finishes(tmp_path, monkeypatch):
    tower = make_tower(tmp_path, monkeypatch)
    flight, lease = await admit(tower)
    await tower.kill(KillRequest(container_id=flight.container_id, reason="maintenance",
                                drain_first=True, drain_timeout_s=120))
    lease.expires_at = lease.issued_at
    assert tower.reap_expired_leases() == []
    assert flight.status == FlightStatus.DRAINING
    assert tower.nodes["n1"].active_weights == {flight.id: flight.weight}
    assert tower._scope_index["work"] == [flight.id]
    flight.takeoff_at = datetime.now(timezone.utc) - timedelta(hours=2)
    await tower._progress_flights()
    assert lease.status == LeaseStatus.RELEASED
    assert not tower.nodes["n1"].active_weights
    tower.storage.close()


@pytest.mark.asyncio
async def test_obsolete_lease_cannot_evict_a_new_attempt(tmp_path, monkeypatch):
    tower = make_tower(tmp_path, monkeypatch)
    flight, current = await admit(tower)
    old = Lease(flight_id=flight.id, node_id="n1")
    old.expires_at = old.issued_at
    tower.leases[old.id] = old
    tower.reap_expired_leases()
    assert old.status == LeaseStatus.EXPIRED
    assert flight.status == FlightStatus.IN_FLIGHT
    assert tower.nodes["n1"].active_weights == {flight.id: flight.weight}
    assert current.status == LeaseStatus.ACTIVE
    assert tower.renew_lease(old.id) is None
    tower.storage.close()


@pytest.mark.asyncio
async def test_landing_releases_current_lease_even_when_older_lease_remains(tmp_path, monkeypatch):
    tower = make_tower(tmp_path, monkeypatch)
    flight, old = await admit(tower)
    current = tower.issue_lease(flight, "n1")
    assert current.id != old.id
    flight.takeoff_at = datetime.now(timezone.utc) - timedelta(hours=2)
    await tower._progress_flights()
    assert flight.status == FlightStatus.LANDED
    assert current.status == LeaseStatus.RELEASED
    stored = {lease.id: lease for lease in tower.storage.load_leases()}
    assert stored[current.id].status == LeaseStatus.RELEASED
    tower.storage.close()


@pytest.mark.asyncio
async def test_expiry_write_failure_keeps_claims_and_allows_next_sweep_retry(tmp_path, monkeypatch):
    tower = make_tower(tmp_path, monkeypatch)
    flight, lease = await admit(tower)
    lease.expires_at = lease.issued_at
    writer = tower.storage._write_lease
    monkeypatch.setattr(tower.storage, "_write_lease", Mock(side_effect=RuntimeError("write failed")))
    with pytest.raises(RuntimeError, match="write failed"):
        tower.reap_expired_leases()
    assert lease.status == LeaseStatus.ACTIVE
    assert flight.status == FlightStatus.IN_FLIGHT
    assert flight.retries == 0
    assert flight.id not in tower._queued_ids
    assert tower.nodes["n1"].active_weights == {flight.id: flight.weight}
    assert tower._scope_index["work"] == [flight.id]
    assert tower.storage.load_flights()[0].status == FlightStatus.IN_FLIGHT
    monkeypatch.setattr(tower.storage, "_write_lease", writer)
    assert len(tower.reap_expired_leases()) == 1
    assert flight.status == FlightStatus.QUEUED
    assert flight.retries == 1
    assert not tower.nodes["n1"].active_weights
    tower.storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["leases", "flights"])
async def test_unreadable_recovery_state_aborts_before_radar(tmp_path, monkeypatch, state):
    tower = make_tower(tmp_path, monkeypatch)
    if state == "leases":
        tower.storage.conn.execute("INSERT INTO leases VALUES ('bad', 'not json')")
        tower.storage.conn.commit()
    else:
        monkeypatch.setattr(tower.storage, "load_flights", Mock(side_effect=ValueError("read failed")))
    with pytest.raises(ValueError):
        await tower.start()
    tower.radar.sweep_once.assert_not_awaited()
    tower.radar.start.assert_not_awaited()
    tower.storage.close()


def test_lease_migration_round_trip(tmp_path):
    storage = Storage(str(tmp_path / "tower.sqlite"))
    lease = Lease(flight_id="f1", node_id="n1")
    storage.upsert_lease(lease)
    assert storage.load_leases() == [lease]
    assert storage.schema_version() == 7
    assert storage.rollback_migration(6) == 1
    assert storage.apply_migrations() == 1
    assert storage.load_leases() == []
    storage.close()


def test_flight_and_lease_transition_rolls_back_together(tmp_path, monkeypatch):
    storage = Storage(str(tmp_path / "tower.sqlite"))
    flight = Flight(task="atomic")
    lease = Lease(flight_id=flight.id, node_id="n1")
    monkeypatch.setattr(storage, "_write_lease", Mock(side_effect=RuntimeError("write failed")))
    with pytest.raises(RuntimeError, match="write failed"):
        storage.upsert_flight(flight, lease)
    assert storage.load_flights() == []
    assert storage.load_leases() == []
    storage.close()
