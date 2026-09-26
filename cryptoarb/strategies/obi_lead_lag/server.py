"""
Lightweight FastAPI server for Render.com Free Tier deployment.
Binds to $PORT (0.0.0.0), serves real-time dark dashboard, and passes /health checks.
"""

import os
import time
from typing import Optional
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from .engine import OBILeadLagEngine
from .stream import VenueWebSocket

app = FastAPI(title="OBI Lead-Lag Arbitrage (Render Free)", version="1.0.0")

# Global instances initialized on startup
engine: Optional[OBILeadLagEngine] = None
lead_ws: Optional[VenueWebSocket] = None
lag_ws: Optional[VenueWebSocket] = None
start_time = time.time()


@app.on_event("startup")
async def startup_event():
    global engine, lead_ws, lag_ws, start_time
    start_time = time.time()
    
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT"]
    engine = OBILeadLagEngine(
        lead_exchange="binance",
        lag_exchange="asterdex",
        symbols=symbols,
        obi_threshold=0.60,
        min_lead_lag_bps=0.5,
    )

    # 1. Binance Lead Stream
    lead_ws = VenueWebSocket(
        name="binance",
        base_ws_url="wss://fstream.binance.com",
        symbols=symbols,
        callback=engine.on_depth_update,
    )
    await lead_ws.start()

    # 2. Aster DEX Lag Stream
    lag_ws = VenueWebSocket(
        name="asterdex",
        base_ws_url="wss://fstream.asterdex.com",
        symbols=symbols,
        callback=engine.on_depth_update,
    )
    await lag_ws.start()


@app.on_event("shutdown")
async def shutdown_event():
    global lead_ws, lag_ws
    if lead_ws:
        await lead_ws.stop()
    if lag_ws:
        await lag_ws.stop()


@app.get("/health")
async def health():
    """Render.com Health Check and Keep-Alive endpoint."""
    uptime = time.time() - start_time
    lead_ok = lead_ws.connected if lead_ws else False
    lag_ok = lag_ws.connected if lag_ws else False
    return JSONResponse({
        "status": "healthy" if (lead_ok and lag_ok) else "reconnecting",
        "uptime_sec": round(uptime, 1),
        "lead_ws_connected": lead_ok,
        "lag_ws_connected": lag_ok,
    })


@app.get("/api/snapshot")
async def api_snapshot():
    """JSON state snapshot for UI and external monitoring."""
    if not engine:
        return JSONResponse({"error": "Engine not initialized"}, status_code=503)
    return JSONResponse(engine.get_snapshot())


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Dark-mode, ultra-lightweight live dashboard."""
    html_content = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>OBI Lead-Lag Arbitrage (Binance -> Aster DEX)</title>
  <style>
    :root {
      --bg: #0b0e14;
      --card-bg: #141923;
      --border: #232a38;
      --text: #e6edf3;
      --muted: #8b949e;
      --green: #238636;
      --green-light: #2ea043;
      --red: #da3633;
      --accent: #58a6ff;
      --yellow: #d29922;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    body { background: var(--bg); color: var(--text); padding: 20px; }
    .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: 16px; margin-bottom: 20px; }
    .badge { background: var(--border); padding: 4px 10px; border-radius: 12px; font-size: 12px; font-weight: bold; }
    .badge.green { background: rgba(46, 160, 67, 0.2); color: var(--green-light); border: 1px solid var(--green); }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }
    .card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
    .card .title { font-size: 12px; text-transform: uppercase; color: var(--muted); margin-bottom: 6px; }
    .card .val { font-size: 24px; font-weight: bold; }
    .section-title { font-size: 16px; font-weight: 600; margin-bottom: 12px; display: flex; align-items: center; gap: 8px; }
    table { width: 100%; border-collapse: collapse; background: var(--card-bg); border-radius: 8px; overflow: hidden; margin-bottom: 24px; border: 1px solid var(--border); }
    th, td { padding: 10px 14px; text-align: left; font-size: 13px; border-bottom: 1px solid var(--border); }
    th { background: #1c2230; color: var(--muted); font-weight: 600; }
    .tag-buy { color: var(--green-light); font-weight: bold; }
    .tag-sell { color: var(--red); font-weight: bold; }
  </style>
</head>
<body>
  <div class="header">
    <div>
      <h1 style="font-size: 20px; font-weight: bold;">OBI Lead-Lag Arbitrage Engine</h1>
      <p style="font-size: 13px; color: var(--muted); margin-top: 4px;">
        Ведущая биржа: <strong style="color:var(--accent);">Binance (Lead)</strong> &rarr; Ведомая площадка: <strong style="color:var(--yellow);">Aster DEX (Lag)</strong>
      </p>
    </div>
    <div style="display: flex; gap: 10px; align-items: center;">
      <span id="statusBadge" class="badge green">● LIVE WS</span>
      <span class="badge" style="color: var(--muted);" id="renderTag">Render Free: &lt;50MB RAM</span>
    </div>
  </div>

  <div class="grid">
    <div class="card">
      <div class="title">Всего сигналов OBI</div>
      <div class="val" id="statSignals">0</div>
    </div>
    <div class="card">
      <div class="title">Исполнено Maker (Fill Rate)</div>
      <div class="val" id="statFills">0%</div>
    </div>
    <div class="card">
      <div class="title">Win Rate (Закрытых)</div>
      <div class="val" id="statWinRate" style="color: var(--green-light);">0.0%</div>
    </div>
    <div class="card">
      <div class="title">Чистый Net PnL</div>
      <div class="val" id="statPnL" style="color: var(--text);">$0.00</div>
    </div>
  </div>

  <div class="section-title">⚡ Текущее состояние стаканов (Top-5 OBI)</div>
  <table>
    <thead><tr><th>Биржа</th><th>Символ</th><th>Mid Price</th><th>Спред (bps)</th><th>Top-5 OBI</th><th>Возраст (мс)</th></tr></thead>
    <tbody id="booksTable"><tr><td colspan="6" style="text-align:center; color:var(--muted);">Ожидание первых котировок...</td></tr></tbody>
  </table>

  <div class="section-title">🎯 Последние сигналы OBI и расхождение цен</div>
  <table>
    <thead><tr><th>Время</th><th>Символ</th><th>Направление</th><th>Binance OBI</th><th>Лаг (bps)</th><th>Maker Limit (Aster)</th></tr></thead>
    <tbody id="signalsTable"><tr><td colspan="6" style="text-align:center; color:var(--muted);">Сигналов пока нет</td></tr></tbody>
  </table>

  <div class="section-title">📊 История сделок (Эмуляция Maker на Aster DEX)</div>
  <table>
    <thead><tr><th>Время</th><th>Символ</th><th>Направление</th><th>Статус</th><th>Gross bps</th><th>Net PnL ($)</th><th>Причина выхода</th></tr></thead>
    <tbody id="tradesTable"><tr><td colspan="7" style="text-align:center; color:var(--muted);">Сделок пока нет</td></tr></tbody>
  </table>

  <script>
    async function updateDashboard() {
      try {
        const res = await fetch('/api/snapshot');
        if (!res.ok) return;
        const d = await res.json();
        
        // Stats
        document.getElementById('statSignals').innerText = d.stats.total_signals;
        document.getElementById('statFills').innerText = `${d.stats.total_filled} (${d.stats.fill_rate_pct}%)`;
        document.getElementById('statWinRate').innerText = `${d.stats.win_rate_pct}% (${d.stats.winning_trades}W / ${d.stats.losing_trades}L)`;
        
        const pnlEl = document.getElementById('statPnL');
        const pnl = d.stats.net_pnl_usd;
        pnlEl.innerText = `${pnl >= 0 ? '+' : ''}$${pnl.toFixed(3)}`;
        pnlEl.style.color = pnl > 0 ? 'var(--green-light)' : (pnl < 0 ? 'var(--red)' : 'var(--text)');

        // Books Table
        const bBody = document.getElementById('booksTable');
        if (d.books && d.books.length > 0) {
          bBody.innerHTML = d.books.map(b => {
            const obiColor = b.obi >= 0.6 ? 'var(--green-light)' : (b.obi <= -0.6 ? 'var(--red)' : 'var(--text)');
            return `<tr>
              <td><strong>${b.exchange.toUpperCase()}</strong></td>
              <td>${b.symbol}</td>
              <td>$${b.mid.toFixed(2)}</td>
              <td>${b.spread_bps.toFixed(1)}</td>
              <td style="color:${obiColor}; font-weight:bold;">${b.obi >= 0 ? '+' : ''}${b.obi.toFixed(3)}</td>
              <td style="color:var(--muted);">${b.age_ms} ms</td>
            </tr>`;
          }).join('');
        }

        // Signals Table
        const sBody = document.getElementById('signalsTable');
        if (d.latest_signals && d.latest_signals.length > 0) {
          sBody.innerHTML = d.latest_signals.map(s => `<tr>
            <td style="color:var(--muted);">${s.time}</td>
            <td><strong>${s.symbol}</strong></td>
            <td class="${s.side === 'BUY' ? 'tag-buy' : 'tag-sell'}">${s.side}</td>
            <td style="font-weight:bold;">${s.lead_obi >= 0 ? '+' : ''}${s.lead_obi.toFixed(2)}</td>
            <td style="color:var(--yellow);">${s.lag_bps >= 0 ? '+' : ''}${s.lag_bps.toFixed(1)} bps</td>
            <td>$${s.limit_price.toFixed(2)}</td>
          </tr>`).join('');
        }

        // Trades Table
        const tBody = document.getElementById('tradesTable');
        if (d.latest_trades && d.latest_trades.length > 0) {
          tBody.innerHTML = d.latest_trades.map(t => {
            const pnlColor = t.net_usd > 0 ? 'var(--green-light)' : (t.net_usd < 0 ? 'var(--red)' : 'var(--muted)');
            return `<tr>
              <td style="color:var(--muted);">${t.time}</td>
              <td><strong>${t.symbol}</strong></td>
              <td class="${t.side === 'BUY' ? 'tag-buy' : 'tag-sell'}">${t.side}</td>
              <td><span class="badge" style="background:#21262d;">${t.status}</span></td>
              <td>${t.pnl_bps >= 0 ? '+' : ''}${t.pnl_bps.toFixed(1)} bps</td>
              <td style="color:${pnlColor}; font-weight:bold;">${t.net_usd >= 0 ? '+' : ''}$${t.net_usd.toFixed(3)}</td>
              <td style="color:var(--muted);">${t.reason || '-'}</td>
            </tr>`;
          }).join('');
        }
      } catch (e) {
        console.error("Dashboard poll error:", e);
      }
    }

    setInterval(updateDashboard, 1000);
    updateDashboard();
  </script>
</body>
</html>"""
    return HTMLResponse(content=html_content)
