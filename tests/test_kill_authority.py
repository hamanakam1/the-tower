"""The Tower must only ever stop processes it owns, and must let a flight
land before it kills it.

These are the two guarantees that make the kill switch safe to point at real
hardware.
"""
import asyncio
import os
import signal
import time
from datetime import datetime, timedelta, timezone

import pytest

from the_tower.core import Tower
from the_tower.schemas import (
    ClearanceRequest,
    Flight,
    FlightStatus,
    KillRequest,
    ResourceWeight,
)


def _quiet(tower):
    """Pin every node to a known-good state so the test does not depend on
    whatever the sampler happened to produce."""
    for node in tower.nodes.values():
        node.telemetry = node.telemetry.model_copy(update={
            "cpu_percent": 5.0, "memory_percent": 10.0, "temperature_c": 40.0,
            "power_watts": 10.0, "timestamp": datetime.now(timezone.utc)})
        node.status = node.status.ONLINE
    return tower


@pytest.fixture
async def tower(tmp_path):
    t = Tower(db_path=str(tmp_path / "tower.sqlite"))
    await t.start()
    _quiet(t)
    yield t
    await t.stop()


def _spawn():
    """A child process that will sit still until it is signalled."""
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child never returns
        try:
            time.sleep(60)
        finally:
            os._exit(0)
    return pid


def _reap(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    try:
        os.waitpid(pid, 0)
    except (ChildProcessError, OSError):
        pass


# --------------------------------------------------------------------------
# Process ownership
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_numeric_container_id_is_not_treated_as_a_pid(tower):
    """Regression: a registered container whose id happens to be all digits
    used to be SIGKILLed as a process id."""
    victim = _spawn()
    try:
        tower.register_container(str(victim))
        resp = await tower.kill(KillRequest(container_id=str(victim), reason="numeric id"))
        assert resp.process_terminated is False
        # the process is still alive
        os.kill(victim, 0)
    finally:
        _reap(victim)


@pytest.mark.asyncio
async def test_an_unregistered_pid_is_never_signalled(tower):
    victim = _spawn()
    try:
        resp = await tower.kill(KillRequest(container_id=str(victim), reason="not registered"))
        assert resp.process_terminated is False
        os.kill(victim, 0)
    finally:
        _reap(victim)


@pytest.mark.asyncio
async def test_a_registered_pid_is_terminated(tower):
    child = _spawn()
    tower.register_container("c-mine", pid=child)
    resp = await tower.kill(KillRequest(container_id="c-mine", reason="owned"))
    assert resp.process_terminated is True
    os.waitpid(child, 0)


@pytest.mark.asyncio
async def test_ownership_survives_a_restart(tmp_path):
    db = str(tmp_path / "tower.sqlite")
    child = _spawn()
    try:
        t = Tower(db_path=db)
        await t.start()
        _quiet(t)
        t.register_container("c-mine", pid=child)
        await t.stop()

        t2 = Tower(db_path=db)
        await t2.start()
        assert t2.owned_pids().get("c-mine") == child
        resp = await t2.kill(KillRequest(container_id="c-mine", reason="after restart"))
        assert resp.process_terminated is True
        os.waitpid(child, 0)
        await t2.stop()
    finally:
        _reap(child)


@pytest.mark.asyncio
async def test_pid_one_is_never_claimed(tower):
    tower.register_container("c-init", pid=1)
    assert tower.owned_pids() == {}
    resp = await tower.kill(KillRequest(container_id="c-init", reason="init"))
    assert resp.process_terminated is False


@pytest.mark.asyncio
async def test_the_kill_is_audited_with_what_actually_happened(tower):
    child = _spawn()
    try:
        tower.register_container("c-mine", pid=child)
        await tower.kill(KillRequest(container_id="c-mine", reason="owned"))
        assert await tower.verify_audit_async() is True
        rows = tower.storage.conn.execute("SELECT payload_json FROM audit").fetchall()
        import json
        kinds = [json.loads(r["payload_json"]).get("kind") for r in rows]
        assert "kill" in kinds
    finally:
        _reap(child)


# --------------------------------------------------------------------------
# Draining: the flight gets a chance to land
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drain_timeout_gives_the_flight_a_grace_period(tower):
    flight = Flight(task="long job", container_id="c-drain",
                    weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=600))
    await tower.request_clearance(ClearanceRequest(flight=flight))
    assert flight.status == FlightStatus.IN_FLIGHT

    resp = await tower.kill(KillRequest(container_id="c-drain", reason="operator",
                                        drain_timeout_s=30))
    assert resp.draining is True
    assert resp.process_terminated is False
    assert flight.status == FlightStatus.DRAINING
    assert flight.drain_until is not None
    # the flight keeps its claim on the node while it drains
    node = tower.nodes.get(flight.assigned_node)
    if node:
        assert flight.id in node.active_flights


@pytest.mark.asyncio
async def test_a_draining_flight_lands_when_it_finishes(tower):
    flight = Flight(task="short job", container_id="c-land",
                    weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=1))
    await tower.request_clearance(ClearanceRequest(flight=flight))
    await tower.kill(KillRequest(container_id="c-land", reason="operator", drain_timeout_s=60))
    assert flight.status == FlightStatus.DRAINING
    await asyncio.sleep(1.2)
    await tower._progress_flights()
    assert flight.status == FlightStatus.LANDED


@pytest.mark.asyncio
async def test_a_draining_flight_is_terminated_when_the_deadline_passes(tower):
    flight = Flight(task="stubborn job", container_id="c-hard",
                    weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=600))
    await tower.request_clearance(ClearanceRequest(flight=flight))
    await tower.kill(KillRequest(container_id="c-hard", reason="operator", drain_timeout_s=1))
    assert flight.status == FlightStatus.DRAINING
    await asyncio.sleep(1.2)
    await tower._progress_flights()
    assert flight.status == FlightStatus.SHOT_DOWN
    assert "drain deadline expired" in flight.reason


@pytest.mark.asyncio
async def test_a_kill_without_a_grace_period_is_immediate(tower):
    flight = Flight(task="instant job", container_id="c-now",
                    weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=600))
    await tower.request_clearance(ClearanceRequest(flight=flight))
    resp = await tower.kill(KillRequest(container_id="c-now", reason="operator"))
    assert resp.draining is False
    assert flight.status == FlightStatus.SHOT_DOWN


@pytest.mark.asyncio
async def test_a_kill_still_requires_a_reason(tower):
    resp = await tower.kill(KillRequest(container_id="c-x", reason="   "))
    assert resp.success is False
    assert "reason" in resp.message.lower()


@pytest.mark.asyncio
async def test_the_drain_deadline_survives_a_restart(tmp_path):
    db = str(tmp_path / "tower.sqlite")
    t = Tower(db_path=db)
    await t.start()
    _quiet(t)
    flight = Flight(task="draining across a restart", container_id="c-drain",
                    weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=600))
    await t.request_clearance(ClearanceRequest(flight=flight))
    await t.kill(KillRequest(container_id="c-drain", reason="operator", drain_timeout_s=600))
    await t.stop()

    t2 = Tower(db_path=db)
    await t2.start()
    reloaded = t2.flights.get(flight.id)
    assert reloaded is not None
    assert reloaded.status == FlightStatus.DRAINING
    assert reloaded.drain_until is not None
    await t2.stop()


# --------------------------------------------------------------------------
# Advisory by default
#
# README, the roadmap and the CHANGELOG all promise that a fresh Tower only
# recommends, and that acting on a node is an explicit opt-in. A schema
# default of "enforce" silently broke that promise for the built-in fleet
# and for every config that did not spell the field out.
# --------------------------------------------------------------------------

def test_a_bare_node_spec_is_advisory():
    from the_tower.schemas import NodeSpec

    spec = NodeSpec(id="bare", name="Bare", role="test", specs="x", constraints="y",
                    cpu_cores=4, memory_gb=8, redline_temp_c=90,
                    max_power_watts=50, network_capacity_mbps=100)
    assert spec.enforcement == "advisory"


def test_the_default_fleet_is_advisory():
    from the_tower.schemas import default_nodes

    assert default_nodes(), "the default fleet must not be empty"
    for spec in default_nodes():
        assert spec.enforcement == "advisory", f"{spec.id} would act without an opt-in"


@pytest.mark.asyncio
async def test_a_redline_on_the_default_fleet_does_not_kill(tower):
    """End to end: a default node at redline recommends a kill, never performs one."""
    node = tower.get_node("m3max")
    assert node.spec.enforcement == "advisory"
    flight = Flight(task="melting", weight=ResourceWeight(cpu_cores=1, memory_gb=1,
                                                          duration_est_s=600))
    await tower.request_clearance(ClearanceRequest(flight=flight))
    assert flight.status == FlightStatus.IN_FLIGHT

    from the_tower.schemas import ThermalState
    node.telemetry = node.telemetry.model_copy(update={
        "temperature_c": node.spec.redline_temp_c + 5,
        "thermal_state": ThermalState.CRITICAL,
        "timestamp": datetime.now(timezone.utc)})
    node.history.append(node.telemetry)

    await tower._auto_kill_on_redline(node.id)

    assert flight.status == FlightStatus.IN_FLIGHT
    assert any("ADVISORY" in e.message for e in tower.events)
    assert await tower.verify_audit_async() is True


@pytest.mark.asyncio
async def test_an_advisory_preemption_recommends_once_and_then_waits(tower):
    """A queued flight must not republish the same recommendation every sweep,
    and must fly once the incumbent lands."""
    from the_tower.schemas import ClearanceRequest, FlightPriority

    low = Flight(task="low", scope=["r"], priority=FlightPriority.P3,
                 weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=100))
    await tower.request_clearance(ClearanceRequest(flight=low))
    high = Flight(task="high", scope=["r"], priority=FlightPriority.P0,
                  weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=5))
    resp = await tower.request_clearance(ClearanceRequest(flight=high))
    assert resp.decision.value == "queued"

    def advice_count():
        return sum("would preempt" in e.message for e in tower.events)

    once = advice_count()
    assert once == 1
    for _ in range(3):
        await tower._dispatch_queued()
    assert advice_count() == once, "the same recommendation must not be republished"
    assert high.status == FlightStatus.QUEUED

    # the incumbent lands, the recommendation is satisfied, and the queued
    # flight is dispatched on the next sweep
    low.takeoff_at = low.takeoff_at.replace(year=2000)
    await tower._progress_flights()
    await tower._dispatch_queued()
    assert high.status == FlightStatus.IN_FLIGHT


