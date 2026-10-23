"""The Tower must only ever stop processes it owns, and must let a flight
land before it kills it.

These are the two guarantees that make the kill switch safe to point at real
hardware.
"""
import subprocess
import sys
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
    # Launch a fresh interpreter without executing Python between fork and
    # exec in the multithreaded test runner. Keep the process handle so cleanup
    # never signals a PID that has already been reaped and potentially reused.
    return subprocess.Popen(
        [sys.executable, "-I", "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _reap(process):
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


# --------------------------------------------------------------------------
# Process ownership
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_numeric_container_id_is_not_treated_as_a_pid(tower):
    """Regression: a registered container whose id happens to be all digits
    used to be SIGKILLed as a process id."""
    victim = _spawn()
    try:
        tower.register_container(str(victim.pid))
        resp = await tower.kill(KillRequest(container_id=str(victim.pid), reason="numeric id"))
        assert resp.process_terminated is False
        # the process is still alive
        assert victim.poll() is None
    finally:
        _reap(victim)


@pytest.mark.asyncio
async def test_an_unregistered_pid_is_never_signalled(tower):
    victim = _spawn()
    try:
        resp = await tower.kill(KillRequest(container_id=str(victim.pid), reason="not registered"))
        assert resp.process_terminated is False
        assert victim.poll() is None
    finally:
        _reap(victim)


@pytest.mark.asyncio
async def test_a_registered_pid_is_terminated(tower):
    child = _spawn()
    try:
        tower.register_container("c-mine", pid=child.pid)
        resp = await tower.kill(KillRequest(container_id="c-mine", reason="owned"))
        assert resp.process_terminated is True
        child.wait(timeout=5)
    finally:
        _reap(child)


@pytest.mark.asyncio
async def test_ownership_survives_a_restart(tmp_path):
    db = str(tmp_path / "tower.sqlite")
    child = _spawn()
    try:
        t = Tower(db_path=db)
        await t.start()
        _quiet(t)
        t.register_container("c-mine", pid=child.pid)
        await t.stop()

        t2 = Tower(db_path=db)
        await t2.start()
        assert t2.owned_pids().get("c-mine") == child.pid
        resp = await t2.kill(KillRequest(container_id="c-mine", reason="after restart"))
        assert resp.process_terminated is True
        child.wait(timeout=5)
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
        tower.register_container("c-mine", pid=child.pid)
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
    flight.takeoff_at = datetime.now(timezone.utc) - timedelta(seconds=2)
    await tower._progress_flights()
    assert flight.status == FlightStatus.LANDED


@pytest.mark.asyncio
async def test_a_draining_flight_is_terminated_when_the_deadline_passes(tower):
    flight = Flight(task="stubborn job", container_id="c-hard",
                    weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=600))
    await tower.request_clearance(ClearanceRequest(flight=flight))
    await tower.kill(KillRequest(container_id="c-hard", reason="operator", drain_timeout_s=1))
    assert flight.status == FlightStatus.DRAINING
    flight.drain_until = datetime.now(timezone.utc) - timedelta(seconds=1)
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
