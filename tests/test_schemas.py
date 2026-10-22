import pytest
from the_tower.schemas import (
    Node, NodeSpec, Telemetry, ThermalState, NodeStatus,
    Flight, FlightStatus, ResourceWeight, FlightPriority,
    ClearanceRequest, ClearanceDecision, default_nodes,
)


def test_default_nodes():
    specs = default_nodes()
    assert len(specs) == 3
    ids = {s.id for s in specs}
    assert ids == {"m3max", "pi-cluster", "trailer-hpc"}


def test_node_can_accept():
    spec = default_nodes()[0]
    from the_tower.schemas import Telemetry, Node
    t = Telemetry(node_id=spec.id, cpu_percent=10, memory_percent=20,
                  memory_used_gb=spec.memory_gb*0.2, memory_total_gb=spec.memory_gb,
                  temperature_c=40, power_watts=20, bandwidth_mbps=0, load_avg=0.5,
                  thermal_state=ThermalState.NOMINAL)
    node = Node(spec=spec, telemetry=t)
    light = ResourceWeight(cpu_cores=1, memory_gb=2, duration_est_s=10)
    assert node.can_accept(light) is True
    heavy = ResourceWeight(cpu_cores=999, memory_gb=999, duration_est_s=10)
    assert node.can_accept(heavy) is False


def test_thermal_redline_blocks():
    spec = default_nodes()[1]  # pi redline 80
    t = Telemetry(node_id=spec.id, cpu_percent=10, memory_percent=20,
                  memory_used_gb=1, memory_total_gb=spec.memory_gb,
                  temperature_c=82, power_watts=20, bandwidth_mbps=0, load_avg=0.5,
                  thermal_state=ThermalState.CRITICAL)
    node = Node(spec=spec, telemetry=t)
    assert node.can_accept(ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=5)) is False


def test_flight_callsign_autogen():
    f = Flight(task="do thing")
    assert f.callsign.startswith("FLT-")
    f2 = Flight(task="do thing", callsign="ALPHA-1")
    assert f2.callsign == "ALPHA-1"
