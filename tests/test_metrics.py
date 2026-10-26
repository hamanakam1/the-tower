"""The metrics endpoint is what a pager watches. The series it exposes have
to match the Tower's actual opinion about a node."""
import pytest

from the_tower.core import Tower
from the_tower.metrics import render


@pytest.fixture
def tower():
    return Tower()


def test_enforcement_is_exported_per_node(tower):
    out = render(tower)
    assert 'tower_node_enforcing{node="m3max"} 0' in out
    tower.set_enforcement("m3max", "enforce")
    assert 'tower_node_enforcing{node="m3max"} 1' in render(tower)


def test_thermal_margin_is_exported(tower):
    node = tower.get_node("m3max")
    node.telemetry = node.telemetry.model_copy(update={"temperature_c": 90.0})
    out = render(tower)
    assert 'tower_node_thermal_margin_c{node="m3max"} 5.0' in out


def test_status_is_a_one_hot_series(tower):
    out = render(tower)
    assert 'tower_node_status{node="m3max",status="online"} 1' in out


def test_metrics_survive_a_hostile_node_id(tower):
    """A node id is operator input. A quote or a newline in it must not be
    able to forge a series."""
    from the_tower.schemas import NodeSpec

    tower.register_node(NodeSpec(
        id='evil"} 1\ntower_fake', name="evil", role="t", specs="x",
        constraints="y", cpu_cores=1, memory_gb=1, redline_temp_c=90,
        max_power_watts=10, network_capacity_mbps=10))
    out = render(tower)
    assert "\ntower_fake" not in out
    assert 'node="evil\\"} 1\\ntower_fake"' in out


def test_an_unknown_node_is_not_counted_online(tower):
    before = tower.airspace().stats["nodes_online"]
    tower.set_node_state("m3max", "unknown")
    after = tower.airspace().stats["nodes_online"]
    assert after == before - 1
