"""Загрузка конфигурации с дефолтами (без обязательных ключей падать не должно)."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict

import yaml

DEFAULTS: Dict[str, Any] = {
    "exchanges": ["bybit", "okx"],
    "symbols_refresh_hours": 6,
    "market": {
        "staleness_sec": 6,
        "max_leg_dt_sec": 2.0,
        "max_sane_spread_pct": 3.0,
        "min_top_notional_usdt": 200,
        "funding_refresh_sec": 300,
        "orderbook_depth": 20,
        "orderbook_ttl_sec": 2.0,
        "orderbook_concurrency": 8,
    },
    "scan": {
        "interval_ms": 500,
        "prefilter_pct": 0.05,
        "signal_log_throttle_sec": 5,
    },
    "scoring": {
        "min_threshold_pct": 0.15,
        "assumed_holding_hours": 8,
        "slippage_buffer_pct": 0.02,
    },
    "default_fees": {"taker": 0.0006, "maker": 0.0002},
    "emulator": {
        "enabled": True,
        "virtual_position_size_usdt": 1000,
        "orphan_leg_timeout_sec": 3,
        "cooldown_sec": 60,
        "max_holding_hours": 72,
        "exit_threshold_frac": 0.5,
    },
    "storage": {"backend": "sqlite", "sqlite_path": "data/scanner.db", "retention_days": 30},
    "logging": {"dir": "logs", "retention_days": 90},
    "dashboard": {"enabled": True, "host": "127.0.0.1", "port": 8080},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str = "config.yaml") -> Dict[str, Any]:
    p = Path(path)
    file_cfg: Dict[str, Any] = {}
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            file_cfg = yaml.safe_load(f) or {}
    return _deep_merge(DEFAULTS, file_cfg)
