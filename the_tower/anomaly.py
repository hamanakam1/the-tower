"""Anomaly Radar - predictive ML on thermal/memory trends.

Lightweight, dependency-free trend detection: linear regression over recent
telemetry samples to predict time-to-redline and flag rogue flights.
"""
from __future__ import annotations

from datetime import timezone
from typing import Dict, List, Optional, Tuple

from .schemas import Node, Telemetry, TowerEvent, EventKind

# Fallback spacing when the samples carry no usable timestamps.
FALLBACK_INTERVAL_S = 5.0


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


def time_axis(hist: List[Telemetry]) -> List[float]:
    """Seconds since the first sample, taken from the real timestamps.

    The sweep interval is configurable, so assuming five seconds between
    samples makes every time-to-redline prediction wrong by the ratio of
    the real interval to five - and that prediction is what drives the
    safety ladder.
    """
    n = len(hist)
    if n == 0:
        return []
    stamps = []
    for h in hist:
        ts = h.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        stamps.append(ts)
    base = min(stamps)
    xs = [(ts - base).total_seconds() for ts in stamps]
    if xs[-1] - xs[0] <= 0 and n > 1:
        # every sample claims the same instant: there is no time axis to
        # regress against, so fall back to even spacing.
        return [i * FALLBACK_INTERVAL_S for i in range(n)]
    return xs


def sample_interval(xs: List[float]) -> float:
    """The mean seconds between samples, from the real time axis."""
    if len(xs) < 2:
        return FALLBACK_INTERVAL_S
    span = max(xs) - min(xs)
    if span <= 0:
        return FALLBACK_INTERVAL_S
    return span / (len(xs) - 1)


class AnomalyRadar:
    def __init__(self, redline_margin_s: float = 120.0):
        self.redline_margin_s = redline_margin_s

    def analyze_node(self, node: Node) -> List[TowerEvent]:
        events: List[TowerEvent] = []
        hist = node.history
        if len(hist) < 5:
            return events
        # Time axis in seconds, taken from the real sample timestamps.
        xs = time_axis(hist)
        dt = sample_interval(xs)
        temps = [h.temperature_c for h in hist]
        mems = [h.memory_percent for h in hist]
        powers = [h.power_watts for h in hist]
        # slopes are per second; the per-sample figures are what the
        # thresholds and the dashboard are expressed in.
        slope_t, _ = _linreg(xs, temps)
        slope_m, _ = _linreg(xs, mems)
        slope_p, _ = _linreg(xs, powers)
        slope_t_sample = slope_t * dt
        slope_m_sample = slope_m * dt
        slope_p_sample = slope_p * dt
        redline = node.spec.redline_temp_c
        current_temp = hist[-1].temperature_c
        # predict seconds to redline
        if slope_t > 0:
            secs_to_redline = (redline - current_temp) / slope_t
        else:
            secs_to_redline = float("inf")

        if secs_to_redline < self.redline_margin_s and current_temp > redline * 0.75:
            events.append(TowerEvent(
                kind=EventKind.ANOMALY,
                node_id=node.spec.id,
                message=f"Thermal trend rising: predicted redline in {int(secs_to_redline)}s "
                        f"(slope {slope_t_sample:+.2f}C/sample, {slope_t * 60:+.2f}C/min)",
                severity="warn",
                data={"slope_temp": slope_t_sample, "slope_temp_per_s": slope_t,
                      "secs_to_redline": secs_to_redline,
                      "sample_interval_s": round(dt, 3),
                      "current_temp": current_temp, "redline": redline},
            ))
        if slope_m_sample > 0.5 and hist[-1].memory_percent > 80:
            events.append(TowerEvent(
                kind=EventKind.ANOMALY,
                node_id=node.spec.id,
                message=f"Memory trend rising: +{slope_m_sample:.2f}%/sample, now {hist[-1].memory_percent:.1f}%",
                severity="warn",
                data={"slope_mem": slope_m_sample},
            ))
        if slope_p_sample > 2.0 and hist[-1].power_watts > node.spec.max_power_watts * 0.8:
            events.append(TowerEvent(
                kind=EventKind.ANOMALY,
                node_id=node.spec.id,
                message=f"Power draw rising: +{slope_p_sample:.2f}W/sample, now {hist[-1].power_watts:.1f}W",
                severity="warn",
                data={"slope_power": slope_p_sample},
            ))
        # z-score spike detection on temperature
        if len(temps) >= 8:
            recent = temps[-3:]
            baseline = temps[:-3]
            if baseline:
                import statistics
                mean = statistics.fmean(baseline)
                try:
                    sd = statistics.pstdev(baseline)
                except Exception:
                    sd = 0.0
                if sd > 0:
                    for v in recent:
                        z = (v - mean) / sd
                        if z > 2.5:
                            events.append(TowerEvent(
                                kind=EventKind.ANOMALY,
                                node_id=node.spec.id,
                                message=f"Thermal spike: {v:.1f}C (z={z:.2f})",
                                severity="warn",
                                data={"z": z, "value": v},
                            ))
                            break
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
            return {"samples": len(hist), "temp_slope": 0.0, "mem_slope": 0.0,
                    "secs_to_redline": None, "sample_interval_s": None}
        xs = time_axis(hist)
        dt = sample_interval(xs)
        slope_t, _ = _linreg(xs, [h.temperature_c for h in hist])
        slope_m, _ = _linreg(xs, [h.memory_percent for h in hist])
        current = hist[-1].temperature_c
        redline = node.spec.redline_temp_c
        # slope_t is per second; secs to redline follows from it directly.
        secs = (redline - current) / slope_t if slope_t > 0 else None
        return {
            "samples": len(hist),
            "temp_slope": round(slope_t * dt, 3),
            "mem_slope": round(slope_m * dt, 3),
            "temp_slope_per_s": round(slope_t, 6),
            "sample_interval_s": round(dt, 3),
            "secs_to_redline": (round(secs, 1) if secs is not None and secs != float("inf") else None),
            "current_temp": round(current, 1),
            "redline": redline,
        }
