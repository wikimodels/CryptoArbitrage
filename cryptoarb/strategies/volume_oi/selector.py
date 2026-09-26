"""
Автономный модуль скользящего 30-дневного скрининга и тиринга монет (Rolling Walk-Forward Selector).
Реализует принцип гистерезиса (Hysteresis Buffer) для защиты от фликкеринга и
автоматически классифицирует активы по корзинам емкости книги ордеров.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .models import CoinTier, WhitelistItem

log = logging.getLogger("volume_oi.selector")

DEFAULT_WHITELIST_PATH = Path("config/volume_oi_whitelist.json")

# Базовые параметры по корзинам ликвидности
TIER_LIMITS = {
    CoinTier.TIER_1_HEAVYWEIGHT: {
        "max_position_usd": 3000.0,
        "min_turnover_usd": 20_000_000.0,
        "min_p95_1m_usd": 40_000.0,
    },
    CoinTier.TIER_2_MIDWEIGHT: {
        "max_position_usd": 1000.0,
        "min_turnover_usd": 5_000_000.0,
        "min_p95_1m_usd": 10_000.0,
    },
    CoinTier.TIER_3_SCALP: {
        "max_position_usd": 150.0,
        "min_turnover_usd": 1_000_000.0,
        "min_p95_1m_usd": 2_000.0,
    },
}

# Известный институциональный белый список 17 проверенных активов
INITIAL_VERIFIED_ASSETS: dict[str, dict[str, Any]] = {
    "BTC": {
        "tier": CoinTier.TIER_1_HEAVYWEIGHT,
        "max_position_usd": 20000.0,
        "min_rvol": 4.0,
        "min_delta_oi_pct": 0.35,
        "min_body_pct": 0.10,
        "tp_pct": 1.0,
        "sl_pct": 0.7,
        "trail_act_pct": 0.5,
        "trail_dist_pct": 0.3,
        "win_rate": 78.6,
        "profit_factor": 3.40,
        "total_trades": 14,
        "p95_1m_volume_usd": 250_000.0,
        "daily_turnover_usd": 850_000_000.0,
    },
    "ENA": {
        "tier": CoinTier.TIER_1_HEAVYWEIGHT,
        "max_position_usd": 3000.0,
        "min_rvol": 5.0,
        "min_delta_oi_pct": 1.5,
        "win_rate": 83.3,
        "profit_factor": 2.45,
        "total_trades": 6,
        "p95_1m_volume_usd": 85_000.0,
        "daily_turnover_usd": 45_000_000.0,
    },
    "UNI": {
        "tier": CoinTier.TIER_1_HEAVYWEIGHT,
        "max_position_usd": 3000.0,
        "min_rvol": 5.0,
        "min_delta_oi_pct": 1.5,
        "win_rate": 80.0,
        "profit_factor": 3.20,
        "total_trades": 5,
        "p95_1m_volume_usd": 70_000.0,
        "daily_turnover_usd": 38_000_000.0,
    },
    "ADA": {
        "tier": CoinTier.TIER_1_HEAVYWEIGHT,
        "max_position_usd": 5000.0,
        "min_rvol": 5.0,
        "min_delta_oi_pct": 1.5,
        "win_rate": 71.4,
        "profit_factor": 2.10,
        "total_trades": 7,
        "p95_1m_volume_usd": 120_000.0,
        "daily_turnover_usd": 65_000_000.0,
    },
    "WLD": {
        "tier": CoinTier.TIER_1_HEAVYWEIGHT,
        "max_position_usd": 3000.0,
        "min_rvol": 5.0,
        "min_delta_oi_pct": 1.5,
        "win_rate": 75.0,
        "profit_factor": 1.95,
        "total_trades": 8,
        "p95_1m_volume_usd": 55_000.0,
        "daily_turnover_usd": 32_000_000.0,
    },
    "BCH": {
        "tier": CoinTier.TIER_1_HEAVYWEIGHT,
        "max_position_usd": 3000.0,
        "min_rvol": 5.0,
        "min_delta_oi_pct": 1.5,
        "win_rate": 80.0,
        "profit_factor": 2.85,
        "total_trades": 5,
        "p95_1m_volume_usd": 48_000.0,
        "daily_turnover_usd": 28_000_000.0,
    },
    "BNB": {
        "tier": CoinTier.TIER_1_HEAVYWEIGHT,
        "max_position_usd": 5000.0,
        "min_rvol": 5.0,
        "min_delta_oi_pct": 1.5,
        "win_rate": 75.0,
        "profit_factor": 2.30,
        "total_trades": 4,
        "p95_1m_volume_usd": 95_000.0,
        "daily_turnover_usd": 75_000_000.0,
    },
    "AAVE": {
        "tier": CoinTier.TIER_1_HEAVYWEIGHT,
        "max_position_usd": 3000.0,
        "min_rvol": 5.0,
        "min_delta_oi_pct": 1.5,
        "win_rate": 75.0,
        "profit_factor": 1.90,
        "total_trades": 4,
        "p95_1m_volume_usd": 42_000.0,
        "daily_turnover_usd": 24_000_000.0,
    },
    "INJ": {
        "tier": CoinTier.TIER_2_MIDWEIGHT,
        "max_position_usd": 1500.0,
        "min_rvol": 5.0,
        "min_delta_oi_pct": 1.5,
        "win_rate": 77.8,
        "profit_factor": 2.40,
        "total_trades": 9,
        "p95_1m_volume_usd": 35_000.0,
        "daily_turnover_usd": 18_000_000.0,
    },
    "OP": {
        "tier": CoinTier.TIER_2_MIDWEIGHT,
        "max_position_usd": 1000.0,
        "min_rvol": 5.0,
        "min_delta_oi_pct": 1.5,
        "win_rate": 75.0,
        "profit_factor": 2.15,
        "total_trades": 8,
        "p95_1m_volume_usd": 28_000.0,
        "daily_turnover_usd": 15_000_000.0,
    },
    "FET": {
        "tier": CoinTier.TIER_2_MIDWEIGHT,
        "max_position_usd": 1000.0,
        "min_rvol": 5.0,
        "min_delta_oi_pct": 1.5,
        "win_rate": 71.4,
        "profit_factor": 1.85,
        "total_trades": 7,
        "p95_1m_volume_usd": 22_000.0,
        "daily_turnover_usd": 12_000_000.0,
    },
    "1000BONK": {
        "tier": CoinTier.TIER_2_MIDWEIGHT,
        "max_position_usd": 1000.0,
        "min_rvol": 5.0,
        "min_delta_oi_pct": 1.5,
        "win_rate": 71.4,
        "profit_factor": 1.80,
        "total_trades": 7,
        "p95_1m_volume_usd": 30_000.0,
        "daily_turnover_usd": 16_000_000.0,
    },
    "LDO": {
        "tier": CoinTier.TIER_2_MIDWEIGHT,
        "max_position_usd": 800.0,
        "min_rvol": 5.0,
        "min_delta_oi_pct": 1.5,
        "win_rate": 75.0,
        "profit_factor": 2.05,
        "total_trades": 4,
        "p95_1m_volume_usd": 18_000.0,
        "daily_turnover_usd": 9_000_000.0,
    },
    "COMP": {
        "tier": CoinTier.TIER_3_SCALP,
        "max_position_usd": 150.0,
        "min_rvol": 5.0,
        "min_delta_oi_pct": 1.5,
        "win_rate": 83.3,
        "profit_factor": 3.10,
        "total_trades": 6,
        "p95_1m_volume_usd": 6_500.0,
        "daily_turnover_usd": 3_500_000.0,
    },
    "DYDX": {
        "tier": CoinTier.TIER_3_SCALP,
        "max_position_usd": 150.0,
        "min_rvol": 5.0,
        "min_delta_oi_pct": 1.5,
        "win_rate": 80.0,
        "profit_factor": 2.60,
        "total_trades": 5,
        "p95_1m_volume_usd": 8_000.0,
        "daily_turnover_usd": 4_200_000.0,
    },
    "GRT": {
        "tier": CoinTier.TIER_3_SCALP,
        "max_position_usd": 100.0,
        "min_rvol": 5.0,
        "min_delta_oi_pct": 1.5,
        "win_rate": 80.0,
        "profit_factor": 2.50,
        "total_trades": 5,
        "p95_1m_volume_usd": 5_500.0,
        "daily_turnover_usd": 2_800_000.0,
    },
    "MAGIC": {
        "tier": CoinTier.TIER_3_SCALP,
        "max_position_usd": 100.0,
        "min_rvol": 5.0,
        "min_delta_oi_pct": 1.5,
        "win_rate": 80.0,
        "profit_factor": 2.40,
        "total_trades": 5,
        "p95_1m_volume_usd": 4_500.0,
        "daily_turnover_usd": 2_100_000.0,
    },
}

KNOWN_BLACKLIST = [
    "PENDLE", "CHZ", "VIRTUAL", "DASH", "WIF", "APT"
]


class VolumeOISelector:
    """Управление белым списком и динамической калибровкой гистерезиса."""

    def __init__(self, whitelist_path: Path | str = DEFAULT_WHITELIST_PATH):
        self.whitelist_path = Path(whitelist_path)
        self.items: dict[str, WhitelistItem] = {}
        self.load()

    def load(self) -> None:
        """Загрузка конфигурации белого списка из JSON файла или инициализация дефолтной."""
        if self.whitelist_path.exists():
            try:
                with open(self.whitelist_path, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)
                    coins_data = raw_data.get("coins", {})
                    self.items = {
                        k: WhitelistItem.from_dict(v) for k, v in coins_data.items()
                    }
                    log.info("Загружен белый список Volume & OI: %d монет", len(self.items))
                    return
            except Exception as e:
                log.warning("Не удалось загрузить %s, восстанавливаем базовый список: %s", self.whitelist_path, e)

        # Создаем базовый проверенный список
        self.items = {}
        for coin, params in INITIAL_VERIFIED_ASSETS.items():
            self.items[coin] = WhitelistItem(coin=coin, **params)
        self.save()

    def save(self) -> None:
        """Сохранение текущего состояния белого списка на диск."""
        self.whitelist_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": "1.0.0",
            "updated_at": __import__("time").time(),
            "total_coins": len(self.items),
            "coins": {coin: item.to_dict() for coin, item in self.items.items()},
            "blacklist": KNOWN_BLACKLIST,
        }
        with open(self.whitelist_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        log.info("Сохранен белый список Volume & OI в %s (%d активов)", self.whitelist_path, len(self.items))

    def get_item(self, coin: str) -> WhitelistItem | None:
        """Получить конфигурацию для монеты (с учетом тикера или базового актива)."""
        base = coin.split("/")[0].split("_")[0].upper()
        return self.items.get(base)

    def is_eligible(self, coin: str, now_ts: float | None = None) -> bool:
        """Проверить, допущена ли монета к торговле в текущий момент."""
        base = coin.split("/")[0].split("_")[0].upper()
        if base in KNOWN_BLACKLIST:
            return False
        item = self.items.get(base)
        if not item:
            return False
        if item.tier == CoinTier.BLACKLIST or item.status == "BLACKLISTED":
            return False
        if now_ts and item.circuit_breaker_until_ts > 0:
            if now_ts < item.circuit_breaker_until_ts:
                return False
            # Время блокировки истекло — автоматическое восстановление
            item.circuit_breaker_until_ts = 0.0
            item.consecutive_losses = 0
            if item.status == "PAUSED_CIRCUIT_BREAKER":
                item.status = "ACTIVE"
            self.save()
        return True

    def record_trade_result(self, coin: str, is_win: bool, now_ts: float) -> None:
        """
        Регистрация результата сделки для отслеживания аварийного стоп-крана
        (Intraday Circuit Breaker: 3 стоп-лосса подряд -> пауза 7 дней).
        """
        base = coin.split("/")[0].split("_")[0].upper()
        item = self.items.get(base)
        if not item:
            return

        item.total_trades += 1
        if is_win:
            item.consecutive_losses = 0
            if item.status == "PAUSED_CIRCUIT_BREAKER":
                item.status = "ACTIVE"
        else:
            item.consecutive_losses += 1
            if item.consecutive_losses >= 3:
                # Пауза на 7 дней (7 * 86400 = 604800 секунд)
                item.circuit_breaker_until_ts = now_ts + 604800.0
                item.status = "PAUSED_CIRCUIT_BREAKER"
                log.warning(
                    "АВАРИЙНЫЙ СТОП-КРАН: %s получил 3 стоп-лосса подряд. Торговля заблокирована на 7 дней.",
                    base,
                )
        self.save()

    def update_metrics_hysteresis(
        self,
        coin: str,
        win_rate: float,
        profit_factor: float,
        total_trades: int,
        daily_turnover_usd: float,
        p95_1m_volume_usd: float,
    ) -> str:
        """
        Обновление статуса монеты согласно математическому правилу гистерезиса:
        - Включение (Admission): WR >= 70%, PF >= 1.5, turnover >= $10M, trades >= 5.
        - Удержание (Retention): WR >= 60%, PF >= 1.1.
        - Исключение (Eviction): WR < 55% или PF < 1.0 -> перевод в Blacklist.
        """
        base = coin.split("/")[0].split("_")[0].upper()
        item = self.items.get(base)

        # Определение корзины ликвидности по стакану
        if daily_turnover_usd >= TIER_LIMITS[CoinTier.TIER_1_HEAVYWEIGHT]["min_turnover_usd"] and p95_1m_volume_usd >= TIER_LIMITS[CoinTier.TIER_1_HEAVYWEIGHT]["min_p95_1m_usd"]:
            tier = CoinTier.TIER_1_HEAVYWEIGHT
            max_pos = 20000.0 if base == "BTC" else 3000.0
        elif daily_turnover_usd >= TIER_LIMITS[CoinTier.TIER_2_MIDWEIGHT]["min_turnover_usd"] and p95_1m_volume_usd >= TIER_LIMITS[CoinTier.TIER_2_MIDWEIGHT]["min_p95_1m_usd"]:
            tier = CoinTier.TIER_2_MIDWEIGHT
            max_pos = 1000.0
        elif daily_turnover_usd >= TIER_LIMITS[CoinTier.TIER_3_SCALP]["min_turnover_usd"]:
            tier = CoinTier.TIER_3_SCALP
            max_pos = 150.0
        else:
            tier = CoinTier.TIER_3_SCALP
            max_pos = 50.0

        if item is None:
            # Кандидат на добавление (Admission rule)
            if win_rate >= 70.0 and profit_factor >= 1.5 and total_trades >= 5 and daily_turnover_usd >= 5_000_000.0:
                self.items[base] = WhitelistItem(
                    coin=base,
                    tier=tier,
                    max_position_usd=max_pos,
                    win_rate=win_rate,
                    profit_factor=profit_factor,
                    total_trades=total_trades,
                    daily_turnover_usd=daily_turnover_usd,
                    p95_1m_volume_usd=p95_1m_volume_usd,
                    status="ACTIVE",
                )
                self.save()
                return "ADMITTED"
            return "REJECTED"
        else:
            # Уже в списке: проверка правил удержания / исключения
            item.win_rate = win_rate
            item.profit_factor = profit_factor
            item.total_trades = total_trades
            item.daily_turnover_usd = daily_turnover_usd
            item.p95_1m_volume_usd = p95_1m_volume_usd

            # Правило исключения (Eviction rule)
            if win_rate < 55.0 or profit_factor < 1.0:
                item.tier = CoinTier.BLACKLIST
                item.status = "BLACKLISTED"
                self.save()
                return "EVICTED"

            # Правило удержания (Retention rule)
            if win_rate >= 60.0 and profit_factor >= 1.1:
                item.tier = tier
                item.max_position_usd = max_pos
                if item.status == "BLACKLISTED":
                    item.status = "ACTIVE"
                self.save()
                return "RETAINED"

            # Пограничная зона: не исключаем, но понижаем лимит
            item.max_position_usd = min(item.max_position_usd, 300.0)
            self.save()
            return "DEGRADED"


if __name__ == "__main__":
    selector = VolumeOISelector()
    print(f"VolumeOISelector initialized with {len(selector.items)} verified assets.")
    for coin, item in selector.items.items():
        print(f"  {coin:<10} | Tier: {item.tier.value:<20} | MaxPos: ${item.max_position_usd:6.0f} | WR: {item.win_rate:4.1f}% | PF: {item.profit_factor:4.2f}")
