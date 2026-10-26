"""Admission policy — the pure decision core of The Tower.

Everything in this module is a pure function of observed state. No I/O, no
clock, no randomness, no mutation. Given the same snapshot and the same
request, `decide()` always returns the same decision.

The impure half — mutating flights, emitting events, persisting rows — lives
in `core.py` and is driven by the `Decision` produced here.

This split is what makes admission:

* **testable** without a database or a running server,
* **replayable** against recorded telemetry,
* **reviewable** by a human: the decision carries the node state it saw, the
  score of every candidate, and a human-readable reason.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .schemas import (
    ClearanceDecision, Flight, FlightPriority, FlightStatus, NodeStatus,
    ResourceWeight, ThermalState,
)

# Bumped when the admission rules change. Recorded with every decision so a
# decision made by an older Tower can be told apart from a newer one.
POLICY_VERSION = "p-2026.10.1"


# ============================================================================
# Snapshot — the observed world, frozen
# ============================================================================

@dataclass(frozen=True)
class NodeView:
    """An immutable view of one node at decision time."""
    id: str
    name: str
    status: str
    cpu_percent: float
    memory_percent: float
    temperature_c: float
    power_watts: float
    thermal_state: str
    gpu_percent: Optional[float]
    cpu_cores: float
    memory_gb: float
    gpu_units: float
    redline_temp_c: float
    max_power_watts: float
    network_capacity_mbps: float
    reserved_cpu: float
    reserved_memory_gb: float
    reserved_gpu: float
    telemetry_age_s: Optional[float] = None

    def free(self) -> Dict[str, float]:
        mem_free = self.memory_gb * (1 - self.memory_percent / 100.0) - self.reserved_memory_gb
        cpu_free = self.cpu_cores * (1 - self.cpu_percent / 100.0) - self.reserved_cpu
        gpu_free = self.gpu_units * (1 - (self.gpu_percent or 0.0) / 100.0) - self.reserved_gpu
        return {
            "cpu": max(0.0, cpu_free),
            "memory_gb": max(0.0, mem_free),
            "gpu": max(0.0, gpu_free),
        }

    def accepts(self, w: ResourceWeight) -> bool:
        """Would this node safely take this flight right now?"""
        if self.status in (NodeStatus.OFFLINE.value, NodeStatus.REDLINE.value,
                           NodeStatus.UNKNOWN.value, NodeStatus.DRAINING.value,
                           NodeStatus.CORDONED.value, NodeStatus.MAINTENANCE.value):
            return False
        if self.thermal_state == ThermalState.CRITICAL.value:
            return False
        if self.temperature_c >= self.redline_temp_c:
            return False
        free = self.free()
        if w.cpu_cores > free["cpu"] + 1e-6:
            return False
        if w.memory_gb > free["memory_gb"] + 1e-6:
            return False
        if w.gpu_units > free["gpu"] + 1e-6:
            return False
        if self.power_watts + w.cpu_cores * 8.0 > self.max_power_watts * 1.05:
            return False
        return True

    def score(self, flight: Flight) -> float:
        """Higher is better. Blends thermal, load, memory and power headroom."""
        free = self.free()
        temp_headroom = (self.redline_temp_c - self.temperature_c) / self.redline_temp_c
        load_factor = 1.0 - (self.cpu_percent / 100.0)
        mem_factor = free["memory_gb"] / self.memory_gb if self.memory_gb else 0.0
        power_headroom = 1.0 - (self.power_watts / self.max_power_watts)
        priority_boost = (4 - int(flight.priority)) * 0.05
        return (temp_headroom * 0.35 + load_factor * 0.25 + mem_factor * 0.25 +
                power_headroom * 0.15 + priority_boost)

    def state_at_decision(self) -> Dict[str, Any]:
        return {
            "temp_c": self.temperature_c,
            "thermal_state": self.thermal_state,
            "cpu_percent": self.cpu_percent,
            "memory_percent": self.memory_percent,
            "power_watts": self.power_watts,
            "status": self.status,
            "telemetry_age_s": self.telemetry_age_s,
        }


@dataclass(frozen=True)
class ScopeView:
    """An in-flight flight that claims one or more scope keys."""
    flight_id: str
    callsign: str
    scope: List[str]
    priority: int
    node_id: Optional[str]
    assigned_node: Optional[str] = None
    cpu_cores: float = 0.0
    memory_gb: float = 0.0
    gpu_units: float = 0.0


@dataclass(frozen=True)
class MeshSnapshot:
    """The whole observed world at one instant."""
    nodes: List[NodeView] = field(default_factory=list)
    in_flight: List[ScopeView] = field(default_factory=list)
    queued_count: int = 0

    def node(self, node_id: str) -> Optional[NodeView]:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    def scope_conflict(self, flight: Flight) -> Optional[ScopeView]:
        """First in-flight flight sharing a scope key with `flight`."""
        if not flight.scope:
            return None
        keys = set(flight.scope)
        for f in self.in_flight:
            if keys & set(f.scope):
                return f
        return None


# ============================================================================
# Decision
# ============================================================================

@dataclass(frozen=True)
class Decision:
    """The outcome of a pure admission decision."""
    decision: ClearanceDecision
    reason: str
    assigned_node: Optional[str] = None
    estimated_wait_s: Optional[float] = None
    policy_version: str = POLICY_VERSION
    scored: Dict[str, float] = field(default_factory=dict)
    node_state_at_decision: Dict[str, Any] = field(default_factory=dict)
    scope_conflict: Optional[str] = None
    preempt_flight_id: Optional[str] = None
    considered_nodes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "decision": self.decision.value,
            "assigned_node": self.assigned_node,
            "reason": self.reason,
            "estimated_wait_s": self.estimated_wait_s,
            "scored": self.scored,
            "node_state_at_decision": self.node_state_at_decision,
            "scope_conflict": self.scope_conflict,
            "considered_nodes": self.considered_nodes,
        }


# ============================================================================
# The pure decision function
# ============================================================================

def decide(flight: Flight, snapshot: MeshSnapshot) -> Decision:
    """Decide whether `flight` may run, and where.

    Pure: no I/O, no clock, no mutation. The same inputs always produce the
    same decision.
    """
    # 1. A flight declared rogue is shot down on approach, before it is ever
    #    placed in the airspace.
    if flight.rogue:
        return Decision(
            decision=ClearanceDecision.SHOT_DOWN,
            reason="rogue flight declared - shot down on approach",
        )

    # 2. Scope conflict. A strictly higher priority flight preempts the
    #    incumbent — but only if the incumbent's node can actually take the
    #    newcomer once the incumbent's weight is removed. Preempting and then
    #    queueing would shoot down work and run nothing.
    conflict = snapshot.scope_conflict(flight)
    if conflict is not None:
        if int(flight.priority) < int(conflict.priority):
            node = (snapshot.node(conflict.assigned_node)
                    if conflict.assigned_node else None)
            if node is not None and _fits_after_preemption(node, conflict, flight.weight):
                return Decision(
                    decision=ClearanceDecision.CLEARED,
                    reason=(f"scope free after preempting {conflict.callsign} "
                            f"(higher priority: P{int(flight.priority)} < P{conflict.priority})"),
                    assigned_node=node.id,
                    scope_conflict=conflict.flight_id,
                    preempt_flight_id=conflict.flight_id,
                    node_state_at_decision=node.state_at_decision(),
                )
            return Decision(
                decision=ClearanceDecision.QUEUED,
                reason=(f"scope conflict with {conflict.callsign}; its node cannot "
                        f"take this flight even after preemption"),
                assigned_node=conflict.assigned_node,
                estimated_wait_s=30.0,
                scope_conflict=conflict.flight_id,
            )
        return Decision(
            decision=ClearanceDecision.QUEUED,
            reason=f"scope conflict with {conflict.callsign}",
            assigned_node=conflict.assigned_node,
            estimated_wait_s=30.0,
            scope_conflict=conflict.flight_id,
        )

    # 3. Candidate nodes: only nodes that can safely take the flight.
    scored: Dict[str, float] = {}
    candidates: List[NodeView] = []
    considered: List[str] = []
    for n in snapshot.nodes:
        if flight.preferred_node and flight.preferred_node != n.id:
            continue
        considered.append(n.id)
        if not n.accepts(flight.weight):
            continue
        s = n.score(flight)
        scored[n.id] = round(s, 6)
        candidates.append(n)

    if not candidates:
        # 4. Nothing can take it now. Wait for the most plausible node.
        best = _best_eventual_node(snapshot, flight)
        return Decision(
            decision=ClearanceDecision.QUEUED,
            reason="no node has sufficient safe capacity",
            assigned_node=best.id if best else None,
            estimated_wait_s=30.0,
            scored=scored,
            considered_nodes=considered,
        )

    # 5. Best node wins.
    candidates.sort(key=lambda n: scored[n.id], reverse=True)
    best = candidates[0]
    return Decision(
        decision=ClearanceDecision.CLEARED,
        reason=f"cleared on {best.name} (score {scored[best.id]:.3f})",
        assigned_node=best.id,
        scored=scored,
        node_state_at_decision=best.state_at_decision(),
        considered_nodes=considered,
    )


def _fits_after_preemption(node: NodeView, conflict: ScopeView,
                           weight: ResourceWeight) -> bool:
    """Would the node take `weight` once the incumbent's claim is released?

    The incumbent is still in the snapshot, so its reservation has to be
    removed before the question means anything.
    """
    freed = replace(
        node,
        reserved_cpu=max(0.0, node.reserved_cpu - conflict.cpu_cores),
        reserved_memory_gb=max(0.0, node.reserved_memory_gb - conflict.memory_gb),
        reserved_gpu=max(0.0, node.reserved_gpu - conflict.gpu_units),
    )
    return freed.accepts(weight)


def _best_eventual_node(snapshot: MeshSnapshot, flight: Flight) -> Optional[NodeView]:
    """The node most likely to accept the flight once capacity frees up."""
    best = None
    best_mem = -1.0
    for n in snapshot.nodes:
        if n.status in (NodeStatus.OFFLINE.value, NodeStatus.REDLINE.value,
                        NodeStatus.UNKNOWN.value, NodeStatus.DRAINING.value,
                        NodeStatus.CORDONED.value, NodeStatus.MAINTENANCE.value):
            continue
        if flight.preferred_node and flight.preferred_node != n.id:
            continue
        free = n.free()
        if free["memory_gb"] > best_mem:
            best_mem = free["memory_gb"]
            best = n
    return best
