"""Prometheus-style metrics for The Tower."""
from __future__ import annotations

from the_tower.core import Tower


def render(tower: Tower) -> str:
    snap = tower.airspace()
    stats = snap.stats
    nodes = snap.nodes
    lines = []
    lines.append("# HELP tower_nodes_total Total mesh nodes")
    lines.append("# TYPE tower_nodes_total gauge")
    lines.append(f"tower_nodes_total {stats['nodes_total']}")
    lines.append("# HELP tower_nodes_online Online mesh nodes")
    lines.append("# TYPE tower_nodes_online gauge")
    lines.append(f"tower_nodes_online {stats['nodes_online']}")
    lines.append("# HELP tower_flights_in_flight Flights currently in flight")
    lines.append("# TYPE tower_flights_in_flight gauge")
    lines.append(f"tower_flights_in_flight {stats['flights_in_flight']}")
    lines.append("# HELP tower_flights_queued Queued flights")
    lines.append("# TYPE tower_flights_queued gauge")
    lines.append(f"tower_flights_queued {stats['flights_queued']}")
    lines.append("# HELP tower_flights_shot_down Shot-down flights (cumulative in memory)")
    lines.append("# TYPE tower_flights_shot_down gauge")
    lines.append(f"tower_flights_shot_down {stats['flights_shot_down']}")
    lines.append("# HELP tower_node_temperature_c Node temperature")
    lines.append("# TYPE tower_node_temperature_c gauge")
    for n in nodes:
        lines.append(f'tower_node_temperature_c{{node="{n.spec.id}"}} {n.telemetry.temperature_c}')
    lines.append("# HELP tower_node_cpu_percent Node CPU percent")
    lines.append("# TYPE tower_node_cpu_percent gauge")
    for n in nodes:
        lines.append(f'tower_node_cpu_percent{{node="{n.spec.id}"}} {n.telemetry.cpu_percent}')
    lines.append("# HELP tower_node_memory_percent Node memory percent")
    lines.append("# TYPE tower_node_memory_percent gauge")
    for n in nodes:
        lines.append(f'tower_node_memory_percent{{node="{n.spec.id}"}} {n.telemetry.memory_percent}')
    lines.append("# HELP tower_max_temperature_c Max node temperature")
    lines.append("# TYPE tower_max_temperature_c gauge")
    lines.append(f"tower_max_temperature_c {stats['max_temp']}")
    return "\n".join(lines) + "\n"
