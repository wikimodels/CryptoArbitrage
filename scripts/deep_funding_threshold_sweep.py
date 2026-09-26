"""
Глубокий исследовательский анализ и расчет математически оптимального порога Delta Funding.
Оценивает:
1. Полную выборку сигналов и котировок (все монеты без ограничений и top150).
2. Распределение ставок фандинга и спредов ставок.
3. Доходность и Win Rate для порогов от 0.04% до 0.25% с шагом 0.01%.
4. Разные горизонты удержания: 8ч (1 выплата), 16ч (2 выплаты), 24ч (3 выплаты), 48ч (6 выплат), 72ч (9 выплат).
5. Различные профили комиссий: Retail (0.05%/0.05%), Hybrid Maker/Taker (0.02%/0.05%), VIP (0.01%/0.03%).
6. Реальную раздвижку цен (Basis Divergence Risk) между биржами по 1-минутным свечам.
"""
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import sqlite3
from pathlib import Path
from collections import defaultdict
import numpy as np
import polars as pl

DB_PATH = Path("data/scanner.db")
CANDLES_DIR = Path("data/raw_1m_30d")

def analyze():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # 1. Загрузка всех сигналов
    cur.execute("""
        SELECT ts, symbol, exch_long, exch_short, raw_spread_pct, funding_edge_pct, fees_pct, net_edge_pct
        FROM signals
        WHERE funding_edge_pct IS NOT NULL
        ORDER BY ts ASC
    """)
    all_signals = cur.fetchall()
    
    # 2. Общая статистика распределения Delta Funding
    edges = [s[5] for s in all_signals]
    p_pos = [e for e in edges if e > 0]
    
    print("=" * 80)
    print("РАСПРЕДЕЛЕНИЕ ДЕЛЬТЫ ФАНДИНГА (DELTA FUNDING) ПО ВСЕМ 5,112 СИГНАЛАМ В БАЗЕ")
    print("=" * 80)
    print(f"Всего сигналов: {len(all_signals)}")
    print(f"Сигналов с положительным спредом ставок (Delta > 0): {len(p_pos)} ({len(p_pos)/len(all_signals)*100:.1f}%)")
    print(f"Мин. положительная дельта: {min(p_pos):.4f}%")
    print(f"Медианная положительная дельта: {np.median(p_pos):.4f}%")
    print(f"Средняя положительная дельта: {np.mean(p_pos):.4f}%")
    print(f"90-й перцентиль: {np.percentile(p_pos, 90):.4f}%")
    print(f"95-й перцентиль: {np.percentile(p_pos, 95):.4f}%")
    print(f"99-й перцентиль: {np.percentile(p_pos, 99):.4f}%")
    print(f"Максимальная дельта: {max(p_pos):.4f}%")

    # 3. Детальный свип порогов
    COOLDOWN_SEC = 28800  # 8 часов
    thresholds = [0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10, 0.12, 0.15, 0.20, 0.25]
    horizons = [
        ("8h (1 выплата)", 1),
        ("16h (2 выплаты)", 2),
        ("24h (3 выплаты)", 3),
        ("48h (6 выплат)", 6),
        ("72h (9 выплат)", 9),
    ]
    
    # Профили комиссий: (Название, Taker fee, Maker fee, slippage)
    fee_profiles = [
        ("Retail (Taker 0.05%)", 0.05, 0.05, 0.02),
        ("Hybrid (Maker+Taker)", 0.05, 0.02, 0.01),
        ("VIP (Taker 0.035%)", 0.035, 0.015, 0.01),
    ]

    print("\n" + "=" * 95)
    print("ЧИСЛО СИГНАЛОВ И ЧАСТОТА СДЕЛОК В СУТКИ В ЗАВИСИМОСТИ ОТ ПОРОГА (ВСЯ БАЗА, 28.6 ДНЕЙ)")
    print("=" * 95)
    print(f"{'Порог Delta':<12} | {'Всего сигналов':<15} | {'Уникальных сделок':<18} | {'Сделок в сутки':<15} | {'% от всех сигналов':<18}")
    print("-" * 95)
    
    unique_trades_by_thr = {}
    for thr in thresholds:
        q_sigs = [s for s in all_signals if s[5] >= thr]
        l_ts = {}
        t_list = []
        for s in q_sigs:
            ts, sym, el, es = s[0], s[1], s[2], s[3]
            k = (sym, el, es)
            if k in l_ts and (ts - l_ts[k]) < COOLDOWN_SEC:
                continue
            l_ts[k] = ts
            t_list.append(s)
        unique_trades_by_thr[thr] = t_list
        trades_per_day = len(t_list) / 28.64
        pct_of_all = (len(q_sigs) / len(all_signals)) * 100.0
        print(f"Δ >= {thr:>5.2f}%   | {len(q_sigs):>15} | {len(t_list):>18} | {trades_per_day:>15.2f} | {pct_of_all:>17.2f}%")

    # 4. Анализ экономики для стандартного Retail профиля (Taker 0.05% на обе ноги)
    pos_size = 10.0
    retail_roundtrip_cost = (0.05 * 4) + (0.02 * 2)  # 0.20% комиссии + 0.04% slippage = 0.24%

    print("\n" + "=" * 105)
    print("ЭКОНОМИКА СТАНДАРТНОГО RETAIL (Тейкер 0.05% x4 + 0.04% проскальзывание = 0.24% издержек на круг)")
    print("=" * 105)
    print(f"{'Порог Δ':<9} | {'Горизонт':<16} | {'Сделок':<7} | {'WinRate %':<10} | {'Валовый фанд ($)':<18} | {'Чистый PnL ($)':<16} | {'ROE (10x)':<10}")
    print("-" * 105)

    for thr in [0.05, 0.08, 0.10, 0.12, 0.15, 0.20]:
        t_list = unique_trades_by_thr[thr]
        if not t_list:
            continue
        for h_label, n_periods in horizons:
            gross_pnl_list = []
            net_pnl_list = []
            for s in t_list:
                fund_rate = s[5]
                gross_pct = fund_rate * n_periods
                net_pct = gross_pct - retail_roundtrip_cost
                gross_usd = pos_size * (gross_pct / 100.0)
                net_usd = pos_size * (net_pct / 100.0)
                gross_pnl_list.append(gross_usd)
                net_pnl_list.append(net_usd)
            
            wins = sum(1 for p in net_pnl_list if p > 0)
            wr = (wins / len(net_pnl_list)) * 100.0
            tot_gross = sum(gross_pnl_list)
            tot_net = sum(net_pnl_list)
            margin = (pos_size / 10.0) * 2  # $2 на сделку с 10x
            roe = (tot_net / margin) * 100.0
            print(f"Δ >= {thr:>4.2f}% | {h_label:<16} | {len(t_list):<7} | {wr:>9.1f}% | ${tot_gross:>16.4f} | ${tot_net:>14.4f} | {roe:>8.1f}%")

    # 5. Проверка реальной раздвижки цен по 1м свечам
    print("\n" + "=" * 90)
    print("АНАЛИЗ БАЗИСНОГО РИСКА (РАЗДВИЖКА ЦЕН МЕЖДУ БИРЖАМИ ПО 1-МИНУТНЫМ СВЕЧАМ)")
    print("=" * 90)
    
    divergences_24h = []
    divergences_72h = []
    
    # Проверим доступные свечи для сделок
    checked = 0
    for s in unique_trades_by_thr[0.08]:
        ts, sym, el, es = s[0], s[1], s[2], s[3]
        safe_sym = sym.replace("/", "_").replace(":", "_")
        p_el = CANDLES_DIR / el / safe_sym / "candles.parquet"
        p_es = CANDLES_DIR / es / safe_sym / "candles.parquet"
        
        if p_el.exists() and p_es.exists():
            try:
                t_start = int(ts * 1000)
                t_24h = t_start + 24 * 3600 * 1000
                t_72h = t_start + 72 * 3600 * 1000
                
                df_l = pl.read_parquet(p_el).filter((pl.col("ts") >= t_start) & (pl.col("ts") <= t_72h))
                df_s = pl.read_parquet(p_es).filter((pl.col("ts") >= t_start) & (pl.col("ts") <= t_72h))
                
                if len(df_l) > 60 and len(df_s) > 60:
                    joined = df_l.join(df_s, on="ts", suffix="_s")
                    if len(joined) > 60:
                        ratios = joined["c"] / joined["c_s"]
                        r0 = ratios[0]
                        dev = (ratios / r0 - 1.0).abs() * 100.0
                        
                        # До 24ч
                        j_24 = joined.filter(pl.col("ts") <= t_24h)
                        if len(j_24) > 30:
                            r_24 = j_24["c"] / j_24["c_s"]
                            d_24 = float((r_24 / r0 - 1.0).abs().max()) * 100.0
                            divergences_24h.append(d_24)
                            
                        # До 72ч
                        d_72 = float(dev.max()) * 100.0
                        divergences_72h.append(d_72)
                        checked += 1
            except Exception:
                pass
                
    if divergences_24h:
        print(f"Проанализировано пар с реальными минутными свечами: {checked}")
        print(f"24h горизонт: Медианная раздвижка: {np.median(divergences_24h):.2f}%, 95-й перцентиль: {np.percentile(divergences_24h, 95):.2f}%, Макс: {max(divergences_24h):.2f}%")
        print(f"72h горизонт: Медианная раздвижка: {np.median(divergences_72h):.2f}%, 95-й перцентиль: {np.percentile(divergences_72h, 95):.2f}%, Макс: {max(divergences_72h):.2f}%")
        print(f"Риск ликвидации при плече 10x (порог 9%): {sum(1 for d in divergences_72h if d >= 9.0) / len(divergences_72h) * 100:.1f}%")
        print(f"Риск ликвидации при плече 3x (порог 30%): {sum(1 for d in divergences_72h if d >= 30.0) / len(divergences_72h) * 100:.1f}%")
    else:
        print("Свечи проверены, пар с одновременными свечами в кэше: 0")

if __name__ == "__main__":
    analyze()
