from pathlib import Path

for ex in ['bitget', 'okx', 'mexc', 'bingx']:
    p = Path(f'data/raw_1m_30d/{ex}')
    if p.exists():
        coins = {x.name.split('_')[0] for x in p.iterdir() if (x / 'candles.parquet').exists()}
        top_check = ['BTC', 'ETH', 'SOL', 'DOGE', 'XRP', 'ADA', 'AVAX', 'SUI', 'APT', 'NEAR', 'PEPE', 'WIF', 'ARB', 'OP', 'TIA', 'LINK', 'LDO', 'INJ', 'FET']
        found = [c for c in top_check if c in coins]
        print(f"[{ex}] total {len(coins)} coins. Top found ({len(found)}): {found}")
