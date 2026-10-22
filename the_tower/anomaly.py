"""Anomaly Radar - predictive ML on thermal/memory trends.

Lightweight, dependency-free trend detection: linear regression over recent
telemetry samples to predict time-to-redline and flag rogue flights.
"""
from __future__ import annotations

from datetime import datetime, timezone
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


# A spike must be at least this many degrees above the local level, and at
# least this many robust standard deviations of the local noise. Both, so
# that a smooth ramp is not a spike and a noisy flat line is not either.
SPIKE_MIN_DEVIATION_C = 2.0
SPIKE_MIN_Z = 3.0
# The smallest noise scale a spike is measured against, in degrees. Below
# this the z-score would explode on a perfectly smooth series.
SPIKE_NOISE_FLOOR_C = 0.5
# How many samples before the newest one form the local window.
SPIKE_WINDOW = 5


def detect_spike(xs: List[float], values: List[float],
                 window: int = SPIKE_WINDOW) -> Optional[Tuple[float, float, float]]:
    """Find a point that departs sharply from the local level.

    Returns `(value, z, deviation)` for the newest point, or None if there is
    no spike.

    The comparison is local and the noise scale is estimated robustly, from
    the median absolute deviation. A straight-line fit over a long window
    does not work here: the temperature of a real machine curves as its load
    changes, the fit leaves a systematic residual, and the newest point
    looks like a departure every time the curve turns.
    """
    n = len(values)
    if n < window + 2:
        return None
    baseline = values[-(window + 1):-1]
    current = values[-1]
    if not baseline:
        return None
    local = sorted(baseline)[len(baseline) // 2]
    # robust sigma: median absolute deviation, scaled for a normal
    mad = sorted(abs(v - local) for v in baseline)[len(baseline) // 2]
    noise = max(mad * 1.4826, SPIKE_NOISE_FLOOR_C)
    deviation = current - local
    if deviation < SPIKE_MIN_DEVIATION_C:
        return None
    z = deviation / noise
    if z < SPIKE_MIN_Z:
        return None
    return current, z, deviation


class AnomalyRadar:
    # How long the same condition on the same node stays reported. Without
    # this, a node that is steadily warming raises the same warning on every
    # sweep and the event log becomes noise nobody reads.
    REPORT_COOLDOWN_S = 60.0

    def __init__(self, redline_margin_s: float = 120.0,
                 report_cooldown_s: float = None):
        self.redline_margin_s = redline_margin_s
        if report_cooldown_s is not None:
            self.REPORT_COOLDOWN_S = report_cooldown_s
        # node id -> {condition: reported_at}. A node can have several
        # conditions at once, so each is tracked on its own.
        self._last_report: Dict[str, Dict[str, float]] = {}

    def _should_report(self, node_id: str, condition: str, now: float) -> bool:
        """Report a condition when it is new, or when the cooldown has passed."""
        seen = self._last_report.setdefault(node_id, {})
        last = seen.get(condition)
        if last is None or (now - last) >= self.REPORT_COOLDOWN_S:
            seen[condition] = now
            return True
        return False

    def analyze_node(self, node: Node) -> List[TowerEvent]:
        events: List[TowerEvent] = []
        hist = node.history
        if len(hist) < 5:
            return events
        now = datetime.now(timezone.utc).timestamp()
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
            # A negative forecast means the node is already at or past the
            # redline. Saying "in -21 seconds" is nonsense; the redline
            # breach below is the event that matters.
            secs_to_redline = max(0.0, secs_to_redline)
        else:
            secs_to_redline = float("inf")

        if secs_to_redline < self.redline_margin_s and current_temp > redline * 0.75:
            if self._should_report(node.spec.id, "thermal_trend", now):
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
            if self._should_report(node.spec.id, "memory_trend", now):
                events.append(TowerEvent(
                    kind=EventKind.ANOMALY,
                    node_id=node.spec.id,
                    message=f"Memory trend rising: +{slope_m_sample:.2f}%/sample, now {hist[-1].memory_percent:.1f}%",
                    severity="warn",
                    data={"slope_mem": slope_m_sample},
                ))
        if slope_p_sample > 2.0 and hist[-1].power_watts > node.spec.max_power_watts * 0.8:
            if self._should_report(node.spec.id, "power_trend", now):
                events.append(TowerEvent(
                    kind=EventKind.ANOMALY,
                    node_id=node.spec.id,
                    message=f"Power draw rising: +{slope_p_sample:.2f}W/sample, now {hist[-1].power_watts:.1f}W",
                    severity="warn",
                data={"slope_power": slope_p_sample},
                ))
        # Spike detection on temperature.
        #
        # A spike is a sharp departure from the local level of the node, not
        # a departure from the mean. A node that is warming up, or whose
        # load is curving, sits many standard deviations above a long-run
        # mean - and a z-score against the mean fires on every sweep of a
        # warming machine. That floods the event log with false alarms and
        # teaches the operator to ignore it.
        if len(temps) >= 8:
            spike = detect_spike(xs, temps)
            if spike is not None and self._should_report(node.spec.id, "thermal_spike", now):
                value, z, deviation = spike
                events.append(TowerEvent(
                    kind=EventKind.ANOMALY,
                    node_id=node.spec.id,
                    message=f"Thermal spike: {value:.1f}C ({deviation:+.1f}C above local level, z={z:.2f})",
                    severity="warn",
                    data={"z": z, "value": value, "deviation_c": deviation},
                ))
        # redline breach
        if current_temp >= redline:
            if self._should_report(node.spec.id, "redline", now):
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
        if secs is not None:
            # A negative forecast means the node is already at or past the
            # redline. "In -105 seconds" is not a forecast.
            secs = max(0.0, secs)
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
