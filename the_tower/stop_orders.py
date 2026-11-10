"""Stop-order polling for the agent.

The Tower decides what must be stopped. The agent is the only thing that
can stop a process on its own machine. The two meet in the middle: the agent
polls the Tower for stop orders and executes them locally.

A stop order is a flight id plus a reason. When the agent sees one, it
terminates the process it is holding for that flight (if any) and exits.

This is the last piece of "the Tower never blocks the mesh": a node agent
that is not reachable cannot be told to stop, but the next time it checks
in it learns that it is already supposed to be dead.
"""
from __future__ import annotations

import asyncio
import os
import signal
import time
from typing import Callable, Optional

import httpx


class StopOrderClient:
    """Polls the Tower for stop orders and dispatches them."""

    def __init__(self, tower_url: str, node_id: str, token: str = ""):
        self.tower_url = tower_url.rstrip("/")
        self.node_id = node_id
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._seen: set = set()

    async def fetch_orders(self) -> List[dict]:
        """Return stop orders for this node that have not been acted on."""
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(f"{self.tower_url}/stop-orders/{self.node_id}",
                                headers=self.headers, timeout=10)
            if r.status_code == 404:
                return []
            if r.status_code != 200:
                return []
            orders = r.json().get("orders", [])
            return [o for o in orders if o.get("flight_id") not in self._seen]
        except Exception:
            return []

    async def acknowledge(self, flight_id: str):
        """Tell the Tower we acted on this stop order."""
        try:
            async with httpx.AsyncClient() as c:
                await c.post(f"{self.tower_url}/stop-orders/{self.node_id}/ack",
                             json={"flight_id": flight_id}, headers=self.headers,
                             timeout=10)
        except Exception:
            pass
        self._seen.add(flight_id)

    async def poll_and_act(self, on_stop: Callable[[str, str], None]) -> bool:
        """Poll once. Returns True if a stop order was acted on."""
        orders = await self.fetch_orders()
        acted = False
        for order in orders:
            fid = order.get("flight_id")
            reason = order.get("reason", "Tower stop order")
            if not fid:
                continue
            try:
                on_stop(fid, reason)
            except Exception:
                pass
            await self.acknowledge(fid)
            acted = True
        return acted


def terminate_process(pid: int, grace_s: float = 5.0):
    """Give a process a chance to clean up, then SIGKILL it."""
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.time() + grace_s
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.25)
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
