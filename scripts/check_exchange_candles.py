import polars as pl

for ex in ['bitget', 'okx', 'mexc', 'bingx']:
    path = f'data/raw_1m_30d/{ex}/DOGE_USDT_USDT/candles.parquet'
    try:
        df = pl.read_parquet(path)
        print(f"[{ex}] rows: {df.shape[0]}, min_ts: {df['ts'].min()}, max_ts: {df['ts'].max()}")
    except Exception as e:
        print(f"[{ex}] error: {e}")
