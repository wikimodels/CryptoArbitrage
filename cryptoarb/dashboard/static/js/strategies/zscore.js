/**
 * Стратегия Z-Score (Межбиржевой статистический арбитраж)
 */

let coinSortKey = 'symbol';
let coinSortAsc = true;

function renderZScore(snapshot) {
  if (!snapshot) return;
  const s = snapshot;
  const zCfg = s.zscore || {};

  // 1. Z-Score Radar Table
  const zFilter = (document.getElementById('zFilter')?.value || '').trim().toLowerCase();
  const onlySignals = document.getElementById('onlyZSignals')?.checked;
  const onlyWatch = document.getElementById('onlyZWatch')?.checked;

  let zRows = zCfg.radar || [];
  if (zFilter) zRows = zRows.filter(r => (r.symbol + r.ex_a + r.ex_b).toLowerCase().includes(zFilter));
  if (onlySignals) zRows = zRows.filter(r => r.abs_z >= (zCfg.entry_z || 3.5));
  else if (onlyWatch) zRows = zRows.filter(r => r.abs_z >= 2.5);

  const zb = document.getElementById('zBody');
  if (zb) {
    if (!zRows.length) {
      zb.innerHTML = `<tr><td colspan="10" class="empty">Нет пар, соответствующих фильтру</td></tr>`;
    } else {
      zb.innerHTML = zRows.map(r => {
        let badge = '<span class="badge z-norm">NORM</span>';
        if (r.abs_z >= (zCfg.entry_z || 3.5)) {
          badge = `<span class="badge z-signal">🔥 SIGNAL (${r.z > 0 ? 'SHORT A / LONG B' : 'LONG A / SHORT B'})</span>`;
        } else if (r.abs_z >= 2.5) {
          badge = '<span class="badge z-watch">⚡ WATCH</span>';
        }
        const zCls = r.abs_z >= (zCfg.entry_z || 3.5) ? 'pos' : (r.abs_z >= 2.5 ? 'purple' : 'muted');
        return `
          <tr>
            <td class="sym">${esc(r.symbol)}</td>
            <td class="ex">${esc(r.ex_a)}</td>
            <td class="ex">${esc(r.ex_b)}</td>
            <td class="num ${zCls}"><b>${r.z >= 0 ? '+' : ''}${r.z.toFixed(2)}</b></td>
            <td class="num">${r.ratio.toFixed(5)}</td>
            <td class="num muted">${r.ma.toFixed(5)}</td>
            <td class="num muted">${r.sd.toFixed(5)}</td>
            <td class="num ${cls(r.spread_pct)}">${r.spread_pct >= 0 ? '+' : ''}${r.spread_pct.toFixed(3)}%</td>
            <td class="num neg">-${r.fees_pct.toFixed(3)}%</td>
            <td>${badge}</td>
          </tr>
        `;
      }).join('');
    }
  }

  // 2. Positions Table (Pure Arbitrage Only)
  const arbPositions = (s.positions || []).filter(p => !p.strategy || (p.strategy !== 'funding' && p.strategy !== 'cross_coin'));
  const nArbPos = arbPositions.length;

  const posTab = document.getElementById('tab-positions');
  if (posTab) {
    posTab.innerHTML = nArbPos > 0
      ? `Арбитраж: Позиции <span style="background:var(--green);color:#0d1117;font-size:11px;font-weight:700;padding:1px 6px;border-radius:10px;margin-left:4px;">${nArbPos}</span>`
      : 'Арбитраж: Позиции';
  }
  const posSumEl = document.getElementById('positionsSummary');
  if (posSumEl) {
    posSumEl.textContent = nArbPos > 0
      ? `Открыто активных позиций арбитража: ${nArbPos} (динамический мониторинг стакана и схождения Z)`
      : `В рынке сейчас нет активных арбитражных позиций. Сделки открываются при |Z| ≥ 4.0 и закрываются при сведении к средней (~65 сек). Сбор фандинга вынесен в отдельную вкладку.`;
  }

  const pb = document.getElementById('posBody');
  if (pb) {
    if (!arbPositions.length) {
      pb.innerHTML = `<tr><td colspan="11" class="empty">Сейчас активных позиций арбитража нет. Сделки открываются при отклонении |Z| &ge; 4.0 и закрываются при возврате к 0.0.</td></tr>`;
    } else {
      pb.innerHTML = arbPositions.map(p => {
        const u = p.unrealized_pnl_usdt;
        const uCls = u == null ? 'muted' : (u > 0 ? 'pos' : u < 0 ? 'neg' : 'muted');
        const uTxt = u == null ? '—' : (u > 0 ? '+' : '') + u.toFixed(2);
        const cur = p.cur_spread_pct == null ? '—' : p.cur_spread_pct.toFixed(3);
        const curZ = p.cur_z == null ? '—' : (p.cur_z >= 0 ? '+' : '') + p.cur_z.toFixed(2);
        const curZCls = p.cur_z == null ? 'muted' : (Math.abs(p.cur_z) <= 0.3 ? 'pos' : 'purple');
        const zinTxt = (p.z_in != null && p.z_in !== 0) ? ((p.z_in > 0 ? '+' : '') + Number(p.z_in).toFixed(2)) : '—';
        const sz = (p.size_usdt != null && p.size_usdt > 0) ? ('$' + Number(p.size_usdt).toFixed(2)) : '—';
        return `
          <tr>
            <td class="sym">${esc(p.symbol)}</td>
            <td class="ex">${esc(p.exch_long)}</td>
            <td class="ex">${esc(p.exch_short)}</td>
            <td class="num purple"><b>${zinTxt}</b></td>
            <td class="num ${curZCls}"><b>${curZ}</b></td>
            <td class="num">${(p.entry_raw_spread_pct||0).toFixed(3)}%</td>
            <td class="num ${cls(cur === '—' ? 0 : p.cur_spread_pct)}">${cur === '—' ? '—' : cur + '%'}</td>
            <td class="num">${sz}</td>
            <td class="num ${uCls}"><b>${uTxt}</b></td>
            <td class="num">${fmtDur(p.holding_seconds)}</td>
            <td class="muted">${(p.trade_id || '').slice(0,8)}</td>
          </tr>`;
      }).join('');
    }
  }

  // 3. Closed Trades Table (Pure Arbitrage Only)
  const trBody = document.getElementById('tradesBody');
  if (trBody) {
    const tFilter = (document.getElementById('tradeFilter')?.value || '').trim().toLowerCase();
    const onlyZ4 = document.getElementById('onlyZ4Trades')?.checked;
    let closedArbTrades = (s.closed_trades || []).filter(t => !t.strategy || (t.strategy !== 'funding' && t.strategy !== 'cross_coin'));
    if (tFilter) {
      closedArbTrades = closedArbTrades.filter(t => (t.symbol + (t.exch_long||'') + (t.exch_short||'') + (t.reason||'')).toLowerCase().includes(tFilter));
    }
    if (onlyZ4) {
      closedArbTrades = closedArbTrades.filter(t => t.is_z4 || Math.abs(t.z_in || 0) >= 4.0);
    }
    const sumEl = document.getElementById('tradeSummary');
    if (sumEl) {
      sumEl.textContent = `Закрытых сделок арбитража: ${closedArbTrades.length}`;
    }
    if (!closedArbTrades.length) {
      trBody.innerHTML = `<tr><td colspan="12" class="empty">Нет закрытых сделок арбитража, соответствующих фильтру</td></tr>`;
    } else {
      trBody.innerHTML = closedArbTrades.map(t => {
        const pnl = t.realized_pnl_usdt ?? 0;
        const pnlCls = pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : '';
        const pnlSign = pnl > 0 ? '+' : '';
        const closeTime = fmtDateTime(t.close_ts);
        const zinTxt = (t.z_in != null && t.z_in !== 0) ? ((t.z_in > 0 ? '+' : '') + Number(t.z_in).toFixed(2)) : '—';
        const isZ4 = t.is_z4 || Math.abs(t.z_in || 0) >= 4.0;
        const sigmaBadge = isZ4
          ? '<span class="badge z-tier4" title="Вход при |Z| >= 4.0">Z &ge; 4.0</span>'
          : '<span class="badge z-tier35" title="Вход при 3.5 <= |Z| < 4.0">Z: 3.5–4.0</span>';
        const dur = fmtDur(t.holding_seconds || 0);
        const exL = t.exch_long || t.exchange || '—';
        const exS = t.exch_short || t.side || '—';
        const sz = (t.size_usdt != null && t.size_usdt > 0) ? ('$' + Number(t.size_usdt).toFixed(2)) : '—';
        const fees = t.fees_usdt != null ? `-$${Math.abs(t.fees_usdt).toFixed(2)}` : '—';
        const pnlPct = t.pnl_pct != null ? `${pnlSign}${Number(t.pnl_pct).toFixed(2)}%` : '—';
        return `<tr>
          <td class="muted">${closeTime}</td>
          <td class="sym">${esc(t.symbol)}</td>
          <td class="ex">${esc(exL)}</td>
          <td class="ex">${esc(exS)}</td>
          <td class="num"><b>${zinTxt}</b></td>
          <td>${sigmaBadge}</td>
          <td class="num">${sz}</td>
          <td class="num">${dur}</td>
          <td class="num ${pnlCls}"><b>${pnlSign}${pnl.toFixed(2)}</b></td>
          <td class="num ${pnlCls}">${pnlPct}</td>
          <td class="num neg">${fees}</td>
          <td><span class="muted">${esc(t.reason || 'close')}</span></td>
        </tr>`;
      }).join('');
    }
  }

  // 4. Render Coins Table
  renderCoins(snapshot);
}

function sortCoins(key) {
  if (coinSortKey === key) {
    coinSortAsc = !coinSortAsc;
  } else {
    coinSortKey = key;
    coinSortAsc = true;
  }
  if (window.currentSnapshot) {
    renderCoins(window.currentSnapshot);
  }
}

function renderCoins(snapshot) {
  if (!snapshot) return;
  const coins = snapshot.arbitrage_coins || [];
  const q = (document.getElementById('coinFilter')?.value || '').trim().toLowerCase();
  const onlySig = document.getElementById('onlyCoinSignals')?.checked;

  let filtered = coins.slice();
  if (q) {
    filtered = filtered.filter(c => 
      c.symbol.toLowerCase().includes(q) ||
      c.base.toLowerCase().includes(q) ||
      (c.exchanges || []).some(e => e.toLowerCase().includes(q))
    );
  }
  if (onlySig) {
    filtered = filtered.filter(c => c.status === 'SIGNAL');
  }

  // Update sort indicators
  ['symbol', 'base', 'exchanges', 'pairs_count', 'max_abs_z', 'max_spread_pct', 'status'].forEach(k => {
    const el = document.getElementById('s-' + k);
    if (el) el.textContent = (coinSortKey === k) ? (coinSortAsc ? '▲' : '▼') : '⇅';
  });

  filtered.sort((a, b) => {
    let va = a[coinSortKey];
    let vb = b[coinSortKey];
    if (coinSortKey === 'exchanges') {
      va = (a.exchanges || []).join(',');
      vb = (b.exchanges || []).join(',');
    }
    if (typeof va === 'string') {
      return coinSortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
    }
    return coinSortAsc ? (va - vb) : (vb - va);
  });

  const sumEl = document.getElementById('coinSummary');
  if (sumEl) {
    sumEl.textContent = `Отображается: ${filtered.length} из ${coins.length} монет`;
  }

  const tb = document.getElementById('coinsBody');
  if (!tb) return;
  if (!filtered.length) {
    tb.innerHTML = `<tr><td colspan="7" class="empty">Монеты не найдены</td></tr>`;
    return;
  }

  tb.innerHTML = filtered.map(c => {
    const exBadges = (c.exchanges || []).map(e => `<span class="badge" style="background:#21262d;color:var(--blue);margin-right:4px;font-size:11px;">${esc(e.toUpperCase())}</span>`).join('');
    let statusBadge = '<span class="badge z-norm">ACTIVE</span>';
    if (c.status === 'SIGNAL') {
      statusBadge = '<span class="badge z-signal">🔥 SIGNAL</span>';
    } else if (c.status === 'WATCH') {
      statusBadge = '<span class="badge z-watch">⚡ WATCH</span>';
    }
    const zCls = c.max_abs_z >= 3.5 ? 'pos' : (c.max_abs_z >= 2.5 ? 'purple' : 'muted');
    return `<tr>
      <td class="sym">${esc(c.symbol)}</td>
      <td><b>${esc(c.base)}</b></td>
      <td>${exBadges}</td>
      <td class="num">${c.pairs_count}</td>
      <td class="num ${zCls}"><b>${c.max_abs_z > 0 ? c.max_abs_z.toFixed(2) : '—'}</b></td>
      <td class="num ${cls(c.max_spread_pct)}">${c.max_spread_pct > 0 ? '+' + c.max_spread_pct.toFixed(3) + '%' : '—'}</td>
      <td>${statusBadge}</td>
    </tr>`;
  }).join('');
}
