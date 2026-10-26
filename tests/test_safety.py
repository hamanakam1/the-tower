"""Tests for the graded safety ladder."""
import pytest

from the_tower.safety import (
    Ladder, assess, ladder_summary, throttled_weight, THROTTLE_FACTOR,
)
from the_tower.schemas import Flight, ResourceWeight, ThermalState


def node(**kw):
    from the_tower.policy import NodeView
    defaults = dict(
        id="n1", name="Node", status="online",
        cpu_percent=10.0, memory_percent=20.0, temperature_c=45.0,
        power_watts=20.0, thermal_state=ThermalState.NOMINAL.value, gpu_percent=0.0,
        cpu_cores=8.0, memory_gb=16.0, gpu_units=0.0, redline_temp_c=90.0,
        max_power_watts=100.0, network_capacity_mbps=1000.0,
        reserved_cpu=0.0, reserved_memory_gb=0.0, reserved_gpu=0.0,
    )
    defaults.update(kw)
    return NodeView(**defaults)


def test_nominal_node_admits():
    a = assess(node())
    assert a.ladder == Ladder.ADMIT


def test_fair_thermal_warns():
    a = assess(node(thermal_state=ThermalState.FAIR.value))
    assert a.ladder == Ladder.WARN


def test_serious_thermal_throttles():
    a = assess(node(thermal_state=ThermalState.SERIOUS.value, temperature_c=82.0))
    assert a.ladder == Ladder.THROTTLE


def test_saturated_node_throttles():
    a = assess(node(cpu_percent=95.0))
    assert a.ladder == Ladder.THROTTLE


def test_predicted_redline_drains():
    a = assess(node(temperature_c=75.0), predicted_redline_in_s=60)
    assert a.ladder == Ladder.DRAIN


def test_redline_kills():
    a = assess(node(status="redline", temperature_c=95.0))
    assert a.ladder == Ladder.KILL


def test_an_offline_node_takes_no_new_work():
    a = assess(node(status="offline"))
    assert a.ladder == Ladder.DRAIN
    assert "offline" in a.reason


def test_unknown_node_drains_but_does_not_kill():
    """Stale telemetry means the Tower cannot see the machine. The safe
    answer is to stop adding work, not to reach in and kill what may be
    perfectly healthy."""
    a = assess(node(status="unknown"))
    assert a.ladder == Ladder.DRAIN
    assert "unknown" in a.reason


def test_critical_thermal_kills():
    a = assess(node(thermal_state=ThermalState.CRITICAL.value))
    assert a.ladder == Ladder.KILL


def test_throttle_reduces_cpu_but_not_memory():
    f = Flight(task="x", weight=ResourceWeight(cpu_cores=8, memory_gb=4, duration_est_s=10))
    t = throttled_weight(f, THROTTLE_FACTOR[Ladder.THROTTLE])
    assert t.weight.cpu_cores < f.weight.cpu_cores
    assert t.weight.memory_gb == f.weight.memory_gb


def test_throttle_does_not_mutate_original():
    f = Flight(task="x", weight=ResourceWeight(cpu_cores=8, memory_gb=4, duration_est_s=10))
    throttled_weight(f, 0.5)
    assert f.weight.cpu_cores == 8


def test_ladder_summary_counts():
    summary = ladder_summary([assess(node()), assess(node(status="redline"))])
    assert summary["admit"] == 1
    assert summary["kill"] == 1


def test_assessment_serialises():
    a = assess(node())
    d = a.to_dict()
    assert d["ladder"] == "admit"
    assert "reason" in d
    assert "headroom_c" in d
