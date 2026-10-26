"""`TOWER_TOKEN` is the only thing standing between the network and the kill
console. It has to guard every path that can change state — including the
WebSocket, which pushes the same data the REST API does — and the dashboard
has to be able to present it, or setting a token bricks the operator UI.
"""
import pytest
from starlette.testclient import TestClient

from the_tower.core import Tower
from the_tower.server import TowerServer

TOKEN = "s3cret"


@pytest.fixture
def guarded(monkeypatch):
    monkeypatch.setenv("TOWER_TOKEN", TOKEN)
    tower = Tower()
    client = TestClient(TowerServer(tower).app)
    yield client


def test_a_write_without_the_token_is_refused(guarded):
    r = guarded.post("/nodes/m3max/enforcement", json={"mode": "enforce"})
    assert r.status_code == 401
    assert "unauthorized" in r.json()["error"]


def test_a_write_with_the_token_is_accepted(guarded):
    r = guarded.post("/nodes/m3max/enforcement", json={"mode": "enforce"},
                     headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.json()["node"]["spec"]["enforcement"] == "enforce"


def test_a_wrong_token_is_refused(guarded):
    r = guarded.post("/nodes/m3max/enforcement", json={"mode": "enforce"},
                     headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_a_bare_token_without_the_bearer_prefix_is_refused(guarded):
    r = guarded.post("/nodes/m3max/enforcement", json={"mode": "advisory"},
                     headers={"Authorization": TOKEN})
    assert r.status_code == 401


def test_the_kill_console_is_behind_the_token(guarded):
    r = guarded.post("/kill/ctr-1", json={"reason": "operator"})
    assert r.status_code == 401


def test_the_websocket_refuses_a_client_without_the_token(guarded):
    # A socket that is accepted has already been handed the airspace, so the
    # check has to happen before accept.
    with pytest.raises(Exception):
        with guarded.websocket_connect("/ws") as ws:
            ws.receive_json()


def test_the_websocket_accepts_the_token_as_a_query_parameter(guarded):
    # A browser cannot set headers on a WebSocket handshake; the token has to
    # be able to travel in the query string.
    with guarded.websocket_connect(f"/ws?token={TOKEN}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "airspace"


def test_heartbeat_is_a_write_and_needs_the_token(guarded):
    r = guarded.post("/containers/ctr-1/heartbeat")
    assert r.status_code == 401
