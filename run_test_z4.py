"""
Тестирование стратегии арбитража для Z-Score = 4.0 с исправленной логикой схождения к 0.
"""
import sys
import os

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

import json
import itertools
from pathlib import Path
from datetime import datetime
import polars as pl

from cryptoarb.backtest.zscore import compute_z, backtest_pair, backtest_directional
from cryptoarb.backtest.metrics import summarize

RAW_DIR = Path("data/raw_1m_30d")
TOP40_FILE = Path("output/top40_4ex.txt")
EXCHANGES = ["okx", "bitget", "mexc", "bingx"]


def load_series(ex: str, sym: str):
    safe = sym.replace("/", "_").replace(":", "_")
    p = RAW_DIR / ex / safe / "candles.parquet"
    if not p.exists():
        return [], []
    try:
        df = pl.read_parquet(p).sort("ts")
        return df["ts"].to_list(), df["c"].to_list()
    except Exception:
        return [], []


def align_series(ta, ca, tb, cb):
    mb = dict(zip(tb, cb))
    ts, pa, pb = [], [], []
    for t, c in zip(ta, ca):
        if t in mb:
            ts.append(t)
            pa.append(c)
            pb.append(mb[t])
    return ts, pa, pb


def run_test():
    if not TOP40_FILE.exists():
        print(f"Top 40 file not found: {TOP40_FILE}")
        return

    symbols = [l.strip() for l in open(TOP40_FILE, encoding="utf-8") if l.strip()]
    print(f"Загрузка 35-дневных данных для {len(symbols)} монет из {RAW_DIR}...", flush=True)

    data = {}
    for sym in symbols:
        for ex in EXCHANGES:
            ts, c = load_series(ex, sym)
            if len(ts) > 500:
                data[(sym, ex)] = (ts, c)

    print(f"Загружено {len(data)} валидных рядов данных (монета, биржа).", flush=True)

    # Параметры теста
    z_thresholds = [4.0, 3.5, 3.0]
    exit_zs = [0.0, 0.3]
    timestops = [15, 30]

    trades_by_cfg = {}
    pair_count = 0

    for sym in symbols:
        available_exs = [ex for ex in EXCHANGES if (sym, ex) in data]
        for ea, eb in itertools.combinations(sorted(available_exs), 2):
            ta, ca = data[(sym, ea)]
            tb, cb = data[(sym, eb)]
            ts, pa, pb = align_series(ta, ca, tb, cb)
            if len(ts) < 1000:
                continue

            # Фильтр битых фидов
            ratios = [a / b for a, b in zip(pa, pb) if b > 0]
            if not ratios:
                continue
            sorted_r = sorted(ratios)
            med = sorted_r[len(sorted_r) // 2]
            if abs(med - 1.0) > 0.008:
                continue

            pair_count += 1
            _, _, z = compute_z(ratios)

            for ez in z_thresholds:
                for xz in exit_zs:
                    for tm in timestops:
                        tr_n = backtest_pair(ts, pa, pb, entry_z=ez, fee_round_pct=0.24, slip_pct=0.04, exit_z=xz, timestop_min=tm, z=z)
                        tr_d = backtest_directional(ts, pa, pb, entry_z=ez, fee_pct=0.12, slip_pct=0.02, exit_z=xz, timestop_min=tm, z=z)

                        cfg_key_n = ("neutral", ez, xz, tm)
                        cfg_key_d = ("dir", ez, xz, tm)

                        if cfg_key_n not in trades_by_cfg:
                            trades_by_cfg[cfg_key_n] = []
                        if cfg_key_d not in trades_by_cfg:
                            trades_by_cfg[cfg_key_d] = []

                        for t in tr_n:
                            t["symbol"] = sym
                            t["a"] = ea
                            t["b"] = eb
                            trades_by_cfg[cfg_key_n].append(t)

                        for t in tr_d:
                            t["symbol"] = sym
                            t["a"] = ea
                            t["b"] = eb
                            trades_by_cfg[cfg_key_d].append(t)

    print(f"Протестировано {pair_count} пар бирж за 35 дней.", flush=True)

    table_rows = []
    for mode in ["neutral", "dir"]:
        for ez in z_thresholds:
            for xz in exit_zs:
                for tm in timestops:
                    tr = trades_by_cfg.get((mode, ez, xz, tm), [])
                    met = summarize(tr)
                    table_rows.append({
                        "mode": mode,
                        "entry_z": ez,
                        "exit_z": xz,
                        "tstop": tm,
                        "metrics": met
                    })

    # Сохраняем JSON-отчет
    report_file = Path("output/report_z4.json")
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(json.dumps(table_rows, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 94)
    print("      ИТОГИ БЭКТЕСТА С ИСПРАВЛЕННЫМ СХОЖДЕНИЕМ (Z-SCORE = 4.0 vs 3.5 vs 3.0)")
    print("=" * 94)
    
    for mode in ["neutral", "dir"]:
        title = "MARKET-NEUTRAL (2 ноги, комиссии 0.28%)" if mode == "neutral" else "DIRECTIONAL (1 нога на аномальной бирже, комиссии 0.14%)"
        print(f"\n--- РЕЖИМ: {title} ---")
        print(f"{'Z-Вход':<8} {'Exit-Z':<8} {'T-Stop':<8} {'Сделок':<8} {'Винрейт':<10} {'Avg Net%':<10} {'PF':<8} {'Sharpe':<8} {'MaxDD%':<8} {'Сумм Net%':<10}")
        print("-" * 96)
        for ez in z_thresholds:
            for xz in exit_zs:
                for tm in timestops:
                    tr = trades_by_cfg.get((mode, ez, xz, tm), [])
                    met = summarize(tr)
                    print(f"{ez:<8.1f} {xz:<8.1f} {tm:<8d} {met['n']:<8d} {met['winrate']:<10.1f} {met['avg_net']:<+10.3f} {met['pf']:<8.2f} {met['sharpe']:<8.2f} {met['maxdd']:<8.2f} {met['sum_net']:<+10.2f}")

    print("\n" + "=" * 94)
    print("      ДЕТАЛИЗАЦИЯ ДЛЯ Z-SCORE = 4.0 (ИСПРАВЛЕННЫЙ ВЫХОД)")
    print("=" * 94)
    
    for mode in ["neutral", "dir"]:
        z4_configs = [(k, trades_by_cfg[k]) for k in trades_by_cfg if k[0] == mode and k[1] == 4.0]
        if not z4_configs:
            continue
        best_cfg = max(z4_configs, key=lambda x: summarize(x[1])["sharpe"])
        cfg_key, tr_list = best_cfg
        met = summarize(tr_list)
        
        mode_title = "Market-Neutral (2 ноги)" if mode == "neutral" else "Directional (1 нога)"
        print(f"\n[{mode_title}] Лучшая конфигурация: entry_z=4.0, exit_z={cfg_key[2]}, timestop={cfg_key[3]}m")
        print(f"  Всего сделок: {met['n']}")
        print(f"  Винрейт:      {met['winrate']}%")
        print(f"  Avg Net:      {met['avg_net']:+.3f}%")
        print(f"  Profit Factor: {met['pf']:.2f}")
        print(f"  Sharpe Ratio:  {met['sharpe']:.2f}")
        print(f"  Max Drawdown:  {met['maxdd']:.2f}%")
        print(f"  Суммарный Net: {met['sum_net']:+.2f}% (Gross: {met['sum_gross']:+.2f}%)")
        
        reasons = {}
        for t in tr_list:
            r = t.get("reason", "unknown")
            reasons[r] = reasons.get(r, 0) + 1
        print("  Причины выходов:", {r: f"{count} ({count/met['n']*100:.1f}%)" for r, count in sorted(reasons.items(), key=lambda x: -x[1])})

        by_pair = {}
        for t in tr_list:
            pair_key = (t["symbol"], t["a"], t["b"])
            if pair_key not in by_pair:
                by_pair[pair_key] = []
            by_pair[pair_key].append(t)
        
        print("\n  Топ-10 пар по Net PnL (z=4.0):")
        pair_summaries = []
        for pk, ptrs in by_pair.items():
            pm = summarize(ptrs)
            pair_summaries.append((pk, pm))
        
        pair_summaries.sort(key=lambda x: x[1]["sum_net"], reverse=True)
        for pk, pm in pair_summaries[:10]:
            print(f"    {pk[0]:<18} {pk[1]}/{pk[2]}: N={pm['n']:3d} Win={pm['winrate']:5.1f}% Avg={pm['avg_net']:+6.3f}% PF={pm['pf']:5.2f} Net={pm['sum_net']:+6.2f}%")


if __name__ == "__main__":
    run_test()
