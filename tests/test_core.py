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
async def test_preemption(tower):
    from the_tower.schemas import Flight, ResourceWeight, ClearanceRequest, FlightPriority
    f1 = Flight(task="low prio", scope=["r"], priority=FlightPriority.P3,
                weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=100))
    await tower.request_clearance(ClearanceRequest(flight=f1))
    assert f1.status.value == "in_flight"
    f2 = Flight(task="high prio", scope=["r"], priority=FlightPriority.P0,
                weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=5))
    resp = await tower.request_clearance(ClearanceRequest(flight=f2))
    # Advisory by default: the Tower recommends the preemption but will not
    # clear the newcomer into capacity the incumbent still holds.
    assert resp.decision.value == "queued"
    assert tower.flights[f1.id].status.value == "in_flight"
    assert tower.flights[f2.id].status.value == "queued"
    assert "advisory" in resp.reason.lower()


@pytest.mark.asyncio
async def test_preemption_acts_when_the_node_is_enforced(tower):
    from the_tower.schemas import Flight, ResourceWeight, ClearanceRequest, FlightPriority
    for node in tower.nodes.values():
        node.spec.enforcement = "enforce"
    f1 = Flight(task="low prio", scope=["r"], priority=FlightPriority.P3,
                weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=100))
    await tower.request_clearance(ClearanceRequest(flight=f1))
    assert f1.status.value == "in_flight"
    f2 = Flight(task="high prio", scope=["r"], priority=FlightPriority.P0,
                weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=5))
    resp = await tower.request_clearance(ClearanceRequest(flight=f2))
    assert resp.decision.value == "cleared"
    assert tower.flights[f1.id].status.value == "shot_down"
    assert tower.flights[f2.id].status.value == "in_flight"


@pytest.mark.asyncio
async def test_container_stale_kill(tower):
    import asyncio
    tower.register_container("c-stale", node_id="m3max")
    # backdate heartbeat
    tower.containers["c-stale"]["last_heartbeat"] = __import__("datetime").datetime.now(__import__("datetime").timezone.utc) - __import__("datetime").timedelta(seconds=100)
    # trigger sweep
    await tower.radar.sweep_once()
    # sweep calls kill for stale; flight may not exist but container status? kill executed
    # ensure no crash; container marked? kill doesn't update container status; check event
    events = [e.message for e in tower.events]
    assert any("c-stale" in m for m in events)


@pytest.mark.asyncio
async def test_scope_index_tracks_in_flight_only(tower):
    from the_tower.schemas import Flight, ResourceWeight, ClearanceRequest
    f = Flight(task="indexed", scope=["k1"], weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=1))
    await tower.request_clearance(ClearanceRequest(flight=f))
    assert "k1" in tower._scope_index
    assert f.id in tower._scope_index["k1"]
    # after landing the flight leaves the index
    f.takeoff_at = f.takeoff_at.replace(year=2000)
    await tower._progress_flights()
    assert f.status.value == "landed"
    assert f.id not in tower._scope_index.get("k1", [])


@pytest.mark.asyncio
async def test_dispatch_queue_promotes_queued_flight_when_capacity_frees():
    from the_tower.core import Tower
    from the_tower.schemas import (NodeSpec, Flight, ResourceWeight,
                                   ClearanceRequest, FlightPriority, Telemetry,
                                   ThermalState)

    spec = NodeSpec(id="tiny", name="Tiny", role="test", specs="x", constraints="y",
                    cpu_cores=2.0, memory_gb=4.0, redline_temp_c=90.0,
                    max_power_watts=50.0, network_capacity_mbps=100.0)
    t = Tower(nodes=[spec])
    await t.start()
    # deterministic telemetry: nothing used yet (set after start so the
    # initial radar sweep does not overwrite it)
    t.nodes["tiny"].telemetry = Telemetry(
        node_id="tiny", cpu_percent=0.0, memory_percent=0.0, memory_used_gb=0.0,
        memory_total_gb=4.0, temperature_c=30.0, power_watts=5.0,
        bandwidth_mbps=0.0, load_avg=0.0, thermal_state=ThermalState.NOMINAL)

    hog = Flight(task="hog", weight=ResourceWeight(cpu_cores=2, memory_gb=4, duration_est_s=600))
    await t.request_clearance(ClearanceRequest(flight=hog))
    assert hog.status.value == "in_flight"

    queued = Flight(task="later", weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=5))
    await t.request_clearance(ClearanceRequest(flight=queued))
    assert queued.status.value == "queued"

    # hog lands -> queued flight is promoted
    hog.takeoff_at = hog.takeoff_at.replace(year=2000)
    await t._progress_flights()
    await t._dispatch_queued()
    assert queued.status.value == "in_flight"
    await t.stop()


@pytest.mark.asyncio
async def test_dispatch_queue_serves_highest_priority_first():
    from the_tower.core import Tower
    from the_tower.schemas import (NodeSpec, Flight, ResourceWeight,
                                   ClearanceRequest, FlightPriority, Telemetry,
                                   ThermalState)

    # one single-core slot, so exactly one of the queued flights can take off
    spec = NodeSpec(id="tiny2", name="Tiny2", role="test", specs="x", constraints="y",
                    cpu_cores=1.0, memory_gb=4.0, redline_temp_c=90.0,
                    max_power_watts=50.0, network_capacity_mbps=100.0)
    t = Tower(nodes=[spec])
    await t.start()
    t.nodes["tiny2"].telemetry = Telemetry(
        node_id="tiny2", cpu_percent=0.0, memory_percent=0.0, memory_used_gb=0.0,
        memory_total_gb=4.0, temperature_c=30.0, power_watts=5.0,
        bandwidth_mbps=0.0, load_avg=0.0, thermal_state=ThermalState.NOMINAL)

    hog = Flight(task="hog", weight=ResourceWeight(cpu_cores=1, memory_gb=4, duration_est_s=600))
    await t.request_clearance(ClearanceRequest(flight=hog))

    low = Flight(task="low", priority=FlightPriority.P3,
                 weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=5))
    high = Flight(task="high", priority=FlightPriority.P0,
                  weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=5))
    await t.request_clearance(ClearanceRequest(flight=low))
    await t.request_clearance(ClearanceRequest(flight=high))
    assert low.status.value == "queued" and high.status.value == "queued"

    # only one slot frees up -> the higher priority flight takes it
    hog.takeoff_at = hog.takeoff_at.replace(year=2000)
    await t._progress_flights()
    await t._dispatch_queued()
    assert high.status.value == "in_flight"
    assert low.status.value == "queued"
    await t.stop()


@pytest.mark.asyncio
async def test_advisory_node_does_not_auto_kill(tower):
    from the_tower.schemas import Flight, ResourceWeight, ClearanceRequest
    node = tower.get_node("m3max")
    node.spec.enforcement = "advisory"
    f = Flight(task="sticky", weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=600))
    await tower.request_clearance(ClearanceRequest(flight=f))
    assert f.status.value == "in_flight"
    # force a redline
    node.telemetry.temperature_c = node.spec.redline_temp_c + 5
    node.telemetry.thermal_state = __import__(
        "the_tower.schemas", fromlist=["ThermalState"]).ThermalState.CRITICAL
    node.history.append(node.telemetry)
    await tower._auto_kill_on_redline(node.spec.id)
    # advisory mode recommends, it does not act
    assert f.status.value == "in_flight"
    assert any("ADVISORY" in e.message for e in tower.events)


@pytest.mark.asyncio
async def test_set_enforcement(tower):
    assert tower.set_enforcement("m3max", "advisory") is True
    assert tower.get_node("m3max").spec.enforcement == "advisory"
    assert tower.set_enforcement("m3max", "bogus") is False
    assert tower.set_enforcement("nope", "advisory") is False


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


# --------------------------------------------------------------------------
# Node lifecycle states are operator intent, not telemetry
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_cordoned_node_stays_cordoned_through_a_sweep(tower):
    from the_tower.schemas import NodeStatus

    assert tower.set_node_state("pi-cluster", "cordoned") is True
    await tower.radar.sweep_once()
    assert tower.get_node("pi-cluster").status == NodeStatus.CORDONED


@pytest.mark.asyncio
async def test_telemetry_does_not_return_a_draining_node_to_service(tower):
    from the_tower.schemas import NodeStatus, Telemetry, ThermalState

    assert tower.set_node_state("m3max", "draining") is True
    node = tower.get_node("m3max")
    tower.ingest_telemetry("m3max", Telemetry(
        node_id="m3max", timestamp=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc),
        cpu_percent=5.0, memory_percent=10.0, memory_used_gb=2.0,
        memory_total_gb=128.0, temperature_c=40.0, power_watts=10.0,
        bandwidth_mbps=5.0, load_avg=1.0, thermal_state=ThermalState.NOMINAL))
    assert node.status == NodeStatus.DRAINING


@pytest.mark.asyncio
async def test_a_stale_agent_node_is_unknown_and_recovers_when_it_reports(tower):
    from datetime import datetime, timedelta, timezone

    from the_tower.schemas import NodeSpec, NodeStatus, Telemetry, ThermalState

    tower.register_node(NodeSpec(
        id="pi-01", name="Pi", role="t", specs="x", constraints="y",
        cpu_cores=4, memory_gb=8, redline_temp_c=80, max_power_watts=20,
        network_capacity_mbps=100, sampler="agent", simulate=False,
        staleness_budget_s=5))
    node = tower.get_node("pi-01")
    node.last_seen = datetime.now(timezone.utc) - timedelta(seconds=60)
    await tower.radar.sweep_once()
    assert node.status == NodeStatus.UNKNOWN

    tower.ingest_telemetry("pi-01", Telemetry(
        node_id="pi-01", timestamp=datetime.now(timezone.utc),
        cpu_percent=5.0, memory_percent=10.0, memory_used_gb=1.0,
        memory_total_gb=8.0, temperature_c=45.0, power_watts=5.0,
        bandwidth_mbps=5.0, load_avg=0.5, thermal_state=ThermalState.NOMINAL))
    assert node.status == NodeStatus.ONLINE


@pytest.mark.asyncio
async def test_a_finished_drain_returns_the_node_to_service(tower):
    from the_tower.schemas import NodeStatus

    tower.set_node_state("pi-cluster", "draining")
    await tower._progress_flights()
    assert tower.get_node("pi-cluster").status == NodeStatus.ONLINE


@pytest.mark.asyncio
async def test_a_cordoned_node_is_never_returned_to_service_automatically(tower):
    from the_tower.schemas import NodeStatus

    tower.set_node_state("pi-cluster", "cordoned")
    await tower._progress_flights()
    await tower.radar.sweep_once()
    assert tower.get_node("pi-cluster").status == NodeStatus.CORDONED
