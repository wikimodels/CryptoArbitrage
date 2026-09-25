/**
 * Стратегия межмонетного арбитража (Cross-Coin Pairs Trading)
 */

function renderCrossCoin(snapshot) {
  if (!snapshot) return;
  const ccData = snapshot.cross_coin || {};
  const radar = ccData.radar || [];
  const entryZ = ccData.entry_z || 2.5;

  const positions = (snapshot.positions || []).filter(p => p.strategy === 'cross_coin');

  // 1. Update tab badge
  const ccTab = document.getElementById('tab-crosscoin');
  if (ccTab) {
    ccTab.innerHTML = positions.length > 0
      ? `Межмонетный <span style="background:var(--purple);color:#0d1117;font-size:11px;font-weight:700;padding:1px 6px;border-radius:10px;margin-left:4px;">${positions.length}</span>`
      : 'Межмонетный';
  }

  // 2. Render KPI cards
  const ccCards = document.getElementById('crossCoinCards');
  if (ccCards) {
    const topPair = radar.length > 0 ? radar[0] : null;
    const topTxt = topPair ? `${topPair.coin_a}/${topPair.coin_b} (Z: ${topPair.z_score >= 0 ? '+' : ''}${topPair.z_score.toFixed(2)})` : '—';
    const topCls = topPair && topPair.abs_z >= entryZ ? 'pos' : (topPair && topPair.abs_z >= 1.8 ? 'yellow' : '');
    const sigCount = radar.filter(r => r.is_signal).length;
    const lastRecal = ccData.last_recalibration && ccData.last_recalibration !== 'N/A'
      ? ccData.last_recalibration.slice(11, 19) + ' UTC'
      : 'Калибровано';

    ccCards.innerHTML = `
      ${card('WFA Ротация пар', `${radar.length} проверенных пар`, 'purple')}
      ${card('Рекалибровка WFA', lastRecal, 'cyan')}
      ${card('Порог входа Z', `|Z| ≥ ${entryZ.toFixed(1)} / Выход Z ≈ 0.0`, '')}
      ${card('Готовых сигналов', `${sigCount} связок`, sigCount > 0 ? 'pos' : 'muted')}
      ${card('Открытых позиций', `${positions.length} поз. (нотионал $10)`, positions.length > 0 ? 'yellow' : '')}
    `;
  }

  // 3. Render Pairs Radar Table
  const filterQ = (document.getElementById('crossCoinFilter')?.value || '').trim().toLowerCase();
  const onlySignals = document.getElementById('onlyCrossSignals')?.checked;

  let filtered = radar.slice();
  if (filterQ) {
    filtered = filtered.filter(r => 
      r.coin_a.toLowerCase().includes(filterQ) ||
      r.coin_b.toLowerCase().includes(filterQ) ||
      r.label.toLowerCase().includes(filterQ)
    );
  }
  if (onlySignals) {
    filtered = filtered.filter(r => r.is_signal);
  }

  const tb = document.getElementById('crossCoinBody');
  if (tb) {
    if (!filtered.length) {
      tb.innerHTML = `<tr><td colspan="10" class="empty">Нет межмонетных пар, соответствующих фильтрам</td></tr>`;
    } else {
      tb.innerHTML = filtered.map(r => {
        let badge = '<span class="badge z-norm">NORM</span>';
        if (r.is_draining) {
          badge = `<span class="badge" style="background:#dc2626;color:#fff;">DRAIN</span>`;
        } else if (r.abs_z >= entryZ) {
          badge = `<span class="badge z-signal">🔥 SIGNAL</span>`;
        } else if (r.abs_z >= 1.8) {
          badge = `<span class="badge z-watch">⚡ WATCH</span>`;
        }
        const zCls = r.abs_z >= entryZ ? 'pos' : (r.abs_z >= 1.8 ? 'purple' : 'muted');

        return `<tr>
          <td class="sym"><b>${esc(r.coin_a)} / ${esc(r.coin_b)}</b></td>
          <td class="muted" style="font-size:12px;">${esc(r.label)}</td>
          <td class="num">$${r.price_a.toFixed(4)}</td>
          <td class="num">$${r.price_b.toFixed(4)}</td>
          <td class="num"><b>${r.ratio.toFixed(4)}</b></td>
          <td class="num muted">${r.mean_ratio > 0 ? r.mean_ratio.toFixed(4) : '—'}</td>
          <td class="num ${zCls}"><b>${r.z_score >= 0 ? '+' : ''}${r.z_score.toFixed(2)}</b></td>
          <td class="num muted">${r.history_points} мин</td>
          <td class="ex"><b>${esc(r.action)}</b></td>
          <td>${badge}</td>
        </tr>`;
      }).join('');
    }
  }
}
