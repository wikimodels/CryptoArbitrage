"""Backtest dashboard: GET /backtest (HTML) + GET /backtest/report (JSON)."""
from __future__ import annotations

import json
from pathlib import Path

REPORT = Path("output/backtest_report.json")


def load_report() -> list[dict]:
    if not REPORT.exists():
        return []
    try:
        return json.loads(REPORT.read_text(encoding="utf-8"))
    except Exception:
        return []


def mount(app) -> None:
    @app.get("/backtest/report")
    async def report():
        return {"rows": load_report()}

    @app.get("/backtest", response_class=None)
    async def page():
        from fastapi.responses import HTMLResponse

        def section(title: str, key_in: str, key_out: str) -> str:
            rows = sorted(load_report(),
                          key=lambda r: r.get(key_in, {}).get("sharpe", 0),
                          reverse=True)[:50]
            trs = []
            for r in rows:
                i, o = r.get(key_in, {}), r.get(key_out, {})
                ok = ("PASS" if (i.get("sharpe", 0) > 1.0 and o.get("sharpe", 0) > 1.0
                                 and i.get("pf", 0) > 1.3) else "")
                trs.append(
                    f"<tr><td>{r['symbol']}</td><td>{r['a']}/{r['b']}</td>"
                    f"<td>{r['entry_z']}/{r.get('exit_z','-')}/{r.get('tstop','-')}</td>"
                    f"<td>{i.get('n')}</td>"
                    f"<td>{i.get('sharpe')}</td><td>{i.get('pf')}</td>"
                    f"<td>{i.get('avg_net')}</td>"
                    f"<td>{o.get('n')}</td><td>{o.get('sharpe')}</td>"
                    f"<td>{ok}</td></tr>")
            return (f"<h2>{title}</h2>"
                    "<table><tr><th>symbol</th><th>pair</th><th>z/exit/t</th><th>n_in</th>"
                    "<th>shr_in</th><th>pf_in</th><th>avg_in%</th>"
                    "<th>n_out</th><th>shr_out</th><th>edge?</th></tr>"
                    + "".join(trs) + "</table>")

        html = ("<html><head><meta charset=utf-8><title>z-backtest</title>"
                "<style>body{background:#111;color:#ddd;font-family:monospace;padding:20px}"
                "table{border-collapse:collapse;margin-bottom:30px}"
                "td,th{border:1px solid #333;padding:4px 8px}"
                "th{color:#7cc4ff}</style></head><body>"
                "<h1>z-score backtest (EMA50, in 75% vs out 25%)</h1>"
                + section("market-neutral (long+short)", "neutral_in", "neutral_out")
                + section("directional-A (одна нога)", "dir_in", "dir_out")
                + "</body></html>")
        return HTMLResponse(html)
