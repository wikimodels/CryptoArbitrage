"""
Базовый интерфейс торговой стратегии (Strategy Pattern).
Все стратегии реализуют этот класс и регистрируются в StrategyRegistry.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cryptoarb.engine import Engine


class BaseStrategy(ABC):
    """Абстрактный базовый класс для модульных стратегий."""

    def __init__(self, engine: Engine, cfg: dict[str, Any]):
        self.engine = engine
        self.cfg = cfg
        self.name: str = "base"
        self.display_name: str = "Базовая стратегия"
        self.enabled: bool = False

    async def start(self) -> None:
        """Инициализация и прогрев исторических данных при запуске движка."""
        pass

    async def stop(self) -> None:
        """Очистка ресурсов при остановке движка."""
        pass

    def on_price_tick(self, symbol: str, exchange: str, mid_price: float, now_sec: float) -> None:
        """Обработка тика реального времени от WebSocket-стрима."""
        pass

    async def on_funding_refresh(self) -> None:
        """Вызывается при периодическом обновлении ставок фандинга по биржам."""
        pass

    async def on_scan_symbol(self, symbol: str, quotes: dict, now: float) -> None:
        """Сканирование символа на наличие точек входа для данной стратегии."""
        pass

    async def check_exits(self, symbol: str, quotes: dict, now: float) -> None:
        """Проверка условий закрытия открытых позиций данной стратегии."""
        pass

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Возвращает срез аналитики, радара и KPI для передачи на фронтенд по WebSocket."""
        pass
