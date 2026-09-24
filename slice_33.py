import sys
sys.stdout.reconfigure(encoding='utf-8')
import polars as pl
from pathlib import Path
from cryptoarb.backtest.zscore import backtest_pair
from cryptoarb.backtest.metrics import summarize
def load(ex,sym, days=5):
    p=Path(f"data/raw_1m_30d/{ex}/{sym.replace('/','_').replace(':','_')}/candles.parquet")
    df=pl.read_parquet(p).sort("ts")
    # take last 5 days
    day_ms=86400*1000
    # last 5 days
    end=df["ts"].max()
    start=end - days*day_ms
    df=df.filter((pl.col("ts")>=start) & (pl.col("ts")<end))
    return df["ts"].to_list(), df["c"].to_list()
symbols=["DRIFT/USDT:USDT","PONS/USDT:USDT","AVNT/USDT:USDT","GRIFFAIN/USDT:USDT","CNPY/USDT:USDT"]
exs=["okx","bitget","mexc","bingx"]
for sym in symbols:
    print(f"\n{sym}")
    for ea in exs:
        for eb in exs:
            if ea>=eb: continue
            try:
                ta,ca=load(ea,sym); tb,cb=load(eb,sym)
                if len(ta)<200 or len(tb)<200: continue
                mb=dict(zip(tb,cb))
                ts=[];pa=[];pb=[]
                for t,c in zip(ta,ca):
                    if t in mb:
                        ts.append(t);pa.append(c);pb.append(mb[t])
                if len(ts)<200: continue
                for ez in [3.0,3.3]:
                    tr=backtest_pair(ts,pa,pb,ez,0.24,0.04,0.3,15)
                    met=summarize(tr)
                    print(f"  {ea}/{eb} z{ez} n={met['n']} win{met['winrate']}% avg{met['avg_net']:+.3f}% pf{met['pf']:.2f} shr{met['sharpe']:.2f}")
            except Exception as e:
                print("fail",e)
