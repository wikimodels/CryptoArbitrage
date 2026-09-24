"""???? 1m ?????? + funding history. ???????? data/raw/{ex}/{symbol}/{yyyy-mm}.parquet.

????????? ????? fetch_ohlcv(since, limit). UTC, ???? = NaN (?? ?????????????).
"""
from __future__ import annotations

import time
from pathlib import Path

import ccxt

from cryptoarb.connectors.ccxt_connector import CCXT_ID_MAP

import os as _os

RAW_ROOT = Path(_os.environ.get("BACKTEST_RAW_ROOT", "data/raw_1m"))
FUND_ROOT = Path("data/funding")


def _client(exchange_id: str):
    ccxt_id = CCXT_ID_MAP.get(exchange_id, exchange_id)
    cls = getattr(ccxt, ccxt_id)
    return cls({"enableRateLimit": True, "timeout": 30000,
                "options": {"defaultType": "swap"}})


def fetch_1m(exchange_id: str, symbol: str, since_ms: int,
             until_ms: int, limit: int = 1000) -> list[list]:
    """??? 1m ????? [ts, o, h, l, c, v] ? [since, until)."""
    ex = _client(exchange_id)
    out: list[list] = []
    # Окна по ~5 суток: gate.io не даёт свечи старше ~10000 минут одним запросом
    win_ms = 5 * 86400 * 1000
    cur = since_ms
    while cur < until_ms:
        wend = min(cur + win_ms, until_ms)
        since = cur
        guard = 0
        while since < wend and guard < 500:
            guard += 1
            batch = ex.fetch_ohlcv(symbol, timeframe="1m", since=since, limit=limit)
            if not batch:
                break
            out.extend(batch)
            last_ts = batch[-1][0]
            if last_ts < since:
                break
            since = last_ts + 60_000
            # NOTE: не выходим по len(batch) < limit — часть бирж (okx/kucoin/htx)
            # отдают меньше запрошенного за раз, но история дальше есть
            time.sleep(ex.rateLimit / 1000.0)
        cur = wend
    # ?????? ?? ???? + ?????
    seen = {}
    for b in out:
        if since_ms <= b[0] < until_ms:
            seen[b[0]] = b
    return sorted(seen.values())


def save_candles(exchange_id: str, symbol: str, rows: list[list]) -> Path:
    safe = symbol.replace("/", "_").replace(":", "_")
    d = RAW_ROOT / exchange_id / safe
    d.mkdir(parents=True, exist_ok=True)
    try:
        import polars as pl
        df = pl.DataFrame({"ts": [r[0] for r in rows], "o": [r[1] for r in rows],
                           "h": [r[2] for r in rows], "l": [r[3] for r in rows],
                           "c": [r[4] for r in rows], "v": [r[5] for r in rows]})
        p = d / "candles.parquet"
        df.write_parquet(p)
        return p
    except ImportError:
        import csv
        p = d / "candles.csv"
        with open(p, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ts", "o", "h", "l", "c", "v"])
            w.writerows(rows)
        return p


def fetch_funding_history(exchange_id: str, symbol: str, since_ms: int) -> list[dict]:
    ex = _client(exchange_id)
    try:
        hist = ex.fetch_funding_rate_history(symbol, since=since_ms)
        return hist or []
    except Exception:
        try:
            hist = ex.fetch_funding_rate_history(symbol)
            return [h for h in (hist or []) if h.get("timestamp", 0) >= since_ms]
        except Exception:
            return []

