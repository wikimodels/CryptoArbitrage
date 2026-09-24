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
            open_pos={"i":i,"z":zi,"pa":pa[i],"pb":pb[i]}
    else:
        hold=i-open_pos["i"]
        if abs(zi)<0.3 or abs(zi)>1.5*ENTRY or hold>=15:
            ra=(pa[i]-open_pos["pa"])/open_pos["pa"]*100
            rb=(pb[i]-open_pos["pb"])/open_pos["pb"]*100
            gross=-ra+rb if open_pos["z"]>0 else ra-rb
            net=gross-0.28
            reason="conv" if abs(zi)<0.3 else "stop" if abs(zi)>1.5*ENTRY else "time"
            trades.append((open_pos["i"], i, open_pos["z"], zi, gross, net, reason, hold))
            open_pos=None
# pick first winning conv
win=None
for t in trades:
    if t[5]>0 and t[6]=="conv":
        win=t
        break
if not win:
    win=trades[0]
i_in,i_out,zin,zout,gross,net,reason,hold=win
print(f"win {i_in}->{i_out} z {zin:.2f}->{zout:.2f} gross {gross:.3f} net {net:.3f} {reason}")
lo=max(0,i_in-100); hi=min(len(ts), i_out+100)
r_seg=ratio[lo:hi]
ma_seg=ma[lo:hi]
z_seg=[v if v is not None else 0 for v in z[lo:hi]]
W=900; H=260
r_vals=[v for v in r_seg if v is not None] + [v for v in ma_seg if v is not None]
rmn=min(r_vals); rmx=max(r_vals)
zmn=min(z_seg); zmx=max(z_seg)
zmn=min(zmn,-3); zmx=max(zmx,3)
def x(i): return 40 + (i/(len(r_seg)-1))*(W-60) if len(r_seg)>1 else 40
def y_ratio(v): return H-30 - (v-rmn)/(rmx-rmn)*(H-60) if rmx!=rmn else H//2
def y_z(v): return 100 - (v-zmn)/(zmx-zmn)*160
pts_ratio=" ".join(f"{x(i):.1f},{y_ratio(v):.1f}" for i,v in enumerate(r_seg) if v is not None)
pts_ma=" ".join(f"{x(i):.1f},{y_ratio(v):.1f}" for i,v in enumerate(ma_seg) if v is not None)
pts_z=" ".join(f"{x(i):.1f},{y_z(v):.1f}" for i,v in enumerate(z_seg))
xe_in=x(i_in-lo); xe_out=x(i_out-lo)
html=f"""<html><head><meta charset=utf-8><title>DRIFT trade</title><style>body{{background:#111;color:#ddd;font-family:monospace;padding:16px}}h2{{color:#7cc4ff}}svg{{background:#1a1a1a;border:1px solid #333}}</style></head><body><h2>DRIFT bitget/mexc z=2.7 -- net {net:.3f}% gross {gross:.3f}% ({reason})</h2><p>Entry bar {i_in} z={zin:.2f} ratio={ratio[i_in]:.4f} | Exit {i_out} z={zout:.2f} hold {hold}m | 1m real data, EMA50 shift 1, fee 0.28%</p><svg width="{W}" height="{H}"><polyline fill="none" stroke="#4a90e2" stroke-width="1.2" points="{pts_ratio}"/><polyline fill="none" stroke="#f5a623" stroke-width="1" points="{pts_ma}"/><line x1="{xe_in}" y1="20" x2="{xe_in}" y2="{H-30}" stroke="#ef5350" stroke-dasharray="4,4"/><line x1="{xe_out}" y1="20" x2="{xe_out}" y2="{H-30}" stroke="#26a69a" stroke-dasharray="4,4"/></svg><svg width="{W}" height="200"><polyline fill="none" stroke="#b0e0e6" stroke-width="1.2" points="{pts_z}"/><line x1="40" y1="{y_z(2.7):.1f}" x2="{W-20}" y2="{y_z(2.7):.1f}" stroke="#ef5350" opacity="0.6"/><line x1="40" y1="{y_z(-2.7):.1f}" x2="{W-20}" y2="{y_z(-2.7):.1f}" stroke="#ef5350" opacity="0.6"/><line x1="40" y1="{y_z(0.3):.1f}" x2="{W-20}" y2="{y_z(0.3):.1f}" stroke="#26a69a" opacity="0.6"/><line x1="{xe_in}" y1="10" x2="{xe_in}" y2="190" stroke="#ef5350" stroke-dasharray="4,4"/><line x1="{xe_out}" y1="10" x2="{xe_out}" y2="190" stroke="#26a69a" stroke-dasharray="4,4"/></svg><p style="color:#888;font-size:12px">Objective: 1m close real, EMA50 shift 1 no lookahead, 3d window top20 walk-forward daily tradable.</p></body></html>"""
Path("output/drift_trade.html").write_text(html,encoding="utf-8")
print("written output/drift_trade.html")
