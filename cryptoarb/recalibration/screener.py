"""
Векторный скрининг коинтеграции криптовалютных пар.
Проверяет ликвидность, рассчитывает корреляционную матрицу,
выполняет двухшаговый тест Энгла-Грейнджера и оценивает период полураспада Орнштейна-Уленбека.
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

import warnings
warnings.filterwarnings("ignore")

import numpy as np
import polars as pl
from statsmodels.tsa.stattools import coint, adfuller

log = logging.getLogger("recalibration.screener")


class CointegrationScreener:
    """Модуль первичного статистического отбора кандидатов на парный арбитраж."""

    def __init__(
        self,
        data_dir: str | Path = "data/raw_1m_30d/bitget",
        min_bars: int = 40000,
        max_zero_vol_ratio: float = 0.35,
        min_notional_volume: float = 100_000.0,
        corr_threshold: float = 0.65,
        in_sample_minutes: int = 20 * 24 * 60,  # 20 дней = 28,800 минут
    ):
        self.data_dir = Path(data_dir)
        self.min_bars = min_bars
        self.max_zero_vol_ratio = max_zero_vol_ratio
        self.min_notional_volume = min_notional_volume
        self.corr_threshold = corr_threshold
        self.in_sample_minutes = in_sample_minutes

    def load_liquid_series(self) -> tuple[dict[str, np.ndarray], list[int]]:
        """Загружает минутные ряды цен закрытия для монет, удовлетворяющих критериям ликвидности."""
        series: dict[str, np.ndarray] = {}
        timestamps: list[int] = []

        if not self.data_dir.exists():
            log.warning("Каталог данных %s не существует", self.data_dir)
            return series, timestamps

        coin_dirs = sorted([d for d in self.data_dir.iterdir() if d.is_dir() and (d / "candles.parquet").exists()])
        log.info("Сканирование %d монет в %s...", len(coin_dirs), self.data_dir)

        for cd in coin_dirs:
            sym_name = cd.name.replace("_USDT_USDT", "")
            fpath = cd / "candles.parquet"
            try:
                df = pl.read_parquet(fpath).select(["ts", "c", "v"])
                if df.height < self.min_bars:
                    continue

                zero_vol_ratio = (df["v"] == 0).sum() / df.height
                tot_notional = (df["c"] * df["v"]).sum()
                if zero_vol_ratio > self.max_zero_vol_ratio or tot_notional < self.min_notional_volume:
                    continue

                prices = df["c"].to_numpy()
                if np.std(prices) <= 1e-9 or np.any(np.isnan(prices)) or np.any(prices <= 0):
                    continue

                series[sym_name] = prices
                if not timestamps:
                    timestamps = df["ts"].to_list()
            except Exception as e:
                log.debug("Ошибка чтения %s: %s", fpath, e)
                continue

        log.info("Отобрано %d ликвидных монет для скрининга", len(series))
        return series, timestamps

    @staticmethod
    def estimate_half_life(spread: np.ndarray) -> float:
        """Оценка периода полураспада возврата к средней через модель Орнштейна-Уленбека:
        delta_S(t) = -theta * (S(t-1) - mu) + e => t_half = ln(2) / theta
        """
        lag = spread[:-1]
        delta = spread[1:] - lag
        x = np.column_stack([lag, np.ones_like(lag)])
        try:
            theta_vec, _, _, _ = np.linalg.lstsq(x, delta, rcond=None)
            theta = -theta_vec[0]
            if theta > 1e-6:
                return math.log(2.0) / theta
        except Exception:
            pass
        return float("inf")

    def run_screening(self, series: dict[str, np.ndarray]) -> list[dict[str, Any]]:
        """Выполняет двухшаговый тест Энгла-Грейнджера и отбор коинтегрированных пар."""
        coins = sorted(series.keys())
        n = len(coins)
        if n < 2:
            return []

        # Корреляционная матрица на In-Sample интервале
        is_matrix = np.zeros((n, self.in_sample_minutes))
        for i, c in enumerate(coins):
            is_matrix[i, :] = series[c][:self.in_sample_minutes]

        corr = np.corrcoef(is_matrix)
        candidates = []

        for i in range(n):
            for j in range(i + 1, n):
                r = corr[i, j]
                if abs(r) >= self.corr_threshold:
                    candidates.append((coins[i], coins[j], r))

        log.info("Пар с корреляцией |r| >= %.2f: %d", self.corr_threshold, len(candidates))

        results = []
        for idx, (ca, cb, r) in enumerate(candidates, 1):
            pa = series[ca][:self.in_sample_minutes]
            pb = series[cb][:self.in_sample_minutes]

            try:
                score, pvalue, _ = coint(pa, pb, maxlag=10)
                if pvalue < 0.05:
                    x = np.column_stack([pb, np.ones_like(pb)])
                    beta_vec, _, _, _ = np.linalg.lstsq(x, pa, rcond=None)
                    beta = beta_vec[0]
                    alpha = beta_vec[1]

                    spread = pa - (beta * pb + alpha)
                    adf_res = adfuller(spread, maxlag=10)
                    adf_stat = adf_res[0]
                    adf_pvalue = adf_res[1]

                    hl = self.estimate_half_life(spread)

                    # Спред должен сходиться: от 30 минут до 24 часов
                    if 30.0 <= hl <= 1440.0:
                        results.append({
                            "coin_a": ca,
                            "coin_b": cb,
                            "sym_a": f"{ca}/USDT:USDT",
                            "sym_b": f"{cb}/USDT:USDT",
                            "corr": round(float(r), 4),
                            "coint_pvalue": float(pvalue),
                            "adf_pvalue": float(adf_pvalue),
                            "adf_stat": round(float(adf_stat), 3),
                            "beta": round(float(beta), 6),
                            "alpha": round(float(alpha), 6),
                            "half_life_min": round(hl, 1),
                            "half_life_hours": round(hl / 60.0, 2),
                        })
            except Exception as e:
                log.debug("Ошибка теста коинтеграции для %s/%s: %s", ca, cb, e)
                continue

        results.sort(key=lambda x: x["coint_pvalue"])
        log.info("Отобрано статистически коинтегрированных пар: %d", len(results))
        return results
