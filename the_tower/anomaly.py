"""Anomaly Radar - predictive ML on thermal/memory trends.

Lightweight, dependency-free trend detection: linear regression over recent
telemetry samples to predict time-to-redline and flag rogue flights.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .schemas import Node, Telemetry, TowerEvent, EventKind


def _linreg(xs: List[float], ys: List[float]) -> Tuple[float, float]:
    n = len(xs)
    if n < 2:
        return 0.0, (ys[-1] if ys else 0.0)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    slope = num / den if den != 0 else 0.0
    intercept = my - slope * mx
    return slope, intercept


class AnomalyRadar:
    def __init__(self, redline_margin_s: float = 120.0):
        self.redline_margin_s = redline_margin_s

    def analyze_node(self, node: Node) -> List[TowerEvent]:
        events: List[TowerEvent] = []
        hist = node.history
        if len(hist) < 5:
            return events
        # time axis in seconds (sample index * interval approx 5s)
        xs = [i * 5.0 for i in range(len(hist))]
        temps = [h.temperature_c for h in hist]
        mems = [h.memory_percent for h in hist]
        slope_t, _ = _linreg(xs, temps)
        slope_m, _ = _linreg(xs, mems)
        redline = node.spec.redline_temp_c
        current_temp = hist[-1].temperature_c
        # predict seconds to redline
        if slope_t > 0:
            secs_to_redline = (redline - current_temp) / (slope_t / 5.0) * 5.0 if slope_t != 0 else float("inf")
            # slope per second
            slope_per_s = slope_t / 5.0
            if slope_per_s > 0:
                secs_to_redline = (redline - current_temp) / slope_per_s
            else:
                secs_to_redline = float("inf")
        else:
            secs_to_redline = float("inf")

        if secs_to_redline < self.redline_margin_s and current_temp > redline * 0.75:
            events.append(TowerEvent(
                kind=EventKind.ANOMALY,
                node_id=node.spec.id,
                message=f"Thermal trend rising: predicted redline in {int(secs_to_redline)}s "
                        f"(slope {slope_t:+.2f}C/sample)",
                severity="warn",
                data={"slope_temp": slope_t, "secs_to_redline": secs_to_redline,
                      "current_temp": current_temp, "redline": redline},
            ))
        if slope_m > 0.5 and hist[-1].memory_percent > 80:
            events.append(TowerEvent(
                kind=EventKind.ANOMALY,
                node_id=node.spec.id,
                message=f"Memory trend rising: +{slope_m:.2f}%/sample, now {hist[-1].memory_percent:.1f}%",
                severity="warn",
                data={"slope_mem": slope_m},
            ))
        # redline breach
        if current_temp >= redline:
            events.append(TowerEvent(
                kind=EventKind.REDLINE,
                node_id=node.spec.id,
                message=f"REDLINE BREACH on {node.spec.name}: {current_temp:.1f}C >= {redline:.1f}C",
                severity="critical",
                data={"temperature_c": current_temp, "redline": redline},
            ))
        return events

    def is_rogue_flight(self, node: Node, weight_delta_temp: float = 0.0) -> bool:
        """Heuristic: a flight is rogue if it would push node past redline quickly."""
        if not node.history:
            return False
        hist = node.history
        temps = [h.temperature_c for h in hist[-5:]]
        if len(temps) < 3:
            return False
        # runaway if temp rising > 3C per sample and already hot
        deltas = [temps[i+1] - temps[i] for i in range(len(temps)-1)]
        avg_delta = sum(deltas) / len(deltas)
        return avg_delta > 3.0 and temps[-1] > node.spec.redline_temp_c * 0.8

    def trend_summary(self, node: Node) -> Dict:
        hist = node.history
        if len(hist) < 3:
            return {"samples": len(hist), "temp_slope": 0.0, "mem_slope": 0.0, "secs_to_redline": None}
        xs = [i * 5.0 for i in range(len(hist))]
        slope_t, _ = _linreg(xs, [h.temperature_c for h in hist])
        slope_m, _ = _linreg(xs, [h.memory_percent for h in hist])
        current = hist[-1].temperature_c
        redline = node.spec.redline_temp_c
        slope_per_s = slope_t / 5.0
        secs = (redline - current) / slope_per_s if slope_per_s > 0 else None
        return {
            "samples": len(hist),
            "temp_slope": round(slope_t, 3),
            "mem_slope": round(slope_m, 3),
            "secs_to_redline": (round(secs, 1) if secs is not None and secs != float("inf") else None),
            "current_temp": round(current, 1),
            "redline": redline,
        }
