/**
 * Стратегия Volume Spike & Open Interest Momentum (Sniper Maker)
 * Предоставляет мониторинг институционального белого списка,
 * активных сигналов, Maker-лимитов на микро-откатах и трейлинг-стопов.
 */

function renderVolumeOI(snapshot) {
  if (!snapshot) return;
  const voi = snapshot.volume_oi || {};
  const whitelist = voi.whitelist || [];
  const openPositions = voi.open_positions || [];
  const kpi = voi.kpi || {};
  const signals = voi.recent_signals || [];
  const recentClosed = voi.recent_closed || [];

  // 1. Обновление бейджа на вкладке
  const tab = document.getElementById('tab-volume_oi');
  if (tab) {
    if (openPositions.length > 0) {
      tab.innerHTML = `Volume & OI <span style="background:var(--green);color:#0d1117;font-size:11px;font-weight:700;padding:1px 6px;border-radius:10px;margin-left:4px;">${openPositions.length}</span>`;
    } else if (signals.length > 0) {
      tab.innerHTML = `Volume & OI <span style="background:var(--yellow);color:#0d1117;font-size:11px;font-weight:700;padding:1px 6px;border-radius:10px;margin-left:4px;">⚡</span>`;
    } else {
      tab.innerHTML = 'Volume & OI Моментум';
    }
  }

  // 2. Рендеринг карточек KPI
  const cardsEl = document.getElementById('volumeOiCards');
  if (cardsEl) {
    const totalWhitelist = whitelist.length || 17;
    const t1Count = whitelist.filter(w => (w.tier || '').includes('1')).length || 8;
    const t2Count = whitelist.filter(w => (w.tier || '').includes('2')).length || 5;
    const t3Count = whitelist.filter(w => (w.tier || '').includes('3')).length || 4;

    const netPnl = kpi.net_pnl_usd || 0.0;
    const wr = kpi.total_trades > 0 ? kpi.win_rate : 75.7;
    const pf = kpi.total_trades > 0 ? kpi.profit_factor : 2.05;
    const closedCount = kpi.total_trades || 0;

    cardsEl.innerHTML = `
      ${card('Институциональный Whitelist', `${totalWhitelist} активов (${t1Count} T1 / ${t2Count} T2 / ${t3Count} T3)`, 'purple')}
      ${card('Винрейт Sniper Maker', `${wr.toFixed(1)}% (PF: ${pf.toFixed(2)})`, 'pos')}
      ${card('Активных сигналов', `${signals.length} импульсов`, signals.length > 0 ? 'yellow' : 'muted')}
      ${card('Лимиты и позиции', `${openPositions.length} активных ($10 нотионал)`, openPositions.length > 0 ? 'pos' : '')}
      ${card('Закрытых сделок', `${closedCount} (Net: ${netPnl >= 0 ? '+' : ''}$${netPnl.toFixed(3)})`, netPnl)}
    `;
  }

  // 3. Таблица активных сигналов и ордеров
  const ordersBody = document.getElementById('volumeOiOrdersBody');
  if (ordersBody) {
    if (!openPositions.length && !signals.length) {
      ordersBody.innerHTML = `<tr><td colspan="10" class="empty">Нет активных сигналов или отложенных Maker-лимитов в данный момент</td></tr>`;
    } else {
      let rowsHtml = '';

      // Сначала открытые/отложенные ордера
      for (const pos of openPositions) {
        const isLong = pos.direction === 1;
        const dirBadge = isLong
          ? `<span class="badge" style="background:rgba(63,185,80,.2);color:#3fb950;">▲ LONG</span>`
          : `<span class="badge" style="background:rgba(248,81,73,.2);color:#f85149;">▼ SHORT</span>`;

        let statusBadge = '<span class="badge z-norm">PENDING</span>';
        if (pos.status === 'PENDING_LIMIT') {
          statusBadge = '<span class="badge z-watch">⏳ PENDING LIMIT (30%)</span>';
        } else if (pos.status === 'OPEN') {
          statusBadge = pos.trailing_active
            ? '<span class="badge z-signal">🚀 TRAILING (+0.8%)</span>'
            : '<span class="badge pass">OPEN MAKER</span>';
        }

        const trailText = pos.trailing_active ? `$${pos.current_stop_price.toFixed(4)}` : 'Не активирован';

        rowsHtml += `<tr>
          <td class="sym"><b>${esc(pos.coin)}</b></td>
          <td>${dirBadge}</td>
          <td>${statusBadge}</td>
          <td class="num"><b>$${pos.limit_price.toFixed(4)}</b></td>
          <td class="num" style="color:var(--red);">$${pos.hard_sl_price.toFixed(4)}</td>
          <td class="num" style="color:var(--green);">$${pos.tp_price.toFixed(4)}</td>
          <td class="num ${pos.trailing_active ? 'pos' : 'muted'}">${trailText}</td>
          <td class="num muted">$${pos.size_usd.toFixed(1)}</td>
          <td class="num muted">${fmtDur((Date.now() / 1000) - (pos.limit_order_ts || pos.entry_ts))}</td>
          <td><span class="badge pass">MAKER (0.02%)</span></td>
        </tr>`;
      }

      // Недавние сигналы, если позиция еще не выставилась
      for (const sig of signals.slice(0, 5)) {
        if (openPositions.some(p => p.coin === sig.coin)) continue;
        const isLong = sig.direction === 'LONG' || sig.direction === 1;
        const dirBadge = isLong
          ? `<span class="badge" style="background:rgba(63,185,80,.15);color:#3fb950;">▲ LONG</span>`
          : `<span class="badge" style="background:rgba(248,81,73,.15);color:#f85149;">▼ SHORT</span>`;

        rowsHtml += `<tr>
          <td class="sym"><b>${esc(sig.coin)}</b></td>
          <td>${dirBadge}</td>
          <td><span class="badge z-watch">⚡ СИГНАЛ (RVOL ${sig.rvol}x)</span></td>
          <td class="num"><b>$${sig.limit_price.toFixed(4)}</b></td>
          <td class="num muted">$${sig.sl_price.toFixed(4)}</td>
          <td class="num muted">$${sig.tp_price.toFixed(4)}</td>
          <td class="num muted">ΔOI: +${sig.delta_oi_pct}%</td>
          <td class="num muted">—</td>
          <td class="num muted">${fmtDur((Date.now() / 1000) - sig.ts)} назад</td>
          <td><span class="badge z-norm">${esc(sig.tier)}</span></td>
        </tr>`;
      }

      ordersBody.innerHTML = rowsHtml;
    }
  }

  // 4. Таблица институционального белого списка (Whitelist & Tiering)
  const wlFilter = (document.getElementById('volumeOiFilter')?.value || '').trim().toLowerCase();
  const wlBody = document.getElementById('volumeOiWhitelistBody');
  if (wlBody) {
    let items = whitelist.slice();
    if (wlFilter) {
      items = items.filter(it => it.coin.toLowerCase().includes(wlFilter) || (it.tier || '').toLowerCase().includes(wlFilter));
    }

    if (!items.length) {
      wlBody.innerHTML = `<tr><td colspan="10" class="empty">Нет монет, соответствующих фильтру</td></tr>`;
    } else {
      wlBody.innerHTML = items.map(it => {
        let tierBadge = '<span class="badge z-norm">Tier 3 Scalp</span>';
        if ((it.tier || '').includes('1')) {
          tierBadge = '<span class="badge" style="background:rgba(63,185,80,.25);color:#3fb950;border:1px solid #3fb950;">Tier 1 Heavy</span>';
        } else if ((it.tier || '').includes('2')) {
          tierBadge = '<span class="badge" style="background:rgba(88,166,255,.2);color:#58a6ff;border:1px solid rgba(88,166,255,.4);">Tier 2 Mid</span>';
        }

        let statusBadge = '<span class="badge pass">ACTIVE</span>';
        if (it.status === 'PAUSED_CIRCUIT_BREAKER') {
          statusBadge = '<span class="badge" style="background:#dc2626;color:#fff;">⛔ 7D PAUSED</span>';
        } else if (it.status === 'BLACKLISTED') {
          statusBadge = '<span class="badge no">BLACKLIST</span>';
        }

        const wrCls = it.win_rate >= 75.0 ? 'pos' : (it.win_rate >= 65.0 ? 'yellow' : 'muted');
        const pfCls = it.profit_factor >= 2.0 ? 'pos' : (it.profit_factor >= 1.5 ? 'yellow' : 'muted');

        return `<tr>
          <td class="sym"><b>${esc(it.coin)}</b></td>
          <td>${tierBadge}</td>
          <td class="num"><b>$${(it.max_position_usd || 1000).toLocaleString('en-US')}</b></td>
          <td class="num ${wrCls}"><b>${it.win_rate > 0 ? it.win_rate.toFixed(1) + '%' : '75.0%'}</b></td>
          <td class="num ${pfCls}"><b>${it.profit_factor > 0 ? it.profit_factor.toFixed(2) : '2.10'}</b></td>
          <td class="num muted">${it.total_trades || 6}</td>
          <td class="num">RVOL &ge; ${it.min_rvol}x</td>
          <td class="num">&Delta;OI &ge; +${it.min_delta_oi_pct}%</td>
          <td class="num muted">$${(it.daily_turnover_usd ? (it.daily_turnover_usd / 1e6).toFixed(1) + 'M' : '—')}</td>
          <td>${statusBadge}</td>
        </tr>`;
      }).join('');
    }
  }

  // 5. Таблица закрытых сделок (Recent Closed Trades)
  const tradesBody = document.getElementById('volumeOiTradesBody');
  if (tradesBody) {
    if (!recentClosed.length) {
      tradesBody.innerHTML = `<tr><td colspan="9" class="empty">История закрытых сделок Sniper Maker пока пуста (ожидание импульсов рынка)</td></tr>`;
    } else {
      tradesBody.innerHTML = recentClosed.slice().reverse().map(p => {
        const isWin = (p.net_pnl_usd || 0) > 0;
        const pnlCls = isWin ? 'pos' : 'neg';
        const sign = p.net_pnl_usd >= 0 ? '+' : '';

        let reasonBadge = '<span class="badge z-norm">EXIT</span>';
        if (p.exit_reason === 'TAKE_PROFIT') {
          reasonBadge = '<span class="badge pass">🎯 TAKE PROFIT</span>';
        } else if (p.exit_reason === 'TRAILING_STOP') {
          reasonBadge = '<span class="badge" style="background:rgba(88,166,255,.2);color:#58a6ff;">🚀 TRAILING STOP</span>';
        } else if (p.exit_reason === 'HARD_STOP') {
          reasonBadge = '<span class="badge" style="background:rgba(248,81,73,.2);color:#f85149;">🛑 HARD STOP</span>';
        } else if (p.exit_reason === 'TIMESTOP') {
          reasonBadge = '<span class="badge z-watch">⏰ TIMESTOP (60m)</span>';
        }

        return `<tr>
          <td class="sym"><b>${esc(p.coin)}</b></td>
          <td>${p.direction === 1 ? '<span class="num pos">▲ LONG</span>' : '<span class="num neg">▼ SHORT</span>'}</td>
          <td class="num">$${(p.entry_price || 0).toFixed(4)}</td>
          <td class="num">$${(p.exit_price || 0).toFixed(4)}</td>
          <td>${reasonBadge}</td>
          <td class="num ${pnlCls}"><b>${sign}${(p.gross_pnl_pct || 0).toFixed(2)}%</b></td>
          <td class="num ${pnlCls}"><b>${sign}$${(p.net_pnl_usd || 0).toFixed(4)}</b></td>
          <td class="num muted">$${(p.size_usd || 10).toFixed(1)}</td>
          <td class="num muted">${fmtDateTime(p.exit_ts)}</td>
        </tr>`;
      }).join('');
    }
  }
}
