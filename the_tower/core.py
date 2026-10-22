"""The Tower - Hardware Air Traffic Control engine.

Monitors the physical airspace, clears compute tasks for takeoff, and
ruthlessly shoots down rogue flights before they melt a motherboard.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from .schemas import (
    Node, NodeSpec, Telemetry, ThermalState, NodeStatus,
    Flight, FlightStatus, ClearanceRequest, ClearanceResponse,
    ClearanceDecision, FlightPriority, ResourceWeight,
    KillRequest, KillResponse, TowerEvent, EventKind,
    AirspaceSnapshot, default_nodes,
)
from .radar import RadarSweep
from .anomaly import AnomalyRadar
from .storage import Storage
import logging

logger = logging.getLogger("the_tower")


class Tower:
    """Hardware ATC controller."""

    def __init__(self, nodes: Optional[List[NodeSpec]] = None, radar_interval_s: float = 5.0,
                 max_events: int = 500, db_path: Optional[str] = None):
        self.db_path = db_path
        self.storage: Optional[Storage] = Storage(db_path) if db_path else None
        # load node specs from storage if present
        if nodes is None and self.storage is not None:
            specs_map = self.storage.load_node_specs()
            if specs_map:
                import json as _j
                nodes = [NodeSpec(**_j.loads(v)) for v in specs_map.values()]
        specs = nodes if nodes is not None else default_nodes()
        self.nodes: Dict[str, Node] = {}
        for spec in specs:
            # seed telemetry
            from .radar import RadarSweep as _R
            # build a placeholder telemetry via radar sampling later; create minimal now
            self.nodes[spec.id] = Node(
                spec=spec,
                status=NodeStatus.ONLINE,
                telemetry=Telemetry(
                    node_id=spec.id,
                    timestamp=datetime.now(timezone.utc),
                    cpu_percent=10.0,
                    memory_percent=20.0,
                    memory_used_gb=spec.memory_gb * 0.2,
                    memory_total_gb=spec.memory_gb,
                    temperature_c=spec.redline_temp_c * 0.5,
                    power_watts=spec.max_power_watts * 0.2,
                    bandwidth_mbps=0.0,
                    load_avg=0.5,
                    thermal_state=ThermalState.NOMINAL,
                ),
            )
        self.flights: Dict[str, Flight] = {}
        self.events: deque = deque(maxlen=max_events)
        self.anomaly = AnomalyRadar()
        self.radar = RadarSweep(self.nodes, interval_s=radar_interval_s)
        self.radar.subscribe(self._on_sweep)
        self._lock = asyncio.Lock()
        self._killed_containers: Dict[str, str] = {}  # container_id -> flight_id
        self._subscribers: List[Callable] = []
        self._async_subscribers: List[Callable] = []
        self.containers: Dict[str, Dict[str, Any]] = {}  # container_id -> info
        # persist node specs
        if self.storage is not None:
            for spec in specs:
                self.storage.upsert_node_spec(spec.id, spec.model_dump_json())
    # ---------------- lifecycle ----------------
    async def start(self):
        import time
        self._started_at = time.time()
        # load persisted state
        if self.storage is not None:
            for f in self.storage.load_flights():
                self.flights[f.id] = f
            for e in self.storage.load_events():
                self.events.append(e)
            for nid, node in self.nodes.items():
                hist = self.storage.load_telemetry(nid, limit=60)
                if hist:
                    node.history = hist
                    node.telemetry = hist[-1]
        # initial sweep to populate real telemetry
        await self.radar.sweep_once()
        await self.radar.start()
        self._emit(EventKind.NODE_ONLINE, None, None, "Tower online: airspace monitored", "info")

    async def stop(self):
        await self.radar.stop()

    def subscribe(self, cb: Callable):
        self._subscribers.append(cb)

    def subscribe_async(self, cb: Callable):
        self._async_subscribers.append(cb)

    def _emit(self, kind: EventKind, node_id=None, flight_id=None, message="", severity="info",
              container_id=None, data=None):
        ev = TowerEvent(
            kind=kind, node_id=node_id, flight_id=flight_id, container_id=container_id,
            message=message, severity=severity, data=data or {},
        )
        self.events.append(ev)
        if self.storage is not None:
            self.storage.append_event(ev)
        logger.debug("event %s %s", ev.kind.value, ev.message)
        for cb in list(self._subscribers):
            try:
                cb(ev)
            except Exception:
                pass
        for cb in list(self._async_subscribers):
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(cb(ev))
            except Exception:
                pass
        return ev

    async def _on_sweep(self, samples):
        if self.storage is not None:
            for nid, node in self.nodes.items():
                self.storage.append_telemetry(node.telemetry)
        # anomaly analysis per node
        for nid, node in self.nodes.items():
            for ev in self.anomaly.analyze_node(node):
                self.events.append(ev)
                # auto-kill check: if redline breach, shoot down active flights on that node
                if ev.kind == EventKind.REDLINE:
                    await self._auto_kill_on_redline(nid)
        # stale container kill-switch
        for cid in self.stale_containers():
            try:
                await self.kill(KillRequest(container_id=cid, reason="heartbeat timeout", requester="tower"))
            except Exception as e:
                print(f"stale kill error: {e}")
        # update flight statuses: in-flight flights may land randomly-ish based on duration
        await self._progress_flights()
        await self._dispatch_queued()

    async def _auto_kill_on_redline(self, node_id: str):
        node = self.nodes.get(node_id)
        if not node:
            return
        for fid in list(node.active_flights):
            flight = self.flights.get(fid)
            if flight and flight.status == FlightStatus.IN_FLIGHT:
                node.active_flights = [f for f in node.active_flights if f != fid]
                node.active_weights.pop(fid, None)
                if flight.retries < flight.max_retries:
                    flight.retries += 1
                    flight.status = FlightStatus.QUEUED
                    flight.assigned_node = None
                    flight.takeoff_at = None
                    flight.cleared_at = None
                    flight.reason = f"retry {flight.retries}/{flight.max_retries}: node redline breach"
                else:
                    flight.status = FlightStatus.SHOT_DOWN
                    flight.shot_down_at = datetime.now(timezone.utc)
                    flight.reason = "auto-kill: node redline breach (max retries)"
                if self.storage is not None:
                    self.storage.upsert_flight(flight)
                self._emit(EventKind.SHOT_DOWN, node_id=node_id, flight_id=fid,
                           message=f"SHOT DOWN {flight.callsign} on {node.spec.name}: redline breach",
                           severity="critical", data={"auto": True})

    async def _progress_flights(self):
        now = datetime.now(timezone.utc)
        for fid, flight in list(self.flights.items()):
            if flight.status == FlightStatus.IN_FLIGHT and flight.takeoff_at:
                elapsed = (now - flight.takeoff_at).total_seconds()
                if elapsed >= flight.weight.duration_est_s:
                    # land
                    flight.status = FlightStatus.LANDED
                    flight.landed_at = now
                    node = self.nodes.get(flight.assigned_node) if flight.assigned_node else None
                    if node and fid in node.active_flights:
                        node.active_flights = [f for f in node.active_flights if f != fid]
                    if node:
                        node.active_weights.pop(fid, None)
                    if self.storage is not None:
                        self.storage.upsert_flight(flight)
                    self._emit(EventKind.LANDING, node_id=flight.assigned_node, flight_id=fid,
                               message=f"{flight.callsign} landed on {flight.assigned_node}",
                               severity="info")

    async def _dispatch_queued(self):
        queued = sorted(
            [f for f in self.flights.values() if f.status == FlightStatus.QUEUED],
            key=lambda f: (int(f.priority), f.submitted_at),
        )
        for f in queued:
            if f.status != FlightStatus.QUEUED:
                continue
            try:
                await self.request_clearance(ClearanceRequest(flight=f))
            except Exception as e:
                print(f"dispatch error: {e}")

    # ---------------- clearance ----------------
    async def request_clearance(self, req: ClearanceRequest) -> ClearanceResponse:
        async with self._lock:
            flight = req.flight
            # ensure stored
            self.flights[flight.id] = flight
            self._emit(EventKind.CLEARANCE_REQUEST, flight_id=flight.id,
                       message=f"Clearance requested: {flight.callsign} ({flight.task[:60]})",
                       severity="info", data={"weight": flight.weight.model_dump()})

            # rogue detection pre-check
            if flight.rogue:
                flight.status = FlightStatus.SHOT_DOWN
                flight.shot_down_at = datetime.now(timezone.utc)
                flight.reason = "rogue flight declared - shot down on approach"
                if self.storage is not None:
                    self.storage.upsert_flight(flight)
                self._emit(EventKind.SHOT_DOWN, flight_id=flight.id,
                           message=f"SHOT DOWN on approach: {flight.callsign} (rogue)",
                           severity="critical")
                return ClearanceResponse(decision=ClearanceDecision.SHOT_DOWN, flight_id=flight.id,
                                        reason=flight.reason)

            # scope conflict / preemption
            conflict_id = self.scope_conflict(flight)
            if conflict_id:
                existing = self.flights.get(conflict_id)
                if existing and int(flight.priority) < int(existing.priority):
                    self._preempt(conflict_id, f"preempted by {flight.callsign} (higher priority)")
                else:
                    flight.status = FlightStatus.QUEUED
                    flight.assigned_node = existing.assigned_node if existing else None
                    if flight.assigned_node and flight.assigned_node in self.nodes:
                        self.nodes[flight.assigned_node].queued_flights.append(flight.id)
                    if self.storage is not None:
                        self.storage.upsert_flight(flight)
                    self._emit(EventKind.CLEARANCE_DENIED, flight_id=flight.id,
                               message=f"Queued: {flight.callsign} - scope conflict with {existing.callsign if existing else conflict_id}",
                               severity="warn")
                    return ClearanceResponse(decision=ClearanceDecision.QUEUED, flight_id=flight.id,
                                            assigned_node=flight.assigned_node,
                                            reason=f"Scope conflict with {existing.callsign if existing else conflict_id}",
                                            estimated_wait_s=30.0)

            # candidate nodes
            candidates = []
            for nid, node in self.nodes.items():
                if flight.preferred_node and flight.preferred_node != nid:
                    continue
                if node.can_accept(flight.weight):
                    # score: prefer lower temp, lower load, more free mem
                    score = self._score_node(node, flight)
                    candidates.append((score, node))

            if not candidates:
                # maybe queue if any node could accept after current flights land? deny for now with queue
                flight.status = FlightStatus.QUEUED
                node = self._best_eventual_node(flight)
                flight.assigned_node = node
                if node:
                    self.nodes[node].queued_flights.append(flight.id)
                if self.storage is not None:
                    self.storage.upsert_flight(flight)
                self._emit(EventKind.CLEARANCE_DENIED, flight_id=flight.id,
                           message=f"Denied/queued: {flight.callsign} - no capacity",
                           severity="warn")
                return ClearanceResponse(decision=ClearanceDecision.QUEUED, flight_id=flight.id,
                                        assigned_node=node,
                                        reason="No node has sufficient safe capacity; queued",
                                        estimated_wait_s=30.0)

            # pick best
            candidates.sort(key=lambda x: x[0], reverse=True)
            best_node = candidates[0][1]
            # anomaly check: would this flight be rogue on best node?
            if self.anomaly.is_rogue_flight(best_node):
                flight.status = FlightStatus.SHOT_DOWN
                flight.shot_down_at = datetime.now(timezone.utc)
                flight.reason = "anomaly radar: node in runaway state - flight shot down"
                if self.storage is not None:
                    self.storage.upsert_flight(flight)
                self._emit(EventKind.SHOT_DOWN, node_id=best_node.spec.id, flight_id=flight.id,
                           message=f"SHOT DOWN: {flight.callsign} - target node runaway",
                           severity="critical")
                return ClearanceResponse(decision=ClearanceDecision.SHOT_DOWN, flight_id=flight.id,
                                        assigned_node=best_node.spec.id, reason=flight.reason)

            # clear for takeoff
            flight.status = FlightStatus.CLEARED
            flight.cleared_at = datetime.now(timezone.utc)
            flight.assigned_node = best_node.spec.id
            flight.takeoff_at = datetime.now(timezone.utc)
            flight.status = FlightStatus.IN_FLIGHT
            best_node.active_flights.append(flight.id)
            best_node.active_weights[flight.id] = flight.weight
            if self.storage is not None:
                self.storage.upsert_flight(flight)
            if flight.id in best_node.queued_flights:
                best_node.queued_flights = [f for f in best_node.queued_flights if f != flight.id]
            self._emit(EventKind.CLEARANCE_GRANTED, node_id=best_node.spec.id, flight_id=flight.id,
                       message=f"CLEARED FOR TAKEOFF: {flight.callsign} -> {best_node.spec.name}",
                       severity="info")
            self._emit(EventKind.TAKEOFF, node_id=best_node.spec.id, flight_id=flight.id,
                       message=f"{flight.callsign} airborne on {best_node.spec.name}",
                       severity="info")
            return ClearanceResponse(decision=ClearanceDecision.CLEARED, flight_id=flight.id,
                                    assigned_node=best_node.spec.id,
                                    reason=f"Cleared on {best_node.spec.name}")

    def _score_node(self, node: Node, flight: Flight) -> float:
        t = node.telemetry
        free = node.capacity_free()
        # higher is better
        temp_headroom = (node.spec.redline_temp_c - t.temperature_c) / node.spec.redline_temp_c
        load_factor = 1.0 - (t.cpu_percent / 100.0)
        mem_factor = free["memory_gb"] / node.spec.memory_gb
        power_headroom = 1.0 - (t.power_watts / node.spec.max_power_watts)
        priority_boost = (4 - int(flight.priority)) * 0.05
        return (temp_headroom * 0.35 + load_factor * 0.25 + mem_factor * 0.25 +
                power_headroom * 0.15 + priority_boost)

    def _best_eventual_node(self, flight: Flight) -> Optional[str]:
        # node with most free memory among online
        best = None
        best_mem = -1
        for nid, node in self.nodes.items():
            if node.status == NodeStatus.OFFLINE:
                continue
            if flight.preferred_node and flight.preferred_node != nid:
                continue
            free = node.capacity_free()
            if free["memory_gb"] > best_mem:
                best_mem = free["memory_gb"]
                best = nid
        return best

    # ---------------- kill ----------------
    async def kill(self, req: KillRequest) -> KillResponse:
        response = None
        async with self._lock:
            # find flight by container
            flight_id = self._killed_containers.get(req.container_id)
            if flight_id is None:
                for fid, f in self.flights.items():
                    if f.container_id == req.container_id:
                        flight_id = fid
                        break
            node_id = None
            flight = self.flights.get(flight_id) if flight_id else None
            if flight:
                node_id = flight.assigned_node
                if flight.status in (FlightStatus.IN_FLIGHT, FlightStatus.CLEARED, FlightStatus.QUEUED):
                    flight.status = FlightStatus.SHOT_DOWN
                    flight.shot_down_at = datetime.now(timezone.utc)
                    flight.reason = f"killed: {req.reason}"
                node = self.nodes.get(node_id) if node_id else None
                if node and flight_id in node.active_flights:
                    node.active_flights = [f for f in node.active_flights if f != flight_id]
                if node and flight_id in node.queued_flights:
                    node.queued_flights = [f for f in node.queued_flights if f != flight_id]
                if node:
                    node.active_weights.pop(flight_id, None)
                if flight and self.storage is not None:
                    self.storage.upsert_flight(flight)
            # actual process kill (best effort) for local containers
            killed_proc = False
            try:
                # if container_id looks like a pid
                if req.container_id.isdigit():
                    import os, signal
                    os.kill(int(req.container_id), signal.SIGKILL)
                    killed_proc = True
            except Exception:
                killed_proc = False
            self._killed_containers[req.container_id] = flight_id or ""
            self._emit(EventKind.KILL, node_id=node_id, flight_id=flight_id,
                       container_id=req.container_id,
                       message=f"KILL executed: {req.container_id} ({req.reason})",
                       severity="critical")
            response = KillResponse(
                success=True,
                container_id=req.container_id,
                flight_id=flight_id,
                node_id=node_id,
                message=f"Killed {req.container_id}" + (" (proc terminated)" if killed_proc else ""),
            )
        await self._dispatch_queued()
        return response

    # ---------------- queries ----------------
    def get_node(self, node_id: str) -> Optional[Node]:
        return self.nodes.get(node_id)

    # ---------------- node registration ----------------
    def register_node(self, spec: NodeSpec) -> Node:
        if spec.id in self.nodes:
            return self.nodes[spec.id]
        node = Node(
            spec=spec,
            status=NodeStatus.ONLINE,
            telemetry=Telemetry(
                node_id=spec.id,
                timestamp=datetime.now(timezone.utc),
                cpu_percent=10.0,
                memory_percent=20.0,
                memory_used_gb=spec.memory_gb * 0.2,
                memory_total_gb=spec.memory_gb,
                temperature_c=spec.redline_temp_c * 0.5,
                power_watts=spec.max_power_watts * 0.2,
                bandwidth_mbps=0.0,
                load_avg=0.5,
                thermal_state=ThermalState.NOMINAL,
            ),
        )
        self.nodes[spec.id] = node
        self.radar.nodes[spec.id] = node
        if self.storage is not None:
            self.storage.upsert_node_spec(spec.id, spec.model_dump_json())
        self._emit(EventKind.NODE_ONLINE, node_id=spec.id,
                   message=f"Node registered: {spec.name}", severity="info")
        return node

    def unregister_node(self, node_id: str) -> bool:
        if node_id not in self.nodes:
            return False
        # shoot down flights on that node
        node = self.nodes[node_id]
        for fid in list(node.active_flights):
            f = self.flights.get(fid)
            if f:
                f.status = FlightStatus.SHOT_DOWN
                f.shot_down_at = datetime.now(timezone.utc)
                f.reason = "node unregistered"
                if self.storage is not None:
                    self.storage.upsert_flight(f)
        del self.nodes[node_id]
        self.radar.nodes.pop(node_id, None)
        self._emit(EventKind.NODE_OFFLINE, node_id=node_id,
                   message=f"Node unregistered: {node_id}", severity="warn")
        return True

    # ---------------- conflict detection ----------------
    def scope_conflict(self, flight: Flight) -> Optional[str]:
        if not flight.scope:
            return None
        for fid, f in self.flights.items():
            if f.status != FlightStatus.IN_FLIGHT:
                continue
            if not f.scope:
                continue
            if set(f.scope) & set(flight.scope):
                return fid
        return None

    def _preempt(self, flight_id: str, reason: str):
        f = self.flights.get(flight_id)
        if not f or f.status != FlightStatus.IN_FLIGHT:
            return
        f.status = FlightStatus.SHOT_DOWN
        f.shot_down_at = datetime.now(timezone.utc)
        f.reason = reason
        node = self.nodes.get(f.assigned_node) if f.assigned_node else None
        if node:
            node.active_flights = [x for x in node.active_flights if x != flight_id]
            node.active_weights.pop(flight_id, None)
        if self.storage is not None:
            self.storage.upsert_flight(f)
        self._emit(EventKind.SHOT_DOWN, node_id=f.assigned_node, flight_id=flight_id,
                   message=f"PREEMPTED {f.callsign}: {reason}", severity="critical")

    def radar_snapshot(self) -> List[Node]:
        return list(self.nodes.values())

    def airspace(self) -> AirspaceSnapshot:
        flights = list(self.flights.values())
        events = list(self.events)[-100:]
        stats = {
            "nodes_online": sum(1 for n in self.nodes.values() if n.status != NodeStatus.OFFLINE),
            "nodes_total": len(self.nodes),
            "flights_total": len(flights),
            "flights_in_flight": sum(1 for f in flights if f.status == FlightStatus.IN_FLIGHT),
            "flights_queued": sum(1 for f in flights if f.status == FlightStatus.QUEUED),
            "flights_shot_down": sum(1 for f in flights if f.status == FlightStatus.SHOT_DOWN),
            "flights_landed": sum(1 for f in flights if f.status == FlightStatus.LANDED),
            "max_temp": max((n.telemetry.temperature_c for n in self.nodes.values()), default=0.0),
        }
        return AirspaceSnapshot(nodes=list(self.nodes.values()), flights=flights, events=events, stats=stats)

    def recent_events(self, limit: int = 50) -> List[TowerEvent]:
        return list(self.events)[-limit:]

    # ---------------- containers ----------------
    def register_container(self, container_id: str, flight_id: Optional[str] = None,
                           node_id: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None):
        self.containers[container_id] = {
            "container_id": container_id,
            "flight_id": flight_id,
            "node_id": node_id,
            "metadata": metadata or {},
            "last_heartbeat": datetime.now(timezone.utc),
            "status": "running",
        }
        return self.containers[container_id]

    def heartbeat(self, container_id: str) -> bool:
        if container_id not in self.containers:
            self.register_container(container_id)
        self.containers[container_id]["last_heartbeat"] = datetime.now(timezone.utc)
        self.containers[container_id]["status"] = "running"
        return True

    def stale_containers(self, timeout_s: float = 30.0) -> List[str]:
        now = datetime.now(timezone.utc)
        out = []
        for cid, info in self.containers.items():
            if info["status"] != "running":
                continue
            if (now - info["last_heartbeat"]).total_seconds() > timeout_s:
                out.append(cid)
        return out

    def trend(self, node_id: str) -> dict:
        node = self.nodes.get(node_id)
        if not node:
            return {}
        return self.anomaly.trend_summary(node)

    # ---------------- audit ----------------
    def log_audit(self, payload: Dict[str, Any]) -> Optional[str]:
        if self.storage is None:
            return None
        return self.storage.append_audit(payload)

    def verify_audit(self) -> bool:
        if self.storage is None:
            return True
        return self.storage.verify_audit_chain()

    def ingest_telemetry(self, node_id: str, telem: Telemetry) -> bool:
        node = self.nodes.get(node_id)
        if not node:
            return False
        node.telemetry = telem
        node.last_seen = datetime.now(timezone.utc)
        node.history.append(telem)
        if len(node.history) > 60:
            node.history = node.history[-60:]
        node.status = self.radar._status_for(node, telem) if hasattr(self.radar, "_status_for") else node.status
        return True
