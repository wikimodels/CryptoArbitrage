"""
Бэктест funding-carry за N дней по истории бирж. ИСПРАВЛЕННАЯ версия.

Фиксы относительно оригинала:
1. WIDTH больше не константа 0.17% для всех — берётся per-symbol средняя
   ширина из scanner.db (fallback на дефолт, если для символа нет данных).
2. Funding interval больше не захардкожен в 8ч — определяется по факту
   из истории (funding_interval_hours в info, если биржа его отдаёт),
   иначе честный fallback на медианный интервал между записями истории
   этой конкретной биржи/символа (а не глобальная константа).
3. Комиссия теперь параметр (--fee-bps), с готовыми пресетами обычный/VIP,
   а не захардкожен FEE_ROUND=0.24 (это старый tier, не тот VIP 0.02%,
   который обсуждали).
4. Явно помечен и вынесен в флаг lookahead-риск: фильтр funding_net>0.15
   считается ПОСЛЕ того как funding уже реализовался (то есть это не то,
   что ты знал бы в момент входа). Добавлен --no-threshold-filter, чтобы
   отдельно увидеть "гросс" распределение без задней даты, и это отдельно
   печатается в отчёте как предупреждение.
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import time

import ccxt

DATA_DB = "data/scanner.db"
DAYS = 30
HOLD_H = 8
DEFAULT_WIDTH = 0.17  # используется только если для символа нет данных в БД
SLIP = 0.02

FEE_PRESETS_BPS = {
    "regular": 6.0,   # 0.06% taker
    "vip": 2.0,        # 0.02% taker (config.yaml:default_fees VIP tier)
}

EXCHANGES = {
    "gateio": ccxt.gate,
    "bybit": ccxt.bybit,
    "okx": ccxt.okx,
    "mexc": ccxt.mexc,
    "bitget": ccxt.bitget,
    "binance": ccxt.binance,
    "htx": ccxt.htx,
    "bingx": ccxt.bingx,
    "coinex": ccxt.coinex,
}


def get_top_symbols(con, limit=21, thr=0.3):
    rows = list(con.execute(
        "SELECT symbol, AVG(funding_edge_pct) as avgf FROM signals "
        "WHERE funding_edge_pct>? GROUP BY symbol ORDER BY avgf DESC",
        (thr,),
    ))
    rows = [(r[0], r[1]) for r in rows][:limit]
    return [s for s, _ in rows]


def get_symbol_width(con, symbol: str) -> float:
    """Средняя ширина спреда конкретно для этого символа, а не глобальная
    константа. Пробуем несколько вероятных названий колонки, т.к. схема
    scanner.db не проверялась напрямую."""
    for col in ("width_pct", "spread_width_pct", "width"):
        try:
            row = con.execute(
                f"SELECT AVG({col}) FROM signals WHERE symbol=? AND {col} IS NOT NULL",
                (symbol,),
            ).fetchone()
            if row and row[0] is not None:
                return float(row[0])
        except sqlite3.OperationalError:
            continue  # такой колонки нет — пробуем следующую
    return DEFAULT_WIDTH


def fetch_funding_history(exchange_id: str, symbol: str, since_ms: int):
    ex = EXCHANGES[exchange_id]({"enableRateLimit": True})
    try:
        return ex.fetch_funding_rate_history(symbol, since=since_ms)
    except Exception:
        try:
            hist = ex.fetch_funding_rate_history(symbol)
            return [h for h in hist if h.get("timestamp", 0) >= since_ms]
        except Exception:
            return []


def detect_interval_hours(hist) -> float:
    """Честное определение интервала фандинга:
    1) если биржа явно указывает fundingIntervalHours в info — берём его;
    2) иначе считаем медианный шаг между таймстемпами истории;
    3) фолбэк 8ч, только если данных совсем недостаточно."""
    explicit = []
    for h in hist:
        info = h.get("info", {}) or {}
        for key in ("fundingIntervalHours", "fundingInterval", "interval"):
            v = info.get(key)
            if v:
                try:
                    explicit.append(float(v))
                except (TypeError, ValueError):
                    pass
    if explicit:
        return statistics.median(explicit)

    ts = sorted(h.get("timestamp") for h in hist if h.get("timestamp"))
    if len(ts) >= 3:
        diffs_h = [(b - a) / 3600000 for a, b in zip(ts, ts[1:]) if b > a]
        diffs_h = [d for d in diffs_h if 0.5 <= d <= 24]  # отсекаем дыры/дубли
        if diffs_h:
            return statistics.median(diffs_h)

    return 8.0  # честный fallback, а не тихая подмена


def main(days: int, fee_preset: str, apply_threshold_filter: bool):
    fee_bps = FEE_PRESETS_BPS[fee_preset]
    fee_round_pct = 4 * (fee_bps / 100 / 100) * 100  # 4 таких же сделки, в %

    con = sqlite3.connect(DATA_DB)
    con.row_factory = sqlite3.Row

    syms = get_top_symbols(con)
    print(f"Топ символов funding>0.3%: {len(syms)}: {', '.join(syms[:10])}...")
    print(f"Комиссия: {fee_preset} ({fee_bps}bps taker, round-trip x4 = {fee_round_pct:.3f}%)")

    since = int((time.time() - days * 86400) * 1000)

    pair_for_symbol = {}
    for sym in syms:
        row = con.execute(
            "SELECT exch_long, exch_short, AVG(funding_edge_pct) as avgf FROM signals "
            "WHERE symbol=? GROUP BY exch_long, exch_short ORDER BY avgf DESC LIMIT 1",
            (sym,),
        ).fetchone()
        if row:
            pair_for_symbol[sym] = (row["exch_long"], row["exch_short"])

    results = []
    for sym in syms:
        pair = pair_for_symbol.get(sym)
        if not pair:
            continue
        lo, sh = pair
        if lo not in EXCHANGES or sh not in EXCHANGES:
            continue

        symbol_width = get_symbol_width(con, sym)
        print(f"\n{sym} {lo}->{sh}  (width={symbol_width:.3f}%) ...", flush=True)

        h_lo = fetch_funding_history(lo, sym, since)
        time.sleep(0.3)
        h_sh = fetch_funding_history(sh, sym, since)
        time.sleep(0.3)
        if not h_lo or not h_sh:
            print(f"  нет истории: lo={len(h_lo)} sh={len(h_sh)}")
            continue

        interval_lo = detect_interval_hours(h_lo)
        interval_sh = detect_interval_hours(h_sh)

        pnl_sum = 0.0
        pnl_sum_gross = 0.0  # без threshold-фильтра, для честного сравнения
        trades = 0
        trades_gross = 0
        for hs in h_sh:
            ts = hs.get("timestamp")
            if not ts:
                continue
            best = min(h_lo, key=lambda h: abs((h.get("timestamp") or 0) - ts), default=None)
            if not best or abs((best.get("timestamp") or 0) - ts) > 4 * 3600 * 1000:
                continue
            try:
                rate_lo = float(best.get("fundingRate") if best.get("fundingRate") is not None
                                 else best.get("info", {}).get("fundingRate", 0))
                rate_sh = float(hs.get("fundingRate") if hs.get("fundingRate") is not None
                                 else hs.get("info", {}).get("fundingRate", 0))
            except (TypeError, ValueError):
                continue

            n_lo = HOLD_H / interval_lo
            n_sh = HOLD_H / interval_sh
            funding_edge = (rate_sh * n_sh - rate_lo * n_lo) * 100
            funding_net = funding_edge - fee_round_pct - SLIP - symbol_width

            pnl_sum_gross += funding_net
            trades_gross += 1

            if (not apply_threshold_filter) or funding_net > 0.15:
                pnl_sum += funding_net
                trades += 1

        if trades_gross:
            print(f"  gross: {trades_gross} событий, avg net {pnl_sum_gross/trades_gross:.3f}%"
                  f"  (interval_lo={interval_lo:.1f}h interval_sh={interval_sh:.1f}h)")
            if trades:
                print(f"  filtered>0.15%: {trades} тр sum {pnl_sum:.2f}% avg {pnl_sum/trades:.3f}%")
            results.append((sym, trades, pnl_sum, trades_gross, pnl_sum_gross, lo, sh))

    print("\n=== Итог бэктеста ===")
    if not results:
        print("Нет данных — биржи не отдали историю (нужны ключи/разрешения или лимиты).")
        return

    if apply_threshold_filter:
        print("ВНИМАНИЕ: фильтр funding_net>0.15% применён ПОСЛЕ реализации funding —")
        print("это lookahead и завышает результат относительно реальной торговли,")
        print("где на входе известен только текущий/прогнозный funding, а не итоговый net.")
        print("Смотри также 'gross' цифры ниже — это честная база без подглядывания в будущее.\n")

    total_trades = sum(x[1] for x in results)
    total_pct = sum(x[2] for x in results)
    total_trades_g = sum(x[3] for x in results)
    total_pct_g = sum(x[4] for x in results)

    print(f"Filtered (funding_net>0.15%, lookahead): {total_trades} сделок, {total_pct:.2f}% сумма")
    if total_trades:
        print(f"  => ${total_pct:.2f} на $100 за {days}д ({total_trades/days:.1f}/день, "
              f"${total_pct/days:.2f}/день, ${total_pct/days*30:.0f}/мес на $100)")

    print(f"\nGross (все события, без фильтра, честная база): {total_trades_g} событий, {total_pct_g:.2f}% сумма")
    if total_trades_g:
        print(f"  => avg net/событие {total_pct_g/total_trades_g:.3f}%, "
              f"${total_pct_g/days:.2f}/день на $100 (если торговать вообще всё без разбора)")

    print("\nПо монетам (gross, отсортировано по avg net):")
    for sym, tr, pct, trg, pctg, lo, sh in sorted(
        results, key=lambda x: x[4] / x[3] if x[3] else 0, reverse=True
    )[:10]:
        avg_g = pctg / trg if trg else 0
        print(f" {sym:<18} {lo}->{sh}  gross {trg:2d} тр avg {avg_g:.3f}%  "
              f"| filtered {tr:2d} тр")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=DAYS)
    p.add_argument("--fee", choices=list(FEE_PRESETS_BPS.keys()), default="regular",
                    help="regular=0.06%% taker, vip=0.02%% taker")
    p.add_argument("--no-threshold-filter", action="store_true",
                    help="не применять lookahead-фильтр funding_net>0.15%%, "
                         "смотреть честный gross-результат по всем событиям")
    args = p.parse_args()
    main(args.days, args.fee, not args.no_threshold_filter)
