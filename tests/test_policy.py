"""Tests for the pure admission policy.

These exercise `decide()` with no database, no server and no clock.
"""
import pytest

from the_tower.policy import (
    POLICY_VERSION, Decision, MeshSnapshot, NodeView, ScopeView, decide,
)
from the_tower.schemas import (
    ClearanceDecision, Flight, FlightPriority, ResourceWeight, ThermalState,
)


def make_node(node_id="n1", name="Node 1", **kw):
    defaults = dict(
        id=node_id, name=name, status="online",
        cpu_percent=10.0, memory_percent=20.0, temperature_c=45.0,
        power_watts=20.0, thermal_state=ThermalState.NOMINAL.value, gpu_percent=0.0,
        cpu_cores=8.0, memory_gb=16.0, gpu_units=0.0, redline_temp_c=90.0,
        max_power_watts=100.0, network_capacity_mbps=1000.0,
        reserved_cpu=0.0, reserved_memory_gb=0.0, reserved_gpu=0.0,
        telemetry_age_s=1.0,
    )
    defaults.update(kw)
    return NodeView(**defaults)


def test_decide_is_pure_and_repeatable():
    snap = MeshSnapshot(nodes=[make_node()])
    flight = Flight(task="x", weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=5))
    d1 = decide(flight, snap)
    d2 = decide(flight, snap)
    assert d1 == d2
    assert d1.decision == ClearanceDecision.CLEARED
    assert d1.assigned_node == "n1"


def test_decide_does_not_mutate_the_snapshot():
    snap = MeshSnapshot(nodes=[make_node()])
    before = snap.nodes[0]
    flight = Flight(task="x", weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=5))
    decide(flight, snap)
    assert snap.nodes[0] is before
    assert snap.nodes[0].reserved_cpu == 0.0


def test_rogue_flight_is_shot_down():
    snap = MeshSnapshot(nodes=[make_node()])
    flight = Flight(task="x", rogue=True, weight=ResourceWeight(cpu_cores=1, memory_gb=1))
    d = decide(flight, snap)
    assert d.decision == ClearanceDecision.SHOT_DOWN
    assert d.assigned_node is None


def test_redline_node_is_never_selected():
    snap = MeshSnapshot(nodes=[make_node(status="redline")])
    flight = Flight(task="x", weight=ResourceWeight(cpu_cores=1, memory_gb=1))
    d = decide(flight, snap)
    assert d.decision == ClearanceDecision.QUEUED
    assert d.assigned_node is None or d.assigned_node == "n1"


def test_unknown_node_is_never_selected():
    snap = MeshSnapshot(nodes=[make_node(status="unknown")])
    flight = Flight(task="x", weight=ResourceWeight(cpu_cores=1, memory_gb=1))
    d = decide(flight, snap)
    assert d.decision == ClearanceDecision.QUEUED


def test_thermal_state_critical_blocks_admission():
    snap = MeshSnapshot(nodes=[make_node(thermal_state=ThermalState.CRITICAL.value)])
    flight = Flight(task="x", weight=ResourceWeight(cpu_cores=1, memory_gb=1))
    d = decide(flight, snap)
    assert d.decision == ClearanceDecision.QUEUED


def test_oversized_flight_is_queued():
    snap = MeshSnapshot(nodes=[make_node()])
    flight = Flight(task="x", weight=ResourceWeight(cpu_cores=999, memory_gb=999))
    d = decide(flight, snap)
    assert d.decision == ClearanceDecision.QUEUED


def test_reserved_capacity_counts():
    snap = MeshSnapshot(nodes=[make_node(reserved_memory_gb=15.0)])
    flight = Flight(task="x", weight=ResourceWeight(cpu_cores=1, memory_gb=4))
    d = decide(flight, snap)
    assert d.decision == ClearanceDecision.QUEUED


def test_scope_conflict_queues_lower_priority():
    conflict = ScopeView(flight_id="f1", callsign="FLT-1", scope=["res"], priority=1,
                         node_id="n1", assigned_node="n1")
    snap = MeshSnapshot(nodes=[make_node()], in_flight=[conflict])
    flight = Flight(task="x", scope=["res"], priority=FlightPriority.P2,
                    weight=ResourceWeight(cpu_cores=1, memory_gb=1))
    d = decide(flight, snap)
    assert d.decision == ClearanceDecision.QUEUED
    assert d.scope_conflict == "f1"


def test_scope_conflict_preempts_lower_priority():
    conflict = ScopeView(flight_id="f1", callsign="FLT-1", scope=["res"], priority=3,
                         node_id="n1", assigned_node="n1")
    snap = MeshSnapshot(nodes=[make_node()], in_flight=[conflict])
    flight = Flight(task="x", scope=["res"], priority=FlightPriority.P0,
                    weight=ResourceWeight(cpu_cores=1, memory_gb=1))
    d = decide(flight, snap)
    # Preemption remains an intent until enforcement and capacity are checked.
    assert d.decision == ClearanceDecision.QUEUED
    assert d.preempt_flight_id == "f1"


def test_best_scoring_node_wins():
    hot = make_node("hot", "Hot", temperature_c=80.0)
    cool = make_node("cool", "Cool", temperature_c=35.0)
    snap = MeshSnapshot(nodes=[hot, cool])
    flight = Flight(task="x", weight=ResourceWeight(cpu_cores=1, memory_gb=1))
    d = decide(flight, snap)
    assert d.assigned_node == "cool"
    assert "hot" in d.scored and "cool" in d.scored


def test_decision_carries_explainability_block():
    snap = MeshSnapshot(nodes=[make_node()])
    flight = Flight(task="x", weight=ResourceWeight(cpu_cores=1, memory_gb=1))
    d = decide(flight, snap)
    payload = d.to_dict()
    assert payload["policy_version"] == POLICY_VERSION
    assert payload["node_state_at_decision"]["temp_c"] == 45.0
    assert payload["scored"]["n1"] > 0
    assert "reason" in payload


def test_preferred_node_is_honoured():
    snap = MeshSnapshot(nodes=[make_node("a"), make_node("b")])
    flight = Flight(task="x", preferred_node="b",
                    weight=ResourceWeight(cpu_cores=1, memory_gb=1))
    d = decide(flight, snap)
    assert d.assigned_node == "b"
    assert "a" not in d.scored


def test_empty_mesh_queues_everything():
    snap = MeshSnapshot(nodes=[])
    flight = Flight(task="x", weight=ResourceWeight(cpu_cores=1, memory_gb=1))
    d = decide(flight, snap)
    assert d.decision == ClearanceDecision.QUEUED
    assert d.assigned_node is None
