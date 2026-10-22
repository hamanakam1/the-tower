"""Decision replay.

A clearance decision is only trustworthy if you can re-derive it. The Tower
records the mesh snapshot it decided against alongside the decision itself,
so any decision can be replayed later:

    decision_now = decide(flight, snapshot_recorded_yesterday)

If the replay produces a different answer, the mesh changed, the policy
changed, or something is wrong. All three are worth knowing.

This module is pure: it serialises and rebuilds snapshots, and re-runs the
policy. It never touches the database or the network.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .policy import Decision, MeshSnapshot, NodeView, ScopeView, decide
from .schemas import Flight


def snapshot_to_dict(snapshot: MeshSnapshot) -> Dict[str, Any]:
    """Serialise a snapshot so it can be stored and replayed."""
    return {
        "nodes": [
            {
                "id": n.id,
                "name": n.name,
                "status": n.status,
                "cpu_percent": n.cpu_percent,
                "memory_percent": n.memory_percent,
                "temperature_c": n.temperature_c,
                "power_watts": n.power_watts,
                "thermal_state": n.thermal_state,
                "gpu_percent": n.gpu_percent,
                "cpu_cores": n.cpu_cores,
                "memory_gb": n.memory_gb,
                "gpu_units": n.gpu_units,
                "redline_temp_c": n.redline_temp_c,
                "max_power_watts": n.max_power_watts,
                "network_capacity_mbps": n.network_capacity_mbps,
                "reserved_cpu": n.reserved_cpu,
                "reserved_memory_gb": n.reserved_memory_gb,
                "reserved_gpu": n.reserved_gpu,
                "telemetry_age_s": n.telemetry_age_s,
            }
            for n in snapshot.nodes
        ],
        "in_flight": [
            {
                "flight_id": f.flight_id,
                "callsign": f.callsign,
                "scope": list(f.scope),
                "priority": f.priority,
                "node_id": f.node_id,
                "assigned_node": f.assigned_node,
            }
            for f in snapshot.in_flight
        ],
        "queued_count": snapshot.queued_count,
    }


def snapshot_from_dict(data: Dict[str, Any]) -> MeshSnapshot:
    """Rebuild a snapshot from its serialised form."""
    nodes = [NodeView(**n) for n in data.get("nodes", [])]
    in_flight = [
        ScopeView(
            flight_id=f["flight_id"],
            callsign=f.get("callsign", ""),
            scope=list(f.get("scope", [])),
            priority=int(f.get("priority", 2)),
            node_id=f.get("node_id"),
            assigned_node=f.get("assigned_node"),
        )
        for f in data.get("in_flight", [])
    ]
    return MeshSnapshot(nodes=nodes, in_flight=in_flight,
                        queued_count=int(data.get("queued_count", 0)))


def replay(flight: Flight, snapshot_data: Dict[str, Any]) -> Decision:
    """Re-run the admission policy against a recorded snapshot."""
    return decide(flight, snapshot_from_dict(snapshot_data))


def compare(original: Dict[str, Any], replayed: Decision) -> Dict[str, Any]:
    """Compare a recorded decision with a replayed one."""
    orig_decision = original.get("decision")
    orig_node = original.get("assigned_node")
    same = (orig_decision == replayed.decision.value and
            orig_node == replayed.assigned_node)
    return {
        "matches": same,
        "recorded": {
            "decision": orig_decision,
            "assigned_node": orig_node,
            "reason": original.get("reason"),
        },
        "replayed": {
            "decision": replayed.decision.value,
            "assigned_node": replayed.assigned_node,
            "reason": replayed.reason,
        },
        "drift": None if same else "the mesh or the policy changed since the decision was made",
    }
