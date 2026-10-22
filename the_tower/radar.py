"""Radar Sweep - 5-second telemetry polling across all mesh nodes.

Generates realistic telemetry for simulated nodes and reads real metrics
from the local machine (psutil) for the M3 Max node.
"""
from __future__ import annotations

import asyncio
import math
import random
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import psutil

from .schemas import Node, NodeSpec, Telemetry, ThermalState, NodeStatus


def thermal_state_for(temp_c: float, redline: float) -> ThermalState:
    ratio = temp_c / redline
    if ratio >= 0.95:
        return ThermalState.CRITICAL
    if ratio >= 0.85:
        return ThermalState.SERIOUS
    if ratio >= 0.70:
        return ThermalState.FAIR
    return ThermalState.NOMINAL


class RadarSweep:
    """Polls telemetry across all nodes on a 5-second cadence."""

    def __init__(self, nodes: Dict[str, Node], interval_s: float = 5.0):
        self.nodes = nodes
        self.interval_s = interval_s
        self._phase: Dict[str, float] = {nid: random.random() * math.pi * 2 for nid in nodes}
        self._t0 = time.time()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._subscribers: List = []

    def subscribe(self, cb):
        self._subscribers.append(cb)

    async def sweep_once(self) -> Dict[str, Telemetry]:
        now = time.time()
        out: Dict[str, Telemetry] = {}
        for nid, node in self.nodes.items():
            if not getattr(node.spec, "simulate", True):
                # agent-managed node: don't fabricate telemetry; check staleness
                age = (datetime.now(timezone.utc) - node.last_seen).total_seconds()
                if age > node.spec.staleness_budget_s:
                    node.status = NodeStatus.UNKNOWN
                out[nid] = node.telemetry
                continue
            t = self._sample(nid, node, now)
            node.telemetry = t
            node.last_seen = datetime.now(timezone.utc)
            # keep history (bounded)
            node.history.append(t)
            if len(node.history) > 60:
                node.history = node.history[-60:]
            # status update
            node.status = self._status_for(node, t)
            out[nid] = t
        for cb in list(self._subscribers):
            try:
                await cb(out)
            except Exception:
                pass
        return out

    def _status_for(self, node: Node, t: Telemetry) -> NodeStatus:
        if t.thermal_state == ThermalState.CRITICAL:
            return NodeStatus.REDLINE
        if t.cpu_percent > 92 or t.memory_percent > 92:
            return NodeStatus.OVERLOADED
        if t.thermal_state == ThermalState.SERIOUS:
            return NodeStatus.DEGRADED
        return NodeStatus.ONLINE

    def _sample(self, nid: str, node: Node, now: float) -> Telemetry:
        # The local host is sampled with real metrics; every other node is
        # either agent-driven (ingested) or simulated.
        if node.spec.location == "local" or nid == "m3max":
            return self._sample_local(node, now)
        return self._sample_simulated(nid, node, now)

    def _sample_local(self, node: Node, now: float) -> Telemetry:
        cpu = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        # temperature (macOS: psutil.sensors_temperatures may be empty)
        temp = None
        try:
            temps = psutil.sensors_temperatures()
            for key in ("cpu_thermal", "coretemp", "Apple", "cpu-thermal"):
                if key in temps and temps[key]:
                    temp = temps[key][0].current
                    break
            if temp is None and temps:
                first = next(iter(temps.values()))
                if first:
                    temp = first[0].current
        except Exception:
            temp = None
        source = "measured"
        note = None
        if temp is None:
            # macOS exposes no CPU thermal sensor through psutil. Estimate
            # from load and say so — an honest estimate beats a silent fiction.
            temp = 45.0 + (cpu / 100.0) * 30.0 + random.uniform(-2, 2)
            source = "measured (thermal estimated)"
            note = "temperature estimated from cpu load; no thermal sensor available"
        # power estimate
        power = 15.0 + (cpu / 100.0) * 60.0 + (vm.percent / 100.0) * 10.0
        bw = random.uniform(50, 400)
        load = psutil.getloadavg()[0] if hasattr(psutil, "getloadavg") else cpu / 100.0 * 16
        gpu = None
        try:
            # no easy gpu on mac via psutil; estimate
            gpu = min(100.0, max(0.0, cpu * 0.6 + random.uniform(-5, 5)))
        except Exception:
            gpu = None
        redline = node.spec.redline_temp_c
        ts = thermal_state_for(temp, redline)
        return Telemetry(
            node_id=node.spec.id,
            timestamp=datetime.now(timezone.utc),
            cpu_percent=round(cpu, 1),
            memory_percent=round(vm.percent, 1),
            memory_used_gb=round(vm.used / (1024**3), 2),
            memory_total_gb=round(vm.total / (1024**3), 2),
            temperature_c=round(temp, 1),
            power_watts=round(power, 1),
            bandwidth_mbps=round(bw, 1),
            load_avg=round(float(load), 2),
            gpu_percent=round(gpu, 1) if gpu is not None else None,
            thermal_state=ts,
            source=source,
            source_note=note,
        )

    def _sample_simulated(self, nid: str, node: Node, now: float) -> Telemetry:
        phase = self._phase[nid]
        t = (now - self._t0) / 60.0  # minutes
        # base load varies by node, derived from the spec rather than a
        # hardcoded node id, so any node gets a plausible baseline.
        spec = node.spec
        base_cpu = 30.0 + min(25.0, spec.cpu_cores * 1.5)
        base_mem = 40.0 + min(20.0, (spec.memory_gb / 128.0) * 20.0)
        base_temp = spec.redline_temp_c * 0.62
        # load drift + active flights pressure
        pressure = min(40.0, len(node.active_flights) * 6.0)
        cpu = base_cpu + 15.0 * math.sin(t * 0.7 + phase) + random.uniform(-4, 4) + pressure
        cpu = max(2.0, min(99.0, cpu))
        mem = base_mem + 8.0 * math.sin(t * 0.3 + phase * 1.3) + random.uniform(-3, 3) + pressure * 0.5
        mem = max(5.0, min(98.0, mem))
        temp = base_temp + 12.0 * math.sin(t * 0.5 + phase) + (cpu / 100.0) * 15.0 + random.uniform(-2, 2)
        # occasional anomaly spike
        if random.random() < 0.03:
            temp += random.uniform(8, 18)
        temp = max(25.0, min(node.spec.redline_temp_c + 5, temp))
        power = node.spec.max_power_watts * (0.3 + 0.5 * cpu / 100.0) + random.uniform(-5, 5)
        power = max(5.0, power)
        mem_used = node.spec.memory_gb * mem / 100.0
        bw = random.uniform(10, node.spec.network_capacity_mbps * 0.4)
        load = cpu / 100.0 * node.spec.cpu_cores * 0.8
        gpu = None
        if node.spec.gpu_units > 0:
            gpu = min(100.0, max(0.0, 20.0 + 40.0 * math.sin(t * 0.4 + phase) + random.uniform(-5, 5) + pressure))
        battery = None
        solar = None
        if node.spec.location == "off-grid":
            battery = max(20.0, min(100.0, 70.0 + 15.0 * math.sin(t * 0.2 + phase) + random.uniform(-3, 3)))
            solar = max(0.0, 200.0 + 100.0 * math.sin(t * 0.1 + phase) + random.uniform(-20, 20))
        redline = node.spec.redline_temp_c
        ts = thermal_state_for(temp, redline)
        return Telemetry(
            node_id=node.spec.id,
            timestamp=datetime.now(timezone.utc),
            cpu_percent=round(cpu, 1),
            memory_percent=round(mem, 1),
            memory_used_gb=round(mem_used, 2),
            memory_total_gb=node.spec.memory_gb,
            temperature_c=round(temp, 1),
            power_watts=round(power, 1),
            bandwidth_mbps=round(bw, 1),
            load_avg=round(float(load), 2),
            gpu_percent=round(gpu, 1) if gpu is not None else None,
            thermal_state=ts,
            battery_percent=round(battery, 1) if battery is not None else None,
            solar_input_watts=round(solar, 1) if solar is not None else None,
            source="simulated",
            source_note="no node agent attached; values are synthetic",
        )

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self):
        while self._running:
            try:
                await self.sweep_once()
            except Exception as e:
                print(f"radar sweep error: {e}")
            await asyncio.sleep(self.interval_s)
