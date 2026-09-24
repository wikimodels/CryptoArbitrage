import time
from pathlib import Path
import polars as pl
from datetime import datetime

from cryptoarb.backtest.collect import fetch_1m, save_candles, RAW_ROOT

ROOT = Path("data/raw_1m_30d")

def update_symbol(ex: str, sym: str):
    safe = sym.replace("/", "_").replace(":", "_")
    p = ROOT / ex / safe / "candles.parquet"
    now_ms = int(time.time() * 1000)
    
    if p.exists():
        df = pl.read_parquet(p)
        max_ts = int(df["ts"].max())
        since_ms = max_ts + 60_000
        if now_ms - since_ms < 60_000:
            print(f"[{ex}] {sym}: already up to date ({datetime.utcfromtimestamp(max_ts/1000)} UTC)")
            return
        print(f"[{ex}] {sym}: updating from {datetime.utcfromtimestamp(since_ms/1000)} to {datetime.utcfromtimestamp(now_ms/1000)} ({len(df)} existing rows)...")
        new_rows = fetch_1m(ex, sym, since_ms, now_ms)
        if not new_rows:
            print(f"[{ex}] {sym}: no new rows returned")
            return
        
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
        print(f"[{ex}] {sym}: added {len(new_rows)} rows -> total {len(merged)} rows (up to {datetime.utcfromtimestamp(merged['ts'].max()/1000)} UTC)")
    else:
        print(f"[{ex}] {sym}: file not found, fetching full 35 days...")
        since_ms = now_ms - 35 * 86400 * 1000
        rows = fetch_1m(ex, sym, since_ms, now_ms)
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
            print(f"[{ex}] {sym}: saved {len(df)} rows")

if __name__ == "__main__":
    # Test on APE/USDT:USDT for bingx
    update_symbol("bingx", "APE/USDT:USDT")
