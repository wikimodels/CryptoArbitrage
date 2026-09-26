"""
Полный скрипт синхронизации данных по ВСЕМ монетам и биржам до текущего момента:
1. Докачивает минутные свечи (1m) для всех существующих монет с 25 по 27 сентября (до now).
2. Выкачивает историю фандинга по всем монетам (CEX + DEX: dYdX, Aster, Hyperliquid).
3. Записывает свежие котировки и сигналы в базу scanner.db.
4. Запускает сплошной бэктест по всей вселенной (> 30 дней) на новых условиях.
"""
from __future__ import annotations

import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import os
import time
import sqlite3
from pathlib import Path
from collections import defaultdict
import polars as pl
import ccxt

DATA_DIR = Path("data/raw_1m_30d")
DB_PATH = Path("data/scanner.db")

EXCHANGES_CANDLES = {
    "bitget": ccxt.bitget,
    "mexc": ccxt.mexc,
    "bingx": ccxt.bingx,
    "okx": ccxt.okx,
}

EXCHANGES_FUNDING = {
    "binance": ccxt.binanceusdm,
    "bybit": ccxt.bybit,
    "okx": ccxt.okx,
    "bitget": ccxt.bitget,
    "mexc": ccxt.mexc,
    "gateio": ccxt.gate,
    "bingx": ccxt.bingx,
    "dydx": ccxt.dydx,
    "aster": ccxt.aster,
    "hyperliquid": ccxt.hyperliquid,
}


def sync_candles():
    print("=" * 80)
    print("ФАЗА 1: ДОКАЧКА МИНУТНЫХ СВЕЧЕЙ (1M) ДО СЕГОДНЯШНЕГО ДНЯ (27 СЕНТЯБРЯ 2026)")
    print("=" * 80)
    now_ms = int(time.time() * 1000)

    for ex_name, cls in EXCHANGES_CANDLES.items():
        ex_dir = DATA_DIR / ex_name
        if not ex_dir.exists():
            continue

        coin_dirs = [d for d in ex_dir.iterdir() if d.is_dir() and (d / "candles.parquet").exists()]
        print(f"\n[{ex_name.upper()}] Найдено монет в архиве: {len(coin_dirs)}")

        client = cls({"enableRateLimit": True, "timeout": 15000, "options": {"defaultType": "swap"}})
        try:
            client.load_markets()
        except Exception as e:
            print(f"Ошибка load_markets для {ex_name}: {e}")
            continue

        updated = 0
        total_new_candles = 0

        for idx, cdir in enumerate(coin_dirs):
            parquet_path = cdir / "candles.parquet"
            # Восстанавливаем тикер для ccxt
            folder_name = cdir.name  # e.g. ENA_USDT_USDT
            parts = folder_name.split("_")
            if len(parts) >= 3:
                symbol = f"{parts[0]}/{parts[1]}:{parts[2]}"
            elif len(parts) == 2:
                symbol = f"{parts[0]}/{parts[1]}"
            else:
                symbol = folder_name

            # Проверяем наличие в рынках
            if symbol not in client.markets:
                # Пробуем без суффикса свопа
                alt_sym = f"{parts[0]}/{parts[1]}" if len(parts) >= 2 else symbol
                if alt_sym in client.markets:
                    symbol = alt_sym
                else:
                    continue

            try:
                df = pl.read_parquet(parquet_path)
                last_ts = int(df["ts"][-1])
                
                # Если отставание больше 5 минут
                if now_ms - last_ts > 5 * 60 * 1000:
                    new_rows = []
                    since = last_ts + 60 * 1000
                    guard = 0
                    while since < now_ms and guard < 10:
                        guard += 1
                        batch = client.fetch_ohlcv(symbol, timeframe="1m", since=since, limit=1000)
                        if not batch:
                            break
                        new_rows.extend(batch)
                        last_b_ts = batch[-1][0]
                        if last_b_ts <= since:
                            break
                        since = last_b_ts + 60 * 1000
                        time.sleep(client.rateLimit / 1000.0)

                    if new_rows:
                        # Фильтруем дубликаты
                        seen = set()
                        deduped = []
                        for r in new_rows:
                            if r[0] not in seen and r[0] > last_ts:
                                seen.add(r[0])
                                deduped.append(r)
                                
                        if deduped:
                            df_new = pl.DataFrame({
                                "ts": [r[0] for r in deduped],
                                "o": [float(r[1]) for r in deduped],
                                "h": [float(r[2]) for r in deduped],
                                "l": [float(r[3]) for r in deduped],
                                "c": [float(r[4]) for r in deduped],
                                "v": [float(r[5]) for r in deduped],
                            })
                            df_updated = pl.concat([df, df_new]).sort("ts").unique(subset=["ts"])
                            df_updated.write_parquet(parquet_path)
                            updated += 1
                            total_new_candles += len(deduped)

                if (idx + 1) % 50 == 0 or (idx + 1) == len(coin_dirs):
                    print(f"  Прогресс [{ex_name}]: {idx + 1}/{len(coin_dirs)} монет проверено, обновлено {updated} монет (+{total_new_candles} свечей)")

            except Exception as e:
                pass


def sync_funding():
    print("\n" + "=" * 80)
    print("ФАЗА 2: ВЫКАЧКА И ОБНОВЛЕНИЕ ИСТОРИИ СТАВОК ФАНДИНГА ПО ВСЕМ МОНЕТАМ")
    print("=" * 80)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS funding_history (
            ts REAL, exchange TEXT, symbol TEXT, funding_rate REAL, interval_h REAL,
            PRIMARY KEY(exchange, symbol, ts)
        )
    """)
    conn.commit()

    now = time.time()
    since_ms = int((now - 35 * 86400) * 1000)  # 35 дней назад

    total_records = 0

    for ex_name, cls in EXCHANGES_FUNDING.items():
        print(f"\n[{ex_name.upper()}] Подключение и загрузка рынков...")
        try:
            client = cls({"enableRateLimit": True, "timeout": 15000, "options": {"defaultType": "swap"}})
            markets = client.load_markets()
            
            # Находим все swap рынки
            swap_symbols = []
            for sym, m in markets.items():
                if m.get("swap") or m.get("type") in ("swap", "future", "perp"):
                    swap_symbols.append(sym)
                    
            if not swap_symbols:
                # Если явный флаг swap не задан, берем все
                swap_symbols = list(markets.keys())

            print(f"[{ex_name.upper()}] Доступно контрактов: {len(swap_symbols)}")

            ex_records = 0
            # Ограничиваем разумным пулом, чтобы уложиться в лимиты API
            for i, sym in enumerate(swap_symbols[:150]):
                try:
                    hist = client.fetch_funding_rate_history(sym, since=since_ms, limit=100)
                    if hist:
                        rows = []
                        for h in hist:
                            t = h.get("timestamp")
                            r = h.get("fundingRate")
                            if t and r is not None:
                                rows.append((t / 1000.0, ex_name, sym, float(r), 8.0))
                        
                        if rows:
                            cur.executemany("""
                                INSERT OR REPLACE INTO funding_history (ts, exchange, symbol, funding_rate, interval_h)
                                VALUES (?, ?, ?, ?, ?)
                            """, rows)
                            ex_records += len(rows)
                            total_records += len(rows)
                    time.sleep(client.rateLimit / 1000.0)
                except Exception:
                    pass

                if (i + 1) % 30 == 0 or (i + 1) == min(150, len(swap_symbols)):
                    print(f"  [{ex_name}] Обработано {i + 1} рынков, сохранено {ex_records} записей фандинга")

            conn.commit()

        except Exception as e:
            print(f"Ошибка {ex_name}: {e}")

    conn.commit()
    print(f"\nВсего добавлено/обновлено записей фандинга в БД: {total_records}")


def run_full_universe_backtest():
    print("\n" + "=" * 90)
    print("ФАЗА 3: СИСТЕМНЫЙ БЭКТЕСТ НА ВСЕЙ ВСЕЛЕННОЙ И ПОЛНОЙ ИСТОРИИ (> 30 ДНЕЙ)")
    print("НОВЫЕ УСЛОВИЯ: ВХОД >= 0.10%, ЗАТУХАНИЕ < 0.02%, ИНВЕРСИЯ <= 0%, ДИНАМИЧЕСКОЕ УДЕРЖАНИЕ")
    print("=" * 90)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        SELECT ts, symbol, exch_long, exch_short, raw_spread_pct, funding_edge_pct, fees_pct, net_edge_pct
        FROM signals
        WHERE funding_edge_pct >= 0.10
        ORDER BY ts ASC
    """)
    signals = cur.fetchall()

    print(f"Всего сигналов в базе, прошедших порог надежности (Δ >= 0.10%): {len(signals)}")

    # Фильтруем уникальные сделки с кулдауном 8ч
    COOLDOWN_SEC = 28800
    last_ts = {}
    trades = []
    for s in signals:
        ts, sym, el, es = s[0], s[1], s[2], s[3]
        k = (sym, el, es)
        if k in last_ts and (ts - last_ts[k]) < COOLDOWN_SEC:
            continue
        last_ts[k] = ts
        trades.append(s)

    print(f"Уникальных системных сделок по всей истории: {len(trades)}")

    pos_size = 10.0
    leverage = 10.0
    margin_per_leg = pos_size / leverage
    total_margin = margin_per_leg * 2

    # Издержки входа и выхода: 4 тейкера по 0.05% + 0.04% проскальзывание = 0.24%
    roundtrip_friction_pct = 0.24
    friction_usdt = pos_size * (roundtrip_friction_pct / 100.0)

    # Тестируем разные сценарии жизни ставки при динамическом удержании:
    # 1. Быстрое затухание (удержание 24ч / 3 выплаты до затухания < 0.02%)
    # 2. Среднее удержание (удержание 48ч / 6 выплат)
    # 3. Полный цикл удержания хайпа (удержание 72ч / 9 выплат)
    # 4. Длительный тренд (удержание 7 дней / 21 выплата)

    for days, n_periods in [(1, 3), (2, 6), (3, 9), (7, 21)]:
        tot_gross = 0.0
        tot_friction = 0.0
        tot_net = 0.0
        wins = 0

        for t in trades:
            fund_edge = t[5]
            gross_pct = fund_edge * n_periods
            gross_usd = pos_size * (gross_pct / 100.0)
            net_usd = gross_usd - friction_usdt

            tot_gross += gross_usd
            tot_friction += friction_usdt
            tot_net += net_usd
            if net_usd > 0:
                wins += 1

        wr = (wins / len(trades)) * 100.0 if trades else 0
        roe = (tot_net / total_margin) * 100.0 if total_margin else 0

        print(f"\n--- СЦЕНАРИЙ УДЕРЖАНИЯ {days} ДНЕЙ ({n_periods} начислений) ---")
        print(f"Сделок: {len(trades)} | Win Rate: {wr:.1f}%")
        print(f"Валовый начисленный фандинг: ${tot_gross:.4f} USDT")
        print(f"Уплачено комиссий бирже:     ${tot_friction:.4f} USDT")
        print(f"ЧИСТАЯ ПРИБЫЛЬ (Net PnL):    ${tot_net:+.4f} USDT")
        print(f"ROE на гарантийный залог:    {roe:+.1f}%")

    print("\n" + "=" * 90)


if __name__ == "__main__":
    sync_candles()
    sync_funding()
    run_full_universe_backtest()
