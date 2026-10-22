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
            return JSONResponse({"status": "ok", "service": "the-tower"})

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
            if "flight" not in body:
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
            return JSONResponse({"valid": tower.verify_audit()})

        async def audit_log(request):
            body = await request.json()
            aid = tower.log_audit(body)
            return JSONResponse({"audit_id": aid})

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
            return JSONResponse({"ok": True, "container": info})

        async def heartbeat(request):
            cid = request.path_params["container_id"]
            tower.heartbeat(cid)
            return JSONResponse({"ok": True})

        async def list_containers(request):
            return JSONResponse({"containers": list(tower.containers.values())})

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
            Route("/trend/{node_id}", trend),
            Route("/ingest/{node_id}", ingest, methods=["POST"]),
            Route("/nodes", register_node, methods=["POST"]),
            Route("/nodes/{node_id}", unregister_node, methods=["DELETE"]),
            Route("/audit/verify", audit_verify),
            Route("/audit", audit_log, methods=["POST"]),
            Route("/conflicts", conflicts),
            Route("/containers", register_container, methods=["POST"]),
            Route("/containers", list_containers, methods=["GET"]),
            Route("/containers/{container_id}/heartbeat", heartbeat, methods=["POST"]),
            WebSocketRoute("/ws", ws_handler),
        ]

        app = Starlette(routes=routes)

        token = os.environ.get("TOWER_TOKEN")

        class AuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
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
