"""Config parity tests.

Every key documented in `config.example.yaml` must provably take effect.
Configuration that silently does nothing is worse than no configuration.
"""
import os
import textwrap

import pytest

from the_tower.config import TowerConfig, load_config


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Keep the environment clean: config tests push values into it."""
    saved = dict(os.environ)
    for key in ("TOWER_TOKEN", "TOWER_RATE_LIMIT", "TOWER_CORS_ORIGINS", "TOWER_DB_PATH"):
        monkeypatch.delenv(key, raising=False)
    yield
    os.environ.clear()
    os.environ.update(saved)


def write_config(tmp_path, body: str) -> str:
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(body))
    return str(p)


def test_db_path_takes_effect(tmp_path):
    path = write_config(tmp_path, f"""
        db_path: {tmp_path / "tower.sqlite"}
    """)
    cfg = load_config(path)
    assert cfg.db_path == str(tmp_path / "tower.sqlite")


def test_radar_interval_takes_effect(tmp_path):
    path = write_config(tmp_path, """
        radar_interval: 2.5
    """)
    cfg = load_config(path)
    assert cfg.radar_interval == 2.5


def test_token_takes_effect(tmp_path):
    path = write_config(tmp_path, """
        token: sekrit
    """)
    cfg = load_config(path)
    assert cfg.token == "sekrit"
    cfg.apply_to_env()
    assert os.environ["TOWER_TOKEN"] == "sekrit"


def test_rate_limit_takes_effect(tmp_path):
    path = write_config(tmp_path, """
        rate_limit: 42
    """)
    cfg = load_config(path)
    assert cfg.rate_limit == 42
    cfg.apply_to_env()
    assert os.environ["TOWER_RATE_LIMIT"] == "42"


def test_cors_origins_take_effect(tmp_path):
    path = write_config(tmp_path, """
        cors_origins:
          - http://localhost:3000
          - https://tower.example
    """)
    cfg = load_config(path)
    assert cfg.cors_origins == ["http://localhost:3000", "https://tower.example"]
    cfg.apply_to_env()
    assert "http://localhost:3000" in os.environ["TOWER_CORS_ORIGINS"]


def test_nodes_take_effect(tmp_path):
    path = write_config(tmp_path, """
        nodes:
          - id: cfg-node
            name: Config Node
            role: test
            specs: x
            constraints: y
            cpu_cores: 4
            memory_gb: 8
            redline_temp_c: 90
            max_power_watts: 50
            network_capacity_mbps: 100
    """)
    cfg = load_config(path)
    assert len(cfg.nodes) == 1
    assert cfg.nodes[0].id == "cfg-node"
    assert cfg.nodes[0].memory_gb == 8


def test_environment_overrides_file(tmp_path, monkeypatch):
    path = write_config(tmp_path, """
        token: from-file
        rate_limit: 10
    """)
    monkeypatch.setenv("TOWER_TOKEN", "from-env")
    monkeypatch.setenv("TOWER_RATE_LIMIT", "99")
    cfg = load_config(path)
    assert cfg.token == "from-env"
    assert cfg.rate_limit == 99


def test_missing_config_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(str(tmp_path / "nope.yaml"))


def test_defaults_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr("the_tower.config.DEFAULT_CONFIG_PATHS", [])
    cfg = load_config()
    assert cfg.radar_interval == 5.0
    assert cfg.rate_limit == 0
    assert cfg.nodes == []
    assert cfg.db_path  # a default is always set


def test_example_config_loads():
    """The shipped example must actually parse."""
    from pathlib import Path
    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    cfg = load_config(str(example))
    assert cfg.radar_interval == 5.0
    assert len(cfg.nodes) == 3
    ids = {n.id for n in cfg.nodes}
    assert ids == {"m3max", "pi-cluster", "trailer-hpc"}
    # every node carries the enforcement and freshness settings
    for n in cfg.nodes:
        assert n.enforcement in ("advisory", "enforce")
        assert n.staleness_budget_s > 0
