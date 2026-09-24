import sys
sys.stdout.reconfigure(encoding='utf-8')
import polars as pl
from pathlib import Path
from cryptoarb.backtest.zscore import compute_z
symbols=[l.strip() for l in open("output/top40_4ex.txt",encoding="utf-8") if l.strip()]
exs=["okx","bitget","mexc","bingx"]
def load(ex,sym):
    p=Path(f"data/raw_1m_30d/{ex}/{sym.replace('/','_').replace(':','_')}/candles.parquet")
    if not p.exists(): return [],[]
    df=pl.read_parquet(p).sort("ts")
    return df["ts"].to_list(), df["c"].to_list()
tot={3.0:0,3.3:0,3.5:0,4.0:0}
per_sym={}
for sym in symbols:
    s={e:load(e,sym) for e in exs}
    s={e:v for e,v in s.items() if len(v[0])>1000}
    exl=sorted(s)
    cs={3.0:0,3.3:0,3.5:0,4.0:0}
    for i in range(len(exl)):
        for j in range(i+1,len(exl)):
            ta,ca=s[exl[i]]; tb,cb=s[exl[j]]
            mb=dict(zip(tb,cb))
            pa=[];pb=[]
            for t,c in zip(ta,ca):
                if t in mb:
                    pa.append(c);pb.append(mb[t])
            if len(pa)<200: continue
            ratio=[a/b for a,b in zip(pa,pb)]
            _,_,z=compute_z(ratio)
            # count entries: |z|>=thr with 15m cooldown approx (skip 15 bars after entry)
            for thr in [3.0,3.3,3.5,4.0]:
                n=0; cool=0
                for zi in z:
                    if cool>0:
                        cool-=1; continue
                    if zi is not None and zi==zi and abs(zi)>=thr:
                        n+=1; cool=15
                cs[thr]+=n; tot[thr]+=n
    per_sym[sym]=cs
print("TOTAL entries 30d:", {k:v for k,v in tot.items()})
print("--- top by z>=3.5 ---")
for sym,cs in sorted(per_sym.items(), key=lambda x: -x[1][3.5])[:15]:
    print(f"{sym:18s} 3.0:{cs[3.0]:4d} 3.3:{cs[3.3]:4d} 3.5:{cs[3.5]:4d} 4.0:{cs[4.0]:4d}")
