import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import ccxt

for name in ['aster', 'dydx', 'apex', 'hyperliquid']:
    try:
        ex = getattr(ccxt, name)({'enableRateLimit': True, 'timeout': 15000})
        m = ex.load_markets()
        print(f"{name}: {len(m)} markets, samples: {list(m.keys())[:5]}")
    except Exception as err:
        print(f"{name} error: {err}")
