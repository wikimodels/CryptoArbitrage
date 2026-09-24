import json, math
trades=[json.loads(l) for l in open("output/walkforward_3d.jsonl",encoding="utf-8")]
print("total", len(trades))
def summ(nets):
    n=len(nets)
    if not n: return (0,0,0,0,0)
    wins=[x for x in nets if x>0]
    pf=sum(wins)/(sum(-x for x in nets if x<=0) or 1e-9)
    m=sum(nets)/n
    sd=(sum((x-m)**2 for x in nets)/n)**0.5 or 1e-9
    return (len(wins)/n*100, m, pf, m/sd*math.sqrt(n) if n>2 else 0, sum(nets))
for mode in ["neutral","dir"]:
    nets=[t["net_pct"] for t in trades if t["mode"]==mode]
    w,a,pf,sh,tot=summ(nets)
    print(mode, f"n={len(nets)} win={w:.1f}% avg={a:.4f}% pf={pf:.2f} shr={sh:.2f} sum={tot:.1f}%")
print("--- only z>=3.3 entries ---")
for mode in ["neutral","dir"]:
    nets=[t["net_pct"] for t in trades if t["mode"]==mode and abs(t["z_in"])>=3.3]
    w,a,pf,sh,tot=summ(nets)
    print(mode, f"n={len(nets)} win={w:.1f}% avg={a:.4f}% pf={pf:.2f} shr={sh:.2f} sum={tot:.1f}%")
