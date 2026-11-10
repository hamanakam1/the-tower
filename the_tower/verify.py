"""Self-verification.

Before pointing the Tower at a real machine, an operator needs to know that
the things the Tower relies on are actually working:

- the audit chain has not been tampered with,
- the policy in force is valid,
- the database schema matches the code,
- the nodes are reporting, and reporting recently,
- no lease or container is stranded.

`verify()` reads the Tower's own state and reports what it finds. It never
changes anything: a verification that repairs things is not a verification.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .schemas import FlightStatus, NodeStatus


@dataclass
class Check:
    """One thing that was verified."""
    name: str
    ok: bool
    detail: str = ""
    severity: str = "error"   # error | warning | info

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail,
                "severity": self.severity}


@dataclass
class Report:
    checks: List[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing failed at error severity."""
        return all(c.ok for c in self.checks if c.severity == "error")

    @property
    def failures(self) -> List[Check]:
        return [c for c in self.checks if not c.ok and c.severity == "error"]

    @property
    def warnings(self) -> List[Check]:
        return [c for c in self.checks if not c.ok and c.severity == "warning"]

    def add(self, name: str, ok: bool, detail: str = "", severity: str = "error"):
        self.checks.append(Check(name=name, ok=ok, detail=detail, severity=severity))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [c.to_dict() for c in self.checks],
            "failures": len(self.failures),
            "warnings": len(self.warnings),
        }

    def render(self) -> str:
        lines = []
        for c in self.checks:
            mark = "✓" if c.ok else ("✗" if c.severity == "error" else "!")
            lines.append(f"  {mark} {c.name}" + (f" — {c.detail}" if c.detail else ""))
        lines.append("")
        lines.append("VERIFIED" if self.ok else "NOT VERIFIED")
        if self.warnings:
            lines.append(f"({len(self.warnings)} warning(s))")
        return "\n".join(lines)


def verify(tower) -> Report:
    """Run every check against a running Tower."""
    report = Report()

    # --- the audit chain ---
    if tower.storage is None:
        report.add("audit chain", True, "no database configured; nothing to verify",
                   severity="warning")
    else:
        try:
            valid = tower.storage.verify_audit_chain()
            report.add("audit chain", bool(valid),
                       "the hash chain verifies" if valid else
                       "the hash chain does NOT verify — the ledger may have been tampered with")
        except Exception as e:
            report.add("audit chain", False, f"could not verify: {e}")

    # --- the schema ---
    if tower.storage is not None:
        try:
            from .storage import MIGRATIONS
            expected = max(v for v, _, _ in MIGRATIONS)
            actual = tower.storage.schema_version()
            report.add("database schema", actual >= expected,
                       f"schema version {actual} (code expects at least {expected})")
        except Exception as e:
            report.add("database schema", False, f"could not read the schema version: {e}")

    # --- the policy ---
    try:
        from .rules import lint
        problems = lint(tower.policy.as_dict())
        report.add("admission policy", not problems,
                   "the policy in force is valid" if not problems else "; ".join(problems))
    except Exception as e:
        report.add("admission policy", False, f"could not check the policy: {e}")

    # --- node freshness ---
    now = datetime.now(timezone.utc)
    unknown = [n for n in tower.nodes.values() if n.status == NodeStatus.UNKNOWN]
    stale = []
    for n in tower.nodes.values():
        if n.last_seen and (now - n.last_seen).total_seconds() > n.spec.staleness_budget_s:
            stale.append(n.spec.id)
    report.add("node freshness", not stale,
               "every node is reporting" if not stale else
               f"no telemetry from: {', '.join(stale)}",
               severity="warning" if stale else "error")
    if unknown:
        report.add("unknown nodes", False,
                   f"{len(unknown)} node(s) are unknown: {', '.join(n.spec.id for n in unknown)}",
                   severity="warning")

    # --- telemetry provenance ---
    lying = [n.spec.id for n in tower.nodes.values()
             if n.telemetry.source == "simulated" and n.spec.sampler != "simulated"]
    report.add("telemetry provenance", not lying,
               "every node's telemetry says where it came from" if not lying else
               f"these nodes report simulated telemetry but are not simulated: {', '.join(lying)}",
               severity="warning" if lying else "error")

    # --- stranded leases ---
    stranded = [l for l in tower.leases.values() if l.status == "active" and l.is_expired(now)]
    report.add("leases", not stranded,
               "every lease is current" if not stranded else
               f"{len(stranded)} lease(s) have expired and not been reaped",
               severity="warning" if stranded else "error")

    # --- stranded flights ---
    missing = []
    for f in tower.flights.values():
        if f.status != FlightStatus.IN_FLIGHT:
            continue
        node = tower.get_node(f.assigned_node) if f.assigned_node else None
        if node is None or f.id not in node.active_flights:
            missing.append(f.callsign)
    report.add("in-flight flights", not missing,
               "every in-flight flight holds its node" if not missing else
               f"these flights are in the air but not on any node: {', '.join(missing)}",
               severity="warning" if missing else "error")

    # --- the tower itself ---
    report.add("tower", True, f"{len(tower.nodes)} node(s), {len(tower.flights)} flight(s), "
                               f"{len(tower.containers)} container(s)")

    return report
