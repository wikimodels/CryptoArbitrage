"""CLI: universe -> collect 1m -> zscore grid -> metrics -> report JSON.

poetry run python -m cryptoarb.backtest.run --help
"""
from __future__ import annotations

import argparse
import itertools
import json
import time
from pathlib import Path

import yaml

from .collect import fetch_1m, save_candles
from .metrics import summarize
from .universe import build_universe, pick_by_volume
from .zscore import ENTRY_ZS, backtest_directional, backtest_pair


def load_cfg(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def align(a_ts: list[int], a_c: list[float],
          b_ts: list[int], b_c: list[float]) -> tuple[list, list, list]:
    """?????? ???????? ????? UTC: ?????? ????? ??????, ???? ?? ?????????."""
    mb = dict(zip(b_ts, b_c))
    ts, pa, pb = [], [], []
    for t, c in zip(a_ts, a_c):
        if t in mb:
            ts.append(t)
            pa.append(c)
            pb.append(mb[t])
    return ts, pa, pb


UNIVERSE_CACHE = Path("output/backtest_universe.json")


def cmd_universe(cfg: dict):
    common, by_ex = build_universe(cfg.get("exchanges", []),
                                   cfg.get("min_common_exchanges", 3))
    UNIVERSE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    UNIVERSE_CACHE.write_text(json.dumps({"ts": time.time(), "symbols": common,
                                          "by_ex": by_ex,
                                          "coverage": {k: len(v) for k, v in by_ex.items()}},
                                         ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"symbols={len(common)} -> {UNIVERSE_CACHE}")


def get_universe(cfg: dict, max_age_h: float = 6.0):
    """Кэш каталога 6ч — load_markets 11 бирж занимает 1-2 мин, не дёргаем каждый раз."""
    if UNIVERSE_CACHE.exists():
        try:
            d = json.loads(UNIVERSE_CACHE.read_text(encoding="utf-8"))
            if time.time() - d.get("ts", 0) < max_age_h * 3600 and d.get("by_ex"):
                print(f"[universe] cache {len(d['symbols'])} символов")
                return d["symbols"], d["by_ex"]
        except Exception:
            pass
    common, by_ex = build_universe(cfg.get("exchanges", []),
                                   cfg.get("min_common_exchanges", 3))
    UNIVERSE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    UNIVERSE_CACHE.write_text(json.dumps({"ts": time.time(), "symbols": common,
                                          "by_ex": by_ex,
                                          "coverage": {k: len(v) for k, v in by_ex.items()}},
                                         ensure_ascii=False, indent=2), encoding="utf-8")
    return common, by_ex


def cmd_collect(cfg: dict, symbols: list[str] | None, days: int):
    import cryptoarb.backtest.collect as C
    C.RAW_ROOT = Path(cfg.get("raw_dir", "data/raw_1m"))
    now = int(time.time() * 1000)
    since = now - days * 86400 * 1000
    common, by_ex = get_universe(cfg)
    targets = [s for s in (symbols or common) if s in common] or common[:5]
    exs = cfg.get("pilot_exchanges") or cfg.get("exchanges", [])
    for sym in targets:
        for ex in exs:
            if sym not in by_ex.get(ex, []):
                continue
            try:
                import cryptoarb.backtest.collect as C
                safe = sym.replace("/", "_").replace(":", "_")
                p = C.RAW_ROOT / ex / safe / "candles.parquet"
                if p.exists():
                    print(f"{sym} {ex}: cached, skip")
                    continue
                rows = fetch_1m(ex, sym, since, now)
                p = save_candles(ex, sym, rows)
                print(f"{sym} {ex}: {len(rows)} -> {p}", flush=True)
            except Exception as e:
                print(f"{sym} {ex} FAIL: {e}")


def load_series(exchange_id: str, symbol: str, raw_root: str = "data/raw_1m"):
    safe = symbol.replace("/", "_").replace(":", "_")
    base = Path(raw_root) / exchange_id / safe
    for name in ("candles.parquet", "candles.csv"):
        p = base / name
        if p.exists():
            if p.suffix == ".parquet":
                import polars as pl
                df = pl.read_parquet(p).sort("ts")
                return df["ts"].to_list(), df["c"].to_list()
            import csv
            ts, c = [], []
            with open(p) as f:
                for row in csv.DictReader(f):
                    ts.append(int(row["ts"]))
                    c.append(float(row["c"]))
            return ts, c
    return [], []


def cmd_run(cfg: dict, symbols: list[str] | None):
    fee = cfg.get("fee_round_pct", 0.24)
    slip = cfg.get("slip_pct", 0.04)
    exs = cfg.get("pilot_exchanges") or cfg.get("exchanges", [])
    raw_root = cfg.get("raw_dir", "data/raw_1m")
    report_path = cfg.get("report_path", "output/backtest_report.json")
    common, by_ex = get_universe(cfg)
    targets = [s for s in (symbols or common) if s in common] or common[:5]
    rows_out = []
    for sym in targets:
        present = [e for e in exs if sym in by_ex.get(e, [])]
        series = {e: load_series(e, sym, raw_root) for e in present}
        series = {e: v for e, v in series.items() if len(v[0]) > 60}
        for ea, eb in itertools.combinations(sorted(series), 2):
            ta, ca = series[ea]
            tb, cb = series[eb]
            ts, pa, pb = align(ta, ca, tb, cb)
            if len(ts) < 200:
                continue
            # sanity: медиана ratio около 1, иначе разные инструменты/битый фид
            # (пример: coinex BTC 80136 против 81130 = ratio 0.988, скип)
            held = sorted(pb[i] and pa[i] / pb[i] for i in range(len(ts)) if pb[i])
            med = held[len(held) // 2]
            if abs(med - 1.0) > 0.005:
                print(f"SKIP {sym} {ea}/{eb}: median ratio {med:.4f} (разные инструменты?)")
                continue
            cut = int(len(ts) * (1 - cfg.get("out_sample_frac", 0.25)))
            exit_zs = cfg.get("exit_zs", [cfg.get("exit_z", 0.3)])
            tmins = cfg.get("timestop_mins", [cfg.get("timestop_min", 15)])
            for ez in cfg.get("entry_zs", ENTRY_ZS):
                for xz in exit_zs:
                    for tm in tmins:
                        tr_in = backtest_pair(ts[:cut], pa[:cut], pb[:cut], ez, fee, slip, xz, tm)
                        tr_ou = backtest_pair(ts[cut:], pa[cut:], pb[cut:], ez, fee, slip, xz, tm)
                        td_in = backtest_directional(ts[:cut], pa[:cut], pb[:cut], ez,
                                                     fee / 2, slip / 2, xz, tm)
                        td_ou = backtest_directional(ts[cut:], pa[cut:], pb[cut:], ez,
                                                     fee / 2, slip / 2, xz, tm)
                        rows_out.append({"symbol": sym, "a": ea, "b": eb,
                                         "entry_z": ez, "exit_z": xz, "tstop": tm,
                                         "n_min": len(ts),
                                         "neutral_in": summarize(tr_in),
                                         "neutral_out": summarize(tr_ou),
                                         "dir_in": summarize(td_in),
                                         "dir_out": summarize(td_ou),
                                  # legacy-ключи для старого дашборда
                                  "in": summarize(tr_in), "out": summarize(tr_ou),
                                  "tstop": tm})
    out = Path(report_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        try:
            prev = json.loads(out.read_text(encoding="utf-8"))
            merged = {(r["symbol"], r["a"], r["b"], r["entry_z"]): r for r in prev}
            for r in rows_out:
                merged[(r["symbol"], r["a"], r["b"], r["entry_z"])] = r
            rows_out = sorted(merged.values(),
                              key=lambda r: (r["symbol"], r["a"], r["b"], r["entry_z"]))
            print(f"merged with {len(prev)} prev rows")
        except Exception as e:
            print(f"merge failed, overwrite: {e}")
    out.write_text(json.dumps(rows_out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"combos={len(rows_out)} -> {out}")
    print("--- TOP market-neutral (in-sharpe) ---")
    def _g(r, *ks, d=0):
        v = r
        for k in ks:
            v = (v or {}).get(k, {}) if isinstance(v, dict) else {}
        return v if v != {} else d

    for r in sorted(rows_out, key=lambda r: _g(r, "neutral_in", "sharpe"),
                    reverse=True)[:10]:
        ni, no = r.get("neutral_in", {}), r.get("neutral_out", {})
        print(f'{r["symbol"]} {r["a"]}/{r["b"]} z={r["entry_z"]}: '
              f'n={ni.get("n")} shr={ni.get("sharpe")} '
              f'pf={ni.get("pf")} avg={ni.get("avg_net")}% | '
              f'OUT shr={no.get("sharpe")} n={no.get("n")}')
    print("--- TOP directional-A (in-sharpe) ---")
    for r in sorted(rows_out, key=lambda r: _g(r, "dir_in", "sharpe"),
                    reverse=True)[:10]:
        di, do = r.get("dir_in", {}), r.get("dir_out", {})
        print(f'{r["symbol"]} {r["a"]}/{r["b"]} z={r["entry_z"]}: '
              f'n={di.get("n")} shr={di.get("sharpe")} '
              f'pf={di.get("pf")} avg={di.get("avg_net")}% | '
              f'OUT shr={do.get("sharpe")} n={do.get("n")}')


def cmd_pick(cfg: dict):
    """Пилот: символы с медианным 24ч объёмом в [vmin, vmax]."""
    common, by_ex = get_universe(cfg)
    vmin = cfg.get("volume_min_usdt", 100_000)
    vmax = cfg.get("volume_max_usdt", 10_000_000)
    exs = cfg.get("pilot_exchanges") or cfg.get("exchanges", [])
    picked = pick_by_volume(common, by_ex, exs, vmin, vmax)
    out = Path("output/backtest_pilot.json")
    out.write_text(json.dumps({"vmin": vmin, "vmax": vmax,
                               "symbols": [s for s, _ in picked],
                               "vols": {s: v for s, v in picked}},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"picked={len(picked)} -> {out}")
    for s, v in picked[:30]:
        print(f"  {s}: {v:,.0f} USDT/день")


def main():
    import sys
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="z-score backtest")
    ap.add_argument("--config", default="cryptoarb/backtest/backtest.yaml")
    ap.add_argument("cmd", choices=["universe", "pick", "collect", "run"])
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--symbols-file", default=None)
    ap.add_argument("--days", type=int, default=90)
    a = ap.parse_args()
    cfg = load_cfg(a.config)
    syms = list(a.symbols or [])
    if a.symbols_file:
        syms += [l.strip() for l in
                 Path(a.symbols_file).read_text(encoding="utf-8").splitlines()
                 if l.strip()]
    syms = syms or None
    if a.cmd == "universe":
        cmd_universe(cfg)
    elif a.cmd == "pick":
        cmd_pick(cfg)
    elif a.cmd == "collect":
        cmd_collect(cfg, syms, a.days)
    else:
        cmd_run(cfg, syms)


if __name__ == "__main__":
    main()

