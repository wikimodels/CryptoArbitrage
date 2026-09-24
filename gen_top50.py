# coding: utf-8
import json
from pathlib import Path
REPORT=Path("output/backtest_screen.json")
if not REPORT.exists():
    REPORT=Path("output/backtest_report.json")
rows=json.load(open(REPORT,encoding="utf-8"))
def section(title, kin, kout):
    rs=sorted(rows, key=lambda r: r.get(kin,{}).get("sharpe",-1), reverse=True)[:50]
    trs=[]
    for r in rs:
        a=r.get(kin,{}); b=r.get(kout,{})
        ok="PASS" if (a.get("sharpe",0)>1 and b.get("sharpe",0)>1 and a.get("pf",0)>1.3) else ""
        c=" style=\"background:#1a3a1a\"" if ok else ""
        trs.append(f"<tr{c}><td>{r['symbol']}</td><td>{r['a']}/{r['b']}</td><td>{r['entry_z']}</td><td>{a.get('n')}</td><td>{a.get('sharpe')}</td><td>{a.get('pf')}</td><td>{a.get('avg_net')}</td><td>{a.get('sum_net')}</td><td>{b.get('n')}</td><td>{b.get('sharpe')}</td><td>{b.get('pf')}</td><td>{ok}</td></tr>")
    return f"<h2>{title} -- top 50</h2><table><tr><th>symbol</th><th>pair</th><th>z</th><th>n_in</th><th>shr_in</th><th>pf</th><th>avg%</th><th>sum%</th><th>n_out</th><th>shr_out</th><th>pf_out</th><th>edge?</th></tr>"+"".join(trs)+"</table>"
html=("<html><head><meta charset=utf-8><title>z-backtest top50</title>"
      "<style>body{background:#0f0f0f;color:#ddd;font-family:Consolas,monospace;padding:18px}h1{color:#7cc4ff}h2{color:#b0e0e6;margin-top:28px}table{border-collapse:collapse;font-size:12px}td,th{border:1px solid #333;padding:4px 7px;white-space:nowrap}th{color:#7cc4ff;background:#1a1a1a}</style></head><body>"
      "<h1>z-score backtest -- screening 3d (591 sym, 3 ex) -- top 50</h1>"
      "<p>File: output/backtest_screen.json | EMA50 | in 75% vs out 25% | fee 0.24%+slip | green = PASS</p>"
      +section("market-neutral (long+short)","neutral_in","neutral_out")
      +section("directional-A (1 leg)","dir_in","dir_out")
      +"</body></html>")
Path("output/backtest_top50.html").write_text(html,encoding="utf-8")
print("written")
