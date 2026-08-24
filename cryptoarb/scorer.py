"""
Скоринг (Блок 3 ТЗ): для символа, доступного на 2+ биржах, перебираем
все пары бирж и считаем net_edge через calc.compute_net_edge.
"""
from __future__ import annotations

import itertools
from typing import Iterable, Optional

from .base import OrderBookSnapshot, Quote
from .calc import NetEdgeResult, compute_net_edge


def score_symbol(
    symbol: str,
    quotes_by_exchange: dict[str, Quote],
    orderbooks_by_exchange: dict[str, Optional[OrderBookSnapshot]],
    holding_hours: float,
    min_threshold_pct: float,
    slippage_buffer_pct: float,
    position_size_usdt: float,
) -> list[NetEdgeResult]:
    """Результаты по ВСЕМ парам бирж символа (не только прошедшим порог)."""
    results: list[NetEdgeResult] = []
    exchanges = list(quotes_by_exchange.keys())
    for exch_a, exch_b in itertools.combinations(exchanges, 2):
        res = compute_net_edge(
            quotes_by_exchange[exch_a], quotes_by_exchange[exch_b],
            holding_hours=holding_hours,
            min_threshold_pct=min_threshold_pct,
            slippage_buffer_pct=slippage_buffer_pct,
            ob_a=orderbooks_by_exchange.get(exch_a),
            ob_b=orderbooks_by_exchange.get(exch_b),
            position_size_usdt=position_size_usdt,
        )
        results.append(res)
    return results


def passing_signals(results: Iterable[NetEdgeResult]) -> list[NetEdgeResult]:
    return [r for r in results if r.passed_threshold]
