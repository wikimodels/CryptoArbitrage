"""
Анализ межрыночного арбитража фандинга между DEX (dYdX, Aster, Hyperliquid) и CEX (Binance, Bybit, Bitget, OKX, MEXC)
на основе выкачанных 139,284 записей истории фандинга в funding_history.
"""
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import sqlite3
from pathlib import Path
from collections import defaultdict
import numpy as np

DB_PATH = Path("data/scanner.db")

def analyze_cross_dex():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Проверим количество записей по каждой бирже
    cur.execute("SELECT exchange, COUNT(*), COUNT(DISTINCT symbol) FROM funding_history GROUP BY exchange")
    counts = cur.fetchall()
    print("=" * 80)
    print("ВЫГРУЖЕННАЯ БАЗА ФАНДИНГА ПО ВСЕМ БИРЖАМ (CEX + DEX)")
    print("=" * 80)
    for ex, cnt, n_sym in counts:
        print(f"Биржа: {ex:<12} | Записей: {cnt:>6} | Уникальных рынков/монет: {n_sym:>4}")

    # Найдем общие монеты между DEX и CEX
    cur.execute("""
        SELECT a.symbol, a.exchange, b.exchange, 
               AVG(ABS(a.funding_rate - b.funding_rate)) * 100 as avg_delta_pct,
               MAX(ABS(a.funding_rate - b.funding_rate)) * 100 as max_delta_pct,
               COUNT(*) as aligned_points
        FROM funding_history a
        JOIN funding_history b 
          ON a.symbol = b.symbol 
         AND a.exchange < b.exchange
         AND ABS(a.ts - b.ts) <= 1800  -- совпадение по времени в пределах 30 мин
        WHERE (a.exchange IN ('dydx', 'aster', 'hyperliquid') OR b.exchange IN ('dydx', 'aster', 'hyperliquid'))
        GROUP BY a.symbol, a.exchange, b.exchange
        HAVING aligned_points >= 5 AND max_delta_pct >= 0.08
        ORDER BY max_delta_pct DESC
    """)
    rows = cur.fetchall()

    print("\n" + "=" * 90)
    print(f"ТОП СВЯЗОК С УЧАСТИЕМ DEX (dYdX, Aster, Hyperliquid) С АНОМАЛЬНОЙ ДЕЛЬТОЙ ФАНДИНГА (всего {len(rows)} связок)")
    print("=" * 90)
    print(f"{'Монета':<18} | {'Пара бирж':<26} | {'Срезов':<7} | {'Средняя Δ %':<12} | {'Макс. Δ %':<12}")
    print("-" * 90)
    for r in rows[:25]:
        sym, e1, e2, avg_d, max_d, n = r
        pair_str = f"{e1} <-> {e2}"
        print(f"{sym:<18} | {pair_str:<26} | {n:<7} | {avg_d:>10.4f}% | {max_d:>10.4f}%")

if __name__ == "__main__":
    analyze_cross_dex()
