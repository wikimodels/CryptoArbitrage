"""
Интеграционный класс стратегии Volume Spike & Open Interest Momentum (Sniper Maker).
Наследует BaseStrategy для бесшовного подключения к ядру движка Engine.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptoarb.strategies.base import BaseStrategy

from .detector import VolumeOIDetector
from .executor import VolumeOIExecutor
from .models import VolumeOIConfig, VolumeOISignal
from .selector import VolumeOISelector

if TYPE_CHECKING:
    from cryptoarb.engine import Engine

log = logging.getLogger("strategies.volume_oi")


class VolumeOIStrategy(BaseStrategy):
    """
    Модульная стратегия снайперского входа на аномалиях объема и открытого интереса.
    """

    def __init__(self, engine: Engine, cfg: dict[str, Any]):
        super().__init__(engine, cfg)
        self.name = "volume_oi"
        self.display_name = "Volume & OI Momentum (Sniper Maker)"

        voi_cfg = cfg.get("volume_oi", {})
        self.enabled = bool(voi_cfg.get("enabled", True))

        # Инициализация конфигурации
        self.config = VolumeOIConfig(
            enabled=self.enabled,
            whitelist_file=voi_cfg.get("whitelist_file", "config/volume_oi_whitelist.json"),
            pullback_frac=float(voi_cfg.get("pullback_frac", 0.30)),
            order_timeout_sec=float(voi_cfg.get("order_timeout_sec", 300.0)),
            tp_pct=float(voi_cfg.get("tp_pct", 1.5)),
            sl_pct=float(voi_cfg.get("sl_pct", 1.0)),
            trail_activation_pct=float(voi_cfg.get("trail_activation_pct", 0.8)),
            trail_distance_pct=float(voi_cfg.get("trail_distance_pct", 0.5)),
            timestop_sec=float(voi_cfg.get("timestop_sec", 3600.0)),
            cooldown_sec=float(voi_cfg.get("cooldown_sec", 900.0)),
            min_rvol=float(voi_cfg.get("min_rvol", 5.0)),
            min_delta_oi_pct=float(voi_cfg.get("min_delta_oi_pct", 1.5)),
            min_exchanges=int(voi_cfg.get("min_exchanges", 2)),
            min_body_pct=float(voi_cfg.get("min_body_pct", 0.15)),
            position_size_usd=float(voi_cfg.get("position_size_usd", 10.0)),
        )

        # Компоненты стратегии
        self.selector = VolumeOISelector(self.config.whitelist_file)
        self.detector = VolumeOIDetector(self.config, self.selector)
        self.executor = VolumeOIExecutor(self.config, self.selector)

        # Кольцевой буфер недавних сигналов для мониторинга
        self._recent_signals: deque[dict[str, Any]] = deque(maxlen=50)
        # Отслеживание последней проверенной минуты для предотвращения дублирования
        self._last_candle_minute: dict[str, int] = {}
        # Локальное накопление цен за минуту для формирования минутных свечей: { coin: {o, h, l, c, v, count, start_ts} }
        self._current_candles: dict[str, dict[str, Any]] = {}

    async def start(self) -> None:
        """Прогрев исторических данных при запуске движка."""
        log.info("Запуск стратегии Volume & OI Momentum (Sniper Maker)...")
        self._warmup_historical_oi()

    def _warmup_historical_oi(self) -> None:
        """Предзагрузка последних срезов открытого интереса из локальных файлов (если доступны)."""
        oi_dir = Path("data/raw_oi_30d/binance")
        majors_dir = Path("data/raw_majors_30d")
        loaded_count = 0

        # Предзагрузка альткоинов
        if oi_dir.exists():
            for coin_path in oi_dir.glob("*_USDT_USDT"):
                f = coin_path / "oi.parquet"
                if f.exists():
                    try:
                        import polars as pl
                        df = pl.read_parquet(f).sort("ts").tail(20)
                        coin = coin_path.name.split("_")[0]
                        for row in df.iter_rows(named=True):
                            self.detector.update_oi(coin, float(row["ts"]), float(row["oi_usd"]))
                        loaded_count += 1
                    except Exception:
                        pass

        # Предзагрузка BTC / ETH / SOL
        if majors_dir.exists():
            for major_file in majors_dir.glob("*_oi.parquet"):
                try:
                    import polars as pl
                    coin = major_file.stem.split("_")[0]
                    df = pl.read_parquet(major_file).sort("ts").tail(20)
                    for row in df.iter_rows(named=True):
                        self.detector.update_oi(coin, float(row["ts"]), float(row["oi_usd"]))
                    loaded_count += 1
                except Exception:
                    pass

        log.info("Прогрев открытого интереса завершен: загружено %d инструментов", loaded_count)

    def on_price_tick(self, symbol: str, exchange: str, mid_price: float, now_sec: float) -> None:
        """
        Обработка тика реального времени от WS стрима:
        1. Агрегация тиков в текущую 1-минутную свечу.
        2. Обновление открытых позиций в executor.
        """
        if not self.enabled:
            return

        base = symbol.split("/")[0].split("_")[0].upper()

        # Формирование минутной свечи
        min_bucket = int(now_sec // 60)
        c_data = self._current_candles.get(base)
        if c_data is None or c_data.get("bucket") != min_bucket:
            # Завершение предыдущей свечи и регистрация в детекторе
            if c_data is not None:
                self.detector.update_candle(
                    coin=base,
                    exchange=c_data.get("exchange", exchange),
                    ts=c_data["bucket"] * 60.0,
                    o=c_data["open"],
                    h=c_data["high"],
                    l=c_data["low"],
                    c=c_data["close"],
                    v=c_data["vol"],
                )
            self._current_candles[base] = {
                "bucket": min_bucket,
                "exchange": exchange,
                "open": mid_price,
                "high": mid_price,
                "low": mid_price,
                "close": mid_price,
                "vol": 1.0,
            }
        else:
            c_data["high"] = max(c_data["high"], mid_price)
            c_data["low"] = min(c_data["low"], mid_price)
            c_data["close"] = mid_price
            c_data["vol"] += 1.0

        # Обновление состояния ордеров/позиций в executor
        self.executor.on_market_update(
            coin=base,
            bid=mid_price,
            ask=mid_price,
            high=mid_price,
            low=mid_price,
            now_ts=now_sec,
        )

    async def on_scan_symbol(self, symbol: str, quotes: dict, now: float) -> None:
        """
        Сканирование символа на появление институционального импульса входа.
        """
        if not self.enabled:
            return

        base = symbol.split("/")[0].split("_")[0].upper()
        if not self.selector.is_eligible(base, now):
            return

        # Проверка кулдауна и наличия позиций
        if self.executor.is_in_cooldown(base, now) or self.executor.has_active_position(base):
            return

        min_bucket = int(now // 60)
        if self._last_candle_minute.get(base) == min_bucket:
            return  # Уже сканировали в эту минуту

        # Берем актуальную биржу из котировок
        best_ex = next(iter(quotes.keys())) if quotes else "binance"
        sig = self.detector.check_signal(base, best_ex, now)
        if sig is not None:
            self._last_candle_minute[base] = min_bucket
            pos = self.executor.submit_signal(sig, now)
            sig_dict = {
                "ts": now,
                "coin": sig.coin,
                "direction": "LONG" if sig.direction == 1 else "SHORT",
                "rvol": sig.rvol,
                "delta_oi_pct": sig.delta_oi_pct,
                "limit_price": sig.limit_price,
                "tp_price": sig.tp_price,
                "sl_price": sig.hard_sl_price,
                "tier": sig.tier,
                "score": sig.confidence_score,
            }
            self._recent_signals.appendleft(sig_dict)

            if getattr(self.engine, "loggers", None) and hasattr(self.engine.loggers, "signals"):
                self.engine.loggers.signals.write({
                    "strategy": "volume_oi",
                    "event": "signal_generated",
                    **sig_dict,
                })

    async def check_exits(self, symbol: str, quotes: dict, now: float) -> None:
        """
        Контроль выходов по открытым позициям на основе лучших бидов и асков книги.
        """
        if not self.enabled:
            return

        base = symbol.split("/")[0].split("_")[0].upper()
        if not self.executor.has_active_position(base):
            return

        best_bid = 0.0
        best_ask = 0.0
        for q in quotes.values():
            if q.best_bid > 0 and (best_bid == 0.0 or q.best_bid > best_bid):
                best_bid = q.best_bid
            if q.best_ask > 0 and (best_ask == 0.0 or q.best_ask < best_ask):
                best_ask = q.best_ask

        if best_bid <= 0 or best_ask <= 0:
            return

        high = max(best_bid, best_ask)
        low = min(best_bid, best_ask)

        events = self.executor.on_market_update(
            coin=base,
            bid=best_bid,
            ask=best_ask,
            high=high,
            low=low,
            now_ts=now,
        )

        for ev in events:
            if getattr(self.engine, "loggers", None) and hasattr(self.engine.loggers, "system"):
                self.engine.loggers.system.write({
                    "strategy": "volume_oi",
                    **ev,
                })

    def snapshot(self) -> dict[str, Any]:
        """Возвращает срез аналитики, радара и KPI для дашборда."""
        whitelist_list = [
            item.to_dict()
            for item in self.selector.items.values()
        ]
        whitelist_list.sort(key=lambda x: (x["tier"], -x["win_rate"]))

        return {
            "enabled": self.enabled,
            "whitelist_total": len(self.selector.items),
            "whitelist": whitelist_list,
            "open_positions": self.executor.get_open_positions(),
            "kpi": self.executor.get_kpi_summary(),
            "recent_signals": list(self._recent_signals)[:20],
            "recent_closed": [p.to_dict() for p in self.executor.closed_positions[-20:]],
        }
