"""On-disk telemetry spool.

A node agent that loses its telemetry because the network blipped is a node
the Tower cannot trust. The spool is a small append-only file of samples
that were collected but not yet delivered.

When the link comes back, the spool is drained in order, oldest first. The
Tower then sees the gap in its history rather than a hole in it.

The file is plain JSON lines: it can be inspected with `cat`, truncated by
hand, and it never needs a migration.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class Spool:
    """A bounded, append-only buffer of undelivered samples."""

    def __init__(self, path: str, max_samples: int = 10000):
        self.path = Path(path).expanduser()
        self.max_samples = max_samples
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ---- writing ----
    def append(self, payload: Dict[str, Any]):
        """Record a sample that could not be delivered."""
        line = json.dumps({"ts": time.time(), "payload": payload}, separators=(",", ":"))
        with self._lock:
            with open(self.path, "a") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._trim()

    def _trim(self):
        """Keep the spool bounded: drop the oldest samples first."""
        try:
            count = sum(1 for _ in open(self.path))
        except OSError:
            return
        if count <= self.max_samples:
            return
        lines = self._read_lines()
        keep = lines[-self.max_samples:]
        self._rewrite(keep)

    # ---- reading ----
    def _read_lines(self) -> List[str]:
        try:
            with open(self.path) as f:
                return [ln for ln in (l.strip() for l in f) if ln]
        except FileNotFoundError:
            return []
        except OSError:
            return []

    def _rewrite(self, lines: List[str]):
        # write to a temp file and rename, so a crash mid-write cannot
        # leave a half-written spool behind
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".spool-")
        try:
            with os.fdopen(fd, "w") as f:
                for ln in lines:
                    f.write(ln + "\n")
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def peek(self, limit: int = 100) -> List[Dict[str, Any]]:
        """The oldest samples, without removing them."""
        out = []
        for ln in self._read_lines()[:limit]:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out

    def pop(self, limit: int = 100) -> List[Dict[str, Any]]:
        """The oldest samples, removed from the spool."""
        with self._lock:
            lines = self._read_lines()
            if not lines:
                return []
            take, rest = lines[:limit], lines[limit:]
            self._rewrite(rest)
        out = []
        for ln in take:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out

    def drain(self, deliver, limit: int = 500) -> int:
        """Deliver the spooled samples, oldest first.

        `deliver(payload)` returns True when the sample was accepted. A
        sample that is not accepted stops the drain, so the order is kept
        and nothing is dropped silently.
        """
        delivered = 0
        for entry in self.peek(limit):
            try:
                payload = entry["payload"]
            except (KeyError, TypeError):
                self.pop(1)
                continue
            try:
                ok = deliver(payload)
            except Exception:
                break
            if not ok:
                break
            self.pop(1)
            delivered += 1
        return delivered

    def count(self) -> int:
        """How many samples are waiting (corrupt lines are not samples)."""
        count = 0
        for ln in self._read_lines():
            try:
                json.loads(ln)
                count += 1
            except json.JSONDecodeError:
                continue
        return count

    def clear(self):
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0
