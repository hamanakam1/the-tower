"""Declarative policy.

The rules the Tower applies — how a node is scored, when it is warned,
throttled, drained, killed, how long a lease lasts, how many times a flight
may be retried — live in a versioned YAML document, not scattered through
Python constants.

This buys three things:

1. **One place to read.** `the-tower policy` prints the rules in force.
2. **A linter.** `the-tower policy --lint` checks a policy file before it
   is allowed near real hardware.
3. **Versioned decisions.** Every clearance records the policy version it
   was made under, so a decision from last month can be told apart from
   one made today.

`Policy` is a frozen dataclass: loading one is the only way to change the
rules, and the object cannot be mutated afterwards.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional

from .scopes import ScopeMode

POLICY_FORMAT_VERSION = 1


@dataclass(frozen=True)
class ScoringWeights:
    """How much each factor counts when choosing between nodes."""
    thermal: float = 0.35
    load: float = 0.25
    memory: float = 0.25
    power: float = 0.15
    priority: float = 0.05  # per priority step

    def normalised(self) -> "ScoringWeights":
        total = self.thermal + self.load + self.memory + self.power
        if total <= 0:
            raise ValueError("scoring weights must sum to a positive number")
        return self

    def as_dict(self) -> Dict[str, float]:
        return {"thermal": self.thermal, "load": self.load, "memory": self.memory,
                "power": self.power, "priority": self.priority}


@dataclass(frozen=True)
class LadderRules:
    """Where the enforcement ladder's rungs sit."""
    warn_headroom_c: float = 12.0
    throttle_cpu_percent: float = 90.0
    throttle_memory_percent: float = 90.0
    drain_eta_seconds: float = 120.0
    throttle_cpu_factor: float = 0.5
    require_reason_for_kill: bool = True
    drain_before_kill: bool = True

    def as_dict(self) -> Dict[str, Any]:
        return {
            "warn_headroom_c": self.warn_headroom_c,
            "throttle_cpu_percent": self.throttle_cpu_percent,
            "throttle_memory_percent": self.throttle_memory_percent,
            "drain_eta_seconds": self.drain_eta_seconds,
            "throttle_cpu_factor": self.throttle_cpu_factor,
            "require_reason_for_kill": self.require_reason_for_kill,
            "drain_before_kill": self.drain_before_kill,
        }


@dataclass(frozen=True)
class LeaseRules:
    """How long a claim on a node's resources lives."""
    default_ttl_s: float = 300.0
    min_ttl_s: float = 30.0
    max_ttl_s: float = 86400.0

    def as_dict(self) -> Dict[str, Any]:
        return {"default_ttl_s": self.default_ttl_s,
                "min_ttl_s": self.min_ttl_s,
                "max_ttl_s": self.max_ttl_s}


@dataclass(frozen=True)
class Policy:
    """The complete set of rules the Tower applies."""
    version: str = "p-2026.10.1"
    format_version: int = POLICY_FORMAT_VERSION
    source: Optional[str] = None
    scoring: ScoringWeights = field(default_factory=ScoringWeights)
    ladder: LadderRules = field(default_factory=LadderRules)
    leases: LeaseRules = field(default_factory=LeaseRules)
    default_scope_mode: str = ScopeMode.WRITE.value
    max_retries: int = 2
    stale_node_is_unknown: bool = True
    flight_retention_days: int = 30

    def as_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "format_version": self.format_version,
            "scoring": self.scoring.as_dict(),
            "ladder": self.ladder.as_dict(),
            "leases": self.leases.as_dict(),
            "default_scope_mode": self.default_scope_mode,
            "max_retries": self.max_retries,
            "stale_node_is_unknown": self.stale_node_is_unknown,
            "flight_retention_days": self.flight_retention_days,
            "source": self.source,
        }

    def fingerprint(self) -> str:
        """A short hash of the rules, for comparing two policies."""
        import hashlib
        import json
        body = json.dumps(self.as_dict(), sort_keys=True, default=str)
        return hashlib.sha256(body.encode()).hexdigest()[:12]


DEFAULT_POLICY = Policy()


# ---------------------------------------------------------------------------
# Loading and linting
# ---------------------------------------------------------------------------

class PolicyError(ValueError):
    """The policy document is not usable."""


def lint(data: Dict[str, Any]) -> List[str]:
    """Check a policy document. Returns a list of problems (empty = valid)."""
    problems: List[str] = []

    if not isinstance(data, dict):
        return ["policy must be a mapping"]

    fmt = data.get("format_version", POLICY_FORMAT_VERSION)
    if fmt != POLICY_FORMAT_VERSION:
        problems.append(f"format_version {fmt} is not supported (expected {POLICY_FORMAT_VERSION})")

    if not data.get("version"):
        problems.append("version is required; a decision cannot be attributed without one")

    scoring = data.get("scoring")
    if scoring is not None:
        if not isinstance(scoring, dict):
            problems.append("scoring must be a mapping")
        else:
            for key in ("thermal", "load", "memory", "power"):
                v = scoring.get(key)
                if v is not None and (not isinstance(v, (int, float)) or v < 0):
                    problems.append(f"scoring.{key} must be a non-negative number")
            total = sum(float(scoring.get(k, 0) or 0)
                        for k in ("thermal", "load", "memory", "power"))
            if total <= 0:
                problems.append("scoring weights must sum to a positive number")
            p = scoring.get("priority")
            if p is not None and (not isinstance(p, (int, float)) or p < 0):
                problems.append("scoring.priority must be a non-negative number")

    ladder = data.get("ladder")
    if ladder is not None:
        if not isinstance(ladder, dict):
            problems.append("ladder must be a mapping")
        else:
            for key in ("warn_headroom_c", "throttle_cpu_percent",
                        "throttle_memory_percent", "drain_eta_seconds",
                        "throttle_cpu_factor"):
                v = ladder.get(key)
                if v is not None and (not isinstance(v, (int, float)) or v < 0):
                    problems.append(f"ladder.{key} must be a non-negative number")
            factor = ladder.get("throttle_cpu_factor")
            if factor is not None and not (0 < float(factor) <= 1):
                problems.append("ladder.throttle_cpu_factor must be between 0 and 1")

    leases = data.get("leases")
    if leases is not None:
        if not isinstance(leases, dict):
            problems.append("leases must be a mapping")
        else:
            ttl = leases.get("default_ttl_s")
            lo = leases.get("min_ttl_s")
            hi = leases.get("max_ttl_s")
            if ttl is not None and lo is not None and float(ttl) < float(lo):
                problems.append("leases.default_ttl_s is below leases.min_ttl_s")
            if ttl is not None and hi is not None and float(ttl) > float(hi):
                problems.append("leases.default_ttl_s is above leases.max_ttl_s")

    mode = data.get("default_scope_mode")
    if mode is not None and mode not in [m.value for m in ScopeMode]:
        problems.append(f"default_scope_mode must be one of {[m.value for m in ScopeMode]}")

    retries = data.get("max_retries")
    if retries is not None and (not isinstance(retries, int) or retries < 0):
        problems.append("max_retries must be a non-negative integer")

    retention = data.get("flight_retention_days")
    if retention is not None and (not isinstance(retention, int) or retention < 0):
        problems.append("flight_retention_days must be a non-negative integer")

    return problems


def from_dict(data: Dict[str, Any], source: Optional[str] = None) -> Policy:
    """Build a policy from a document, refusing an invalid one."""
    problems = lint(data)
    if problems:
        raise PolicyError("; ".join(problems))
    scoring = data.get("scoring") or {}
    ladder = data.get("ladder") or {}
    leases = data.get("leases") or {}
    weights = ScoringWeights(
        thermal=float(scoring.get("thermal", 0.35)),
        load=float(scoring.get("load", 0.25)),
        memory=float(scoring.get("memory", 0.25)),
        power=float(scoring.get("power", 0.15)),
        priority=float(scoring.get("priority", 0.05)),
    ).normalised()
    return Policy(
        version=str(data.get("version")),
        format_version=int(data.get("format_version", POLICY_FORMAT_VERSION)),
        source=source,
        scoring=weights,
        ladder=LadderRules(
            warn_headroom_c=float(ladder.get("warn_headroom_c", 12.0)),
            throttle_cpu_percent=float(ladder.get("throttle_cpu_percent", 90.0)),
            throttle_memory_percent=float(ladder.get("throttle_memory_percent", 90.0)),
            drain_eta_seconds=float(ladder.get("drain_eta_seconds", 120.0)),
            throttle_cpu_factor=float(ladder.get("throttle_cpu_factor", 0.5)),
            require_reason_for_kill=bool(ladder.get("require_reason_for_kill", True)),
            drain_before_kill=bool(ladder.get("drain_before_kill", True)),
        ),
        leases=LeaseRules(
            default_ttl_s=float(leases.get("default_ttl_s", 300.0)),
            min_ttl_s=float(leases.get("min_ttl_s", 30.0)),
            max_ttl_s=float(leases.get("max_ttl_s", 86400.0)),
        ),
        default_scope_mode=str(data.get("default_scope_mode", ScopeMode.WRITE.value)),
        max_retries=int(data.get("max_retries", 2)),
        stale_node_is_unknown=bool(data.get("stale_node_is_unknown", True)),
        flight_retention_days=int(data.get("flight_retention_days", 30)),
    )


def load_policy(path: Optional[str]) -> Policy:
    """Load a policy from YAML. A missing path yields the default policy."""
    if not path:
        return DEFAULT_POLICY
    p = Path(path).expanduser()
    if not p.exists():
        raise PolicyError(f"policy file not found: {p}")
    import yaml
    with open(p) as f:
        data = yaml.safe_load(f) or {}
    return from_dict(data, source=str(p))


def dump_policy(policy: Policy) -> str:
    """Render a policy back to YAML."""
    import yaml
    data = policy.as_dict()
    data.pop("source", None)
    return yaml.safe_dump(data, sort_keys=False)
