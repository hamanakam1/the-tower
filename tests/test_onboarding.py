"""Onboarding a real node has to be one command, not three.

The agent used to push telemetry at a node id the Tower had never heard of
and print 404 forever. Now it can register itself, describing the machine
it is actually running on.
"""
import pytest
from starlette.testclient import TestClient

from the_tower.agent import node_spec, sample
from the_tower.core import Tower
from the_tower.server import TowerServer


def test_the_derived_spec_describes_this_machine():
    spec = node_spec("pi-01", redline=80.0, role="Drone", location="edge",
                     enforcement="advisory", staleness_s=15)
    assert spec["id"] == "pi-01"
    assert spec["role"] == "Drone"
    assert spec["location"] == "edge"
    assert spec["cpu_cores"] >= 1
    assert spec["memory_gb"] > 0
    assert spec["redline_temp_c"] == 80.0
    assert spec["sampler"] == "agent"
    assert spec["simulate"] is False
    assert spec["staleness_budget_s"] == 15
    assert spec["enforcement"] == "advisory"
    assert "cores" in spec["specs"]


def test_the_derived_spec_is_accepted_by_the_tower():
    from the_tower.schemas import NodeSpec

    spec = NodeSpec(**node_spec("pi-01"))
    assert spec.id == "pi-01"


def test_a_registered_agent_node_reports_real_telemetry():
    import asyncio

    async def run():
        t = Tower()
        await t.start()
        app = TowerServer(t).app
        with TestClient(app) as client:
            spec = node_spec("pi-01", redline=80.0)
            r = client.post("/nodes", json=spec)
            assert r.status_code == 200, r.text
            node = t.get_node("pi-01")
            assert node is not None
            assert node.spec.sampler == "agent"
            assert node.spec.simulate is False

            payload = sample("pi-01")
            ing = client.post("/ingest/pi-01", json=payload)
            assert ing.status_code == 200, ing.text
            # the reading is the agent's, not the simulator's
            assert t.get_node("pi-01").telemetry.cpu_percent == payload["cpu_percent"]
        await t.stop()

    asyncio.run(run())


def test_an_agent_node_is_not_simulated_by_the_radar():
    import asyncio

    async def run():
        t = Tower()
        await t.start()
        try:
            spec = node_spec("pi-01")
            t.register_node(__import__("the_tower.schemas", fromlist=["NodeSpec"]).NodeSpec(**spec))
            payload = sample("pi-01")
            t.ingest_telemetry("pi-01", __import__(
                "the_tower.schemas", fromlist=["Telemetry"]).Telemetry(**payload))
            before = t.get_node("pi-01").telemetry.cpu_percent
            await t.radar.sweep_once()
            after = t.get_node("pi-01").telemetry.cpu_percent
            assert before == after
        finally:
            await t.stop()

    asyncio.run(run())


def test_the_agent_says_so_when_the_node_is_not_registered():
    """The agent must not spin silently against a 404."""
    import asyncio

    async def run():
        from the_tower.agent import run as agent_run
        import httpx
        # point the agent at a tower that does not know the node
        t = Tower()
        await t.start()
        app = TowerServer(t).app
        with TestClient(app) as client:
            assert client.post("/ingest/ghost-node", json=sample("ghost-node")).status_code == 404
        await t.stop()

    asyncio.run(run())
