"""Prometheus-style metrics for The Tower.

Every series here answers an operational question, not a decorative one:
is the node reachable, how close is it to its redline, is the Tower allowed
to act on it, and how many claims are outstanding.
"""
from __future__ import annotations

from the_tower.core import Tower


def _label(value: str) -> str:
    """Escape a Prometheus label value.

    Node ids are operator-supplied. An id containing a quote or a newline
    would otherwise break the exposition format (or forge a series).
    """
    return (str(value).replace("\\", "\\\\")
            .replace("\n", "\\n").replace('"', '\\"'))


def _gauge(lines, name, help_text, samples):
    """Emit one gauge family. `samples` is a list of (labels, value)."""
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} gauge")
    for labels, value in samples:
        if labels:
            rendered = ",".join(f'{k}="{_label(v)}"' for k, v in labels.items())
            lines.append(f"{name}{{{rendered}}} {value}")
        else:
            lines.append(f"{name} {value}")


def render(tower: Tower) -> str:
    snap = tower.airspace()
    stats = snap.stats
    nodes = snap.nodes
    ladder = tower.ladder()

    lines = []
    _gauge(lines, "tower_nodes_total", "Total mesh nodes",
           [({}, stats["nodes_total"])])
    _gauge(lines, "tower_nodes_online", "Mesh nodes that are reachable",
           [({}, stats["nodes_online"])])
    _gauge(lines, "tower_flights_in_flight", "Flights currently in flight",
           [({}, stats["flights_in_flight"])])
    _gauge(lines, "tower_flights_queued", "Queued flights",
           [({}, stats["flights_queued"])])
    _gauge(lines, "tower_flights_shot_down", "Shot-down flights (cumulative in memory)",
           [({}, stats["flights_shot_down"])])
    _gauge(lines, "tower_flights_landed", "Landed flights (cumulative in memory)",
           [({}, stats["flights_landed"])])

    # One-hot status, so a dashboard can alert on `status="unknown"` without
    # having to know the status vocabulary.
    _gauge(lines, "tower_node_status", "Node lifecycle status (1 for the current status)",
           [({"node": n.spec.id, "status": n.status.value}, 1) for n in nodes])

    _gauge(lines, "tower_node_enforcing",
           "1 when the Tower may act on this node, 0 when it only advises",
           [({"node": n.spec.id}, 1 if n.spec.enforcement == "enforce" else 0)
            for n in nodes])

    _gauge(lines, "tower_node_temperature_c", "Node temperature",
           [({"node": n.spec.id}, n.telemetry.temperature_c) for n in nodes])
    _gauge(lines, "tower_node_redline_temp_c", "Node redline temperature",
           [({"node": n.spec.id}, n.spec.redline_temp_c) for n in nodes])
    _gauge(lines, "tower_node_thermal_margin_c",
           "Degrees remaining before the node's redline (negative means past it)",
           [({"node": n.spec.id}, round(n.spec.redline_temp_c - n.telemetry.temperature_c, 2))
            for n in nodes])
    _gauge(lines, "tower_node_telemetry_age_seconds",
           "Age of the newest telemetry sample for a node",
           [({"node": n.spec.id},
             round((snap.timestamp - n.telemetry.timestamp).total_seconds(), 1))
            for n in nodes])
    _gauge(lines, "tower_node_cpu_percent", "Node CPU percent",
           [({"node": n.spec.id}, n.telemetry.cpu_percent) for n in nodes])
    _gauge(lines, "tower_node_memory_percent", "Node memory percent",
           [({"node": n.spec.id}, n.telemetry.memory_percent) for n in nodes])

    _gauge(lines, "tower_leases_active", "Resource leases currently held",
           [({}, sum(1 for l in tower.leases.values() if l.status.value == "active"))])

    # The ladder is a decision, so expose the rung as a one-hot series.
    _gauge(lines, "tower_safety_ladder",
           "Current safety-ladder rung per node (1 for the current rung)",
           [({"node": node_id, "rung": info["ladder"]}, 1)
            for node_id, info in ladder.get("nodes", {}).items()])

    _gauge(lines, "tower_max_temperature_c", "Max node temperature",
           [({}, stats["max_temp"])])
    return "\n".join(lines) + "\n"
