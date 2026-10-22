import asyncio
import pytest
from the_tower.core import Tower
from the_tower.schemas import Flight, ResourceWeight, FlightPriority, ClearanceDecision


@pytest.fixture
async def tower():
    t = Tower()
    await t.start()
    yield t
    await t.stop()


@pytest.mark.asyncio
async def test_clearance_granted(tower):
    flight = Flight(task="train model", weight=ResourceWeight(cpu_cores=2, memory_gb=4, duration_est_s=5))
    from the_tower.schemas import ClearanceRequest
    resp = await tower.request_clearance(ClearanceRequest(flight=flight))
    assert resp.decision == ClearanceDecision.CLEARED
    assert resp.assigned_node is not None
    assert flight.status.value == "in_flight"


@pytest.mark.asyncio
async def test_rogue_shot_down(tower):
    flight = Flight(task="rogue miner", rogue=True, weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=5))
    from the_tower.schemas import ClearanceRequest
    resp = await tower.request_clearance(ClearanceRequest(flight=flight))
    assert resp.decision == ClearanceDecision.SHOT_DOWN


@pytest.mark.asyncio
async def test_kill_flight(tower):
    flight = Flight(task="leaky job", container_id="c-123", weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=100))
    from the_tower.schemas import ClearanceRequest, KillRequest
    await tower.request_clearance(ClearanceRequest(flight=flight))
    assert flight.status.value == "in_flight"
    kr = await tower.kill(KillRequest(container_id="c-123", reason="test"))
    assert kr.success is True
    assert kr.flight_id == flight.id
    assert tower.flights[flight.id].status.value == "shot_down"


@pytest.mark.asyncio
async def test_airspace_snapshot(tower):
    snap = tower.airspace()
    assert len(snap.nodes) == 3
    assert snap.stats["nodes_total"] == 3


@pytest.mark.asyncio
async def test_overload_queued(tower):
    # hammer with huge but not impossible? use preferred node and overload by repeated
    from the_tower.schemas import ClearanceRequest
    # request many heavy flights to exhaust m3max
    for _ in range(6):
        f = Flight(task="heavy", preferred_node="m3max",
                   weight=ResourceWeight(cpu_cores=4, memory_gb=30, duration_est_s=60))
        await tower.request_clearance(ClearanceRequest(flight=f))
    # at least one should be queued or shot; ensure not all cleared
    statuses = [f.status.value for f in tower.flights.values()]
    assert "queued" in statuses or "shot_down" in statuses or "denied" in statuses
