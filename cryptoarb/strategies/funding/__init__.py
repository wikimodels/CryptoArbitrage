"""
Пакет арбитража фандинга (Funding Arbitrage Systems).
Разделен на 2 независимых под-проекта:
1. CryptoFundingStrategy: Межбиржевой крипто-фандинг (CEX <-> CEX, 24/7).
2. UTexFundingStrategy: TradFi-арбитраж фандинга (UTEX CME & US Stocks <-> Binance/Bitget).

Также экспортирует единые протоколы двух ног (TwoLegSyncProtocol, LegFailureHandler).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .common import (
    DeltaNeutralPair,
    LegFailureHandler,
    LegFillResult,
    LegSide,
    LegState,
    OptimalSizeResult,
    OrderBookSizer,
    TwoLegSyncProtocol,
)
from .crypto_funding import CryptoFundingStrategy
from .utex_funding import UTexFundingStrategy

if TYPE_CHECKING:
    from cryptoarb.engine import Engine


class FundingArbitrageStrategy(CryptoFundingStrategy):
    """
    Фасадный класс для полной обратной совместимости с существующим Engine и Dashboard.
    Делегирует сбор крипто-фандинга в CryptoFundingStrategy и агрегирует TradFi модуль UTexFundingStrategy.
    """

    def __init__(self, engine: Engine, cfg: dict[str, Any]):
        super().__init__(engine, cfg)
        self.tradfi_strat = UTexFundingStrategy(engine, cfg)

    def snapshot(self) -> dict[str, Any]:
        """Возвращает агрегированный снимок обоих под-проектов."""
        snap = super().snapshot()
        if self.tradfi_strat and self.tradfi_strat.enabled:
            snap["tradfi"] = self.tradfi_strat.snapshot()
        return snap


__all__ = [
    "LegState",
    "LegSide",
    "LegFillResult",
    "DeltaNeutralPair",
    "TwoLegSyncProtocol",
    "LegFailureHandler",
    "CryptoFundingStrategy",
    "UTexFundingStrategy",
    "FundingArbitrageStrategy",
    "OrderBookSizer",
    "OptimalSizeResult",
]
