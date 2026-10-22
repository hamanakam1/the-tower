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
from .policy import MeshSnapshot, NodeView, ScopeView, Decision, decide, POLICY_VERSION
from .safety import Ladder, assess, throttled_weight, THROTTLE_FACTOR, ladder_summary
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
        # scope key -> flight ids that claim it (only in-flight ones matter)
        self._scope_index: Dict[str, List[str]] = {}
        # flight ids currently waiting for clearance
        self._queued_ids: set = set()
        # idempotency key -> last clearance response
        self._idempotency: Dict[str, Dict[str, Any]] = {}
        # the most recent admission decision (explainability / debugging)
        self._last_decision: Optional[Decision] = None
        # node id -> latest safety-ladder assessment
        self._node_ladder: Dict[str, Any] = {}
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
            for f in await self._store(self.storage.load_flights) or []:
                self.flights[f.id] = f
                if f.status == FlightStatus.IN_FLIGHT:
                    self._index_scope(f)
                elif f.status == FlightStatus.QUEUED:
                    self._queued_ids.add(f.id)
            for e in await self._store(self.storage.load_events) or []:
                self.events.append(e)
            for nid, node in self.nodes.items():
                hist = await self._store(self.storage.load_telemetry, nid, 60) or []
                if hist:
                    node.history = hist
                    node.telemetry = hist[-1]
            for c in await self._store(self.storage.load_containers) or []:
                hb = c.get("last_heartbeat")
                if isinstance(hb, str):
                    try:
                        c["last_heartbeat"] = datetime.fromisoformat(hb)
                    except ValueError:
                        pass
                self.containers[c["container_id"]] = c
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
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(asyncio.to_thread(self.storage.append_event, ev))
                else:
                    self.storage.append_event(ev)
            except Exception:
                try:
                    self.storage.append_event(ev)
                except Exception:
                    pass
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
                await self._store(self.storage.append_telemetry, node.telemetry)
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
        if node.spec.enforcement != "enforce":
            # advisory node: recommend the kill, do not perform it
            self._emit(EventKind.REDLINE, node_id=node_id,
                       message=(f"ADVISORY: {node.spec.name} at redline - "
                                f"recommend killing {len(node.active_flights)} flight(s)"),
                       severity="critical",
                       data={"recommendation": "kill", "flights": list(node.active_flights)})
            return
        for fid in list(node.active_flights):
            flight = self.flights.get(fid)
            if flight and flight.status == FlightStatus.IN_FLIGHT:
                node.active_flights = [f for f in node.active_flights if f != fid]
                node.active_weights.pop(fid, None)
                self._deindex_scope(flight)
                if flight.retries < flight.max_retries:
                    flight.retries += 1
                    flight.status = FlightStatus.QUEUED
                    self._queued_ids.add(fid)
                    flight.assigned_node = None
                    flight.takeoff_at = None
                    flight.cleared_at = None
                    flight.reason = f"retry {flight.retries}/{flight.max_retries}: node redline breach"
                else:
                    flight.status = FlightStatus.SHOT_DOWN
                    self._queued_ids.discard(fid)
                    flight.shot_down_at = datetime.now(timezone.utc)
                    flight.reason = "auto-kill: node redline breach (max retries)"
                if self.storage is not None:
                    await self._store(self.storage.upsert_flight, flight)
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
                    self._queued_ids.discard(fid)
                    flight.landed_at = now
                    node = self.nodes.get(flight.assigned_node) if flight.assigned_node else None
                    if node and fid in node.active_flights:
                        node.active_flights = [f for f in node.active_flights if f != fid]
                    if node:
                        node.active_weights.pop(fid, None)
                    self._deindex_scope(flight)
                    if self.storage is not None:
                        await self._store(self.storage.upsert_flight, flight)
                    self._emit(EventKind.LANDING, node_id=flight.assigned_node, flight_id=fid,
                               message=f"{flight.callsign} landed on {flight.assigned_node}",
                               severity="info")

    async def _dispatch_queued(self):
        # The queued index keeps this proportional to the number of *waiting*
        # flights instead of the total flight history.
        if not self._queued_ids:
            return
        queued = [self.flights[fid] for fid in list(self._queued_ids)
                  if fid in self.flights and self.flights[fid].status == FlightStatus.QUEUED]
        if not queued:
            return
        queued.sort(key=lambda f: (int(f.priority), f.submitted_at))
        for f in queued:
            if f.status != FlightStatus.QUEUED:
                continue
            try:
                await self.request_clearance(ClearanceRequest(flight=f))
            except Exception as e:
                logger.warning("dispatch error: %s", e)

    # ---------------- clearance ----------------
    def idempotent_result(self, key: str) -> Optional[Dict[str, Any]]:
        """Return a previously issued response for this idempotency key."""
        entry = self._idempotency.get(key)
        if entry is None:
            return None
        entry["hits"] += 1
        return entry["response"]

    def remember_idempotent(self, key: str, response: Dict[str, Any]):
        """Record a clearance response so a retried request never double-schedules."""
        self._idempotency[key] = {"response": response, "hits": 0}
        # bounded cache: the oldest keys are dropped first
        if len(self._idempotency) > 1024:
            for old in list(self._idempotency.keys())[:256]:
                self._idempotency.pop(old, None)

    def snapshot(self) -> MeshSnapshot:
        """Freeze the observed world for a pure admission decision."""
        now = datetime.now(timezone.utc)
        nodes: List[NodeView] = []
        for node in self.nodes.values():
            t = node.telemetry
            reserved = node.reserved()
            age = None
            if node.last_seen:
                age = max(0.0, (now - node.last_seen).total_seconds())
            nodes.append(NodeView(
                id=node.spec.id,
                name=node.spec.name,
                status=node.status.value,
                cpu_percent=t.cpu_percent,
                memory_percent=t.memory_percent,
                temperature_c=t.temperature_c,
                power_watts=t.power_watts,
                thermal_state=t.thermal_state.value,
                gpu_percent=t.gpu_percent,
                cpu_cores=node.spec.cpu_cores,
                memory_gb=node.spec.memory_gb,
                gpu_units=node.spec.gpu_units,
                redline_temp_c=node.spec.redline_temp_c,
                max_power_watts=node.spec.max_power_watts,
                network_capacity_mbps=node.spec.network_capacity_mbps,
                reserved_cpu=reserved["cpu"],
                reserved_memory_gb=reserved["memory_gb"],
                reserved_gpu=reserved["gpu"],
                telemetry_age_s=age,
            ))
        in_flight: List[ScopeView] = []
        for f in self.flights.values():
            if f.status == FlightStatus.IN_FLIGHT and f.scope:
                in_flight.append(ScopeView(
                    flight_id=f.id, callsign=f.callsign, scope=list(f.scope),
                    priority=int(f.priority), node_id=f.assigned_node,
                    assigned_node=f.assigned_node,
                ))
        return MeshSnapshot(nodes=nodes, in_flight=in_flight,
                            queued_count=len(self._queued_ids))

    async def request_clearance(self, req: ClearanceRequest) -> ClearanceResponse:
        async with self._lock:
            flight = req.flight
            # ensure stored
            self.flights[flight.id] = flight
            self._emit(EventKind.CLEARANCE_REQUEST, flight_id=flight.id,
                       message=f"Clearance requested: {flight.callsign} ({flight.task[:60]})",
                       severity="info", data={"weight": flight.weight.model_dump()})

            # --- PURE: decide from a frozen snapshot ---
            snap = self.snapshot()
            decision = decide(flight, snap)
            self._last_decision = decision

            # --- IMPURE: act on the decision ---

            # rogue flight: shot down on approach
            if decision.decision == ClearanceDecision.SHOT_DOWN:
                flight.status = FlightStatus.SHOT_DOWN
                self._queued_ids.discard(flight.id)
                flight.shot_down_at = datetime.now(timezone.utc)
                flight.reason = decision.reason
                if self.storage is not None:
                    await self._store(self.storage.upsert_flight, flight)
                self._emit(EventKind.SHOT_DOWN, flight_id=flight.id,
                           message=f"SHOT DOWN on approach: {flight.callsign} (rogue)",
                           severity="critical")
                return ClearanceResponse(decision=decision.decision, flight_id=flight.id,
                                        reason=decision.reason)

            # scope conflict: preempt the incumbent if we outrank it.
            # Preemption is an enforcement action: in advisory mode the
            # Tower recommends and records, it does not revoke clearance.
            if decision.preempt_flight_id:
                existing = self.flights.get(decision.preempt_flight_id)
                target_node = self.nodes.get(existing.assigned_node) if existing else None
                enforcing = (target_node.spec.enforcement == "enforce") if target_node else True
                if enforcing:
                    await self._preempt(decision.preempt_flight_id,
                                        f"preempted by {flight.callsign} (higher priority)")
                else:
                    self._emit(EventKind.ANOMALY, node_id=decision.preempt_flight_id,
                               flight_id=flight.id,
                               message=(f"ADVISORY: would preempt {existing.callsign if existing else decision.preempt_flight_id} "
                                        f"for {flight.callsign} - node is advisory-only"),
                               severity="warn",
                               data={"recommendation": "preempt", "target": decision.preempt_flight_id})

            # queued: no capacity, or a scope conflict we do not outrank
            if decision.decision == ClearanceDecision.QUEUED:
                flight.status = FlightStatus.QUEUED
                self._queued_ids.add(flight.id)
                flight.assigned_node = decision.assigned_node
                if decision.assigned_node and decision.assigned_node in self.nodes:
                    node = self.nodes[decision.assigned_node]
                    if flight.id not in node.queued_flights:
                        node.queued_flights.append(flight.id)
                if self.storage is not None:
                    await self._store(self.storage.upsert_flight, flight)
                self._emit(EventKind.CLEARANCE_DENIED, flight_id=flight.id,
                           message=f"Denied/queued: {flight.callsign} - {decision.reason}",
                           severity="warn")
                return ClearanceResponse(decision=decision.decision, flight_id=flight.id,
                                        assigned_node=decision.assigned_node,
                                        reason=decision.reason,
                                        estimated_wait_s=decision.estimated_wait_s,
                                        admission=decision.to_dict())

            # cleared: check the anomaly radar before letting it fly
            best_node = self.nodes.get(decision.assigned_node) if decision.assigned_node else None
            if best_node is not None and self.anomaly.is_rogue_flight(best_node):
                flight.status = FlightStatus.SHOT_DOWN
                self._queued_ids.discard(flight.id)
                flight.shot_down_at = datetime.now(timezone.utc)
                flight.reason = "anomaly radar: node in runaway state - flight shot down"
                if self.storage is not None:
                    await self._store(self.storage.upsert_flight, flight)
                self._emit(EventKind.SHOT_DOWN, node_id=best_node.spec.id, flight_id=flight.id,
                           message=f"SHOT DOWN: {flight.callsign} - target node runaway",
                           severity="critical")
                return ClearanceResponse(decision=ClearanceDecision.SHOT_DOWN, flight_id=flight.id,
                                        assigned_node=best_node.spec.id, reason=flight.reason,
                                        admission=decision.to_dict())

            # clear for takeoff
            flight.status = FlightStatus.CLEARED
            self._queued_ids.discard(flight.id)
            flight.cleared_at = datetime.now(timezone.utc)
            flight.assigned_node = decision.assigned_node
            flight.takeoff_at = datetime.now(timezone.utc)
            flight.status = FlightStatus.IN_FLIGHT

            # The safety ladder decides how much of the request the node
            # actually gets. A throttled flight still flies, but at a
            # reduced CPU share; a drained node takes no new work.
            ladder = Ladder.ADMIT
            if best_node is not None:
                view = next((n for n in self.snapshot().nodes if n.id == best_node.spec.id), None)
                if view is not None:
                    predicted = self.anomaly.trend_summary(best_node).get("secs_to_redline")
                    assessment = assess(view, flight, predicted)
                    ladder = assessment.ladder
                    self._node_ladder[best_node.spec.id] = assessment
                    if assessment.ladder in (Ladder.WARN, Ladder.THROTTLE, Ladder.DRAIN):
                        self._emit(EventKind.ANOMALY, node_id=best_node.spec.id,
                                   flight_id=flight.id,
                                   message=f"{assessment.ladder.value.upper()}: {assessment.reason}",
                                   severity=assessment.severity,
                                   data=assessment.to_dict())
                    if assessment.ladder == Ladder.DRAIN:
                        if best_node.spec.enforcement == "enforce":
                            # node is heading for redline: do not admit this flight
                            flight.status = FlightStatus.QUEUED
                            self._queued_ids.add(flight.id)
                            flight.takeoff_at = None
                            flight.cleared_at = None
                            flight.reason = f"node draining: {assessment.reason}"
                            if self.storage is not None:
                                await self._store(self.storage.upsert_flight, flight)
                            return ClearanceResponse(decision=ClearanceDecision.QUEUED,
                                                    flight_id=flight.id,
                                                    assigned_node=decision.assigned_node,
                                                    reason=flight.reason,
                                                    estimated_wait_s=60.0,
                                                    admission=decision.to_dict())
                        # advisory mode: recommend draining, but do not block the mesh
                        self._emit(EventKind.ANOMALY, node_id=best_node.spec.id,
                                   flight_id=flight.id,
                                   message=f"ADVISORY: recommend draining {best_node.spec.name} - {assessment.reason}",
                                   severity="warn", data=assessment.to_dict())
                    if assessment.ladder == Ladder.THROTTLE:
                        factor = THROTTLE_FACTOR.get(Ladder.THROTTLE, 0.5)
                        throttled = throttled_weight(flight, factor)
                        flight.weight = throttled.weight
                        flight.metadata = {**flight.metadata, "throttled": True,
                                           "throttle_factor": factor}

            if best_node is not None:
                best_node.active_flights.append(flight.id)
                best_node.active_weights[flight.id] = flight.weight
                if flight.id in best_node.queued_flights:
                    best_node.queued_flights = [f for f in best_node.queued_flights if f != flight.id]
            self._index_scope(flight)
            if self.storage is not None:
                await self._store(self.storage.upsert_flight, flight)
            await self.log_audit_async({
                "kind": "clearance",
                "flight_id": flight.id,
                "callsign": flight.callsign,
                "decision": decision.decision.value,
                "assigned_node": decision.assigned_node,
                "reason": decision.reason,
                "ladder": ladder.value,
                "admission": decision.to_dict(),
            })
            self._emit(EventKind.CLEARANCE_GRANTED, node_id=decision.assigned_node, flight_id=flight.id,
                       message=f"CLEARED FOR TAKEOFF: {flight.callsign} -> {best_node.spec.name if best_node else decision.assigned_node}",
                       severity="info")
            self._emit(EventKind.TAKEOFF, node_id=decision.assigned_node, flight_id=flight.id,
                       message=f"{flight.callsign} airborne on {best_node.spec.name if best_node else decision.assigned_node}",
                       severity="info")
            return ClearanceResponse(decision=ClearanceDecision.CLEARED, flight_id=flight.id,
                                    assigned_node=decision.assigned_node,
                                    reason=decision.reason,
                                    admission=decision.to_dict())

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
                    self._queued_ids.discard(flight.id)
                    flight.shot_down_at = datetime.now(timezone.utc)
                    flight.reason = f"killed: {req.reason}"
                node = self.nodes.get(node_id) if node_id else None
                if node and flight_id in node.active_flights:
                    node.active_flights = [f for f in node.active_flights if f != flight_id]
                if node and flight_id in node.queued_flights:
                    node.queued_flights = [f for f in node.queued_flights if f != flight_id]
                if node:
                    node.active_weights.pop(flight_id, None)
                if flight:
                    self._deindex_scope(flight)
                if flight and self.storage is not None:
                    await self._store(self.storage.upsert_flight, flight)
            # actual process kill (best effort) for local containers
            killed_proc = False
            try:
                # Only kill processes the Tower actually owns. A numeric
                # container_id is treated as a PID only when that container
                # was explicitly registered with a matching pid.
                info = self.containers.get(req.container_id)
                pid = None
                if info is not None:
                    pid = (info.get("metadata") or {}).get("pid")
                    if pid is None and req.container_id.isdigit():
                        pid = int(req.container_id)
                if pid is not None and info is not None:
                    import os, signal
                    os.kill(int(pid), signal.SIGKILL)
                    killed_proc = True
            except Exception:
                killed_proc = False
            self._killed_containers[req.container_id] = flight_id or ""
            self._emit(EventKind.KILL, node_id=node_id, flight_id=flight_id,
                       container_id=req.container_id,
                       message=f"KILL executed: {req.container_id} ({req.reason})",
                       severity="critical")
            await self.log_audit_async({"kind": "kill", "container_id": req.container_id,
                                        "flight_id": flight_id, "node_id": node_id,
                                        "reason": req.reason, "requester": req.requester})
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

    def set_enforcement(self, node_id: str, mode: str) -> bool:
        """Switch a node between 'advisory' and 'enforce'.

        This is an auditable, per-node opt-in. The change is recorded in the
        audit chain so "why was this flight not stopped?" has an answer.
        """
        node = self.nodes.get(node_id)
        if node is None:
            return False
        if mode not in ("advisory", "enforce"):
            return False
        previous = node.spec.enforcement
        node.spec.enforcement = mode
        if self.storage is not None:
            try:
                self.storage.upsert_node_spec(node_id, node.spec.model_dump_json())
            except Exception as e:
                logger.warning("node spec persist failed: %s", e)
        self._emit(EventKind.ANOMALY, node_id=node_id,
                   message=f"Enforcement mode for {node.spec.name}: {previous} -> {mode}",
                   severity="warn" if mode == "enforce" else "info",
                   data={"previous": previous, "mode": mode})
        self.log_audit({"kind": "enforcement_change", "node_id": node_id,
                        "previous": previous, "mode": mode})
        return True

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
                    self.storage.upsert_flight(f)  # sync path (unregister_node is sync)
        del self.nodes[node_id]
        self.radar.nodes.pop(node_id, None)
        self._emit(EventKind.NODE_OFFLINE, node_id=node_id,
                   message=f"Node unregistered: {node_id}", severity="warn")
        return True

    # ---------------- conflict detection ----------------
    def scope_conflict(self, flight: Flight) -> Optional[str]:
        """Return the id of an in-flight flight that shares a scope key.

        Uses the scope index rather than scanning every flight, so the cost
        is proportional to the number of *conflicting* flights, not to the
        size of the flight history.
        """
        if not flight.scope:
            return None
        for key in flight.scope:
            for fid in self._scope_index.get(key, ()):  # usually 0 or 1 entries
                f = self.flights.get(fid)
                if f is None:
                    continue
                if f.status != FlightStatus.IN_FLIGHT:
                    continue
                if set(f.scope) & set(flight.scope):
                    return fid
        return None

    def _index_scope(self, flight: Flight):
        for key in flight.scope:
            bucket = self._scope_index.setdefault(key, [])
            if flight.id not in bucket:
                bucket.append(flight.id)

    def _deindex_scope(self, flight: Flight):
        for key in flight.scope:
            bucket = self._scope_index.get(key)
            if bucket and flight.id in bucket:
                bucket.remove(flight.id)

    async def _preempt(self, flight_id: str, reason: str):
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
        self._deindex_scope(f)
        if self.storage is not None:
            await self._store(self.storage.upsert_flight, f)
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
        if self.storage is not None:
            try:
                self.storage.upsert_container(self.containers[container_id])
            except Exception as e:
                logger.warning("container persist failed: %s", e)
        return self.containers[container_id]

    def heartbeat(self, container_id: str) -> bool:
        if container_id not in self.containers:
            self.register_container(container_id)
        self.containers[container_id]["last_heartbeat"] = datetime.now(timezone.utc)
        self.containers[container_id]["status"] = "running"
        if self.storage is not None:
            try:
                self.storage.upsert_container(self.containers[container_id])
            except Exception as e:
                logger.warning("container persist failed: %s", e)
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

    def ladder(self) -> Dict[str, Any]:
        """Current enforcement-ladder state for every node."""
        out = {}
        counts: Dict[str, int] = {}
        for node in self.nodes.values():
            assessment = self._node_ladder.get(node.spec.id)
            if assessment is None:
                view = next((n for n in self.snapshot().nodes if n.id == node.spec.id), None)
                predicted = self.anomaly.trend_summary(node).get("secs_to_redline")
                assessment = assess(view, None, predicted) if view else None
            if assessment is None:
                continue
            out[node.spec.id] = assessment.to_dict()
            counts[assessment.ladder.value] = counts.get(assessment.ladder.value, 0) + 1
        return {"nodes": out, "counts": counts}

    # ---------------- persistence helpers ----------------
    async def _store(self, fn, *args, **kwargs):
        """Run a storage call off the event loop.

        SQLite calls are blocking. Every write from an async handler goes
        through a worker thread so the mesh is never stalled by disk I/O.
        """
        if self.storage is None:
            return None
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except Exception as e:
            logger.warning("storage call failed: %s", e)
            return None

    # ---------------- audit ----------------
    def log_audit(self, payload: Dict[str, Any]) -> Optional[str]:
        if self.storage is None:
            return None
        return self.storage.append_audit(payload)

    async def log_audit_async(self, payload: Dict[str, Any]) -> Optional[str]:
        if self.storage is None:
            return None
        return await self._store(self.storage.append_audit, payload)

    async def verify_audit_async(self) -> bool:
        if self.storage is None:
            return True
        return bool(await self._store(self.storage.verify_audit_chain))

    def ingest_telemetry(self, node_id: str, telem: Telemetry) -> bool:
        node = self.nodes.get(node_id)
        if not node:
            return False
        telem.source = "ingested"
        if telem.source_note is None:
            telem.source_note = f"reported by node agent for {node_id}"
        node.telemetry = telem
        node.last_seen = datetime.now(timezone.utc)
        node.history.append(telem)
        if len(node.history) > 60:
            node.history = node.history[-60:]
        node.status = self.radar._status_for(node, telem) if hasattr(self.radar, "_status_for") else node.status
        return True
