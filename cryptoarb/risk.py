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
    last_price = levels[0].price
    for lvl in levels:
        if remaining <= 0:
            break
        last_price = lvl.price
        remaining -= lvl.price * lvl.size

    if remaining > 0:
        return LegFillResult(False, None, "insufficient_depth")
    return LegFillResult(True, last_price, "filled")


def check_orphan_leg(leg_a: LegFillResult, leg_b: LegFillResult) -> bool:
    """True, если одна нога исполнилась, а другая — нет (orphan)."""
    return leg_a.filled != leg_b.filled
