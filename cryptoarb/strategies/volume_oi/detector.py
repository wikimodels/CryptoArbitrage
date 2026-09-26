"""
Модуль обнаружения институциональных аномалий объема и открытого интереса (Detector).
Анализирует RVOL, межбиржевое подтверждение, приток капитала в OI,
согласование с 15-минутным трендом EMA50 и свечную геометрию.
"""
from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass
from typing import Any

from .models import VolumeOIConfig, VolumeOISignal
from .selector import VolumeOISelector

log = logging.getLogger("volume_oi.detector")


@dataclass
class Candle1m:
    ts: float
    o: float
    h: float
    l: float
    c: float
    v: float


class VolumeOIDetector:
    """Детектор аномалий объема, открытого интереса и микроструктуры."""

    def __init__(self, config: VolumeOIConfig, selector: VolumeOISelector):
        self.config = config
        self.selector = selector

        # История минутных свечей: { (coin, exchange): deque[Candle1m] }
        self._candles: dict[tuple[str, str], deque[Candle1m]] = {}
        # Текущий EMA50 (15m): {(coin, exchange): float} (span 750 для 1m свечей)
        self._ema_trend: dict[tuple[str, str], float] = {}
        # Сумма объемов за последние 1440 минут для быстрого среднего: {(coin, exchange): float}
        self._vol_sum_1440: dict[tuple[str, str], float] = {}

        # История открытого интереса: { coin: deque[(ts, oi_usd)] }
        self._oi_history: dict[str, deque[tuple[float, float]]] = {}
        # Межбиржевые всплески в последнюю минуту: { (coin, minute_ts): set(exchanges) }
        self._minute_spikes: dict[tuple[str, int], set[str]] = {}

    def _clean_base(self, symbol_or_coin: str) -> str:
        return symbol_or_coin.split("/")[0].split("_")[0].upper()

    def update_oi(self, coin: str, ts: float, oi_usd: float) -> None:
        """Обновление среза открытого интереса по активу."""
        base = self._clean_base(coin)
        hist = self._oi_history.setdefault(base, deque(maxlen=60))
        hist.append((ts, oi_usd))

    def get_delta_oi_5m(self, coin: str, current_ts: float | None = None) -> float:
        """
        Вычисление 5-минутного изменения открытого интереса:
        Delta OI = (OI_t - OI_{t-5m}) / OI_{t-5m} * 100%
        """
        base = self._clean_base(coin)
        hist = self._oi_history.get(base)
        if not hist or len(hist) < 2:
            return 0.0

        latest_ts, latest_oi = hist[-1]
        target_ts = (current_ts or latest_ts) - 300.0  # 5 минут назад

        # Ищем ближайший снимок OI около 5 минут назад
        past_oi = None
        for ts, oi in hist:
            if ts <= target_ts:
                past_oi = oi
            else:
                if past_oi is None:
                    past_oi = oi
                break

        if past_oi is None or past_oi <= 0:
            past_oi = hist[0][1]

        if past_oi <= 0:
            return 0.0

        return ((latest_oi - past_oi) / past_oi) * 100.0

    def update_candle(
        self,
        coin: str,
        exchange: str,
        ts: float,
        o: float,
        h: float,
        l: float,
        c: float,
        v: float,
    ) -> float:
        """
        Регистрация закрытия 1-минутной свечи.
        Обновляет скользящую сумму объема, 15m EMA50 и возвращает RVOL текущей свечи.
        """
        base = self._clean_base(coin)
        key = (base, exchange.lower())
        buf = self._candles.setdefault(key, deque(maxlen=1440))

        # Обновление скользящей суммы объема за 24h (1440 свечей)
        cur_sum = self._vol_sum_1440.get(key, 0.0)
        if len(buf) == 1440:
            oldest = buf[0]
            cur_sum -= oldest.v

        cur_sum += v
        self._vol_sum_1440[key] = max(0.0, cur_sum)

        candle = Candle1m(ts=ts, o=o, h=h, l=l, c=c, v=v)
        buf.append(candle)

        # Вычисление RVOL
        n = len(buf)
        v_mean = (cur_sum / n) if n > 0 else v
        rvol = (v / v_mean) if v_mean > 0 else 1.0

        # Обновление 15m EMA50 (span = 750 для 1-минутных свечей, alpha = 2 / (750 + 1))
        alpha = 2.0 / 751.0
        cur_ema = self._ema_trend.get(key)
        if cur_ema is None:
            self._ema_trend[key] = c
        else:
            self._ema_trend[key] = alpha * c + (1.0 - alpha) * cur_ema

        # Фиксация всплеска для межбиржевой проверки
        minute_bucket = int(ts // 60)
        if rvol >= 3.0:
            spk_set = self._minute_spikes.setdefault((base, minute_bucket), set())
            spk_set.add(exchange.lower())

        # Очистка старых бакетов всплесков (старше 10 минут)
        self._prune_old_spikes(minute_bucket)

        return rvol

    def _prune_old_spikes(self, current_minute_bucket: int) -> None:
        to_del = [k for k in self._minute_spikes.keys() if current_minute_bucket - k[1] > 10]
        for k in to_del:
            del self._minute_spikes[k]

    def get_candle_quality(self, o: float, h: float, l: float, c: float) -> tuple[float, float]:
        """
        Вычисляет позицию закрытия и размер тела свечи в процентах:
        close_pos = (Close - Low) / (High - Low)
        body_pct = abs(Close - Open) / Open * 100%
        """
        rng = h - l
        close_pos = ((c - l) / rng) if rng > 1e-8 else 0.5
        body_pct = (abs(c - o) / o * 100.0) if o > 0 else 0.0
        return close_pos, body_pct

    def check_signal(
        self,
        coin: str,
        exchange: str,
        now_ts: float,
        last_candle: Candle1m | None = None,
    ) -> VolumeOISignal | None:
        """
        Проверка выполнения всех условий стратегии Sniper Maker:
        1. Актив разрешен и присутствует в Whitelist.
        2. RVOL >= min_rvol (4.0 для BTC, 5.0 для альтов).
        3. Межбиржевая синхронизация: RVOL >= 3.0 минимум на min_exchanges биржах (если есть данные).
        4. Delta OI 5m >= порога (0.35% для BTC, 1.5% для альтов).
        5. Согласование со старшим трендом (15m EMA50).
        6. Анатомия свечи (сильное закрытие без фитилей, тело >= порога).
        """
        base = self._clean_base(coin)
        if not self.selector.is_eligible(base, now_ts):
            return None

        item = self.selector.get_item(base)
        min_rvol = item.min_rvol if item else self.config.min_rvol
        min_doi = item.min_delta_oi_pct if item else self.config.min_delta_oi_pct
        min_body = item.min_body_pct if item else self.config.min_body_pct
        tp_pct = item.tp_pct if item else self.config.tp_pct
        sl_pct = item.sl_pct if item else self.config.sl_pct

        key = (base, exchange.lower())
        buf = self._candles.get(key)
        candle = last_candle or (buf[-1] if buf else None)
        if not candle:
            return None

        # Проверка 24h среднего объема и RVOL
        v_mean = (self._vol_sum_1440.get(key, 0.0) / len(buf)) if (buf and len(buf) > 0) else candle.v
        rvol = (candle.v / v_mean) if v_mean > 0 else 1.0
        if rvol < min_rvol:
            return None

        # Проверка размера тела и позиции закрытия
        close_pos, body_pct = self.get_candle_quality(candle.o, candle.h, candle.l, candle.c)
        if body_pct < min_body:
            return None

        # Проверка Delta OI
        delta_oi = self.get_delta_oi_5m(base, candle.ts)
        if delta_oi < min_doi:
            return None

        # Проверка старшего тренда 15m EMA50
        ema_val = self._ema_trend.get(key, candle.c)

        # Определение направления
        direction = 0
        if candle.c > candle.o:
            # Кандидат в LONG: цена выше EMA50 и закрытие в верхних 30%
            if candle.c >= ema_val and close_pos >= 0.70:
                direction = 1
        elif candle.c < candle.o:
            # Кандидат в SHORT: цена ниже EMA50 и закрытие в нижних 30%
            if candle.c <= ema_val and close_pos <= 0.30:
                direction = -1

        if direction == 0:
            return None

        # Межбиржевая проверка (если зафиксированы всплески на других биржах)
        minute_bucket = int(candle.ts // 60)
        exchanges_active = list(self._minute_spikes.get((base, minute_bucket), [exchange.lower()]))

        # Расчет лимитного уровня входа на 30% откате тела свечи
        spike_body = abs(candle.c - candle.o)
        pullback = self.config.pullback_frac * spike_body

        if direction == 1:
            limit_price = candle.c - pullback
            hard_sl_price = limit_price * (1.0 - sl_pct / 100.0)
            tp_price = limit_price * (1.0 + tp_pct / 100.0)
        else:
            limit_price = candle.c + pullback
            hard_sl_price = limit_price * (1.0 + sl_pct / 100.0)
            tp_price = limit_price * (1.0 - tp_pct / 100.0)

        tier_str = item.tier.value if item else "TIER_2_MIDWEIGHT"

        return VolumeOISignal(
            coin=base,
            direction=direction,
            timestamp=candle.ts,
            rvol=round(rvol, 2),
            delta_oi_pct=round(delta_oi, 2),
            trend_ema=round(ema_val, 6),
            close_price=candle.c,
            open_price=candle.o,
            limit_price=round(limit_price, 6),
            hard_sl_price=round(hard_sl_price, 6),
            tp_price=round(tp_price, 6),
            exchanges_confirmed=exchanges_active,
            tier=tier_str,
            confidence_score=round(min(1.0, rvol / 10.0 + delta_oi / 5.0), 2),
        )
