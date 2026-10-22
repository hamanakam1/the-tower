"""HTTP API + dashboard server for The Tower."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import JSONResponse, HTMLResponse, FileResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware

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
        ]

        app = Starlette(routes=routes)
        app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
        return app

    def run(self, host: str = "127.0.0.1", port: int = 8765):
        import uvicorn
        uvicorn.run(self.app, host=host, port=port, log_level="info")


async def create_app(tower: Tower) -> Starlette:
    return TowerServer(tower).app
