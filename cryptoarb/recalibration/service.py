"""
Асинхронный фоновый сервис регулярной рекалибровки парного арбитража.
Запускает конвейер по расписанию (раз в 24 часа в полночь UTC или с заданным интервалом)
в отдельном пуле потоков, не блокируя событийный цикл движка.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptoarb.recalibration.pipeline import RecalibrationPipeline

log = logging.getLogger("recalibration.service")


class RecalibrationService:
    """Фоновый планировщик суточной рекалибровки пар."""

    def __init__(
        self,
        interval_hours: float = 24.0,
        run_at_utc_hour: int = 0,
        data_dir: str | Path = "data/raw_1m_30d/bitget",
        output_dir: str | Path = "output",
        top_screen_candidates: int = 15,
        max_active_pairs: int = 6,
        min_wfe_pct: float = 50.0,
    ):
        self.interval_sec = interval_hours * 3600.0
        self.run_at_utc_hour = run_at_utc_hour
        self.pipeline = RecalibrationPipeline(
            data_dir=data_dir,
            output_dir=output_dir,
            top_screen_candidates=top_screen_candidates,
            max_active_pairs=max_active_pairs,
            min_wfe_pct=min_wfe_pct,
        )
        self._task: asyncio.Task | None = None
        self._running = False
        self.last_run_utc: str | None = None
        self.last_duration_sec: float = 0.0
        self.next_run_timestamp: float = 0.0
        self.is_recalibrating = False

    def start(self) -> None:
        """Запуск фоновой задачи в текущем цикле событий asyncio."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="recalibration_service")
        log.info("RecalibrationService запущен (интервал %.1f ч, целевой час %02d:00 UTC)", self.interval_sec / 3600.0, self.run_at_utc_hour)

    def stop(self) -> None:
        """Остановка фоновой задачи."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        log.info("RecalibrationService остановлен")

    def _seconds_until_target_hour(self) -> float:
        """Рассчитывает количество секунд до следующего наступления целевого UTC часа."""
        now = datetime.now(timezone.utc)
        target = now.replace(hour=self.run_at_utc_hour, minute=0, second=0, microsecond=0)
        if target <= now:
            # Если сегодня этот час уже прошел, целевое время — завтра в тот же час
            target = target.replace(day=now.day + 1)
        return (target - now).total_seconds()

    async def _run_loop(self) -> None:
        """Основной рабочий цикл сервиса."""
        # Первоначальный сон: если интервал 24ч, ждем до 00:00 UTC, иначе спим интервал
        if self.interval_sec >= 86400.0:
            delay = self._seconds_until_target_hour()
        else:
            delay = self.interval_sec

        self.next_run_timestamp = time.time() + delay

        while self._running:
            try:
                log.info("Следующая рекалибровка запланирована через %.1f ч (в %s UTC)", delay / 3600.0, datetime.fromtimestamp(self.next_run_timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
                await asyncio.sleep(delay)
                if not self._running:
                    break

                await self.trigger_recalibration()

                # Вычисляем задержку для следующего шага
                if self.interval_sec >= 86400.0:
                    delay = self._seconds_until_target_hour()
                else:
                    delay = self.interval_sec
                self.next_run_timestamp = time.time() + delay

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Исключение в цикле RecalibrationService: %s", e, exc_info=True)
                await asyncio.sleep(60.0)

    async def trigger_recalibration(self) -> dict[str, Any]:
        """Принудительный запуск конвейера в отдельном пуле потоков."""
        if self.is_recalibrating:
            log.warning("Рекалибровка уже выполняется, пропуск вызова")
            return {"status": "in_progress"}

        self.is_recalibrating = True
        log.info("Старт процедуры Walk-Forward рекалибровки пула пар...")
        start_t = time.time()
        loop = asyncio.get_running_loop()

        try:
            # Выполняем тяжелый расчет в ThreadPool, чтобы не тормозить веб-сокеты и торговлю
            registry = await loop.run_in_executor(None, self.pipeline.run)
            self.last_run_utc = datetime.now(timezone.utc).isoformat()
            self.last_duration_sec = round(time.time() - start_t, 2)
            log.info("Рекалибровка успешно завершена за %.2f сек. Активно пар: %d", self.last_duration_sec, registry.get("pairs_active_count", 0))
            return registry
        except Exception as e:
            log.error("Сбой рекалибровки: %s", e, exc_info=True)
            return {"status": "error", "error": str(e)}
        finally:
            self.is_recalibrating = False

    def get_status(self) -> dict[str, Any]:
        """Возвращает статус сервиса для дашборда."""
        now = time.time()
        time_to_next = max(0.0, self.next_run_timestamp - now) if self.next_run_timestamp > 0 else 0.0
        return {
            "running": self._running,
            "is_recalibrating": self.is_recalibrating,
            "last_run_utc": self.last_run_utc,
            "last_duration_sec": self.last_duration_sec,
            "next_run_in_sec": round(time_to_next, 1),
            "next_run_in_hours": round(time_to_next / 3600.0, 2),
        }
