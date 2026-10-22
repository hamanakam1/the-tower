"""CLI entry point for The Tower."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .core import Tower
from .server import TowerServer


def build_parser():
    p = argparse.ArgumentParser(prog="the-tower", description="The Tower - Hardware ATC")
    sub = p.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the Tower API + dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--interval", type=float, default=5.0, help="Radar sweep interval seconds")
    serve.add_argument("--db", default=None, help="SQLite db path")
    serve.add_argument("--config", default=None, help="YAML config file")

    radar = sub.add_parser("radar", help="Print one radar sweep and exit")
    status = sub.add_parser("status", help="Print node status")
    status.add_argument("node", nargs="?")

    demo = sub.add_parser("demo", help="Run a demo: submit sample flights")
    demo.add_argument("--port", type=int, default=8765)

    return p


async def cmd_serve(args):
    import yaml
    db_path = args.db
    interval = args.interval
    nodes = None
    if args.config and Path(args.config).exists():
        with open(args.config) as f:
            cfg = yaml.safe_load(f) or {}
        db_path = cfg.get("db_path", db_path)
        interval = cfg.get("radar_interval", interval)
        if cfg.get("nodes"):
            from .schemas import NodeSpec
            nodes = [NodeSpec(**n) for n in cfg["nodes"]]
    if db_path is None:
        db_path = str(Path.home() / ".the-tower" / "tower.sqlite")
    tower = Tower(nodes=nodes, radar_interval_s=interval, db_path=db_path)
    await tower.start()
    srv = TowerServer(tower)
    # run uvicorn in same loop
    import uvicorn
    config = uvicorn.Config(srv.app, host=args.host, port=args.port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()
    await tower.stop()


async def cmd_radar(args):
    tower = Tower()
    await tower.start()
    await tower.radar.sweep_once()
    for n in tower.radar_snapshot():
        print(f"{n.spec.id:12} {n.spec.name:28} cpu={n.telemetry.cpu_percent:5.1f}% "
              f"mem={n.telemetry.memory_percent:5.1f}% temp={n.telemetry.temperature_c:5.1f}C "
              f"pwr={n.telemetry.power_watts:6.1f}W status={n.status.value}")
    await tower.stop()


async def cmd_status(args):
    tower = Tower()
    await tower.start()
    await tower.radar.sweep_once()
    if args.node:
        n = tower.get_node(args.node)
        if not n:
            print(f"unknown node: {args.node}", file=sys.stderr)
            return 1
        print(json.dumps({"node": n.model_dump(mode="json"), "trend": tower.trend(args.node)}, indent=2))
    else:
        for n in tower.radar_snapshot():
            print(f"{n.spec.id:12} status={n.status.value:10} temp={n.telemetry.temperature_c:5.1f}C "
                  f"cpu={n.telemetry.cpu_percent:5.1f}% mem={n.telemetry.memory_percent:5.1f}%")
    await tower.stop()


def main():
    args = build_parser().parse_args()
    if args.command == "serve":
        asyncio.run(cmd_serve(args))
    elif args.command == "radar":
        asyncio.run(cmd_radar(args))
    elif args.command == "status":
        asyncio.run(cmd_status(args))
    else:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
