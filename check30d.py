import time
import ccxt
from cryptoarb.connectors.ccxt_connector import CCXT_ID_MAP
from cryptoarb.backtest.collect import fetch_1m
for ex in ['okx','bitget','mexc','kucoin','bingx','coinex','htx','gateio']:
    try:
        since=int(time.time()*1000)-30*86400*1000
        until=int(time.time()*1000)
        rows=fetch_1m(ex, 'BTC/USDT:USDT', since, until)
        print(ex, len(rows), "candles for 30d")
    except Exception as e:
        print(ex, "fail", e)
