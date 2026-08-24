"""
Файловое логирование — самодостаточный источник истины (ТЗ 5.1).
JSON Lines, ротация по дням, отдельные потоки. Плюс очистка файлов
старше retention_days при старте.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone


class JsonlLogger:
    def __init__(self, log_dir: str, stream_name: str):
        self.log_dir = log_dir
        self.stream_name = stream_name
        os.makedirs(log_dir, exist_ok=True)
        self._current_date = None
        self._fh = None
        self._roll_if_needed()

    def _roll_if_needed(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._current_date:
            if self._fh:
                self._fh.close()
            self._current_date = today
            path = os.path.join(self.log_dir, f"{self.stream_name}.{today}.jsonl")
            self._fh = open(path, "a", encoding="utf-8")

    def write(self, record: dict):
        self._roll_if_needed()
        record = {"ts": time.time(), "ts_iso": datetime.now(timezone.utc).isoformat(), **record}
        self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()

    def close(self):
        if self._fh:
            self._fh.close()


class Loggers:
    def __init__(self, log_dir: str, retention_days: int = 90):
        self._cleanup(log_dir, retention_days)
        self.signals = JsonlLogger(log_dir, "signals")
        self.emulator_trades = JsonlLogger(log_dir, "emulator_trades")
        self.errors = JsonlLogger(log_dir, "errors")
        self.system = JsonlLogger(log_dir, "system")

    @staticmethod
    def _cleanup(log_dir: str, retention_days: int):
        if retention_days <= 0 or not os.path.isdir(log_dir):
            return
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        for name in os.listdir(log_dir):
            path = os.path.join(log_dir, name)
            try:
                if os.path.isfile(path) and datetime.fromtimestamp(os.path.getmtime(path), timezone.utc) < cutoff:
                    os.remove(path)
            except Exception:
                pass

    def close_all(self):
        for l in (self.signals, self.emulator_trades, self.errors, self.system):
            l.close()
