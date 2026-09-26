import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import ccxt

exchanges = {
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

for name, cls in exchanges.items():
    try:
        ex = cls({"enableRateLimit": True, "timeout": 10000, "options": {"defaultType": "swap"}})
        has_fund = bool(ex.has.get("fetchFundingRateHistory") or ex.has.get("fetchFundingRate"))
        has_ohlcv = bool(ex.has.get("fetchOHLCV"))
        print(f"[{name:<11}] hasFundingHistory: {has_fund:<5} | hasOHLCV: {has_ohlcv:<5}")
    except Exception as err:
        print(f"[{name:<11}] ERROR: {err}")
