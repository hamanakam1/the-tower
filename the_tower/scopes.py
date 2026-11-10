"""Hierarchical scope keys with access modes.

A scope key names a resource in the mesh — `data/train`, `model/lora`,
`node/m3max/gpu`. Keys are hierarchical: a claim on `model/lora` covers
`model/lora/v2`, but not `model/lora-adapters`.

A claim also carries a *mode*:

| Mode | Meaning | Conflicts with |
|---|---|---|
| `read` | the flight looks at the resource | any `write` or `intent` on the same or a nested key |
| `write` | the flight modifies the resource | any `read`, `write` or `intent` on the same or a nested key |
| `intent` | the flight declares it *will* write | everything, including another `intent` |

Two readers never conflict. A reader and a writer do. An intent is a
declaration of future conflict, so it conflicts with anything else that
touches the same subtree.

Everything here is pure: given two sets of claims, the same conflict is
always reported.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


class ScopeMode(str, Enum):
    READ = "read"
    WRITE = "write"
    INTENT = "intent"


# How a mode conflicts with another mode.
#   rows: the first claim's mode, columns: the second claim's mode
#   True means the two claims cannot coexist.
_CONFLICT = {
    (ScopeMode.READ, ScopeMode.READ): False,
    (ScopeMode.READ, ScopeMode.WRITE): True,
    (ScopeMode.READ, ScopeMode.INTENT): True,
    (ScopeMode.WRITE, ScopeMode.READ): True,
    (ScopeMode.WRITE, ScopeMode.WRITE): True,
    (ScopeMode.WRITE, ScopeMode.INTENT): True,
    (ScopeMode.INTENT, ScopeMode.READ): True,
    (ScopeMode.INTENT, ScopeMode.WRITE): True,
    (ScopeMode.INTENT, ScopeMode.INTENT): True,
}


def _modes_conflict(a: ScopeMode, b: ScopeMode) -> bool:
    return _CONFLICT.get((a, b), False)


@dataclass(frozen=True)
class ScopeClaim:
    """One claim on one resource."""
    path: str
    mode: ScopeMode = ScopeMode.WRITE

    def __post_init__(self):
        if not self.path or not self.path.strip():
            raise ValueError("scope path must not be empty")
        stripped = self.path.strip("/")
        if not stripped:
            raise ValueError(f"invalid scope path: {self.path!r}")
        segments = stripped.split("/")
        if any(not seg.strip() for seg in segments):
            raise ValueError(f"invalid scope path: {self.path!r}")
        if any(seg != seg.strip() for seg in segments):
            raise ValueError(f"invalid scope path: {self.path!r}")

    def segments(self) -> List[str]:
        return [s for s in self.path.strip("/").split("/") if s]

    def covers(self, other: "ScopeClaim") -> bool:
        """Does this claim's path cover the other claim's path?

        `model/lora` covers `model/lora` and `model/lora/v2`.
        `model/lora` does not cover `model/lora-adapters`.
        """
        a = self.segments()
        b = other.segments()
        if len(a) > len(b):
            return False
        return b[:len(a)] == a

    def to_dict(self) -> Dict[str, str]:
        return {"path": self.path, "mode": self.mode.value}


def parse_claim(spec: str) -> ScopeClaim:
    """Parse a scope specification.

    Accepted forms:
        "data/train"              -> write (the default: a claim that
                                     touches a resource conflicts)
        "data/train:read"         -> read
        "data/train:write"        -> write
        "data/train:intent"       -> intent
    """
    if ":" in spec:
        path, _, mode = spec.rpartition(":")
        path = path.strip()
        try:
            return ScopeClaim(path=path, mode=ScopeMode(mode.strip().lower()))
        except ValueError:
            # unknown mode: treat the whole thing as a path
            return ScopeClaim(path=spec.strip())
    return ScopeClaim(path=spec.strip())


def parse_claims(specs: Iterable[str]) -> List[ScopeClaim]:
    return [parse_claim(s) for s in specs if s and s.strip()]


def claims_conflict(a: ScopeClaim, b: ScopeClaim) -> bool:
    """Do two claims on two resources conflict?"""
    if not a.covers(b) and not b.covers(a):
        return False
    return _modes_conflict(a.mode, b.mode)


def find_conflict(a: Sequence[ScopeClaim], b: Sequence[ScopeClaim]) -> Optional[Tuple[ScopeClaim, ScopeClaim]]:
    """The first conflicting pair between two claim sets, if any."""
    for claim_a in a:
        for claim_b in b:
            if claims_conflict(claim_a, claim_b):
                return (claim_a, claim_b)
    return None


def normalise(scope: Sequence[str]) -> List[str]:
    """Canonical string form of a claim set, for storage and comparison."""
    return sorted(f"{c.path}:{c.mode.value}" for c in parse_claims(scope))


def expand_hierarchy(paths: Sequence[str]) -> List[str]:
    """All ancestor paths of the given keys.

    `model/lora/v2` expands to `model`, `model/lora`, `model/lora/v2`.
    Used to ask "which subtree does this flight claim?".
    """
    out: Set[str] = set()
    for p in paths:
        segs = [s for s in p.strip("/").split("/") if s]
        for i in range(1, len(segs) + 1):
            out.add("/".join(segs[:i]))
    return sorted(out)
