"""??????? ?? ???????: winrate, avg, PF, maxDD, Sharpe, gross vs net."""
from __future__ import annotations

import math


def summarize(trades: list[dict]) -> dict:
    n = len(trades)
    if not n:
        return {"n": 0, "winrate": 0.0, "avg_net": 0.0, "pf": 0.0,
                "sharpe": 0.0, "maxdd": 0.0, "sum_net": 0.0, "sum_gross": 0.0}
    nets = [t["net_pct"] for t in trades]
    gross = [t["gross_pct"] for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [-x for x in nets if x <= 0]
    gp = sum(wins)
    gl = sum(losses) or 1e-9
    m = sum(nets) / n
    if n < 3:
        sharpe = 0.0
    else:
        sd = (sum((x - m) ** 2 for x in nets) / n) ** 0.5
        sharpe = m / sd * math.sqrt(n) if sd > 1e-9 else 0.0
    eq, peak, dd = 0.0, 0.0, 0.0
    for x in nets:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    # ??? ???-3: ????????????, ?? ?????
    top3 = sum(sorted(nets, reverse=True)[:3])
    return {"n": n, "winrate": round(len(wins) / n * 100, 1),
            "avg_net": round(m, 4), "pf": round(gp / gl, 3),
            "sharpe": round(sharpe, 3), "maxdd": round(dd, 3),
            "sum_net": round(sum(nets), 3), "sum_gross": round(sum(gross), 3),
            "sum_no_top3": round(sum(nets) - top3, 3)}

