from pathlib import Path
import ccxt

ex = ccxt.binance({"options": {"defaultType": "swap"}})
markets = ex.load_markets()

bg_coins = {x.name.split('_')[0] for x in Path('data/raw_1m_30d/bitget').iterdir() if (x / 'candles.parquet').exists()}

candidates = ["PYTH", "PENDLE", "GRT", "STX", "CHZ", "ARKM", "SNX", "MAGIC", "BLUR", "EIGEN"]
found = []
for c in candidates:
    if c in bg_coins:
        sym = f"{c}/USDT:USDT"
        if sym in markets:
            found.append((c, sym))

print(f"Additional found: {len(found)}: {found}")
