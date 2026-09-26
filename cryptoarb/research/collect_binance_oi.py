"""
Binance Futures Open Interest (5m) Historical Collector (30 Days)
Collects 5-minute resolution Open Interest (base and USD value) from Binance Futures
for research and backtesting.
"""

import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import ccxt
import polars as pl
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("collect_oi")

# Combined master list of 48 liquid coins
ALL_COINS = [
    # Initial 20 coins (already collected)
    "DOGE", "ADA", "AVAX", "APT", "WIF", "ARB", "OP", "TIA", 
    "LDO", "INJ", "FET", "AAVE", "ATOM", "RENDER", "SEI", 
    "ENA", "JUP", "CRV", "DOT", "GALA",
    # 28 New High-Turnover coins
    "UNI", "WLD", "BNB", "BCH", "XLM", "RAY", "DASH", "HBAR", 
    "ETHFI", "VIRTUAL", "COMP", "FIL", "ALGO", "SAND", "MANA", 
    "AXS", "DYDX", "1000BONK", "1INCH", "PYTH", "PENDLE", "GRT", 
    "STX", "CHZ", "ARKM", "SNX", "MAGIC", "EIGEN"
]

OUTPUT_DIR = Path("data/raw_oi_30d/binance")


def collect_oi_for_symbol(ex: ccxt.binance, coin: str, days: int = 30) -> Path | None:
    target_path = OUTPUT_DIR / f"{coin}_USDT_USDT" / "oi.parquet"
    
    # Check if already collected and complete (> 8000 points)
    if target_path.exists():
        try:
            existing = pl.read_parquet(target_path)
            if len(existing) >= 8000:
                logger.info(f"Skipping {coin}: already has {len(existing)} points in {target_path}")
                return target_path
        except Exception:
            pass

    symbol = f"{coin}/USDT:USDT"
    logger.info(f"Collecting 5m Open Interest for {symbol} for last {days} days...")
    
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 24 * 3600 * 1000
    
    current_since = start_ms
    all_records = []
    
    while current_since < now_ms:
        try:
            records = ex.fetch_open_interest_history(
                symbol,
                timeframe="5m",
                since=current_since,
                limit=500,
            )
        except Exception as e:
            logger.warning(f"Error fetching {symbol} at {current_since}: {e}")
            time.sleep(1.0)
            try:
                records = ex.fetch_open_interest_history(symbol, timeframe="5m", since=current_since, limit=500)
            except Exception as e2:
                logger.error(f"Failed retry for {symbol}: {e2}")
                break
                
        if not records:
            break
            
        for r in records:
            ts = int(r["timestamp"])
            oi_base = float(r.get("openInterestAmount") or (r.get("info", {}).get("sumOpenInterest", 0.0)))
            oi_usd = float(r.get("openInterestValue") or (r.get("info", {}).get("sumOpenInterestValue", 0.0)))
            all_records.append({
                "ts": ts,
                "oi_base": oi_base,
                "oi_usd": oi_usd,
            })
            
        last_ts = records[-1]["timestamp"]
        if last_ts <= current_since:
            current_since += 500 * 5 * 60 * 1000
        else:
            current_since = last_ts + 5 * 60 * 1000
            
        time.sleep(0.08)  # Fast rate limit
        
    if not all_records:
        logger.warning(f"No OI records collected for {symbol}")
        return None
        
    df = pl.DataFrame(all_records)
    df = df.unique(subset=["ts"]).sort("ts")
    
    target_path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(target_path)
    
    logger.info(
        f"Saved {len(df)} 5m OI points for {coin} to {target_path} "
        f"({datetime.fromtimestamp(df['ts'][0]/1000, tz=timezone.utc):%Y-%m-%d} to "
        f"{datetime.fromtimestamp(df['ts'][-1]/1000, tz=timezone.utc):%Y-%m-%d})"
    )
    return target_path


def main():
    ex = ccxt.binance({
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",
        },
    })
    
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    success = 0
    for coin in ALL_COINS:
        res = collect_oi_for_symbol(ex, coin, days=30)
        if res:
            success += 1
            
    logger.info(f"Done! Successfully verified/collected OI for {success}/{len(ALL_COINS)} coins.")


if __name__ == "__main__":
    main()
