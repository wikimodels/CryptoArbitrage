"""
Риск-менеджмент (Блок 6 ТЗ). На этапе эмулятора живого капитала нет,
поэтому лимиты объёма/числа позиций (6.1-6.2) добавляются в модуле
реального исполнения. Уже сейчас применяется orphan-leg protection:
отказ ноги моделируется ТОЛЬКО по фактической глубине стакана на момент
входа (не рандом), иначе статистика будет завышенно оптимистичной.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .base import OrderBookSnapshot


@dataclass
class LegFillResult:
    filled: bool
    fill_price: Optional[float]
    reason: str


def simulate_leg_fill(orderbook: Optional[OrderBookSnapshot], side: str, size_usdt: float) -> LegFillResult:
    """Хватает ли реальной ликвидности стакана на весь объём ноги.
    side: 'buy' (идём по ask) | 'sell' (идём по bid). Отказ — только из-за
    фактической нехватки глубины книги."""
    if orderbook is None:
        return LegFillResult(False, None, "no_orderbook_snapshot")

    levels = orderbook.asks if side == "buy" else orderbook.bids
    if not levels:
        return LegFillResult(False, None, "empty_book_side")

    remaining = size_usdt
    total_qty = 0.0
    for lvl in levels:
        if remaining <= 0:
            break
        if lvl.price <= 0:
            continue
        level_notional = lvl.price * lvl.size
        take_notional = min(remaining, level_notional)
        total_qty += take_notional / lvl.price
        remaining -= take_notional

    if remaining > 0 or total_qty <= 0:
        return LegFillResult(False, None, "insufficient_depth")
    vwap = size_usdt / total_qty
    return LegFillResult(True, vwap, "filled")


def check_orphan_leg(leg_a: LegFillResult, leg_b: LegFillResult) -> bool:
    """True, если одна нога исполнилась, а другая — нет (orphan)."""
    return leg_a.filled != leg_b.filled
