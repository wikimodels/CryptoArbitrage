"""
Общее состояние рынка, наполняемое WS-стримами цен и периодическим
REST-снимком funding. Живёт в одном event loop — блокировок не нужно.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from .base import Quote


class MarketState:
    def __init__(self, staleness_sec: float):
        self.staleness_sec = staleness_sec
        # (exchange, symbol) -> [bid, ask, bid_size, ask_size, ts]
        self._price: dict[tuple[str, str], list] = {}
        # (exchange, symbol) -> (rate, interval_h, next_ts)
        self._funding: dict[tuple[str, str], tuple[float, float, Optional[float]]] = {}
        # fee lookup: (exchange, symbol) -> (taker, maker)
        self._fees: Callable[[str, str], tuple[float, float]] = lambda e, s: (0.0006, 0.0002)
        self.updates = 0

    def set_fee_lookup(self, fn: Callable[[str, str], tuple[float, float]]):
        self._fees = fn

    def update_price(self, exchange: str, symbol: str, bid: float, ask: float,
                     bid_size: float, ask_size: float):
        self._price[(exchange, symbol)] = [bid, ask, bid_size, ask_size, time.time()]
        self.updates += 1

    def update_funding(self, exchange: str, mapping: dict[str, tuple[float, float, Optional[float]]]):
        for sym, val in mapping.items():
            self._funding[(exchange, symbol_key(sym))] = val

    def _build_quote(self, exchange: str, symbol: str, now: float) -> Optional[Quote]:
        p = self._price.get((exchange, symbol))
        if not p:
            return None
        bid, ask, bsz, asz, ts = p
        if now - ts > self.staleness_sec:
            return None
        if bid <= 0 or ask <= 0:
            return None
        if bid >= ask:
            # перекрёстный/локнутый стакан на одной бирже = битая котировка
            return None
        rate, interval_h, next_ts = self._funding.get((exchange, symbol), (0.0, 8.0, None))
        taker, maker = self._fees(exchange, symbol)
        return Quote(
            exchange=exchange, symbol=symbol, ts=ts,
            best_bid=bid, best_ask=ask, bid_size=bsz, ask_size=asz,
            funding_rate=rate, funding_interval_hours=interval_h, next_funding_ts=next_ts,
            taker_fee=taker, maker_fee=maker,
        )

    def fresh_quotes(self, symbol: str, exchanges: list[str]) -> dict[str, Quote]:
        now = time.time()
        out: dict[str, Quote] = {}
        for exch in exchanges:
            q = self._build_quote(exch, symbol, now)
            if q is not None:
                out[exch] = q
        return out

    def fresh_count_by_exchange(self, exchanges: list[str]) -> dict[str, int]:
        now = time.time()
        counts = {e: 0 for e in exchanges}
        for (exch, _sym), p in self._price.items():
            if exch in counts and now - p[4] <= self.staleness_sec:
                counts[exch] += 1
        return counts


def symbol_key(sym: str) -> str:
    return sym
