import sqlite3
from pathlib import Path
import polars as pl

DB_PATH = Path("data/scanner.db")
CANDLES_DIR = Path("data/raw_1m_30d")
conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()
cur.execute("""
    SELECT ts, symbol, exch_long, exch_short, funding_edge_pct
    FROM signals
    WHERE funding_edge_pct >= 0.08
""")
rows = cur.fetchall()
print(f"Total signals >= 0.08: {len(rows)}")

matched = []
for r in rows:
    ts, sym, el, es, fund = r
    safe = sym.replace('/', '_').replace(':', '_')
    p_el = CANDLES_DIR / el / safe / "candles.parquet"
    p_es = CANDLES_DIR / es / safe / "candles.parquet"
    if p_el.exists() and p_es.exists():
        matched.append((r, p_el, p_es))

print(f"Matched signals with candles on both legs: {len(matched)}")
for (ts, sym, el, es, fund), p_el, p_es in matched:
    t_start = int(ts * 1000)
    t_24h = t_start + 24 * 3600 * 1000
    t_72h = t_start + 72 * 3600 * 1000
    df_l = pl.read_parquet(p_el).filter((pl.col("ts") >= t_start) & (pl.col("ts") <= t_72h))
    df_s = pl.read_parquet(p_es).filter((pl.col("ts") >= t_start) & (pl.col("ts") <= t_72h))
    j = df_l.join(df_s, on="ts", suffix="_s")
    if len(j) > 0:
        r0 = j["c"][0] / j["c_s"][0]
        dev_max = float(((j["c"] / j["c_s"]) / r0 - 1.0).abs().max()) * 100.0
        last_dev = abs(float((j["c"][-1] / j["c_s"][-1]) / r0 - 1.0)) * 100.0
        print(f"Coin: {sym} ({el} vs {es}), fund={fund:.4f}%, candles count={len(j)}, max_dev={dev_max:.2f}%, final_dev={last_dev:.2f}%")
