"""Universe: ??? ????? ?? config.yaml + ??? USDT-????? ????? ccxt markets.

?????????????? CCXT_ID_MAP ?? ?????? ?????????? — ??? ?? ???????,
?? ?? ??????? (swap + linear + USDT + active is not False).
"""
from __future__ import annotations

import statistics

import ccxt

from cryptoarb.connectors.ccxt_connector import CCXT_ID_MAP


def fetch_perp_symbols(exchange_id: str) -> tuple[list[str], dict[str, tuple[float, float]]]:
    """?????????? ccxt: ?????? ?????? + (taker, maker) ?? ???????."""
    ccxt_id = CCXT_ID_MAP.get(exchange_id, exchange_id)
    cls = getattr(ccxt, ccxt_id)
    client = cls({"enableRateLimit": True, "timeout": 30000,
                  "options": {"defaultType": "swap"}})
    markets = client.load_markets()
    out: list[str] = []
    fees: dict[str, tuple[float, float]] = {}
    for m in markets.values():
        if (m.get("swap") and m.get("linear") and m.get("quote") == "USDT"
                and m.get("active") is not False):
            sym = m["symbol"]
            out.append(sym)
            taker = m.get("taker") or 0.0006
            maker = m.get("maker") or 0.0002
            fees[sym] = (float(taker), float(maker))
    return sorted(out), fees


def build_universe(exchange_ids: list[str],
                   min_common: int = 2) -> tuple[list[str], dict[str, list[str]]]:
    """Каталог: символ -> биржи. Общий список = на >= min_common биржах."""
    by_exch: dict[str, list[str]] = {}
    counter: dict[str, int] = {}
    for ex in exchange_ids:
        try:
            syms, _ = fetch_perp_symbols(ex)
        except Exception as e:
            print(f"[universe] {ex} failed: {e}")
            syms = []
        by_exch[ex] = syms
        for s in set(syms):
            counter[s] = counter.get(s, 0) + 1
    common = sorted(s for s, n in counter.items() if n >= min_common)
    print(f"[universe] {len(common)} symbols on >={min_common} ex")
    return common, by_exch


def fetch_volumes(exchange_id: str) -> dict[str, float]:
    """24ч quoteVolume (USDT) по всем свопам биржи одним bulk-запросом."""
    ccxt_id = CCXT_ID_MAP.get(exchange_id, exchange_id)
    cls = getattr(ccxt, ccxt_id)
    client = cls({"enableRateLimit": True, "timeout": 30000,
                  "options": {"defaultType": "swap"}})
    tickers = client.fetch_tickers()
    out: dict[str, float] = {}
    for sym, t in tickers.items():
        if not isinstance(t, dict):
            continue
        v = t.get("quoteVolume") or 0.0
        try:
            out[sym] = float(v)
        except (TypeError, ValueError):
            pass
    return out


def pick_by_volume(symbols: list[str], by_ex: dict[str, list[str]],
                   exchanges: list[str],
                   vmin: float, vmax: float) -> list[tuple[str, float]]:
    """Медианный 24ч объём по биржам в [vmin, vmax]. Возврат (symbol, med_vol)."""
    vols: dict[str, dict[str, float]] = {}
    for ex in exchanges:
        try:
            vols[ex] = fetch_volumes(ex)
            print(f"[vol] {ex}: {len(vols[ex])} тикеров")
        except Exception as e:
            print(f"[vol] {ex} failed: {e}")
            vols[ex] = {}
    picked: list[tuple[str, float]] = []
    for s in symbols:
        vs = [vols[ex].get(s, 0.0) for ex in exchanges if vols[ex].get(s)]
        if len(vs) < 2:
            continue
        med = statistics.median(vs)
        if vmin <= med <= vmax:
            picked.append((s, med))
    picked.sort(key=lambda x: x[1])
    print(f"[vol] picked {len(picked)} symbols in [{vmin:.0f}, {vmax:.0f}] USDT/day")
    return picked

