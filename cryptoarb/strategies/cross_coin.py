"""
Стратегия межмонетного статистического арбитража (Cross-Coin Cointegration & Pairs Trading).
Отслеживает синтетические спреды и соотношения цен фундаментально связанных активов
на основе динамически обновляемого реестра Walk-Forward рекалибровки.
"""
from __future__ import annotations

import json
import logging
import math
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptoarb.strategies.base import BaseStrategy

if TYPE_CHECKING:
    from cryptoarb.engine import Engine

log = logging.getLogger("strategies.cross_coin")

# Резервный список пар на случай отсутствия сформированного файла реестра
DEFAULT_CROSS_PAIRS = [
    ("ACE/USDT:USDT", "ONG/USDT:USDT", "WFA Top 1: ACE / ONG (WR 76.9%, +212.8% к марже, WFE 148%)", 1.0, 0.0, 2.5, 147.9, 4.255, 76.9, 1.2),
    ("LDO/USDT:USDT", "SNXX/USDT:USDT", "WFA Top 2: LDO / SNXX (WR 40.9%, +187.7% к марже, WFE 315%)", 1.0, 0.0, 2.5, 315.1, 3.754, 40.9, 2.4),
    ("LDO/USDT:USDT", "MUU/USDT:USDT", "WFA Top 3: LDO / MUU (WR 66.7%, +105.8% к марже, WFE 126%)", 1.0, 0.0, 2.5, 125.9, 2.116, 66.7, 3.1),
    ("ONG/USDT:USDT", "TRUMP/USDT:USDT", "WFA Top 4: ONG / TRUMP (WR 64.0%, +102.7% к марже, WFE 98%)", 1.0, 0.0, 2.5, 98.3, 2.053, 64.0, 1.8),
    ("LDO/USDT:USDT", "TIA/USDT:USDT", "WFA Top 5: LDO / TIA (WR 57.1%, +55.2% к марже, WFE 107%)", 1.0, 0.0, 2.5, 106.6, 1.105, 57.1, 2.9),
    ("ADA/USDT:USDT", "SHIB/USDT:USDT", "WFA Top 6: ADA / SHIB (WR 62.5%, +22.3% к марже, WFE 67%)", 1.0, 0.0, 2.5, 66.7, 0.447, 62.5, 4.2),
]


class CrossCoinPairState:
    """Хранит скользящую историю спреда и параметры WFA для конкретной пары."""

    def __init__(
        self,
        sym_a: str,
        sym_b: str,
        label: str,
        beta: float = 1.0,
        alpha: float = 0.0,
        entry_z: float = 2.5,
        wfe_pct: float = 0.0,
        oos_pnl_usdt: float = 0.0,
        oos_win_rate: float = 0.0,
        half_life_hours: float = 0.0,
        max_points: int = 1440,
    ):
        self.sym_a = sym_a
        self.sym_b = sym_b
        self.label = label
        self.beta = beta
        self.alpha = alpha
        self.entry_z = entry_z
        self.wfe_pct = wfe_pct
        self.oos_pnl_usdt = oos_pnl_usdt
        self.oos_win_rate = oos_win_rate
        self.half_life_hours = half_life_hours
        self.history: deque[float] = deque(maxlen=max_points)
        self.last_ts: float = 0.0
        self.mean: float = 0.0
        self.std: float = 0.0
        self.is_draining: bool = False  # Флаг Graceful Exit: пара выводится, доводятся только старые сделки
        self.has_open_position: bool = False

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
    """Модуль межмонетного статистического арбитража с поддержкой суточной ротации пар."""

    def __init__(self, engine: Engine, cfg: dict[str, Any]):
        super().__init__(engine, cfg)
        self.name = "cross_coin"
        self.display_name = "Межмонетный арбитраж"

        cc = cfg.get("cross_coin", {})
        self.enabled = bool(cc.get("enabled", True))
        self.default_entry_z = float(cc.get("entry_z", 2.5))
        self.exit_z = float(cc.get("exit_z", 0.0))
        self.position_size = float(cc.get("position_size_usdt", 10.0))

        self.registry_file = Path(cc.get("registry_file", "output/active_cross_pairs.json"))
        self.last_registry_mtime: float = 0.0
        self.last_recalibration_time: str = "N/A"

        self.pairs_map: dict[tuple[str, str], CrossCoinPairState] = {}
        self._load_registry()

    def _load_registry(self) -> None:
        """Загружает проверенный реестр пар или использует дефолтные значения."""
        if self.registry_file.exists():
            try:
                mtime = self.registry_file.stat().st_mtime
                with open(self.registry_file, "r", encoding="utf-8") as f:
                    reg = json.load(f)

                self.last_registry_mtime = mtime
                self.last_recalibration_time = reg.get("updated_at", "N/A")

                new_pairs_keys = set()
                for p in reg.get("pairs", []):
                    key = (p["sym_a"], p["sym_b"])
                    new_pairs_keys.add(key)
                    if key in self.pairs_map:
                        # Обновляем калиброванные параметры существующей пары без сброса накопленной истории
                        state = self.pairs_map[key]
                        state.label = p.get("label", state.label)
                        state.beta = p.get("beta", state.beta)
                        state.alpha = p.get("alpha", state.alpha)
                        state.entry_z = p.get("entry_z", state.entry_z)
                        state.wfe_pct = p.get("wfe_pct", state.wfe_pct)
                        state.oos_pnl_usdt = p.get("oos_pnl_usdt", state.oos_pnl_usdt)
                        state.oos_win_rate = p.get("oos_win_rate", state.oos_win_rate)
                        state.half_life_hours = p.get("half_life_hours", state.half_life_hours)
                        state.is_draining = False
                    else:
                        self.pairs_map[key] = CrossCoinPairState(
                            sym_a=p["sym_a"],
                            sym_b=p["sym_b"],
                            label=p.get("label", f"{p['coin_a']}/{p['coin_b']}"),
                            beta=p.get("beta", 1.0),
                            alpha=p.get("alpha", 0.0),
                            entry_z=p.get("entry_z", self.default_entry_z),
                            wfe_pct=p.get("wfe_pct", 0.0),
                            oos_pnl_usdt=p.get("oos_pnl_usdt", 0.0),
                            oos_win_rate=p.get("oos_win_rate", 0.0),
                            half_life_hours=p.get("half_life_hours", 0.0),
                        )

                # Обработка Graceful Exit: помечаем исключенные пары как is_draining
                for key, state in list(self.pairs_map.items()):
                    if key not in new_pairs_keys:
                        if state.has_open_position:
                            state.is_draining = True
                            log.info("Пара %s / %s исключена из реестра -> переведена в Graceful Exit", key[0], key[1])
                        else:
                            del self.pairs_map[key]

                log.info("Реестр пар загружен из %s: %d активных пар", self.registry_file, len(self.pairs_map))
                return
            except Exception as e:
                log.error("Ошибка загрузки реестра %s: %s", self.registry_file, e)

        # Fallback если файл не найден или поврежден
        if not self.pairs_map:
            for p in DEFAULT_CROSS_PAIRS:
                key = (p[0], p[1])
                self.pairs_map[key] = CrossCoinPairState(
                    sym_a=p[0], sym_b=p[1], label=p[2],
                    beta=p[3], alpha=p[4], entry_z=p[5],
                    wfe_pct=p[6], oos_pnl_usdt=p[7], oos_win_rate=p[8], half_life_hours=p[9],
                )

    def _check_hot_reload(self) -> None:
        """Проверка модификации файла реестра для бесшовного обновления на лету."""
        if not self.registry_file.exists():
            return
        try:
            mtime = self.registry_file.stat().st_mtime
            if mtime > self.last_registry_mtime:
                log.info("Обнаружен свежий файл реестра %s -> горячее обновление пар", self.registry_file)
                self._load_registry()
        except Exception:
            pass

    async def start(self) -> None:
        log.info("CrossCoinStrategy запущена: %d пар отслеживаются", len(self.pairs_map))

    def on_price_tick(self, symbol: str, exchange: str, mid_price: float, now_sec: float) -> None:
        pass

    async def on_scan_symbol(self, symbol: str, quotes: dict, now: float) -> None:
        pass

    async def check_exits(self, symbol: str, quotes: dict, now: float) -> None:
        pass

    def _get_mid_price(self, symbol: str) -> float | None:
        if not self.engine or not getattr(self.engine, "state", None):
            return None
        quotes = self.engine.state.fresh_quotes(symbol, self.engine.exchanges)
        if not quotes:
            return None
        mids = [(q.best_bid + q.best_ask) / 2.0 for q in quotes.values() if q.best_bid > 0 and q.best_ask > 0]
        return sum(mids) / len(mids) if mids else None

    def snapshot(self) -> dict[str, Any]:
        """Возвращает срез радара межмонетных аномалий для веб-дашборда."""
        self._check_hot_reload()
        now = time.time()
        radar = []

        for p in self.pairs_map.values():
            qa = self._get_mid_price(p.sym_a)
            qb = self._get_mid_price(p.sym_b)
            if not qa or not qb or qa <= 0 or qb <= 0:
                continue

            z = p.update(qa, qb, now)
            ratio = qa / qb
            abs_z = abs(z) if z is not None else 0.0

            action = "Норма"
            is_sig = False
            if z is not None:
                if p.is_draining:
                    action = "ВЫВОД (Graceful Exit)"
                elif z >= p.entry_z:
                    action = f"SHORT {p.sym_a.split('/')[0]} / LONG {p.sym_b.split('/')[0]}"
                    is_sig = True
                elif z <= -p.entry_z:
                    action = f"LONG {p.sym_a.split('/')[0]} / SHORT {p.sym_b.split('/')[0]}"
                    is_sig = True

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
                "is_signal": is_sig,
                "is_draining": p.is_draining,
                "beta": round(p.beta, 4),
                "wfe_pct": round(p.wfe_pct, 1),
                "oos_pnl_usdt": round(p.oos_pnl_usdt, 3),
                "oos_win_rate": round(p.oos_win_rate, 1),
                "half_life_hours": round(p.half_life_hours, 1),
            })

        radar.sort(key=lambda x: x["abs_z"], reverse=True)
        return {
            "enabled": self.enabled,
            "entry_z": self.default_entry_z,
            "exit_z": self.exit_z,
            "pairs_count": len(self.pairs_map),
            "last_recalibration": self.last_recalibration_time,
            "radar": radar,
        }
