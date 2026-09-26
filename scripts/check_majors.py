from pathlib import Path

for ex in ['bitget', 'okx', 'mexc', 'bingx']:
    for c in ['BTC', 'ETH', 'SOL', 'XRP', 'BNB', 'NEAR', 'SUI', 'PEPE']:
        p = Path(f'data/raw_1m_30d/{ex}/{c}_USDT_USDT/candles.parquet')
        if p.exists():
            print(f"[{ex}] {c}: EXISTS ({p.stat().st_size / 1024:.0f} KB)")
