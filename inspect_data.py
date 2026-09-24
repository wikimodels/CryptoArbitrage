from pathlib import Path
import polars as pl
from datetime import datetime

for root_name in ["data/raw_1m_30d", "data/raw_1m"]:
    root = Path(root_name)
    print(f"\n--- {root_name} ---")
    exchanges = [d.name for d in root.iterdir() if d.is_dir()]
    print("Exchanges:", exchanges)
    for ex in exchanges:
        parquets = list((root / ex).glob("*/candles.parquet"))
        print(f"  {ex}: {len(parquets)} symbols")
        if parquets:
            sample = parquets[0]
            df = pl.read_parquet(sample)
            t_min = df["ts"].min()
            t_max = df["ts"].max()
            print(f"    Sample {sample.parent.name}: {len(df)} rows, from {datetime.utcfromtimestamp(t_min/1000)} to {datetime.utcfromtimestamp(t_max/1000)}")
