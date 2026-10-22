"""Tests for decision replay."""
import pytest

from the_tower.policy import MeshSnapshot, NodeView, decide
from the_tower.replay import (
    compare, replay, snapshot_from_dict, snapshot_to_dict,
)
from the_tower.schemas import Flight, ResourceWeight


def node(node_id="n1", **kw):
    defaults = dict(
        id=node_id, name="Node", status="online",
        cpu_percent=10.0, memory_percent=20.0, temperature_c=45.0,
        power_watts=20.0, thermal_state="nominal", gpu_percent=0.0,
        cpu_cores=8.0, memory_gb=16.0, gpu_units=0.0, redline_temp_c=90.0,
        max_power_watts=100.0, network_capacity_mbps=1000.0,
        reserved_cpu=0.0, reserved_memory_gb=0.0, reserved_gpu=0.0,
        telemetry_age_s=1.0,
    )
    defaults.update(kw)
    return NodeView(**defaults)


def test_snapshot_round_trips():
    snap = MeshSnapshot(nodes=[node("a"), node("b", temperature_c=60.0)])
    data = snapshot_to_dict(snap)
    back = snapshot_from_dict(data)
    assert len(back.nodes) == 2
    assert back.nodes[1].temperature_c == 60.0
    assert back.nodes[0].id == "a"


def test_replay_reproduces_the_original_decision():
    snap = MeshSnapshot(nodes=[node()])
    flight = Flight(task="x", weight=ResourceWeight(cpu_cores=1, memory_gb=1))
    original = decide(flight, snap)
    data = snapshot_to_dict(snap)
    again = replay(flight, data)
    assert again.decision == original.decision
    assert again.assigned_node == original.assigned_node


def test_replay_detects_drift():
    snap = MeshSnapshot(nodes=[node()])
    flight = Flight(task="x", weight=ResourceWeight(cpu_cores=1, memory_gb=1))
    original = decide(flight, snap)
    # the node crosses its redline after the decision was made
    hotter = MeshSnapshot(nodes=[node(temperature_c=95.0, thermal_state="critical")])
    again = replay(flight, snapshot_to_dict(hotter))
    result = compare(original.to_dict(), again)
    assert result["matches"] is False
    assert result["drift"]


def test_compare_reports_match():
    snap = MeshSnapshot(nodes=[node()])
    flight = Flight(task="x", weight=ResourceWeight(cpu_cores=1, memory_gb=1))
    d = decide(flight, snap)
    result = compare(d.to_dict(), d)
    assert result["matches"] is True
    assert result["drift"] is None
