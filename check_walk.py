import json, math
trades=[json.loads(l) for l in open("output/walkforward_30d.jsonl",encoding="utf-8")]
def summ(nets):
    n=len(nets)
    if not n: return (0,0,0,0,0)
    wins=[x for x in nets if x>0]
    pf=sum(wins)/(sum(-x for x in nets if x<=0) or 1e-9)
    m=sum(nets)/n
    sd=(sum((x-m)**2 for x in nets)/n)**0.5 or 1e-9
    sharpe=m/sd*math.sqrt(n) if n>2 else 0
    return (len(wins)/n*100, m, pf, sharpe, sum(nets))
for mode in ["neutral","dir"]:
    nets=[t["net_pct"] for t in trades if t["mode"]==mode]
    win,avg,pf,shr,tot=summ(nets)
    print(mode, f"n={len(nets)} win={win:.1f}% avg={avg:.4f}% pf={pf:.2f} sharpe={shr:.2f} sum={tot:.2f}%")
    # also without top3
    nets_sorted=sorted(nets, reverse=True)
    tot2=sum(nets_sorted[3:]) if len(nets_sorted)>3 else 0
    print("  without top3 sum", round(tot2,2))
