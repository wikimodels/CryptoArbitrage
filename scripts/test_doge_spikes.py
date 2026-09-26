import polars as pl
from datetime import datetime, timezone

candles = pl.read_parquet('data/raw_1m_30d/bitget/DOGE_USDT_USDT/candles.parquet').sort('ts')
oi = pl.read_parquet('data/raw_oi_30d/binance/DOGE_USDT_USDT/oi.parquet').sort('ts')

c_start = datetime.fromtimestamp(candles['ts'].min()/1000, tz=timezone.utc)
c_end = datetime.fromtimestamp(candles['ts'].max()/1000, tz=timezone.utc)
oi_start = datetime.fromtimestamp(oi['ts'].min()/1000, tz=timezone.utc)
oi_end = datetime.fromtimestamp(oi['ts'].max()/1000, tz=timezone.utc)

print(f"Candles DOGE: {candles.shape[0]} rows, from {c_start} to {c_end}")
print(f"OI DOGE: {oi.shape[0]} rows, from {oi_start} to {oi_end}")

# 24h rolling volume mean (1440 candles)
candles = candles.with_columns([
    pl.col('v').rolling_mean(window_size=1440).alias('vol_mean_24h')
]).with_columns([
    (pl.col('v') / pl.col('vol_mean_24h')).alias('rvol')
])

spikes_5x = candles.filter(pl.col('rvol') >= 5.0)
spikes_10x = candles.filter(pl.col('rvol') >= 10.0)
print(f"Total 1m candles with RVOL >= 5x: {len(spikes_5x)}")
print(f"Total 1m candles with RVOL >= 10x: {len(spikes_10x)}")

# Print 3 sample spikes
for row in spikes_5x.head(3).iter_rows(named=True):
    ts_dt = datetime.fromtimestamp(row['ts']/1000, tz=timezone.utc)
    print(f"Spike at {ts_dt}: O={row['o']}, C={row['c']}, V={row['v']:.0f}, RVOL={row['rvol']:.1f}x")
