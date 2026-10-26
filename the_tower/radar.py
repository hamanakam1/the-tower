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
from .sensors import read_temperature

# States a sweep must not overwrite: they mean "a human (or a drain) decided
# this node should not take work".
_PINNED_STATUSES = {
    NodeStatus.CORDONED, NodeStatus.MAINTENANCE,
    NodeStatus.OFFLINE, NodeStatus.DRAINING,
}


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

    def register_node(self, node: Node) -> None:
        """Give a newly registered node sampler state before it joins a sweep."""
        self._phase[node.id] = random.random() * math.pi * 2
        self.nodes[node.id] = node

    def unregister_node(self, node_id: str) -> None:
        """Discard sampler state with the node so re-registration starts fresh."""
        self.nodes.pop(node_id, None)
        self._phase.pop(node_id, None)

    def subscribe(self, cb):
        self._subscribers.append(cb)

    async def sweep_once(self) -> Dict[str, Telemetry]:
        now = time.time()
        out: Dict[str, Telemetry] = {}
        for nid, node in self.nodes.items():
            sampler = getattr(node.spec, "sampler", None) or (
                "simulated" if getattr(node.spec, "simulate", True) else "agent")
            if sampler == "agent":
                # agent-managed node: never fabricate telemetry, only check
                # that the agent is still alive
                age = (datetime.now(timezone.utc) - node.last_seen).total_seconds()
                if age > node.spec.staleness_budget_s and node.status not in _PINNED_STATUSES:
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
        # Lifecycle states that an operator (or a drain) set on purpose are
        # not telemetry. A sweep must never quietly return a cordoned or
        # draining node to service: that is exactly the machine you took out
        # of the airspace on purpose.
        if node.status in _PINNED_STATUSES:
            return node.status
        if t.thermal_state == ThermalState.CRITICAL:
            return NodeStatus.REDLINE
        if t.cpu_percent > 92 or t.memory_percent > 92:
            return NodeStatus.OVERLOADED
        if t.thermal_state == ThermalState.SERIOUS:
            return NodeStatus.DEGRADED
        return NodeStatus.ONLINE

    def _sample(self, nid: str, node: Node, now: float) -> Telemetry:
        """Pick the sampler for this node.

        The sampler is a property of the node, not a hardcoded node id: any
        node can be a local machine, an agent-driven machine, or simulated.
        """
        sampler = getattr(node.spec, "sampler", None)
        if sampler is None:
            sampler = "local" if node.spec.location == "local" else "simulated"
        if sampler == "local":
            return self._sample_local(node, now)
        if sampler == "agent":
            # never fabricate for an agent-driven node
            return node.telemetry
        return self._sample_simulated(nid, node, now)

    def _sample_local(self, node: Node, now: float) -> Telemetry:
        cpu = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        # temperature: a real sensor if the platform has one, otherwise an
        # estimate that says so.
        reading = read_temperature(getattr(node.spec, "sensor", None), cpu)
        temp = reading.celsius if reading.celsius is not None else 45.0
        source = reading.source
        note = reading.note
        if reading.celsius is None and node.history:
            # The estimate is derived from an instantaneous CPU reading, so it
            # is noisy. A machine's temperature does not jump four degrees
            # between two sweeps, so the estimate is smoothed the way the
            # hardware behaves - otherwise every reading looks like a spike
            # and the anomaly radar has nothing left to say.
            previous = node.telemetry.temperature_c
            alpha = 0.25 if node.spec.thermal_mass >= 1.0 else 0.4
            temp = previous + (temp - previous) * alpha
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
        # Temperature moves with thermal inertia: the machine drifts toward the
        # temperature its load implies instead of jumping to it. A real board
        # cannot change 18C between two five second samples, and a signal
        # that does not behave like a machine teaches the anomaly radar to
        # cry wolf.
        target_temp = base_temp + (cpu / 100.0) * 15.0
        if not node.history:
            # The first sample of a node has no predecessor to drift from.
            # Starting at the seeded value would show up as a step change.
            temp = target_temp
        else:
            previous = node.telemetry.temperature_c
            alpha = 0.25 if node.spec.thermal_mass >= 1.0 else 0.4
            temp = previous + (target_temp - previous) * alpha + random.uniform(-0.4, 0.4)
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
