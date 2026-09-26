import asyncio
import ccxt.pro as ccxtpro

async def test_exchanges():
    exchs = ["binanceusdm", "bybit", "okx", "gate", "bitget", "mexc", "kucoinfutures", "htx", "bingx", "hyperliquid"]
    for e in exchs:
        try:
            cls = getattr(ccxtpro, e)
            client = cls({"enableRateLimit": True, "timeout": 10000})
            print(f"Exchange {e}: successfully initialized, has watchTickers={client.has.get('watchTickers')}, watchOrderBook={client.has.get('watchOrderBook')}")
            await client.close()
        except Exception as err:
            print(f"Exchange {e} ERROR: {err}")

asyncio.run(test_exchanges())
