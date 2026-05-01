"""
config.py — Configuration loader for Nova-NextGen Gateway.
Reads config.yaml, resolves paths, and exposes typed settings.

Author: Jordan Koch
"""

import yaml
from pathlib import Path
from typing import Optional


_config: dict = {}


def load(path: str = None) -> dict:
    global _config
    if not path:
        path = Path(__file__).parent.parent / "config.yaml"
    with open(path, "r") as f:
        _config = yaml.safe_load(f)
    # Ensure pg_dsn has a default
    gw = _config.setdefault("gateway", {})
    if "pg_dsn" not in gw:
        gw["pg_dsn"] = "postgresql://localhost/nova_ops"
    return _config


def get() -> dict:
    if not _config:
        load()
    return _config


def gateway_port() -> int:
    return get()["gateway"]["port"]


def gateway_host() -> str:
    return get()["gateway"].get("host", "127.0.0.1")


def pg_dsn() -> str:
    return get()["gateway"]["pg_dsn"]


def backend_cfg(name: str) -> dict:
    return get().get("backends", {}).get(name, {})


def routing_rules() -> list[dict]:
    return get().get("routing", {}).get("rules", [])


def default_backend() -> str:
    return get().get("routing", {}).get("default_backend", "ollama")


def default_model() -> str:
    return get().get("routing", {}).get("default_model", "qwen3-coder:30b")


def context_ttl() -> int:
    return get().get("context", {}).get("ttl_seconds", 3600)


def validation_enabled() -> bool:
    return get().get("validation", {}).get("enabled", True)


def consensus_threshold() -> float:
    return get().get("validation", {}).get("consensus_threshold", 0.7)


def max_validators() -> int:
    return get().get("validation", {}).get("max_validators", 2)
