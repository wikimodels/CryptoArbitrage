"""
Фасадный модуль стратегий арбитража фандинга.
Разделен на 2 независимых под-проекта:
1. CryptoFundingStrategy: Межбиржевой крипто-фандинг (CEX <-> CEX, 24/7).
2. UTexFundingStrategy: TradFi-арбитраж фандинга (UTEX CME & US Stocks <-> Binance/Bitget).

Детальная архитектурная документация: docs/FUNDING_ARBITRAGE_SYSTEM_DESIGN.md
"""
from __future__ import annotations

from cryptoarb.strategies.funding import (
    CryptoFundingStrategy,
    DeltaNeutralPair,
    FundingArbitrageStrategy,
    LegFailureHandler,
    LegFillResult,
    LegSide,
    LegState,
    TwoLegSyncProtocol,
    UTexFundingStrategy,
)

__all__ = [
    "FundingArbitrageStrategy",
    "CryptoFundingStrategy",
    "UTexFundingStrategy",
    "DeltaNeutralPair",
    "TwoLegSyncProtocol",
    "LegFailureHandler",
    "LegFillResult",
    "LegSide",
    "LegState",
]
