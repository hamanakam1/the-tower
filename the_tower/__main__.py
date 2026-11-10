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
    serve.add_argument("--policy", default=None, help="YAML admission policy file")

    radar = sub.add_parser("radar", help="Print one radar sweep and exit")
    status = sub.add_parser("status", help="Print node status")
    status.add_argument("node", nargs="?")
    status.add_argument("--watch", action="store_true", help="Continuously refresh")

    demo = sub.add_parser("demo", help="Seed demo flights against a running Tower")
    demo.add_argument("--port", type=int, default=8765)
    demo.add_argument("--count", type=int, default=5)

    config_parser = sub.add_parser("config", help="Show the effective configuration")
    config_parser.add_argument("--config", default=None, help="YAML config file")

    policy_parser = sub.add_parser("policy", help="Show or lint the admission policy")
    policy_parser.add_argument("--policy", default=None, help="YAML policy file")
    policy_parser.add_argument("--lint", action="store_true", help="Check the policy and exit")
    policy_parser.add_argument("--print", action="store_true", help="Print the policy as YAML")

    verify_parser = sub.add_parser("verify", help="Check that the Tower is trustworthy")
    verify_parser.add_argument("--db", default=None, help="SQLite db path to check")
    verify_parser.add_argument("--config", default=None, help="YAML config file")
    verify_parser.add_argument("--policy", default=None, help="YAML policy file")
    verify_parser.add_argument("--json", action="store_true", help="Print the report as JSON")

    agent = sub.add_parser("agent", help="Run telemetry agent pushing local metrics to a Tower")
    agent.add_argument("--tower", default="http://127.0.0.1:8765")
    agent.add_argument("--node-id", default=None)
    agent.add_argument("--interval", type=float, default=5.0)
    agent.add_argument("--token", default="")
    agent.add_argument("--sensor", default="", help="temperature backend: psutil, osx-cpu-temp, estimate")
    agent.add_argument("--lease", default="", help="lease id to renew on every sweep")
    agent.add_argument("--lease-ttl", type=float, default=300.0)
    agent.add_argument("--spool", default="",
                       help="path to an on-disk buffer for samples that could not be delivered")
    agent.add_argument("--no-discover-lease", action="store_true",
                       help="do not ask the tower which lease this node holds")
    agent.add_argument("--register", action="store_true",
                       help="Register this node with the tower before reporting, "
                            "describing the machine the agent is running on")
    agent.add_argument("--redline", type=float, default=85.0,
                       help="Redline temperature for this node, in Celsius")
    agent.add_argument("--role", default="Mesh Node", help="What this node is for")
    agent.add_argument("--location", default="local", help="Where this node physically is")
    agent.add_argument("--enforcement", choices=("advisory", "enforce"), default="advisory",
                       help="Whether the tower may act on this node on its own")
    agent.add_argument("--staleness", type=int, default=30,
                       help="Seconds without telemetry before the node is unknown")

    return p


async def cmd_serve(args):
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .config import load_config
    cfg = load_config(args.config)
    cfg.apply_to_env()
    db_path = args.db or cfg.db_path
    interval = args.interval if args.interval != 5.0 else cfg.radar_interval
    nodes = cfg.nodes or None
    from .rules import load_policy
    policy = load_policy(args.policy)
    tower = Tower(nodes=nodes, radar_interval_s=interval, db_path=db_path, policy=policy)
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
    while True:
        await tower.radar.sweep_once()
        if args.node:
            n = tower.get_node(args.node)
            if not n:
                print(f"unknown node: {args.node}", file=sys.stderr)
                await tower.stop()
                return 1
            print(json.dumps({"node": n.model_dump(mode="json"), "trend": tower.trend(args.node)}, indent=2))
        else:
            print("\033[2J\033[H", end="")
            for n in tower.radar_snapshot():
                print(f"{n.spec.id:12} status={n.status.value:10} temp={n.telemetry.temperature_c:5.1f}C "
                      f"cpu={n.telemetry.cpu_percent:5.1f}% mem={n.telemetry.memory_percent:5.1f}%")
        if not args.watch:
            break
        await asyncio.sleep(2)
    await tower.stop()


async def cmd_demo(args):
    import httpx
    base = f"http://127.0.0.1:{args.port}"
    samples = [
        ("train model", 4, 16, 30, ["model-x"], False),
        ("ingest batch", 2, 4, 10, ["ingest"], False),
        ("rogue miner", 8, 32, 120, ["miner"], True),
        ("cleanup job", 1, 2, 5, [], False),
        ("eval suite", 2, 8, 20, ["eval"], False),
    ]
    async with httpx.AsyncClient() as c:
        for i in range(args.count):
            task, cpu, mem, dur, scope, rogue = samples[i % len(samples)]
            body = {"task": f"{task} #{i+1}", "priority": (i % 3),
                    "weight": {"cpu_cores": cpu, "memory_gb": mem, "duration_est_s": dur},
                    "scope": scope, "rogue": rogue}
            r = await c.post(f"{base}/clearance", json=body)
            print(r.status_code, r.json().get("decision"), body["task"])
    print("demo seeded")

def cmd_config(args):
    from .config import load_config
    cfg = load_config(args.config)
    print(json.dumps(cfg.to_dict(), indent=2))
    return 0


def cmd_policy(args):
    from .rules import load_policy, lint, dump_policy, PolicyError
    if args.lint:
        import yaml
        path = args.policy
        if not path:
            print("nothing to lint: no --policy file given", file=sys.stderr)
            return 1
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            print(f"policy file not found: {path}", file=sys.stderr)
            return 1
        problems = lint(data)
        if problems:
            for p in problems:
                print(f"  ✗ {p}", file=sys.stderr)
            return 1
        print(f"policy {path} is valid")
        return 0
    try:
        policy = load_policy(args.policy)
    except PolicyError as e:
        print(f"invalid policy: {e}", file=sys.stderr)
        return 1
    if args.print:
        print(dump_policy(policy))
        return 0
    print(json.dumps(policy.as_dict(), indent=2))
    print(f"\nfingerprint: {policy.fingerprint()}")
    return 0


async def cmd_verify(args):
    from .config import load_config
    from .rules import load_policy
    from .verify import verify
    cfg = load_config(args.config)
    policy = load_policy(args.policy)
    tower = Tower(nodes=cfg.nodes or None, radar_interval_s=cfg.radar_interval,
                  db_path=args.db or cfg.db_path, policy=policy)
    await tower.start()
    try:
        report = verify(tower)
    finally:
        await tower.stop()
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.render())
    return 0 if report.ok else 1


def main():
    args = build_parser().parse_args()
    if args.command == "serve":
        asyncio.run(cmd_serve(args))
    elif args.command == "radar":
        asyncio.run(cmd_radar(args))
    elif args.command == "status":
        asyncio.run(cmd_status(args))
    elif args.command == "demo":
        asyncio.run(cmd_demo(args))
    elif args.command == "config":
        sys.exit(cmd_config(args))
    elif args.command == "policy":
        sys.exit(cmd_policy(args))
    elif args.command == "verify":
        sys.exit(asyncio.run(cmd_verify(args)))
    elif args.command == "agent":
        import platform
        node_id = args.node_id or platform.node()
        from .agent import run
        asyncio.run(run(args.tower, node_id, args.interval, args.token, args.sensor,
                        args.lease, args.lease_ttl, args.register, args.redline,
                        args.role, args.location, args.enforcement, args.staleness,
                        args.spool, not args.no_discover_lease))
    else:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
