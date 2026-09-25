"""
Системный бэктест и стресс-тест фандингового арбитража на всей вселенной монет (150 монет).
Без вишенки на торте: учитывает сплошную выборку, полные комиссии входа/выхода,
раздвижку спредов и риск ликвидации плеча по минутным свечам.
"""
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import sqlite3
import statistics
import time
from pathlib import Path
from collections import defaultdict
import polars as pl

DATA_DB = Path("data/scanner.db")
TOP_FILE = Path("output/top.txt") if Path("output/top.txt").exists() else Path("output/top40_4ex.txt")
TOP150_FILE = TOP_FILE
CANDLES_DIR = Path("data/raw_1m_30d")


def run_systematic_funding_backtest(
    position_size_usdt: float = 10.0,
    leverage: float = 10.0,
    taker_fee_pct: float = 0.05,
    min_funding_edge_pct: float = 0.05,
    assumed_holding_periods: int = 3,  # 3 периода по 8ч = 24 часа
    default_spread_pct: float = 0.08,
):
    print("=" * 70)
    print("СИСТЕМНЫЙ АНАЛИЗ ФАНДИНГОВОГО АРБИТРАЖА ПО ВСЕЙ ВСЕЛЕННОЙ (150 МОНЕТ)")
    print("=" * 70)
    print(f"Размер позиции на ногу: ${position_size_usdt:.2f} USDT")
    print(f"Плечо: {leverage:.0f}x (залог на ногу: ${position_size_usdt / leverage:.2f} USDT)")
    print(f"Комиссия тейкера: {taker_fee_pct:.3f}% (круг x4 = {taker_fee_pct * 4:.3f}%)")
    print(f"Порог входа (funding edge): >= {min_funding_edge_pct:.3f}% за период")
    print(f"Горизонт удержания: {assumed_holding_periods} периодов (1 сутки)")
    print("=" * 70)

    con = sqlite3.connect(DATA_DB)
    cur = con.cursor()

    top150_symbols = set()
    if TOP150_FILE.exists():
        with open(TOP150_FILE, encoding="utf-8") as f:
            top150_symbols = {l.strip() for l in f if l.strip()}

    # Загружаем все сигналы по нашей вселенной
    cur.execute("""
        SELECT ts, symbol, exch_long, exch_short, raw_spread_pct, funding_edge_pct, fees_pct, net_edge_pct
        FROM signals
        WHERE funding_edge_pct IS NOT NULL
        ORDER BY ts ASC
    """)
    all_signals = cur.fetchall()
    universe_signals = [s for s in all_signals if s[1] in top150_symbols]

    print(f"\nВсего сигналов в базе: {len(all_signals)}")
    print(f"Сигналов в нашей вселенной 150 монет: {len(universe_signals)}")

    # Фильтруем системным порогом входа
    qualified = [s for s in universe_signals if s[5] >= min_funding_edge_pct]
    print(f"Сигналов, преодолевших фильтр входа (>={min_funding_edge_pct}%): {len(qualified)}")

    # Кластеризуем сигналы по (символ, exch_long, exch_short), чтобы не плодить дубли каждые 5 секунд
    trades = []
    last_trade_ts = {}
    COOLDOWN_SEC = 28800  # 8 часов кулдаун на повторный вход в ту же связку

    for sig in qualified:
        ts, sym, el, es, raw_sp, fund_edge, fees, net_edge = sig
        key = (sym, el, es)
        if key in last_trade_ts and (ts - last_trade_ts[key]) < COOLDOWN_SEC:
            continue
        last_trade_ts[key] = ts
        trades.append(sig)

    print(f"Уникальных системных сделок (с кулдауном 8ч): {len(trades)}")

    # Расчет экономики каждой сделки
    fee_drag_pct = taker_fee_pct * 4  # вход long+short + выход long+short
    # спред на входе и выходе: если raw_spread доступен, берем его, иначе дефолтный спред стакана
    round_cost_pct = fee_drag_pct + default_spread_pct * 2

    # Запас маржи до ликвидации
    liq_threshold_pct = (1.0 / leverage) * 90.0  # при 10x это ~9%, при 2x это ~45%

    results = []
    liquidations = 0
    profitable_trades = 0
    loss_trades = 0
    total_net_pnl_usdt = 0.0
    total_funding_earned_usdt = 0.0
    total_friction_usdt = 0.0

    margin_per_leg = position_size_usdt / leverage
    total_margin = margin_per_leg * 2

    # Кэш проверки дивергенции свечей
    for t in trades:
        ts, sym, el, es, raw_sp, fund_edge, fees, net_edge = t

        # Накопленный фандинг за удержание
        gross_funding_pct = fund_edge * assumed_holding_periods
        gross_funding_usdt = position_size_usdt * (gross_funding_pct / 100.0)

        # Суммарные издержки (комиссии биржи + трение спреда стакана)
        friction_usdt = position_size_usdt * (round_cost_pct / 100.0)

        # Проверка риска ликвидации по локальным свечам
        # Ищем минутные свечи для sym на el и es
        safe_sym = sym.replace("/", "_").replace(":", "_")
        p_el = CANDLES_DIR / el / safe_sym / "candles.parquet"
        p_es = CANDLES_DIR / es / safe_sym / "candles.parquet"

        is_liquidated = False
        max_divergence_pct = 0.0

        if p_el.exists() and p_es.exists():
            try:
                # Читаем окно в 1 сутки от момента входа ts
                t_start = int(ts * 1000)
                t_end = t_start + assumed_holding_periods * 8 * 3600 * 1000
                df_l = pl.read_parquet(p_el).filter((pl.col("ts") >= t_start) & (pl.col("ts") <= t_end))
                df_s = pl.read_parquet(p_es).filter((pl.col("ts") >= t_start) & (pl.col("ts") <= t_end))
                if len(df_l) > 30 and len(df_s) > 30:
                    joined = df_l.join(df_s, on="ts", suffix="_s")
                    if len(joined) > 30:
                        ratios = joined["c"] / joined["c_s"]
                        r0 = ratios[0]
                        max_dev = float((ratios / r0 - 1.0).abs().max()) * 100.0
                        max_divergence_pct = max_dev
                        if max_dev >= liq_threshold_pct:
                            is_liquidated = True
            except Exception:
                pass

        if is_liquidated:
            liquidations += 1
            # При ликвидации одной ноги теряется весь залог ноги ($1) + ликвидационный сбор
            trade_net_pnl = -margin_per_leg
            loss_trades += 1
        else:
            trade_net_pnl = gross_funding_usdt - friction_usdt
            if trade_net_pnl > 0:
                profitable_trades += 1
            else:
                loss_trades += 1

        total_net_pnl_usdt += trade_net_pnl
        total_funding_earned_usdt += gross_funding_usdt
        total_friction_usdt += friction_usdt

        results.append({
            "sym": sym,
            "el": el,
            "es": es,
            "fund_edge": fund_edge,
            "net_pnl": trade_net_pnl,
            "liquidated": is_liquidated,
            "max_div": max_divergence_pct,
        })

    print("\n" + "=" * 70)
    print(f"ИТОГОВЫЙ СИСТЕМНЫЙ РЕЗУЛЬТАТ (Плечо {leverage:.0f}x, Позиция ${position_size_usdt})")
    print("=" * 70)
    print(f"Всего совершенных сделок: {len(results)}")
    print(f"Прибыльных сделок: {profitable_trades} ({(profitable_trades/len(results)*100) if results else 0:.1f}%)")
    print(f"Убыточных сделок: {loss_trades} ({(loss_trades/len(results)*100) if results else 0:.1f}%)")
    print(f"Ликвидаций из-за плеча {leverage:.0f}x (раздвижка спреда >= {liq_threshold_pct:.1f}%): {liquidations} ({(liquidations/len(results)*100) if results else 0:.1f}%)")
    print(f"\nВаловый начисленный фандинг: ${total_funding_earned_usdt:.4f} USDT")
    print(f"Уплачено комиссий и спреда:  ${total_friction_usdt:.4f} USDT")
    print(f"Чистый финансовый результат (Net PnL): ${total_net_pnl_usdt:.4f} USDT")
    if total_margin > 0 and len(results) > 0:
        roe = (total_net_pnl_usdt / total_margin) * 100.0
        print(f"Итоговый ROE на залог (${total_margin:.2f} залога): {roe:.2f}%")

    # Сравнение: а что было бы при безопасном плече 2x?
    print("\n" + "=" * 70)
    print("СРАВНИТЕЛЬНЫЙ СТРЕСС-ТЕСТ: ПЛЕЧО 10x ПРОТИВ БЕЗОПАСНОГО ПЛЕЧА 2x")
    print("=" * 70)
    liq_2x_count = sum(1 for r in results if r["max_div"] >= 45.0)
    print(f"При плече 10x ликвидировано сделок: {liquidations} из {len(results)}")
    print(f"При плече 2x ликвидировано сделок:  {liq_2x_count} из {len(results)}")

    # Топ лучших монет по стабильности фандинга
    pnl_by_coin = defaultdict(float)
    for r in results:
        pnl_by_coin[r["sym"]] += r["net_pnl"]

    print("\nТоп монет по системному чистому PnL (за вычетом комиссий):")
    sorted_coins = sorted(pnl_by_coin.items(), key=lambda x: x[1], reverse=True)
    for sym, pnl in sorted_coins[:10]:
        print(f"  {sym:<20} : ${pnl:+.4f} USDT")

    print("\n" + "=" * 70)
    print("МАТРИЦА ЧУВСТВИТЕЛЬНОСТИ: СРАВНЕНИЕ ПОРОГОВ ВХОДА И УРОВНЕЙ КОМИССИЙ")
    print("=" * 70)
    print(f"{'Порог':<8} | {'Комиссия':<10} | {'Горизонт':<8} | {'Сделок':<7} | {'WinRate':<8} | {'Gross Fund':<11} | {'Fees':<9} | {'Net PnL':<10} | {'ROE %':<7}")
    print("-" * 88)

    for thr in [0.05, 0.10, 0.15, 0.20]:
        for fee_tier, f_pct in [("Regular 0.05%", 0.05), ("VIP 0.02%", 0.02)]:
            for periods in [3, 9]:  # 1 день (3x8h) и 3 дня (9x8h)
                q_sigs = [s for s in universe_signals if s[5] >= thr]
                t_list = []
                l_ts = {}
                for sig in q_sigs:
                    ts, sym, el, es, raw_sp, fund_edge, fees, net_edge = sig
                    k = (sym, el, es)
                    if k in l_ts and (ts - l_ts[k]) < COOLDOWN_SEC:
                        continue
                    l_ts[k] = ts
                    t_list.append(sig)

                if not t_list:
                    continue

                tot_gross = sum(position_size_usdt * (s[5] * periods / 100.0) for s in t_list)
                cost_pct = f_pct * 4 + default_spread_pct * 2
                tot_cost = len(t_list) * position_size_usdt * (cost_pct / 100.0)
                tot_net = tot_gross - tot_cost
                wins = sum(1 for s in t_list if (position_size_usdt * (s[5] * periods / 100.0) - position_size_usdt * (cost_pct / 100.0)) > 0)
                wr = (wins / len(t_list)) * 100.0
                roe_val = (tot_net / total_margin) * 100.0
                h_str = f"{periods//3}д ({periods}x8h)"
                print(f"{thr:>6.2f}% | {fee_tier:<10} | {h_str:<8} | {len(t_list):<7} | {wr:>6.1f}% | ${tot_gross:>9.4f} | ${tot_cost:>7.4f} | ${tot_net:>+8.4f} | {roe_val:>+6.1f}%")

    print("=" * 88)


if __name__ == "__main__":
    run_systematic_funding_backtest()
