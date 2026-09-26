import polars as pl
from pathlib import Path

whitelist = ['WLD', 'INJ', 'OP', 'COMP', 'LDO', 'DYDX', 'ENA', 'UNI', 'BCH', 'GRT', 'MAGIC', 'BNB', 'ADA', '1000BONK', 'FET', 'AAVE']
for c in whitelist:
    p = Path(f'data/raw_1m_30d/bitget/{c}_USDT_USDT/candles.parquet')
    if p.exists():
        df = pl.read_parquet(p)
        usd_v = df['v'] * df['c']
        avg_vol = usd_v.mean()
        p95_vol = usd_v.quantile(0.95)
        p99_vol = usd_v.quantile(0.99)
        print(f"{c:<10}: 1m Mean = ${avg_vol:>7,.0f} | 95th Pct (Spike) = ${p95_vol:>8,.0f} | 99th Pct = ${p99_vol:>9,.0f}")
