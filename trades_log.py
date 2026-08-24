import sqlite3, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
conn = sqlite3.connect("data/scanner.db")
print("=== ВСЕ ЗАКРЫТЫЕ СДЕЛКИ ===")
rows = conn.execute("""SELECT symbol, strategy, exch_long, exch_short, entry_net_edge_pct,
    exit_net_edge_pct, price_pnl_usdt, fees_usdt, funding_usdt, realized_pnl_usdt,
    holding_seconds, status FROM emulator_trades WHERE status='closed'
    ORDER BY open_ts""").fetchall()
if not rows:
    print("пока пусто")
for r in rows:
    print("%s [%s] %s/%s entry=%.3f%% exit=%.3f%% | price=%+.2f fees=-%.2f fund=%+.2f => %+.2f | %.0fs" % (
        r[0][:16], r[1], r[2], r[3], r[4] or 0, r[5] or 0, r[6] or 0, r[7] or 0, r[8] or 0, r[9] or 0, r[10] or 0))
print()
print("=== ОТКРЫТЫЕ ===")
for r in conn.execute("""SELECT symbol, strategy, entry_net_edge_pct, open_ts FROM emulator_trades WHERE status='open'"""):
    print("  %s [%s] entry=%.3f%%" % (r[0][:20], r[1], r[2] or 0))
conn.close()
