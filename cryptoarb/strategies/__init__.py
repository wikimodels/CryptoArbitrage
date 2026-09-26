"""
Модульный реестр стратегий CryptoArbitrage.
Позволяет независимо подключать, инициализировать и вызывать любую стратегию.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .base import BaseStrategy
from .cross_coin import CrossCoinStrategy
from .funding import FundingArbitrageStrategy
from .spread_arb import SpreadArbitrageStrategy
from .volume_oi import VolumeOIStrategy
from .zscore import ZScoreStrategy

if TYPE_CHECKING:
    from cryptoarb.engine import Engine


def build_strategies(engine: Engine, cfg: dict[str, Any]) -> dict[str, BaseStrategy]:
    """Фабрика стратегий: инстанцирует все модульные стратегии из конфигурации."""
    strategies: dict[str, BaseStrategy] = {
        "zscore": ZScoreStrategy(engine, cfg),
        "funding_arb": FundingArbitrageStrategy(engine, cfg),
        "cross_coin": CrossCoinStrategy(engine, cfg),
        "spread_arb": SpreadArbitrageStrategy(engine, cfg),
        "volume_oi": VolumeOIStrategy(engine, cfg),
    }
    return strategies


__all__ = [
    "BaseStrategy",
    "ZScoreStrategy",
    "FundingArbitrageStrategy",
    "CrossCoinStrategy",
    "SpreadArbitrageStrategy",
    "VolumeOIStrategy",
    "build_strategies",
]
