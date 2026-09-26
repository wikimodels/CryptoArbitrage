"""
Общий фундамент арбитража фандинга (Shared Core Funding Protocols).
Содержит математические модели дельта-нейтральности, протокол синхронизации ног
(Two-Phase Legging) и обработчик сбоев исполнения (Leg Failure Handler).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import logging
import time
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger("strategies.funding.common")


class LegState(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    REJECTED = "REJECTED"
    UNWOUND = "UNWOUND"


class LegSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(slots=True)
class LegFillResult:
    """Результат исполнения отдельной ноги связки."""
    exchange: str
    symbol: str
    side: LegSide
    requested_qty: float
    filled_qty: float
    avg_price: float
    state: LegState
    fee_usd: float = 0.0
    ts: float = field(default_factory=time.time)

    @property
    def filled_notional_usd(self) -> float:
        return self.filled_qty * self.avg_price

    @property
    def is_fully_filled(self) -> bool:
        return self.state == LegState.FILLED and abs(self.filled_qty - self.requested_qty) < 1e-6


@dataclass(slots=True)
class DeltaNeutralPair:
    """Модель дельта-нейтральной связки из двух ног."""
    pair_id: str
    strategy_type: str          # 'crypto_crypto' или 'utex_tradfi'
    primary_leg: LegFillResult  # Менее ликвидная / сложная нога (UTEX или сонный CEX)
    hedge_leg: LegFillResult    # Ликвидная нога-хедж (Binance / Bitget)
    target_notional_usd: float
    entry_basis_spread_pct: float
    funding_rate_primary: float
    funding_rate_hedge: float
    interval_hours: int = 8
    overnight_daily_pct: float = 0.0
    open_ts: float = field(default_factory=time.time)
    funding_accrued_usd: float = 0.0

    @property
    def net_delta_notional_usd(self) -> float:
        """
        Чистый дисбаланс дельты в долларах.
        При идеальной балансировке стремится к 0.0.
        """
        dir_p = +1.0 if self.primary_leg.side == LegSide.BUY else -1.0
        dir_h = +1.0 if self.hedge_leg.side == LegSide.BUY else -1.0
        return (self.primary_leg.filled_notional_usd * dir_p) + (self.hedge_leg.filled_notional_usd * dir_h)

    @property
    def is_delta_balanced(self) -> bool:
        """Допустимый порог рассинхрона дельты не более 1.0% от ноционала."""
        if self.target_notional_usd <= 0:
            return True
        return abs(self.net_delta_notional_usd) / self.target_notional_usd <= 0.01


class LegFailureHandler:
    """
    Обработчик нештатных ситуаций при исполнении ног:
    1. Нога 1 налита, нога 2 отклонена -> экстренный сброс ноги 1 (Emergency Unwind).
    2. Частичное исполнение -> выравнивание объемов до меньшего (Delta Equalization).
    3. Превышение порога проскальзывания -> сброс связки.
    """

    @staticmethod
    def evaluate_partial_fill(primary: LegFillResult, hedge: LegFillResult) -> Tuple[str, float]:
        """
        Определяет избыток дельты при частичном исполнении.
        Возвращает ('REDUCE_PRIMARY' / 'REDUCE_HEDGE', delta_qty_to_flatten).
        """
        notional_p = primary.filled_notional_usd
        notional_h = hedge.filled_notional_usd
        diff = abs(notional_p - notional_h)

        if diff < 1.0:  # Разница менее $1 — связка сбалансирована
            return ("BALANCED", 0.0)

        if notional_p > notional_h:
            # На первичной ноге куплено больше, чем покрыто хеджем -> сбросить излишек на primary
            excess_qty = (notional_p - notional_h) / primary.avg_price if primary.avg_price > 0 else 0.0
            log.warning(
                f"🚨 [PARTIAL FILL IMBALANCE] Primary ({primary.exchange}) has ${notional_p:.1f} vs "
                f"Hedge ({hedge.exchange}) ${notional_h:.1f}! Flattening {excess_qty:.4f} units on primary."
            )
            return ("REDUCE_PRIMARY", excess_qty)
        else:
            # На хедже продано больше -> сбросить излишек на hedge
            excess_qty = (notional_h - notional_p) / hedge.avg_price if hedge.avg_price > 0 else 0.0
            log.warning(
                f"🚨 [PARTIAL FILL IMBALANCE] Hedge ({hedge.exchange}) has ${notional_h:.1f} vs "
                f"Primary ({primary.exchange}) ${notional_p:.1f}! Flattening {excess_qty:.4f} units on hedge."
            )
            return ("REDUCE_HEDGE", excess_qty)

    @staticmethod
    def check_slippage(expected_price: float, actual_price: float, side: LegSide, max_slippage_bps: float = 15.0) -> bool:
        """Проверяет, не ушла ли цена дальше допустимого порога проскальзывания."""
        if expected_price <= 0 or actual_price <= 0:
            return False

        if side == LegSide.BUY:
            slip_bps = (actual_price - expected_price) / expected_price * 10000.0
        else:
            slip_bps = (expected_price - actual_price) / expected_price * 10000.0

        return slip_bps <= max_slippage_bps


class TwoLegSyncProtocol:
    """
    Протокол одновременного входа двух ног:
    Правило: сначала берется сложная нога (Primary), затем моментальный хедж (Hedge).
    """

    def __init__(self, timeout_ms: int = 500, max_slippage_bps: float = 15.0):
        self.timeout_ms = timeout_ms
        self.max_slippage_bps = max_slippage_bps

    def plan_entry(
        self,
        symbol_primary: str,
        exchange_primary: str,
        side_primary: LegSide,
        symbol_hedge: str,
        exchange_hedge: str,
        target_notional_usd: float,
        price_primary: float,
        price_hedge: float,
    ) -> Dict[str, Any]:
        """Формирует план синхронного входа с расчетом точных объемов по обеим ногам."""
        qty_primary = target_notional_usd / price_primary if price_primary > 0 else 0.0
        side_hedge = LegSide.SELL if side_primary == LegSide.BUY else LegSide.BUY
        qty_hedge = target_notional_usd / price_hedge if price_hedge > 0 else 0.0

        return {
            "primary": {
                "exchange": exchange_primary,
                "symbol": symbol_primary,
                "side": side_primary,
                "qty": qty_primary,
                "expected_price": price_primary,
                "order_type": "LIMIT_OR_IOC",
            },
            "hedge": {
                "exchange": exchange_hedge,
                "symbol": symbol_hedge,
                "side": side_hedge,
                "qty": qty_hedge,
                "expected_price": price_hedge,
                "order_type": "IOC_OR_MARKET",
            },
            "target_notional_usd": target_notional_usd,
            "max_slippage_bps": self.max_slippage_bps,
            "timeout_ms": self.timeout_ms,
        }


@dataclass(slots=True)
class OptimalSizeResult:
    """Результат расчета безопасного объема входа по глубине стакана."""
    optimal_size_usd: float              # Рекомендуемый размер разового входа (слайса)
    bottleneck_leg: str                  # 'long' или 'short' (слабая нога)
    bottleneck_exchange: str             # Биржа с наименьшей ликвидностью
    weak_depth_usd: float                # Доступный ноционал в топ-N слоях слабой ноги
    strong_depth_usd: float              # Доступный ноционал на сильной ноге
    participation_rate: float            # Доля взятия (например, 0.10)
    top_n_levels: int                    # Число слоев (например, 3)
    estimated_slippage_bps: float        # Ожидаемый сдвиг цены в bps
    is_tradable: bool                    # Хватает ли на минимальный размер (min_notional)
    reason: str = "ok"                   # Пояснение
    chunk_plan: Optional[Dict[str, Any]] = None  # План дробления для крупного депозита


class OrderBookSizer:
    """
    Калькулятор безопасного объема входа по микроструктуре стакана:
    1. Сканирует топ-N уровней стакана (по умолчанию 3 уровня) на обеих ногах.
    2. Отсекает уровни с аномальными дырами (цена ушла дальше max_level_slippage_bps).
    3. Рассчитывает безопасный объем: не более participation_rate (по умолчанию 10%) от топ-N уровней слабой ноги.
    4. Оценивает ожидаемый маркет-импакт (VWAP) и формирует план дробления (Chunking).
    """

    @staticmethod
    def calculate_depth_notional(levels, max_levels: int = 3, max_slippage_bps: float = 15.0) -> float:
        """Суммарный ноционал в топ-N уровнях с фильтром дыр в стакане."""
        if not levels:
            return 0.0
        best_price = levels[0].price
        if best_price <= 0:
            return 0.0

        total_notional = 0.0
        for lvl in levels[:max_levels]:
            if lvl.price <= 0 or lvl.size <= 0:
                continue
            slip_bps = abs(lvl.price - best_price) / best_price * 10000.0
            if slip_bps > max_slippage_bps:
                break
            total_notional += lvl.price * lvl.size

        return total_notional

    @staticmethod
    def calculate_optimal_entry_size(
        ob_long,
        ob_short,
        exch_long: str,
        exch_short: str,
        participation_rate: float = 0.10,
        top_n_levels: int = 3,
        max_level_slippage_bps: float = 15.0,
        min_notional_usd: float = 10.0,
        max_notional_usd: float = 5000.0,
        target_portfolio_usd: Optional[float] = None,
    ) -> OptimalSizeResult:
        """
        Рассчитывает оптимальный объем входа в дельта-нейтральную связку.
        Long нога покупает по Asks биржи exch_long.
        Short нога продает по Bids биржи exch_short.
        """
        if not ob_long or not ob_long.asks:
            return OptimalSizeResult(
                optimal_size_usd=0.0, bottleneck_leg="long", bottleneck_exchange=exch_long,
                weak_depth_usd=0.0, strong_depth_usd=0.0, participation_rate=participation_rate,
                top_n_levels=top_n_levels, estimated_slippage_bps=0.0, is_tradable=False,
                reason="empty_long_asks",
            )
        if not ob_short or not ob_short.bids:
            return OptimalSizeResult(
                optimal_size_usd=0.0, bottleneck_leg="short", bottleneck_exchange=exch_short,
                weak_depth_usd=0.0, strong_depth_usd=0.0, participation_rate=participation_rate,
                top_n_levels=top_n_levels, estimated_slippage_bps=0.0, is_tradable=False,
                reason="empty_short_bids",
            )

        depth_long = OrderBookSizer.calculate_depth_notional(ob_long.asks, top_n_levels, max_level_slippage_bps)
        depth_short = OrderBookSizer.calculate_depth_notional(ob_short.bids, top_n_levels, max_level_slippage_bps)

        cap_long = depth_long * participation_rate
        cap_short = depth_short * participation_rate

        if cap_long <= cap_short:
            bottleneck_leg = "long"
            bottleneck_exchange = exch_long
            weak_depth = depth_long
            strong_depth = depth_short
            raw_opt_size = cap_long
        else:
            bottleneck_leg = "short"
            bottleneck_exchange = exch_short
            weak_depth = depth_short
            strong_depth = depth_long
            raw_opt_size = cap_short

        if raw_opt_size < min_notional_usd:
            return OptimalSizeResult(
                optimal_size_usd=0.0, bottleneck_leg=bottleneck_leg, bottleneck_exchange=bottleneck_exchange,
                weak_depth_usd=weak_depth, strong_depth_usd=strong_depth, participation_rate=participation_rate,
                top_n_levels=top_n_levels, estimated_slippage_bps=0.0, is_tradable=False,
                reason=f"weak_leg_depth_too_thin ({raw_opt_size:.2f} < min {min_notional_usd:.2f})",
            )

        optimal_slice = min(raw_opt_size, max_notional_usd)

        # Оценка проскальзывания на слабой ноге при заборе optimal_slice
        levels_to_check = ob_long.asks if bottleneck_leg == "long" else ob_short.bids
        best_p = levels_to_check[0].price
        rem = optimal_slice
        tot_q = 0.0
        for lvl in levels_to_check:
            take_usd = min(rem, lvl.price * lvl.size)
            tot_q += take_usd / lvl.price
            rem -= take_usd
            if rem <= 0:
                break
        vwap = (optimal_slice / tot_q) if tot_q > 0 else best_p
        est_slip_bps = abs(vwap - best_p) / best_p * 10000.0

        # План дробления (Chunking), если целевой объем портфеля больше безопасного разового слайса
        chunk_plan = None
        if target_portfolio_usd and target_portfolio_usd > optimal_slice:
            num_chunks = int(target_portfolio_usd // optimal_slice)
            rem_chunk = target_portfolio_usd % optimal_slice
            if rem_chunk >= min_notional_usd:
                num_chunks += 1
            chunk_plan = {
                "target_total_usd": target_portfolio_usd,
                "slice_size_usd": optimal_slice,
                "recommended_chunks": max(1, num_chunks),
                "delay_between_chunks_sec": 45,
            }

        return OptimalSizeResult(
            optimal_size_usd=round(optimal_slice, 2),
            bottleneck_leg=bottleneck_leg,
            bottleneck_exchange=bottleneck_exchange,
            weak_depth_usd=round(weak_depth, 2),
            strong_depth_usd=round(strong_depth, 2),
            participation_rate=participation_rate,
            top_n_levels=top_n_levels,
            estimated_slippage_bps=round(est_slip_bps, 2),
            is_tradable=True,
            reason="ok",
            chunk_plan=chunk_plan,
        )

