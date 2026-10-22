import pytest
from starlette.testclient import TestClient
from the_tower.core import Tower
from the_tower.server import TowerServer
import asyncio


@pytest.fixture
def client():
    tower = Tower()
    asyncio.run(tower.start())
    srv = TowerServer(tower)
    c = TestClient(srv.app)
    yield c
    asyncio.run(tower.stop())


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_radar(client):
    r = client.get("/radar")
    assert r.status_code == 200
    assert "nodes" in r.json()
    assert len(r.json()["nodes"]) == 3


def test_status_node(client):
    r = client.get("/status/m3max")
    assert r.status_code == 200
    assert r.json()["node"]["spec"]["id"] == "m3max"


def test_status_404(client):
    r = client.get("/status/nope")
    assert r.status_code == 404


def test_clearance_endpoint(client):
    r = client.post("/clearance", json={"task": "hello", "weight": {"cpu_cores": 1, "memory_gb": 2, "duration_est_s": 5}})
    assert r.status_code == 200
    data = r.json()
    assert data["decision"] in ("cleared", "queued", "shot_down", "denied")
    assert "flight_id" in data


def test_airspace(client):
    r = client.get("/airspace")
    assert r.status_code == 200
    assert "stats" in r.json()


def test_kill_endpoint(client):
    # create a flight with container
    r = client.post("/clearance", json={"task": "x", "container_id": "ctr-9", "weight": {"cpu_cores": 1, "memory_gb": 1, "duration_est_s": 100}})
    assert r.status_code == 200
    r2 = client.post("/kill/ctr-9", json={"reason": "test"})
    assert r2.status_code == 200
    assert r2.json()["success"] is True


def test_root_dashboard(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "THE TOWER" in r.text
