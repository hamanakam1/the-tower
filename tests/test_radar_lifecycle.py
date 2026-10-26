"""Dynamic nodes must join and leave radar without stranding a sweep."""
from __future__ import annotations

import pytest

from the_tower.core import Tower
from the_tower.schemas import Node, NodeSpec, NodeStatus, Telemetry


def node_spec(node_id: str, sampler: str = "simulated") -> NodeSpec:
    return NodeSpec(
        id=node_id, name="Synthetic node", role="test", specs="fixture",
        constraints="fixture", cpu_cores=2, memory_gb=4, redline_temp_c=90,
        max_power_watts=50, network_capacity_mbps=100, sampler=sampler,
        simulate=sampler == "simulated",
    )


@pytest.mark.asyncio
async def test_registered_simulated_nodes_complete_the_sweep() -> None:
    tower = Tower(nodes=[])
    first = tower.register_node(node_spec("simulated-first"))
    second = tower.register_node(node_spec("simulated-second"))
    delivered: list[set[str]] = []

    async def record(readings: dict[str, Telemetry]) -> None:
        delivered.append(set(readings))

    tower.radar.subscribe(record)
    readings = await tower.radar.sweep_once()

    assert set(readings) == {first.id, second.id}
    assert delivered == [{first.id, second.id}]
    for node in (first, second):
        assert readings[node.id].source == "simulated"
        assert node.history == [readings[node.id]]
        assert node.spec.enforcement == "advisory"


@pytest.mark.asyncio
async def test_removal_discards_phase_and_reregistration_starts_fresh() -> None:
    retained = node_spec("retained")
    replaced = node_spec("replaced")
    tower = Tower(nodes=[retained, replaced])
    old_node = tower.get_node(replaced.id)
    assert old_node is not None
    await tower.radar.sweep_once()
    retained_phase = tower.radar._phase[retained.id]

    assert tower.unregister_node(replaced.id) is True
    assert replaced.id not in tower.radar.nodes
    assert replaced.id not in tower.radar._phase, "removed nodes must not retain simulation state"
    assert set(await tower.radar.sweep_once()) == {retained.id}

    new_node = tower.register_node(replaced)
    assert new_node is not old_node
    assert new_node.history == []
    readings = await tower.radar.sweep_once()
    assert set(readings) == {retained.id, replaced.id}
    assert readings[replaced.id].source == "simulated"
    assert new_node.history == [readings[replaced.id]]
    assert tower.radar._phase[retained.id] == retained_phase


@pytest.mark.asyncio
async def test_duplicate_registration_preserves_live_sampler_state() -> None:
    spec = node_spec("existing")
    tower = Tower(nodes=[spec])
    node = tower.get_node(spec.id)
    assert node is not None
    await tower.radar.sweep_once()
    phase = tower.radar._phase[spec.id]
    reading = node.telemetry

    assert tower.register_node(spec) is node
    assert node.telemetry is reading
    assert node.history == [reading]
    assert tower.radar._phase[spec.id] == phase


@pytest.mark.asyncio
@pytest.mark.parametrize("sampler", ["local", "agent"])
async def test_reregistered_nonsimulated_nodes_keep_sampler_and_operator_state(
    sampler: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tower = Tower(nodes=[])
    tower.register_node(node_spec("replacement"))
    assert tower.unregister_node("replacement") is True
    node = tower.register_node(node_spec("replacement", sampler))
    reading = node.telemetry.model_copy(update={
        "cpu_percent": 17.0, "temperature_c": 44.0, "source": "measured",
    })

    def local_sample(current: Node, now: float) -> Telemetry:
        assert current is node
        return reading

    def reject_simulation(node_id: str, current: Node, now: float) -> Telemetry:
        raise AssertionError("local and agent nodes must not use simulated telemetry")

    monkeypatch.setattr(tower.radar, "_sample_local", local_sample)
    monkeypatch.setattr(tower.radar, "_sample_simulated", reject_simulation)
    if sampler == "agent":
        assert tower.ingest_telemetry(node.id, reading) is True
    assert tower.set_node_state(node.id, "cordoned") is True

    readings = await tower.radar.sweep_once()

    assert readings[node.id] is reading
    assert node.telemetry is reading
    assert node.history == [reading]
    assert reading.source == ("ingested" if sampler == "agent" else "measured")
    assert node.status == NodeStatus.CORDONED
    assert node.spec.enforcement == "advisory"
