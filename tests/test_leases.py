"""Tests for leases — time-bounded claims on node resources."""
import asyncio

import pytest

from the_tower.core import Tower
from the_tower.schemas import (
    Flight, ResourceWeight, ClearanceRequest, Lease, LeaseStatus,
)


@pytest.fixture
async def tower():
    t = Tower()
    await t.start()
    yield t
    await t.stop()


@pytest.mark.asyncio
async def test_clearance_issues_a_lease(tower):
    f = Flight(task="leased", weight=ResourceWeight(cpu_cores=2, memory_gb=4, duration_est_s=600))
    await tower.request_clearance(ClearanceRequest(flight=f))
    assert len(tower.leases) == 1
    lease = next(iter(tower.leases.values()))
    assert lease.flight_id == f.id
    assert lease.node_id == f.assigned_node
    assert lease.cpu_cores == 2
    assert lease.memory_gb == 4
    assert lease.status == LeaseStatus.ACTIVE
    assert f.metadata["lease_id"] == lease.id


@pytest.mark.asyncio
async def test_lease_renews(tower):
    f = Flight(task="leased", weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=600))
    await tower.request_clearance(ClearanceRequest(flight=f))
    lease = next(iter(tower.leases.values()))
    before = lease.expires_at
    renewed = tower.renew_lease(lease.id, ttl_s=600)
    assert renewed is not None
    assert renewed.renewals == 1
    assert renewed.expires_at > before


@pytest.mark.asyncio
async def test_expired_lease_releases_resources(tower):
    f = Flight(task="doomed", weight=ResourceWeight(cpu_cores=2, memory_gb=4, duration_est_s=600))
    await tower.request_clearance(ClearanceRequest(flight=f))
    lease = next(iter(tower.leases.values()))
    node = tower.get_node(f.assigned_node)
    assert f.id in node.active_flights

    # expire it
    lease.expires_at = lease.issued_at
    reaped = tower.reap_expired_leases()
    assert len(reaped) == 1
    assert lease.status == LeaseStatus.EXPIRED
    # the node no longer holds the flight's resources
    assert f.id not in node.active_flights
    assert f.id not in node.active_weights
    # and the flight is rescheduled rather than stranded
    assert f.status.value == "queued"


@pytest.mark.asyncio
async def test_lease_released_on_kill(tower):
    f = Flight(task="killed", container_id="c-lease",
               weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=600))
    await tower.request_clearance(ClearanceRequest(flight=f))
    lease = next(iter(tower.leases.values()))
    from the_tower.schemas import KillRequest
    await tower.kill(KillRequest(container_id="c-lease", reason="test"))
    assert lease.status == LeaseStatus.RELEASED


@pytest.mark.asyncio
async def test_lease_released_on_landing(tower):
    f = Flight(task="lands", weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=5))
    await tower.request_clearance(ClearanceRequest(flight=f))
    lease = next(iter(tower.leases.values()))
    f.takeoff_at = f.takeoff_at.replace(year=2000)
    await tower._progress_flights()
    assert f.status.value == "landed"
    assert lease.status == LeaseStatus.RELEASED


def test_lease_expiry_math():
    from datetime import datetime, timedelta, timezone
    lease = Lease(flight_id="f1", node_id="n1", ttl_s=60)
    now = datetime.now(timezone.utc)
    assert lease.remaining_s(now) <= 60
    assert lease.is_expired(now) is False
    assert lease.is_expired(now + timedelta(seconds=120)) is True
