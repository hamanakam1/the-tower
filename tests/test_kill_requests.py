"""Destructive requests must validate before any flight or process changes."""
import asyncio
import signal
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from starlette.testclient import TestClient

from the_tower.core import Tower
from the_tower.schemas import ClearanceRequest, Flight, FlightStatus, KillRequest
from the_tower.server import TowerServer


@pytest.fixture
def target(monkeypatch):
    tower = Tower()
    flight = Flight(task="finish safely", container_id="owned-job")
    asyncio.run(tower.request_clearance(ClearanceRequest(flight=flight)))
    assert flight.status == FlightStatus.IN_FLIGHT
    tower.register_container("owned-job", flight_id=flight.id,
                             node_id=flight.assigned_node, pid=987654)
    kill = Mock()
    monkeypatch.setattr("os.kill", kill)
    return tower, flight, kill


@pytest.mark.parametrize("body", [
    "", "{", "null", "[]", '"kill"',
    '{"reason": null}', '{"reason": 123}',
    '{"reason": "test", "drain_timeout_s": -1}',
    '{"reason": "test", "drain_timeout_s": "Infinity"}',
    '{"reason": "test", "drain_timeout_s": 1e300}',
    '{"reason": "test", "drain_first": "false"}',
    '{"reason": "test", "force": "false"}',
    '{"reason": "test", "drain_timeot_s": 60}',
])
def test_invalid_http_kill_cannot_mutate_a_target(target, body):
    tower, flight, kill = target
    with TestClient(TowerServer(tower).app) as client:
        response = client.post("/kill/owned-job", content=body,
                               headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"]
    assert flight.status == FlightStatus.IN_FLIGHT
    assert tower.containers["owned-job"]["status"] == "running"
    kill.assert_not_called()


@pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "   "}])
def test_absent_or_blank_reason_is_refused(target, body):
    tower, flight, kill = target
    with TestClient(TowerServer(tower).app) as client:
        response = client.post("/kill/owned-job", json=body)
    assert response.status_code == 200
    assert response.json()["success"] is False
    assert flight.status == FlightStatus.IN_FLIGHT
    kill.assert_not_called()


def test_direct_kill_without_a_reason_is_refused(target):
    tower, flight, kill = target
    response = asyncio.run(tower.kill(KillRequest(container_id="owned-job")))
    assert response.success is False
    assert flight.status == FlightStatus.IN_FLIGHT
    kill.assert_not_called()


def test_owned_process_survives_grace_and_is_signalled_at_deadline(target):
    tower, flight, kill = target

    async def run():
        response = await tower.kill(KillRequest(
            container_id="owned-job", reason="operator maintenance", drain_timeout_s=30))
        assert response.draining is True
        assert response.process_terminated is False
        assert flight.status == FlightStatus.DRAINING
        assert flight.id in tower.nodes[flight.assigned_node].active_weights
        kill.assert_not_called()
        await tower._progress_flights()
        kill.assert_not_called()
        flight.drain_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        await tower._progress_flights()
        assert flight.status == FlightStatus.SHOT_DOWN
        assert tower.containers["owned-job"]["status"] == "killed"
        assert flight.id not in tower.nodes[flight.assigned_node].active_weights
        kill.assert_called_once_with(987654, signal.SIGKILL)
        await tower._progress_flights()
        kill.assert_called_once()

    asyncio.run(run())


def test_graceful_landing_does_not_signal_owned_process(target):
    tower, flight, kill = target

    async def run():
        await tower.kill(KillRequest(container_id="owned-job", reason="maintenance",
                                     drain_timeout_s=30))
        flight.takeoff_at = datetime.now(timezone.utc) - timedelta(seconds=61)
        await tower._progress_flights()
        assert flight.status == FlightStatus.LANDED
        kill.assert_not_called()

    asyncio.run(run())


def test_retried_grace_request_keeps_the_original_deadline(target):
    tower, flight, kill = target

    async def run():
        request = KillRequest(container_id="owned-job", reason="maintenance",
                              drain_timeout_s=30)
        first = await tower.kill(request)
        deadline = flight.drain_until
        again = await tower.kill(request)
        assert first.draining is True
        assert again.draining is True
        assert again.process_terminated is False
        assert flight.drain_until == deadline
        assert flight.status == FlightStatus.DRAINING
        kill.assert_not_called()
        immediate = await tower.kill(KillRequest(
            container_id="owned-job", reason="stop immediately", drain_timeout_s=0))
        assert immediate.process_terminated is True
        assert flight.status == FlightStatus.SHOT_DOWN
        kill.assert_called_once_with(987654, signal.SIGKILL)

    asyncio.run(run())
