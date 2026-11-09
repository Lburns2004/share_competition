/* Share Showdown front-end – no libraries, no build step. */
const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const gbp = v => v == null ? '—' : '£' + v.toLocaleString('en-GB', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const pct = (v, dp = 2) => v == null ? '—' : (v > 0 ? '+' : '') + v.toFixed(dp) + '%';
const sgn = v => v == null ? 'flat' : v > 0 ? 'up' : v < 0 ? 'down' : 'flat';
const CUR = { CAD: 'CA$', USD: 'US$', EUR: '€', GBP: '£' };
const RANGES = [['Max', null], ['5Y', 60], ['1Y', 12], ['6M', 6], ['3M', 3], ['1M', 1]];
const S = { live: null, board: null, hist: null, range: '1Y', mode: 'pct', hidden: new Set(), hover: null, geo: null };

const bust = u => u + '?_=' + Date.now();
async function getJSON(u) {
  const r = await fetch(bust(u), { cache: 'no-store' });
  if (!r.ok) throw new Error(u + ' ' + r.status);
  return r.json();
}

async function load(first) {
  try {
    const [live, board] = await Promise.all([getJSON('data/live.json'), getJSON('data/leaderboard.json')]);
    S.live = live; S.board = board;
    if (first || !S.hist) S.hist = await getJSON('data/history.json');
    $('#error').hidden = true;
    render();
  } catch (e) {
    console.error(e);
    const el = $('#error');
    el.hidden = false;
    el.textContent = "Couldn't load the latest data – retrying shortly.";
  }
}

function render() {
  const L = S.live;
  document.title = L.title || 'Share Showdown';
  $('#title').textContent = L.title || 'Share Showdown';
  renderUpdated(); renderWinners(); renderCards(); renderBoard(); renderValue(); renderChartControls(); renderLegend(); drawMain();
}

function renderUpdated() {
  const t = new Date(S.live.updated);
  const txt = t.toLocaleString('en-GB', { timeZone: 'Europe/London', weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
  const ageMin = Math.round((Date.now() - t) / 60000);
  const anyOpen = S.live.players.some(p => p.status === 'open');
  const stale = anyOpen && ageMin > 25 ? ` · ⚠ data is ${ageMin} min old` : '';
  $('#updated').textContent = `Last updated ${txt} (UK)${stale} · points awarded at ${S.live.finalise_after} UK`;
}

function names(list) { return list.length ? list.join(' & ') : '—'; }
function ptsLabel(p) { return p >= 2 ? `+${p} pts · Friday double` : `+${p} pt`; }
function miniRank(ranking) {
  return ranking.map(r => `<span>${esc(r.name)} <b class="${sgn(r.pct)}">${pct(r.pct)}</b></span>`).join('');
}
function renderWinners() {
  const T = S.live.today, Y = S.live.yesterday;
  let today;
  if (T.status === 'final' || T.status === 'provisional') {
    const lbl = T.status === 'final' ? "Today's winner" : "Today's leader · live";
    const top = T.ranking[0];
    today = `<div class="wcard"><div class="lbl">${lbl}</div>
      <div class="name">${esc(names(T.winners))}${T.winners.length > 1 ? ' <span class="muted" style="font-size:14px">(tie – both score)</span>' : ''}<span class="pts">${ptsLabel(T.points)}</span></div>
      <div class="${sgn(top && top.pct)}">${top ? pct(top.pct) + ' today' : ''}</div>
      <div class="rank-mini">${miniRank(T.ranking)}</div></div>`;
  } else {
    today = `<div class="wcard"><div class="lbl">Today</div><div class="name muted" style="font-size:20px">${T.status === 'weekend' ? 'Markets closed – back on Monday' : 'Waiting for at least two markets to open'}</div></div>`;
  }
  const yd = Y ? `<div class="wcard"><div class="lbl">Yesterday's winner · ${new Date(Y.date + 'T12:00:00Z').toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' })}</div>
      <div class="name">${esc(names(Y.winners))}<span class="pts">${ptsLabel(Y.points)}</span></div>
      <div class="rank-mini">${miniRank(Y.ranking)}</div></div>`
    : `<div class="wcard"><div class="lbl">Yesterday's winner</div><div class="name muted" style="font-size:20px">No result yet</div></div>`;
  const lead = S.board.standings[0];
  const lb = `<div class="wcard"><div class="lbl">Leading overall</div><div class="name">${esc(lead.name)}<span class="pts">${lead.points} pts</span></div><div class="muted">${esc(lead.stock)} · ${lead.wins} day win${lead.wins === 1 ? '' : 's'}</div></div>`;
  $('#winners').innerHTML = today + yd + lb;
}

function renderCards() {
  $('#cards').innerHTML = S.live.players.map((p, i) => {
    const open = p.status === 'open';
    const badge = open ? 'Market open' : p.status === 'pre' ? 'Opens later' : 'Closed';
    const chg = p.change_pct == null
      ? '<span class="flat">No trading yet today</span>'
      : `<span class="${sgn(p.change_gbp)}">${p.change_gbp > 0 ? '▲' : p.change_gbp < 0 ? '▼' : '■'} ${gbp(Math.abs(p.change_gbp))} (${pct(p.change_pct)}) today</span>`;
    const loc = p.currency === 'GBP' ? '' : `${CUR[p.currency] || p.currency}${p.price_local.toFixed(2)} · £1 = ${(CUR[p.currency] || '')}${p.fx.toFixed(4)}`;
    return `<div class="card" style="--c:${p.color || '#6ea8ff'}">
      <div class="who"><b>${esc(p.name)}</b><span class="stock">${esc(p.stock)}</span><span class="badge ${open ? 'open' : ''}">${badge}</span></div>
      <div class="price">${gbp(p.price_gbp)}</div><div class="chg">${chg}</div>
      <canvas class="spark" data-i="${i}"></canvas>
      <div class="sub">${loc}</div></div>`;
  }).join('');
  document.querySelectorAll('.spark').forEach(c => drawSpark(c, S.live.players[+c.dataset.i]));
}

function drawSpark(cv, p) {
  const pts = p.intraday || [];
  const w = cv.clientWidth, h = cv.clientHeight, dpr = window.devicePixelRatio || 1;
  cv.width = w * dpr; cv.height = h * dpr;
  const g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0);
  if (pts.length < 2) { g.fillStyle = '#8b97a5'; g.font = '12px system-ui'; g.fillText('Intraday chart appears when the market opens', 0, h / 2); return; }
  const base = p.prev_close_gbp;
  const vals = pts.map(x => x[1]).concat(base ? [base] : []);
  let lo = Math.min(...vals), hi = Math.max(...vals); if (hi === lo) { hi += 1; lo -= 1; }
  const pad = (hi - lo) * 0.1; lo -= pad; hi += pad;
  const t0 = pts[0][0], t1 = pts[pts.length - 1][0] || t0 + 1;
  const X = t => 2 + (w - 4) * (t - t0) / Math.max(1, t1 - t0), Y = v => h - 2 - (h - 4) * (v - lo) / (hi - lo);
  const col = p.change_gbp >= 0 ? '#2ecc71' : '#ff5c5c';
  if (base) { g.strokeStyle = '#3a4350'; g.setLineDash([4, 4]); g.beginPath(); g.moveTo(0, Y(base)); g.lineTo(w, Y(base)); g.stroke(); g.setLineDash([]); }
  g.beginPath(); pts.forEach((q, i) => i ? g.lineTo(X(q[0]), Y(q[1])) : g.moveTo(X(q[0]), Y(q[1])));
  g.strokeStyle = col; g.lineWidth = 1.8; g.stroke();
  g.lineTo(X(t1), h); g.lineTo(X(t0), h); g.closePath();
  const gr = g.createLinearGradient(0, 0, 0, h); gr.addColorStop(0, col + '44'); gr.addColorStop(1, col + '00');
  g.fillStyle = gr; g.fill();
}

function renderBoard() {
  const B = S.board, L = S.live;
  const medal = r => ({ 1: '🥇', 2: '🥈', 3: '🥉' }[r] || r);
  $('#standings').innerHTML = `<tr><th>#</th><th>Player</th><th>Stock</th><th class="r">Day wins</th><th class="r">Points</th></tr>` +
    B.standings.map(s => `<tr><td class="medal">${medal(s.rank)}</td><td><span class="dot" style="background:${s.color}"></span><b>${esc(s.name)}</b></td><td>${esc(s.stock)}</td><td class="r">${s.wins}</td><td class="r"><b>${s.points}</b></td></tr>`).join('');
  $('#lb-note').textContent = `since ${new Date(B.points_start + 'T12:00:00Z').toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' })}`;
  const cols = L.players.map(p => p.name);
  const rows = B.days.slice(-15).reverse().map(d => {
    const cells = cols.map(n => {
      const r = d.results[n]; if (!r) return '<td class="r muted">closed</td>';
      const w = d.winners.includes(n);
      return `<td class="r ${w ? 'win' : sgn(r.pct)}">${pct(r.pct)}</td>`;
    }).join('');
    const dt = new Date(d.date + 'T12:00:00Z').toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });
    return `<tr><td>${dt}${d.week_close ? '<span class="x2">×2</span>' : ''}</td>${cells}<td>${esc(names(d.winners))}</td><td class="r">${d.winners.length ? '+' + d.points : '–'}</td></tr>`;
  }).join('');
  $('#days').innerHTML = `<tr><th>Day</th>${cols.map(c => `<th class="r">${esc(c)}</th>`).join('')}<th>Winner</th><th class="r">Pts</th></tr>` + rows;
  const basis = B.scoring_basis === 'gbp' ? 'in £, including currency moves' : 'in local share price';
  $('#rules').textContent = `Each day the biggest % mover (${basis}) from the previous close scores ${B.points.normal} point; ties both score. Friday's close is worth ${B.points.week_close}. Only markets that traded that day are ranked.`;
}

function renderValue() {
  const V = S.live.value_added; if (!V.length) return;
  const max = Math.max(...V.map(v => Math.abs(v.total_pct)), 1);
  const col = Object.fromEntries(S.live.players.map(p => [p.name, p.color]));
  $('#valuetbl').innerHTML = `<tr><th>#</th><th>Player</th><th>Baseline close</th><th class="r">Start £</th><th class="r">Now £</th><th class="r">Share price</th><th class="r">Currency</th><th class="r">Total £ change</th><th></th></tr>` +
    V.map((v, i) => {
      const w = Math.abs(v.total_pct) / max * 50;
      const bar = `<div class="bar"><i style="background:${v.total_pct >= 0 ? '#2ecc71' : '#ff5c5c'};width:${w}%;${v.total_pct >= 0 ? 'left:50%' : 'right:50%'}"></i></div>`;
      return `<tr><td>${i + 1}</td><td><span class="dot" style="background:${col[v.name]}"></span><b>${esc(v.name)}</b> <span class="muted">${esc(v.stock)}</span></td>
      <td>${new Date(v.baseline_date + 'T12:00:00Z').toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: '2-digit' })}</td>
      <td class="r">${gbp(v.start_price_gbp)}</td><td class="r">${gbp(v.now_price_gbp)}</td>
      <td class="r ${sgn(v.share_pct_local)}">${pct(v.share_pct_local)}</td><td class="r ${sgn(v.fx_effect_pct)}">${pct(v.fx_effect_pct)}</td>
      <td class="r ${sgn(v.total_pct)}"><b>${pct(v.total_pct)}</b></td><td>${bar}</td></tr>`;
    }).join('');
}

/* ------------------------------------------------------------- charts */
function renderChartControls() {
  if (!$('#ranges').children.length) {
    $('#ranges').innerHTML = RANGES.map(([l]) => `<button data-r="${l}" class="${l === S.range ? 'on' : ''}">${l}</button>`).join('');
    $('#ranges').onclick = e => { const b = e.target.closest('button'); if (!b) return; S.range = b.dataset.r; setOn('#ranges', b); renderLegend(); drawMain(); };
    $('#legend').onclick = e => { const b = e.target.closest('.chip'); if (!b) return; const n = b.dataset.n; S.hidden.has(n) ? S.hidden.delete(n) : S.hidden.add(n); renderLegend(); drawMain(); };
    $('#modes').onclick = e => { const b = e.target.closest('button'); if (!b) return; S.mode = b.dataset.mode; setOn('#modes', b); drawMain(); };
    const cv = $('#chart');
    const move = e => { const r = cv.getBoundingClientRect(); const x = (e.touches ? e.touches[0].clientX : e.clientX) - r.left; S.hover = x; drawMain(); };
    cv.addEventListener('mousemove', move); cv.addEventListener('touchmove', move, { passive: true });
    cv.addEventListener('mouseleave', () => { S.hover = null; drawMain(); });
    window.addEventListener('resize', () => { drawMain(); renderCardsSparkOnly(); });
  }
}
function renderCardsSparkOnly() { document.querySelectorAll('.spark').forEach(c => drawSpark(c, S.live.players[+c.dataset.i])); }
function setOn(sel, btn) { document.querySelectorAll(sel + ' button').forEach(b => b.classList.toggle('on', b === btn)); }

const day = s => Date.parse(s + 'T00:00:00Z');
function seriesData() {
  const out = [];
  for (const p of S.live.players) {
    const h = S.hist.series[p.name]; if (!h) continue;
    const pts = h.d.map((d, i) => [day(d), h.p[i]]);
    const lt = day(new Date(S.live.updated).toLocaleDateString('en-CA', { timeZone: 'Europe/London' }));
    if (p.price_gbp != null) {
      if (pts.length && pts[pts.length - 1][0] === lt) pts[pts.length - 1][1] = p.price_gbp; else if (!pts.length || lt > pts[pts.length - 1][0]) pts.push([lt, p.price_gbp]);
    }
    out.push({ name: p.name, stock: p.stock, color: p.color, pts });
  }
  return out;
}

function prepare() {
  const full = seriesData(), all = full.filter(s => !S.hidden.has(s.name));
  const tmax = Math.max(...full.map(s => s.pts.length ? s.pts[s.pts.length - 1][0] : 0));
  const months = RANGES.find(r => r[0] === S.range)[1];
  let tmin = -Infinity;
  if (months) { const c = new Date(tmax); c.setUTCMonth(c.getUTCMonth() - months); tmin = c.getTime(); }
  let sel = all.map(s => ({ ...s, pts: s.pts.filter(p => p[0] >= tmin) })).filter(s => s.pts.length > 1);
  if (S.mode === 'pct' && sel.length) {
    const start = Math.max(...sel.map(s => s.pts[0][0]));           // common start so lines are comparable
    sel = sel.map(s => { const pts = s.pts.filter(p => p[0] >= start); const b = pts[0][1]; return { ...s, raw: pts, pts: pts.map(p => [p[0], (p[1] / b - 1) * 100]) }; });
  } else sel = sel.map(s => ({ ...s, raw: s.pts }));
  return sel;
}

function niceTicks(lo, hi, n) {
  const span = hi - lo || 1, raw = span / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw) || raw;
  const out = []; for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(v); return out;
}

function renderLegend() {
  const allS = seriesData(), cut = cutoff(allS);
  $('#legend').innerHTML = allS.map(s => {
    const f = s.pts.filter(p => p[0] >= cut); const ch = f.length > 1 ? (f[f.length - 1][1] / f[0][1] - 1) * 100 : null;
    return `<button class="chip ${S.hidden.has(s.name) ? 'off' : ''}" data-n="${esc(s.name)}" style="border-color:${s.color}"><span class="dot" style="background:${s.color}"></span>${esc(s.name)} · ${esc(s.stock)} <b class="${sgn(ch)}">${ch == null ? '' : pct(ch, 1)}</b></button>`;
  }).join('');
}

function drawMain() {
  const cv = $('#chart'); if (!S.live || !S.hist) return;
  const w = cv.clientWidth, h = cv.clientHeight, dpr = window.devicePixelRatio || 1;
  cv.width = w * dpr; cv.height = h * dpr;
  const g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
  const sel = prepare();
  $('#chartnote').textContent = S.mode === 'pct' ? 'Lines start at 0% on the first date all selected shares have data for. Prices converted to £ using the exchange rate on each day.' : 'Prices converted to £ using the exchange rate on each day.';
  if (!sel.length) { g.fillStyle = '#8b97a5'; g.font = '14px system-ui'; g.fillText('No data for this selection', 20, 30); return; }
  const m = { l: 54, r: 14, t: 12, b: 28 };
  let t0 = Infinity, t1 = -Infinity, lo = Infinity, hi = -Infinity;
  sel.forEach(s => s.pts.forEach(p => { t0 = Math.min(t0, p[0]); t1 = Math.max(t1, p[0]); lo = Math.min(lo, p[1]); hi = Math.max(hi, p[1]); }));
  const pad = (hi - lo) * 0.06 || 1; lo -= pad; hi += pad;
  const X = t => m.l + (w - m.l - m.r) * (t - t0) / Math.max(1, t1 - t0), Y = v => h - m.b - (h - m.t - m.b) * (v - lo) / (hi - lo);
  g.font = '11.5px system-ui'; g.fillStyle = '#8b97a5'; g.strokeStyle = '#222a33'; g.lineWidth = 1;
  niceTicks(lo, hi, 5).forEach(v => { const y = Y(v); g.beginPath(); g.moveTo(m.l, y); g.lineTo(w - m.r, y); g.stroke(); g.textAlign = 'right'; g.fillText(S.mode === 'pct' ? v.toFixed(v % 1 ? 1 : 0) + '%' : '£' + v.toFixed(v % 1 ? 1 : 0), m.l - 6, y + 4); });
  if (S.mode === 'pct' && lo < 0 && hi > 0) { g.strokeStyle = '#3a4350'; g.beginPath(); g.moveTo(m.l, Y(0)); g.lineTo(w - m.r, Y(0)); g.stroke(); }
  const spanDays = (t1 - t0) / 864e5, nx = Math.max(2, Math.min(7, Math.floor((w - m.l) / 90)));
  g.textAlign = 'center'; g.fillStyle = '#8b97a5';
  for (let i = 0; i <= nx; i++) {
    const t = t0 + (t1 - t0) * i / nx, d = new Date(t);
    const label = spanDays > 200 ? d.toLocaleDateString('en-GB', { month: 'short', year: '2-digit', timeZone: 'UTC' }) : d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' });
    g.textAlign = i === 0 ? 'left' : i === nx ? 'right' : 'center'; g.fillText(label, X(t), h - 8);
  }
  sel.forEach(s => { g.beginPath(); s.pts.forEach((p, i) => i ? g.lineTo(X(p[0]), Y(p[1])) : g.moveTo(X(p[0]), Y(p[1]))); g.strokeStyle = s.color; g.lineWidth = 1.8; g.lineJoin = 'round'; g.stroke(); });
  S.geo = { m, t0, t1, w, h };
  if (S.hover != null && S.hover >= m.l && S.hover <= w - m.r) {
    const th = t0 + (t1 - t0) * (S.hover - m.l) / (w - m.l - m.r);
    const rows = sel.map(s => { const i = nearest(s.pts, th); return { s, p: s.pts[i], raw: s.raw[i] }; });
    const tx = X(rows[0].p[0]);
    g.strokeStyle = '#5a6675'; g.setLineDash([4, 4]); g.beginPath(); g.moveTo(tx, m.t); g.lineTo(tx, h - m.b); g.stroke(); g.setLineDash([]);
    rows.forEach(r => { g.fillStyle = r.s.color; g.beginPath(); g.arc(X(r.p[0]), Y(r.p[1]), 4, 0, 7); g.fill(); });
    const lines = [new Date(rows[0].p[0]).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' })]
      .concat(rows.map(r => `${r.s.name}: ${S.mode === 'pct' ? pct(r.p[1]) + '  (' + gbp(r.raw[1]) + ')' : gbp(r.p[1])}`));
    g.font = '12px system-ui'; const bw = Math.max(...lines.map(l => g.measureText(l).width)) + 20, bh = lines.length * 18 + 10;
    let bx = tx + 12; if (bx + bw > w - 4) bx = tx - bw - 12; const by = m.t + 6;
    g.fillStyle = 'rgba(22,27,34,.96)'; g.strokeStyle = '#3a4350'; g.beginPath(); g.roundRect ? g.roundRect(bx, by, bw, bh, 8) : g.rect(bx, by, bw, bh); g.fill(); g.stroke();
    g.textAlign = 'left'; lines.forEach((l, i) => { g.fillStyle = i ? rows[i - 1].s.color : '#e6edf3'; g.fillText(l, bx + 10, by + 18 + i * 18 - 2); });
  }
}
function cutoff(all) {
  const months = RANGES.find(r => r[0] === S.range)[1]; if (!months) return -Infinity;
  const tmax = Math.max(...all.map(s => s.pts[s.pts.length - 1][0])); const c = new Date(tmax); c.setUTCMonth(c.getUTCMonth() - months); return c.getTime();
}
function nearest(pts, t) {
  let a = 0, b = pts.length - 1;
  while (b - a > 1) { const mid = (a + b) >> 1; pts[mid][0] <= t ? a = mid : b = mid; }
  return Math.abs(pts[a][0] - t) <= Math.abs(pts[b][0] - t) ? a : b;
}

load(true);
setInterval(() => load(false), 60000);
