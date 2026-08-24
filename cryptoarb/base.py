"""
Единый внутренний интерфейс коннекторов бирж.

Ядро (calc/scorer/emulator/engine) работает только с этим контрактом,
никогда с конкретной биржей напрямую — это plug-in модель коннекторов
из ТЗ (п.2.1). Заменить polling-коннектор на нативный WS = реализовать
этот же ABC, ядро не меняется.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional


@dataclass
class Quote:
    """Снимок рынка по инструменту на бирже в момент времени."""
    exchange: str
    symbol: str
    ts: float
    best_bid: float
    best_ask: float
    bid_size: float
    ask_size: float
    funding_rate: float               # доля, не %
    funding_interval_hours: float
    next_funding_ts: Optional[float] = None
    taker_fee: float = 0.0006
    maker_fee: float = 0.0002
    exchange_type: str = "CEX"


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class OrderBookSnapshot:
    exchange: str
    symbol: str
    ts: float
    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)


# callback(exchange, symbol, best_bid, best_ask, bid_size, ask_size)
PriceCallback = Callable[[str, str, float, float, float, float], None]


class ExchangeConnector(ABC):
    """Абстрактный коннектор. Реализации живут в connectors/*.py"""

    name: str

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def fetch_symbols(self) -> list[str]:
        """Нормализованные USDT-перпетуалы на бирже."""
        ...

    @abstractmethod
    async def watch_prices(self, symbols: list[str], on_price: PriceCallback) -> None:
        """Долгоживущий WS-цикл: пушит best bid/ask через on_price.
        Должен сам переподключаться при обрыве."""
        ...

    @abstractmethod
    async def refresh_funding(self, symbols: list[str]) -> dict[str, tuple[float, float, Optional[float]]]:
        """REST-снимок funding: {symbol: (rate, interval_hours, next_funding_ts)}."""
        ...

    @abstractmethod
    async def fetch_orderbook(self, symbol: str, depth: int = 20) -> Optional[OrderBookSnapshot]: ...

    def fees_for(self, symbol: str) -> tuple[float, float]:
        """(taker, maker) для символа. Дефолт — из настроек коннектора."""
        return (getattr(self, "default_taker_fee", 0.0006),
                getattr(self, "default_maker_fee", 0.0002))
