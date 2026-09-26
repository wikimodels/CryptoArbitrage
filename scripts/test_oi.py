import ccxt
import time
from datetime import datetime, timezone

ex = ccxt.binance({'options': {'defaultType': 'swap'}})
now = int(time.time() * 1000)
since = now - 30 * 24 * 3600 * 1000

print(f"Testing Binance fetch_open_interest_history since {datetime.fromtimestamp(since/1000, tz=timezone.utc)}...")
try:
    res = ex.fetch_open_interest_history('DOGE/USDT:USDT', timeframe='5m', since=since, limit=500)
    print(f"Success! DOGE/USDT records: {len(res)}")
    if res:
        print(f"First record: {res[0]}")
        print(f"Last record: {res[-1]}")
except Exception as e:
    print(f"Error: {e}")
