from pathlib import Path
import ccxt

ex = ccxt.binance({"options": {"defaultType": "swap"}})
markets = ex.load_markets()

# Check what coins exist in Bitget folder
bg_coins = {x.name.split('_')[0] for x in Path('data/raw_1m_30d/bitget').iterdir() if (x / 'candles.parquet').exists()}

# Candidate new high-turnover coins
candidates = [
    "UNI", "WLD", "BNB", "BCH", "XLM", "RAY", "DASH", "HBAR", "ETHFI", 
    "VIRTUAL", "COMP", "FIL", "SHIB", "ALGO", "KAVA", "SAND", "MANA", 
    "AXS", "DYDX", "SUI", "NEAR", "LINK", "PEPE", "1000BONK", "1INCH", "MKR"
]

available = []
for c in candidates:
    if c in bg_coins:
        sym = f"{c}/USDT:USDT"
        if sym in markets:
            available.append((c, sym))
        else:
            # Check alternative symbol (e.g. 1000PEPE or similar)
            for m in markets:
                if m.startswith(c) and m.endswith("/USDT:USDT"):
                    available.append((c, m))
                    break

print(f"Total verified new candidate coins: {len(available)}")
for c, sym in available:
    print(f"  Coin: {c:<10} -> Binance Symbol: {sym}")
