"""
Стратегия межмонетного статистического арбитража (Cross-Coin Cointegration & Pairs Trading).
Отслеживает синтетические спреды и соотношения цен фундаментально связанных активов
(L1-блокчейны, токены L2, экосистемные корзины DeFi и Memecoins).
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque
from typing import TYPE_CHECKING, Any

from cryptoarb.strategies.base import BaseStrategy

if TYPE_CHECKING:
    from cryptoarb.engine import Engine

log = logging.getLogger("strategies.cross_coin")

# Дефолтные фундаментально связанные пары для межмонетного арбитража
DEFAULT_CROSS_PAIRS = [
    # Layer 1
    ("SOL/USDT:USDT", "AVAX/USDT:USDT", "Layer 1: Solana vs Avalanche"),
    ("ETH/USDT:USDT", "SOL/USDT:USDT", "Layer 1: Ethereum vs Solana"),
    ("BTC/USDT:USDT", "BCH/USDT:USDT", "Bitcoin Core vs Cash"),
    ("NEAR/USDT:USDT", "APT/USDT:USDT", "Layer 1: Near vs Aptos"),
    ("SUI/USDT:USDT", "APT/USDT:USDT", "Move VM: Sui vs Aptos"),
    # Layer 2
    ("ARB/USDT:USDT", "OP/USDT:USDT", "Ethereum L2: Arbitrum vs Optimism"),
    # DeFi
    ("UNI/USDT:USDT", "AAVE/USDT:USDT", "DeFi Bluechips: Uniswap vs Aave"),
    ("MKR/USDT:USDT", "AAVE/USDT:USDT", "DeFi Lending: Maker vs Aave"),
    # Memes
    ("DOGE/USDT:USDT", "SHIB/USDT:USDT", "Meme Leaders: Doge vs Shiba"),
    ("PEPE/USDT:USDT", "BONK/USDT:USDT", "Modern Memes: Pepe vs Bonk"),
]


class CrossCoinPairState:
    """Хранит скользящую историю отношения цен двух монет."""

    def __init__(self, sym_a: str, sym_b: str, label: str, max_points: int = 1440):
        self.sym_a = sym_a
        self.sym_b = sym_b
        self.label = label
        self.history: deque[float] = deque(maxlen=max_points)
        self.last_ts: float = 0.0
        self.mean: float = 0.0
        self.std: float = 0.0

    def update(self, price_a: float, price_b: float, now: float) -> float | None:
        if price_a <= 0 or price_b <= 0:
            return None
        ratio = price_a / price_b
        # Добавляем точку не чаще одного раза в минуту
        if now - self.last_ts >= 60.0 or not self.history:
            self.history.append(ratio)
            self.last_ts = now
            n = len(self.history)
            if n >= 10:
                self.mean = sum(self.history) / n
                var = sum((x - self.mean) ** 2 for x in self.history) / n
                self.std = math.sqrt(var)

        if self.std > 1e-9:
            return (ratio - self.mean) / self.std
        return 0.0


class CrossCoinStrategy(BaseStrategy):
    """Модуль межмонетного статистического арбитража."""

    def __init__(self, engine: Engine, cfg: dict[str, Any]):
        super().__init__(engine, cfg)
        self.name = "cross_coin"
        self.display_name = "Межмонетный арбитраж"

        cc = cfg.get("cross_coin", {})
        self.enabled = bool(cc.get("enabled", True))
        self.entry_z = float(cc.get("entry_z", 2.5))
        self.exit_z = float(cc.get("exit_z", 0.0))
        self.position_size = float(cc.get("position_size_usdt", 10.0))

        self.pairs: list[CrossCoinPairState] = [
            CrossCoinPairState(p[0], p[1], p[2]) for p in DEFAULT_CROSS_PAIRS
        ]
        self.radar_cache: list[dict[str, Any]] = []

    async def start(self) -> None:
        log.info("CrossCoinStrategy инициализирована: %d пар отслеживаются", len(self.pairs))

    def on_price_tick(self, symbol: str, exchange: str, mid_price: float, now_sec: float) -> None:
        pass  # Отношения рассчитываются синхронно по свежим котировкам на скане

    async def on_scan_symbol(self, symbol: str, quotes: dict, now: float) -> None:
        pass

    async def check_exits(self, symbol: str, quotes: dict, now: float) -> None:
        pass

    def _get_mid_price(self, symbol: str) -> float | None:
        quotes = self.engine.state.fresh_quotes(symbol, self.engine.exchanges)
        if not quotes:
            return None
        mids = [(q.best_bid + q.best_ask) / 2.0 for q in quotes.values() if q.best_bid > 0 and q.best_ask > 0]
        return sum(mids) / len(mids) if mids else None

    def snapshot(self) -> dict[str, Any]:
        """Возвращает срез радара межмонетных аномалий для веб-дашборда."""
        now = time.time()
        radar = []

        for p in self.pairs:
            qa = self._get_mid_price(p.sym_a)
            qb = self._get_mid_price(p.sym_b)
            if not qa or not qb or qa <= 0 or qb <= 0:
                continue

            z = p.update(qa, qb, now)
            ratio = qa / qb
            abs_z = abs(z) if z is not None else 0.0

            action = "Норма"
            if z is not None:
                if z >= self.entry_z:
                    action = f"SHORT {p.sym_a.split('/')[0]} / LONG {p.sym_b.split('/')[0]}"
                elif z <= -self.entry_z:
                    action = f"LONG {p.sym_a.split('/')[0]} / SHORT {p.sym_b.split('/')[0]}"

            radar.append({
                "coin_a": p.sym_a.split("/")[0],
                "coin_b": p.sym_b.split("/")[0],
                "label": p.label,
                "price_a": round(qa, 4),
                "price_b": round(qb, 4),
                "ratio": round(ratio, 4),
                "mean_ratio": round(p.mean, 4),
                "z_score": round(z, 2) if z is not None else 0.0,
                "abs_z": round(abs_z, 2),
                "history_points": len(p.history),
                "action": action,
                "is_signal": abs_z >= self.entry_z,
            })

        radar.sort(key=lambda x: x["abs_z"], reverse=True)
        return {
            "enabled": self.enabled,
            "entry_z": self.entry_z,
            "exit_z": self.exit_z,
            "pairs_count": len(self.pairs),
            "radar": radar,
        }
