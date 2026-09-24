"""Z-score стратегия: ratio = closeA/closeB, EMA50, std50, z. Входы от 2.2 до 4.0+.

Выход: схождение (к 0 или 0.3) | стоп |z| > 1.5*entry | тайм-стоп 15-60м | макс-холд 4ч.
Без lookahead: MA/std берутся со сдвигом на 1 бар назад (shift 1).
"""
from __future__ import annotations

ENTRY_ZS = [2.2, 2.4, 2.5, 2.7, 3.0, 3.5, 4.0]
EXIT_Z = 0.3
STOP_MULT = 1.5
TIMESTOP_MIN = 15
MAX_HOLD_MIN = 240
MA_PERIOD = 50


def ema(values: list[float], period: int) -> list[float | None]:
    k = 2 / (period + 1)
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    sma = sum(values[:period]) / period
    out[period - 1] = sma
    e = sma
    for i in range(period, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def rolling_std(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(period - 1, len(values)):
        w = values[i - period + 1:i + 1]
        m = sum(w) / period
        var = sum((x - m) ** 2 for x in w) / period
        out[i] = var ** 0.5
    return out


def compute_z(ratio: list[float], period: int = MA_PERIOD) -> tuple[list, list, list]:
    ma = ema(ratio, period)
    sd = rolling_std(ratio, period)
    z: list[float | None] = [None] * len(ratio)
    for i in range(len(ratio)):
        # Без заглядывания: используем MA и STD с предыдущего бара (i-1)
        j = i - 1
        if j >= 0 and ma[j] is not None and sd[j]:
            if sd[j] > 0:
                z[i] = (ratio[i] - ma[j]) / sd[j]
    return ma, sd, z


def backtest_pair(ts: list[int], px_a: list[float], px_b: list[float],
                  entry_z: float, fee_round_pct: float = 0.24,
                  slip_pct: float = 0.04,
                  exit_z: float = EXIT_Z, timestop_min: int = TIMESTOP_MIN,
                  z: list | None = None) -> list[dict]:
    """Вход парой, выход парой. Торговля спредом между биржами (market-neutral, equal-notional)."""
    if z is None:
        ratio = [a / b if b else float("nan") for a, b in zip(px_a, px_b)]
        _, _, z = compute_z([r for r in ratio])
    trades: list[dict] = []
    open_pos: dict | None = None
    for i in range(len(ts)):
        zi = z[i]
        if zi is None or zi != zi:
            continue
        if open_pos is None:
            if abs(zi) >= entry_z:
                # z > 0 => биржа A локально дороже B: шорт A, лонг B (dir = -1)
                # z < 0 => биржа A локально дешевле B: лонг A, шорт B (dir = 1)
                open_pos = {"i_in": i, "ts_in": ts[i], "z_in": zi,
                            "px_a_in": px_a[i], "px_b_in": px_b[i],
                            "dir": -1 if zi > 0 else 1}
        else:
            hold = i - open_pos["i_in"]
            exit_r = None

            # Схождение: возврат к среднему (порог exit_z или пересечение 0)
            converged = False
            if open_pos["z_in"] > 0:
                if zi <= exit_z:
                    converged = True
            else:
                if zi >= -exit_z:
                    converged = True
            if abs(zi) <= exit_z:
                converged = True

            if converged:
                exit_r = "converged"
            elif abs(zi) > STOP_MULT * entry_z:
                exit_r = "stop"
            elif hold >= timestop_min:
                exit_r = "timestop"
            elif hold >= MAX_HOLD_MIN:
                exit_r = "maxhold"

            if exit_r:
                ra = (px_a[i] - open_pos["px_a_in"]) / open_pos["px_a_in"] * 100
                rb = (px_b[i] - open_pos["px_b_in"]) / open_pos["px_b_in"] * 100
                if open_pos["dir"] < 0:
                    gross = -ra + rb
                else:
                    gross = ra - rb
                net = gross - fee_round_pct - slip_pct
                trades.append({**open_pos, "i_out": i, "ts_out": ts[i],
                               "z_out": zi, "reason": exit_r,
                               "gross_pct": round(gross, 4), "net_pct": round(net, 4),
                               "hold_min": hold})
                open_pos = None
    return trades


def backtest_directional(ts: list[int], px_a: list[float], px_b: list[float],
                         entry_z: float, fee_pct: float = 0.12,
                         slip_pct: float = 0.02,
                         exit_z: float = EXIT_Z, timestop_min: int = TIMESTOP_MIN,
                         z: list | None = None) -> list[dict]:
    """Торговля одной ногой на аномальной бирже A: z > 0 => шорт A, z < 0 => лонг A.
    Издержки одной ноги: 2 x taker = 0.12% + 0.02% slip = 0.14%."""
    if z is None:
        ratio = [a / b if b else float("nan") for a, b in zip(px_a, px_b)]
        _, _, z = compute_z([r for r in ratio])
    trades: list[dict] = []
    open_pos: dict | None = None
    for i in range(len(ts)):
        zi = z[i]
        if zi is None or zi != zi:
            continue
        if open_pos is None:
            if abs(zi) >= entry_z:
                side = "short" if zi > 0 else "long"
                open_pos = {"i_in": i, "ts_in": ts[i], "z_in": zi,
                            "px_in": px_a[i], "side": side}
        else:
            hold = i - open_pos["i_in"]
            exit_r = None

            # Схождение: возврат к среднему (exit_z или пересечение 0)
            converged = False
            if open_pos["z_in"] > 0:
                if zi <= exit_z:
                    converged = True
            else:
                if zi >= -exit_z:
                    converged = True
            if abs(zi) <= exit_z:
                converged = True

            if converged:
                exit_r = "converged"
            elif abs(zi) > STOP_MULT * entry_z:
                exit_r = "stop"
            elif hold >= timestop_min:
                exit_r = "timestop"
            elif hold >= MAX_HOLD_MIN:
                exit_r = "maxhold"

            if exit_r:
                if open_pos["side"] == "short":
                    gross = (open_pos["px_in"] - px_a[i]) / open_pos["px_in"] * 100
                else:
                    gross = (px_a[i] - open_pos["px_in"]) / open_pos["px_in"] * 100
                net = gross - fee_pct - slip_pct
                trades.append({**open_pos, "i_out": i, "ts_out": ts[i],
                               "z_out": zi, "reason": exit_r,
                               "gross_pct": round(gross, 4), "net_pct": round(net, 4),
                               "hold_min": hold})
                open_pos = None
    return trades
