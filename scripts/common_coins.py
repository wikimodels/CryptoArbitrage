from pathlib import Path

exchanges = ['bitget', 'okx', 'mexc', 'bingx']
sets = []
for ex in exchanges:
    p = Path(f'data/raw_1m_30d/{ex}')
    coins = {x.name.split('_')[0] for x in p.iterdir() if (x / 'candles.parquet').exists()}
    sets.append(coins)

common = set.intersection(*sets)
print(f"Total coins common to all 4 exchanges: {len(common)}")
print("Common coins:", sorted(list(common)))
