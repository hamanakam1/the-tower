"""The Tower - Hardware Air Traffic Control engine.

Monitors the physical airspace, clears compute tasks for takeoff, and
ruthlessly shoots down rogue flights before they melt a motherboard.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from .schemas import (
    Node, NodeSpec, Telemetry, ThermalState, NodeStatus,
    Flight, FlightStatus, ClearanceRequest, ClearanceResponse,
    ClearanceDecision, FlightPriority, ResourceWeight,
    KillRequest, KillResponse, TowerEvent, EventKind,
    AirspaceSnapshot, default_nodes, Lease, LeaseStatus, LeaseRequest,
)
from .radar import RadarSweep
from .anomaly import AnomalyRadar
from .storage import Storage
from .policy import MeshSnapshot, NodeView, ScopeView, Decision, decide, POLICY_VERSION
from .safety import Ladder, assess, throttled_weight, THROTTLE_FACTOR, ladder_summary
from .replay import snapshot_to_dict, snapshot_from_dict, replay, compare
import logging

logger = logging.getLogger("the_tower")

# A node is "online" when the Tower can still believe its reports. STALE
# telemetry (UNKNOWN) and OFFLINE are not online; lifecycle states on a live
# machine are.
_REACHABLE_STATUSES = {
    NodeStatus.ONLINE, NodeStatus.DEGRADED, NodeStatus.OVERLOADED,
    NodeStatus.REDLINE, NodeStatus.DRAINING,
}


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
        # last telemetry rollup (epoch seconds)
        self._last_rollup: float = 0.0
        # lease id -> Lease (the Tower's claims on node resources)
        self.leases: Dict[str, Lease] = {}
        # Processes the Tower is allowed to stop. Only a PID that was
        # explicitly registered with the Tower ends up here, and nothing
        # else on the host is ever signalled.
        self._owned_pids: Dict[int, str] = {}  # pid -> container_id
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
                # Ownership of a process survives a restart: a container that
                # was registered with a pid is a process the Tower may stop.
                self._claim_pid(c.get("metadata", {}).get("pid"), c["container_id"])
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
                logger.warning("stale kill error: %s", e)
        # telemetry retention: roll old samples up so history stays small
        await self._maybe_rollup()
        # release the resources of any lease whose worker went away
        reaped = self.reap_expired_leases()
        if reaped:
            await self._dispatch_queued()
        # update flight statuses: in-flight flights may land randomly-ish based on duration
        await self._progress_flights()
        await self._dispatch_queued()

    async def _maybe_rollup(self):
        """Roll up telemetry at most once every five minutes."""
        now = time.time()
        if now - self._last_rollup < 300:
            return
        self._last_rollup = now
        if self.storage is None:
            return
        result = await self._store(self.storage.rollup_telemetry, now)
        if result and result.get("rolled"):
            logger.info("telemetry rollup: %s samples rolled, %s deleted",
                        result["rolled"], result["deleted"])

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
                    self._release_lease_for(fid)
                    if self.storage is not None:
                        await self._store(self.storage.upsert_flight, flight)
                    self._emit(EventKind.LANDING, node_id=flight.assigned_node, flight_id=fid,
                               message=f"{flight.callsign} landed on {flight.assigned_node}",
                               severity="info")
            elif flight.status == FlightStatus.DRAINING:
                # A draining flight is still allowed to finish. It either
                # lands on its own, or it is terminated when the grace period
                # the operator asked for runs out.
                elapsed = (now - flight.takeoff_at).total_seconds() if flight.takeoff_at else 0.0
                if elapsed >= flight.weight.duration_est_s:
                    flight.status = FlightStatus.LANDED
                    flight.landed_at = now
                    node = self.nodes.get(flight.assigned_node) if flight.assigned_node else None
                    if node and fid in node.active_flights:
                        node.active_flights = [f for f in node.active_flights if f != fid]
                    if node:
                        node.active_weights.pop(fid, None)
                    self._deindex_scope(flight)
                    self._release_lease_for(fid)
                    if self.storage is not None:
                        await self._store(self.storage.upsert_flight, flight)
                    self._emit(EventKind.LANDING, node_id=flight.assigned_node, flight_id=fid,
                               message=f"{flight.callsign} drained and landed on {flight.assigned_node}",
                               severity="info")
                elif flight.drain_until and now >= flight.drain_until:
                    flight.status = FlightStatus.SHOT_DOWN
                    flight.shot_down_at = now
                    flight.reason = "drain deadline expired - terminated"
                    node = self.nodes.get(flight.assigned_node) if flight.assigned_node else None
                    if node and fid in node.active_flights:
                        node.active_flights = [f for f in node.active_flights if f != fid]
                    if node:
                        node.active_weights.pop(fid, None)
                    self._deindex_scope(flight)
                    self._release_lease_for(fid)
                    if self.storage is not None:
                        await self._store(self.storage.upsert_flight, flight)
                    self._emit(EventKind.SHOT_DOWN, node_id=flight.assigned_node, flight_id=fid,
                               message=f"SHOT DOWN {flight.callsign}: drain deadline expired",
                               severity="critical",
                               data={"drain": True})

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
            if self._blocked_on_advisory_preempt(f):
                # Re-asking every tick would republish the same recommendation
                # forever. The blocker is re-checked each sweep, so the flight
                # resumes as soon as the incumbent lands or the node is enforced.
                continue
            try:
                await self.request_clearance(ClearanceRequest(flight=f), announce=False)
            except Exception as e:
                logger.warning("dispatch error: %s", e)

    def _blocked_on_advisory_preempt(self, flight: Flight) -> bool:
        """True while a queued flight is waiting for an advisory preemption.

        The block clears itself once the incumbent is gone or the node has been
        switched to enforcement.
        """
        block = (flight.metadata or {}).get("advisory_preempt")
        if not block:
            return False
        node = self.nodes.get(block.get("node")) if block.get("node") else None
        target = self.flights.get(block.get("target"))
        if node is not None and node.spec.enforcement == "enforce":
            flight.metadata.pop("advisory_preempt", None)
            return False
        if target is None or target.status not in (FlightStatus.IN_FLIGHT,
                                                   FlightStatus.DRAINING):
            flight.metadata.pop("advisory_preempt", None)
            return False
        return True

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

    def flight_by_idempotency_key(self, key: str) -> Optional[Flight]:
        """Find a flight that was already answered for this idempotency key.

        The in-memory cache does not survive a restart, so a retried request
        after a restart would schedule the work a second time. The key is
        stored on the flight, which is persisted, so the answer does.
        """
        if not key:
            return None
        for flight in self.flights.values():
            if (flight.metadata or {}).get("idempotency_key") == key:
                return flight
        return None

    async def persist_flight(self, flight: Flight):
        """Write a flight to the store."""
        if self.storage is None:
            return
        await self._store(self.storage.upsert_flight, flight)

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

    async def request_clearance(self, req: ClearanceRequest,
                                announce: bool = True) -> ClearanceResponse:
        """Clear a flight for takeoff, or explain why it cannot fly.

        The decision is pure (`policy.decide`); everything below it is the
        effect: state changes, events, persistence, audit. `announce=False`
        is used by the queue dispatcher: a retry that reaches the same answer
        is not a new decision and must not republish it.
        """
        async with self._lock:
            flight = req.flight
            self.flights[flight.id] = flight
            if announce:
                self._emit(EventKind.CLEARANCE_REQUEST, flight_id=flight.id,
                           message=f"Clearance requested: {flight.callsign} ({flight.task[:60]})",
                           severity="info", data={"weight": flight.weight.model_dump()})

            # --- PURE: decide from a frozen snapshot ---
            snap = self.snapshot()
            decision = decide(flight, snap)
            self._last_decision = decision

            # --- IMPURE: act on the decision ---
            if decision.decision == ClearanceDecision.SHOT_DOWN:
                return await self._act_shot_down(flight, decision, "rogue flight declared")

            if decision.preempt_flight_id:
                displaced = await self._act_preempt(flight, decision)
                if not displaced:
                    # Advisory node: the incumbent still holds the capacity the
                    # decision assumed. Clearing this flight would overcommit
                    # the node, so it waits instead of flying.
                    return await self._act_queued(
                        flight, decision, snap,
                        why=(f"advisory: recommended preempting "
                             f"{decision.preempt_flight_id}, capacity not freed"),
                        announce=announce)

            if decision.decision == ClearanceDecision.QUEUED:
                return await self._act_queued(flight, decision, snap, announce=announce)

            best_node = self.nodes.get(decision.assigned_node) if decision.assigned_node else None
            if best_node is not None and self.anomaly.is_rogue_flight(best_node):
                return await self._act_shot_down(
                    flight, decision, "anomaly radar: node in runaway state",
                    node_id=best_node.spec.id)

            ladder_response, ladder_rung = await self._act_safety_ladder(flight, decision, best_node)
            if ladder_response is not None:
                return ladder_response  # a drain returned a response of its own

            await self._act_takeoff(flight, decision, best_node)
            await self.log_audit_async({
                "kind": "clearance",
                "flight_id": flight.id,
                "callsign": flight.callsign,
                "decision": decision.decision.value,
                "assigned_node": decision.assigned_node,
                "reason": decision.reason,
                "ladder": ladder_rung.value,
                "admission": decision.to_dict(),
                # the flight and the mesh state this decision was made
                # against, so the decision can be replayed later
                "flight": self.flight_record(flight),
                "snapshot": snapshot_to_dict(snap),
            })
            node_name = best_node.spec.name if best_node else decision.assigned_node
            self._emit(EventKind.CLEARANCE_GRANTED, node_id=decision.assigned_node,
                       flight_id=flight.id,
                       message=f"CLEARED FOR TAKEOFF: {flight.callsign} -> {node_name}",
                       severity="info")
            self._emit(EventKind.TAKEOFF, node_id=decision.assigned_node, flight_id=flight.id,
                       message=f"{flight.callsign} airborne on {node_name}",
                       severity="info")
            return ClearanceResponse(decision=ClearanceDecision.CLEARED, flight_id=flight.id,
                                    assigned_node=decision.assigned_node,
                                    reason=decision.reason,
                                    admission=decision.to_dict())

    # ---- the effects of a decision ----

    async def _act_shot_down(self, flight: Flight, decision: Decision, why: str,
                             node_id: Optional[str] = None) -> ClearanceResponse:
        """Kill a flight before it flies (or while it does)."""
        flight.status = FlightStatus.SHOT_DOWN
        self._queued_ids.discard(flight.id)
        flight.shot_down_at = datetime.now(timezone.utc)
        flight.reason = why if why != "rogue flight declared" else decision.reason
        if self.storage is not None:
            await self._store(self.storage.upsert_flight, flight)
        self._emit(EventKind.SHOT_DOWN, node_id=node_id, flight_id=flight.id,
                   message=f"SHOT DOWN: {flight.callsign} - {flight.reason}",
                   severity="critical")
        await self.log_audit_async({
            "kind": "shot_down", "flight_id": flight.id, "callsign": flight.callsign,
            "node_id": node_id, "reason": flight.reason,
        })
        return ClearanceResponse(decision=ClearanceDecision.SHOT_DOWN, flight_id=flight.id,
                                assigned_node=node_id or decision.assigned_node,
                                reason=flight.reason, admission=decision.to_dict())

    async def _act_preempt(self, flight: Flight, decision: Decision) -> bool:
        """Preempt the incumbent, or recommend it if the node is advisory.

        Returns True when the incumbent was actually displaced. When it was
        not, the caller must not clear the new flight: the capacity the
        decision assumed is still held.
        """
        existing = self.flights.get(decision.preempt_flight_id)
        target_node = self.nodes.get(existing.assigned_node) if existing else None
        enforcing = (target_node.spec.enforcement == "enforce") if target_node else True
        if enforcing:
            await self._preempt(decision.preempt_flight_id,
                                f"preempted by {flight.callsign} (higher priority)")
            return True
        # advisory mode: the Tower recommends, and records, but does not act
        flight.metadata["advisory_preempt"] = {
            "target": decision.preempt_flight_id,
            "node": target_node.spec.id if target_node else None,
        }
        self._emit(EventKind.ANOMALY,
                   node_id=target_node.spec.id if target_node else None,
                   flight_id=flight.id,
                   message=(f"ADVISORY: would preempt {existing.callsign if existing else decision.preempt_flight_id} "
                            f"for {flight.callsign} - node is advisory-only"),
                   severity="warn",
                   data={"recommendation": "preempt", "target": decision.preempt_flight_id})
        await self.log_audit_async({
            "kind": "preempt_recommendation", "flight_id": flight.id,
            "target": decision.preempt_flight_id, "enforcement": "advisory",
        })
        return False

    async def _act_queued(self, flight: Flight, decision: Decision,
                          snapshot: Optional[MeshSnapshot] = None,
                          why: Optional[str] = None,
                          announce: bool = True) -> ClearanceResponse:
        """Put a flight on the waiting list."""
        reason = why or decision.reason
        unchanged = (flight.status == FlightStatus.QUEUED and flight.reason == reason)
        flight.status = FlightStatus.QUEUED
        self._queued_ids.add(flight.id)
        flight.assigned_node = decision.assigned_node
        flight.reason = reason
        if decision.assigned_node and decision.assigned_node in self.nodes:
            node = self.nodes[decision.assigned_node]
            if flight.id not in node.queued_flights:
                node.queued_flights.append(flight.id)
        if self.storage is not None:
            await self._store(self.storage.upsert_flight, flight)
        if announce or not unchanged:
            self._emit(EventKind.CLEARANCE_DENIED, flight_id=flight.id,
                       message=f"Denied/queued: {flight.callsign} - {reason}",
                       severity="warn")
            await self.log_audit_async({
                "kind": "clearance", "flight_id": flight.id, "callsign": flight.callsign,
                "decision": ClearanceDecision.QUEUED.value, "reason": reason,
                "admission": decision.to_dict(),
                # a queued decision is a decision too, and it must be replayable
                "flight": self.flight_record(flight),
                "snapshot": snapshot_to_dict(snapshot) if snapshot else None,
            })
        return ClearanceResponse(decision=ClearanceDecision.QUEUED, flight_id=flight.id,
                                assigned_node=decision.assigned_node,
                                reason=reason,
                                estimated_wait_s=decision.estimated_wait_s,
                                admission=decision.to_dict())

    async def _act_safety_ladder(self, flight: Flight, decision: Decision,
                                 node: Optional[Node]):
        """Apply the enforcement ladder.

        Returns `(response, rung)`. A non-None response means the flight
        must not fly; the rung says how far up the ladder the node is.
        """
        if node is None:
            return None, Ladder.ADMIT
        view = next((n for n in self.snapshot().nodes if n.id == node.spec.id), None)
        if view is None:
            return None, Ladder.ADMIT
        predicted = self.anomaly.trend_summary(node).get("secs_to_redline")
        assessment = assess(view, flight, predicted)
        self._node_ladder[node.spec.id] = assessment

        if assessment.ladder in (Ladder.WARN, Ladder.THROTTLE, Ladder.DRAIN):
            self._emit(EventKind.ANOMALY, node_id=node.spec.id, flight_id=flight.id,
                       message=f"{assessment.ladder.value.upper()}: {assessment.reason}",
                       severity=assessment.severity, data=assessment.to_dict())
        await self.log_audit_async({
            "kind": "ladder", "flight_id": flight.id, "node_id": node.spec.id,
            "ladder": assessment.ladder.value, "reason": assessment.reason,
        })

        if assessment.ladder == Ladder.DRAIN:
            if node.spec.enforcement == "enforce":
                # the node is heading for redline: it takes no new work
                flight.status = FlightStatus.QUEUED
                self._queued_ids.add(flight.id)
                flight.takeoff_at = None
                flight.cleared_at = None
                flight.reason = f"node draining: {assessment.reason}"
                if self.storage is not None:
                    await self._store(self.storage.upsert_flight, flight)
                return (ClearanceResponse(decision=ClearanceDecision.QUEUED,
                                          flight_id=flight.id,
                                          assigned_node=decision.assigned_node,
                                          reason=flight.reason,
                                          estimated_wait_s=60.0,
                                          admission=decision.to_dict()),
                        assessment.ladder)
            # advisory mode: recommend draining, but do not block the mesh
            self._emit(EventKind.ANOMALY, node_id=node.spec.id, flight_id=flight.id,
                       message=f"ADVISORY: recommend draining {node.spec.name} - {assessment.reason}",
                       severity="warn", data=assessment.to_dict())

        if assessment.ladder == Ladder.THROTTLE:
            factor = THROTTLE_FACTOR.get(Ladder.THROTTLE, 0.5)
            throttled = throttled_weight(flight, factor)
            flight.weight = throttled.weight
            flight.metadata = {**flight.metadata, "throttled": True,
                               "throttle_factor": factor}
        return None, assessment.ladder

    async def _act_takeoff(self, flight: Flight, decision: Decision,
                           node: Optional[Node]):
        """Let the flight fly."""
        flight.status = FlightStatus.CLEARED
        self._queued_ids.discard(flight.id)
        flight.cleared_at = datetime.now(timezone.utc)
        flight.assigned_node = decision.assigned_node
        flight.takeoff_at = datetime.now(timezone.utc)
        flight.status = FlightStatus.IN_FLIGHT
        if node is not None:
            node.active_flights.append(flight.id)
            node.active_weights[flight.id] = flight.weight
            if flight.id in node.queued_flights:
                node.queued_flights = [f for f in node.queued_flights if f != flight.id]
        # the flight holds a time-bounded claim on the node's resources
        lease = self.issue_lease(flight, decision.assigned_node)
        if lease is not None:
            self._emit(EventKind.LEASE, node_id=decision.assigned_node,
                       flight_id=flight.id,
                       message=f"Lease {lease.id} issued for {flight.callsign}",
                       severity="info", data={"lease_id": lease.id})
        self._index_scope(flight)
        if self.storage is not None:
            await self._store(self.storage.upsert_flight, flight)

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
            # A kill is destructive. It requires a reason.
            if not req.reason or not req.reason.strip():
                self._emit(EventKind.KILL, container_id=req.container_id,
                           message=f"KILL refused: {req.container_id} - no reason given",
                           severity="warn")
                return KillResponse(success=False, container_id=req.container_id,
                                    message="Refused: a kill requires a reason")

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
            # Drain before kill: stop admitting to the node so the kill is
            # the last thing that happens, not the first.
            if req.drain_first and node_id and node_id in self.nodes:
                node = self.nodes[node_id]
                if node.status == NodeStatus.ONLINE and node.active_flights:
                    node.status = NodeStatus.DRAINING
                    self._emit(EventKind.ANOMALY, node_id=node_id,
                               message=(f"Draining {node.spec.name} before kill "
                                        f"({len(node.active_flights)} flight(s) in the air)"),
                               severity="warn",
                               data={"active_flights": list(node.active_flights)})
                    await self.log_audit_async({"kind": "drain", "node_id": node_id,
                                                "active_flights": list(node.active_flights),
                                                "reason": req.reason})

            # A drain timeout gives the flight itself a chance to land before
            # it is terminated. Without one, the kill is immediate.
            draining = False
            if (flight and req.drain_timeout_s > 0
                    and flight.status in (FlightStatus.IN_FLIGHT, FlightStatus.CLEARED)):
                flight.status = FlightStatus.DRAINING
                flight.drain_until = datetime.now(timezone.utc) + timedelta(seconds=req.drain_timeout_s)
                flight.reason = f"draining for {req.drain_timeout_s:.0f}s: {req.reason}"
                draining = True
                if self.storage is not None:
                    await self._store(self.storage.upsert_flight, flight)
                self._emit(EventKind.ANOMALY, node_id=node_id, flight_id=flight.id,
                           message=(f"Draining {flight.callsign} for {req.drain_timeout_s:.0f}s "
                                    f"before it is terminated"),
                           severity="warn",
                           data={"drain_until": flight.drain_until.isoformat(),
                                 "reason": req.reason})
                await self.log_audit_async({"kind": "drain_flight", "flight_id": flight.id,
                                            "node_id": node_id, "timeout_s": req.drain_timeout_s,
                                            "reason": req.reason})
            elif flight:
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
                    self._release_lease_for(flight.id)
                if flight and self.storage is not None:
                    await self._store(self.storage.upsert_flight, flight)
            # Actual process termination. Only a PID the Tower was explicitly
            # given ownership of is ever signalled: a container id that merely
            # looks like a PID is not a licence to kill that process.
            killed_proc = False
            pid = self._owned_pid_for(req.container_id)
            if pid is not None:
                try:
                    import os, signal
                    os.kill(pid, signal.SIGKILL)
                    killed_proc = True
                except Exception:
                    killed_proc = False
            info = self.containers.get(req.container_id)
            if info is not None:
                info["status"] = "draining" if draining else "killed"
                if self.storage is not None:
                    await self._store(self.storage.upsert_container, info)
            self._killed_containers[req.container_id] = flight_id or ""
            if draining:
                message = (f"Draining {req.container_id} for {req.drain_timeout_s:.0f}s "
                           f"({req.reason})")
            elif killed_proc:
                message = f"KILL executed: {req.container_id} ({req.reason}) - process terminated"
            else:
                message = (f"KILL executed: {req.container_id} ({req.reason}) - "
                           f"no owned process to terminate")
            self._emit(EventKind.KILL, node_id=node_id, flight_id=flight_id,
                       container_id=req.container_id,
                       message=message,
                       severity="warn" if draining else "critical",
                       data={"draining": draining, "process_terminated": killed_proc,
                             "pid": pid, "pid_owned": pid is not None})
            await self.log_audit_async({"kind": "kill", "container_id": req.container_id,
                                        "flight_id": flight_id, "node_id": node_id,
                                        "reason": req.reason, "requester": req.requester,
                                        "draining": draining,
                                        "process_terminated": killed_proc})
            response = KillResponse(
                success=True,
                container_id=req.container_id,
                flight_id=flight_id,
                node_id=node_id,
                message=message,
                process_terminated=killed_proc,
                draining=draining,
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

    # ---------------- leases ----------------
    def issue_lease(self, flight: Flight, node_id: str, ttl_s: float = 300.0) -> Optional[Lease]:
        """Claim a node's resources for a flight, for a bounded time.

        The lease is the Tower's claim. If the worker never comes back, the
        lease expires and the resources are released by the sweep — nothing
        is stranded.
        """
        if not flight.assigned_node or flight.assigned_node != node_id:
            return None
        lease = Lease(
            flight_id=flight.id,
            node_id=node_id,
            cpu_cores=flight.weight.cpu_cores,
            memory_gb=flight.weight.memory_gb,
            gpu_units=flight.weight.gpu_units,
            ttl_s=ttl_s,
        )
        self.leases[lease.id] = lease
        flight.metadata = {**flight.metadata, "lease_id": lease.id}
        return lease

    def renew_lease(self, lease_id: str, ttl_s: Optional[float] = None) -> Optional[Lease]:
        lease = self.leases.get(lease_id)
        # Only an active lease can be renewed. Reviving an expired or released
        # one would claim resources the sweep has already handed back.
        if lease is None or lease.status != LeaseStatus.ACTIVE:
            return None
        lease.renew(ttl_s)
        return lease

    def release_lease(self, lease_id: str, status: LeaseStatus = LeaseStatus.RELEASED) -> bool:
        """Give a lease back.

        Refused while the flight still holds the node: releasing the claim
        without stopping the work would let the node be oversubscribed.
        """
        lease = self.leases.get(lease_id)
        if lease is None or lease.status != LeaseStatus.ACTIVE:
            return False
        flight = self.flights.get(lease.flight_id)
        if flight is not None and flight.status in (FlightStatus.CLEARED,
                                                    FlightStatus.IN_FLIGHT,
                                                    FlightStatus.DRAINING):
            return False
        lease.status = status
        return True

    def _release_lease_for(self, flight_id: str):
        for lease in self.leases.values():
            if lease.flight_id == flight_id and lease.status == LeaseStatus.ACTIVE:
                lease.status = LeaseStatus.RELEASED
                return lease
        return None

    def expired_leases(self) -> List[Lease]:
        now = datetime.now(timezone.utc)
        return [l for l in self.leases.values()
                if l.status == LeaseStatus.ACTIVE and l.is_expired(now)]

    def reap_expired_leases(self) -> List[Lease]:
        """Release the resources of expired leases and reschedule their flights.

        This is what stops a dead agent from holding a node hostage.
        """
        reaped = []
        for lease in self.expired_leases():
            lease.status = LeaseStatus.EXPIRED
            node = self.nodes.get(lease.node_id)
            if node is not None:
                node.active_flights = [f for f in node.active_flights if f != lease.flight_id]
                node.active_weights.pop(lease.flight_id, None)
            flight = self.flights.get(lease.flight_id)
            if flight is not None and flight.status == FlightStatus.IN_FLIGHT:
                if flight.retries < flight.max_retries:
                    flight.retries += 1
                    flight.status = FlightStatus.QUEUED
                    self._queued_ids.add(flight.id)
                    flight.assigned_node = None
                    flight.takeoff_at = None
                    flight.cleared_at = None
                    flight.reason = f"lease expired, rescheduling (retry {flight.retries}/{flight.max_retries})"
                else:
                    flight.status = FlightStatus.SHOT_DOWN
                    flight.shot_down_at = datetime.now(timezone.utc)
                    flight.reason = "lease expired (max retries)"
                self._deindex_scope(flight)
            reaped.append(lease)
            self._emit(EventKind.LEASE, node_id=lease.node_id, flight_id=lease.flight_id,
                       message=f"Lease {lease.id} expired - resources released",
                       severity="warn",
                       data={"lease_id": lease.id, "flight_id": lease.flight_id})
        if reaped:
            self.log_audit({"kind": "lease_expired",
                            "leases": [l.id for l in reaped]})
        return reaped

    # ---------------- node lifecycle ----------------
    def set_node_state(self, node_id: str, state: str) -> bool:
        """Move a node through its lifecycle.

        `online -> draining -> cordoned -> maintenance -> offline -> unknown`

        Draining stops new flights but lets in-flight work land, so
        "shut this node down for a firmware update" is a safe operation
        rather than a kill-everything event.
        """
        node = self.nodes.get(node_id)
        if node is None:
            return False
        try:
            new_status = NodeStatus(state)
        except ValueError:
            return False
        previous = node.status
        node.status = new_status
        if self.storage is not None:
            try:
                self.storage.upsert_node_spec(node_id, node.spec.model_dump_json())
            except Exception as e:
                logger.warning("node spec persist failed: %s", e)
        self._emit(EventKind.ANOMALY, node_id=node_id,
                   message=f"Node {node.spec.name}: {previous.value} -> {new_status.value}",
                   severity="warn" if new_status != NodeStatus.ONLINE else "info",
                   data={"previous": previous.value, "state": new_status.value})
        self.log_audit({"kind": "node_state", "node_id": node_id,
                        "previous": previous.value, "state": new_status.value})
        return True

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
        node = self.nodes[node_id]
        # Every claim the node was holding has to go with it, or the lease and
        # queue panels keep advertising capacity that no longer exists.
        for fid in list(node.active_flights):
            f = self.flights.get(fid)
            if f is None:
                continue
            f.status = FlightStatus.SHOT_DOWN
            f.shot_down_at = datetime.now(timezone.utc)
            f.reason = "node unregistered"
            self._deindex_scope(f)
            self._queued_ids.discard(fid)
            if self.storage is not None:
                self.storage.upsert_flight(f)  # sync path (unregister_node is sync)
        for fid in list(node.queued_flights):
            f = self.flights.get(fid)
            self._queued_ids.discard(fid)
            if f is not None and f.status == FlightStatus.QUEUED:
                f.status = FlightStatus.SHOT_DOWN
                f.shot_down_at = datetime.now(timezone.utc)
                f.reason = "node unregistered"
                if self.storage is not None:
                    self.storage.upsert_flight(f)
        for lease in self.leases.values():
            if lease.node_id == node_id and lease.status == LeaseStatus.ACTIVE:
                lease.status = LeaseStatus.RELEASED
        node.active_flights.clear()
        node.queued_flights.clear()
        node.active_weights.clear()
        del self.nodes[node_id]
        self.radar.nodes.pop(node_id, None)
        self._node_ladder.pop(node_id, None)
        if self.storage is not None:
            try:
                self.storage.delete_node_spec(node_id)
            except Exception as e:
                logger.warning("node spec delete failed: %s", e)
        self._emit(EventKind.NODE_OFFLINE, node_id=node_id,
                   message=f"Node unregistered: {node_id}", severity="warn")
        self.log_audit({"kind": "node_unregistered", "node_id": node_id})
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
            # "online" means reachable. A node with stale telemetry is
            # UNKNOWN, and counting it as online would hide a dead machine
            # behind a healthy-looking 3/3.
            "nodes_online": sum(1 for n in self.nodes.values()
                                if n.status in _REACHABLE_STATUSES),
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
                           node_id: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
                           pid: Optional[int] = None):
        meta = dict(metadata or {})
        if pid is not None:
            meta["pid"] = int(pid)
        self.containers[container_id] = {
            "container_id": container_id,
            "flight_id": flight_id,
            "node_id": node_id,
            "metadata": meta,
            "last_heartbeat": datetime.now(timezone.utc),
            "status": "running",
        }
        # Registering a pid is the Tower taking ownership of that process.
        # Nothing else on the host is ever signalled.
        self._claim_pid(meta.get("pid"), container_id)
        if self.storage is not None:
            try:
                self.storage.upsert_container(self.containers[container_id])
            except Exception as e:
                logger.warning("container persist failed: %s", e)
        return self.containers[container_id]

    # ---------------- process ownership ----------------
    def _claim_pid(self, pid: Any, container_id: str) -> Optional[int]:
        """Record that the Tower owns a process and may stop it."""
        if pid is None:
            return None
        try:
            value = int(pid)
        except (TypeError, ValueError):
            return None
        if value <= 1:
            # PID 0 and 1 are never a flight. Refuse to claim them.
            logger.warning("refusing to claim pid %s for %s", value, container_id)
            return None
        self._owned_pids[value] = container_id
        return value

    def _owned_pid_for(self, container_id: str) -> Optional[int]:
        """The PID of a process the Tower owns, or None.

        A container id that merely looks like a PID is never a licence to
        signal that process: only an explicitly registered pid is.
        """
        for pid, owner in self._owned_pids.items():
            if owner == container_id:
                return pid
        return None

    def owned_pids(self) -> Dict[str, int]:
        """Which container owns which process. Exposed for the operator."""
        return {owner: pid for pid, owner in self._owned_pids.items()}

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

    # ---------------- replayable decisions ----------------
    @staticmethod
    def flight_record(flight: Flight) -> Dict[str, Any]:
        """The fields a decision depends on, so it can be re-derived later.

        A replay is only meaningful if the input is the input. Without this,
        a caller has to reconstruct the flight by hand and any difference
        looks like policy drift.
        """
        return {
            "id": flight.id,
            "task": flight.task,
            "callsign": flight.callsign,
            "requester": flight.requester,
            "priority": int(flight.priority),
            "weight": flight.weight.model_dump(),
            "scope": list(flight.scope),
            "preferred_node": flight.preferred_node,
            "container_id": flight.container_id,
            "rogue": flight.rogue,
        }

    def flight_from_record(self, record: Dict[str, Any]) -> Flight:
        """Rebuild the flight a recorded decision was made about."""
        return Flight(
            id=record.get("id") or Flight(task="replay").id,
            task=record.get("task", "replay"),
            callsign=record.get("callsign", ""),
            requester=record.get("requester", "replay"),
            priority=int(record.get("priority", 2)),
            weight=record.get("weight") or {},
            scope=list(record.get("scope") or []),
            preferred_node=record.get("preferred_node"),
            container_id=record.get("container_id"),
            rogue=bool(record.get("rogue", False)),
        )

    async def recorded_decision(self, flight_id: str) -> Optional[Dict[str, Any]]:
        """The last recorded clearance decision for a flight."""
        if self.storage is None:
            return None
        rows = await self._store(self.storage.load_decisions, 500) or []
        for row in rows:
            if row.get("flight_id") == flight_id:
                return row
        return None

    async def replay_flight(self, flight_id: str) -> Optional[Dict[str, Any]]:
        """Re-derive a recorded decision from the flight and the mesh state
        it was made against."""
        recorded = await self.recorded_decision(flight_id)
        if not recorded:
            return None
        snapshot = recorded.get("snapshot")
        flight_record = recorded.get("flight")
        if not snapshot or not flight_record:
            return None
        flight = self.flight_from_record(flight_record)
        result = replay(flight, snapshot)
        payload = {"flight_id": flight_id, "replayed": result.to_dict()}
        admission = recorded.get("admission")
        if admission:
            payload["comparison"] = compare(admission, result)
        return payload

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
