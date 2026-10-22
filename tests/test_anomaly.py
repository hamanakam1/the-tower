"""The thermal forecast must not depend on a hardcoded sample interval.

The sweep interval is configurable, so a prediction that assumes five
seconds between samples is wrong by the ratio of the real interval to five
- and that prediction is what decides whether a node is drained.
"""
from datetime import datetime, timedelta, timezone

import pytest

from the_tower.anomaly import AnomalyRadar, sample_interval, time_axis
from the_tower.schemas import Node, NodeSpec, Telemetry, ThermalState


def _spec(node_id: str = "n1") -> NodeSpec:
    return NodeSpec(id=node_id, name=node_id, role="test", specs="x", constraints="y",
                    cpu_cores=8.0, memory_gb=16.0, redline_temp_c=90.0,
                    max_power_watts=100.0, network_capacity_mbps=1000.0)


def _node(history):
    node = Node(spec=_spec(), status=__import__(
        "the_tower.schemas", fromlist=["NodeStatus"]).NodeStatus.ONLINE,
        telemetry=history[-1])
    node.history = list(history)
    return node


def _history(interval_s: float, count: int = 12, start_temp: float = 60.0,
             step: float = 1.0, duration_s: float = None):
    """A steady temperature ramp sampled every `interval_s` seconds."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    out = []
    for i in range(count):
        out.append(Telemetry(
            node_id="n1", timestamp=base + timedelta(seconds=i * interval_s),
            cpu_percent=20.0, memory_percent=40.0, memory_used_gb=4.0, memory_total_gb=16.0,
            temperature_c=start_temp + step * i, power_watts=20.0, bandwidth_mbps=5.0,
            load_avg=1.0, thermal_state=ThermalState.NOMINAL))
    return out


def _ramp(interval_s: float, rate_c_per_s: float = 0.2, duration_s: float = 60.0,
          start_temp: float = 60.0):
    """The same physical ramp, sampled at a different rate."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    out = []
    t = 0.0
    while t <= duration_s + 1e-9:
        out.append(Telemetry(
            node_id="n1", timestamp=base + timedelta(seconds=t),
            cpu_percent=20.0, memory_percent=40.0, memory_used_gb=4.0, memory_total_gb=16.0,
            temperature_c=start_temp + rate_c_per_s * t, power_watts=20.0, bandwidth_mbps=5.0,
            load_avg=1.0, thermal_state=ThermalState.NOMINAL))
        t += interval_s
    return out


def test_time_axis_uses_the_real_timestamps():
    hist = _history(interval_s=2.0)
    xs = time_axis(hist)
    assert xs[0] == 0.0
    assert xs[1] == pytest.approx(2.0)
    assert xs[-1] == pytest.approx(22.0)


def test_sample_interval_is_measured():
    assert sample_interval(time_axis(_history(interval_s=2.0))) == pytest.approx(2.0)
    assert sample_interval(time_axis(_history(interval_s=30.0))) == pytest.approx(30.0)


def test_time_to_redline_is_the_same_at_any_interval():
    """The same physical ramp, sampled at different rates, must produce the
    same time-to-redline."""
    radar = AnomalyRadar()
    results = {}
    for interval in (1.0, 2.0, 5.0, 15.0, 30.0):
        node = _node(_ramp(interval_s=interval, rate_c_per_s=0.2, duration_s=60.0))
        summary = radar.trend_summary(node)
        results[interval] = summary["secs_to_redline"]
    values = list(results.values())
    assert all(v is not None for v in values)
    # 0.2C per second from 72C to a 90C redline is 90 seconds, whatever the
    # sampling rate.
    assert max(values) - min(values) < 2.0, results
    assert abs(results[1.0] - 90.0) < 2.0, results


def test_slope_is_reported_per_sample_and_per_second():
    radar = AnomalyRadar()
    node = _node(_history(interval_s=5.0, count=12, start_temp=60.0, step=1.0))
    summary = radar.trend_summary(node)
    # 1C per 5s sample == 0.2C per sample, 0.2C/s
    assert summary["temp_slope"] == pytest.approx(1.0)
    assert summary["temp_slope_per_s"] == pytest.approx(0.2, abs=1e-3)
    assert summary["sample_interval_s"] == pytest.approx(5.0)


def test_anomaly_event_reports_the_real_interval():
    radar = AnomalyRadar()
    hist = _history(interval_s=2.0, count=12, start_temp=80.0, step=1.0)
    node = _node(hist)
    events = radar.analyze_node(node)
    thermal = [e for e in events if "Thermal trend" in e.message]
    assert thermal
    data = thermal[0].data
    assert data["sample_interval_s"] == pytest.approx(2.0)
    assert data["secs_to_redline"] < 60


def test_identical_timestamps_fall_back_without_crashing():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    hist = [Telemetry(node_id="n1", timestamp=base, cpu_percent=10.0, memory_percent=10.0,
                      memory_used_gb=1.0, memory_total_gb=16.0, temperature_c=50.0,
                      power_watts=10.0, bandwidth_mbps=1.0, load_avg=0.5,
                      thermal_state=ThermalState.NOMINAL) for _ in range(8)]
    xs = time_axis(hist)
    assert len(xs) == 8
    assert sample_interval(xs) == pytest.approx(5.0)
    radar = AnomalyRadar()
    node = _node(hist)
    assert isinstance(radar.trend_summary(node), dict)


def test_naive_timestamps_are_treated_as_utc():
    naive = datetime(2026, 1, 1, 0, 0, 0)
    hist = [Telemetry(node_id="n1", timestamp=naive + timedelta(seconds=i * 5),
                      cpu_percent=10.0, memory_percent=10.0, memory_used_gb=1.0,
                      memory_total_gb=16.0, temperature_c=50.0 + i, power_watts=10.0,
                      bandwidth_mbps=1.0, load_avg=0.5, thermal_state=ThermalState.NOMINAL)
            for i in range(8)]
    xs = time_axis(hist)
    assert xs[-1] == pytest.approx(35.0)
