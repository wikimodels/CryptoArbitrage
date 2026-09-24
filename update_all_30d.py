"""
Докачка 1-минутных свечей по топ-40 монетам до сегодняшнего дня для data/raw_1m_30d.
Запуск в 4 потока (по одному на каждую биржу: okx, bitget, mexc, bingx).
"""
import sys
import time
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import polars as pl

from cryptoarb.connectors.ccxt_connector import CCXT_ID_MAP
from cryptoarb.backtest.collect import _client

ROOT = Path("data/raw_1m_30d")
TOP40_FILE = Path("output/top40_4ex.txt")
EXCHANGES = ["okx", "bitget", "mexc", "bingx"]


def fetch_1m_safe(ex_client, symbol: str, since_ms: int, until_ms: int, limit: int = 1000):
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
            except Exception as e:
                # small pause on error and retry once
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


def update_exchange(ex: str, symbols: list[str]):
    print(f"[{ex}] Starting update for {len(symbols)} symbols...", flush=True)
    ex_client = _client(ex)
    success_count = 0
    fail_count = 0
    now_ms = int(time.time() * 1000)

    for idx, sym in enumerate(symbols, 1):
        safe = sym.replace("/", "_").replace(":", "_")
        p = ROOT / ex / safe / "candles.parquet"
        
        try:
            if p.exists():
                df = pl.read_parquet(p)
                max_ts = int(df["ts"].max())
                since_ms = max_ts + 60_000
                if now_ms - since_ms < 60_000:
                    print(f"[{ex}] ({idx}/{len(symbols)}) {sym}: already up to date ({datetime.utcfromtimestamp(max_ts/1000)} UTC)", flush=True)
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
                    merged = pl.concat([df, new_df]).unique(subset=["ts"]).sort("ts")
                    merged.write_parquet(p)
                    print(f"[{ex}] ({idx}/{len(symbols)}) {sym}: +{len(new_rows)} rows -> {len(merged)} total (to {datetime.utcfromtimestamp(merged['ts'].max()/1000)} UTC)", flush=True)
                else:
                    print(f"[{ex}] ({idx}/{len(symbols)}) {sym}: 0 new rows (kept {len(df)} rows)", flush=True)
                success_count += 1
            else:
                # full 35 days fetch
                since_ms = now_ms - 35 * 86400 * 1000
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
                    print(f"[{ex}] ({idx}/{len(symbols)}) {sym}: fetched initial {len(df)} rows", flush=True)
                    success_count += 1
                else:
                    print(f"[{ex}] ({idx}/{len(symbols)}) {sym}: symbol not available or 0 rows", flush=True)
                    fail_count += 1
        except Exception as e:
            print(f"[{ex}] ({idx}/{len(symbols)}) {sym} ERROR: {e}", flush=True)
            fail_count += 1

    print(f"[{ex}] FINISHED: {success_count} success, {fail_count} failed/skipped", flush=True)
    return ex, success_count, fail_count


def main():
    if not TOP40_FILE.exists():
        print(f"File not found: {TOP40_FILE}")
        return
    symbols = [l.strip() for l in open(TOP40_FILE, encoding="utf-8") if l.strip()]
    print(f"Loaded {len(symbols)} symbols from {TOP40_FILE}")
    print(f"Exchanges: {EXCHANGES}")
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=len(EXCHANGES)) as executor:
        futures = {executor.submit(update_exchange, ex, symbols): ex for ex in EXCHANGES}
        for future in as_completed(futures):
            ex = futures[future]
            try:
                ex, succ, fail = future.result()
            except Exception as e:
                print(f"[{ex}] Thread exception: {e}", flush=True)

    dt = time.time() - t0
    print(f"\nAll exchanges updated in {dt:.1f}s ({dt/60:.2f} min).", flush=True)


if __name__ == "__main__":
    main()
