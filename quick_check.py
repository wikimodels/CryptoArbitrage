import asyncio, json
import websockets

async def t():
    async with websockets.connect("ws://127.0.0.1:8080/ws") as ws:
        d = json.loads(await asyncio.wait_for(ws.recv(), 10))
        alive = sum(1 for e in d.get("exchanges", []) if e.get("ws_alive"))
        fresh = sum(e.get("fresh_symbols", 0) for e in d.get("exchanges", []))
        sp = d.get("spreads", [])
        st = d.get("stats", {})
        print("WS OK | alive=%s/11 | fresh=%s | spreads=%s | pos=%s" % (
            alive, fresh, len(sp), st.get("open_positions")))
        print("arb closed=%s pnl=%s | scalp closed=%s pnl=%s" % (
            st["arb"]["closed"], st["arb"]["pnl_usdt"],
            st["scalp"]["closed"], st["scalp"]["pnl_usdt"]))
        rank = d.get("scalp_rank", [])
        print("рейтинг: %s монет" % len(rank))
        ev = d.get("events", [])[:3]
        for e in ev:
            print("  событие:", e.get("msg", "")[:70])

asyncio.run(t())
