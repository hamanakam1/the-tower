"""Telemetry agent: collects local metrics and pushes to a Tower instance."""
from __future__ import annotations

import argparse
import asyncio
import platform
import signal
from typing import Optional

import httpx
import psutil

from .schemas import Telemetry, ThermalState
from .sensors import read_temperature
from .spool import Spool


def sample(node_id: str, sensor: str = "") -> dict:
    cpu = psutil.cpu_percent(interval=1)
    vm = psutil.virtual_memory()
    reading = read_temperature(sensor or None, cpu)
    temp = reading.celsius if reading.celsius is not None else 45.0 + (cpu / 100.0) * 30.0
    power = 15.0 + (cpu / 100.0) * 60.0
    redline = 95.0
    ratio = temp / redline
    state = "nominal"
    if ratio >= 0.95:
        state = "critical"
    elif ratio >= 0.85:
        state = "serious"
    elif ratio >= 0.70:
        state = "fair"
    t = Telemetry(
        node_id=node_id,
        cpu_percent=round(cpu, 1),
        memory_percent=round(vm.percent, 1),
        memory_used_gb=round(vm.used / (1024**3), 2),
        memory_total_gb=round(vm.total / (1024**3), 2),
        temperature_c=round(temp, 1),
        power_watts=round(power, 1),
        bandwidth_mbps=0.0,
        load_avg=round(float(psutil.getloadavg()[0]) if hasattr(psutil, "getloadavg") else cpu / 100 * 4, 2),
        thermal_state=state,
        source=reading.source,
        source_note=reading.note,
    )
    return t.model_dump(mode="json")


def node_spec(node_id: str, redline: float = 85.0, role: str = "Mesh Node",
              location: str = "local", sensor: str = "",
              enforcement: str = "advisory", staleness_s: int = 30) -> dict:
    """Derive a node specification from the machine the agent runs on.

    A node that reports for itself must be described honestly, or the Tower
    will schedule work it cannot actually run.
    """
    vm = psutil.virtual_memory()
    cores = psutil.cpu_count(logical=True) or 1
    try:
        physical = psutil.cpu_count(logical=False) or cores
    except Exception:
        physical = cores
    gib = round(vm.total / (1024 ** 3), 2)
    # A node with no real sensor is a node whose temperature is a guess.
    reading = read_temperature(sensor or None, 10.0)
    measured = reading.celsius is not None
    return {
        "id": node_id,
        "name": node_id,
        "role": role,
        "specs": f"{physical} cores, {gib}GB, {platform.system()} {platform.machine()}",
        "constraints": (f"{redline:.0f}C redline"
                        + ("" if measured else ", temperature is estimated")),
        "cpu_cores": float(cores),
        "memory_gb": gib,
        "gpu_units": 0.0,
        "redline_temp_c": float(redline),
        # a rough envelope; the operator can correct it in the config
        "max_power_watts": max(50.0, float(cores) * 8.0),
        "network_capacity_mbps": 1000.0,
        "thermal_mass": 2.0,
        "location": location,
        "simulate": False,
        "sampler": "agent",
        "sensor": sensor or None,
        "staleness_budget_s": int(staleness_s),
        "enforcement": enforcement,
    }


async def register(c: httpx.AsyncClient, tower_url: str, spec: dict, headers: dict) -> bool:
    try:
        r = await c.post(f"{tower_url}/nodes", json=spec, headers=headers, timeout=10)
    except Exception as e:
        print(f"register error: {e}")
        return False
    if r.status_code == 200:
        print(f"registered node {spec['id']} ({spec['specs']})")
        return True
    print(f"register failed: {r.status_code} {r.text}")
    return False


async def find_lease(c: httpx.AsyncClient, tower_url: str, node_id: str,
                     headers: dict) -> str:
    """Find the lease this node is holding, if any.

    The Tower is the holder of the claim, so the agent asks the Tower which
    lease is its own rather than being told.
    """
    try:
        r = await c.get(f"{tower_url}/leases", headers=headers, timeout=10)
        if r.status_code != 200:
            return ""
        for lease in r.json().get("leases", []):
            if lease.get("node_id") == node_id and lease.get("status") == "active":
                return lease.get("id", "")
    except Exception:
        pass
    return ""


async def run(tower_url: str, node_id: str, interval: float, token: str = "",
              sensor: str = "", lease_id: str = "", lease_ttl: float = 300.0,
              do_register: bool = False, redline: float = 85.0, role: str = "Mesh Node",
              location: str = "local", enforcement: str = "advisory",
              staleness_s: int = 30, spool_path: str = "", discover_lease: bool = True,
              stop_event: Optional[asyncio.Event] = None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    spool = Spool(spool_path) if spool_path else None
    stop_event = stop_event or asyncio.Event()

    async with httpx.AsyncClient() as c:
        if do_register:
            await register(c, tower_url,
                           node_spec(node_id, redline, role, location, sensor,
                                     enforcement, staleness_s),
                           headers)
        if not lease_id and discover_lease:
            lease_id = await find_lease(c, tower_url, node_id, headers)
            if lease_id:
                print(f"[{node_id}] renewing lease {lease_id}")

        async def deliver(payload: dict) -> bool:
            try:
                r = await c.post(f"{tower_url}/ingest/{node_id}", json=payload,
                                 headers=headers, timeout=10)
            except Exception:
                return False
            return r.status_code < 400

        missing = 0
        while not stop_event.is_set():
            # deliver anything the spool is holding, oldest first
            if spool is not None and spool.count():
                delivered = spool.drain(lambda p: True, limit=200)
                if delivered:
                    print(f"[{node_id}] replayed {delivered} spooled sample(s)")

            payload = sample(node_id, sensor)
            try:
                r = await c.post(f"{tower_url}/ingest/{node_id}", json=payload,
                                 headers=headers, timeout=10)
                if r.status_code == 404:
                    missing += 1
                    if missing == 1:
                        print(f"[{node_id}] this node is not registered on the tower - "
                              f"restart the agent with --register")
                    elif missing % 12 == 0:
                        print(f"[{node_id}] still not registered ({missing} attempts)")
                elif r.status_code == 403:
                    print(f"[{node_id}] rejected: {r.text}")
                elif r.status_code == 401:
                    print(f"[{node_id}] unauthorized: check the token")
                else:
                    missing = 0
            except Exception as e:
                # the link is down: keep the sample for when it comes back
                if spool is not None:
                    spool.append(payload)
                    print(f"[{node_id}] link down ({e}); spooled the sample")
                else:
                    print(f"[{node_id}] error: {e}")

            # renew the lease that holds this node's resources
            if lease_id:
                try:
                    lr = await c.post(f"{tower_url}/leases/{lease_id}/renew",
                                      json={"ttl_s": lease_ttl}, headers=headers, timeout=10)
                    if lr.status_code != 200:
                        print(f"[{node_id}] lease renewal failed: {lr.status_code} {lr.text}")
                        lease_id = ""
                except Exception:
                    pass

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

        # graceful shutdown: give the claim back rather than letting it
        # expire and leave the Tower guessing
        if lease_id:
            try:
                await c.delete(f"{tower_url}/leases/{lease_id}", headers=headers, timeout=10)
                print(f"[{node_id}] released lease {lease_id}")
            except Exception:
                pass
    if spool is not None:
        print(f"[{node_id}] spool holds {spool.count()} sample(s)")


def main():
    p = argparse.ArgumentParser(prog="the-tower-agent")
    p.add_argument("--tower", default="http://127.0.0.1:8765")
    p.add_argument("--node-id", default=platform.node())
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--token", default="")
    p.add_argument("--sensor", default="", help="temperature backend: psutil, osx-cpu-temp, estimate")
    p.add_argument("--lease", default="", help="lease id to renew on every sweep")
    p.add_argument("--lease-ttl", type=float, default=300.0)
    p.add_argument("--register", action="store_true",
                   help="Register this node with the tower before reporting, describing "
                        "the machine the agent is running on")
    p.add_argument("--redline", type=float, default=85.0,
                   help="Redline temperature for this node, in Celsius")
    p.add_argument("--role", default="Mesh Node", help="What this node is for")
    p.add_argument("--location", default="local", help="Where this node physically is")
    p.add_argument("--enforcement", choices=("advisory", "enforce"), default="advisory",
                   help="Whether the tower may act on this node on its own")
    p.add_argument("--staleness", type=int, default=30,
                   help="Seconds without telemetry before the node is unknown")
    p.add_argument("--spool", default="",
                   help="Path to an on-disk buffer for samples that could not be delivered")
    p.add_argument("--no-discover-lease", action="store_true",
                   help="Do not ask the tower which lease this node holds")
    args = p.parse_args()

    async def _main():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        await run(args.tower, args.node_id, args.interval, args.token, args.sensor,
                  args.lease, args.lease_ttl, args.register, args.redline, args.role,
                  args.location, args.enforcement, args.staleness, args.spool,
                  not args.no_discover_lease, stop)

    asyncio.run(_main())


if __name__ == "__main__":
    main()
