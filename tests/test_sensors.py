"""Tests for the pluggable sensor backends and samplers."""
import pytest

from the_tower.radar import RadarSweep
from the_tower.schemas import Node, NodeSpec, Telemetry, ThermalState
from the_tower.sensors import (
    BACKENDS, EstimateBackend, Reading, SensorBackend,
    read_temperature, register_backend, select_backend,
)


class FakeBackend(SensorBackend):
    name = "fake"

    def read(self, cpu_percent):
        return Reading(celsius=61.5, source="measured", note="fake sensor")


def test_estimate_backend_is_labelled():
    r = EstimateBackend().read(50.0)
    assert r.source == "estimated"
    assert r.note
    assert r.measured is False


def test_measured_reading_is_marked_measured():
    r = FakeBackend().read(10.0)
    assert r.measured is True
    assert r.celsius == 61.5


def test_select_backend_by_name():
    assert select_backend("estimate").name == "estimate"


def test_unknown_backend_falls_back_to_auto():
    b = select_backend("does-not-exist")
    assert b is not None


def test_read_temperature_falls_back_to_estimate(monkeypatch):
    class Empty(SensorBackend):
        name = "empty"

        def read(self, cpu_percent):
            return Reading(celsius=None, source="unavailable")

    register_backend("empty-test", Empty())
    r = read_temperature("empty-test", 40.0)
    assert r.source == "estimated"
    assert r.celsius is not None


def test_registered_backend_is_used():
    register_backend("fake", FakeBackend())
    r = read_temperature("fake", 10.0)
    assert r.celsius == 61.5
    assert r.source == "measured"


def make_node(node_id="n1", **kw):
    defaults = dict(
        id=node_id, name="N", role="test", specs="x", constraints="y",
        cpu_cores=2.0, memory_gb=4.0, redline_temp_c=90.0,
        max_power_watts=50.0, network_capacity_mbps=100.0,
    )
    defaults.update(kw)
    return Node(spec=NodeSpec(**defaults), telemetry=Telemetry(
        node_id=node_id, cpu_percent=0.0, memory_percent=0.0, memory_used_gb=0.0,
        memory_total_gb=4.0, temperature_c=40.0, power_watts=5.0,
        bandwidth_mbps=0.0, load_avg=0.0, thermal_state=ThermalState.NOMINAL))


@pytest.mark.asyncio
async def test_agent_sampler_never_fabricates():
    node = make_node("agent-node", sampler="agent")
    sweep = RadarSweep({"agent-node": node})
    before = node.telemetry.temperature_c
    await sweep.sweep_once()
    assert node.telemetry.temperature_c == before


@pytest.mark.asyncio
async def test_simulated_sampler_produces_values():
    node = make_node("sim-node", sampler="simulated")
    sweep = RadarSweep({"sim-node": node})
    await sweep.sweep_once()
    assert node.telemetry.source == "simulated"


@pytest.mark.asyncio
async def test_local_sampler_reports_provenance():
    node = make_node("local-node", sampler="local")
    sweep = RadarSweep({"local-node": node})
    await sweep.sweep_once()
    assert node.telemetry.source in ("measured", "estimated")
    if node.telemetry.source == "estimated":
        assert node.telemetry.source_note


@pytest.mark.asyncio
async def test_stale_agent_node_becomes_unknown():
    node = make_node("stale-node", sampler="agent", staleness_budget_s=1)
    from datetime import datetime, timedelta, timezone
    node.last_seen = datetime.now(timezone.utc) - timedelta(seconds=120)
    sweep = RadarSweep({"stale-node": node})
    await sweep.sweep_once()
    assert node.status.value == "unknown"
