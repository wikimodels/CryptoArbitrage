"""
Скринер коинтеграции криптовалютных пар на 30-дневных минутных данных.
Использует двухшаговый тест Энгла-Грейнджера, проверку стационарности ADF и период полураспада Орнштейна-Уленбека.
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path
from typing import Any

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import numpy as np
import polars as pl
from statsmodels.tsa.stattools import coint, adfuller

DATA_DIR = Path("data/raw_1m_30d/bitget")
IN_SAMPLE_MINUTES = 20 * 24 * 60  # 20 дней = 28,800 минут


def load_all_series() -> tuple[dict[str, np.ndarray], list[int]]:
    """Загружает минутные цены закрытия для ликвидных монет."""
    series: dict[str, np.ndarray] = {}
    timestamps: list[int] = []

    coin_dirs = sorted([d for d in DATA_DIR.iterdir() if d.is_dir() and (d / "candles.parquet").exists()])
    print(f"[DATA] Сканирование {len(coin_dirs)} монет в {DATA_DIR}...", flush=True)

    for cd in coin_dirs:
        sym_name = cd.name.replace("_USDT_USDT", "")
        fpath = cd / "candles.parquet"
        try:
            df = pl.read_parquet(fpath).select(["ts", "c", "v"])
            if df.height < 40000:
                continue
            
            # Проверка ликвидности: не более 35% нулевых минут и объем > $100k
            zero_vol_ratio = (df["v"] == 0).sum() / df.height
            tot_notional = (df["c"] * df["v"]).sum()
            if zero_vol_ratio > 0.35 or tot_notional < 100_000:
                continue

            prices = df["c"].to_numpy()
            if np.std(prices) <= 1e-9 or np.any(np.isnan(prices)) or np.any(prices <= 0):
                continue

            series[sym_name] = prices
            if not timestamps:
                timestamps = df["ts"].to_list()
        except Exception:
            continue

    print(f"[DATA] Отобрано {len(series)} активных ликвидных монет для скрининга", flush=True)
    return series, timestamps


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
            half_life = math.log(2.0) / theta
            return half_life
    except Exception:
        pass
    return float("inf")


def screen_cointegration(series: dict[str, np.ndarray]) -> list[dict[str, Any]]:
    """Матричный скрининг коинтеграции на In-Sample отрезке (первые 20 дней = 28800 баров)."""
    coins = sorted(series.keys())
    n = len(coins)
    
    # 1. Корреляционная матрица по In-Sample
    is_matrix = np.zeros((n, IN_SAMPLE_MINUTES))
    for i, c in enumerate(coins):
        is_matrix[i, :] = series[c][:IN_SAMPLE_MINUTES]

    corr = np.corrcoef(is_matrix)
    print(f"[SCREENER] Расчет корреляции для {n * (n - 1) // 2} пар завершен", flush=True)

    # 2. Отбор пар с высокой корреляцией (|r| >= 0.65)
    candidates = []
    for i in range(n):
        for j in range(i + 1, n):
            r = corr[i, j]
            if abs(r) >= 0.65:
                candidates.append((coins[i], coins[j], r))

    print(f"[SCREENER] Отобрано {len(candidates)} пар с |r| >= 0.65 для теста Энгла-Грейнджера...", flush=True)

    results = []
    t0 = time.time()

    for idx, (ca, cb, r) in enumerate(candidates):
        if (idx + 1) % 100 == 0:
            print(f"  Проверено {idx + 1}/{len(candidates)} пар...", flush=True)

        pa = is_matrix[coins.index(ca)]
        pb = is_matrix[coins.index(cb)]

        try:
            score, p_value, _ = coint(pa, pb, maxlag=10)
        except Exception:
            continue

        if p_value < 0.05:
            # Оценка Hedge Ratio beta: pa = alpha + beta * pb
            x = np.column_stack([pb, np.ones_like(pb)])
            beta_vec, _, _, _ = np.linalg.lstsq(x, pa, rcond=None)
            beta = beta_vec[0]
            alpha = beta_vec[1]

            spread = pa - (beta * pb + alpha)
            
            try:
                adf_res = adfuller(spread, maxlag=10)
                adf_p = float(adf_res[1])
                adf_stat = float(adf_res[0])
            except Exception:
                adf_p = 1.0
                adf_stat = 0.0

            hl_mins = estimate_half_life(spread)
            hl_hours = hl_mins / 60.0

            results.append({
                "coin_a": ca,
                "coin_b": cb,
                "corr": round(float(r), 4),
                "coint_pvalue": float(p_value),
                "adf_pvalue": float(adf_p),
                "adf_stat": round(float(adf_stat), 3),
                "beta": round(float(beta), 5),
                "alpha": round(float(alpha), 5),
                "half_life_mins": round(hl_mins, 1),
                "half_life_hours": round(hl_hours, 2),
                "spread_std": round(float(np.std(spread)), 6),
            })

    dt = time.time() - t0
    print(f"[SCREENER] Тестирование завершено за {dt:.1f}с. Найдено {len(results)} пар с p-value < 0.05", flush=True)

    # Фильтр: 30 мин <= half_life <= 24 ч
    valid_results = [r for r in results if 30.0 <= r["half_life_mins"] <= 1440.0]
    valid_results.sort(key=lambda x: (x["coint_pvalue"], x["adf_pvalue"]))

    print(f"[SCREENER] {len(valid_results)} пар имеют период полураспада от 30 мин до 24 ч", flush=True)
    return valid_results


def main():
    series, timestamps = load_all_series()
    if not series:
        print("[ERROR] Не удалось загрузить данные")
        return

    pairs = screen_cointegration(series)

    out_file = Path("output/cointegrated_pairs.json")
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(pairs, f, indent=2, ensure_ascii=False)

    print(f"\n[REPORT] Сохранено {len(pairs)} коинтегрированных пар в {out_file}", flush=True)
    print("\n--- ТОП-20 КОИНТЕГРИРОВАННЫХ ПАР ПО МАТЕМАТИЧЕСКИМ ТЕСТАМ ---", flush=True)
    for idx, p in enumerate(pairs[:20], 1):
        print(
            f"{idx:2d}. {p['coin_a']:<10} / {p['coin_b']:<10} | "
            f"p-val: {p['coint_pvalue']:.6f} | ADF p: {p['adf_pvalue']:.6f} | "
            f"Corr: {p['corr']:+.3f} | Beta: {p['beta']:.4f} | "
            f"Half-life: {p['half_life_hours']:.1f}ч ({p['half_life_mins']:.0f}м)",
            flush=True
        )


if __name__ == "__main__":
    main()
