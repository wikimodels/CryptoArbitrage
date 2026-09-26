import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import time
import ccxt

print("--- TESTING DYDX FUNDING HISTORY ---")
try:
    dydx = ccxt.dydx({"enableRateLimit": True, "timeout": 15000})
    dydx.load_markets()
    # Find a major market like BTC/USDC:USDC or ETH/USDC:USDC
    sym = "BTC/USDC:USDC"
    if sym in dydx.markets:
        now_ms = int(time.time() * 1000)
        since_ms = now_ms - 30 * 86400 * 1000
        hist = dydx.fetch_funding_rate_history(sym, since=since_ms, limit=10)
        print(f"dYdX {sym} funding items: {len(hist)}")
        if hist:
            print("Sample dYdX funding:", hist[0])
    else:
        print(f"{sym} not found in dYdX markets: {list(dydx.markets.keys())[:5]}")
except Exception as e:
    print(f"dYdX error: {e}")

print("\n--- TESTING ASTER FUNDING HISTORY ---")
try:
    aster = ccxt.aster({"enableRateLimit": True, "timeout": 15000})
    aster.load_markets()
    print("Aster markets count:", len(aster.markets))
    # Pick first market
    sample_sym = list(aster.markets.keys())[0]
    print(f"Testing Aster symbol: {sample_sym}")
    hist = aster.fetch_funding_rate_history(sample_sym, limit=10)
    print(f"Aster {sample_sym} funding items: {len(hist)}")
    if hist:
        print("Sample Aster funding:", hist[0])
except Exception as e:
    print(f"Aster error: {e}")
