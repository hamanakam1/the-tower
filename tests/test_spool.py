"""Tests for the on-disk telemetry spool."""
import json

import pytest

from the_tower.spool import Spool


def test_append_and_count(tmp_path):
    s = Spool(str(tmp_path / "spool.jsonl"))
    s.append({"cpu_percent": 10})
    s.append({"cpu_percent": 20})
    assert s.count() == 2


def test_peek_does_not_remove(tmp_path):
    s = Spool(str(tmp_path / "spool.jsonl"))
    s.append({"n": 1})
    assert len(s.peek()) == 1
    assert s.count() == 1


def test_pop_removes_oldest_first(tmp_path):
    s = Spool(str(tmp_path / "spool.jsonl"))
    s.append({"n": 1})
    s.append({"n": 2})
    out = s.pop(1)
    assert out[0]["payload"]["n"] == 1
    assert s.count() == 1


def test_drain_delivers_in_order(tmp_path):
    s = Spool(str(tmp_path / "spool.jsonl"))
    for i in range(5):
        s.append({"n": i})
    seen = []
    delivered = s.drain(lambda p: seen.append(p["n"]) or True, limit=10)
    assert delivered == 5
    assert seen == [0, 1, 2, 3, 4]
    assert s.count() == 0


def test_drain_stops_at_the_first_refusal(tmp_path):
    s = Spool(str(tmp_path / "spool.jsonl"))
    for i in range(5):
        s.append({"n": i})
    seen = []

    def deliver(p):
        seen.append(p["n"])
        return p["n"] < 2

    delivered = s.drain(deliver, limit=10)
    assert delivered == 2
    assert seen == [0, 1, 2]
    # the rest is still spooled, in order
    assert [e["payload"]["n"] for e in s.peek()] == [2, 3, 4]


def test_drain_survives_a_raising_deliverable(tmp_path):
    s = Spool(str(tmp_path / "spool.jsonl"))
    s.append({"n": 1})

    def deliver(p):
        raise RuntimeError("network down")

    assert s.drain(deliver, limit=10) == 0
    assert s.count() == 1


def test_spool_is_bounded(tmp_path):
    s = Spool(str(tmp_path / "spool.jsonl"), max_samples=10)
    for i in range(25):
        s.append({"n": i})
    assert s.count() == 10
    # the oldest were dropped, the newest kept
    assert s.peek()[0]["payload"]["n"] == 15


def test_clear_removes_the_file(tmp_path):
    s = Spool(str(tmp_path / "spool.jsonl"))
    s.append({"n": 1})
    s.clear()
    assert s.count() == 0
    assert s.size_bytes() == 0


def test_spool_file_is_json_lines(tmp_path):
    s = Spool(str(tmp_path / "spool.jsonl"))
    s.append({"n": 1})
    lines = [l for l in open(s.path).read().splitlines() if l.strip()]
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["payload"] == {"n": 1}
    assert "ts" in entry


def test_corrupt_lines_are_skipped(tmp_path):
    s = Spool(str(tmp_path / "spool.jsonl"))
    s.append({"n": 1})
    with open(s.path, "a") as f:
        f.write("not json\n")
    assert s.count() == 1
    assert len(s.peek()) == 1


def test_empty_spool_drains_nothing(tmp_path):
    s = Spool(str(tmp_path / "spool.jsonl"))
    assert s.drain(lambda p: True, limit=10) == 0
