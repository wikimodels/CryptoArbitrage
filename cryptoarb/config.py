"""Загрузка конфигурации с дефолтами (без обязательных ключей падать не должно)."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict

import yaml

DEFAULTS: Dict[str, Any] = {
    "exchanges": ["bybit", "okx"],
    "symbols_refresh_hours": 6,
    "filters": {
        "min_common_exchanges": 2,
        "exclude_bases": [],
        "exclude_symbols": [],
    },
    "output_dir": "output",
    "market": {
        "staleness_sec": 6,
        "max_leg_dt_sec": 2.0,
        "max_sane_spread_pct": 3.0,
        "min_top_notional_usdt": 200,
        "price_sanity_deviation_pct": 50,
        "funding_refresh_sec": 300,
        "orderbook_depth": 20,
        "orderbook_ttl_sec": 2.0,
        "orderbook_concurrency": 8,
    },
    "scan": {
        "interval_ms": 200,
        "prefilter_pct": 0.05,
        "signal_log_throttle_sec": 5,
    },
    "scalp": {
        "enabled": True,
        "exit_spread_frac": 0.3,
        "max_holding_sec": 90,
        "max_entry_spread_pct": 1.0,
        "watchlist_mode": "auto",
        "watchlist_top": 15,
        "watchlist_min_spikes": 5,
        "spike_min_spread_pct": 0.3,
        "convergence_frac": 0.5,
        "convergence_window_sec": 120,
    },
    "scoring": {
        "min_threshold_pct": 0.30,
        "assumed_holding_hours": 8,
        "slippage_buffer_pct": 0.02,
        "funding_max_share_of_spread": 0.15,
    },
    "default_fees": {"taker": 0.0006, "maker": 0.0002},
    "emulator": {
        "enabled": True,
        "position_size_mode": "dynamic",
        "fixed_position_size_usdt": 1000,
        "dynamic_size_pct_of_book": 10,
        "dynamic_size_top_levels": 3,
        "min_position_size_usdt": 10,
        "max_position_size_usdt": 1000,
        "max_entry_slippage_pct": 0.15,
        "orphan_leg_timeout_sec": 3,
        "cooldown_sec": 60,
        "max_open_per_pair": 1,
        "loss_cooldown_sec": 900,
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
