"""Pluggable sensor backends.

A node's temperature is either **measured** by a real sensor or **estimated**
from CPU load. The Tower never pretends: every sample carries the source and,
for an estimate, a note saying so.

Backends are selected per node, so a Raspberry Pi with a real SoC sensor and a
laptop without one can sit in the same mesh without lying to each other.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

import psutil


@dataclass(frozen=True)
class Reading:
    """One temperature reading, with its provenance."""
    celsius: Optional[float]
    source: str
    note: Optional[str] = None

    @property
    def measured(self) -> bool:
        return self.source == "measured"


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class SensorBackend:
    """Base class. Subclasses implement `read()`."""
    name = "none"

    def available(self) -> bool:
        return True

    def read(self, cpu_percent: float) -> Reading:
        raise NotImplementedError


class PsutilBackend(SensorBackend):
    """Real sensors through psutil (Linux hwmon/coretemp, and some others)."""
    name = "psutil"

    def read(self, cpu_percent: float) -> Reading:
        try:
            temps = psutil.sensors_temperatures()
        except Exception:
            temps = {}
        for key in ("cpu_thermal", "coretemp", "k10temp", "cpu-thermal", "soc_thermal"):
            entries = temps.get(key) or []
            if entries:
                return Reading(celsius=entries[0].current, source="measured",
                               note=f"{key} sensor")
        if temps:
            for key, entries in temps.items():
                if entries:
                    return Reading(celsius=entries[0].current, source="measured",
                                   note=f"{key} sensor (not a CPU sensor)")
        return Reading(celsius=None, source="unavailable",
                       note="no temperature sensor exposed by psutil")


class OsxCpuTempBackend(SensorBackend):
    """macOS CPU temperature via the `osx-cpu-temp` helper, if installed."""
    name = "osx-cpu-temp"

    def available(self) -> bool:
        return shutil.which("osx-cpu-temp") is not None

    def read(self, cpu_percent: float) -> Reading:
        exe = shutil.which("osx-cpu-temp")
        if not exe:
            return Reading(celsius=None, source="unavailable", note="osx-cpu-temp not installed")
        try:
            out = subprocess.run([exe, "-m"], capture_output=True, text=True, timeout=5)
            m = re.search(r"([0-9]+\.?[0-9]*)", out.stdout or "")
            if m:
                return Reading(celsius=float(m.group(1)), source="measured",
                               note="osx-cpu-temp")
        except Exception:
            pass
        return Reading(celsius=None, source="unavailable", note="osx-cpu-temp failed")


class EstimateBackend(SensorBackend):
    """Fallback: derive a temperature from CPU load.

    This is an *estimate*, and it says so. A node running on this backend
    gets a stricter policy elsewhere in the Tower, because the number is a
    guess.
    """
    name = "estimate"

    def read(self, cpu_percent: float) -> Reading:
        import random
        celsius = 45.0 + (cpu_percent / 100.0) * 30.0 + random.uniform(-2, 2)
        return Reading(celsius=celsius, source="estimated",
                       note="temperature estimated from cpu load; no sensor available")


BACKENDS: Dict[str, SensorBackend] = {
    "psutil": PsutilBackend(),
    "osx-cpu-temp": OsxCpuTempBackend(),
    "estimate": EstimateBackend(),
}

# Order in which backends are tried when a node does not name one.
AUTO_ORDER = ["psutil", "osx-cpu-temp", "estimate"]


def select_backend(name: Optional[str] = None) -> SensorBackend:
    """Pick a backend by name, or the best available one."""
    if name:
        backend = BACKENDS.get(name)
        if backend is not None:
            return backend
    for key in AUTO_ORDER:
        backend = BACKENDS.get(key)
        if backend is not None and backend.available():
            # a real sensor that returns nothing is not "available"
            if key != "estimate":
                return backend
    return BACKENDS["estimate"]


def read_temperature(name: Optional[str], cpu_percent: float) -> Reading:
    """Read a temperature using the named (or best) backend.

    Falls back to the estimate only when no real sensor answers, and the
    resulting Reading always states which it is.
    """
    backend = select_backend(name)
    reading = backend.read(cpu_percent)
    if reading.celsius is None:
        return EstimateBackend().read(cpu_percent)
    return reading


def register_backend(name: str, backend: SensorBackend):
    """Register a custom backend (used by tests and platform adapters)."""
    BACKENDS[name] = backend
    if name not in AUTO_ORDER:
        AUTO_ORDER.insert(0, name)
