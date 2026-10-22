"""Telemetry agent: collects local metrics and pushes to a Tower instance."""
from __future__ import annotations

import argparse
import asyncio
import platform
import time

import httpx
import psutil

from .schemas import Telemetry, ThermalState
from .sensors import read_temperature


def sample(node_id: str, sensor: str = "") -> dict:
    cpu = psutil.cpu_percent(interval=1)
    vm = psutil.virtual_memory()
    reading = read_temperature(sensor or None, cpu)
    temp = reading.celsius if reading.celsius is not None else 45.0 + (cpu / 100.0) * 30.0
    power = 15.0 + (cpu / 100.0) * 60.0
    redline = 95.0
    ratio = temp / redline
    state = "nominal"
    if ratio >= 0.95: state = "critical"
    elif ratio >= 0.85: state = "serious"
    elif ratio >= 0.70: state = "fair"
    t = Telemetry(
        node_id=node_id,
        cpu_percent=round(cpu, 1),
        memory_percent=round(vm.percent, 1),
        memory_used_gb=round(vm.used / (1024**3), 2),
        memory_total_gb=round(vm.total / (1024**3), 2),
        temperature_c=round(temp, 1),
        power_watts=round(power, 1),
        bandwidth_mbps=0.0,
        load_avg=round(float(psutil.getloadavg()[0]) if hasattr(psutil, "getloadavg") else cpu/100*4, 2),
        thermal_state=state,
        source=reading.source,
        source_note=reading.note,
    )
    return t.model_dump(mode="json")


async def run(tower_url: str, node_id: str, interval: float, token: str = "",
              sensor: str = "", lease_id: str = "", lease_ttl: float = 300.0):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient() as c:
        while True:
            try:
                payload = sample(node_id, sensor)
                r = await c.post(f"{tower_url}/ingest/{node_id}", json=payload, headers=headers, timeout=10)
                print(f"[{node_id}] push {r.status_code} ({payload['source']} thermal)")
                # renew the lease that holds this node's resources
                if lease_id:
                    lr = await c.post(f"{tower_url}/leases/{lease_id}/renew",
                                      json={"ttl_s": lease_ttl}, headers=headers, timeout=10)
                    if lr.status_code != 200:
                        print(f"[{node_id}] lease renewal failed: {lr.status_code} {lr.text}")
                        lease_id = ""
            except Exception as e:
                print(f"[{node_id}] error: {e}")
            await asyncio.sleep(interval)


def main():
    p = argparse.ArgumentParser(prog="the-tower-agent")
    p.add_argument("--tower", default="http://127.0.0.1:8765")
    p.add_argument("--node-id", default=platform.node())
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--token", default="")
    p.add_argument("--sensor", default="", help="temperature backend: psutil, osx-cpu-temp, estimate")
    p.add_argument("--lease", default="", help="lease id to renew on every sweep")
    p.add_argument("--lease-ttl", type=float, default=300.0)
    args = p.parse_args()
    asyncio.run(run(args.tower, args.node_id, args.interval, args.token, args.sensor,
                    args.lease, args.lease_ttl))


if __name__ == "__main__":
    main()
