import os
from pathlib import Path
import polars as pl

base_dir = Path("data/raw_1m_30d")
exchanges = [d.name for d in base_dir.iterdir() if d.is_dir()]
print("Available exchanges:", exchanges)

for ex in exchanges:
    symbols = [d.name for d in (base_dir / ex).iterdir() if d.is_dir() and (d / "candles.parquet").exists()]
    print(f"  {ex}: {len(symbols)} coins with candles.parquet")

sample_file = next(base_dir.rglob("candles.parquet"))
print("\nSample file:", sample_file)
df = pl.read_parquet(sample_file)
print("Shape:", df.shape)
print("Columns:", df.columns)
print("Schema:", df.schema)
print("First row:", df.row(0))
print("Last row:", df.row(-1))
