"""
Walk-Forward Analysis (WFA) для парного межмонетного арбитража.
Моделирует скользящие окна калибровки (In-Sample) и слепой торговли (Out-Of-Sample)
с жестким учетом комиссий 4x taker, проскальзывания и размера позиции $10 (10x плечо).
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import numpy as np
import polars as pl

DATA_DIR = Path("data/raw_1m_30d/bitget")
POSITION_SIZE_USDT = 10.0  # Нотионал $10 ($1 маржи на ногу при 10x плече)
LEVERAGE = 10.0
MARGIN_PER_LEG = POSITION_SIZE_USDT / LEVERAGE  # $1.0 USDT
TOTAL_MARGIN = MARGIN_PER_LEG * 2.0             # $2.0 USDT на связку

TAKER_FEE_RATE = 0.0006  # 0.06% на ногу
SLIPPAGE_RATE = 0.0005   # 0.05% на ногу

# 4 комиссии тейкера на обе ноги (вход и выход) + проскальзывание
ROUNDTRIP_FEE_PCT = (TAKER_FEE_RATE * 4.0) + (SLIPPAGE_RATE * 2.0)  # ~0.34%
FEE_PER_TRADE_USDT = POSITION_SIZE_USDT * ROUNDTRIP_FEE_PCT         # ~$0.034


def load_pair_prices(ca: str, cb: str) -> tuple[np.ndarray, np.ndarray, list[int]]:
    """Загружает синхронные минутные цены закрытия двух монет."""
    fa = DATA_DIR / f"{ca}_USDT_USDT" / "candles.parquet"
    fb = DATA_DIR / f"{cb}_USDT_USDT" / "candles.parquet"
    
    df_a = pl.read_parquet(fa).select(["ts", "c"]).rename({"c": "price_a"})
    df_b = pl.read_parquet(fb).select(["ts", "c"]).rename({"c": "price_b"})
    
    joined = df_a.join(df_b, on="ts", how="inner").sort("ts")
    return (
        joined["price_a"].to_numpy(),
        joined["price_b"].to_numpy(),
        joined["ts"].to_list(),
    )


def simulate_trading(
    pa: np.ndarray,
    pb: np.ndarray,
    alpha: float,
    beta: float,
    entry_z: float,
    exit_z: float = 0.0,
    stop_mult: float = 2.0,
    max_holding_bars: int = 2880,  # 48 часов
    rolling_window: int = 1440,    # 24 часа для Z-Score
) -> dict[str, Any]:
    """Симулирует побарное исполнение стратегии на заданном отрезке."""
    n = len(pa)
    if n < rolling_window + 100:
        return {"trades": 0, "net_pnl": 0.0, "win_rate": 0.0, "profit_factor": 0.0, "max_dd": 0.0}

    # Расчет спреда: S = pa - (beta * pb + alpha)
    spread = pa - (beta * pb + alpha)
    
    trades = []
    in_pos = False
    side = 0  # +1: SHORT A / LONG B, -1: LONG A / SHORT B
    entry_idx = 0
    entry_pa = 0.0
    entry_pb = 0.0
    entry_spread = 0.0

    # Быстрый расчет скользящего среднего и STD
    # Для эффективности используем 24-часовое окно (1440 мин)
    means = np.zeros(n)
    stds = np.zeros(n)

    # Кумулятивные суммы для O(1) rolling mean/std
    c_sum = np.cumsum(np.insert(spread, 0, 0))
    c_sq_sum = np.cumsum(np.insert(spread**2, 0, 0))
    w = rolling_window

    for i in range(w, n):
        m = (c_sum[i] - c_sum[i - w]) / w
        var = ((c_sq_sum[i] - c_sq_sum[i - w]) / w) - (m * m)
        sd = math.sqrt(max(var, 1e-12))
        means[i] = m
        stds[i] = sd

    equity = 0.0
    equity_curve = [0.0]

    for i in range(w, n):
        sd = stds[i]
        if sd <= 1e-8:
            continue
        z = (spread[i] - means[i]) / sd

        if not in_pos:
            if z >= entry_z:
                in_pos = True
                side = 1  # Short A, Long B
                entry_idx = i
                entry_pa = pa[i]
                entry_pb = pb[i]
                entry_spread = spread[i]
            elif z <= -entry_z:
                in_pos = True
                side = -1  # Long A, Short B
                entry_idx = i
                entry_pa = pa[i]
                entry_pb = pb[i]
                entry_spread = spread[i]
        else:
            # Проверка выхода
            holding_bars = i - entry_idx
            close_reason = None

            if side == 1:
                if z <= exit_z:
                    close_reason = "converged"
                elif z >= stop_mult * entry_z:
                    close_reason = "stop_loss"
            elif side == -1:
                if z >= -exit_z:
                    close_reason = "converged"
                elif z <= -stop_mult * entry_z:
                    close_reason = "stop_loss"

            if not close_reason and holding_bars >= max_holding_bars:
                close_reason = "timestop"

            if close_reason:
                # Расчет PnL на $10 размера позиции на каждую ногу
                # Нога A:
                ret_a = (entry_pa - pa[i]) / entry_pa if side == 1 else (pa[i] - entry_pa) / entry_pa
                pnl_a = POSITION_SIZE_USDT * ret_a

                # Нога B:
                ret_b = (pb[i] - entry_pb) / entry_pb if side == 1 else (entry_pb - pb[i]) / entry_pb
                pnl_b = POSITION_SIZE_USDT * ret_b

                gross_pnl = pnl_a + pnl_b
                net_pnl = gross_pnl - FEE_PER_TRADE_USDT

                trades.append({
                    "entry_idx": entry_idx,
                    "exit_idx": i,
                    "holding_hours": round(holding_bars / 60.0, 1),
                    "gross_pnl": gross_pnl,
                    "net_pnl": net_pnl,
                    "reason": close_reason,
                    "side": side,
                })

                equity += net_pnl
                equity_curve.append(equity)
                in_pos = False

    # Расчет метрик
    n_trades = len(trades)
    if n_trades == 0:
        return {
            "trades": 0, "net_pnl": 0.0, "win_rate": 0.0,
            "profit_factor": 0.0, "max_dd": 0.0, "avg_holding_h": 0.0,
        }

    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] <= 0]
    gross_profit = sum(t["net_pnl"] for t in wins)
    gross_loss = abs(sum(t["net_pnl"] for t in losses))
    pf = (gross_profit / gross_loss) if gross_loss > 1e-6 else (99.0 if gross_profit > 0 else 0.0)

    # Максимальная просадка
    eq_arr = np.array(equity_curve)
    peaks = np.maximum.accumulate(eq_arr)
    dds = peaks - eq_arr
    max_dd = float(np.max(dds)) if len(dds) > 0 else 0.0

    avg_hold = sum(t["holding_hours"] for t in trades) / n_trades

    return {
        "trades": n_trades,
        "net_pnl": round(float(equity), 4),
        "win_rate": round(len(wins) / n_trades * 100.0, 1),
        "profit_factor": round(float(pf), 2),
        "max_dd": round(max_dd, 4),
        "avg_holding_h": round(avg_hold, 1),
    }


def run_walk_forward_for_pair(ca: str, cb: str) -> dict[str, Any]:
    """Запускает 4-шаговый Walk-Forward анализ для пары."""
    try:
        pa, pb, ts = load_pair_prices(ca, cb)
    except Exception as e:
        return {"error": str(e)}

    total_len = len(pa)
    # Фолд структура (минуты):
    # Каждое окно In-Sample = 14 дней (20160 мин)
    # Каждое окно Out-Of-Sample = 7 дней (10080 мин)
    # Сдвиг окна = 3.5 дня (5040 мин)
    is_bars = 14 * 24 * 60     # 20,160 мин
    oos_bars = 7 * 24 * 60     # 10,080 мин
    step_bars = int(3.5 * 24 * 60)  # 5,040 мин

    folds = []
    fold_idx = 0
    start = 0

    while start + is_bars + oos_bars <= total_len:
        fold_idx += 1
        is_start = start
        is_end = is_start + is_bars
        oos_start = is_end
        oos_end = oos_start + oos_bars

        # 1. Калибровка на In-Sample
        is_pa = pa[is_start:is_end]
        is_pb = pb[is_start:is_end]

        x = np.column_stack([is_pb, np.ones_like(is_pb)])
        beta_vec, _, _, _ = np.linalg.lstsq(x, is_pa, rcond=None)
        calibrated_beta = beta_vec[0]
        calibrated_alpha = beta_vec[1]

        # Подбор лучшего порога Z в [2.0, 2.5, 3.0] на In-Sample
        best_z = 2.5
        best_is_pnl = -999.0
        best_is_res = None

        for test_z in [2.0, 2.5, 3.0]:
            res = simulate_trading(is_pa, is_pb, calibrated_alpha, calibrated_beta, entry_z=test_z)
            if res["net_pnl"] > best_is_pnl:
                best_is_pnl = res["net_pnl"]
                best_z = test_z
                best_is_res = res

        # 2. Слепое тестирование на Out-Of-Sample с зафиксированными параметрами
        # Для прогрева rolling window захватываем 1440 баров до oos_start
        oos_feed_start = max(0, oos_start - 1440)
        oos_pa = pa[oos_feed_start:oos_end]
        oos_pb = pb[oos_feed_start:oos_end]

        oos_res = simulate_trading(oos_pa, oos_pb, calibrated_alpha, calibrated_beta, entry_z=best_z)

        # Walk-Forward Efficiency (отношение форвардного PnL к калибровочному, приведенное к 1 неделе)
        # IS длится 2 недели, OOS длится 1 неделю => нормализация: (OOS_pnl) / (IS_pnl / 2)
        norm_is_pnl = best_is_pnl / 2.0
        wfe = (oos_res["net_pnl"] / norm_is_pnl * 100.0) if norm_is_pnl > 0.01 else (0.0 if oos_res["net_pnl"] <= 0 else 100.0)

        folds.append({
            "fold": fold_idx,
            "best_z": best_z,
            "beta": round(float(calibrated_beta), 4),
            "is_trades": best_is_res["trades"] if best_is_res else 0,
            "is_pnl": best_is_res["net_pnl"] if best_is_res else 0.0,
            "is_wr": best_is_res["win_rate"] if best_is_res else 0.0,
            "oos_trades": oos_res["trades"],
            "oos_pnl": oos_res["net_pnl"],
            "oos_wr": oos_res["win_rate"],
            "oos_pf": oos_res["profit_factor"],
            "oos_max_dd": oos_res["max_dd"],
            "wfe_pct": round(wfe, 1),
        })

        start += step_bars

    # Агрегация форвардных результатов по всем фолдам
    tot_oos_trades = sum(f["oos_trades"] for f in folds)
    tot_oos_pnl = sum(f["oos_pnl"] for f in folds)
    avg_oos_wr = (sum(f["oos_wr"] * f["oos_trades"] for f in folds) / tot_oos_trades) if tot_oos_trades > 0 else 0.0
    max_oos_dd = max((f["oos_max_dd"] for f in folds), default=0.0)
    avg_wfe = sum(f["wfe_pct"] for f in folds) / len(folds) if folds else 0.0

    # Процент к выделенной марже $2 ($1 на ногу)
    roi_to_margin_pct = (tot_oos_pnl / TOTAL_MARGIN) * 100.0

    return {
        "coin_a": ca,
        "coin_b": cb,
        "folds_count": len(folds),
        "tot_oos_trades": tot_oos_trades,
        "tot_oos_pnl_usdt": round(tot_oos_pnl, 4),
        "avg_oos_win_rate": round(avg_oos_wr, 1),
        "max_oos_dd_usdt": round(max_oos_dd, 4),
        "avg_wfe_pct": round(avg_wfe, 1),
        "roi_to_margin_pct": round(roi_to_margin_pct, 1),
        "folds": folds,
    }


def main():
    coint_file = Path("output/cointegrated_pairs.json")
    if not coint_file.exists():
        print("[ERROR] Сначала запустите скрининг коинтеграции!")
        return

    with open(coint_file, "r", encoding="utf-8") as f:
        pairs = json.load(f)

    # Берем топ-15 статистически лучших пар
    candidates = pairs[:15]
    print(f"[WFA] Запуск Walk-Forward анализа для топ-{len(candidates)} коинтегрированных пар...", flush=True)
    print(f"[CONFIG] Размер позиции: ${POSITION_SIZE_USDT:.1f} (залог ${TOTAL_MARGIN:.1f} USDT при {LEVERAGE:.0f}x плече)", flush=True)
    print(f"[CONFIG] Комиссии: 4x taker + slippage = {ROUNDTRIP_FEE_PCT*100:.2f}% (${FEE_PER_TRADE_USDT:.4f}/сделка)\n", flush=True)

    wfa_results = []
    for idx, p in enumerate(candidates, 1):
        ca, cb = p["coin_a"], p["coin_b"]
        print(f"[{idx:2d}/{len(candidates)}] Walk-Forward анализ для {ca} / {cb}...", end=" ", flush=True)
        res = run_walk_forward_for_pair(ca, cb)
        if "error" in res:
            print(f"ОШИБКА: {res['error']}")
            continue
        print(
            f"OOS Сделок: {res['tot_oos_trades']} | "
            f"OOS PnL: {res['tot_oos_pnl_usdt']:+.3f}$ | "
            f"WR: {res['avg_oos_win_rate']:.1f}% | "
            f"WFE: {res['avg_wfe_pct']:.1f}% | "
            f"ROI: {res['roi_to_margin_pct']:+.1f}%",
            flush=True
        )
        wfa_results.append(res)

    out_file = Path("output/wfa_results.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(wfa_results, f, indent=2, ensure_ascii=False)

    print(f"\n[REPORT] Результаты WFA сохранены в {out_file}", flush=True)

    # Сортировка по чистому форвардному профиту
    wfa_results.sort(key=lambda x: x["tot_oos_pnl_usdt"], reverse=True)

    print("\n" + "=" * 90)
    print("ИТОГОВЫЙ РЕЙТИНГ WALK-FORWARD АНАЛИЗА (СТРОГО НА НЕВИДАННЫХ OUT-OF-SAMPLE ДАННЫХ)")
    print("=" * 90)
    for idx, r in enumerate(wfa_results, 1):
        status = "ВЫДЕРЖАЛ WFA (ПРИГОДЕН)" if (r["tot_oos_pnl_usdt"] > 0 and r["avg_wfe_pct"] >= 50.0) else "ПРОВАЛИЛ ФОРВАРД (БРАК)"
        print(
            f"{idx:2d}. {r['coin_a']:<10} / {r['coin_b']:<10} | "
            f"OOS PnL: {r['tot_oos_pnl_usdt']:>+7.3f}$ | "
            f"Сделок: {r['tot_oos_trades']:>2d} | "
            f"WR: {r['avg_oos_win_rate']:>5.1f}% | "
            f"MaxDD: {r['max_oos_dd_usdt']:>6.3f}$ | "
            f"WFE: {r['avg_wfe_pct']:>5.1f}% | "
            f"[{status}]"
        )
    print("=" * 90)


if __name__ == "__main__":
    main()
