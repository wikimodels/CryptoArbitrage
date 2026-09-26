import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import time
from pathlib import Path
import polars as pl
import ccxt

ex = ccxt.bitget({"enableRateLimit": True, "timeout": 15000, "options": {"defaultType": "swap"}})
sym = "ENA/USDT:USDT"
p = Path("data/raw_1m_30d/bitget/ENA_USDT_USDT/candles.parquet")

if p.exists():
    df = pl.read_parquet(p)
    last_ts = int(df["ts"][-1])
    print(f"Last ts in parquet: {last_ts} ({time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(last_ts/1000))})")
    now_ms = int(time.time() * 1000)
    print(f"Current time: {now_ms} ({time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(now_ms/1000))})")
    
    # Fetch new candles
    batch = ex.fetch_ohlcv(sym, timeframe="1m", since=last_ts + 60000, limit=100)
    print(f"Fetched new candles: {len(batch)}")
    if batch:
        print(f"First new: {batch[0][0]}, Last new: {batch[-1][0]}")
