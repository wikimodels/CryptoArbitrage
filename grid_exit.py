import polars as pl
from pathlib import Path

def load(ex,sym):
    p=Path(f"data/raw_1m_3d/{ex}/{sym.replace('/','_').replace(':','_')}/candles.parquet")
    if not p.exists():
        p=Path(f"data/raw_1m/{ex}/{sym.replace('/','_').replace(':','_')}/candles.parquet")
    df=pl.read_parquet(p).sort("ts")
    return df["ts"].to_list(), df["c"].to_list()

def ema(vals, period):
    k=2/(period+1)
    out=[None]*len(vals)
    if len(vals)<period: return out
    sma=sum(vals[:period])/period
    out[period-1]=sma
    e=sma
    for i in range(period,len(vals)):
        e=vals[i]*k+e*(1-k)
        out[i]=e
    return out
def rolling_std(vals, period):
    out=[None]*len(vals)
    for i in range(period-1,len(vals)):
        w=vals[i-period+1:i+1]
        m=sum(w)/period
        var=sum((x-m)**2 for x in w)/period
        out[i]=var**0.5
    return out

def backtest(ts,pa,pb,entry_z,exit_z,timestop, fee=0.24, slip=0.04):
    ratio=[a/b for a,b in zip(pa,pb)]
    ma=ema(ratio,50)
    sd=rolling_std(ratio,50)
    z=[None]*len(ratio)
    for i in range(len(ratio)):
        j=i-1
        if j>=0 and ma[j] is not None and sd[j] and sd[j]>0:
            z[i]=(ratio[i]-ma[j])/sd[j]
    trades=[]
    open_pos=None
    for i,zi in enumerate(z):
        if zi is None or zi!=zi: continue
        if open_pos is None:
            if abs(zi)>=entry_z:
                open_pos={"i":i,"z":zi,"pa":pa[i],"pb":pb[i]}
        else:
            hold=i-open_pos["i"]
            if abs(zi)<exit_z or abs(zi)>1.5*entry_z or hold>=timestop:
                ra=(pa[i]-open_pos["pa"])/open_pos["pa"]*100
                rb=(pb[i]-open_pos["pb"])/open_pos["pb"]*100
                gross=-ra+rb if open_pos["z"]>0 else ra-rb
                net=gross-fee-slip
                trades.append(net)
                open_pos=None
    if not trades: return {"n":0,"winrate":0,"avg":0,"pf":0}
    wins=[x for x in trades if x>0]
    pf=sum(wins)/(sum(-x for x in trades if x<=0) or 1e-9)
    return {"n":len(trades),"winrate":round(len(wins)/len(trades)*100,1),"avg":round(sum(trades)/len(trades),4),"pf":round(pf,2)}

# top symbols from screen
symbols=[("DRIFT/USDT:USDT","bitget","mexc"),("PONS/USDT:USDT","bitget","mexc"),("AVNT/USDT:USDT","mexc","okx"),("GRIFFAIN/USDT:USDT","bitget","mexc"),("CNPY/USDT:USDT","mexc","okx")]
for sym,a,b in symbols:
    ta,ca=load(a,sym)
    tb,cb=load(b,sym)
    mb=dict(zip(tb,cb))
    ts=[];pa=[];pb=[]
    for t,c in zip(ta,ca):
        if t in mb:
            ts.append(t);pa.append(c);pb.append(mb[t])
    print(f"\n{sym} {a}/{b} aligned {len(ts)}")
    for exit_z in [0.0,0.3,0.5]:
        for tstop in [5,15,30,60]:
            for ez in [2.7,3.0]:
                m=backtest(ts,pa,pb,ez,exit_z,tstop)
                print(f"  exit {exit_z} tstop {tstop:2d} z{ez} -> n={m['n']:3d} win {m['winrate']:4.1f}% avg {m['avg']:+6.3f}% pf {m['pf']:5.2f}")
