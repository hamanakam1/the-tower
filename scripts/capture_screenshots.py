"""Capture the dashboard screenshots used in the README.

The README shows the Tower's dashboard. Those pictures have to be real, or
they are a lie about what the software does. This script starts a Tower,
seeds it with the synthetic demo fleet, drives headless Chrome over the
DevTools protocol, and captures each panel.

    python scripts/capture_screenshots.py

Writes into docs/screenshots/.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "docs" / "screenshots"

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
]

# (filename, panel heading, width, height)
PANELS = [
    ("radar.png", "Radar", 640, 640),
    ("flight-board.png", "Flight Board", 900, 460),
    ("nodes.png", "Nodes", 360, 640),
    ("event-log.png", "Event Log", 360, 460),
    ("anti-air-battery.png", "Anti-Air Battery", 420, 300),
    ("trends.png", "Trends", 360, 320),
    ("clearance-request.png", "Clearance Request", 420, 420),
    ("containers.png", "Containers", 360, 240),
    ("safety-ladder.png", "Safety Ladder", 360, 240),
    ("leases.png", "Leases", 360, 240),
]

FULL_SHOTS = [
    ("dashboard-overview.png", 1600, 1100),
    ("dashboard-narrow.png", 460, 1500),
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_chrome() -> str:
    for c in CHROME_CANDIDATES:
        if c and Path(c).exists():
            return c
    raise SystemExit("no Chrome or Chromium found; cannot capture screenshots")


async def start_tower(port: int, db_path: str):
    proc = subprocess.Popen(
        [sys.executable, "-m", "the_tower", "serve", "--port", str(port),
         "--db", db_path, "--interval", "2"],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(80):
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(f"http://127.0.0.1:{port}/health", timeout=1)
                if r.status_code == 200:
                    return proc
        except Exception:
            pass
        await asyncio.sleep(0.25)
    proc.terminate()
    raise SystemExit("the tower did not start")


async def seed(port: int):
    """Put some traffic on the radar so the screenshots are not empty."""
    samples = [
        ("train model", 4, 16, 45, ["model/lora"], False),
        ("ingest batch", 2, 4, 20, ["data/train:read"], False),
        ("rogue miner", 8, 32, 120, ["miner"], True),
        ("cleanup job", 1, 2, 15, [], False),
        ("eval suite", 2, 8, 30, ["eval"], False),
        ("render frames", 3, 6, 60, ["render"], False),
    ]
    async with httpx.AsyncClient() as c:
        for i, (task, cpu, mem, dur, scope, rogue) in enumerate(samples):
            await c.post(f"http://127.0.0.1:{port}/clearance", json={
                "task": f"{task} #{i+1}", "priority": i % 3, "scope": scope, "rogue": rogue,
                "weight": {"cpu_cores": cpu, "memory_gb": mem, "duration_est_s": dur},
            })
        await c.post(f"http://127.0.0.1:{port}/containers", json={
            "container_id": "ctr-7f3a", "node_id": "m3max",
            "metadata": {"pid": 4821}})
        await c.post(f"http://127.0.0.1:{port}/containers", json={
            "container_id": "ctr-91bc", "node_id": "pi-cluster",
            "metadata": {"pid": 5102}})


class Chrome:
    """A very small DevTools protocol client."""

    def __init__(self, exe: str, port: int, width: int, height: int):
        self.exe = exe
        self.port = port
        self.width = width
        self.height = height
        self.proc = None
        self.ws = None
        self._id = 0

    async def __aenter__(self):
        profile = tempfile.mkdtemp(prefix="tower-shots-")
        self.proc = subprocess.Popen([
            self.exe, "--headless=new", "--disable-gpu", "--no-sandbox",
            "--hide-scrollbars", "--force-device-scale-factor=2",
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={profile}",
            f"--window-size={self.width},{self.height}",
            "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import websockets
        target = None
        for _ in range(80):
            try:
                async with httpx.AsyncClient() as c:
                    r = await c.get(f"http://127.0.0.1:{self.port}/json", timeout=1)
                    for t in r.json():
                        if t.get("type") == "page":
                            target = t["webSocketDebuggerUrl"]
                            break
                if target:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.25)
        if not target:
            raise SystemExit("chrome did not expose a debugging target")
        self.ws = await websockets.connect(target, max_size=64 * 1024 * 1024)
        return self

    async def __aexit__(self, *exc):
        if self.ws:
            await self.ws.close()
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()

    async def call(self, method: str, **params):
        self._id += 1
        msg_id = self._id
        await self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
        while True:
            raw = await self.ws.recv()
            msg = json.loads(raw)
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    async def goto(self, url: str):
        await self.call("Page.enable")
        await self.call("Page.navigate", url=url)
        await asyncio.sleep(3.0)

    async def set_viewport(self, width: int, height: int):
        await self.call("Emulation.setDeviceMetricsOverride", width=width,
                        height=height, deviceScaleFactor=2, mobile=False)

    async def evaluate(self, expression: str):
        r = await self.call("Runtime.evaluate", expression=expression,
                             returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")

    async def shot(self, path: Path, clip=None):
        params = {"format": "png", "captureBeyondViewport": bool(clip)}
        if clip:
            params["clip"] = {**clip, "scale": 2}
        r = await self.call("Page.captureScreenshot", **params)
        data = base64.b64decode(r["data"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return len(data)

    async def shot_panel(self, heading: str, path: Path):
        """Capture the panel whose heading matches, by its bounding box."""
        box = await self.evaluate(f"""
          (() => {{
            const panels = [...document.querySelectorAll('.panel')];
            const panel = panels.find(p => {{
              const h = p.querySelector('h3');
              return h && h.textContent.trim().toLowerCase() === {json.dumps(heading.lower())};
            }});
            if (!panel) return null;
            panel.scrollIntoView({{block: 'center'}});
            const r = panel.getBoundingClientRect();
            return {{x: r.x + window.scrollX, y: r.y + window.scrollY,
                     width: r.width, height: r.height}};
          }})()
        """)
        if not box:
            return 0
        await asyncio.sleep(0.4)
        return await self.shot(path, clip=box)


async def main():
    chrome_exe = find_chrome()
    port = free_port()
    db = tempfile.mktemp(suffix=".sqlite")
    tower = await start_tower(port, db)
    try:
        await seed(port)
        await asyncio.sleep(2.0)

        async with Chrome(chrome_exe, free_port(), 1600, 1100) as chrome:
            await chrome.set_viewport(1600, 1100)
            await chrome.goto(f"http://127.0.0.1:{port}/")
            await asyncio.sleep(2.5)

            for name, w, h in FULL_SHOTS:
                await chrome.set_viewport(w, h)
                await asyncio.sleep(1.0)
                size = await chrome.shot(SHOTS / name)
                print(f"  {name}: {size // 1024} KB")

            for name, heading, w, h in PANELS:
                await chrome.set_viewport(w, h + 200)
                await asyncio.sleep(0.6)
                size = await chrome.shot_panel(heading, SHOTS / name)
                if size:
                    print(f"  {name}: {size // 1024} KB")
                else:
                    print(f"  {name}: panel not found")
    finally:
        tower.terminate()
        try:
            tower.wait(timeout=5)
        except Exception:
            tower.kill()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(db + suffix)
            except OSError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
