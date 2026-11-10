"""A node registered after the radar starts must not break the sweep.

The radar keeps the same node dict the Tower mutates, but it seeded its
per-node simulation phase once, in ``__init__``. Registering a node later
therefore raised ``KeyError`` inside ``sweep_once``, and because the Tower's
sweep handler swallows exceptions, the visible symptom was silence: every
node's telemetry stopped updating and the scheduler kept deciding on stale
readings. Nothing surfaced until a decision went to the wrong node.
"""

from __future__ import annotations

import asyncio

from the_tower.core import Tower
from the_tower.schemas import NodeSpec


def _spec(node_id: str) -> NodeSpec:
    return NodeSpec(
        id=node_id, name=node_id, role="test", specs="x", constraints="y",
        cpu_cores=4, memory_gb=8, redline_temp_c=80,
        max_power_watts=20, network_capacity_mbps=100,
    )


def test_a_node_registered_after_start_is_swept(tmp_path):
    async def run():
        t = Tower(db_path=str(tmp_path / "tower.sqlite"))
        await t.start()
        t.register_node(_spec("late-01"))
        before = t.get_node("late-01").telemetry.timestamp

        # This is what the radar's background loop does every interval.
        swept = await t.radar.sweep_once()

        assert "late-01" in swept, "the late node was skipped by the sweep"
        assert t.get_node("late-01").telemetry.timestamp > before
        # The old nodes must keep being sampled too: a raising sweep aborts
        # the loop and freezes everyone, not just the node that was added.
        assert set(swept) == set(t.nodes)
        await t.stop()

    asyncio.run(run())
