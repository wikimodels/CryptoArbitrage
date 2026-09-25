"""
Скрипт сравнительного анализа эффективности стратегии Z-Score:
Сравнение реальных входов (|Z| >= 3.5) с гипотезой входов только при экстремальной сигме (|Z| >= 4.0).
"""
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import sqlite3
from pathlib import Path

DB_PATH = Path("data/scanner.db")


def main():
    if not DB_PATH.exists():
        print(f"База данных не найдена: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    query = """
        SELECT trade_id, symbol, exch_long, exch_short, open_ts, close_ts,
               size_usdt, realized_pnl_usdt, fees_usdt, holding_seconds,
               pnl_pct, z_in, reason
        FROM emulator_trades
        WHERE strategy = 'arb' AND status = 'closed'
        ORDER BY close_ts ASC
    """
    try:
        rows = c.execute(query).fetchall()
    except Exception as e:
        print(f"Ошибка чтения сделок: {e}")
        return

    if not rows:
        print("В базе пока нет закрытых арбитражных сделок.")
        return

    all_trades = []
    z4_trades = []
    z35_trades = []

    for r in rows:
        trade = {
            "id": r[0], "symbol": r[1], "long": r[2], "short": r[3],
            "open_ts": r[4], "close_ts": r[5], "size": float(r[6] or 0),
            "pnl": float(r[7] or 0), "fees": float(r[8] or 0),
            "holding": float(r[9] or 0), "pnl_pct": float(r[10] or 0),
            "z_in": float(r[11] or 0), "reason": r[12],
        }
        all_trades.append(trade)
        if abs(trade["z_in"]) >= 4.0:
            z4_trades.append(trade)
        else:
            z35_trades.append(trade)

    def calc_metrics(trades: list[dict]) -> dict:
        n = len(trades)
        if n == 0:
            return {"n": 0, "wins": 0, "losses": 0, "wr": 0.0, "pnl": 0.0,
                    "fees": 0.0, "avg_pnl": 0.0, "avg_hold": 0.0}
        wins = sum(1 for t in trades if t["pnl"] >= 0)
        losses = n - wins
        wr = (wins / n) * 100.0
        tot_pnl = sum(t["pnl"] for t in trades)
        tot_fees = sum(t["fees"] for t in trades)
        avg_pnl = tot_pnl / n
        avg_hold = sum(t["holding"] for t in trades) / n
        return {
            "n": n, "wins": wins, "losses": losses, "wr": wr,
            "pnl": tot_pnl, "fees": tot_fees, "avg_pnl": avg_pnl, "avg_hold": avg_hold,
        }

    m_all = calc_metrics(all_trades)
    m_z4 = calc_metrics(z4_trades)
    m_z35 = calc_metrics(z35_trades)

    w_l_all = f"{m_all['wins']}/{m_all['losses']}"
    w_l_z4 = f"{m_z4['wins']}/{m_z4['losses']}"
    w_l_z35 = f"{m_z35['wins']}/{m_z35['losses']}"

    wr_all = f"{m_all['wr']:.1f}%"
    wr_z4 = f"{m_z4['wr']:.1f}%"
    wr_z35 = f"{m_z35['wr']:.1f}%"

    pnl_all = f"{m_all['pnl']:+.4f} $"
    pnl_z4 = f"{m_z4['pnl']:+.4f} $"
    pnl_z35 = f"{m_z35['pnl']:+.4f} $"

    fees_all = f"-{m_all['fees']:.4f} $"
    fees_z4 = f"-{m_z4['fees']:.4f} $"
    fees_z35 = f"-{m_z35['fees']:.4f} $"

    avg_all = f"{m_all['avg_pnl']:+.4f} $"
    avg_z4 = f"{m_z4['avg_pnl']:+.4f} $"
    avg_z35 = f"{m_z35['avg_pnl']:+.4f} $"

    hold_all = f"{m_all['avg_hold']:.1f} сек"
    hold_z4 = f"{m_z4['avg_hold']:.1f} сек"
    hold_z35 = f"{m_z35['avg_hold']:.1f} сек"

    print("\n" + "=" * 78)
    print("СРАВНИТЕЛЬНЫЙ АНАЛИЗ: ВХОД ПРИ |Z| >= 3.5 ПРОТИВ ГИПОТЕЗЫ |Z| >= 4.0")
    print("=" * 78)
    print(f"{'Метрика':<28} | {'Все (|Z| >= 3.5)':<15} | {'Только |Z| >= 4.0':<16} | {'Дельта (3.5..4.0)':<16}")
    print("-" * 78)
    print(f"{'Всего сделок':<28} | {str(m_all['n']):<15} | {str(m_z4['n']):<16} | {str(m_z35['n']):<16}")
    print(f"{'Прибыльных / Убыточных':<28} | {w_l_all:<15} | {w_l_z4:<16} | {w_l_z35:<16}")
    print(f"{'Винрейт (Win Rate)':<28} | {wr_all:<15} | {wr_z4:<16} | {wr_z35:<16}")
    print(f"{'Чистый PnL (USDT)':<28} | {pnl_all:<15} | {pnl_z4:<16} | {pnl_z35:<16}")
    print(f"{'Комиссии бирж Σ (USDT)':<28} | {fees_all:<15} | {fees_z4:<16} | {fees_z35:<16}")
    print(f"{'Средний PnL / сделка':<28} | {avg_all:<15} | {avg_z4:<16} | {avg_z35:<16}")
    print(f"{'Ср. время удержания':<28} | {hold_all:<15} | {hold_z4:<16} | {hold_z35:<16}")
    print("=" * 78)

    print("\nСПИСОК ВСЕХ СДЕЛОК С РАЗБИВКОЙ ПО СИГМЕ:")
    print("-" * 78)
    for idx, t in enumerate(all_trades, 1):
        tier = "Z >= 4.0 [ГИПОТЕЗА]" if abs(t["z_in"]) >= 4.0 else "3.5 <= Z < 4.0"
        pnl_str = f"{t['pnl']:+.4f} $"
        print(f"{idx:2d}. {t['symbol']:<20} {t['long']}->{t['short']} | Z_in={t['z_in']:+.2f} ({tier:<18}) | PnL={pnl_str:<10} | {t['holding']:.0f}с ({t['reason']})")
    print("-" * 78 + "\n")


if __name__ == "__main__":
    main()
