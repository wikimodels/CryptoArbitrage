import polars as pl
from pathlib import Path
def load(ex):
    p=Path(f"data/raw_1m_3d/{ex}/DRIFT_USDT_USDT/candles.parquet")
    df=pl.read_parquet(p).sort("ts")
    return df["ts"].to_list(), df["c"].to_list()
ta,ca=load("bitget")
tb,cb=load("mexc")
mb=dict(zip(tb,cb))
ts=[];pa=[];pb=[]
for t,c in zip(ta,ca):
    if t in mb:
        ts.append(t);pa.append(c);pb.append(mb[t])
def ema(v,p):
    k=2/(p+1)
    o=[None]*len(v)
    if len(v)<p: return o
    sma=sum(v[:p])/p
    o[p-1]=sma
    e=sma
    for i in range(p,len(v)):
        e=v[i]*k+e*(1-k)
        o[i]=e
    return o
def std(v,p):
    o=[None]*len(v)
    for i in range(p-1,len(v)):
        w=v[i-p+1:i+1]
        m=sum(w)/p
        o[i]=(sum((x-m)**2 for x in w)/p)**0.5
    return o
ratio=[a/b for a,b in zip(pa,pb)]
ma=ema(ratio,50)
sd=std(ratio,50)
z=[None]*len(ratio)
for i in range(len(ratio)):
    j=i-1
    if j>=0 and ma[j] is not None and sd[j] and sd[j]>0:
        z[i]=(ratio[i]-ma[j])/sd[j]
ENTRY=2.7
trades=[]
open_pos=None
for i,zi in enumerate(z):
    if zi is None or zi!=zi: continue
    if open_pos is None:
        if abs(zi)>=ENTRY:
            open_pos={'i':i,'z':zi,'pa':pa[i],'pb':pb[i]}
    else:
        hold=i-open_pos['i']
        if abs(zi)<0.3 or abs(zi)>1.5*ENTRY or hold>=15:
            ra=(pa[i]-open_pos['pa'])/open_pos['pa']*100
            rb=(pb[i]-open_pos['pb'])/open_pos['pb']*100
            gross=-ra+rb if open_pos['z']>0 else ra-rb
            net=gross-0.28
            reason='conv' if abs(zi)<0.3 else 'stop' if abs(zi)>1.5*ENTRY else 'time'
            trades.append((i,open_pos['i'],zi,open_pos['z'],gross,net,reason))
            open_pos=None
for t in trades:
    if t[5]>0 and t[6]=='conv':
        print('win conv',t)
        break
else:
    print('no win conv, first 5',trades[:5])
print('total',len(trades))
