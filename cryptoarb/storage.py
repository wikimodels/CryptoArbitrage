"""
Хранилище (SQLite) с фиксами: WAL-режим, единый писатель в фоновом
потоке через очередь + батч-коммиты (не блокирует async event loop и
не делает commit на каждую строку), плюс ротация по retention_days.
Схема близка к будущей TimescaleDB — ядро про БД ничего не знает.
"""
from __future__ import annotations

import queue
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
    ts REAL, exchange TEXT, symbol TEXT,
    best_bid REAL, best_ask REAL,
    funding_rate REAL, funding_interval_hours REAL
);
CREATE INDEX IF NOT EXISTS idx_quotes_symbol_ts ON quotes(symbol, ts);

CREATE TABLE IF NOT EXISTS signals (
    ts REAL, symbol TEXT,
    exch_long TEXT, exch_short TEXT,
    raw_spread_pct REAL, funding_edge_pct REAL,
    fees_pct REAL, slippage_pct REAL,
    net_edge_pct REAL, passed_threshold INTEGER
);
CREATE INDEX IF NOT EXISTS idx_signals_symbol_ts ON signals(symbol, ts);

CREATE TABLE IF NOT EXISTS emulator_trades (
    trade_id TEXT PRIMARY KEY,
    symbol TEXT, exch_long TEXT, exch_short TEXT,
    open_ts REAL, close_ts REAL,
    entry_net_edge_pct REAL, exit_net_edge_pct REAL,
    price_pnl_usdt REAL, fees_usdt REAL, funding_usdt REAL,
    realized_pnl_usdt REAL, holding_seconds REAL,
    orphan_leg INTEGER, status TEXT, strategy TEXT
);
"""

_STOP = object()


class Storage:
    def __init__(self, sqlite_path: str, retention_days: int = 30):
        Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self._conn = sqlite3.connect(sqlite_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        # миграция старых баз: колонка strategy
        try:
            self._conn.execute("ALTER TABLE emulator_trades ADD COLUMN strategy TEXT")
        except Exception:
            pass
        self._conn.commit()
        self._q: "queue.Queue" = queue.Queue(maxsize=100_000)
        self._last_retention = 0.0
        self._thread = threading.Thread(target=self._writer, name="storage-writer", daemon=True)
        self._thread.start()

    # ---- публичный API (неблокирующий, кладёт в очередь) ----

    def save_quote(self, q) -> None:
        self._put(("quote", (q.ts, q.exchange, q.symbol, q.best_bid, q.best_ask,
                             q.funding_rate, q.funding_interval_hours)))

    def save_signal(self, s: dict) -> None:
        self._put(("signal", (s["ts"], s["symbol"], s["exch_long"], s["exch_short"],
                              s["raw_spread_pct"], s["funding_edge_pct"], s["fees_pct"],
                              s["slippage_pct"], s["net_edge_pct"], int(s["passed_threshold"]))))

    def save_emulator_trade(self, t: dict) -> None:
        self._put(("trade", (
            t["trade_id"], t["symbol"], t["exch_long"], t["exch_short"],
            t.get("open_ts"), t.get("close_ts"),
            t.get("entry_net_edge_pct"), t.get("exit_net_edge_pct"),
            t.get("price_pnl_usdt"), t.get("fees_usdt"), t.get("funding_usdt"),
            t.get("realized_pnl_usdt"), t.get("holding_seconds"),
            int(t.get("orphan_leg", False)), t.get("status"),
            t.get("strategy", "arb"),
        )))

    def _put(self, item) -> None:
        try:
            self._q.put_nowait(item)
        except queue.Full:
            pass  # дропаем при переполнении, чтобы не блокировать сканер

    def close(self) -> None:
        self._q.put(_STOP)
        self._thread.join(timeout=5)
        try:
            self._conn.close()
        except Exception:
            pass

    # ---- фоновый писатель ----

    def _writer(self) -> None:
        buf_q, buf_s, buf_t = [], [], []
        last_flush = time.time()

        def flush():
            nonlocal buf_q, buf_s, buf_t
            if buf_q:
                self._conn.executemany("INSERT INTO quotes VALUES (?,?,?,?,?,?,?)", buf_q)
                buf_q = []
            if buf_s:
                self._conn.executemany("INSERT INTO signals VALUES (?,?,?,?,?,?,?,?,?,?)", buf_s)
                buf_s = []
            if buf_t:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO emulator_trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", buf_t)
                buf_t = []
            self._conn.commit()

        while True:
            try:
                item = self._q.get(timeout=0.5)
            except queue.Empty:
                item = None

            if item is _STOP:
                flush()
                return
            if item is not None:
                kind, row = item
                (buf_q if kind == "quote" else buf_s if kind == "signal" else buf_t).append(row)

            now = time.time()
            if (len(buf_q) + len(buf_s) + len(buf_t) >= 500) or (now - last_flush > 1.0):
                try:
                    flush()
                except Exception:
                    pass
                last_flush = now

            if now - self._last_retention > 3600 and self.retention_days > 0:
                self._last_retention = now
                cutoff = now - self.retention_days * 86400
                try:
                    self._conn.execute("DELETE FROM quotes WHERE ts < ?", (cutoff,))
                    self._conn.execute("DELETE FROM signals WHERE ts < ?", (cutoff,))
                    self._conn.commit()
                except Exception:
                    pass
