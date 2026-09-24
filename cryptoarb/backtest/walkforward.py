"""Walk-forward 1d train -> 1d trade x30 for top40."""
import json, time
from pathlib import Path
import polars as pl
from .zscore import backtest_pair, backtest_directional, compute_z
from .metrics import summarize

RAW="data/raw_1m_30d"

def load_series(ex, sym):
    p=Path(f"{RAW}/{ex}/{sym.replace('/','_').replace(':','_')}/candles.parquet")
    if not p.exists():
        return [],[],[]
    try:
        df=pl.read_parquet(p).sort("ts")
        return df["ts"].to_list(), df["c"].to_list(), df["v"].to_list()
    except Exception:
        return [],[],[]

def slice_day(ts, pas, pbs, vola, volb, day_idx):
    day_ms=86400*1000
    start=ts[0] - (ts[0] % day_ms) + day_idx*day_ms
    end=start+day_ms
    idx=[i for i,t in enumerate(ts) if start <= t < end]
    if not idx: return [],[],[],[],[]
    return [ts[i] for i in idx],[pas[i] for i in idx],[pbs[i] for i in idx],[vola[i] for i in idx],[volb[i] for i in idx]

def slice_days(ts, pas, pbs, vola, volb, start_day, n_days):
    out_ts=[]; out_pa=[]; out_pb=[]; out_va=[]; out_vb=[]
    for d in range(start_day, start_day+n_days):
        s_ts,s_pa,s_pb,s_va,s_vb=slice_day(ts,pas,pbs,vola,volb,d)
        out_ts.extend(s_ts); out_pa.extend(s_pa); out_pb.extend(s_pb); out_va.extend(s_va); out_vb.extend(s_vb)
    return out_ts,out_pa,out_pb,out_va,out_vb

def run_walkforward(top40_file="output/top40_4ex.txt", days=30, train_days=1):
    symbols=[l.strip() for l in open(top40_file,encoding="utf-8") if l.strip()]
    exs=["okx","bitget","mexc","bingx"]
    data={}
    for sym in symbols:
        for ex in exs:
            ts,c,v=load_series(ex,sym)
            data[(sym,ex)]=(ts,c,v)
    all_trades=[]
    Path("output").mkdir(exist_ok=True)
    _out = Path("output/walkforward_3d.jsonl")
    done_days=set()
    if _out.exists():
        try:
            for _l in open(_out,encoding="utf-8"):
                try: done_days.add(json.loads(_l).get("day"))
                except Exception: pass
        except Exception: pass
        if done_days:
            print(f"resume: skip days {sorted(done_days)}", flush=True)
    else:
        open(_out,"w").close()  # чистый файл на прогон
    for d in range(days-train_days):
        if (d+train_days) in done_days:
            print(f"Day {d} skip (cached)", flush=True)
            continue
        try:
            cand=[]
            for sym in symbols:
                present=[ex for ex in exs if (sym,ex) in data and len(data[(sym,ex)][0])>0]
                for i in range(len(present)):
                    for j in range(i+1,len(present)):
                        ea=present[i]; eb=present[j]
                        ta,ca,va=data[(sym,ea)]
                        tb,cb,vb=data[(sym,eb)]
                        mb=dict(zip(tb,cb)); mvb=dict(zip(tb,vb))
                        mva=dict(zip(ta,va))
                        ts=[];pa=[];pb=[]; vola=[];volb=[]
                        for t,c in zip(ta,ca):
                            if t in mb:
                                ts.append(t);pa.append(c);pb.append(mb[t]); vola.append(mva.get(t,0)); volb.append(mvb.get(t,0))
                        if not ts: continue
                        s_ts,s_pa,s_pb,s_vola,s_volb=slice_days(ts,pa,pb,vola,volb,d,train_days)
                        if len(s_ts)<200: continue
                        # z один раз на окно, 6 порогов оцениваем на нём (было 6 пересчётов EMA/std)
                        _ratio=[a/b if b else float("nan") for a,b in zip(s_pa,s_pb)]
                        _,_,_zw=compute_z(_ratio)
                        best=None; best_sharpe=-1
                        for ez in [2.2,2.4,2.5,2.7,3.0,3.3]:
                            tr=backtest_pair(s_ts,s_pa,s_pb,ez,0.24,0.04,0.3,15,z=_zw)
                            met=summarize(tr)
                            if met["sharpe"]>best_sharpe:
                                best_sharpe=met["sharpe"]; best=(ez,met,tr)
                        if best and best[1]["n"]>0:
                            cand.append((best[1]["sharpe"], sym, ea, eb, best[0], best[1]))
            cand=sorted(cand, key=lambda x: x[0], reverse=True)[:20]
            import datetime as _dt
            print(f"Day {d} top20: {[c[1] for c in cand][:5]} ... [{_dt.datetime.now():%H:%M:%S}]", flush=True)
            for _,sym,ea,eb,ez,_ in cand:
                ta,ca,va=data[(sym,ea)]; tb,cb,vb=data[(sym,eb)]
                mb=dict(zip(tb,cb)); mvb=dict(zip(tb,vb)); mva=dict(zip(ta,va))
                ts=[];pa=[];pb=[]; vola=[];volb=[]
                for t,c in zip(ta,ca):
                    if t in mb:
                        ts.append(t);pa.append(c);pb.append(mb[t]); vola.append(mva.get(t,0)); volb.append(mvb.get(t,0))
                s_ts,s_pa,s_pb,s_vola,s_volb=slice_day(ts,pa,pb,vola,volb,d+train_days)
                if len(s_ts)<50: continue
                # z один раз на торговый день
                _r2=[a/b if b else float("nan") for a,b in zip(s_pa,s_pb)]
                _,_,_z2=compute_z(_r2)
                tr_n=backtest_pair(s_ts,s_pa,s_pb,ez,0.24,0.04,0.3,15,z=_z2)
                tr_d=backtest_directional(s_ts,s_pa,s_pb,ez,0.12,0.02,0.3,15,z=_z2)
                for tr,mode in [(tr_n,"neutral"),(tr_d,"dir")]:
                    for t in tr:
                        t["mode"]=mode; t["symbol"]=sym; t["a"]=ea; t["b"]=eb; t["day"]=d+train_days; t["fee"]=0.24 if mode=="neutral" else 0.12; t["slip"]=0.04 if mode=="neutral" else 0.02; t["width"]=0.04; t["funding"]=0.0
                        all_trades.append(t)
            # дозапись дня сразу — падение не теряет готовые дни
            with open("output/walkforward_3d.jsonl","a",encoding="utf-8") as _f:
                for t in all_trades:
                    if t["day"]==d+train_days:
                        _f.write(json.dumps(t,ensure_ascii=False)+"\n")
        except Exception as e:
            print(f"Day {d} failed: {e}", flush=True)
            import traceback; traceback.print_exc()
            continue
    print(f"walkforward done {len(all_trades)} trades -> output/walkforward_3d.jsonl")
    return all_trades

if __name__=="__main__":
    import sys
    td=1
    if len(sys.argv)>1:
        try: td=int(sys.argv[1])
        except: pass
    run_walkforward(train_days=td)
