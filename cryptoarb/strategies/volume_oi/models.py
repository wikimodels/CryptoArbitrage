"""
Модели данных, перечисления и структуры конфигурации для стратегии
Volume Spike & Open Interest Momentum (Sniper Maker).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class CoinTier(str, Enum):
    """Категория ликвидности и емкости актива."""
    TIER_1_HEAVYWEIGHT = "TIER_1_HEAVYWEIGHT"  # Емкость $1,000 - $5,000 (BTC до $20,000)
    TIER_2_MIDWEIGHT = "TIER_2_MIDWEIGHT"      # Емкость $300 - $1,500
    TIER_3_SCALP = "TIER_3_SCALP"              # Емкость $50 - $150
    BLACKLIST = "BLACKLIST"                    # Запрещено к торговле


@dataclass
class WhitelistItem:
    """Элемент реестра проверенных активов."""
    coin: str
    tier: CoinTier = CoinTier.TIER_2_MIDWEIGHT
    max_position_usd: float = 1000.0
    min_rvol: float = 5.0
    min_delta_oi_pct: float = 1.5
    min_body_pct: float = 0.15
    tp_pct: float = 1.5
    sl_pct: float = 1.0
    trail_act_pct: float = 0.8
    trail_dist_pct: float = 0.5
    daily_turnover_usd: float = 0.0
    p95_1m_volume_usd: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    total_trades: int = 0
    consecutive_losses: int = 0
    circuit_breaker_until_ts: float = 0.0
    status: str = "ACTIVE"  # ACTIVE, PAUSED_CIRCUIT_BREAKER, BLACKLISTED

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["tier"] = self.tier.value if isinstance(self.tier, CoinTier) else str(self.tier)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WhitelistItem:
        tier_val = data.get("tier", CoinTier.TIER_2_MIDWEIGHT.value)
        try:
            tier = CoinTier(tier_val)
        except Exception:
            tier = CoinTier.TIER_2_MIDWEIGHT
        copied = dict(data)
        copied["tier"] = tier
        return cls(**copied)


@dataclass
class VolumeOISignal:
    """Торговый сигнал институционального всплеска с открытым интересом."""
    coin: str
    direction: int  # +1 = LONG, -1 = SHORT
    timestamp: float
    rvol: float
    delta_oi_pct: float
    trend_ema: float
    close_price: float
    open_price: float
    limit_price: float
    hard_sl_price: float
    tp_price: float
    exchanges_confirmed: list[str] = field(default_factory=list)
    tier: str = "TIER_2_MIDWEIGHT"
    confidence_score: float = 1.0


@dataclass
class PositionState:
    """Состояние активной заявки или открытой позиции стратегии."""
    position_id: str
    coin: str
    direction: int  # +1 = LONG, -1 = SHORT
    entry_price: float
    entry_ts: float
    size_usd: float
    status: str = "PENDING_LIMIT"  # PENDING_LIMIT, OPEN, CLOSED
    limit_order_ts: float = 0.0
    limit_price: float = 0.0
    tp_price: float = 0.0
    hard_sl_price: float = 0.0
    trail_act_pct: float = 0.8
    trail_dist_pct: float = 0.5
    trailing_active: bool = False
    peak_price: float = 0.0
    current_stop_price: float = 0.0
    exit_ts: float | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    gross_pnl_pct: float = 0.0
    net_pnl_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VolumeOIConfig:
    """Глобальная конфигурация стратегии."""
    enabled: bool = True
    whitelist_file: str = "config/volume_oi_whitelist.json"
    pullback_frac: float = 0.30
    order_timeout_sec: float = 300.0       # 5 минут таймаут ожидания лимита
    tp_pct: float = 1.5                    # +1.5% TP (для альтов)
    sl_pct: float = 1.0                    # -1.0% SL (для альтов)
    trail_activation_pct: float = 0.8      # +0.8% трейлинг
    trail_distance_pct: float = 0.5        # 0.5% дистанция
    timestop_sec: float = 3600.0           # 60 минут тайм-стоп
    cooldown_sec: float = 900.0            # 15 минут кулдаун
    min_rvol: float = 5.0
    min_delta_oi_pct: float = 1.5
    min_exchanges: int = 2
    min_body_pct: float = 0.15
    position_size_usd: float = 10.0        # строго $10 по умолчанию
