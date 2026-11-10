"""Tests for the self-verification report."""
import pytest

from the_tower.core import Tower
from the_tower.verify import verify


@pytest.fixture
async def tower():
    t = Tower()
    await t.start()
    yield t
    await t.stop()


@pytest.mark.asyncio
async def test_a_healthy_tower_verifies(tower):
    report = verify(tower)
    assert report.ok is True
    names = [c.name for c in report.checks]
    assert "audit chain" in names
    assert "admission policy" in names
    assert "node freshness" in names


@pytest.mark.asyncio
async def test_report_renders():
    t = Tower()
    await t.start()
    report = verify(t)
    rendered = report.render()
    assert "VERIFIED" in rendered or "NOT VERIFIED" in rendered
    await t.stop()


@pytest.mark.asyncio
async def test_a_tampered_audit_chain_fails(tmp_path):
    db = str(tmp_path / "tampered.sqlite")
    t = Tower(db_path=db)
    await t.start()
    # write a chain, then break a link in it
    t.storage.append_audit({"n": 1})
    t.storage.append_audit({"n": 2})
    assert t.storage.verify_audit_chain() is True
    t.storage.conn.execute("UPDATE audit SET previous_hash='tampered' "
                            "WHERE id = (SELECT id FROM audit ORDER BY rowid LIMIT 1 OFFSET 1)")
    t.storage.conn.commit()
    report = verify(t)
    assert report.ok is False
    assert any(c.name == "audit chain" and not c.ok for c in report.checks)
    await t.stop()


@pytest.mark.asyncio
async def test_a_stale_node_is_reported():
    from datetime import datetime, timedelta, timezone
    t = Tower()
    await t.start()
    node = t.get_node("m3max")
    node.last_seen = datetime.now(timezone.utc) - timedelta(seconds=600)
    report = verify(t)
    stale = [c for c in report.checks if c.name == "node freshness"]
    assert stale and stale[0].ok is False
    # a stale node is a warning, not a failure: it does not stop the tower
    assert report.ok is True
    await t.stop()


@pytest.mark.asyncio
async def test_a_stranded_flight_is_reported():
    from the_tower.schemas import Flight, ResourceWeight, ClearanceRequest
    t = Tower()
    await t.start()
    f = Flight(task="stranded", weight=ResourceWeight(cpu_cores=1, memory_gb=1, duration_est_s=600))
    await t.request_clearance(ClearanceRequest(flight=f))
    # pretend the node forgot about it
    node = t.get_node(f.assigned_node)
    node.active_flights = [x for x in node.active_flights if x != f.id]
    report = verify(t)
    check = [c for c in report.checks if c.name == "in-flight flights"][0]
    assert check.ok is False
    assert f.callsign in check.detail
    await t.stop()


@pytest.mark.asyncio
async def test_report_is_json_serialisable():
    import json
    t = Tower()
    await t.start()
    report = verify(t)
    payload = json.dumps(report.to_dict())
    assert "checks" in payload
    await t.stop()
