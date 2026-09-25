/**
 * CryptoArbitrage - Вспомогательные утилиты форматирования UI
 */

function fmtTimer(sec) {
  if (sec == null || sec < 0) return '—';
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  const pad = n => String(n).padStart(2, '0');
  return `${pad(h)}:${pad(m)}:${pad(s)}`;
}

function fmtDur(s) {
  if (s == null) return '—';
  s = Math.floor(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (h) return `${h}ч ${m}м`;
  if (m) return `${m}м ${sec}с`;
  return `${sec}с`;
}

function fmtDateTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const pad = n => String(n).padStart(2, '0');
  const Y = d.getFullYear();
  const M = pad(d.getMonth() + 1);
  const D = pad(d.getDate());
  const h = pad(d.getHours());
  const m = pad(d.getMinutes());
  const s = pad(d.getSeconds());
  return `${Y}-${M}-${D} ${h}:${m}:${s}`;
}

function cls(v) {
  return v > 0 ? 'pos' : v < 0 ? 'neg' : 'muted';
}

function esc(s) {
  if (s == null) return '';
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function card(label, value, signed, onClick) {
  let c = '';
  if (signed === 'purple') c = 'purple';
  else if (signed === 'yellow') c = 'yellow';
  else if (signed !== undefined && typeof signed === 'number') c = signed > 0 ? 'pos' : signed < 0 ? 'neg' : '';
  else if (signed === 'pos') c = 'pos';
  const clickAttr = onClick ? ` onclick="${onClick}" style="cursor:pointer;" title="Перейти"` : '';
  return `<div class="card"${clickAttr}><div class="label">${label}</div><div class="value ${c}">${value}</div></div>`;
}
