"""HTTP API + dashboard server for The Tower."""
from __future__ import annotations

import asyncio
import hmac
import json
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import JSONResponse, HTMLResponse, FileResponse
from starlette.routing import Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
import os
from starlette.websockets import WebSocket, WebSocketDisconnect

from .core import Tower
from .replay import replay, compare, snapshot_to_dict, snapshot_from_dict
from .schemas import (
    ClearanceDecision, ClearanceRequest, Flight, FlightStatus, KillRequest,
    Telemetry, AirspaceSnapshot,
)

STATIC_DIR = Path(__file__).parent / "static"


_DECISION_FOR_STATUS = {
    FlightStatus.CLEARED: ClearanceDecision.CLEARED,
    FlightStatus.IN_FLIGHT: ClearanceDecision.CLEARED,
    FlightStatus.DRAINING: ClearanceDecision.CLEARED,
    FlightStatus.LANDED: ClearanceDecision.CLEARED,
    FlightStatus.QUEUED: ClearanceDecision.QUEUED,
    FlightStatus.SHOT_DOWN: ClearanceDecision.SHOT_DOWN,
}


def _response_for_existing_flight(flight: Flight, key: str) -> dict:
    """Rebuild the answer for an idempotency key whose stored payload is gone.

    The key is persisted with the flight before the answer is, so a crash in
    between used to let a retry schedule the same work a second time. The
    flight's own state is enough to answer without doing that.
    """
    decision = _DECISION_FOR_STATUS.get(flight.status, ClearanceDecision.QUEUED)
    return {
        "decision": decision.value,
        "flight_id": flight.id,
        "assigned_node": flight.assigned_node,
        "reason": flight.reason or "this request was already accepted",
        "estimated_wait_s": None,
        "admission": None,
        "idempotency_key": key,
    }


def _version() -> str:
    from . import __version__
    return __version__


def _container_json(info):
    out = dict(info)
    hb = out.get("last_heartbeat")
    if hasattr(hb, "isoformat"):
        out["last_heartbeat"] = hb.isoformat()
    return out


def _bearer(headers) -> str:
    """Extract the token from an `Authorization: Bearer <token>` header.

    Strict on purpose: a bare token without the scheme is not a credential,
    and accepting it would silently widen what counts as one.
    """
    scheme, _, value = headers.get("authorization", "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


class TowerServer:
    def __init__(self, tower: Tower):
        self.tower = tower
        self.app = self._build()

    def _build(self) -> Starlette:
        tower = self.tower
        connections = set()

        async def broadcast(obj):
            dead = []
            for ws in list(connections):
                try:
                    await ws.send_json(obj)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                connections.discard(ws)

        async def on_event(ev):
            await broadcast({"type": "event", "event": ev.model_dump(mode="json")})
            snap = tower.airspace()
            await broadcast({"type": "airspace", "airspace": snap.model_dump(mode="json")})

        tower.subscribe_async(on_event)

        async def ws_handler(websocket: WebSocket):
            # A WebSocket carries the same telemetry the REST API does, so it
            # is behind the same token. Checked before accept: an unauthenticated
            # client must not get a socket at all.
            expected = os.environ.get("TOWER_TOKEN")
            if expected:
                supplied = (websocket.query_params.get("token")
                            or _bearer(websocket.headers))
                if not hmac.compare_digest(supplied, expected):
                    await websocket.close(code=1008)
                    return
            await websocket.accept()
            connections.add(websocket)
            try:
                snap = tower.airspace()
                await websocket.send_json({"type": "airspace", "airspace": snap.model_dump(mode="json")})
                while True:
                    msg = await websocket.receive_text()
                    # allow client to request refresh
                    if msg == "ping":
                        await websocket.send_json({"type": "pong"})
            except WebSocketDisconnect:
                pass
            finally:
                connections.discard(websocket)

        async def root(request):
            index = STATIC_DIR / "index.html"
            if index.exists():
                return HTMLResponse(index.read_text())
            return HTMLResponse("<h1>The Tower</h1><p>Dashboard not found.</p>")

        async def health(request):
            import time
            start = getattr(tower, "_started_at", None)
            uptime = (time.time() - start) if start else None
            return JSONResponse({
                "status": "ok",
                "service": "the-tower",
                "version": _version(),
                "nodes": len(tower.nodes),
                "flights": len(tower.flights),
                "uptime_s": uptime,
            })

        async def radar(request):
            nodes = tower.radar_snapshot()
            return JSONResponse({"nodes": [n.model_dump(mode="json") for n in nodes]})

        async def status_node(request):
            node_id = request.path_params["node_id"]
            node = tower.get_node(node_id)
            if not node:
                return JSONResponse({"error": "node not found"}, status_code=404)
            return JSONResponse({
                "node": node.model_dump(mode="json"),
                "trend": tower.trend(node_id),
            })

        async def clearance(request):
            body = await request.json()
            # allow shorthand: if body has "task" instead of full flight, build one
            if "flight" not in body and "work_package" not in body:
                from .schemas import Flight, ResourceWeight, FlightPriority
                constraints = body.get("constraints") or {}
                flight = Flight(
                    task=body.get("task", body.get("true_goal", "unnamed task")),
                    requester=body.get("requester", "api"),
                    priority=FlightPriority(body.get("priority", 2)),
                    weight=ResourceWeight(**body.get("weight", {})),
                    container_id=body.get("container_id"),
                    preferred_node=(body.get("preferred_node")
                                    or constraints.get("preferred_node")),
                    rogue=bool(body.get("rogue", False)),
                    callsign=body.get("callsign", ""),
                    scope=body.get("scope", []),
                )
                req = ClearanceRequest(flight=flight, dag=body.get("dag"), force=bool(body.get("force", False)))
            elif "work_package" in body:
                from .schemas import Flight, ResourceWeight, FlightPriority
                wp = body["work_package"]
                # map DistilledWorkPackage -> Flight
                prio = wp.get("suggested_priority", 2)
                tokens = wp.get("estimated_tokens", 8000)
                dur = max(5.0, tokens / 100.0)
                flight = Flight(
                    task=wp.get("true_goal", wp.get("true_goal_summary", "work package")),
                    requester=body.get("requester", "el-destilador"),
                    priority=FlightPriority(prio if prio in (0,1,2,3) else 2),
                    weight=ResourceWeight(
                        cpu_cores=body.get("weight", {}).get("cpu_cores", 2),
                        memory_gb=body.get("weight", {}).get("memory_gb", 4),
                        duration_est_s=dur,
                    ),
                    container_id=body.get("container_id"),
                    preferred_node=body.get("preferred_node"),
                    scope=wp.get("suggested_scope", []),
                    metadata={"work_package_id": wp.get("id"), "feasibility": wp.get("feasibility_score")},
                )
                req = ClearanceRequest(flight=flight, dag=body.get("dag"), force=bool(body.get("force", False)))
            else:
                req = ClearanceRequest(**body)
            resp = await tower.request_clearance(req)
            return JSONResponse(resp.model_dump(mode="json"))

        async def clearance_v2(request):
            """Clearance contract v2: idempotent, explainable, 202 on accept."""
            body = await request.json()
            key = body.get("idempotency_key")
            if key:
                cached = tower.idempotent_result(key)
                if cached is not None:
                    return JSONResponse(cached, status_code=200)
                # The in-memory cache does not survive a restart. The key is
                # stored on the flight, which is persisted, so a retry after a
                # restart still gets the original answer instead of a second
                # flight.
                existing = tower.flight_by_idempotency_key(key)
                if existing is not None:
                    stored = (existing.metadata or {}).get("idempotency_response")
                    if not stored:
                        # the answer was never persisted (crash between the
                        # decision and the metadata write): reconstruct it
                        # instead of scheduling the work again
                        stored = _response_for_existing_flight(existing, key)
                    tower.remember_idempotent(key, stored)
                    return JSONResponse(stored, status_code=200)
            from .schemas import Flight, ResourceWeight, FlightPriority
            constraints = body.get("constraints") or {}
            flight = Flight(
                task=body.get("task", "unnamed task"),
                requester=body.get("requester", "el-hidratador"),
                priority=FlightPriority(body.get("priority", 2)),
                weight=ResourceWeight(**(body.get("weight") or {})),
                container_id=body.get("container_id"),
                preferred_node=constraints.get("preferred_node"),
                scope=body.get("scope", []),
                metadata={"idempotency_key": key} if key else {},
            )
            resp = await tower.request_clearance(ClearanceRequest(flight=flight))
            payload = resp.model_dump(mode="json")
            if key:
                payload = dict(payload)
                payload["idempotency_key"] = key
                flight.metadata = {**flight.metadata, "idempotency_response": payload}
                tower.remember_idempotent(key, payload)
                await tower.persist_flight(flight)
            status = 202 if resp.decision.value == "cleared" else 200
            return JSONResponse(payload, status_code=status)

        async def last_decision(request):
            d = tower._last_decision
            if d is None:
                return JSONResponse({"decision": None})
            return JSONResponse(d.to_dict())

        async def safety_ladder(request):
            return JSONResponse(tower.ladder())

        async def list_leases(request):
            leases = list(tower.leases.values())
            return JSONResponse({"leases": [l.model_dump(mode="json") for l in leases]})

        async def renew_lease(request):
            lease_id = request.path_params["lease_id"]
            body = {}
            try:
                body = await request.json()
            except Exception:
                body = {}
            lease = tower.renew_lease(lease_id, body.get("ttl_s"))
            if lease is None:
                return JSONResponse({"error": "lease not found"}, status_code=404)
            return JSONResponse({"ok": True, "lease": lease.model_dump(mode="json")})

        async def release_lease(request):
            lease_id = request.path_params["lease_id"]
            ok = tower.release_lease(lease_id)
            if not ok:
                if lease_id in tower.leases:
                    return JSONResponse(
                        {"error": "lease is held by a flight that is still airborne"},
                        status_code=409)
                return JSONResponse({"error": "lease not found"}, status_code=404)
            return JSONResponse({"ok": True})

        async def decisions(request):
            limit = int(request.query_params.get("limit", 50))
            if tower.storage is None:
                return JSONResponse({"decisions": []})
            rows = await tower._store(tower.storage.load_decisions, limit) or []
            # strip the bulky snapshot unless it is asked for
            include_snapshot = request.query_params.get("snapshot") == "1"
            out = []
            for row in rows:
                if not include_snapshot:
                    row = {k: v for k, v in row.items() if k != "snapshot"}
                out.append(row)
            return JSONResponse({"decisions": out})

        async def replay_decision(request):
            body = await request.json()
            from .schemas import Flight
            flight_data = body.get("flight") or {}
            snap = body.get("snapshot")
            original = body.get("admission")
            if snap is None and original and "snapshot" in original:
                snap = original["snapshot"]
            if snap is None:
                return JSONResponse({"error": "snapshot required"}, status_code=400)
            flight = Flight(**flight_data) if flight_data else Flight(task="replay")
            result = replay(flight, snap)
            payload = {"replayed": result.to_dict()}
            if original:
                payload["comparison"] = compare(original, result)
            return JSONResponse(payload)

        async def replay_flight(request):
            """Replay a decision the Tower already made, from its own record."""
            flight_id = request.path_params["flight_id"]
            result = await tower.replay_flight(flight_id)
            if result is None:
                return JSONResponse(
                    {"error": "no replayable decision recorded for this flight"},
                    status_code=404)
            return JSONResponse(result)

        async def kill(request):
            body = {}
            try:
                body = await request.json()
            except Exception:
                body = {}
            container_id = request.path_params["container_id"]
            req = KillRequest(
                container_id=container_id,
                reason=body.get("reason", "manual kill"),
                requester=body.get("requester", "operator"),
                force=bool(body.get("force", False)),
                drain_first=bool(body.get("drain_first", True)),
                drain_timeout_s=float(body.get("drain_timeout_s", 0) or 0),
            )
            resp = await tower.kill(req)
            return JSONResponse(resp.model_dump(mode="json"))

        async def airspace(request):
            snap = tower.airspace()
            return JSONResponse(snap.model_dump(mode="json"))

        async def events(request):
            limit = int(request.query_params.get("limit", 50))
            evs = tower.recent_events(limit)
            return JSONResponse({"events": [e.model_dump(mode="json") for e in evs]})

        async def flights(request):
            return JSONResponse({"flights": [f.model_dump(mode="json") for f in tower.flights.values()]})

        async def ingest(request):
            node_id = request.path_params["node_id"]
            body = await request.json()
            telem = Telemetry(**body)
            ok = tower.ingest_telemetry(node_id, telem)
            if not ok:
                return JSONResponse({"error": "node not found"}, status_code=404)
            return JSONResponse({"ok": True})

        async def trend(request):
            node_id = request.path_params["node_id"]
            return JSONResponse(tower.trend(node_id))

        async def register_node(request):
            body = await request.json()
            from .schemas import NodeSpec
            spec = NodeSpec(**body)
            node = tower.register_node(spec)
            return JSONResponse({"ok": True, "node": node.model_dump(mode="json")})

        async def unregister_node(request):
            node_id = request.path_params["node_id"]
            ok = tower.unregister_node(node_id)
            if not ok:
                return JSONResponse({"error": "not found"}, status_code=404)
            return JSONResponse({"ok": True})

        async def set_enforcement(request):
            node_id = request.path_params["node_id"]
            body = await request.json()
            # Acting on a node is an explicit opt-in: never infer "enforce"
            # from a missing field.
            mode = body.get("mode")
            if mode not in ("advisory", "enforce"):
                return JSONResponse(
                    {"error": "mode must be 'advisory' or 'enforce'"}, status_code=400)
            ok = tower.set_enforcement(node_id, mode)
            if not ok:
                return JSONResponse({"error": "node not found or bad mode"}, status_code=400)
            node = tower.get_node(node_id)
            return JSONResponse({"ok": True, "node": node.model_dump(mode="json")})

        async def set_node_state(request):
            node_id = request.path_params["node_id"]
            body = await request.json()
            state = body.get("state", "online")
            ok = tower.set_node_state(node_id, state)
            if not ok:
                return JSONResponse({"error": "node not found or bad state"}, status_code=400)
            node = tower.get_node(node_id)
            return JSONResponse({"ok": True, "node": node.model_dump(mode="json")})

        async def audit_verify(request):
            return JSONResponse({"valid": await tower.verify_audit_async()})

        async def audit_log(request):
            body = await request.json()
            aid = await tower.log_audit_async(body)
            return JSONResponse({"audit_id": aid})

        async def rollup(request):
            node_id = request.query_params.get("node")
            if tower.storage is None:
                return JSONResponse({"rollups": []})
            rows = await tower._store(tower.storage.load_rollup, node_id, 500) or []
            return JSONResponse({"rollups": rows})

        async def node_history(request):
            node_id = request.path_params["node_id"]
            node = tower.get_node(node_id)
            if not node:
                return JSONResponse({"error": "not found"}, status_code=404)
            # from storage if available, else in-memory history
            hist = node.history
            if tower.storage is not None:
                try:
                    hist = tower.storage.load_telemetry(node_id, limit=120)
                except Exception:
                    pass
            return JSONResponse({"node_id": node_id, "samples": [t.model_dump(mode="json") for t in hist]})

        async def flight_detail(request):
            fid = request.path_params["flight_id"]
            f = tower.flights.get(fid)
            if not f:
                return JSONResponse({"error": "not found"}, status_code=404)
            related = [e for e in tower.events if e.flight_id == fid]
            return JSONResponse({"flight": f.model_dump(mode="json"),
                                 "events": [e.model_dump(mode="json") for e in related]})

        async def conflicts(request):
            scope = [s for s in request.query_params.get("scope", "").split(",") if s]
            conflicting = []
            from the_tower.schemas import FlightStatus
            for fid, f in tower.flights.items():
                if f.status == FlightStatus.IN_FLIGHT and set(f.scope) & set(scope):
                    conflicting.append(fid)
            return JSONResponse({"conflicting_flights": conflicting})

        async def register_container(request):
            body = await request.json()
            cid = body.get("container_id") or body.get("id")
            if not cid:
                return JSONResponse({"error": "container_id required"}, status_code=400)
            # A pid handed over here is a process the Tower takes ownership of
            # and is therefore allowed to stop later.
            pid = body.get("pid")
            if pid is None:
                pid = (body.get("metadata") or {}).get("pid")
            info = tower.register_container(cid, body.get("flight_id"), body.get("node_id"),
                                            body.get("metadata"), pid=pid)
            return JSONResponse({"ok": True, "container": _container_json(info)})

        async def heartbeat(request):
            cid = request.path_params["container_id"]
            tower.heartbeat(cid)
            return JSONResponse({"ok": True})

        async def list_containers(request):
            return JSONResponse({"containers": [_container_json(c) for c in tower.containers.values()]})

        async def metrics(request):
            from .metrics import render
            from starlette.responses import PlainTextResponse
            return PlainTextResponse(render(tower), media_type="text/plain; version=0.0.4")

        routes = [
            Route("/", root),
            Route("/health", health),
            Route("/radar", radar),
            Route("/status/{node_id}", status_node),
            Route("/clearance", clearance, methods=["POST"]),
            Route("/clearance/v2", clearance_v2, methods=["POST"]),
            Route("/decision/last", last_decision),
            Route("/safety/ladder", safety_ladder),
            Route("/leases", list_leases, methods=["GET"]),
            Route("/leases/{lease_id}/renew", renew_lease, methods=["POST"]),
            Route("/leases/{lease_id}", release_lease, methods=["DELETE"]),
            Route("/decisions", decisions),
            Route("/replay", replay_decision, methods=["POST"]),
            Route("/replay/flight/{flight_id}", replay_flight, methods=["GET"]),
            Route("/kill/{container_id}", kill, methods=["POST"]),
            Route("/airspace", airspace),
            Route("/events", events),
            Route("/flights", flights),
            Route("/flights/{flight_id}", flight_detail),
            Route("/trend/{node_id}", trend),
            Route("/history/{node_id}", node_history),
            Route("/rollup", rollup),
            Route("/ingest/{node_id}", ingest, methods=["POST"]),
            Route("/nodes", register_node, methods=["POST"]),
            Route("/nodes/{node_id}", unregister_node, methods=["DELETE"]),
            Route("/nodes/{node_id}/enforcement", set_enforcement, methods=["POST"]),
            Route("/nodes/{node_id}/state", set_node_state, methods=["POST"]),
            Route("/audit/verify", audit_verify),
            Route("/audit", audit_log, methods=["POST"]),
            Route("/conflicts", conflicts),
            Route("/containers", register_container, methods=["POST"]),
            Route("/containers", list_containers, methods=["GET"]),
            Route("/metrics", metrics),
            Route("/containers/{container_id}/heartbeat", heartbeat, methods=["POST"]),
            WebSocketRoute("/ws", ws_handler),
        ]

        app = Starlette(routes=routes)

        token = os.environ.get("TOWER_TOKEN")
        rate_limit = int(os.environ.get("TOWER_RATE_LIMIT", "0"))  # requests per minute per IP, 0=off

        _buckets = {}

        class AuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                if rate_limit > 0:
                    import time
                    ip = request.client.host if request.client else "unknown"
                    now = time.time()
                    b = _buckets.get(ip)
                    if b is None or now - b[0] > 60:
                        # prune expired buckets so the table cannot grow
                        # without bound on a long-lived server
                        if len(_buckets) > 1024:
                            for k in [k for k, v in _buckets.items() if now - v[0] > 60]:
                                _buckets.pop(k, None)
                        _buckets[ip] = [now, 1]
                    else:
                        b[1] += 1
                        if b[1] > rate_limit:
                            return JSONResponse({"error": "rate limited"}, status_code=429)
                if token and request.method in ("POST", "DELETE", "PUT", "PATCH"):
                    if not hmac.compare_digest(_bearer(request.headers), token):
                        return JSONResponse({"error": "unauthorized"}, status_code=401)
                return await call_next(request)

        app.add_middleware(AuthMiddleware)
        # CORS is an allowlist, not a wildcard. A browser on another origin
        # has no business driving the hardware.
        origins = [o.strip() for o in os.environ.get("TOWER_CORS_ORIGINS", "").split(",")
                   if o.strip()]
        app.add_middleware(CORSMiddleware, allow_origins=origins,
                           allow_methods=["GET", "POST", "DELETE"],
                           allow_headers=["Authorization", "Content-Type"])
        return app

    def run(self, host: str = "127.0.0.1", port: int = 8765):
        import uvicorn
        uvicorn.run(self.app, host=host, port=port, log_level="info")


async def create_app(tower: Tower) -> Starlette:
    return TowerServer(tower).app
