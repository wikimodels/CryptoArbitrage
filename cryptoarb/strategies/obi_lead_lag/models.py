"""
Ultra-lightweight models for OBI Lead-Lag (Render Free Tier: < 50MB RAM).
Uses __slots__ to eliminate dictionary overhead and prevent GC fragmentation.
"""

from dataclasses import dataclass, field
import time
from typing import Optional, List, Tuple


@dataclass(slots=True)
class OrderBookDepth5:
    exchange: str
    symbol: str
    ts: float  # local epoch timestamp
    exchange_ts: int  # exchange epoch ms
    bids: List[Tuple[float, float]]  # [(price, qty), ...] up to 5
    asks: List[Tuple[float, float]]  # [(price, qty), ...] up to 5

    @property
    def best_bid(self) -> float:
        return self.bids[0][0] if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0][0] if self.asks else 0.0

    @property
    def mid_price(self) -> float:
        bb = self.best_bid
        ba = self.best_ask
        return (bb + ba) / 2.0 if (bb and ba) else 0.0

    @property
    def spread_bps(self) -> float:
        mid = self.mid_price
        if not mid or not self.bids or not self.asks:
            return 0.0
        return (self.best_ask - self.best_bid) / mid * 10000.0

    @property
    def bid_vol_5(self) -> float:
        return sum(qty for _, qty in self.bids)

    @property
    def ask_vol_5(self) -> float:
        return sum(qty for _, qty in self.asks)

    @property
    def obi(self) -> float:
        """
        Order Book Imbalance for Top 5 levels:
        OBI = (BidVol - AskVol) / (BidVol + AskVol) in [-1.0, +1.0]
        +0.60 corresponds to 80/20 ratio in favor of buyers.
        -0.60 corresponds to 80/20 ratio in favor of sellers.
        """
        bv = self.bid_vol_5
        av = self.ask_vol_5
        total = bv + av
        if total <= 1e-9:
            return 0.0
        return (bv - av) / total


@dataclass(slots=True)
class OBISignal:
    id: str
    symbol: str
    side: str  # 'BUY' or 'SELL'
    lead_exchange: str
    lag_exchange: str
    lead_obi: float
    lead_mid: float
    lag_mid: float
    lag_best_bid: float
    lag_best_ask: float
    lead_lag_bps: float
    suggested_limit_price: float
    created_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class PaperTrade:
    id: str
    symbol: str
    side: str  # 'BUY' or 'SELL'
    exchange: str
    limit_price: float
    notional_usd: float
    entry_ts: float
    status: str = "PENDING"  # PENDING, FILLED, EXPIRED, CLOSED
    fill_price: float = 0.0
    fill_ts: float = 0.0
    exit_price: float = 0.0
    exit_ts: float = 0.0
    exit_reason: str = ""
    gross_pnl_bps: float = 0.0
    net_pnl_usd: float = 0.0
