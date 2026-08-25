import sqlite3, sys, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
conn = sqlite3.connect("data/scanner.db")
print("=== ЗАКРЫТЫЕ СКАЛЬПЫ ===")
for r in conn.execute("""SELECT symbol, strategy, exch_long, exch_short, entry_net_edge_pct,
    price_pnl_usdt, fees_usdt, funding_usdt, realized_pnl_usdt, holding_seconds, open_ts, close_ts
    FROM emulator_trades WHERE status='closed' AND strategy='scalp'"""):
    print("%s %s/%s entry=%.2f%% | price=%+.3f fees=-%.2f fund=%+.3f => %+.2f | %.0fs" % (
        r[0][:20], r[2], r[3], r[4] or 0, r[5] or 0, r[6] or 0, r[7] or 0, r[8] or 0, r[9] or 0))
conn.close()

import asyncio, websockets
async def ws_part():
    async with websockets.connect("ws://127.0.0.1:8080/ws") as ws:
        d = json.loads(await asyncio.wait_for(ws.recv(), 10))
        print("\n=== ПРОВЕРКА ВИСЯЩИХ ПОЗИЦИЙ (есть ли котировки) ===")
        exs = {e["name"]: e for e in d.get("exchanges", [])}
        for e in exs.values():
            print("  %-8s ws_alive=%s fresh=%s" % (e["name"], e["ws_alive"], e["fresh_symbols"]))
        targets = {"ONE/USDT:USDT", "FLOKI/USDT:USDT"}
        found = [s for s in d.get("spreads", []) if s["symbol"] in targets]
        print("в спред-таблице:", [(s["symbol"], s["exch_long"], s["exch_short"]) for s in found] or "нет")
asyncio.run(ws_part())
