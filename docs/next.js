/* next.js — router and views for the mobile-first surface.
 *
 * No framework, on purpose. The audit found Style & Layout at 2,917 ms of a
 * 5,300 ms main thread on the broadsheet page; a framework would add script
 * evaluation to a problem that was never about script. What fixes it is
 * shipping less DOM, and a hash router over static JSON does that with ~10 KB.
 *
 * THREE RULES THIS FILE KEEPS
 *
 *  1. Reserve before you fetch. Every async block paints a skeleton of the
 *     same height first, so nothing that arrives late moves anything already
 *     read. The broadsheet's CLS of 0.303 is one element reflowing.
 *  2. Fail out loud. A dead endpoint renders what failed and when it was last
 *     good — never a spinner that never resolves, and never an empty panel
 *     that reads as "no data" when it means "no answer".
 *  3. Serve stale before nothing. Every payload is cached in sessionStorage;
 *     on a failed refetch the last good copy renders with its age stated.
 */
(() => {
  'use strict';

  /* ── data ──────────────────────────────────────────────────────────────── */
  const CACHE = 'sig:';
  const TIMEOUT = 8000;

  async function get(url) {
    const key = CACHE + url;
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), TIMEOUT);
    try {
      const r = await fetch(url, { signal: ctl.signal });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const j = await r.json();
      try { sessionStorage.setItem(key, JSON.stringify({ at: Date.now(), j })); } catch (e) { /* private mode */ }
      return { ok: true, data: j, stale: false };
    } catch (err) {
      let cached = null;
      try { cached = JSON.parse(sessionStorage.getItem(key) || 'null'); } catch (e) { /* ignore */ }
      if (cached) return { ok: true, data: cached.j, stale: true, age: Date.now() - cached.at };
      return { ok: false, error: err.name === 'AbortError' ? 'timed out' : err.message };
    } finally { clearTimeout(t); }
  }

  /* ── format ────────────────────────────────────────────────────────────── */
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  // Crore / lakh. "₹1,00,00,000" makes the reader count digit groups to tell a
  // crore from ten lakh, which is the work a headline number exists to save.
  const money = v => {
    const n = Number(v);
    if (!isFinite(n)) return '—';
    const s = n < 0 ? '-' : '', a = Math.abs(n);
    if (a >= 1e7) return s + '₹' + trim(a / 1e7) + ' Cr';
    if (a >= 1e5) return s + '₹' + trim(a / 1e5) + ' L';
    return s + '₹' + Math.round(a).toLocaleString('en-IN');
  };
  const trim = x => String(x.toFixed(2)).replace(/\.?0+$/, '');
  const pct = v => { const n = Number(v); return isFinite(n) ? (n > 0 ? '+' : '') + n.toFixed(2) + '%' : '—'; };
  const dir = v => Number(v) > 0 ? 'up' : Number(v) < 0 ? 'dn' : '';
  const ago = ms => { const m = Math.round(ms / 60000); return m < 60 ? m + 'm' : Math.round(m / 60) + 'h'; };

  /* Outbound detail links.
   *
   * Deliberately NOT a modal fed by screen-detail.json: that file is 3.1 MB,
   * and this surface exists because a phone should not download 3.1 MB to read
   * one company. Screener.in already renders the filings better than a modal
   * would, and TradingView already renders the chart — linking out costs one
   * tap and zero bytes until the reader asks.
   */
  const chartUrl  = (sym, tv) => `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(tv || ('NSE:' + sym))}`;
  const detailUrl = sym => `https://www.screener.in/company/${encodeURIComponent(sym)}/consolidated/`;
  const symLinks = (sym, tv) => !sym ? '' :
    `<span class="lnks">
      <a href="${detailUrl(sym)}" target="_blank" rel="noopener" title="Fundamentals on Screener.in">Details</a>
      <a href="${chartUrl(sym, tv)}" target="_blank" rel="noopener" title="Chart on TradingView">Chart</a>
    </span>`;

  /* Live prices for arbitrary symbols, from /api/signals?px=.
   * Batched into one request — 30 cards asking individually is 30 round trips
   * and Yahoo rate-limits long before that. */
  async function quotes(syms) {
    const list = [...new Set((syms || []).filter(Boolean))].slice(0, 40);
    if (!list.length) return {};
    const r = await get('/api/signals?px=' + encodeURIComponent(list.join(',')));
    return (r.ok && r.data && r.data.quotes) ? r.data.quotes : {};
  }
  // Unrealised move on an unfilled order or an open signal. Direction-aware:
  // a SELL signal that falls is winning, and treating every position as long
  // is how a short book reports its losses as gains.
  const pnlOf = (entry, last, action) => {
    const e = Number(entry), l = Number(last);
    if (!isFinite(e) || !isFinite(l) || e === 0) return null;
    const raw = (l - e) / e * 100;
    return /SELL|SHORT/i.test(String(action || '')) ? -raw : raw;
  };

  const skel = (cls, n) => Array.from({ length: n }, () => `<div class="sk ${cls}"></div>`).join('');
  const fail = (what, why) =>
    `<div class="note err"><b>${esc(what)} did not load.</b> ${esc(why)}. Everything else on this page is unaffected.</div>`;
  const staleNote = age =>
    `<div class="note">Showing the last good copy, <b>${ago(age)}</b> old — the live call did not answer just now.</div>`;

  /* ── shell ─────────────────────────────────────────────────────────────── */
  const main = document.getElementById('main');
  const paint = html => { main.innerHTML = html; };

  const head = (title, sub) =>
    `<div class="route-h"><h1>${esc(title)}</h1>${sub ? `<p>${esc(sub)}</p>` : ''}</div>`;
  const sec = (label, body, n) =>
    `<section class="sec"><div class="sec-h"><h2>${esc(label)}</h2>${n ? `<span class="sec-n">${esc(n)}</span>` : ''}</div>${body}</section>`;
  const tile = (v, k, sub, cls) =>
    `<div class="tile"><div class="v ${cls || ''}">${v}</div>${sub ? `<div class="sub">${sub}</div>` : ''}<div class="k">${esc(k)}</div></div>`;

  /* ── shared widgets ────────────────────────────────────────────────────── */

  // Five steps each way, on the sector's own median. A continuous ramp reads
  // as decoration; steps read as a scale you can compare two tiles against.
  const heatClass = v => v >= 1.5 ? 'h-p3' : v >= .6 ? 'h-p2' : v > .1 ? 'h-p1'
                       : v <= -1.5 ? 'h-n3' : v <= -.6 ? 'h-n2' : v < -.1 ? 'h-n1' : 'h-z';

  // Tiles are buttons. A heat map that shows a median and refuses "which
  // names" is half an answer, and the drill-down data is already in the
  // digest — no extra request, no 1.26 MB download.
  window.__sectors = {};
  window.__heatKey = 'r1w';
  const heatmap = (sectors, key) => { window.__heatKey = key || 'r1w'; return !sectors || !sectors.length ? '' :
    `<div class="heat">${sectors.map(s => {
      window.__sectors[s.name] = s;
      // Width carries the sector's weight in names, so a 105-name move and a
      // 12-name move are not the same rectangle.
      const grow = Math.max(1, Math.round(s.n / 8));
      return `<button type="button" class="heat-t ${heatClass(s.median)}" style="flex-grow:${grow}"
                   data-sector="${esc(s.name)}"
                   title="${esc(s.name)} — open the names behind this move">
        <span class="hs">${esc(s.name)}</span>
        <span class="hv">${pct(s.median)}</span>
        <span class="hn">${s.n} names · ${s.up} up</span>
      </button>`; }).join('')}</div>`; };

  // One delegated listener for every heat tile on every route.
  // One delegated listener for every heat tile AND every symbol, bound once.
  // Per-route binding is what left most of the site dead: a row rendered by a
  // route that forgot to bind was unclickable, and every route forgot.
  document.addEventListener('click', ev => {
    if (!ev.target.closest) return;
    const t = ev.target.closest('.heat-t');
    if (t && t.dataset.sector) { openSector(t.dataset.sector); return; }
    if (ev.target.closest('a')) return;          // never hijack a real link
    const n = ev.target.closest('[data-sym]');
    if (n && n.dataset.sym) openStock(n.dataset.sym);
  });
  document.addEventListener('keydown', ev => {
    if (ev.key !== 'Enter' && ev.key !== ' ') return;
    const n = ev.target.closest && ev.target.closest('[data-sym]');
    if (n && n.dataset.sym) { ev.preventDefault(); openStock(n.dataset.sym); }
  });

  function openSector(name) {
    const s = window.__sectors[name];
    if (!s) return;
    const k = window.__heatKey;
    const list = rows => rows && rows.length ? `<div class="rank">${rows.map((r, i) => `
        <div class="rank-r" data-sym="${esc(r.sym)}" role="button" tabindex="0">
          <span class="i">${i + 1}</span>
          <span class="s"><b>${esc(r.sym)}</b><span>${esc(r.name || '')}</span></span>
          <span class="x" style="color:var(--dim)">${r.rsi != null ? 'RSI ' + Math.round(r.rsi) : ''}</span>
          <span class="m ${dir(r[k])}">${pct(r[k])}</span>
        </div>`).join('')}</div>` : `<div class="empty">No names.</div>`;
    sheet(`${esc(name)}`,
      `<p class="hint" style="margin:0 0 14px">Median <b class="${dir(s.median)}">${pct(s.median)}</b>
        ${k === 'r1d' ? 'today' : 'on the week'} across <b>${s.n}</b> names, <b>${s.up}</b> of them up.</p>` +
      // "Held it back" implied losses. When a sector is broadly up its weakest
      // five are still positive — that is the +0.60% sitting under a "losers"
      // heading. Strongest and weakest; the numbers say whether either is red.
      sec('Strongest five', list(s.top)) +
      sec('Weakest five', list(s.bottom)));
  }

  /* A bottom sheet on a phone, a centred dialog on a desk. <dialog> gives the
   * focus trap and Escape for free — reimplementing either by hand is how
   * modals end up unreachable by keyboard. */
  function sheet(title, html) {
    let d = document.getElementById('sheet');
    if (!d) {
      d = document.createElement('dialog');
      d.id = 'sheet'; d.className = 'sheet';
      document.body.appendChild(d);
      d.addEventListener('click', e => { if (e.target === d) d.close(); });
    }
    d.innerHTML = `<div class="sheet-in">
      <div class="sheet-h"><h2>${title}</h2>
        <button type="button" class="icon-btn" data-x aria-label="Close">✕</button></div>
      <div class="sheet-b">${html}</div></div>`;
    d.querySelector('[data-x]').addEventListener('click', () => d.close());
    if (!d.open) d.showModal();
  }

  const breadthWidget = b => {
    if (!b || !b.counted) return '';
    const up = b.up / b.counted * 100, dn = b.down / b.counted * 100;
    return `<div class="breadth">
      <div class="breadth-n">
        <span><b class="up">${b.up}</b> <span style="color:var(--dim)">up</span></span>
        <span style="color:var(--dim);font:400 11px/1 var(--mono)">${b.counted} names screened</span>
        <span><b class="dn">${b.down}</b> <span style="color:var(--dim)">down</span></span>
      </div>
      <div class="breadth-bar"><i class="bu" style="width:${up.toFixed(1)}%"></i><i class="bd" style="width:${dn.toFixed(1)}%"></i></div>
      <div class="breadth-sub">Median name ${pct(b.median)} on the week ·
        <b style="color:var(--muted)">${b.above_200dma}</b> hold their 200-day ·
        <b style="color:var(--muted)">${b.at_52w_high}</b> at a 52-week high</div>
    </div>`;
  };

  // Every column is labelled. The first version printed "₹1,036cr" and "16.1"
  // with no header, and the honest reading of that is: nobody can tell what
  // either number is. A figure without its unit is decoration.
  /* Levels, not turnover. Turnover says how much traded, which almost never
   * changes a decision; where price sits against its own 50-day and 200-day,
   * and whether it is stretched on the daily AND the monthly, does. */
  const levelTable = rows => !rows || !rows.length ?
    `<div class="empty">Nothing qualifies today.</div>` :
    `<div class="rank">
      <div class="rank-r lvl-r rank-head">
        <span class="i">#</span><span class="s">Name</span>
        <span class="x">Price</span><span class="x">vs 50D</span><span class="x">vs 200D</span>
        <span class="x">RSI 14D</span><span class="x">RSI 1M</span><span class="m">1W</span>
      </div>
      ${rows.map((r, i) => {
        const v50 = r.sma50 ? (r.price - r.sma50) / r.sma50 * 100 : null;
        const v200 = r.sma200 ? (r.price - r.sma200) / r.sma200 * 100 : null;
        const hot = v => v == null ? 'var(--dim)' : v > 70 ? 'var(--warn)' : v < 35 ? 'var(--accent)' : 'var(--dim)';
        return `<div class="rank-r lvl-r" data-sym="${esc(r.sym)}" role="button" tabindex="0">
          <span class="i">${i + 1}</span>
          <span class="s"><b>${esc(r.sym)}</b><span>${esc(r.name || r.sector || '')}</span></span>
          <span class="x" data-px>₹${esc(r.price ?? '—')}</span>
          <span class="x ${dir(v50)}">${v50 == null ? '—' : pct(v50)}</span>
          <span class="x ${dir(v200)}">${v200 == null ? '—' : pct(v200)}</span>
          <span class="x" style="color:${hot(r.rsi)}">${r.rsi != null ? Math.round(r.rsi) : '—'}</span>
          <span class="x" style="color:${hot(r.rsi_m)}">${r.rsi_m != null ? Math.round(r.rsi_m) : '—'}</span>
          <span class="m ${dir(r.r1w)}">${pct(r.r1w)}</span>
        </div>`; }).join('')}</div>`;

  const COLHEAD = {
    turnover_cr: 'Turnover', vol_spike: 'Volume vs avg', rsi: 'RSI',
    r1w: '1 week', r1m: '1 month', from_high: 'From high'
  };
  const rankList = (rows, valKey, fmt, subKey) => !rows || !rows.length ?
    `<div class="empty">Nothing qualifies today.</div>` :
    `<div class="rank">
      <div class="rank-r rank-head">
        <span class="i">#</span>
        <span class="s">Name</span>
        <span class="x">${esc(COLHEAD[subKey] || '')}</span>
        <span class="m">${esc(COLHEAD[valKey] || '')}</span>
      </div>
      ${rows.map((r, i) => `
      <div class="rank-r">
        <span class="i">${i + 1}</span>
        <span class="s"><b>${esc(r.sym)}</b><span>${esc(r.name || r.sector || '')}</span>${symLinks(r.sym)}</span>
        <span class="x" style="color:var(--dim)">${subKey && r[subKey] != null ? esc(fmtSub(subKey, r[subKey])) : ''}</span>
        <span class="m ${dir(r[valKey])}">${fmt(r[valKey])}</span>
      </div>`).join('')}</div>`;
  const fmtSub = (k, v) => k === 'turnover_cr' ? '₹' + Math.round(v).toLocaleString('en-IN') + ' cr'
                         : k === 'vol_spike' ? v.toFixed(1) + '×'
                         : k === 'rsi' ? Math.round(v) : String(v);

  /* ── routes ────────────────────────────────────────────────────────────── */
  const R = {};

  R['/'] = async () => {
    paint(head('Today', 'India’s markets, in one screen — rebuilt every morning before the open.') +
      sec('The tape', `<div class="grid">${skel('sk-tile', 4)}</div>`) +
      sec('Where the money went', `<div class="sk" style="height:104px"></div>`) +
      sec('The wire', skel('sk-card', 3)));

    const [t, p, n, m] = await Promise.all(
      [get('/today.json'), get('/pulse.json'), get('/news.json'), get('/api/markets')]);
    let out = head('Today', 'India’s markets, in one screen — rebuilt every morning before the open.');
    if (!t.ok && !p.ok) { paint(out + fail('Today', t.error || p.error)); return; }

    const d = t.ok ? t.data : {}, pu = p.ok ? p.data : {}, br = pu.breadth || {};
    const mk = m.ok ? m.data : null;
    const nifty = mk && (mk.markets || []).find(x => /nifty 50/i.test(x.name || ''));

    out += sec('The tape', `<div class="grid">
        ${tile(nifty ? esc(nifty.price) : '—', 'Nifty 50',
               nifty ? pct(nifty.change_pct) : 'feed unreachable', nifty ? dir(nifty.change_pct) : '')}
        ${tile(br.up != null ? `${br.up}<span style="color:var(--dim)">/${br.counted}</span>` : '—',
               'Advancing', br.median != null ? `median ${pct(br.median)} on the week` : '',
               br.up > br.down ? 'up' : 'dn')}
        ${tile(br.at_52w_high ?? '—', 'At 52-week highs', 'across the screened universe', 'ac')}
        ${tile((d.picks || []).length, 'Ideas this week', 'ranked once per ISO week')}
      </div>`);

    // Today, over the 250 largest — not a week over all 750. A daily paper's
    // front page should answer "what happened today", and a 750-name median is
    // dominated by the 500 small and micro caps most readers never trade.
    // Falls back to the week when the screen predates r1d, and SAYS which one
    // it is showing — an unlabelled heat map is the reader guessing.
    const dayMap = (pu.sectors_day || []).length;
    out += sec(dayMap ? 'Where the money went today' : 'Where the money went this week',
      heatmap(dayMap ? pu.sectors_day : (pu.sectors || []).slice(0, 11), dayMap ? 'r1d' : 'r1w') +
      `<p class="hint">${dayMap
        ? `Median move <b>today</b> per sector, across the <b>${pu.day_universe || 250} largest</b> by market cap. Tile width is how many names it holds.`
        : 'Median move over the <b>week</b>, across all 750 screened names — today\'s figures arrive with the next screen build.'}
        Tap a sector for the names behind it.</p>`,
      dayMap ? 'today · large caps' : 'this week · all 750');

    const wire = n.ok ? n.data : [];
    out += sec('The wire', wire.length ? `<div class="wire">${wire.slice(0, 6).map(x => `
        <a href="${esc(x.link || '#')}" ${x.link ? 'target="_blank" rel="noopener"' : ''}>
          <span class="ws">${esc(x.source || 'wire')}</span>
          <span class="wt">${esc(x.title || '')}</span>
          ${x.summary ? `<span class="wd">${esc(String(x.summary).slice(0, 150))}</span>` : ''}
        </a>`).join('')}</div>` : `<div class="empty">The wire is quiet.</div>`, `${wire.length} stories`);

    const cv = await get('/conviction.json');
    if (cv.ok && (cv.data.picks || []).length) {
      const c = cv.data;
      // Five symbols, one call. The slate is priced at the morning build; this
      // is what it is worth now.
      const cvpx = await quotes(c.picks.map(x => x.sym));
      c.picks.forEach(x => { x._live = cvpx[x.sym] || null; });
      out += sec('Today’s conviction', c.picks.map(convictionCard).join('') +
        `<details class="meth"><summary>How these five were chosen</summary>
           <p>${esc(c.method)}</p>
           <p class="hint">Ranked ${esc(c.date)} over ${esc(c.universe)} screened names. The slate is
           logged every day, so it can be graded later rather than quietly rewritten.</p>
         </details>`,
        `${c.picks.length} names · ${esc(c.date)}`);
    } else {
      const pk = (d.picks || [])[0];
      if (pk) out += sec('This week’s top idea', ideaCard(pk, true));
    }

    const io = (await get('/ipo.json'));
    if (io.ok && (io.data.open || []).length) {
      out += sec('Open right now', io.data.open.slice(0, 2).map(ipoCard).join(''),
        `${io.data.open.length} book${io.data.open.length === 1 ? '' : 's'} open`);
    }
    paint(out);
  };

  const convictionCard = p => `<article class="card cv" data-sym="${esc(p.sym)}" role="button" tabindex="0">
    <div class="card-h">
      <span class="sym">${esc(p.sym)}</span>
      <span class="pill pill-ac">${esc(p.score)}</span>
      ${p.brk52w ? `<span class="pill pill-up">52w high</span>` : ''}
      <span class="spacer"></span>
      <span class="pill">${esc(p.sector || '')}</span>
    </div>
    <div class="card-body" style="color:var(--text);font-weight:500">${esc(p.name || '')}</div>
    ${p.view ? `<div class="cv-view"><span>View</span>${esc(p.view)}</div>` : ''}
    ${(p.reasons || []).length ? `<div class="reads">${p.reasons.map(x =>
        `<div class="read read-for">${esc(x)}</div>`).join('')}</div>` : ''}
    <div class="kv">
      <div><span class="kk">${p._live ? 'Live' : 'Price'}</span><span class="vv${p._live ? ' lv' : ''}">₹${p._live ? p._live.price.toFixed(2) : esc(p.price)}</span></div>
      <div><span class="kk">Today</span><span class="vv ${p._live ? dir(p._live.change_pct) : ''}">${p._live && p._live.change_pct != null ? pct(p._live.change_pct) : '—'}</span></div>
      <div><span class="kk">1M</span><span class="vv ${dir(p.r1m)}">${pct(p.r1m)}</span></div>
      <div><span class="kk">3M</span><span class="vv ${dir(p.r3m)}">${pct(p.r3m)}</span></div>
      <div><span class="kk">ROCE</span><span class="vv ${dir(p.roce)}">${p.roce != null ? p.roce.toFixed(1) + '%' : '—'}</span></div>
      <div><span class="kk">Piotroski</span><span class="vv">${p.piotroski != null ? p.piotroski + '/9' : '—'}</span></div>
      <div><span class="kk">RSI 14D</span><span class="vv">${p.rsi != null ? Math.round(p.rsi) : '—'}</span></div>
    </div>
    ${p.entry ? `<div class="kv lv-plan">
      <div><span class="kk">Entry</span><span class="vv">₹${esc(p.entry)}</span></div>
      <div><span class="kk">Stop</span><span class="vv dn">₹${esc(p.stop)} <i>${esc(p.stop_pct)}%</i></span></div>
      <div><span class="kk">Target 1</span><span class="vv up">₹${esc(p.t1)} <i>+${esc(p.t1_pct)}%</i></span></div>
      <div><span class="kk">Target 2</span><span class="vv up">₹${esc(p.t2)} <i>+${esc(p.t2_pct)}%</i></span></div>
    </div>
    ${trailPlan(p.entry, p.stop, p.t1, p.t2, 'BUY')}` : ''}
    <div class="card-foot">
      <span class="mono" style="font-size:11px;color:var(--dim)">₹${p.turnover_cr != null ? Math.round(p.turnover_cr) : '—'} cr traded · not advice</span>
      ${symLinks(p.sym)}
    </div>
  </article>`;

  const ideaCard = (p, lead) => {
    const cur = p.currency || '₹';
    return `<article class="card" data-sym="${esc(p.symbol || '')}" role="button" tabindex="0">
      <div class="card-h">
        <span class="sym">${esc(p.symbol || '')}</span>
        ${p.score != null ? `<span class="pill pill-ac">${esc(p.score)}/100</span>` : ''}
        <span class="spacer"></span>
        <span class="num ${dir(p.change_1d)}" style="font-size:13px">${pct(p.change_1d)}</span>
      </div>
      ${lead && p.target_basis ? `<div class="card-body">Target is ${esc(p.target_basis)}; the stop is ${esc(p.stop_basis || 'below the trend')}.</div>` : ''}
      <div class="kv">
        <div><span class="kk">Price</span><span class="vv">${cur}${esc(p.price)}</span></div>
        <div><span class="kk">Target</span><span class="vv up">${cur}${esc(p.target)}</span></div>
        <div><span class="kk">Stop</span><span class="vv dn">${cur}${esc(p.stop_loss)}</span></div>
        <div><span class="kk">R:R</span><span class="vv">${esc(p.rr)}</span></div>
        <div><span class="kk">1M</span><span class="vv ${dir(p.mom_1m)}">${pct(p.mom_1m)}</span></div>
        <div><span class="kk">Horizon</span><span class="vv" style="font-size:11.5px">${esc(p.timeframe || '—')}</span></div>
      </div>
    </article>`;
  };

  // The verdict leads. Someone deciding whether to apply wants the call and
  // the reason before the lot size.
  const ipoCard = r => {
    const v = String(r.verdict || '').toUpperCase();
    const cls = v.startsWith('APPLY') ? 'v-apply' : v === 'AVOID' ? 'v-avoid' : 'v-watch';
    const sub = Number(r.subscription_x);
    const pctOfTen = isFinite(sub) ? Math.min(100, sub / 10 * 100) : 0;
    const forr = r.reads_for || [], agn = r.reads_against || [];
    return `<article class="ipo" data-sym="${esc(r.symbol || r.sym || '')}" role="button" tabindex="0">
      <div class="ipo-h">
        <span class="sym">${esc(r.symbol || r.sym || '')}</span>
        ${r.verdict ? `<span class="pill ${cls}">${esc(r.verdict)}</span>` : ''}
        <span class="spacer"></span>
        ${r.days_left != null ? `<span class="pill">${r.days_left === 0 ? 'closes today' : esc(r.days_left) + 'd left'}</span>` : ''}
        <span class="co">${esc(r.company || '')}</span>
      </div>
      ${r.verdict_why ? `<div class="ipo-why">${esc(r.verdict_why)}</div>` : ''}
      ${isFinite(sub) ? `<div class="subs">
        <span class="subs-v">${sub.toFixed(2)}×</span>
        <span class="subs-bar" style="--one:10%"><i style="width:${pctOfTen.toFixed(0)}%"></i></span>
        <span class="subs-v" style="color:var(--dim);font-size:11px">of 10×</span>
      </div>` : ''}
      <div class="kv">
        <div><span class="kk">Band</span><span class="vv" style="font-size:11.5px">${esc(r.price_band || '—')}</span></div>
        <div><span class="kk">Lot</span><span class="vv">${esc(r.lot_size ?? '—')}</span></div>
        <div><span class="kk">Min</span><span class="vv">${r.min_investment ? money(r.min_investment) : '—'}</span></div>
        <div><span class="kk">Size</span><span class="vv">${r.issue_size_cr ? '₹' + Math.round(r.issue_size_cr) + 'cr' : '—'}</span></div>
        <div><span class="kk">GMP</span><span class="vv">${esc(r.gmp_text || '—')}</span></div>
        <div><span class="kk">P/E post</span><span class="vv">${r.pe_post_issue ? r.pe_post_issue.toFixed(1) + '×' : '—'}</span></div>
      </div>
      ${(forr.length || agn.length) ? `<div class="reads">
        ${forr.slice(0, 2).map(x => `<div class="read read-for">${esc(x)}</div>`).join('')}
        ${agn.slice(0, 2).map(x => `<div class="read read-against">${esc(x)}</div>`).join('')}
      </div>` : ''}
      ${r.verdict_caveat ? `<div class="ipo-caveat">${esc(r.verdict_caveat)}</div>` : ''}
    </article>`;
  };

  R['/markets'] = async () => {
    paint(head('Markets', 'The board live, and what the 750-name screen underneath it did.') +
      sec('Breadth', `<div class="sk" style="height:104px"></div>`) +
      sec('Sector heat', `<div class="sk" style="height:120px"></div>`) +
      sec('The board', `<div class="board">${skel('sk-row', 8)}</div>`));

    const [m, p] = await Promise.all([get('/api/markets'), get('/pulse.json')]);
    let out = head('Markets', 'The board live, and what the 750-name screen underneath it did.');
    const pu = p.ok ? p.data : {};

    out += sec('Breadth', breadthWidget(pu.breadth) || `<div class="empty">Screen not built yet.</div>`);
    out += sec('Sector heat — the week, all 750', heatmap(pu.sectors, 'r1w') +
      `<p class="hint">Median move over the <b>past week</b> across the full <b>${pu.universe || 750}-name</b>
        screen — the wider, slower view. The front page shows today over the largest 250.
        Width is how many names the sector holds; tap one for the names behind it.</p>`,
      pu.sectors ? `${pu.sectors.length} sectors · one week` : '');

    // /api/ticker, not /api/markets: markets returns a curated NINE, the
    // ticker returns all 71 across eleven segments — Asia, India, Europe, US,
    // commodities, FX (USD/INR, MYR/INR, USD/MYR, AED/INR), crypto. The board
    // was showing a twelfth of what the origin already computes.
    const tk = await get('/api/ticker');
    if (tk.ok) {
      const segs = (tk.data.segments || []).filter(sg => (sg.items || []).length);
      out += sec('The board', segs.map(sg => `
        <div class="segh">${esc(sg.icon || '')} ${esc(sg.label)}</div>
        <div class="board">${sg.items.map(r => `
          <div class="board-row">
            <span class="n">${esc(r.name || r.symbol || '')}</span>
            <span class="p">${esc(r.price ?? '—')}</span>
            <span class="c ${dir(r.change_pct)}">${pct(r.change_pct)}</span>
          </div>`).join('')}</div>`).join(''),
        `${tk.data.live ?? 0} of ${tk.data.total ?? 0} live`);
    } else { out += sec('The board', fail('The live board', tk.error)); }

    out += sec('Biggest movers, one week', levelTable((pu.movers_up || []).slice(0, 8)));
    out += sec('Biggest fallers, one week', levelTable((pu.movers_dn || []).slice(0, 8)));
    paint(out);
  };

  R['/ideas'] = async () => {
    paint(head('Ideas', 'Ranked names, and the orders a fully-sized book would place against them. Sizes are shown as a share of the book, so they scale to whatever you run.') +
      sec('Trade ideas', skel('sk-card', 3)));
    const [t, mn, p] = await Promise.all([get('/today.json'), get('/mandate.json'), get('/pulse.json')]);
    let out = head('Ideas', 'Ranked names, and the orders a fully-sized book would place against them. Sizes are shown as a share of the book, so they scale to whatever you run.');
    if (!t.ok) { paint(out + fail('Ideas', t.error)); return; }

    const picks = t.data.picks || [];
    out += sec('Trade ideas', picks.length ? picks.map(x => ideaCard(x, false)).join('')
      : `<div class="empty">Nothing clears the bar this week. That is a result, not a gap.</div>`,
      `${picks.length} ranked`);

    const pu = p.ok ? p.data : {};
    out += sec('Breaking to 52-week highs',
      levelTable((pu.breakouts || []).slice(0, 12)),
      pu.breakouts ? `${pu.breakouts.length} names` : '');

    if (mn.ok) {
      const d = mn.data, st = d.state || {}, orders = d.admitted || [];
      // Marks for every order in one call, so the page shows what the idea is
      // worth NOW rather than what it was worth at 6 AM.
      const px = await quotes(orders.map(o => o.symbol));
      // Size as a SHARE of the book, not only in rupees. A reader running
      // ₹2 lakh cannot use "₹10 L"; they can use "10% of the book".
      const shareOf = v => d.capital ? (Number(v) / d.capital * 100) : null;

      out += sec('The book', `<div class="grid" style="margin-bottom:10px">
          ${tile(orders.length, 'Orders to place', 'nothing here is bought yet', 'ac')}
          ${tile(st.deployed_pct != null ? st.deployed_pct + '%' : '—', 'Would be deployed',
                 st.heat_pct != null ? st.heat_pct + '% at risk if every stop hits' : '')}
        </div>` + (orders.length ? orders.map(o => {
          const live = px[o.symbol];
          const move = live ? pnlOf(o.entry, live.price, 'BUY') : null;
          const sh = shareOf(o.notional);
          return `<article class="card" data-sym="${esc(o.symbol || '')}" role="button" tabindex="0">
            <div class="card-h"><span class="sym">${esc(o.symbol || '')}</span>
              ${o.engine ? `<span class="pill">${esc(o.engine)}</span>` : ''}
              <span class="spacer"></span>
              ${live ? `<span class="pill ${move > 0 ? 'pill-up' : 'pill-dn'}">${pct(move)} vs entry</span>`
                     : `<span class="pill">no mark</span>`}
              ${o.rr ? `<span class="pill pill-up">${esc(o.rr)}:1</span>` : ''}</div>
            <div class="kv">
              <div><span class="kk">Entry</span><span class="vv">₹${esc(o.entry ?? '—')}</span></div>
              <div><span class="kk">Last</span><span class="vv">${live ? '₹' + live.price.toFixed(2) : '—'}</span></div>
              <div><span class="kk">Stop</span><span class="vv dn">₹${esc(o.stop ?? '—')}</span></div>
              <div><span class="kk">Size</span><span class="vv">${sh != null ? sh.toFixed(1) + '% of book' : '—'}</span></div>
              <div><span class="kk">Risk</span><span class="vv">${o.risk_pct != null ? o.risk_pct + '%' : (d.capital ? (Number(o.risk_amount) / d.capital * 100).toFixed(2) + '%' : '—')}</span></div>
              <div><span class="kk">Hold</span><span class="vv" style="font-size:11px">${esc(o.hold_days || o.horizon || '—')}</span></div>
            </div>
            ${(o.legs || []).length ? `<div class="ladder">
              ${o.legs.map(l => `<div class="leg">
                <span class="leg-l">${esc(l.label)}</span>
                <span class="leg-p">₹${esc(l.price)}</span>
                <span class="leg-q">${esc(l.qty)} sh</span>
                <span class="leg-g up">+${esc(l.gain_pct)}%</span>
                <span class="leg-r">${esc(l.r_multiple)}R</span>
              </div>`).join('')}
            </div>` : ''}
            ${o.trail_note ? `<div class="trail"><span>Trailing stop</span>${esc(o.trail_note)}</div>` : ''}
            <div class="card-foot">
              <span class="mono" style="font-size:11px;color:var(--dim)">${sh != null ? 'sized at ' + sh.toFixed(1) + '% — scale to your own book' : ''}${o.hold_days ? ' · hold ' + esc(o.hold_days) : ''}</span>
              ${symLinks(o.symbol)}
            </div>
          </article>`; }).join('') : `<div class="empty">No orders clear the mandate today.</div>`));
    }
    paint(out);
  };

  R['/ipo'] = async () => {
    paint(head('IPO', 'Books open now, what is coming, and how the last year of listings actually did.') +
      sec('Open now', skel('sk-card', 2)));
    const io = await get('/ipo.json');
    let out = head('IPO', 'Books open now, what is coming, and how the last year of listings actually did.');
    if (!io.ok) { paint(out + fail('The IPO radar', io.error)); return; }
    const d = io.data, c = d.counts || {};

    out += sec('Where it stands', `<div class="grid">
        ${tile((d.open || []).length, 'Books open', 'bidding today', (d.open || []).length ? 'ac' : '')}
        ${tile((d.upcoming || []).length, 'Upcoming', 'announced, not open')}
        ${tile((d.awaiting_listing || []).length, 'Awaiting listing', 'closed, not yet traded')}
        ${tile(c.apply ?? '—', 'Rated apply', 'on public demand only', c.apply ? 'up' : '')}
      </div>`);

    out += sec('Open now', (d.open || []).length ? d.open.map(ipoCard).join('')
      : `<div class="empty">No mainboard book is open today.</div>`);

    if ((d.upcoming || []).length) out += sec('Upcoming', d.upcoming.map(ipoCard).join(''));
    if ((d.awaiting_listing || []).length)
      out += sec('Awaiting listing', d.awaiting_listing.map(ipoCard).join(''));

    const rec = (d.recent_listed || []).slice().sort((a, b) =>
      (b.since_listing_pct ?? -1e9) - (a.since_listing_pct ?? -1e9));
    out += sec('How last year’s listings did', rec.length ? `<div class="rank">${rec.map((r, i) => `
        <div class="rank-r">
          <span class="i">${i + 1}</span>
          <span class="s"><b>${esc(r.sym || r.symbol || '')}</b><span>listed ${esc(r.listed_on || '—')}${r.price_band ? ' · band ' + esc(r.price_band) : ''}</span>${symLinks(r.sym || r.symbol)}</span>
          <span class="x" style="color:var(--dim)">${r.from_high_pct != null ? pct(r.from_high_pct) + ' off high' : ''}</span>
          <span class="m ${dir(r.since_listing_pct)}">${pct(r.since_listing_pct)}</span>
        </div>`).join('')}</div>` : `<div class="empty">No listings in the window.</div>`,
      `${rec.length} shown`);
    paint(out);
  };

  // The tool. Filters run over the pulse digest, not over screen.json — the
  // answers are already computed, so a chip is a re-render and not a download.
  /* THE SCREEN — all 750 names, not the digest.
   *
   * The digest answers four fixed questions in 23 KB. A screener has to answer
   * the reader's question, which means the whole file: screen.json, 1.26 MB
   * raw and ~260 KB over the wire. It is fetched ONLY when this route opens
   * and cached for the session, so every other route still costs nothing —
   * which is the whole reason the digest exists and why both can coexist.
   */
  let SCREEN = null;
  let scrQ = '', scrPreset = 'all', scrSort = 'comp';
  const PRESETS = {
    all:        ['Everything',     () => true],
    breakout:   ['Breaking out',   r => (r.setup?.tags || []).some(t => /BREAKOUT/.test(t))],
    rsleader:   ['RS leaders',     r => (r.setup?.tags || []).includes('RS LEADER')],
    volume:     ['Volume spike',   r => (r.vol_spike ?? 0) >= 2],
    oversold:   ['Oversold',       r => (r.rsi ?? 99) < 35],
    quality:    ['High quality',   r => (r.q ?? 0) >= 70],
    value:      ['Cheap',          r => (r.v ?? 0) >= 70],
    debtfree:   ['Debt-free',      r => (r.de ?? 9) <= 0.1],
    compounder: ['Compounders',    r => (r.roce ?? 0) >= 20 && (r.rev_cagr ?? 0) >= 12],
  };
  const SORTS = { comp: 'Composite', q: 'Quality', g: 'Growth', v: 'Value',
                  tech: 'Technical', r1m: '1M return', roce: 'ROCE', mcap_cr: 'Size' };

  R['/screen'] = async () => {
    const shell = body => head('Screen',
      'Every one of the 750 names, searchable. Tap any row for the full card.') + body;
    if (!SCREEN) paint(shell(`<div class="note">Loading the full universe — about 260 KB, once per session.</div>` +
      `<div class="sk" style="height:320px"></div>`));

    if (!SCREEN) {
      const r = await get('/screen.json');
      if (!r.ok) { paint(shell(fail('The screen', r.error))); return; }
      SCREEN = (r.data.rows || []).filter(x => x && x.sym);
    }

    const draw = () => {
      const q = scrQ.trim().toLowerCase();
      const rows = SCREEN
        .filter(PRESETS[scrPreset][1])
        .filter(r => !q || (r.sym || '').toLowerCase().includes(q)
                        || (r.name || '').toLowerCase().includes(q)
                        || (r.sector || '').toLowerCase().includes(q))
        .sort((a, b) => (b[scrSort] ?? -1e9) - (a[scrSort] ?? -1e9));

      main.innerHTML = shell(
        `<div class="tools">
          <input type="search" id="scrq" class="scr-in" placeholder="Symbol, company or sector"
                 value="${esc(scrQ)}" aria-label="Search the screen">
          <select id="scrs" class="scr-sel" aria-label="Rank by">
            ${Object.entries(SORTS).map(([k, l]) =>
              `<option value="${k}"${scrSort === k ? ' selected' : ''}>Rank by ${esc(l)}</option>`).join('')}
          </select>
        </div>
        <div class="chips" role="group" aria-label="Screen preset">${
          Object.entries(PRESETS).map(([k, [l]]) =>
            `<button type="button" class="chip" data-p="${k}" aria-pressed="${scrPreset === k}">${esc(l)}</button>`).join('')}
        </div>` +
        // 40, not 60: /api/signals?px= takes 40 symbols a call, so a 40-row
        // page is exactly one request and every visible row can carry a live
        // mark. A 60-row page would leave a third of the screen showing the
        // morning close beside two thirds showing live — worse than either.
        sec(PRESETS[scrPreset][0], rows.length ? screenTable(rows.slice(0, 40))
          : `<div class="empty">Nothing matches. Try a different preset or clear the search.</div>`,
          `${rows.length} of ${SCREEN.length}`));

      const inp = main.querySelector('#scrq');
      inp.addEventListener('input', () => {
        // Coalesced: 750 rows re-filtered on every keystroke is 750 rows of
        // work per keystroke, and the phone feels it.
        clearTimeout(inp._t);
        inp._t = setTimeout(() => { scrQ = inp.value; const at = inp.selectionStart; draw();
          const n = main.querySelector('#scrq'); n.focus(); n.setSelectionRange(at, at); }, 160);
      });
      main.querySelector('#scrs').addEventListener('change', e => { scrSort = e.target.value; draw(); });
      main.querySelectorAll('.chip').forEach(b =>
        b.addEventListener('click', () => { scrPreset = b.dataset.p; draw(); }));
      main.querySelectorAll('[data-sym]').forEach(el =>
        el.addEventListener('click', () => openStock(el.dataset.sym)));

      /* Live marks for the rows actually on screen. Fired after paint so the
       * table is readable immediately and the quotes fill in — a screen that
       * waits for a network call before showing 750 names it already has is
       * slower for no reason. Stamped by symbol, so a re-render mid-flight
       * cannot write a price into the wrong row. */
      const shown = rows.slice(0, 40).map(r => r.sym);
      const token = ++drawToken;
      quotes(shown).then(q => {
        if (token !== drawToken) return;          // a newer draw has replaced this one
        for (const sym of shown) {
          const live = q[sym];
          if (!live) continue;
          const cell = main.querySelector(`[data-sym="${CSS.escape(sym)}"] [data-px]`);
          if (!cell) continue;
          cell.textContent = '₹' + live.price.toFixed(2);
          cell.classList.add('lv');
          const day = main.querySelector(`[data-sym="${CSS.escape(sym)}"] [data-day]`);
          if (day && live.change_pct != null) {
            day.textContent = pct(live.change_pct);
            day.className = 'x ' + dir(live.change_pct);
          }
        }
      });
    };
    draw();
  };
  let drawToken = 0;

  // Turnover is out. It says how much traded, which almost never changes a
  // decision — where price sits against its own 50 and 200 day, and whether
  // it is stretched, does.
  const screenTable = rows => `<div class="rank">
    <div class="rank-r rank-head scr-r">
      <span class="i">#</span><span class="s">Name</span>
      <span class="x">Price</span><span class="x">Today</span><span class="x">vs 50D</span>
      <span class="x">vs 200D</span><span class="x">RSI 14D</span><span class="m">1M</span>
    </div>
    ${rows.map((r, i) => {
      const v50 = r.sma50 ? (r.price - r.sma50) / r.sma50 * 100 : null;
      const v200 = r.sma200 ? (r.price - r.sma200) / r.sma200 * 100 : null;
      return `<div class="rank-r scr-r" data-sym="${esc(r.sym)}" role="button" tabindex="0">
        <span class="i">${i + 1}</span>
        <span class="s"><b>${esc(r.sym)}</b><span>${esc(r.name || '')}</span></span>
        <span class="x" data-px>₹${esc(r.price ?? '—')}</span>
        <span class="x" data-day style="color:var(--dim)">·</span>
        <span class="x ${dir(v50)}">${v50 == null ? '—' : pct(v50)}</span>
        <span class="x ${dir(v200)}">${v200 == null ? '—' : pct(v200)}</span>
        <span class="x" style="color:${(r.rsi ?? 50) > 70 ? 'var(--warn)' : (r.rsi ?? 50) < 35 ? 'var(--accent)' : 'var(--dim)'}">${r.rsi != null ? Math.round(r.rsi) : '—'}</span>
        <span class="m ${dir(r.r1m)}">${pct(r.r1m)}</span>
      </div>`; }).join('')}</div>`;

  /* THE CARD. Same fields the broadsheet's modal shows, from the same
   * screen.json — the 3.1 MB screen-detail.json is not needed for any of it,
   * and downloading it would undo the point of this surface. */
  /* Any symbol, from any route.
   *
   * openStock needed SCREEN, and SCREEN was only fetched by the Screen route —
   * so a name on Today, Markets, Ideas or a sector drill-down looked clickable
   * and did nothing. That is why COFORGE, QPOWER and ATHERENERG would not
   * open. The universe is now fetched on first use from wherever the reader
   * is, and cached for the session. */
  async function openStock(sym) {
    if (!SCREEN) {
      sheet(esc(sym), `<div class="sk" style="height:210px"></div>
        <p class="hint">Loading the screen — about 260 KB, once per session.</p>`);
      const r0 = await get('/screen.json');
      if (!r0.ok) { sheet(esc(sym), fail('The company card', r0.error)); return; }
      SCREEN = (r0.data.rows || []).filter(x => x && x.sym);
    }
    const r = (SCREEN || []).find(x => x.sym === sym);
    if (!r) {
      sheet(esc(sym), `<div class="empty">${esc(sym)} is not in the 750-name screen,
        so there is no card for it — it may be an index, a commodity, or a name
        outside the screened universe.</div>`);
      return;
    }
    const bar = (v, max = 100) => `<span class="mini-bar"><i style="width:${Math.max(0, Math.min(100, (v ?? 0) / max * 100)).toFixed(0)}%"></i></span>`;
    const score = (k, l) => r[k] == null ? '' : `<div class="sc-t">
      <span class="sc-l">${esc(l)}</span><span class="sc-v">${Number(r[k]).toFixed(1)}</span>${bar(r[k])}</div>`;
    const why = [];
    for (const t of (r.setup?.tags || [])) {
      if (/52W BREAKOUT/.test(t)) why.push(['Broke to a 52-week high', 'close above the prior 52-week range']);
      else if (/50D BREAKOUT/.test(t)) why.push(['Broke its 50-day high', 'close above the prior 50-day range']);
      else if (/20D BREAKOUT/.test(t)) why.push(['Broke its 20-day high', 'close above the prior 20-day range']);
      else if (t === 'RS LEADER') why.push(['Leads the market on relative strength', 'outperforming the index over 3 and 12 months']);
      else if (t === 'TREND INTACT') why.push(['Trend intact', 'price above the 50-day, 50 above the 200']);
      else if (t === 'OVERSOLD') why.push(['Oversold', `RSI ${r.rsi != null ? Math.round(r.rsi) : '—'}`]);
      else if (t === 'VOLUME') why.push(['Trading above its own average volume', `${(r.vol_spike ?? 0).toFixed(1)}x its average`]);
    }
    if ((r.roce ?? 0) >= 20) why.push([`Earns ${Math.round(r.roce)}% on capital employed`, 'ROCE, invested capital']);
    if ((r.de ?? 9) <= 0.1) why.push(['Effectively debt-free', `D/E ${r.de}`]);
    const flags = (r.risk?.flags || []);
    const yoy = (l, v, unit = '%') => v == null ? '' :
      `<div class="yy"><span>${esc(l)}</span><b class="${dir(v)}">${v > 0 ? '+' : ''}${Number(v).toFixed(1)}${unit}</b></div>`;

    sheet(`${esc(r.sym)} <small>${esc(r.name || '')}</small>`, `
      <p class="hint" style="margin:0 0 12px">₹${esc(r.price)} · ${esc(r.ind || r.sector || '')} ·
        ₹${r.mcap_cr != null ? Math.round(r.mcap_cr).toLocaleString('en-IN') : '—'} cr ·
        accounts to ${esc(r.fy || '—')}</p>
      <div class="tags">${(r.setup?.tags || []).map(t => `<span class="pill pill-ac">${esc(t)}</span>`).join('')}
        ${r.risk?.level ? `<span class="pill ${r.risk.level === 'LOW' ? 'pill-up' : r.risk.level === 'HIGH' ? 'pill-dn' : 'pill-wn'}">RISK ${esc(r.risk.level)}</span>` : ''}</div>
      <div class="scores">
        ${score('comp', 'Composite')}${score('q', 'Quality')}${score('g', 'Growth')}
        ${score('em', 'Earnings mom.')}${score('cf', 'Cash flow')}${score('v', 'Value')}${score('tech', 'Technical')}
      </div>
      <div class="two">
        <div class="pane pane-ok"><h4>Why now</h4>${why.length ? why.map(([t, k]) =>
          `<div class="read read-for"><b>${esc(t)}</b><span>${esc(k)}</span></div>`).join('')
          : '<p class="hint">No setup is firing on this name today.</p>'}</div>
        <div class="pane pane-risk"><h4>What can go wrong</h4>${flags.length ? flags.map(f =>
          `<div class="read read-against"><b>${esc(f.t)}</b><span>${esc(f.k || '')}</span></div>`).join('')
          : '<p class="hint">No flags raised by the risk screen.</p>'}</div>
      </div>
      <h4 class="sh">Latest year on year</h4>
      <div class="yoy">${yoy('Revenue', r.rev_yoy)}${yoy('EBITDA', r.ebitda_yoy)}${yoy('Profit', r.pat_yoy)}
        ${yoy('EPS', r.eps_yoy)}${yoy('EBIT margin', r.margin_delta, 'pt')}</div>
      <h4 class="sh">Cash quality</h4>
      <div class="yoy">${yoy('Cash conversion (CFO/PAT)', r.cfo_pat, 'x')}${yoy('Free cash / profit', r.fcf_pat, 'x')}
        ${yoy('ROCE', r.roce)}${yoy('Debt / equity', r.de, '')}</div>
      <h4 class="sh">Where price sits</h4>
      <div class="yoy">${yoy('vs 50-day', r.sma50 ? (r.price - r.sma50) / r.sma50 * 100 : null)}
        ${yoy('vs 200-day', r.sma200 ? (r.price - r.sma200) / r.sma200 * 100 : null)}
        ${yoy('From 52w high', r.from_high)}${yoy('RSI', r.rsi, '')}</div>
      <div class="card-foot" style="margin-top:14px">${symLinks(r.sym)}</div>`);

    /* One live quote, for the name actually being read. Marking all 750 is not
     * possible — Yahoo takes 20 symbols a call — and marking the 60 on screen
     * would re-fetch on every keystroke. The card is where the price matters,
     * and it is one symbol. */
    quotes([r.sym]).then(q => {
      const live = q[r.sym];
      if (!live) return;
      const host = document.querySelector('#sheet .hint');
      if (!host) return;
      const mv = r.price ? (live.price - r.price) / r.price * 100 : null;
      host.insertAdjacentHTML('afterend',
        `<div class="livebox"><span class="lv-k">Live</span>
          <b class="lv-p">₹${live.price.toFixed(2)}</b>
          ${live.change_pct != null ? `<span class="lv-c ${dir(live.change_pct)}">${pct(live.change_pct)} today</span>` : ''}
          ${mv != null ? `<span class="lv-s">${pct(mv)} vs the ${esc(String(r.last_date || 'screen'))} close</span>` : ''}
        </div>`);
    });
  }

  let sigFilter = 'all';
  R['/signals'] = async () => {
    const intro = 'Every alert this site has sent since it launched, with the levels it was sent at. Scored when it closes — losers included, which is the point of publishing it.';
    paint(head('Signals', intro) + skel('sk-card', 4));
    const a = await get('/alerts.json');
    const base = head('Signals', intro);
    if (!a.ok) { paint(base + fail('The signal ledger', a.error)); return; }
    /* DAY ONE IS TODAY.
     *
     * This surface starts its record from launch. Everything before it was
     * published under a different engine configuration and a ledger that has
     * been re-graded twice, and carrying that history here would mean showing
     * a win rate this site never produced.
     *
     * Nothing is deleted. alerts.json is untouched and news.askakshay.com
     * still carries all 200 signals and the full performance record — this is
     * a filter on what THIS page counts, not a rewrite of the ledger.
     *
     * Anything sent on or after LAUNCH counts. When there is nothing yet, the
     * page says so rather than showing an empty table that reads as a fault.
     */
    const LAUNCH = '2026-08-29';
    const dayOf = r => String(r.alert_date || r.date || '').slice(0, 10);
    const every = Array.isArray(a.data) ? a.data : (a.data.rows || []);
    const all = every.filter(r => dayOf(r) >= LAUNCH);

    // Filter on the row's OWN badge, not on arithmetic over pnl_pct.
    // `Number(null) <= 0` is true, so the first version put all 138 open
    // signals under "Losers" — the ledger already labels every row, and
    // re-deriving a label that exists is how you invent a different one.
    const isOpen = r => (r.badge || '').toLowerCase() === 'open';
    const closed = all.filter(r => !isOpen(r) && r.pnl_pct != null);
    const wins = all.filter(r => (r.badge || '').toLowerCase() === 'win').length;
    const losses = all.filter(r => (r.badge || '').toLowerCase() === 'loss').length;
    const opens = all.filter(isOpen);

    // One request for every open signal's mark, not one per card.
    const px = await quotes(opens.map(r => r.symbol));

    const chips = [['all', `All ${all.length}`], ['open', `Open ${opens.length}`],
                   ['win', `Winners ${wins}`], ['loss', `Losers ${losses}`],
                   ['expired', 'Expired']];

    const card = r => {
      const b = (r.badge || '').toLowerCase();
      const cur = r.currency || '₹';
      const live = px[r.symbol];
      const open = isOpen(r);
      const unreal = open && live ? pnlOf(r.entry, live.price, r.action) : null;
      const shown = open ? unreal : (r.pnl_pct == null ? null : Number(r.pnl_pct));
      const pill = shown == null ? `<span class="pill pill-ac">open</span>`
        : `<span class="pill ${shown > 0 ? 'pill-up' : 'pill-dn'}">${pct(shown)}${open ? ' live' : ''}</span>`;
      return `<article class="card" data-sym="${esc(r.symbol || '')}" role="button" tabindex="0">
        <div class="card-h">
          <span class="sym">${esc(r.symbol || '')}</span>
          ${r.action ? `<span class="pill ${/SELL|SHORT/i.test(r.action) ? 'pill-dn' : 'pill-up'}">${esc(r.action)}</span>` : ''}
          ${r.signal_type ? `<span class="pill">${esc(String(r.signal_type).replace(/_/g, ' '))}</span>` : ''}
          ${r.timeframe ? `<span class="pill">${esc(r.timeframe)}</span>` : ''}
          <span class="spacer"></span>${pill}
        </div>
        <div class="kv">
          <div><span class="kk">Entry</span><span class="vv">${cur}${esc(r.entry ?? '—')}</span></div>
          <div><span class="kk">${open ? 'Last' : 'Exit'}</span><span class="vv">${
            open ? (live ? cur + live.price.toFixed(2) : '<span style="color:var(--dim)">no mark</span>')
                 : cur + esc(r.exit_price ?? '—')}</span></div>
          <div><span class="kk">Stop</span><span class="vv dn">${cur}${esc(r.sl ?? '—')}</span></div>
          <div><span class="kk">Target 1</span><span class="vv up">${cur}${esc(r.target1 ?? '—')}</span></div>
          <div><span class="kk">Target 2</span><span class="vv up">${cur}${esc(r.target2 ?? '—')}</span></div>
          <div><span class="kk">R:R</span><span class="vv">${esc(r.rr ?? '—')}</span></div>
        </div>
        ${open && live && isFinite(Number(r.sl)) && isFinite(Number(r.target1))
          ? progressToTarget(Number(r.entry), Number(r.sl), Number(r.target1), live.price, r.action) : ''}
        ${open ? trailPlan(r.entry, r.sl, r.target1, r.target2, r.action) : ''}
        <div class="card-foot">
          <span class="mono" style="font-size:11px;color:var(--dim)">${esc(String(r.alert_date || r.date || '').slice(0, 10))}
            ${r.status ? ' · ' + esc(String(r.status).replace(/_/g, ' ').toLowerCase()) : ''}</span>
          ${symLinks(r.symbol, r.tv)}
        </div>
        ${r.remarks ? `<div class="card-body">${esc(String(r.remarks).slice(0, 180))}</div>` : ''}
      </article>`;
    };

    const draw = () => {
      const rows = all.filter(r => {
        const b = (r.badge || '').toLowerCase();
        return sigFilter === 'all' ? true : b === sigFilter;
      }).slice(0, 30);
      if (!all.length) {
        main.innerHTML = base + `<div class="note">
          <b>No signals yet — the record starts today.</b> This page counts only what the
          engine sends from <b>${esc(LAUNCH)}</b> onward, so its win rate is earned here
          rather than inherited. The scanner publishes at 10:30 and 16:30 IST on weekdays;
          the first entries will appear after those runs.
          <br><br>The full history of ${every.length} earlier signals is unchanged and still
          published on <a href="/" style="color:var(--accent)">the broadsheet edition</a>.
        </div>`;
        return;
      }
      main.innerHTML = base +
        sec('The record', `<div class="grid">
          ${tile(all.length, 'Signals published', 'since ' + esc(LAUNCH), 'ac')}
          ${tile(opens.length, 'Still open', 'marked to live prices')}
          ${tile(closed.length ? Math.round(wins / (wins + losses) * 100) + '%' : '—', 'Win rate',
                 `${wins}W / ${losses}L closed`, (wins + losses) && wins / (wins + losses) >= .5 ? 'up' : 'dn')}
          ${tile(closed.length, 'Closed and scored', 'expiries counted as losses')}
        </div>`) +
        `<div class="chips" role="group" aria-label="Signal filter">${chips.map(([k, l]) =>
          `<button type="button" class="chip" data-s="${k}" aria-pressed="${sigFilter === k}">${esc(l)}</button>`).join('')}</div>` +
        sec('Alerts', rows.length ? rows.map(card).join('')
          : `<div class="empty">No signals with that state.</div>`, `${rows.length} shown`);
      main.querySelectorAll('.chip').forEach(b => b.addEventListener('click', () => {
        sigFilter = b.dataset.s; draw();
      }));
    };
    draw();
  };

  /* THE TRAILING RULE, ON EVERY TRADE.
   *
   * The engines write entry, stop and targets. None of them writes a trailing
   * level, so this DERIVES the management rule from the levels that were
   * actually sent — it invents no price the signal did not carry.
   *
   * It is shown as a RULE, and it deliberately does NOT re-grade anything.
   * exit_rule_study.py simulated this over 470 closed trades on the same bars,
   * every trade subject to the rule in both directions:
   *
   *     baseline            +0.194R
   *     break-even @ 1.0R   +0.166R   <- worse
   *     break-even @ 0.5R   +0.220R   (+0.026R, about a quarter of one SE)
   *
   * Trailing does not pay on this ledger: the same volatility that carries a
   * trade to +1R carries winners back through entry and scratches them. So the
   * ledger stays scored on the stop the signal was sent with, and this is
   * published as what to DO with an open position — not as an edge, and not as
   * a silent rewrite of the record.
   */
  const trailPlan = (entry, sl, t1, t2, action) => {
    const e = Number(entry), s0 = Number(sl), a = Number(t1), b = Number(t2);
    if (![e, s0, a].every(Number.isFinite)) return '';
    const short = /SELL|SHORT/i.test(String(action || ''));
    const f = v => '₹' + Number(v).toFixed(2);
    const steps = [
      [`Stop stays at ${f(s0)}`, 'until the first target prints'],
      [`After T1, trail to ${f(e)}`, 'break-even — never back below it'],
    ];
    if (Number.isFinite(b)) steps.push([`After T2, trail to ${f(a)}`, 'locking the first target in']);
    return `<div class="trail"><span>Trailing rule</span>
      ${steps.map(([t, k]) => `<div class="tr-s"><b>${esc(t)}</b><i>${esc(k)}</i></div>`).join('')}
      <div class="tr-n">Published as a management rule. The ledger is still scored on the
        stop the signal was sent with — a break-even trail measured
        <b>worse</b> than the fixed stop over 470 closed trades.</div>
    </div>`;
  };

  /* Where price sits between the stop and the first target. The number says
   * how far; the bar says how far RELATIVE TO THE RISK TAKEN, which is the
   * thing a stop and a target exist to frame. */
  const progressToTarget = (entry, sl, t1, last, action) => {
    const lo = Math.min(sl, t1), hi = Math.max(sl, t1);
    if (!(hi > lo)) return '';
    const at = Math.max(0, Math.min(100, (last - lo) / (hi - lo) * 100));
    const entryAt = Math.max(0, Math.min(100, (entry - lo) / (hi - lo) * 100));
    return `<div class="prog" title="Stop ${sl} · entry ${entry} · target ${t1}">
      <span class="prog-bar"><i style="width:${at.toFixed(1)}%"></i>
        <em style="left:${entryAt.toFixed(1)}%"></em></span>
      <span class="prog-l"><span>stop</span><span>entry</span><span>target</span></span>
    </div>`;
  };

  /* ── router ────────────────────────────────────────────────────────────── */
  const routeOf = () => {
    const h = (location.hash || '#/').replace(/^#/, '');
    return R[h] ? h : '/';
  };

  async function render() {
    const path = routeOf();
    document.querySelectorAll('.tabs a').forEach(a =>
      a.dataset.route === path ? a.setAttribute('aria-current', 'page') : a.removeAttribute('aria-current'));
    // Scroll first, then paint: painting first lets the old route's height
    // hold the scroll position and the new route lands mid-page.
    window.scrollTo(0, 0);
    try { await R[path](); } catch (err) {
      paint(fail('This section', err && err.message ? err.message : 'unexpected error'));
    }
  }

  window.addEventListener('hashchange', render);

  /* LIVE, AROUND THE CLOCK.
   *
   * The page was a snapshot: whatever the data said when you opened it, until
   * you reloaded. A market page left open on a second monitor should not go
   * quietly stale.
   *
   * setInterval, not requestAnimationFrame — rAF does not run in a hidden tab
   * or on a phone with the screen off, which is exactly when a page is left
   * open. The same trap is documented in app.js on the broadsheet.
   *
   * Sixty seconds while visible. When the tab is hidden the timer keeps
   * running but the refresh is SKIPPED, and one runs immediately on return —
   * so a tab left open overnight makes ~0 requests and is current the moment
   * it is looked at again, instead of replaying eight hours of them.
   */
  let lastRefresh = Date.now();
  async function refresh(force) {
    if (!force && document.visibilityState === 'hidden') return;
    if (document.getElementById('sheet')?.open) return;   // never yank an open card
    lastRefresh = Date.now();
    try { await R[routeOf()](); } catch (e) { /* a failed refresh keeps what is on screen */ }
  }
  setInterval(() => refresh(false), 60000);
  document.addEventListener('visibilitychange', () => {
    // Back on screen after more than a minute away: refresh at once.
    if (document.visibilityState === 'visible' && Date.now() - lastRefresh > 60000) refresh(true);
  });

  /* ── theme ─────────────────────────────────────────────────────────────── */
  const root = document.documentElement;
  const saved = (() => { try { return localStorage.getItem('sig:theme'); } catch (e) { return null; } })();
  if (saved) root.setAttribute('data-theme', saved);
  document.getElementById('themeBtn').addEventListener('click', () => {
    const next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    document.querySelector('meta[name="theme-color"]').setAttribute('content', next === 'dark' ? '#0B0F14' : '#F7F8FA');
    try { localStorage.setItem('sig:theme', next); } catch (e) { /* private mode */ }
  });

  /* ── live clock, in MYT ────────────────────────────────────────────────
   * The header carried the EDITION date, which is the day the paper was
   * built — correct, and read as "the site is a day stale" every morning
   * between midnight MYT and the 6 AM build. A running clock says the page
   * is alive; the edition date moves to where it belongs, beside the data
   * that actually carries it. */
  const clockEl = document.getElementById('edition');
  function tickClock() {
    if (!clockEl) return;
    const t = new Date().toLocaleTimeString('en-GB', {
      timeZone: 'Asia/Kuala_Lumpur', hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const d = new Date().toLocaleDateString('en-GB', {
      timeZone: 'Asia/Kuala_Lumpur', day: '2-digit', month: 'short' });
    clockEl.innerHTML = `<span class="clk-d">${d}</span><span class="clk-t">${t}</span><span class="clk-z">MYT</span>`;
  }
  tickClock();
  setInterval(tickClock, 1000);

  /* ── edition stamp and data health ─────────────────────────────────────── */
  get('/edition.json').then(r => {
    if (r.ok && r.data && r.data.build_date) {
      document.getElementById('edition').textContent = r.data.build_date;
    }
  });

  render();
})();
