"""
Ультра-быстрый бэктест арбитража фандинга с участием DEX (Aster DEX, dYdX, Hyperliquid) против CEX и между DEX
на полной базе данных (139,284 записей фандинга).
"""
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import sqlite3
from pathlib import Path
from collections import defaultdict
import polars as pl
import numpy as np

DB_PATH = Path("data/scanner.db")

def run_dex_backtest():
    conn = sqlite3.connect(DB_PATH)
    
    # Загружаем всю историю фандинга через Polars
    df = pl.read_database("SELECT ts, exchange, symbol, funding_rate FROM funding_history", conn)
    print("=" * 95)
    print("ВЫГРУЗКА И ВАЛИДАЦИЯ БАЗЫ ДАННЫХ:")
    print("=" * 95)
    print(f"Всего записей в базе: {df.height}")
    
    counts = df.group_by("exchange").agg([
        pl.len().alias("count"),
        pl.col("symbol").n_unique().alias("symbols")
    ]).sort("count", descending=True)
    
    for row in counts.iter_rows(named=True):
        print(f"Площадка: {row['exchange']:<14} | Записей: {row['count']:>6} | Пар: {row['symbols']:>4}")

    # Нормализуем символ по базовой монете (BTC из BTC/USDT или BTC/USDC)
    # и создаем 8-часовые корзины для точной синхронизации начислений
    df = df.with_columns([
        pl.col("symbol").str.split("/").list.get(0).alias("base_coin"),
        ((pl.col("ts") / 28800).round() * 28800).cast(pl.Int64).alias("ts_bucket")
    ])

    # Разделяем на DEX и CEX
    dex_names = ["aster", "dydx", "hyperliquid"]

    # Самообъединение по (base_coin, ts_bucket)
    df_dex = df.filter(pl.col("exchange").is_in(dex_names))
    
    joined = df_dex.join(
        df,
        on=["base_coin", "ts_bucket"],
        how="inner",
        suffix="_target"
    ).filter(
        pl.col("exchange") != pl.col("exchange_target")
    )

    # Рассчитываем дельту ставок
    joined = joined.with_columns([
        ((pl.col("funding_rate") - pl.col("funding_rate_target")).abs() * 100.0).alias("delta_pct")
    ])

    # Исключаем дубли (ex1 < ex2)
    joined = joined.filter(pl.col("exchange") < pl.col("exchange_target"))

    # Отбираем квалифицированные сигналы (delta_pct >= 0.10%)
    qual = joined.filter(pl.col("delta_pct") >= 0.10).sort(["base_coin", "ts_bucket", "delta_pct"], descending=[False, False, True])
    print(f"\nВсего синхронных точек с участием DEX (Δ >= 0.10%): {qual.height}")

    # Фильтрация по кулдауну 8 часов (1 начисление) на монету и связку
    trades = []
    seen = set()
    
    # Сортируем по времени
    qual_rows = qual.sort("ts_bucket").iter_rows(named=True)
    for r in qual_rows:
        key = (r["base_coin"], r["exchange"], r["exchange_target"], r["ts_bucket"])
        if key in seen:
            continue
        seen.add(key)
        
        # Определяем long и short
        # Funding rate > target => short exchange, long exchange_target
        if r["funding_rate"] > r["funding_rate_target"]:
            ex_short, ex_long = r["exchange"], r["exchange_target"]
        else:
            ex_short, ex_long = r["exchange_target"], r["exchange"]
            
        trades.append({
            "ts": r["ts_bucket"],
            "symbol": r["base_coin"],
            "long_ex": ex_long,
            "short_ex": ex_short,
            "delta_pct": r["delta_pct"]
        })

    print(f"Уникальных системных сделок с участием DEX: {len(trades)}")

    DEFAULT_SLICE_USD = 350.0  # 10% от топ-3 стакана тонкой ноги
    ROUNDTRIP_COST_PCT = 0.224  # комиссии тейкера 0.20% + slippage 0.024%

    print("\n" + "=" * 95)
    print("РЕЗУЛЬТАТЫ БЭКТЕСТА С УЧАСТИЕМ DEX ПО ГОРИЗОНТАМ УДЕРЖАНИЯ:")
    print("=" * 95)
    for days, n_periods in [(1, 3), (2, 6), (3, 9), (7, 21)]:
        tot_gross = 0.0
        tot_cost = 0.0
        tot_net = 0.0
        wins = 0

        for t in trades:
            delta = t["delta_pct"]
            gross = DEFAULT_SLICE_USD * (delta * n_periods / 100.0)
            cost = DEFAULT_SLICE_USD * (ROUNDTRIP_COST_PCT / 100.0)
            net = gross - cost
            tot_gross += gross
            tot_cost += cost
            tot_net += net
            if net > 0:
                wins += 1

        wr = (wins / len(trades)) * 100.0 if trades else 0.0
        print(f"Горизонт: {days} дн. ({n_periods:>2} списаний) | Сделок: {len(trades):>3} | Win Rate: {wr:5.1f}% | "
              f"Gross: ${tot_gross:>8.2f} | Косты: ${tot_cost:>7.2f} | ЧИСТЫЙ PnL: ${tot_net:>+8.2f} USDT")

    # Топ связок с участием DEX
    print("\n" + "=" * 95)
    print("ТОП 20 СВЯЗОК С УЧАСТИЕМ ASTER DEX, DYDX, HYPERLIQUID (3 ДНЯ УДЕРЖАНИЯ):")
    print("=" * 95)
    print(f"{'Монета':<12} | {'Long площадка':<16} | {'Short площадка':<16} | {'Сделок':<6} | {'Ср. Δ %':<10} | {'Чистый PnL ($)':<14}")
    print("-" * 95)

    pair_stats = defaultdict(lambda: {"pnl": 0.0, "deltas": [], "cnt": 0})
    for t in trades:
        k = (t["symbol"], t["long_ex"], t["short_ex"])
        gross = DEFAULT_SLICE_USD * (t["delta_pct"] * 9 / 100.0)
        cost = DEFAULT_SLICE_USD * (ROUNDTRIP_COST_PCT / 100.0)
        net = gross - cost
        pair_stats[k]["pnl"] += net
        pair_stats[k]["deltas"].append(t["delta_pct"])
        pair_stats[k]["cnt"] += 1

    sorted_pairs = sorted(pair_stats.items(), key=lambda x: x[1]["pnl"], reverse=True)
    for (sym, el, es), data in sorted_pairs[:20]:
        avg_d = np.mean(data["deltas"])
        print(f"{sym:<12} | {el:<16} | {es:<16} | {data['cnt']:<6} | {avg_d:>8.4f}% | ${data['pnl']:>+12.2f}")

    # Статистика по DEX биржам
    print("\n" + "=" * 95)
    print("РАСПРЕДЕЛЕНИЕ ПРИБЫЛИ ПО САМИМ DEX ПЛОЩАДКАМ:")
    print("=" * 95)
    dex_pnl = defaultdict(float)
    dex_trades_cnt = defaultdict(int)
    for t in trades:
        gross = DEFAULT_SLICE_USD * (t["delta_pct"] * 9 / 100.0)
        cost = DEFAULT_SLICE_USD * (ROUNDTRIP_COST_PCT / 100.0)
        net = gross - cost
        for dex in dex_names:
            if t["long_ex"] == dex or t["short_ex"] == dex:
                dex_pnl[dex] += net
                dex_trades_cnt[dex] += 1

    for dex in dex_names:
        print(f"DEX: {dex:<14} | Сделок с участием: {dex_trades_cnt[dex]:>4} | Суммарный чистый PnL (3d): ${dex_pnl[dex]:>+10.2f} USDT")

if __name__ == "__main__":
    run_dex_backtest()
