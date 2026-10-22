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


@pytest.mark.asyncio
async def test_scope_conflict_queues(tower):
    from the_tower.schemas import Flight, ResourceWeight, ClearanceRequest
    f1 = Flight(task="job a", scope=["res-x"], weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=100))
    await tower.request_clearance(ClearanceRequest(flight=f1))
    assert f1.status.value == "in_flight"
    f2 = Flight(task="job b", scope=["res-x"], priority=__import__("the_tower.schemas", fromlist=["FlightPriority"]).FlightPriority.P2,
                weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=5))
    resp = await tower.request_clearance(ClearanceRequest(flight=f2))
    assert resp.decision.value == "queued"


@pytest.mark.asyncio
async def test_node_registration(tower):
    from the_tower.schemas import NodeSpec
    spec = NodeSpec(id="newnode", name="New Node", role="test", specs="x", constraints="y",
                    cpu_cores=4, memory_gb=8, redline_temp_c=90, max_power_watts=50,
                    network_capacity_mbps=100)
    n = tower.register_node(spec)
    assert n.spec.id == "newnode"
    assert "newnode" in tower.nodes
    assert tower.unregister_node("newnode") is True
    assert "newnode" not in tower.nodes
