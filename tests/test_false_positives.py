"""The event log is the operator's audit trail. It must not fill up with
false alarms, because a log that cries wolf is a log nobody reads.

A warming machine is a trend, not a spike. A spike is a point that departs
sharply from the trend.
"""
import pytest

from the_tower.anomaly import (
    AnomalyRadar,
    SPIKE_MIN_DEVIATION_C,
    detect_spike,
    time_axis,
)
from the_tower.schemas import Node, NodeSpec, Telemetry, ThermalState


def _node(history):
    node = Node(spec=NodeSpec(id="n1", name="n1", role="t", specs="x", constraints="y",
                               cpu_cores=8.0, memory_gb=16.0, redline_temp_c=90.0,
                               max_power_watts=100.0, network_capacity_mbps=1000.0),
               status=__import__("the_tower.schemas", fromlist=["NodeStatus"]).NodeStatus.ONLINE,
               telemetry=history[-1])
    node.history = list(history)
    return node


def _series(temps, interval_s=5.0):
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        Telemetry(node_id="n1", timestamp=base + timedelta(seconds=i * interval_s),
                  cpu_percent=20.0, memory_percent=40.0, memory_used_gb=4.0,
                  memory_total_gb=16.0, temperature_c=t, power_watts=20.0,
                  bandwidth_mbps=5.0, load_avg=1.0, thermal_state=ThermalState.NOMINAL)
        for i, t in enumerate(temps)
    ]


def test_a_smooth_ramp_is_not_a_spike():
    """Regression: a steadily warming node fired a thermal spike on every
    sweep, because the baseline spread shrank as the series trended."""
    temps = [60.0 + i * 0.4 for i in range(14)]
    assert detect_spike(time_axis(_series(temps)), temps) is None


def test_a_slow_ramp_is_not_a_spike():
    temps = [60.0 + i * 0.05 for i in range(20)]
    assert detect_spike(time_axis(_series(temps)), temps) is None


def test_a_flat_noisy_line_is_not_a_spike():
    import random

    random.seed(7)
    temps = [60.0 + random.uniform(-0.3, 0.3) for _ in range(20)]
    assert detect_spike(time_axis(_series(temps)), temps) is None


def test_a_real_jump_is_a_spike():
    temps = [60.0 + i * 0.1 for i in range(12)] + [72.0, 72.2, 72.1]
    spike = detect_spike(time_axis(_series(temps)), temps)
    assert spike is not None
    value, z, deviation = spike
    assert value > 70
    assert z >= 3.0
    assert deviation >= SPIKE_MIN_DEVIATION_C


def test_a_small_jitter_is_not_a_spike():
    temps = [60.0 + i * 0.1 for i in range(12)] + [61.4, 61.5, 61.3]
    assert detect_spike(time_axis(_series(temps)), temps) is None


def test_the_anomaly_radar_stays_quiet_on_a_warming_node():
    radar = AnomalyRadar()
    temps = [60.0 + i * 0.4 for i in range(14)]
    node = _node(_series(temps))
    events = radar.analyze_node(node)
    spikes = [e for e in events if "Thermal spike" in e.message]
    assert spikes == [], [e.message for e in events]


def test_the_anomaly_radar_reports_a_real_spike():
    radar = AnomalyRadar()
    temps = [60.0 + i * 0.1 for i in range(12)] + [72.0, 72.2, 72.1]
    node = _node(_series(temps))
    events = radar.analyze_node(node)
    spikes = [e for e in events if "Thermal spike" in e.message]
    assert len(spikes) == 1
    assert spikes[0].data["deviation_c"] >= SPIKE_MIN_DEVIATION_C


def test_the_same_condition_is_not_reported_on_every_sweep():
    """A log that repeats the same warning every sweep is a log nobody reads."""
    radar = AnomalyRadar(report_cooldown_s=60.0)
    temps = [60.0 + i * 0.1 for i in range(12)] + [72.0, 72.2, 72.1]
    node = _node(_series(temps))
    first = [e for e in radar.analyze_node(node) if "Thermal spike" in e.message]
    assert first
    # the same spike, seen again on the next sweep
    second = [e for e in radar.analyze_node(node) if "Thermal spike" in e.message]
    assert second == []


def test_a_different_condition_is_still_reported():
    radar = AnomalyRadar(report_cooldown_s=60.0)
    warm = _series([60.0 + i * 0.1 for i in range(12)] + [72.0, 72.2, 72.1])
    node = _node(warm)
    radar.analyze_node(node)
    # now a redline breach (the node's redline is 90C), which is a different condition
    hot = _series([95.0] * 12)
    node = _node(hot)
    events = radar.analyze_node(node)
    assert any(e.kind.value == "redline" for e in events), [e.message for e in events]


def test_the_time_to_redline_is_never_negative():
    """Regression: the forecast said 'predicted redline in -21 seconds'."""
    radar = AnomalyRadar()
    # a node that is already past its redline
    temps = [95.0 + i * 0.5 for i in range(12)]
    node = _node(_series(temps))
    radar.analyze_node(node)
    summary = radar.trend_summary(node)
    assert summary["secs_to_redline"] is None or summary["secs_to_redline"] >= 0


def test_the_simulated_signal_behaves_like_a_machine():
    """A simulated node must not jump four degrees between two sweeps."""
    import asyncio

    from the_tower.core import Tower

    async def run():
        t = Tower()
        await t.start()
        try:
            node = t.get_node("pi-cluster")
            for _ in range(12):
                await t.radar.sweep_once()
            temps = [h.temperature_c for h in node.history]
            steps = [abs(b - a) for a, b in zip(temps, temps[1:])]
            assert max(steps) < 2.0, temps
        finally:
            await t.stop()

    asyncio.run(run())
