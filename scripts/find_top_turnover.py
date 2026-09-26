from pathlib import Path
import polars as pl

# Find all 156 common coins
exchanges = ['bitget', 'okx', 'mexc', 'bingx']
sets = []
for ex in exchanges:
    p = Path(f'data/raw_1m_30d/{ex}')
    coins = {x.name.split('_')[0] for x in p.iterdir() if (x / 'candles.parquet').exists()}
    sets.append(coins)
common = set.intersection(*sets)

# For each coin, compute average daily turnover (USD volume) over last 30 days from Bitget candles
stats = []
for coin in common:
    p = Path(f'data/raw_1m_30d/bitget/{coin}_USDT_USDT/candles.parquet')
    try:
        df = pl.read_parquet(p)
        # Turnover ~ volume * close
        avg_price = df['c'].mean()
        tot_vol = df['v'].sum()
        days = len(df) / 1440.0
        daily_usd_turnover = (tot_vol * avg_price) / max(days, 1.0)
        stats.append((coin, daily_usd_turnover))
    except Exception:
        continue

stats.sort(key=lambda x: x[1], reverse=True)
print("Top 35 highest turnover coins in our local 1m database:")
for rank, (c, turn) in enumerate(stats[:35], 1):
    print(f"{rank:>2}. {c:<12}: ${turn/1e6:>8.2f}M/day")
