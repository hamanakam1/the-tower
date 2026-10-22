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


def test_register_container(client):
    r = client.post("/containers", json={"container_id": "c-1", "node_id": "m3max"})
    assert r.status_code == 200
    r2 = client.post("/containers/c-1/heartbeat")
    assert r2.status_code == 200
    r3 = client.get("/containers")
    assert r3.status_code == 200
    assert len(r3.json()["containers"]) >= 1


def test_register_node_endpoint(client):
    spec = {"id":"n2","name":"N2","role":"test","specs":"x","constraints":"y",
            "cpu_cores":2,"memory_gb":4,"redline_temp_c":90,"max_power_watts":30,
            "network_capacity_mbps":100}
    r = client.post("/nodes", json=spec)
    assert r.status_code == 200
    r2 = client.delete("/nodes/n2")
    assert r2.status_code == 200


def test_audit_verify(client):
    r = client.get("/audit/verify")
    assert r.status_code == 200
    assert "valid" in r.json()


def test_metrics(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "tower_nodes_total" in r.text


def test_flight_detail(client):
    r = client.post("/clearance", json={"task": "x", "weight": {"cpu_cores": 1, "memory_gb": 1, "duration_est_s": 5}})
    fid = r.json()["flight_id"]
    r2 = client.get(f"/flights/{fid}")
    assert r2.status_code == 200
    assert r2.json()["flight"]["id"] == fid


def test_clearance_v2_returns_202_and_admission_block(client):
    r = client.post("/clearance/v2", json={
        "idempotency_key": "k-1",
        "task": "distill",
        "scope": ["model/x"],
        "priority": 1,
        "weight": {"cpu_cores": 1, "memory_gb": 2, "duration_est_s": 10},
    })
    assert r.status_code in (200, 202)
    body = r.json()
    assert body["decision"] in ("cleared", "queued", "shot_down")
    assert body["admission"]["policy_version"]
    assert "scored" in body["admission"]


def test_clearance_v2_is_idempotent(client):
    payload = {"idempotency_key": "k-2", "task": "retry me",
               "weight": {"cpu_cores": 1, "memory_gb": 2, "duration_est_s": 10}}
    r1 = client.post("/clearance/v2", json=payload)
    r2 = client.post("/clearance/v2", json=payload)
    assert r1.json()["flight_id"] == r2.json()["flight_id"]


def test_last_decision_endpoint(client):
    client.post("/clearance", json={"task": "x", "weight": {"cpu_cores": 1, "memory_gb": 1}})
    r = client.get("/decision/last")
    assert r.status_code == 200
    assert r.json()["decision"] in ("cleared", "queued", "shot_down", "denied")


def test_safety_ladder_endpoint(client):
    r = client.get("/safety/ladder")
    assert r.status_code == 200
    assert "nodes" in r.json()
    assert "counts" in r.json()


def test_replay_endpoint_reproduces_a_decision(client):
    r = client.post("/clearance", json={"task": "replayable",
                                        "weight": {"cpu_cores": 1, "memory_gb": 1}})
    assert r.status_code == 200
    admission = r.json()["admission"]
    # rebuild the recorded snapshot from the live mesh is not possible
    # without storage, so replay against an explicit snapshot
    snap = {"nodes": [], "in_flight": [], "queued_count": 0}
    r2 = client.post("/replay", json={
        "flight": {"task": "replayable",
                   "weight": {"cpu_cores": 1, "memory_gb": 1, "duration_est_s": 5}},
        "snapshot": snap,
        "admission": admission,
    })
    assert r2.status_code == 200
    assert "replayed" in r2.json()
    assert "comparison" in r2.json()


def test_scope_conflict_survives_the_http_shorthand(client):
    """Roadmap 0.3: the shorthand body must forward `scope`."""
    first = client.post("/clearance", json={
        "task": "holder", "scope": ["data/train"],
        "weight": {"cpu_cores": 1, "memory_gb": 1, "duration_est_s": 600},
    })
    assert first.status_code == 200
    second = client.post("/clearance", json={
        "task": "contender", "scope": ["data/train"], "priority": 2,
        "weight": {"cpu_cores": 1, "memory_gb": 1, "duration_est_s": 5},
    })
    assert second.status_code == 200
    assert second.json()["decision"] == "queued"
    assert "scope conflict" in second.json()["reason"].lower()


def test_gpu_units_survive_the_http_shorthand(client):
    r = client.post("/clearance", json={
        "task": "gpu job", "weight": {"cpu_cores": 1, "memory_gb": 1,
                                       "gpu_units": 2, "duration_est_s": 5}})
    assert r.status_code == 200
    fid = r.json()["flight_id"]
    detail = client.get(f"/flights/{fid}")
    assert detail.json()["flight"]["weight"]["gpu_units"] == 2


def test_constraints_survive_the_http_shorthand(client):
    r = client.post("/clearance", json={
        "task": "pinned", "constraints": {"preferred_node": "pi-cluster"},
        "weight": {"cpu_cores": 1, "memory_gb": 1, "duration_est_s": 5}})
    assert r.status_code == 200
    fid = r.json()["flight_id"]
    detail = client.get(f"/flights/{fid}")
    assert detail.json()["flight"]["preferred_node"] == "pi-cluster"


def test_kill_requires_a_reason(client):
    r = client.post("/kill/ctr-noreason", json={"reason": ""})
    assert r.status_code == 200
    assert r.json()["success"] is False
    assert "reason" in r.json()["message"].lower()


def test_kill_refuses_an_unowned_pid(client):
    # a numeric container id that the Tower never registered is not ours
    r = client.post("/kill/999999", json={"reason": "test"})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True  # the flight record is closed...
    assert "proc terminated" not in body["message"]  # ...but no signal was sent


def test_node_state_endpoint(client):
    r = client.post("/nodes/m3max/state", json={"state": "draining"})
    assert r.status_code == 200
    assert r.json()["node"]["status"] == "draining"
    r2 = client.post("/nodes/m3max/state", json={"state": "nonsense"})
    assert r2.status_code == 400
