/**
 * CryptoArbitrage - Главный контроллер веб-дашборда
 */

let snapshot = null;
window.currentSnapshot = null;
window.render = function() { renderApp(); };


// Навигация по вкладкам
const tabs = document.querySelectorAll('.tab');
tabs.forEach(t => t.onclick = () => {
  tabs.forEach(x => x.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  t.classList.add('active');
  const panelId = 'panel-' + t.dataset.tab;
  const targetPanel = document.getElementById(panelId);
  if (targetPanel) {
    targetPanel.classList.add('active');
  }
  if (t.dataset.tab === 'coins') renderCoins(snapshot);
  if (t.dataset.tab === 'funding') renderFunding(snapshot);
  if (t.dataset.tab === 'crosscoin') renderCrossCoin(snapshot);
});

// Подключение к WebSocket
function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  
  ws.onopen = () => {
    const dot = document.getElementById('liveDot');
    if (dot) dot.classList.add('live');
  };

  ws.onclose = () => {
    const dot = document.getElementById('liveDot');
    if (dot) dot.classList.remove('live');
    setTimeout(connect, 2000);
  };

  ws.onmessage = (e) => {
    try {
      snapshot = JSON.parse(e.data);
      window.currentSnapshot = snapshot;
      renderApp();
    } catch (err) {
      console.error("Ошибка парсинга сокета:", err);
    }
  };
}

// Рендеринг основного дашборда
function renderApp() {
  if (!snapshot) return;
  const s = snapshot;

  // 1. Метаданные шапки
  const mUp = document.getElementById('mUptime');
  if (mUp) mUp.textContent = 'uptime ' + fmtDur(s.uptime_sec);

  const mSc = document.getElementById('mScan');
  if (mSc) mSc.textContent = 'scan ' + (s.last_scan_age_sec == null ? '—' : (s.last_scan_age_sec.toFixed(1) + 'с назад'));

  const mSym = document.getElementById('mSymbols');
  if (mSym) mSym.textContent = 'symbols ' + (s.symbols_total != null ? s.symbols_total : '—');
  
  const zCfg = s.zscore || {};
  const mTh = document.getElementById('mThreshold');
  if (mTh) mTh.textContent = `порог Z: ±${zCfg.entry_z || 3.5}`;

  // 2. Верхние KPI карточки
  const arb = (s.stats && s.stats.arb) || { closed: 0, win_rate_pct: 0, pnl_usdt: 0, fees_usdt: 0, funding_usdt: 0, orphan_aborts: 0 };
  const arb4 = (s.stats && s.stats.arb_z4) || { closed: 0, win_rate_pct: 0, pnl_usdt: 0 };
  const arb35 = (s.stats && s.stats.arb_z35) || { closed: 0, win_rate_pct: 0, pnl_usdt: 0 };
  const maxZTxt = zCfg.max_z_info || '—';
  const fKpi = (s.funding_arb && s.funding_arb.kpi) || {};
  const totFundEarned = fKpi.total_funding_earned_usdt || 0;
  const arbPositions = (s.positions || []).filter(p => !p.strategy || (p.strategy !== 'funding' && p.strategy !== 'cross_coin'));
  const fundingPositions = (s.positions || []).filter(p => p.strategy === 'funding');
  const nArbPos = arbPositions.length;
  const nFundPos = fundingPositions.length;
  
  const cardsEl = document.getElementById('cards');
  if (cardsEl) {
    cardsEl.innerHTML = `
      ${card('Арбитраж (активно)', `${nArbPos} поз.`, nArbPos > 0 ? 1 : 0, "document.querySelector('.tab[data-tab=\\'positions\\']').click()")}
      ${card('Стратегия Z-Score', `Вход ±${zCfg.entry_z || 3.5} / Выход ${zCfg.exit_z || 0.0}`, 'purple')}
      ${card('Max |Z| на рынке', maxZTxt, zCfg.max_abs_z >= (zCfg.entry_z || 3.5) ? 'pos' : (zCfg.max_abs_z >= 2.5 ? 'yellow' : ''))}
      ${card('Арбитраж: сделки', `${arb.closed} (WR ${arb.win_rate_pct}%)`, arb.pnl_usdt, "document.querySelector('.tab[data-tab=\\'trades\\']').click()")}
      ${card('PnL арбитража', (arb.pnl_usdt >= 0 ? '+' : '') + arb.pnl_usdt.toFixed(2), arb.pnl_usdt, "document.querySelector('.tab[data-tab=\\'trades\\']').click()")}
      ${card('Z ≥ 4.0 (гипотеза)', `${arb4.closed} сд. (${arb4.pnl_usdt >= 0 ? '+' : ''}${arb4.pnl_usdt.toFixed(2)}$)`, arb4.pnl_usdt)}
      ${card('Дельта 3.5 ≤ Z < 4.0', `${arb35.closed} сд. (${arb35.pnl_usdt >= 0 ? '+' : ''}${arb35.pnl_usdt.toFixed(2)}$)`, arb35.pnl_usdt)}
      ${card('Сбор фандинга', `${nFundPos} связок (+$${totFundEarned.toFixed(2)})`, nFundPos > 0 ? 'yellow' : '', "document.querySelector('.tab[data-tab=\\'funding\\']').click()")}
      ${card('Комиссии Σ (арб)', '-' + arb.fees_usdt.toFixed(2), -1)}
    `;
  }

  // 3. Вызов модульных обработчиков стратегий
  if (typeof renderZScore === 'function') renderZScore(snapshot);
  if (typeof renderFunding === 'function') renderFunding(snapshot);
  if (typeof renderCrossCoin === 'function') renderCrossCoin(snapshot);

  // 4. Спреды L2
  const f = (document.getElementById('filter')?.value || '').trim().toLowerCase();
  const onlyPass = document.getElementById('onlyPass')?.checked;
  let rows = s.spreads || [];
  if (f) rows = rows.filter(r => (r.symbol + r.exch_long + r.exch_short).toLowerCase().includes(f));
  if (onlyPass) rows = rows.filter(r => r.passed);
  const sBody = document.getElementById('spreadsBody');
  if (sBody) {
    if (!rows.length) {
      sBody.innerHTML = `<tr><td colspan="10" class="empty">Нет данных по спредам</td></tr>`;
    } else {
      sBody.innerHTML = rows.map(r => `
        <tr>
          <td class="sym">${esc(r.symbol)}</td>
          <td class="ex">${esc(r.exch_long)}</td>
          <td class="ex">${esc(r.exch_short)}</td>
          <td class="num ${cls(r.raw_spread_pct)}">${r.raw_spread_pct.toFixed(3)}</td>
          <td class="num ${cls(r.funding_edge_pct)}">${r.funding_edge_pct.toFixed(3)}</td>
          <td class="num neg">-${r.fees_pct.toFixed(3)}</td>
          <td class="num neg">-${r.slippage_pct.toFixed(3)}</td>
          <td class="num neg">-${(r.width_pct ?? 0).toFixed(3)}</td>
          <td class="num ${cls(r.net_edge_pct)}"><b>${r.net_edge_pct.toFixed(3)}</b></td>
          <td>${r.suspect ? '<span class="badge suspect" title="dt='+r.leg_dt_sec+'s, top=$'+r.top_notional_usdt+'">⚠ SUSPECT</span>' : (r.passed ? '<span class="badge pass">PASS</span>' : '<span class="badge no">—</span>')}</td>
        </tr>`).join('');
    }
  }

  // 5. Таблица бирж
  const exBody = document.getElementById('exBody');
  if (exBody) {
    exBody.innerHTML = (s.exchanges || []).map(e => `
      <tr>
        <td class="sym">${esc(e.name)}</td>
        <td>${e.ws_alive ? '<span class="badge pass">LIVE</span>' : '<span class="badge no">OFF</span>'}</td>
        <td class="num">${e.fresh_symbols}</td>
      </tr>`).join('');
  }

  // 6. Список событий
  const evList = document.getElementById('eventsList');
  if (evList) {
    evList.innerHTML = (s.events || []).map(ev => {
      const t = new Date(ev.ts * 1000).toLocaleTimeString('ru-RU');
      return `<li><span class="t">${t}</span><span class="lvl-${ev.level}">${esc(ev.msg)}</span></li>`;
    }).join('') || `<li class="empty">Событий пока нет</li>`;
  }
}

// Модальное окно правил
const modal = document.getElementById('rulesModal');
const infoBtn = document.getElementById('infoBtn');
const rulesClose = document.getElementById('rulesClose');

if (infoBtn && modal) infoBtn.onclick = () => modal.classList.add('open');
if (rulesClose && modal) rulesClose.onclick = () => modal.classList.remove('open');
if (modal) {
  modal.addEventListener('click', e => { if (e.target === modal) modal.classList.remove('open'); });
}
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && modal) modal.classList.remove('open');
});

// Запуск сокета
connect();
