"""
Под-проект 2: TradFi-арбитраж фандинга и базиса (UTEX CME & US Stocks <-> Binance/Bitget).
Специфика: часы сессий NYSE/CME, асимметрия овернайтов (0.0% на акциях 1:1 vs 0.0194% на фьючерсах),
4-часовые интервалы на газе, порционное исполнение лесенкой и скальпинг раздвижки спреда (сетап Антона).
Спецификация: docs/FUNDING_ARBITRAGE_SYSTEM_DESIGN.md (Раздел 4).
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from cryptoarb.strategies.base import BaseStrategy
from cryptoarb.strategies.funding.common import LegFailureHandler, LegSide, TwoLegSyncProtocol

if TYPE_CHECKING:
    from cryptoarb.engine import Engine

log = logging.getLogger("strategies.funding.utex")


class UTexFundingStrategy(BaseStrategy):
    """
    Стратегия арбитража фандинга и базиса TradFi (UTEX <-> Binance / Bitget).
    """

    DEFAULT_SPECS = {
        "NATGAS": {
            "utex_ticker": "GASM",
            "crypto_ticker": "NATGASUSDT",
            "asset_class": "COMMODITY_FUTURES",
            "interval_hours": 4,          # 6 выплат в сутки на Binance
            "overnight_daily_pct": 0.0194, # ~7% годовых на ВЕСЬ ноционал
            "min_profitable_funding_pct": 0.010,
            "lot_size": 1000,
        },
        "CL": {
            "utex_ticker": "CL",
            "crypto_ticker": "CLUSDT",
            "asset_class": "COMMODITY_FUTURES",
            "interval_hours": 8,
            "overnight_daily_pct": 0.0194,
            "min_profitable_funding_pct": 0.015,
            "lot_size": 1000,
        },
        "NOK": {
            "utex_ticker": "NOK",
            "crypto_ticker": "NOKUSDT",
            "asset_class": "US_EQUITY",
            "interval_hours": 8,
            "overnight_daily_pct": 0.0000, # 0.00% при работе 1:1 без плеча!
            "min_profitable_funding_pct": 0.020,
        },
        "AXTI": {
            "utex_ticker": "AXTI",
            "crypto_ticker": "AXTIUSDT",
            "asset_class": "US_EQUITY",
            "interval_hours": 8,
            "overnight_daily_pct": 0.0000,
            "min_profitable_funding_pct": 0.020,
        },
        "MSTR": {
            "utex_ticker": "MSTR",
            "crypto_ticker": "MSTRUSDT",
            "asset_class": "US_EQUITY",
            "interval_hours": 8,
            "overnight_daily_pct": 0.0000,
            "min_profitable_funding_pct": 0.020,
        },
        "TSLA": {
            "utex_ticker": "TSLA",
            "crypto_ticker": "TSLAUSDT",
            "asset_class": "US_EQUITY",
            "interval_hours": 8,
            "overnight_daily_pct": 0.0000,
            "min_profitable_funding_pct": 0.020,
        },
    }

    def __init__(self, engine: Engine, cfg: dict[str, Any]):
        super().__init__(engine, cfg)
        self.name = "utex_funding"
        self.display_name = "Утекс-Традфай Фандинг (UTEX <-> CEX)"

        fa = cfg.get("funding_arb", {})
        self.enabled = bool(fa.get("tradfi_enabled", True))
        self.position_size = float(fa.get("position_size_usdt", 5000.0))
        self.exit_threshold_pct = float(fa.get("tradfi_exit_threshold_pct", 0.015))
        self.basis_scalp_bps = float(fa.get("tradfi_basis_scalp_bps", 30.0))
        self.specs = self.DEFAULT_SPECS

        self.sync_protocol = TwoLegSyncProtocol(timeout_ms=1000, max_slippage_bps=20.0)
        self._active_tradfi_pairs: Dict[str, Dict[str, Any]] = {}
        self.last_scan_ts = 0.0

    @staticmethod
    def is_market_open(asset_class: str, now_dt: Optional[datetime] = None) -> bool:
        """
        Проверка открыта ли сессия на UTEX для соответствующего класса активов.
        NYSE/NASDAQ открыты Пн-Пт 13:30 - 20:00 UTC.
        CME фьючерсы открыты Пн-Пт с перерывом 21:00 - 22:00 UTC.
        """
        dt = now_dt or datetime.now(timezone.utc)
        weekday = dt.weekday()  # 0=Пн, 4=Пт, 5=Сб, 6=Вс

        # Выходные дни (Суббота, большая часть Воскресенья)
        if weekday == 5:
            return False
        if weekday == 6 and dt.hour < 22:
            return False
        if weekday == 4 and dt.hour >= 21:
            return False

        if asset_class == "US_EQUITY":
            # Основная сессия акций США: 13:30 - 20:00 UTC (плюс пре-/постмаркет)
            return (dt.hour > 13 or (dt.hour == 13 and dt.minute >= 30)) and dt.hour < 21
        elif asset_class == "COMMODITY_FUTURES":
            # CME фьючерсы торгуются круглосуточно с перерывом 21:00-22:00 UTC
            return dt.hour != 21

        return True

    def calculate_net_daily_yield(self, symbol: str, funding_rate: float, is_unleveraged: bool = True) -> Dict[str, float]:
        """
        Расчет чистой суточной доходности с учетом асимметрии овернайтов UTEX.
        Для акций 1:1 овернайт = 0.0%.
        Для фьючерсов овернайт = 0.0194% на весь объем.
        """
        spec = self.specs.get(symbol.upper(), {})
        interval_h = spec.get("interval_hours", 8)
        intervals_per_day = 24 // interval_h
        daily_funding = abs(funding_rate) * 100.0 * intervals_per_day

        overnight = 0.0
        if spec.get("asset_class") == "COMMODITY_FUTURES":
            overnight = spec.get("overnight_daily_pct", 0.0194)
        elif spec.get("asset_class") == "US_EQUITY":
            overnight = 0.0 if is_unleveraged else spec.get("overnight_daily_pct", 0.0278)

        net_daily = daily_funding - overnight
        annualized = net_daily * 365.0

        return {
            "daily_funding_pct": round(daily_funding, 4),
            "overnight_daily_pct": round(overnight, 4),
            "net_daily_yield_pct": round(net_daily, 4),
            "annualized_net_pct": round(annualized, 1),
            "intervals_per_day": intervals_per_day,
        }

    def evaluate_basis_scalp(self, cex_price: float, utex_price: float) -> Tuple[bool, float, str]:
        """
        Детектор сетапа Антона: скальпинг раздвижки спреда (базиса) между отсечками фандинга.
        Возвращает (is_scalp_opportunity, basis_bps, recommended_action).
        """
        if cex_price <= 0 or utex_price <= 0:
            return (False, 0.0, "NONE")

        basis_bps = (cex_price - utex_price) / utex_price * 10000.0

        # Если спред раздвинулся шире установленного порога (например > 30 bps)
        if abs(basis_bps) >= self.basis_scalp_bps:
            action = "SCALP_SHORT_CEX_LONG_UTEX" if basis_bps > 0 else "SCALP_LONG_CEX_SHORT_UTEX"
            return (True, round(basis_bps, 1), action)

        return (False, round(basis_bps, 1), "HOLD")

    def plan_laddered_entry(self, total_notional_usd: float, chunk_size_usd: float = 10000.0) -> List[float]:
        """
        Алгоритм дробления крупной позиции на микро-чанки (лесенка) для тонкого стакана UTEX.
        Защищает от пробития стакана ('уронить стакан').
        """
        chunks = []
        rem = total_notional_usd
        while rem > 0:
            sz = min(rem, chunk_size_usd)
            chunks.append(sz)
            rem -= sz
        return chunks

    def snapshot(self) -> dict[str, Any]:
        """Формирование аналитики для TradFi связок."""
        items = []
        now_dt = datetime.now(timezone.utc)

        for sym, spec in self.specs.items():
            market_open = self.is_market_open(spec.get("asset_class", ""), now_dt)
            calc = self.calculate_net_daily_yield(sym, funding_rate=0.0015)  # Пример 0.15%

            items.append({
                "symbol": sym,
                "utex_ticker": spec["utex_ticker"],
                "crypto_ticker": spec["crypto_ticker"],
                "asset_class": spec["asset_class"],
                "interval_hours": spec["interval_hours"],
                "is_market_open": market_open,
                "overnight_daily_pct": calc["overnight_daily_pct"],
                "est_net_daily_pct": calc["net_daily_yield_pct"],
                "est_annualized_pct": calc["annualized_net_pct"],
            })

        return {
            "subproject": "utex_tradfi",
            "enabled": self.enabled,
            "position_size_usdt": self.position_size,
            "specs_count": len(self.specs),
            "instruments": items,
        }
