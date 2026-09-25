"""
Модуль умной синхронизации и ротации архива 1-минутных свечей (data/raw_1m_30d).

Особенности:
- Быстрая проверка свежести архива (< 50мс).
- Умный старт: если данные отстают менее чем на max_gap_minutes (по умолчанию 90 мин),
  докачка пропускается и сервер стартует мгновенно (2 сек).
- Автоматическая дельта-докачка: при отставании >= 90 мин подгружаются только
  недостающие свечи в 4 параллельных потока (~30-50 сек).
- Автоматическая ротация: удаление старых свечей старше retention_days (30 дней),
  что навсегда фиксирует размер папки на уровне ~90-100 МБ.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import ccxt
import polars as pl

from cryptoarb.connectors.ccxt_connector import CCXT_ID_MAP

log = logging.getLogger("candles")

DEFAULT_ROOT = Path("data/raw_1m_30d")
DEFAULT_TOP40 = Path("output/top40_4ex.txt")
DEFAULT_EXCHANGES = ["okx", "bitget", "mexc", "bingx"]


def _client(exchange_id: str):
    ccxt_id = CCXT_ID_MAP.get(exchange_id, exchange_id)
    cls = getattr(ccxt, ccxt_id)
    return cls({"enableRateLimit": True, "timeout": 30000,
                "options": {"defaultType": "swap"}})


def fetch_1m_safe(ex_client, symbol: str, since_ms: int, until_ms: int, limit: int = 1000) -> list[list]:
    out = []
    win_ms = 5 * 86400 * 1000
    cur = since_ms
    while cur < until_ms:
        wend = min(cur + win_ms, until_ms)
        since = cur
        guard = 0
        while since < wend and guard < 500:
            guard += 1
            try:
                batch = ex_client.fetch_ohlcv(symbol, timeframe="1m", since=since, limit=limit)
            except Exception:
                time.sleep(1.0)
                try:
                    batch = ex_client.fetch_ohlcv(symbol, timeframe="1m", since=since, limit=limit)
                except Exception:
                    break
            if not batch:
                break
            out.extend(batch)
            last_ts = batch[-1][0]
            if last_ts < since:
                break
            since = last_ts + 60_000
            time.sleep(ex_client.rateLimit / 1000.0)
        cur = wend

    seen = {}
    for b in out:
        if since_ms <= b[0] < until_ms:
            seen[b[0]] = b
    return sorted(seen.values())


def get_newest_archive_ts(data_dir: Path | str = DEFAULT_ROOT,
                          exchanges: list[str] | None = None) -> int:
    """Быстро находит самый свежий timestamp свечи в локальном архиве."""
    p_dir = Path(data_dir)
    if not p_dir.exists():
        return 0
    target_exs = exchanges or DEFAULT_EXCHANGES
    max_ts = 0
    for ex in target_exs:
        ed = p_dir / ex
        if not ed.exists():
            continue
        for p in ed.glob("*/candles.parquet"):
            try:
                df = pl.read_parquet(p, columns=["ts"])
                if len(df) > 0:
                    t = int(df["ts"].max())
                    if t > max_ts:
                        max_ts = t
                    break
            except Exception:
                pass
    return max_ts


def update_exchange(ex: str, symbols: list[str], root_dir: Path,
                    retention_days: int = 30) -> tuple[str, int, int]:
    """Обновляет и ротирует 1-минутные свечи для одной биржи."""
    ex_client = _client(ex)
    success_count = 0
    fail_count = 0
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - retention_days * 86400 * 1000

    for sym in symbols:
        safe = sym.replace("/", "_").replace(":", "_")
        p = root_dir / ex / safe / "candles.parquet"
        try:
            if p.exists():
                df = pl.read_parquet(p)
                max_ts = int(df["ts"].max())
                since_ms = max_ts + 60_000
                if now_ms - since_ms < 60_000:
                    # Свечи уже актуальны — только ротация старых баров
                    if df["ts"].min() < cutoff_ms:
                        trimmed = df.filter(pl.col("ts") >= cutoff_ms)
                        trimmed.write_parquet(p)
                    success_count += 1
                    continue

                new_rows = fetch_1m_safe(ex_client, sym, since_ms, now_ms)
                if new_rows:
                    new_df = pl.DataFrame({
                        "ts": [r[0] for r in new_rows],
                        "o": [r[1] for r in new_rows],
                        "h": [r[2] for r in new_rows],
                        "l": [r[3] for r in new_rows],
                        "c": [r[4] for r in new_rows],
                        "v": [r[5] for r in new_rows],
                    })
                    merged = pl.concat([df, new_df]).unique(subset=["ts"]).filter(pl.col("ts") >= cutoff_ms).sort("ts")
                    merged.write_parquet(p)
                else:
                    if df["ts"].min() < cutoff_ms:
                        df.filter(pl.col("ts") >= cutoff_ms).write_parquet(p)
                success_count += 1
            else:
                # Первый запуск для монеты — берем окно retention_days
                since_ms = cutoff_ms
                rows = fetch_1m_safe(ex_client, sym, since_ms, now_ms)
                if rows:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    df = pl.DataFrame({
                        "ts": [r[0] for r in rows],
                        "o": [r[1] for r in rows],
                        "h": [r[2] for r in rows],
                        "l": [r[3] for r in rows],
                        "c": [r[4] for r in rows],
                        "v": [r[5] for r in rows],
                    }).sort("ts")
                    df.write_parquet(p)
                    success_count += 1
                else:
                    fail_count += 1
        except Exception as e:
            log.debug("Ошибка обновления свечей %s на %s: %s", sym, ex, e)
            fail_count += 1

    return ex, success_count, fail_count


def sync_candles(symbols: list[str] | None = None,
                 exchanges: list[str] | None = None,
                 data_dir: Path | str = DEFAULT_ROOT,
                 top40_file: Path | str = DEFAULT_TOP40,
                 max_gap_minutes: int = 90,
                 retention_days: int = 30,
                 force: bool = False) -> bool:
    """
    Интеллектуальная синхронизация и ротация свечей.
    Возвращает True, если докачка выполнялась, и False, если архив уже был свежим.
    """
    p_dir = Path(data_dir)
    target_exs = [e for e in (exchanges or DEFAULT_EXCHANGES) if e in DEFAULT_EXCHANGES]
    if not target_exs:
        target_exs = DEFAULT_EXCHANGES

    now_ms = int(time.time() * 1000)
    newest_ts = get_newest_archive_ts(p_dir, target_exs)
    gap_minutes = (now_ms - newest_ts) / (60 * 1000) if newest_ts > 0 else 999999

    # Загружаем целевые символы (из файла top-40 или переданного списка)
    top_path = Path(top40_file)
    if top_path.exists():
        target_symbols = [l.strip() for l in open(top_path, encoding="utf-8") if l.strip()]
    elif symbols:
        target_symbols = list(symbols)
    else:
        target_symbols = []

    if not target_symbols:
        log.warning("Список символов для синхронизации свечей пуст")
        return False

    # Проверяем, есть ли монеты, для которых архивов еще нет
    missing_files = False
    for ex in target_exs:
        for sym in target_symbols:
            safe = sym.replace("/", "_").replace(":", "_")
            if not (p_dir / ex / safe / "candles.parquet").exists():
                missing_files = True
                break
        if missing_files:
            break

    if not force and not missing_files and newest_ts > 0 and gap_minutes < max_gap_minutes:
        log.info("Архив 1m свечей свежий (отставание %.1f мин < %d мин, все файлы на месте) — мгновенный старт",
                 gap_minutes, max_gap_minutes)
        return False

    log.info("Синхронизация свечей (отставание %.1f мин, новые монеты: %s). Автоматическая докачка...",
             gap_minutes, missing_files)
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=len(target_exs)) as executor:
        futures = {executor.submit(update_exchange, ex, target_symbols, p_dir, retention_days): ex
                   for ex in target_exs}
        for future in as_completed(futures):
            ex = futures[future]
            try:
                ex, succ, fail = future.result()
                log.info("[%s] Докачано свечей: %d успешно, %d ошибок", ex, succ, fail)
            except Exception as e:
                log.warning("[%s] Ошибка потока докачки свечей: %s", ex, e)

    dt = time.time() - t0
    log.info("Синхронизация свечей завершена за %.1fс (ротация: хранятся последние %d дней)",
             dt, retention_days)
    return True
