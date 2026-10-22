"""Configuration for The Tower.

Every key documented in `config.example.yaml` is loaded here, and every key
has a test proving it takes effect. Configuration that silently does nothing
is worse than no configuration at all.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .schemas import NodeSpec

DEFAULT_CONFIG_PATHS = [
    Path.home() / ".the-tower" / "config.yaml",
    Path.home() / ".the-tower" / "config.yml",
]


@dataclass
class TowerConfig:
    """The Tower's runtime configuration."""
    db_path: Optional[str] = None
    radar_interval: float = 5.0
    token: Optional[str] = None
    rate_limit: int = 0
    cors_origins: List[str] = field(default_factory=list)
    nodes: List[NodeSpec] = field(default_factory=list)
    source: Optional[str] = None

    def apply_to_env(self):
        """Push config values into the environment.

        The server and the agent read their settings from the environment, so
        this is where a config file becomes behaviour. Only values that were
        explicitly set in the file are applied, and the environment still
        wins if it already has a value.
        """
        if self.token:
            os.environ.setdefault("TOWER_TOKEN", str(self.token))
        if self.rate_limit is not None:
            os.environ.setdefault("TOWER_RATE_LIMIT", str(self.rate_limit))
        if self.cors_origins:
            os.environ.setdefault("TOWER_CORS_ORIGINS", ",".join(self.cors_origins))
        if self.db_path:
            os.environ.setdefault("TOWER_DB_PATH", str(self.db_path))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "db_path": self.db_path,
            "radar_interval": self.radar_interval,
            "token": "set" if self.token else None,
            "rate_limit": self.rate_limit,
            "cors_origins": list(self.cors_origins),
            "nodes": [n.id for n in self.nodes],
            "source": self.source,
        }


def default_db_path() -> str:
    return os.environ.get("TOWER_DB_PATH") or str(Path.home() / ".the-tower" / "tower.sqlite")


def load_config(path: Optional[str] = None) -> TowerConfig:
    """Load configuration from a YAML file.

    Precedence: explicit path > default locations > built-in defaults.
    Environment variables always win over the file, because the environment
    is what a deployment actually sets.
    """
    import yaml

    chosen: Optional[Path] = None
    if path:
        p = Path(path).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"config file not found: {p}")
        chosen = p
    else:
        for candidate in DEFAULT_CONFIG_PATHS:
            if candidate.exists():
                chosen = candidate
                break

    cfg = TowerConfig()
    if chosen is not None:
        with open(chosen) as f:
            raw = yaml.safe_load(f) or {}
        cfg.source = str(chosen)
        if raw.get("db_path"):
            cfg.db_path = str(Path(raw["db_path"]).expanduser())
        if raw.get("radar_interval") is not None:
            cfg.radar_interval = float(raw["radar_interval"])
        if raw.get("token"):
            cfg.token = str(raw["token"])
        if raw.get("rate_limit") is not None:
            cfg.rate_limit = int(raw["rate_limit"])
        if raw.get("cors_origins"):
            cfg.cors_origins = [str(o).strip() for o in raw["cors_origins"] if str(o).strip()]
        if raw.get("nodes"):
            cfg.nodes = [NodeSpec(**n) for n in raw["nodes"]]

    # the environment always wins
    if os.environ.get("TOWER_TOKEN"):
        cfg.token = os.environ["TOWER_TOKEN"]
    if os.environ.get("TOWER_RATE_LIMIT"):
        cfg.rate_limit = int(os.environ["TOWER_RATE_LIMIT"])
    if os.environ.get("TOWER_CORS_ORIGINS"):
        cfg.cors_origins = [o.strip() for o in os.environ["TOWER_CORS_ORIGINS"].split(",") if o.strip()]
    if os.environ.get("TOWER_DB_PATH"):
        cfg.db_path = os.environ["TOWER_DB_PATH"]
    if not cfg.db_path:
        cfg.db_path = default_db_path()
    return cfg
