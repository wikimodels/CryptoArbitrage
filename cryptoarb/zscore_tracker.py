"""
Модуль онлайн-расчета Z-Score для пар бирж.

Хранит скользящие 24-часовые окна (1440 минутных точек) соотношений цен:
    ratio = price_a / price_b
Ведет экспоненциальную скользящую среднюю (EMA) и стандартное отклонение (STD).
Рассчитывает Z-Score в реальном времени по входящим WebSocket-котировкам:
    z = (live_ratio - ma) / sd
Поддерживает мгновенный прогрев из локального кэша свечей (data/raw_1m_30d).
"""
from __future__ import annotations

import logging
import math
from collections import deque
from pathlib import Path
from typing import Optional

import polars as pl

log = logging.getLogger("zscore")

DEFAULT_PERIOD = 1440  # 24 часа по 1 минуте


class PairZState:
    """Состояние одной пары (символ, биржа_A, биржа_B)."""
    def __init__(self, period: int = DEFAULT_PERIOD):
        self.period = period
        self.alpha = 2.0 / (period + 1.0)
        self.window: deque[float] = deque(maxlen=period)
        self.ma: Optional[float] = None
        self.sd: Optional[float] = None
        self.last_bar_ts: int = 0  # timestamp последней обработанной минуты (мс)

    def push_bar(self, ratio: float, ts_ms: int = 0) -> None:
        """Добавить закрытую минутную точку в историю."""
        if ratio <= 0 or math.isnan(ratio) or math.isinf(ratio):
            return

        self.window.append(ratio)
        if self.ma is None:
            self.ma = ratio
        else:
            self.ma = self.alpha * ratio + (1.0 - self.alpha) * self.ma

        # Пересчет стандартного отклонения по окну
        n = len(self.window)
        if n >= 2:
            m = sum(self.window) / n
            var = sum((x - m) ** 2 for x in self.window) / n
            self.sd = math.sqrt(var) if var > 0 else 1e-6
        else:
            self.sd = None

        if ts_ms > 0:
            self.last_bar_ts = ts_ms

    def load_history(self, ratios: list[float], last_ts_ms: int = 0) -> None:
        """Быстрая пакетная загрузка исторического ряда баров O(N)."""
        clean = [r for r in ratios if r > 0 and not (math.isnan(r) or math.isinf(r))]
        if not clean:
            return
        slice_ratios = clean[-self.period:]
        self.window.clear()
        self.window.extend(slice_ratios)
        # O(N) расчет EMA
        ma = slice_ratios[0]
        alpha = self.alpha
        one_minus = 1.0 - alpha
        for r in slice_ratios[1:]:
            ma = alpha * r + one_minus * ma
        self.ma = ma
        # Расчет STD один раз для всего окна
        n = len(self.window)
        if n >= 2:
            m = sum(self.window) / n
            var = sum((x - m) ** 2 for x in self.window) / n
            self.sd = math.sqrt(var) if var > 0 else 1e-6
        else:
            self.sd = None
        if last_ts_ms > 0:
            self.last_bar_ts = last_ts_ms

    def get_z(self, live_price_a: float, live_price_b: float) -> Optional[float]:
        """Мгновенный расчет Z-Score по текущим ценам стакана/тикера."""
        if live_price_a <= 0 or live_price_b <= 0:
            return None
        if self.ma is None or self.sd is None or self.sd <= 0:
            return None

        live_ratio = live_price_a / live_price_b
        return (live_ratio - self.ma) / self.sd


class ZScoreTracker:
    """Глобальный трекер Z-Score для всех отслеживаемых символов и бирж."""
    def __init__(self, period: int = DEFAULT_PERIOD):
        self.period = period
        # Ключ: (symbol, exch_a, exch_b) -> PairZState
        self.states: dict[tuple[str, str, str], PairZState] = {}
        # Хранение последней минутной цены: (symbol, exch) -> (ts_minute, close_price)
        self._minute_candles: dict[tuple[str, str], tuple[int, float]] = {}

    def _get_or_create(self, symbol: str, ex_a: str, ex_b: str) -> PairZState:
        key = (symbol, ex_a, ex_b)
        if key not in self.states:
            self.states[key] = PairZState(self.period)
        return self.states[key]

    def prewarm(self, symbols: list[str], exchanges: list[str],
                data_dir: Path | str = Path("data/raw_1m_30d")) -> int:
        """Прогрев скользящих окон из локального архива parquet-свечей."""
        p_dir = Path(data_dir)
        if not p_dir.exists():
            log.warning("Каталог данных для прогрева Z-Score не найден: %s", p_dir)
            return 0

        prewarmed_pairs = 0
        log.info("Прогрев ZScoreTracker из %s для %d монет...", p_dir, len(symbols))

        available_safes: set[str] = set()
        for ex in exchanges:
            ed = p_dir / ex
            if ed.exists():
                try:
                    available_safes.update(p.name for p in ed.iterdir() if p.is_dir())
                except Exception:
                    pass

        for sym in symbols:
            safe = sym.replace("/", "_").replace(":", "_")
            if safe not in available_safes:
                continue

            candles_by_ex: dict[str, dict[int, float]] = {}

            for ex in exchanges:
                f_path = p_dir / ex / safe / "candles.parquet"
                if f_path.exists():
                    try:
                        df = pl.read_parquet(f_path).sort("ts")
                        # Берем последние period свечей
                        tail = df.tail(self.period + 100)
                        candles_by_ex[ex] = dict(zip(tail["ts"].to_list(), tail["c"].to_list()))
                    except Exception as e:
                        log.debug("Ошибка чтения %s: %s", f_path, e)

            # Формируем пары
            ex_list = [e for e in exchanges if e in candles_by_ex]
            for i in range(len(ex_list)):
                for j in range(len(ex_list)):
                    if i == j:
                        continue
                    ea, eb = ex_list[i], ex_list[j]
                    ca, cb = candles_by_ex[ea], candles_by_ex[eb]
                    common_ts = sorted(set(ca.keys()) & set(cb.keys()))
                    if len(common_ts) < 60:
                        continue

                    # Быстрая загрузка истории баров
                    ts_slice = common_ts[-self.period:]
                    ratios = [ca[t] / cb[t] for t in ts_slice if ca[t] > 0 and cb[t] > 0]
                    st = self._get_or_create(sym, ea, eb)
                    st.load_history(ratios, last_ts_ms=ts_slice[-1] if ts_slice else 0)

                    prewarmed_pairs += 1

        log.info("ZScoreTracker успешно прогрет: %d пар активны", prewarmed_pairs)
        return prewarmed_pairs

    def on_tick(self, symbol: str, exchange: str, mid_price: float, now_sec: float) -> None:
        """Обработка тика для агрегации в 1-минутные бары."""
        if mid_price <= 0:
            return
        minute_ts = int(now_sec // 60) * 60 * 1000
        key = (symbol, exchange)
        prev = self._minute_candles.get(key)

        if prev is None:
            self._minute_candles[key] = (minute_ts, mid_price)
            return

        prev_min, _ = prev
        if minute_ts > prev_min:
            # Наступила новая минута — фиксируем бар прошлой минуты
            # Обновляем все пары с этой биржей
            for (s, ea, eb), state in self.states.items():
                if s != symbol:
                    continue
                other_ex = eb if ea == exchange else (ea if eb == exchange else None)
                if not other_ex:
                    continue
                other_prev = self._minute_candles.get((symbol, other_ex))
                if other_prev and other_prev[0] == prev_min:
                    pa = prev[1] if ea == exchange else other_prev[1]
                    pb = other_prev[1] if ea == exchange else prev[1]
                    if pa > 0 and pb > 0 and minute_ts > state.last_bar_ts:
                        state.push_bar(pa / pb, ts_ms=prev_min)

            self._minute_candles[key] = (minute_ts, mid_price)
        else:
            # Обновляем текущую закрывающую цену внутри минуты
            self._minute_candles[key] = (minute_ts, mid_price)

    def get_z(self, symbol: str, ex_a: str, ex_b: str,
              live_px_a: float, live_px_b: float) -> Optional[float]:
        """Получить текущий Z-Score между биржами ex_a и ex_b по живым ценам."""
        state = self.states.get((symbol, ex_a, ex_b))
        if state is None:
            return None
        return state.get_z(live_px_a, live_px_b)
