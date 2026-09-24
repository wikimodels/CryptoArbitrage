import json
from pathlib import Path
from collections import defaultdict
import math
trades=[json.loads(l) for l in open("output/walkforward_30d.jsonl",encoding="utf-8")]
# group by mode and day
by_mode=defaultdict(list)
by_day=defaultdict(list)
for t in trades:
    by_mode[t["mode"]].append(t)
    by_day[t["day"]].append(t)

def summarize(nets):
    n=len(nets)
    if not n: return {"n":0,"winrate":0,"avg":0,"pf":0,"sharpe":0,"sum":0}
    wins=[x for x in nets if x>0]
    pf=sum(wins)/(sum(-x for x in nets if x<=0) or 1e-9)
    m=sum(nets)/n
    sd=(sum((x-m)**2 for x in nets)/n)**0.5 or 1e-9
    sharpe=m/sd*math.sqrt(n) if n>2 else 0
    return {"n":n,"winrate":round(len(wins)/n*100,1),"avg":round(m,4),"pf":round(pf,2),"sharpe":round(sharpe,2),"sum":round(sum(nets),2)}

html=["<html><head><meta charset=utf-8><title>walkforward 30d</title><style>body{background:#111;color:#ddd;font-family:monospace;padding:16px}h2{color:#7cc4ff}table{border-collapse:collapse;font-size:12px}td,th{border:1px solid #333;padding:4px 7px}th{color:#7cc4ff;background:#1a1a1a}</style></head><body><h1>Walk-forward 30d top40 (1d train -> 1d trade)</h1>"]
for mode in ["neutral","dir"]:
    nets=[t["net_pct"] if "net_pct" in t else t.get("net",0) for t in by_mode[mode]]
    # our walkforward stores net_pct? check
    # In walkforward we stored net? Actually we stored gross/net? In walkforward we used backtest_pair net? Let's use net_pct
    # fallback to realized
    nets=[t.get("net_pct", t.get("net",0)) for t in by_mode[mode]]
    s=summarize(nets)
    html.append(f"<h2>{mode} — {s['n']} trades, win {s['winrate']}%, avg {s['avg']}%, PF {s['pf']}, Sharpe {s['sharpe']}, sum {s['sum']}%</h2>")
    # per day equity
    html.append("<table><tr><th>day</th><th>n</th><th>sum%</th><th>win%</th></tr>")
    cum=0
    for d in sorted(by_day):
        day_trades=[t for t in by_day[d] if t["mode"]==mode]
        nets_d=[t.get("net_pct",0) for t in day_trades]
        s=summarize(nets_d)
        cum+=s["sum"]
        html.append(f"<tr><td>{d}</td><td>{s['n']}</td><td>{s['sum']}</td><td>{s['winrate']}</td></tr>")
    html.append("</table>")
    html.append(f"<p>cum {cum:.2f}% over 29 trading days</p>")

html.append("</body></html>")
Path("output/walkforward_report.html").write_text("".join(html),encoding="utf-8")
print("written output/walkforward_report.html", len(trades))
