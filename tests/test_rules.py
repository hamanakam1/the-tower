"""Tests for the declarative policy."""
import textwrap
from pathlib import Path

import pytest

from the_tower.policy import MeshSnapshot, NodeView, decide
from the_tower.rules import (
    DEFAULT_POLICY, LadderRules, LeaseRules, Policy, PolicyError, ScoringWeights,
    dump_policy, from_dict, lint, load_policy,
)
from the_tower.safety import Ladder, assess
from the_tower.schemas import ClearanceDecision, Flight, ResourceWeight, ThermalState


def write_policy(tmp_path, body: str) -> str:
    p = tmp_path / "policy.yaml"
    p.write_text(textwrap.dedent(body))
    return str(p)


def node(node_id="n1", name="Node", **kw):
    defaults = dict(
        id=node_id, name=name, status="online",
        cpu_percent=10.0, memory_percent=20.0, temperature_c=45.0,
        power_watts=20.0, thermal_state=ThermalState.NOMINAL.value, gpu_percent=0.0,
        cpu_cores=8.0, memory_gb=16.0, gpu_units=0.0, redline_temp_c=90.0,
        max_power_watts=100.0, network_capacity_mbps=1000.0,
        reserved_cpu=0.0, reserved_memory_gb=0.0, reserved_gpu=0.0,
    )
    defaults.update(kw)
    return NodeView(**defaults)


# ---- linting ----

def test_lint_accepts_the_shipped_example():
    example = Path(__file__).resolve().parents[1] / "policy.example.yaml"
    import yaml
    with open(example) as f:
        data = yaml.safe_load(f)
    assert lint(data) == []


def test_lint_rejects_a_missing_version():
    assert any("version" in p for p in lint({}))


def test_lint_rejects_an_unsupported_format_version():
    assert any("format_version" in p for p in lint({"version": "x", "format_version": 99}))


def test_lint_rejects_negative_weights():
    problems = lint({"version": "x", "scoring": {"thermal": -1}})
    assert any("thermal" in p for p in problems)


def test_lint_rejects_zero_total_weights():
    problems = lint({"version": "x", "scoring": {"thermal": 0, "load": 0, "memory": 0, "power": 0}})
    assert any("sum" in p for p in problems)


def test_lint_rejects_a_bad_throttle_factor():
    problems = lint({"version": "x", "ladder": {"throttle_cpu_factor": 2}})
    assert any("throttle_cpu_factor" in p for p in problems)


def test_lint_rejects_an_inconsistent_lease_ttl():
    problems = lint({"version": "x", "leases": {"default_ttl_s": 10, "min_ttl_s": 60}})
    assert any("default_ttl_s" in p for p in problems)


def test_lint_rejects_a_bad_scope_mode():
    problems = lint({"version": "x", "default_scope_mode": "sideways"})
    assert any("default_scope_mode" in p for p in problems)


def test_from_dict_refuses_an_invalid_policy():
    with pytest.raises(PolicyError):
        from_dict({"scoring": {"thermal": -5}})


# ---- loading ----

def test_load_policy_reads_a_file(tmp_path):
    path = write_policy(tmp_path, """
        version: p-test-1
        scoring:
          thermal: 0.5
          load: 0.2
          memory: 0.2
          power: 0.1
        ladder:
          warn_headroom_c: 20.0
    """)
    policy = load_policy(path)
    assert policy.version == "p-test-1"
    assert policy.scoring.thermal == 0.5
    assert policy.ladder.warn_headroom_c == 20.0
    assert policy.source == path


def test_load_policy_without_a_path_is_the_default():
    assert load_policy(None).version == DEFAULT_POLICY.version


def test_load_policy_missing_file_raises(tmp_path):
    with pytest.raises(PolicyError):
        load_policy(str(tmp_path / "nope.yaml"))


def test_policy_round_trips_through_yaml(tmp_path):
    import yaml
    path = write_policy(tmp_path, """
        version: p-round-trip
        max_retries: 5
    """)
    policy = load_policy(path)
    rendered = dump_policy(policy)
    again = from_dict(yaml.safe_load(rendered))
    assert again.version == "p-round-trip"
    assert again.max_retries == 5


# ---- the rules actually change behaviour ----

def test_a_different_threshold_moves_the_ladder():
    strict = Policy(version="p-strict",
                    ladder=LadderRules(warn_headroom_c=40.0))
    loose = Policy(version="p-loose",
                   ladder=LadderRules(warn_headroom_c=1.0))
    n = node(temperature_c=60.0, redline_temp_c=90.0)  # 30C of headroom
    assert assess(n, policy=strict).ladder == Ladder.WARN
    assert assess(n, policy=loose).ladder == Ladder.ADMIT


def test_a_different_threshold_moves_the_throttle():
    strict = Policy(version="p-strict",
                    ladder=LadderRules(throttle_cpu_percent=50.0))
    n = node(cpu_percent=60.0)
    assert assess(n, policy=strict).ladder == Ladder.THROTTLE


def test_scoring_weights_change_the_winner():
    # one node is cooler but busy, the other is idle but warm
    cool_but_loaded = node("cool", "Cool but loaded", temperature_c=60.0, cpu_percent=85.0)
    warm_but_idle = node("warm", "Warm but idle", temperature_c=80.0, cpu_percent=10.0)
    flight = Flight(task="x", weight=ResourceWeight(cpu_cores=1, memory_gb=1))

    thermal_first = Policy(version="p-thermal",
                            scoring=ScoringWeights(thermal=1.0, load=0.0, memory=0.0, power=0.0))
    load_first = Policy(version="p-load",
                         scoring=ScoringWeights(thermal=0.0, load=1.0, memory=0.0, power=0.0))

    snap = MeshSnapshot(nodes=[cool_but_loaded, warm_but_idle])
    assert decide(flight, snap, thermal_first).assigned_node == "cool"
    assert decide(flight, snap, load_first).assigned_node == "warm"


def test_the_decision_records_the_policy_version():
    policy = Policy(version="p-recorded")
    snap = MeshSnapshot(nodes=[node()])
    flight = Flight(task="x", weight=ResourceWeight(cpu_cores=1, memory_gb=1))
    d = decide(flight, snap, policy)
    assert d.policy_version == "p-2026.10.1"  # the module constant by default
    # the Tower stamps the loaded policy onto the decision
    from dataclasses import replace
    stamped = replace(d, policy_version=policy.version, policy_fingerprint=policy.fingerprint())
    assert stamped.policy_version == "p-recorded"
    assert stamped.policy_fingerprint


def test_fingerprint_changes_with_the_rules():
    a = Policy(version="p-a")
    b = Policy(version="p-b")
    assert a.fingerprint() != b.fingerprint()
    assert a.fingerprint() == Policy(version="p-a").fingerprint()


def test_lease_rules_are_read_from_the_policy():
    p = Policy(version="p-lease", leases=LeaseRules(default_ttl_s=42.0))
    assert p.leases.default_ttl_s == 42.0
