import asyncio, json
import websockets

async def t():
    async with websockets.connect("ws://127.0.0.1:8080/ws") as ws:
        d = json.loads(await asyncio.wait_for(ws.recv(), 10))
        pos = d.get("positions", [])
        st = d.get("stats", {})
        print("Позиций открыто:", len(pos))
        tot = 0.0
        for p in pos:
            u = p.get("unrealized_pnl_usdt")
            if u is not None:
                tot += u
            print("  [%s] %-18s %s->%s | entry_net=%.2f%% cur_spread=%s | unrealized=%s USDT | %.0f мин" % (
                p.get("strategy"), p.get("symbol","")[:18], p.get("exch_long"), p.get("exch_short"),
                p.get("entry_net_edge_pct") or 0,
                ("%.3f" % p["cur_spread_pct"]) if p.get("cur_spread_pct") is not None else "—",
                ("+%+.2f" % u) if u is not None else "—",
                (p.get("holding_seconds") or 0)/60))
        print("СУММАРНЫЙ нереализованный: %+.2f USDT" % tot)
        print("arb closed=%s pnl=%s | scalp closed=%s pnl=%s" % (
            st["arb"]["closed"], st["arb"]["pnl_usdt"], st["scalp"]["closed"], st["scalp"]["pnl_usdt"]))
        rank = d.get("scalp_rank", [])
        print("скальп-рейтинг: %s монет" % len(rank))
        for r in rank[:5]:
            print("  %-18s spikes=%s conv=%s%% capture=%s%% width=%s%% score=%s" % (
                r["symbol"][:18], r["spikes"], r["conv_rate"], r["avg_capture"], r["avg_width"], r["score"]))

asyncio.run(t())
