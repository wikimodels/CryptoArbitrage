import json
rows=json.load(open("output/backtest_screen.json",encoding="utf-8"))
def pr(title, kin, kout):
    print(title)
    top=sorted(rows, key=lambda r: r.get(kin,{}).get("sharpe",-1), reverse=True)[:20]
    for i,r in enumerate(top,1):
        a=r.get(kin,{}); b=r.get(kout,{})
        sym=r["symbol"]; ea=r["a"]; eb=r["b"]; ez=r["entry_z"]
        print(f"{i:2d} {sym:14s} {ea:7s}/{eb:7s} z={ez}  n={a.get('n'):3d} shr={a.get('sharpe'):5.2f} pf={a.get('pf'):5.2f} avg={a.get('avg_net',0):+6.3f}% | out n={b.get('n'):3d} shr={b.get('sharpe',0):6.2f}")
pr("=== NEUTRAL TOP 20 ===","neutral_in","neutral_out")
print()
pr("=== DIR TOP 20 ===","dir_in","dir_out")
