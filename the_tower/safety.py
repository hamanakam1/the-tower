"""Graded safety — the enforcement ladder.

The Tower does not treat "allow" and "SIGKILL" as the only two answers.
Real hardware needs a ladder, and every rung is a recorded policy decision:

    admit ──▶ warn ──▶ throttle ──▶ drain ──▶ kill
       │         │         │           │        │
   log only  log only  cap CPU/MEM  stop admitting  revoke
                                     new flights   flight

`assess()` is pure: given a node view and a flight, it returns the rung of
the ladder the node is on and what the Tower should do about it. The Tower
core then performs the effect.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

from .policy import NodeView
from .schemas import Flight, ThermalState


class Ladder(str, Enum):
    ADMIT = "admit"
    WARN = "warn"
    THROTTLE = "throttle"
    DRAIN = "drain"
    KILL = "kill"


# How much of a flight's requested CPU is actually granted at each rung.
THROTTLE_FACTOR = {
    Ladder.THROTTLE: 0.5,
    Ladder.DRAIN: 0.0,
}


@dataclass(frozen=True)
class Assessment:
    """The rung of the ladder a node is on, and why."""
    ladder: Ladder
    reason: str
    severity: str = "info"
    headroom_c: float = 0.0
    thermal_state: str = ThermalState.NOMINAL.value
    predicted_redline_in_s: Optional[float] = None
    metrics: Dict[str, Any] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ladder": self.ladder.value,
            "reason": self.reason,
            "severity": self.severity,
            "headroom_c": round(self.headroom_c, 2),
            "thermal_state": self.thermal_state,
            "predicted_redline_in_s": self.predicted_redline_in_s,
            "metrics": self.metrics or {},
        }


def assess(node: NodeView, flight: Optional[Flight] = None,
           predicted_redline_in_s: Optional[float] = None) -> Assessment:
    """Place a node on the enforcement ladder.

    Pure: no I/O, no clock, no mutation.
    """
    metrics: Dict[str, Any] = {
        "temp_c": node.temperature_c,
        "redline_c": node.redline_temp_c,
        "cpu_percent": node.cpu_percent,
        "memory_percent": node.memory_percent,
        "status": node.status,
    }
    headroom = node.redline_temp_c - node.temperature_c

    # 1. Hard stops. A node that is offline, unknown, at redline, or
    #    thermally critical cannot take new work. "Unknown" means the Tower
    #    cannot see the machine, and the safe answer to not being able to see
    #    is to stop adding work — not to reach in and kill what may be fine.
    if node.status == "offline":
        return Assessment(Ladder.DRAIN, "node is offline - no new work",
                          severity="critical", headroom_c=headroom,
                          thermal_state=node.thermal_state, metrics=metrics)
    if node.status == "unknown":
        return Assessment(Ladder.DRAIN,
                          "telemetry is stale - node state is unknown; refusing new work",
                          severity="critical", headroom_c=headroom,
                          thermal_state=node.thermal_state, metrics=metrics)
    if node.status == "redline" or node.temperature_c >= node.redline_temp_c:
        return Assessment(Ladder.KILL,
                          f"node at redline ({node.temperature_c:.1f}C >= {node.redline_temp_c:.1f}C)",
                          severity="critical", headroom_c=headroom,
                          thermal_state=node.thermal_state, metrics=metrics)
    if node.thermal_state == ThermalState.CRITICAL.value:
        return Assessment(Ladder.KILL, "thermal state critical", severity="critical",
                          headroom_c=headroom, thermal_state=node.thermal_state,
                          metrics=metrics)

    # 2. Drain: the node is heading for redline. Stop admitting new work and
    #    let what is running finish on its own.
    if predicted_redline_in_s is not None and predicted_redline_in_s < 120:
        return Assessment(Ladder.DRAIN,
                          f"predicted redline in {int(predicted_redline_in_s)}s - draining",
                          severity="warn", headroom_c=headroom,
                          thermal_state=node.thermal_state,
                          predicted_redline_in_s=predicted_redline_in_s, metrics=metrics)

    # 3. Throttle: the node is hot or saturated. Admit, but cap the share.
    if node.thermal_state == ThermalState.SERIOUS.value:
        return Assessment(Ladder.THROTTLE,
                          f"thermal state serious ({node.temperature_c:.1f}C) - throttling admission",
                          severity="warn", headroom_c=headroom,
                          thermal_state=node.thermal_state,
                          predicted_redline_in_s=predicted_redline_in_s, metrics=metrics)
    if node.cpu_percent >= 90 or node.memory_percent >= 90:
        return Assessment(Ladder.THROTTLE,
                          f"node saturated (cpu {node.cpu_percent:.0f}% / mem {node.memory_percent:.0f}%) - throttling",
                          severity="warn", headroom_c=headroom,
                          thermal_state=node.thermal_state,
                          predicted_redline_in_s=predicted_redline_in_s, metrics=metrics)

    # 4. Warn: still fine, but worth telling the operator.
    if node.thermal_state == ThermalState.FAIR.value or headroom < 12.0:
        return Assessment(Ladder.WARN,
                          f"thermal headroom low ({headroom:.1f}C)",
                          severity="info", headroom_c=headroom,
                          thermal_state=node.thermal_state,
                          predicted_redline_in_s=predicted_redline_in_s, metrics=metrics)

    # 5. Admit.
    return Assessment(Ladder.ADMIT, "nominal", severity="info",
                      headroom_c=headroom, thermal_state=node.thermal_state,
                      predicted_redline_in_s=predicted_redline_in_s, metrics=metrics)


def throttled_weight(flight: Flight, factor: float) -> Flight:
    """Return a copy of the flight with its CPU share capped.

    The throttle is expressed as a reduction in the *requested* CPU, which
    is what the node's capacity accounting uses. Memory is left alone:
    starving memory causes swapping, which makes a node hotter, not cooler.
    """
    from .schemas import ResourceWeight
    w = flight.weight.model_copy(deep=True)
    w.cpu_cores = max(0.1, w.cpu_cores * factor)
    w.thermal_budget_c = max(0.0, w.thermal_budget_c * factor)
    return flight.model_copy(update={"weight": w}, deep=True)


def ladder_summary(assessments: List[Assessment]) -> Dict[str, int]:
    """Count of nodes at each rung of the ladder."""
    out = {rung.value: 0 for rung in Ladder}
    for a in assessments:
        out[a.ladder.value] = out.get(a.ladder.value, 0) + 1
    return out
