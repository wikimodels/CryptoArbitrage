"""
Модуль скользящего Walk-Forward анализа (WFA) для парного арбитража.
Моделирует скользящие окна калибровки (In-Sample) и слепой торговли (Out-Of-Sample)
со строгим учетом комиссий 4x taker, проскальзывания и размера позиции $10 на ногу ($2 совокупного залога при 10x плече).
"""
from __future__ import annotations

import logging
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

log = logging.getLogger("recalibration.wfa")

# Экономические константы риск-менеджмента
POSITION_SIZE_USDT = 10.0  # Нотионал $10 на каждую ногу
LEVERAGE = 10.0
MARGIN_PER_LEG = POSITION_SIZE_USDT / LEVERAGE  # $1.0 USDT
TOTAL_MARGIN = MARGIN_PER_LEG * 2.0             # $2.0 USDT совокупного залога на пару

TAKER_FEE_RATE = 0.0006  # 0.06% на ногу
SLIPPAGE_RATE = 0.0005   # 0.05% на ногу
ROUNDTRIP_FEE_PCT = (TAKER_FEE_RATE * 4.0) + (SLIPPAGE_RATE * 2.0)  # ~0.34%
FEE_PER_TRADE_USDT = POSITION_SIZE_USDT * ROUNDTRIP_FEE_PCT         # ~$0.034


class WalkForwardEngine:
    """Движок скользящего форвардного тестирования межмонетных пар."""

    def __init__(
        self,
        data_dir: str | Path = "data/raw_1m_30d/bitget",
        is_window_days: int = 14,
        oos_window_days: int = 7,
        num_folds: int = 4,
        candidate_entry_z: tuple[float, ...] = (2.0, 2.5, 3.0),
        exit_z: float = 0.0,
        stop_mult: float = 2.0,
    ):
        self.data_dir = Path(data_dir)
        self.is_window_days = is_window_days
        self.oos_window_days = oos_window_days
        self.num_folds = num_folds
        self.candidate_entry_z = candidate_entry_z
        self.exit_z = exit_z
        self.stop_mult = stop_mult

    def load_pair_prices(self, ca: str, cb: str) -> tuple[np.ndarray, np.ndarray, list[int]] | None:
        """Синхронная загрузка минутных цен закрытия двух монет."""
        fa = self.data_dir / f"{ca}_USDT_USDT" / "candles.parquet"
        fb = self.data_dir / f"{cb}_USDT_USDT" / "candles.parquet"
        if not fa.exists() or not fb.exists():
            return None

        try:
            df_a = pl.read_parquet(fa).select(["ts", "c"]).rename({"c": "price_a"})
            df_b = pl.read_parquet(fb).select(["ts", "c"]).rename({"c": "price_b"})
            joined = df_a.join(df_b, on="ts", how="inner").sort("ts")
            return (
                joined["price_a"].to_numpy(),
                joined["price_b"].to_numpy(),
                joined["ts"].to_list(),
            )
        except Exception as e:
            log.debug("Ошибка загрузки пары %s/%s: %s", ca, cb, e)
            return None

    @staticmethod
    def simulate_trading(
        pa: np.ndarray,
        pb: np.ndarray,
        alpha: float,
        beta: float,
        entry_z: float,
        exit_z: float = 0.0,
        stop_mult: float = 2.0,
        max_holding_bars: int = 2880,  # 48 часов
        rolling_window: int = 1440,    # 24 часа
    ) -> dict[str, Any]:
        """Симулирует побарное исполнение парной торговли с фиксацией $10 на ногу."""
        n = len(pa)
        if n < rolling_window + 100:
            return {"trades": 0, "net_pnl": 0.0, "win_rate": 0.0, "profit_factor": 0.0, "max_dd": 0.0}

        spread = pa - (beta * pb + alpha)
        trades = []
        in_pos = False
        side = 0
        entry_idx = 0
        entry_pa = 0.0
        entry_pb = 0.0

        means = np.zeros(n)
        stds = np.zeros(n)
        c_sum = np.cumsum(np.insert(spread, 0, 0))
        c_sq_sum = np.cumsum(np.insert(spread**2, 0, 0))
        w = rolling_window

        for i in range(w, n):
            m = (c_sum[i] - c_sum[i - w]) / w
            var = ((c_sq_sum[i] - c_sq_sum[i - w]) / w) - (m * m)
            means[i] = m
            stds[i] = math.sqrt(max(var, 1e-12))

        equity = 0.0
        equity_curve = [0.0]

        for i in range(w, n):
            z = (spread[i] - means[i]) / stds[i] if stds[i] > 1e-9 else 0.0

            if not in_pos:
                if z >= entry_z:
                    in_pos = True
                    side = 1  # Short A / Long B
                    entry_idx = i
                    entry_pa = pa[i]
                    entry_pb = pb[i]
                elif z <= -entry_z:
                    in_pos = True
                    side = -1  # Long A / Short B
                    entry_idx = i
                    entry_pa = pa[i]
                    entry_pb = pb[i]
            else:
                bars_held = i - entry_idx
                ret_a = (pa[i] - entry_pa) / entry_pa
                ret_b = (pb[i] - entry_pb) / entry_pb

                if side == 1:
                    gross_pnl = (-ret_a + ret_b) * POSITION_SIZE_USDT
                else:
                    gross_pnl = (ret_a - ret_b) * POSITION_SIZE_USDT

                pnl = gross_pnl - FEE_PER_TRADE_USDT

                # Условия выхода
                should_exit = False
                if side == 1 and z <= exit_z:
                    should_exit = True
                elif side == -1 and z >= -exit_z:
                    should_exit = True
                elif abs(z) >= entry_z * stop_mult:  # Защитный стоп
                    should_exit = True
                elif bars_held >= max_holding_bars:   # Выход по времени
                    should_exit = True
                elif i == n - 1:                      # Конец отрезка
                    should_exit = True

                if should_exit:
                    trades.append(pnl)
                    equity += pnl
                    equity_curve.append(equity)
                    in_pos = False
                    side = 0

        num_trades = len(trades)
        if num_trades == 0:
            return {"trades": 0, "net_pnl": 0.0, "win_rate": 0.0, "profit_factor": 0.0, "max_dd": 0.0}

        wins = [t for t in trades if t > 0]
        losses = [t for t in trades if t < 0]
        win_rate = len(wins) / num_trades * 100.0

        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 1e-6 else (99.0 if gross_profit > 0 else 0.0)

        # Расчет максимальной просадки
        peak = 0.0
        max_dd = 0.0
        for eq in equity_curve:
            if eq > peak:
                peak = eq
            dd = peak - eq
            if dd > max_dd:
                max_dd = dd

        return {
            "trades": num_trades,
            "net_pnl": equity,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "max_dd": max_dd,
        }

    def evaluate_pair(self, ca: str, cb: str) -> dict[str, Any] | None:
        """Проводит 4-фолдовый скользящий Walk-Forward тест для пары."""
        prices = self.load_pair_prices(ca, cb)
        if prices is None:
            return None

        pa, pb, timestamps = prices
        total_len = len(pa)

        is_len = self.is_window_days * 1440
        oos_len = self.oos_window_days * 1440
        step_len = (total_len - is_len - oos_len) // max(1, self.num_folds - 1) if self.num_folds > 1 else 0

        if total_len < is_len + oos_len:
            return None

        folds_results = []
        best_overall_beta = 1.0
        best_overall_alpha = 0.0

        for f_idx in range(self.num_folds):
            start_is = f_idx * step_len
            end_is = start_is + is_len
            start_oos = end_is
            end_oos = min(start_oos + oos_len, total_len)

            if end_oos <= start_oos:
                break

            pa_is, pb_is = pa[start_is:end_is], pb[start_is:end_is]
            pa_oos, pb_oos = pa[start_oos:end_oos], pb[start_oos:end_oos]

            # 1. Калибровка параметров на In-Sample
            x_is = np.column_stack([pb_is, np.ones_like(pb_is)])
            beta_vec, _, _, _ = np.linalg.lstsq(x_is, pa_is, rcond=None)
            beta_calib = float(beta_vec[0])
            alpha_calib = float(beta_vec[1])

            if f_idx == self.num_folds - 1:
                best_overall_beta = beta_calib
                best_overall_alpha = alpha_calib

            # Подбор оптимального entry_z на In-Sample
            best_z = 2.5
            best_is_metric = -999.0
            best_is_res: dict[str, Any] = {}

            for cand_z in self.candidate_entry_z:
                sim_is = self.simulate_trading(
                    pa_is, pb_is, alpha_calib, beta_calib,
                    entry_z=cand_z, exit_z=self.exit_z, stop_mult=self.stop_mult
                )
                metric = sim_is["net_pnl"] - (sim_is["max_dd"] * 0.5)
                if metric > best_is_metric:
                    best_is_metric = metric
                    best_z = cand_z
                    best_is_res = sim_is

            # 2. Слепой тест на Out-Of-Sample
            oos_res = self.simulate_trading(
                pa_oos, pb_oos, alpha_calib, beta_calib,
                entry_z=best_z, exit_z=self.exit_z, stop_mult=self.stop_mult
            )

            # Расчет Walk-Forward Efficiency: (OOS PnL / IS PnL) * (IS Days / OOS Days)
            is_pnl = best_is_res.get("net_pnl", 0.0)
            oos_pnl = oos_res.get("net_pnl", 0.0)
            if is_pnl > 0.01:
                wfe = (oos_pnl / is_pnl) * (self.is_window_days / self.oos_window_days) * 100.0
            else:
                wfe = 100.0 if oos_pnl > 0 else 0.0

            folds_results.append({
                "fold": f_idx + 1,
                "best_z": best_z,
                "is_pnl": round(is_pnl, 3),
                "is_trades": best_is_res.get("trades", 0),
                "oos_pnl": round(oos_pnl, 3),
                "oos_trades": oos_res.get("trades", 0),
                "oos_win_rate": round(oos_res.get("win_rate", 0.0), 1),
                "oos_max_dd": round(oos_res.get("max_dd", 0.0), 3),
                "wfe_pct": round(wfe, 1),
            })

        if not folds_results:
            return None

        tot_oos_pnl = sum(f["oos_pnl"] for f in folds_results)
        tot_oos_trades = sum(f["oos_trades"] for f in folds_results)
        avg_oos_wr = sum(f["oos_win_rate"] for f in folds_results) / len(folds_results)
        max_oos_dd = max(f["oos_max_dd"] for f in folds_results)
        avg_wfe = sum(f["wfe_pct"] for f in folds_results) / len(folds_results)
        roi_to_margin = (tot_oos_pnl / TOTAL_MARGIN) * 100.0

        return {
            "coin_a": ca,
            "coin_b": cb,
            "sym_a": f"{ca}/USDT:USDT",
            "sym_b": f"{cb}/USDT:USDT",
            "tot_oos_pnl_usdt": round(tot_oos_pnl, 3),
            "tot_oos_trades": tot_oos_trades,
            "avg_oos_win_rate": round(avg_oos_wr, 1),
            "max_oos_dd_usdt": round(max_oos_dd, 3),
            "avg_wfe_pct": round(avg_wfe, 1),
            "roi_to_margin_pct": round(roi_to_margin, 1),
            "calibrated_beta": round(best_overall_beta, 6),
            "calibrated_alpha": round(best_overall_alpha, 6),
            "optimal_entry_z": float(folds_results[-1]["best_z"]),
            "folds": folds_results,
            "is_valid": bool(tot_oos_pnl > 0.0 and avg_wfe >= 50.0),
        }
