/**
 * Стратегия сбора фандинга (Funding Rate Arbitrage)
 */

function renderFunding(snapshot) {
  if (!snapshot) return;
  const fData = snapshot.funding_arb || {};
  const kpi = fData.kpi || {};
  const opps = fData.opportunities || [];
  const positions = (snapshot.positions || []).filter(p => p.strategy === 'funding');

  // 1. Update tab badge
  const fTab = document.getElementById('tab-funding');
  if (fTab) {
    fTab.innerHTML = positions.length > 0
      ? `Сбор фандинга <span style="background:var(--yellow);color:#0d1117;font-size:11px;font-weight:700;padding:1px 6px;border-radius:10px;margin-left:4px;">${positions.length}</span>`
      : 'Сбор фандинга';
  }

  // 2. Render KPI cards inside funding tab
  const fc = document.getElementById('fundingCards');
  if (fc) {
    const bestTxt = kpi.best_opportunity ? `${kpi.best_opportunity.symbol.split('/')[0]} (${kpi.best_opportunity.diff_8h_pct >= 0 ? '+' : ''}${kpi.best_opportunity.diff_8h_pct.toFixed(2)}%)` : '—';
    const totEarned = kpi.total_funding_earned_usdt || 0;
    const dayDrip = kpi.daily_dripping_income_usdt || 0;
    const p8hDrip = kpi.payment_8h_dripping_income_usdt || 0;
    const openPos = kpi.open_positions_count || 0;
    const openMargin = kpi.open_margin_usdt || 0;
    const openNotional = kpi.open_notional_usdt || 0;
    const closedTr = kpi.closed_trades_count || 0;
    const winRate = kpi.win_rate_pct || 0;
    const closedPnl = kpi.closed_pnl_usdt || 0;

    fc.innerHTML = `
      ${card('Накапало фандинга Σ', (totEarned >= 0 ? '+' : '') + '$' + totEarned.toFixed(4), totEarned > 0 ? 'pos' : '')}
      ${card('Капает в сутки (24ч)', (dayDrip >= 0 ? '+' : '') + '$' + dayDrip.toFixed(4), dayDrip > 0 ? 'pos' : '')}
      ${card('Капает за выплату (8ч)', (p8hDrip >= 0 ? '+' : '') + '$' + p8hDrip.toFixed(4), p8hDrip > 0 ? 'pos' : '')}
      ${card('Позиции фандинга', `${openPos} шт. (залог $${openMargin.toFixed(0)} / объем $${openNotional.toFixed(0)})`, openPos > 0 ? 'yellow' : '')}
      ${card('Сделок закрыто (WR)', `${closedTr} сд. (${winRate}% WR | ${closedPnl >= 0 ? '+' : ''}$${closedPnl.toFixed(2)})`, closedPnl >= 0 ? 'pos' : 'neg')}
      ${card('Лучшая дельта рынка', bestTxt, 'purple')}
    `;
  }

  // 3. Render Active Funding Positions
  const posCountEl = document.getElementById('fundingPosCount');
  if (posCountEl) {
    posCountEl.textContent = `Активно: ${positions.length} поз. (суммарный залог: $${(positions.length * 1.0).toFixed(0)} USDT | объем $${(positions.length * 10.0).toFixed(0)} USDT)`;
  }
  const fPosBody = document.getElementById('fundingPosBody');
  if (fPosBody) {
    if (!positions.length) {
      fPosBody.innerHTML = `<tr><td colspan="13" class="empty">Сейчас нет открытых позиций по сбору фандинга. Система автоматически открывает сделки при разнице ставок &ge; 0.08% и расхождении цен &le; 0.20%.</td></tr>`;
    } else {
      fPosBody.innerHTML = positions.map(p => {
        const diffRate = ((p.funding_rate_short || 0) - (p.funding_rate_long || 0)) * 100.0;
        const apr = diffRate * 3 * 365.0;
        const curSp = p.cur_spread_pct == null ? '—' : (p.cur_spread_pct >= 0 ? '+' : '') + p.cur_spread_pct.toFixed(3) + '%';
        const u = p.unrealized_pnl_usdt;
        const uTxt = u == null ? '—' : (u >= 0 ? '+' : '') + '$' + u.toFixed(2);
        const uCls = u == null ? 'muted' : (u > 0 ? 'pos' : u < 0 ? 'neg' : 'muted');
        const accrued = p.funding_accrued_usdt || 0;
        const nextTimer = p.next_payment_in_sec != null ? fmtTimer(p.next_payment_in_sec) : '—';

        return `<tr>
          <td class="sym">${esc(p.symbol)}</td>
          <td class="ex">${esc(p.exch_short)} <span class="muted" style="font-size:11px;">(${((p.funding_rate_short||0)*100).toFixed(3)}%)</span></td>
          <td class="ex">${esc(p.exch_long)} <span class="muted" style="font-size:11px;">(${((p.funding_rate_long||0)*100).toFixed(3)}%)</span></td>
          <td class="num pos"><b>+${diffRate.toFixed(3)}%</b></td>
          <td class="num purple"><b>+${apr.toFixed(1)}%</b></td>
          <td class="num">$${(p.size_usdt||10).toFixed(0)} <span class="muted" style="font-size:11px;">(залог $1)</span></td>
          <td class="num"><b>${p.funding_payments_count || 0}</b></td>
          <td class="num yellow"><span style="font-family:monospace; font-size:13px; font-weight:600;">⏱ ${nextTimer}</span></td>
          <td class="num pos"><b>+$${accrued.toFixed(4)}</b></td>
          <td class="num ${cls(p.cur_spread_pct)}">${curSp}</td>
          <td class="num ${uCls}"><b>${uTxt}</b></td>
          <td class="num">${fmtDur(p.holding_seconds)}</td>
          <td class="muted">${(p.trade_id || '').slice(0, 8)}</td>
        </tr>`;
      }).join('');
    }
  }

  // 4. Render Closed Funding Trades
  const closedFunding = (snapshot.closed_trades || []).filter(t => t.strategy === 'funding');
  const fClosedCountEl = document.getElementById('fundingClosedCount');
  if (fClosedCountEl) {
    fClosedCountEl.textContent = `Закрыто: ${closedFunding.length} связок`;
  }
  const fTradesBody = document.getElementById('fundingTradesBody');
  if (fTradesBody) {
    if (!closedFunding.length) {
      fTradesBody.innerHTML = `<tr><td colspan="11" class="empty">Пока нет закрытых связок по фандингу. Связки удерживаются до 72 часов (9 выплат) либо закрываются при инверсии ставок.</td></tr>`;
    } else {
      fTradesBody.innerHTML = closedFunding.map(t => {
        const pnl = t.realized_pnl_usdt ?? 0;
        const pnlCls = pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : '';
        const pnlSign = pnl > 0 ? '+' : '';
        const closeTime = fmtDateTime(t.close_ts);
        const dur = fmtDur(t.holding_seconds || 0);
        const exS = t.exch_short || '—';
        const exL = t.exch_long || '—';
        const sz = (t.size_usdt != null && t.size_usdt > 0) ? ('$' + Number(t.size_usdt).toFixed(2)) : '$10.00';
        const fundEarned = t.funding_usdt != null ? (t.funding_usdt >= 0 ? '+' : '') + '$' + t.funding_usdt.toFixed(4) : '$0.0000';
        const fees = t.fees_usdt != null ? `-$${Math.abs(t.fees_usdt).toFixed(2)}` : '—';
        return `<tr>
          <td class="muted">${closeTime}</td>
          <td class="sym">${esc(t.symbol)}</td>
          <td class="ex">${esc(exS)}</td>
          <td class="ex">${esc(exL)}</td>
          <td class="num">${sz}</td>
          <td class="num">${dur}</td>
          <td class="num"><b>${t.funding_payments_count || 0}</b></td>
          <td class="num pos"><b>${fundEarned}</b></td>
          <td class="num neg">${fees}</td>
          <td class="num ${pnlCls}"><b>${pnlSign}${pnl.toFixed(2)}</b></td>
          <td><span class="muted">${esc(t.reason || 'close')}</span></td>
        </tr>`;
      }).join('');
    }
  }

  // 5. Render Funding Opportunities Table
  const filterQ = (document.getElementById('fundingFilter')?.value || '').trim().toLowerCase();
  const minDelta = parseFloat(document.getElementById('fundingMinDelta')?.value || '0.0');
  const safeSpread = document.getElementById('fundingSafeSpread')?.checked;
  const onlySignals = document.getElementById('fundingOnlySignals')?.checked;

  let filteredOpps = opps.slice();
  if (filterQ) {
    filteredOpps = filteredOpps.filter(o => 
      o.symbol.toLowerCase().includes(filterQ) ||
      o.base.toLowerCase().includes(filterQ) ||
      o.exch_short.toLowerCase().includes(filterQ) ||
      o.exch_long.toLowerCase().includes(filterQ)
    );
  }
  if (minDelta > 0) {
    filteredOpps = filteredOpps.filter(o => o.diff_8h_pct >= minDelta);
  }
  if (safeSpread) {
    filteredOpps = filteredOpps.filter(o => Math.abs(o.raw_spread_pct) <= 0.20);
  }
  if (onlySignals) {
    filteredOpps = filteredOpps.filter(o => o.passed);
  }

  const oppsCountEl = document.getElementById('fundingOppsCount');
  if (oppsCountEl) {
    oppsCountEl.textContent = `Отображается: ${filteredOpps.length} из ${opps.length} связок`;
  }

  const fOppsBody = document.getElementById('fundingOppsBody');
  if (fOppsBody) {
    if (!filteredOpps.length) {
      fOppsBody.innerHTML = `<tr><td colspan="9" class="empty">Нет возможностей арбитража фандинга по заданным фильтрам</td></tr>`;
    } else {
      fOppsBody.innerHTML = filteredOpps.map(o => {
        let statusBadge = '<span class="badge no">WATCH</span>';
        if (o.is_open) {
          statusBadge = '<span class="badge funding-open">В ПОЗИЦИИ</span>';
        } else if (o.passed) {
          statusBadge = '<span class="badge funding-sig">⚡ СИГНАЛ</span>';
        }
        const net3dCls = o.net_3d_usdt > 0 ? 'pos' : (o.net_3d_usdt < 0 ? 'neg' : 'muted');

        return `<tr>
          <td class="sym">${esc(o.symbol)}</td>
          <td class="ex">${esc(o.exch_short)} <span class="muted" style="font-size:11px;">(${o.rate_short_8h_pct >= 0 ? '+' : ''}${o.rate_short_8h_pct.toFixed(3)}%)</span></td>
          <td class="ex">${esc(o.exch_long)} <span class="muted" style="font-size:11px;">(${o.rate_long_8h_pct >= 0 ? '+' : ''}${o.rate_long_8h_pct.toFixed(3)}%)</span></td>
          <td class="num pos"><b>+${o.diff_8h_pct.toFixed(3)}%</b></td>
          <td class="num purple"><b>+${o.apr_pct.toFixed(1)}%</b></td>
          <td class="num ${cls(o.raw_spread_pct)}">${o.raw_spread_pct >= 0 ? '+' : ''}${o.raw_spread_pct.toFixed(3)}%</td>
          <td class="num pos">+$${o.inc_8h_usdt.toFixed(4)}</td>
          <td class="num ${net3dCls}"><b>${o.net_3d_usdt >= 0 ? '+' : ''}$${o.net_3d_usdt.toFixed(4)}</b> <span class="muted" style="font-size:11px;">(+${((o.net_3d_usdt/1.0)*100).toFixed(0)}% к марже $1)</span></td>
          <td>${statusBadge}</td>
        </tr>`;
      }).join('');
    }
  }

  // 6. Render Heatmap Matrix
  renderFundingMatrix(snapshot);
}

function renderFundingMatrix(snapshot) {
  if (!snapshot) return;
  const fData = snapshot.funding_arb || {};
  const matrix = fData.matrix || [];
  const q = (document.getElementById('matrixFilter')?.value || '').trim().toLowerCase();

  let filtered = matrix.slice();
  if (q) {
    filtered = filtered.filter(m => m.symbol.toLowerCase().includes(q) || m.base.toLowerCase().includes(q));
  }

  const mCountEl = document.getElementById('matrixCount');
  if (mCountEl) {
    mCountEl.textContent = `Монет в матрице: ${filtered.length} из ${matrix.length}`;
  }

  const mb = document.getElementById('matrixBody');
  if (!mb) return;
  if (!filtered.length) {
    mb.innerHTML = `<tr><td colspan="12" class="empty">Монеты не найдены</td></tr>`;
    return;
  }

  const exchangesList = ['bitget', 'bingx', 'mexc', 'okx', 'bybit', 'gateio', 'binance', 'kucoin', 'htx'];

  mb.innerHTML = filtered.map(m => {
    const exTds = exchangesList.map(ex => {
      const r = m.rates ? m.rates[ex] : undefined;
      if (r === undefined) return '<td class="muted" style="text-align:center;">—</td>';
      let cClass = 'heat-zero';
      if (r >= 0.05) cClass = 'heat-high';
      else if (r > 0.01) cClass = 'heat-pos';
      else if (r < 0) cClass = 'heat-neg';
      return `<td><span class="heat-cell ${cClass}">${r >= 0 ? '+' : ''}${r.toFixed(3)}%</span></td>`;
    }).join('');

    return `<tr>
      <td class="sym">${esc(m.symbol)}</td>
      <td><b>${esc(m.base)}</b></td>
      ${exTds}
      <td class="num pos"><b>+${m.delta.toFixed(3)}%</b></td>
    </tr>`;
  }).join('');
}
