"""HTTP API + dashboard server for The Tower."""
from __future__ import annotations

import asyncio
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
from .schemas import (
    ClearanceRequest, Flight, KillRequest, Telemetry, AirspaceSnapshot,
)

STATIC_DIR = Path(__file__).parent / "static"


def _version() -> str:
    from . import __version__
    return __version__


def _container_json(info):
    out = dict(info)
    hb = out.get("last_heartbeat")
    if hasattr(hb, "isoformat"):
        out["last_heartbeat"] = hb.isoformat()
    return out


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
                flight = Flight(
                    task=body.get("task", body.get("true_goal", "unnamed task")),
                    requester=body.get("requester", "api"),
                    priority=FlightPriority(body.get("priority", 2)),
                    weight=ResourceWeight(**body.get("weight", {})),
                    container_id=body.get("container_id"),
                    preferred_node=body.get("preferred_node"),
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

        async def audit_verify(request):
            return JSONResponse({"valid": await tower.verify_audit_async()})

        async def audit_log(request):
            body = await request.json()
            aid = await tower.log_audit_async(body)
            return JSONResponse({"audit_id": aid})

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
            info = tower.register_container(cid, body.get("flight_id"), body.get("node_id"), body.get("metadata"))
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
            Route("/kill/{container_id}", kill, methods=["POST"]),
            Route("/airspace", airspace),
            Route("/events", events),
            Route("/flights", flights),
            Route("/flights/{flight_id}", flight_detail),
            Route("/trend/{node_id}", trend),
            Route("/history/{node_id}", node_history),
            Route("/ingest/{node_id}", ingest, methods=["POST"]),
            Route("/nodes", register_node, methods=["POST"]),
            Route("/nodes/{node_id}", unregister_node, methods=["DELETE"]),
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
                    auth = request.headers.get("authorization", "")
                    if auth != f"Bearer {token}":
                        return JSONResponse({"error": "unauthorized"}, status_code=401)
                return await call_next(request)

        app.add_middleware(AuthMiddleware)
        app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
        return app

    def run(self, host: str = "127.0.0.1", port: int = 8765):
        import uvicorn
        uvicorn.run(self.app, host=host, port=port, log_level="info")


async def create_app(tower: Tower) -> Starlette:
    return TowerServer(tower).app
