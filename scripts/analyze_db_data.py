import sqlite3
from datetime import datetime

conn = sqlite3.connect('data/scanner.db')
c = conn.cursor()

print("--- EXCHANGES IN QUOTES ---")
exchs = c.execute("SELECT exchange, COUNT(*), MIN(funding_rate), MAX(funding_rate), AVG(funding_rate) FROM quotes GROUP BY exchange").fetchall()
for ex in exchs:
    print(ex)

print("\n--- TIMESTAMPS IN QUOTES ---")
min_ts, max_ts = c.execute("SELECT MIN(ts), MAX(ts) FROM quotes").fetchone()
print(f"Min: {datetime.fromtimestamp(min_ts)}, Max: {datetime.fromtimestamp(max_ts)}, Duration: {(max_ts - min_ts) / 86400:.2f} days")

print("\n--- EXCHANGES IN SIGNALS ---")
long_exchs = c.execute("SELECT exch_long, COUNT(*) FROM signals GROUP BY exch_long").fetchall()
print("Long exchanges:", long_exchs)
short_exchs = c.execute("SELECT exch_short, COUNT(*) FROM signals GROUP BY exch_short").fetchall()
print("Short exchanges:", short_exchs)

print("\n--- SIGNALS FUNDING EDGE PCT STATS ---")
stats = c.execute("""
    SELECT MIN(funding_edge_pct), MAX(funding_edge_pct), AVG(funding_edge_pct), 
           COUNT(*),
           SUM(CASE WHEN funding_edge_pct >= 0.05 THEN 1 ELSE 0 END),
           SUM(CASE WHEN funding_edge_pct >= 0.08 THEN 1 ELSE 0 END),
           SUM(CASE WHEN funding_edge_pct >= 0.10 THEN 1 ELSE 0 END),
           SUM(CASE WHEN funding_edge_pct >= 0.15 THEN 1 ELSE 0 END),
           SUM(CASE WHEN funding_edge_pct >= 0.20 THEN 1 ELSE 0 END)
    FROM signals
""").fetchone()
print("Funding Edge Stats:")
print(f"Min: {stats[0]:.4f}%, Max: {stats[1]:.4f}%, Avg: {stats[2]:.4f}%, Total: {stats[3]}")
print(f">= 0.05%: {stats[4]} ({stats[4]/stats[3]*100:.1f}%)")
print(f">= 0.08%: {stats[5]} ({stats[5]/stats[3]*100:.1f}%)")
print(f">= 0.10%: {stats[6]} ({stats[6]/stats[3]*100:.1f}%)")
print(f">= 0.15%: {stats[7]} ({stats[7]/stats[3]*100:.1f}%)")
print(f">= 0.20%: {stats[8]} ({stats[8]/stats[3]*100:.1f}%)")
