import sys
sys.stdout.reconfigure(encoding='utf-8')
# test with 5 symbols, 5 days
import polars as pl
from pathlib import Path
from cryptoarb.backtest.zscore import backtest_pair, backtest_directional
from cryptoarb.backtest.metrics import summarize

def load(ex,sym):
    p=Path(f"data/raw_1m_30d/{ex}/{sym.replace('/','_').replace(':','_')}/candles.parquet")
    df=pl.read_parquet(p).sort("ts")
    return df["ts"].to_list(), df["c"].to_list()

symbols=["SEI/USDT:USDT","FARTCOIN/USDT:USDT","BCH/USDT:USDT","USELESS/USDT:USDT","DASH/USDT:USDT"]
exs=["okx","bitget","mexc","bingx"]
for sym in symbols:
    for ex in exs:
        ts,c=load(ex,sym)
        print(sym, ex, len(ts))
print("done load")
# quick backtest one pair
import time
start=time.time()
for sym in symbols[:2]:
    for ea in exs:
        for eb in exs:
            if ea>=eb: continue
            try:
                ta,ca=load(ea,sym); tb,cb=load(eb,sym)
                mb=dict(zip(tb,cb))
                ts=[];pa=[];pb=[]
                for t,c in zip(ta,ca):
                    if t in mb:
                        ts.append(t);pa.append(c);pb.append(mb[t])
                # slice 1 day
                day_ms=86400*1000
                # take last day
                ts_day=[t for t in ts if t>=ts[-1]-day_ms]
                print(sym, ea, eb, len(ts_day))
            except Exception as e:
                print("fail",e)
print("elapsed", time.time()-start)
