"""
Детальный расчет и бэктест стратегии Cash & Carry (Спот Uniswap Long + Перп CEX/DEX Short)
на базе 139,284 записей фандинга по всем 150 монетам.
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

def run_cash_and_carry_calculations():
    conn = sqlite3.connect(DB_PATH)
    
    # 1. Загружаем все положительные ставки фандинга
    df = pl.read_database("""
        SELECT ts, exchange, symbol, funding_rate 
        FROM funding_history 
        WHERE funding_rate >= 0.0008
    """, conn)
    
    # Нормализуем по базовой монете и 8-часовым корзинам
    df = df.with_columns([
        pl.col("symbol").str.split("/").list.get(0).alias("base_coin"),
        ((pl.col("ts") / 28800).round() * 28800).cast(pl.Int64).alias("ts_bucket"),
        (pl.col("funding_rate") * 100.0).alias("rate_8h_pct")
    ])

    print("=" * 100)
    print("РАСЧЕТ И БЭКТЕСТ СТРАТЕГИИ CASH & CARRY: СПОТ (UNISWAP LONG) + ФЬЮЧЕРС (CEX/PERP DEX SHORT)")
    print("=" * 100)
    print(f"Всего срезов в базе с положительным фандингом (Rate >= 0.08% за 8ч): {df.height}")

    # Фильтрация по кулдауну 8 часов (1 сделка на монету в корзину)
    # Если несколько бирж предлагают высокий фандинг, выбираем биржу с МАКСИМАЛЬНЫМ фандингом
    best_per_bucket = df.group_by(["base_coin", "ts_bucket"]).agg([
        pl.col("rate_8h_pct").max().alias("max_rate_8h_pct"),
        pl.col("exchange").sort_by("rate_8h_pct", descending=True).first().alias("best_exchange")
    ]).sort(["ts_bucket", "max_rate_8h_pct"], descending=[False, True])

    # Фильтруем уникальные точки входа с кулдауном 8ч
    seen = set()
    trades = []
    for r in best_per_bucket.iter_rows(named=True):
        k = (r["base_coin"], r["ts_bucket"])
        if k in seen:
            continue
        seen.add(k)
        trades.append(r)

    print(f"Уникальных системных сделок Cash & Carry (Rate >= 0.08%): {len(trades)}")
    
    # Отдельно выделим сделки с порогом 0.10% и 0.15%
    trades_10 = [t for t in trades if t["max_rate_8h_pct"] >= 0.10]
    trades_15 = [t for t in trades if t["max_rate_8h_pct"] >= 0.15]
    print(f"Из них при пороге Rate >= 0.10%: {len(trades_10)}")
    print(f"Из них при пороге Rate >= 0.15%: {len(trades_15)}")

    DEFAULT_NOTIONAL_USD = 1000.0  # Базовый размер позиции $1,000 ($500 спот + $500 шорт)

    # Модель издержек:
    # 1. Пул Uniswap v3 0.05% (мейджоры, стейблы, ликвидные L1)
    # Круг: Вход Uniswap 0.05% + CEX 0.05% + Выход Uniswap 0.05% + CEX 0.05% + slippage 0.04% = 0.24%
    COST_TIER_1_PCT = 0.24

    # 2. Пул Uniswap v3 0.30% (стандартные альткоины)
    # Круг: Вход Uniswap 0.30% + CEX 0.05% + Выход Uniswap 0.30% + CEX 0.05% + slippage 0.06% = 0.76%
    COST_TIER_2_PCT = 0.76

    # 1. ТАБЛИЦА РАСЧЕТОВ ДЛЯ ПУЛОВ 0.05% (TIER 1 - МЕЙДЖОРЫ И ЛИКВИДНЫЕ ТОКЕНЫ)
    print("\n" + "=" * 100)
    print("КЕЙС А: ПУЛЫ UNISWAP V3 0.05% (Мейджоры, ETH, WBTC, ликвидные L1) — ИЗДЕРЖКИ КРУГА: 0.24%")
    print("=" * 100)
    print(f"{'Горизонт':<22} | {'Порог':<8} | {'Сделок':<7} | {'WinRate':<8} | {'Валовый фанд ($)':<18} | {'Комиссии ($)':<14} | {'ЧИСТЫЙ PnL ($)':<16} | {'APR %':<8}")
    print("-" * 100)
    
    for days, n_periods in [(1, 3), (2, 6), (3, 9), (7, 21), (14, 42)]:
        for threshold, t_list in [(0.08, trades), (0.10, trades_10), (0.15, trades_15)]:
            tot_gross = 0.0
            tot_cost = 0.0
            tot_net = 0.0
            wins = 0
            for t in t_list:
                rate = t["max_rate_8h_pct"]
                gross = DEFAULT_NOTIONAL_USD * (rate * n_periods / 100.0)
                cost = DEFAULT_NOTIONAL_USD * (COST_TIER_1_PCT / 100.0)
                net = gross - cost
                tot_gross += gross
                tot_cost += cost
                tot_net += net
                if net > 0:
                    wins += 1
            wr = (wins / len(t_list)) * 100.0 if t_list else 0.0
            # Расчет годовой доходности APR на капитал $1000
            # за 32 дня выборки
            apr = (tot_net / (DEFAULT_NOTIONAL_USD * len(t_list) if t_list else 1)) * (365.0 / days) * 100.0
            print(f"{days:>2} дн. ({n_periods:>2} выплат)     | >= {threshold:.2f}% | {len(t_list):<7} | {wr:6.1f}% | "
                  f"${tot_gross:>16.2f} | ${tot_cost:>12.2f} | ${tot_net:>+14.2f} | {apr:>6.1f}%")

    # 2. ТАБЛИЦА РАСЧЕТОВ ДЛЯ ПУЛОВ 0.30% (TIER 2 - СТАНДАРТНЫЕ АЛЬТКОИНЫ)
    print("\n" + "=" * 100)
    print("КЕЙС Б: ПУЛЫ UNISWAP V3 0.30% (Стандартные альткоины) — ИЗДЕРЖКИ КРУГА: 0.76%")
    print("=" * 100)
    print(f"{'Горизонт':<22} | {'Порог':<8} | {'Сделок':<7} | {'WinRate':<8} | {'Валовый фанд ($)':<18} | {'Комиссии ($)':<14} | {'ЧИСТЫЙ PnL ($)':<16} | {'APR %':<8}")
    print("-" * 100)
    for days, n_periods in [(2, 6), (3, 9), (7, 21), (14, 42)]:
        for threshold, t_list in [(0.10, trades_10), (0.15, trades_15)]:
            tot_gross = 0.0
            tot_cost = 0.0
            tot_net = 0.0
            wins = 0
            for t in t_list:
                rate = t["max_rate_8h_pct"]
                gross = DEFAULT_NOTIONAL_USD * (rate * n_periods / 100.0)
                cost = DEFAULT_NOTIONAL_USD * (COST_TIER_2_PCT / 100.0)
                net = gross - cost
                tot_gross += gross
                tot_cost += cost
                tot_net += net
                if net > 0:
                    wins += 1
            wr = (wins / len(t_list)) * 100.0 if t_list else 0.0
            apr = (tot_net / (DEFAULT_NOTIONAL_USD * len(t_list) if t_list else 1)) * (365.0 / days) * 100.0
            print(f"{days:>2} дн. ({n_periods:>2} выплат)     | >= {threshold:.2f}% | {len(t_list):<7} | {wr:6.1f}% | "
                  f"${tot_gross:>16.2f} | ${tot_cost:>12.2f} | ${tot_net:>+14.2f} | {apr:>6.1f}%")

    # 3. ТОП МОНЕТ ДЛЯ CASH & CARRY
    print("\n" + "=" * 100)
    print("ТОП МОНЕТ ДЛЯ CASH & CARRY (UNISWAP LONG + CEX/DEX SHORT, 3 ДНЯ УДЕРЖАНИЯ, TIER 1 ПУЛЫ):")
    print("=" * 100)
    print(f"{'Монета':<12} | {'Шорт биржа':<16} | {'Сделок':<6} | {'Ср. ставка 8ч':<15} | {'Пик ставка 8ч':<15} | {'Чистый PnL ($1000)':<18}")
    print("-" * 100)

    coin_stats = defaultdict(lambda: {"pnl": 0.0, "rates": [], "cnt": 0, "best_ex": ""})
    for t in trades_10:
        c = t["base_coin"]
        rate = t["max_rate_8h_pct"]
        gross = DEFAULT_NOTIONAL_USD * (rate * 9 / 100.0)
        cost = DEFAULT_NOTIONAL_USD * (COST_TIER_1_PCT / 100.0)
        net = gross - cost
        coin_stats[c]["pnl"] += net
        coin_stats[c]["rates"].append(rate)
        coin_stats[c]["cnt"] += 1
        coin_stats[c]["best_ex"] = t["best_exchange"]

    sorted_coins = sorted(coin_stats.items(), key=lambda x: x[1]["pnl"], reverse=True)
    for c, d in sorted_coins[:15]:
        avg_r = np.mean(d["rates"])
        max_r = max(d["rates"])
        print(f"{c:<12} | {d['best_ex']:<16} | {d['cnt']:<6} | {avg_r:>13.4f}% | {max_r:>13.4f}% | ${d['pnl']:>+16.2f}")

if __name__ == "__main__":
    run_cash_and_carry_calculations()
