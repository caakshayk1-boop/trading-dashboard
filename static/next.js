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

  /* ── routes ────────────────────────────────────────────────────────────── */
  const R = {};

  R['/'] = async () => {
    paint(head('Today', 'The whole day in one screen. Everything below is one tap away.') +
      sec('State of things', `<div class="grid">${skel('sk-tile', 4)}</div>`) +
      sec('The strongest idea', skel('sk-card', 1)) +
      sec('Last engine change', skel('sk-card', 1)));

    const [t, h] = await Promise.all([get('/today.json'), get('/api/health')]);
    let out = head('Today', 'The whole day in one screen. Everything below is one tap away.');

    if (!t.ok) { paint(out + fail('Today', t.error)); return; }
    const d = t.data, dh = d.data_health || {};
    const hv = h.ok ? h.data : null;

    out += (t.stale ? staleNote(t.age) : '');
    out += sec('State of things',
      `<div class="grid">
        ${tile(`${dh.current ?? '—'}<span style="color:var(--dim)">/${dh.total ?? '—'}</span>`, 'Datasets current',
          dh.worst && dh.worst !== 'LIVE' ? `worst: ${esc(String(dh.worst).toLowerCase())}` : 'all feeds reporting',
          dh.current === dh.total ? 'up' : 'wn')}
        ${tile(hv ? hv.signals : '—', 'Signals logged', hv ? `${hv.open_setups ?? 0} open setups` : 'ledger unreachable', 'ac')}
        ${tile((d.picks || []).length, 'Ideas this week', 'ranked once per ISO week')}
        ${(() => { const s = (d.ipos && d.ipos.summary) || {};
            return tile(d.ipos ? d.ipos.count : 0, 'Listings tracked',
              s.up != null ? `${s.up} of ${s.count} above issue · median ${pct(s.median_pct)}` : 'mainboard only',
              s.up_pct != null ? (s.up_pct >= 50 ? 'up' : 'dn') : ''); })()}
      </div>`);

    const p = (d.picks || [])[0];
    out += sec('The strongest idea', p ? ideaCard(p, true) : `<div class="empty">Nothing clears the bar this week.</div>`);

    const e = (d.engine || [])[0];
    out += sec('Last engine change', e ? `<article class="card">
        <div class="card-h"><span class="pill pill-ac">${esc(e.tag || 'LOG')}</span>
          <span class="spacer"></span><span class="mono" style="font-size:11px;color:var(--dim)">${esc(e.date || '')}</span></div>
        <div class="sym" style="font-family:var(--ui);font-size:15px;font-weight:600">${esc(e.title || '')}</div>
        <div class="card-body">${esc(String(e.body || '').slice(0, 260))}${String(e.body || '').length > 260 ? '…' : ''}</div>
      </article>` : `<div class="empty">No entries yet.</div>`);

    paint(out);
  };

  const ideaCard = (p, lead) => {
    const cur = p.currency || '₹';
    return `<article class="card">
      <div class="card-h">
        <span class="sym">${esc(p.symbol || '')}</span>
        ${p.score != null ? `<span class="pill pill-ac">${esc(p.score)}/100</span>` : ''}
        <span class="spacer"></span>
        <span class="num ${dir(p.change_1d)}" style="font-size:13px">${pct(p.change_1d)}</span>
      </div>
      ${lead && p.target_basis ? `<div class="card-body">Target is ${esc(p.target_basis)}; the stop is ${esc(p.stop_basis || 'below the trend')}. ${esc(p.horizon_basis || '')}</div>` : ''}
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

  R['/markets'] = async () => {
    paint(head('Markets', 'The board, live. Prices refresh from the exchange feed, not from the morning build.') +
      sec('Breadth', `<div class="grid">${skel('sk-tile', 2)}</div>`) +
      sec('The board', `<div class="board">${skel('sk-row', 8)}</div>`));

    const m = await get('/api/markets');
    let out = head('Markets', 'The board, live. Prices refresh from the exchange feed, not from the morning build.');
    if (!m.ok) { paint(out + fail('The market board', m.error)); return; }
    const d = m.data, rows = d.markets || [];
    const adv = d.advancing ?? 0, tot = d.total ?? rows.length;

    out += (m.stale ? staleNote(m.age) : '');
    out += sec('Breadth', `<div class="grid">
        ${tile(`${adv}<span style="color:var(--dim)">/${tot}</span>`, 'Advancing', 'instruments up on the day', adv * 2 >= tot ? 'up' : 'dn')}
        ${tile(d.live ? 'LIVE' : 'CLOSED', 'Feed', d.fetched_at ? 'as of ' + esc(String(d.fetched_at).slice(11, 16)) + ' UTC' : '', d.live ? 'up' : '')}
      </div>`);

    out += sec('The board', rows.length ? `<div class="board">${rows.map(r => `
        <div class="board-row">
          <span class="n">${esc(r.name || r.symbol || '')}</span>
          <span class="p">${esc(r.price ?? '—')}</span>
          <span class="c ${dir(r.change_pct)}">${pct(r.change_pct)}</span>
        </div>`).join('')}</div>` : `<div class="empty">The board is empty right now.</div>`, `${rows.length} instruments`);
    paint(out);
  };

  R['/research'] = async () => {
    paint(head('Research', 'Ranked ideas and open books. These are ideas, not ledger signals — they never touch the win rate.') +
      sec('Trade ideas', skel('sk-card', 3)));
    const t = await get('/today.json');
    let out = head('Research', 'Ranked ideas and open books. These are ideas, not ledger signals — they never touch the win rate.');
    if (!t.ok) { paint(out + fail('Research', t.error)); return; }
    const d = t.data, picks = d.picks || [], ipos = d.ipos || {};
    out += (t.stale ? staleNote(t.age) : '');
    out += sec('Trade ideas', picks.length ? picks.map(p => ideaCard(p, false)).join('') :
      `<div class="empty">Nothing clears the bar this week. That is a result, not a gap.</div>`, `${picks.length} ranked`);

    // Sorted by performance since listing, worst last — a listings board that
    // opens on its winners and hides its losers is a brochure.
    const rows = (ipos.rows || []).slice().sort((a, b) =>
      (b.since_listing_pct ?? -1e9) - (a.since_listing_pct ?? -1e9));
    const sm = ipos.summary || {};
    out += sec('New listings',
      (sm.count ? `<div class="grid" style="margin-bottom:10px">
        ${tile(`${sm.up ?? 0}<span style="color:var(--dim)">/${sm.count}</span>`, 'Above issue',
               sm.up_pct != null ? pct(sm.up_pct).replace('+', '') + ' of the cohort' : '',
               (sm.up_pct ?? 0) >= 50 ? 'up' : 'dn')}
        ${tile(pct(sm.median_pct), 'Median return', 'since listing', dir(sm.median_pct))}
      </div>` : '') +
      (rows.length ? `<div class="board">${rows.map(r => `
        <div class="board-row">
          <span class="n">${esc(r.sym || '')}<br><span style="font-size:11px;color:var(--dim)">listed ${esc(r.listed_on || '—')}</span></span>
          <span class="p" style="font-size:12px;color:var(--muted)">${esc(r.last_close ?? '—')}</span>
          <span class="c ${dir(r.since_listing_pct)}">${pct(r.since_listing_pct)}</span>
        </div>`).join('')}</div>` : `<div class="empty">No listings in the window.</div>`),
      ipos.count ? `${ipos.count} in ${esc(ipos.months || '')} months` : '');
    paint(out);
  };

  R['/portfolio'] = async () => {
    paint(head('The book', 'Orders to place, and what the same capital already holds.') +
      sec('Mandate', `<div class="grid">${skel('sk-tile', 4)}</div>`) +
      sec('Orders to place', skel('sk-card', 3)));

    const [mn, w] = await Promise.all([get('/mandate.json'), get('/api/signals?wallet=1')]);
    let out = head('The book', 'Orders to place, and what the same capital already holds.');
    if (!mn.ok && !w.ok) { paint(out + fail('The book', mn.error || w.error)); return; }

    if (mn.ok) {
      const d = mn.data, st = d.state || {};
      out += (mn.stale ? staleNote(mn.age) : '');
      out += sec('Mandate', `<div class="grid">
          ${tile(money(d.capital), 'Capital', 'Indian listed equity, no intraday', 'ac')}
          ${tile((d.admitted || []).length, 'Orders to place', 'nothing here is bought yet')}
          ${tile(st.deployed_pct != null ? st.deployed_pct + '%' : '—', 'Deployed', st.cash != null ? money(st.cash) + ' in cash' : '')}
          ${tile(st.heat_pct != null ? st.heat_pct + '%' : '—', 'Heat', 'risk if every stop hits', Number(st.heat_pct) > 6 ? 'wn' : '')}
        </div>`);

      const orders = d.admitted || [];
      out += sec('Orders to place', orders.length ? orders.map(o => `
        <article class="card">
          <div class="card-h"><span class="sym">${esc(o.symbol || '')}</span>
            ${o.engine ? `<span class="pill">${esc(o.engine)}</span>` : ''}
            <span class="spacer"></span>
            ${o.rr ? `<span class="pill pill-up">${esc(o.rr)}:1</span>` : ''}</div>
          <div class="kv">
            <div><span class="kk">Buy</span><span class="vv">${esc(o.qty ?? '—')} @ ${esc(o.entry ?? '—')}</span></div>
            <div><span class="kk">Stop</span><span class="vv dn">${esc(o.stop ?? '—')}</span></div>
            <div><span class="kk">Size</span><span class="vv">${money(o.notional)}</span></div>
            <div><span class="kk">Risk</span><span class="vv">${money(o.risk_amount)}</span></div>
          </div>
        </article>`).join('') : `<div class="empty">No orders clear the mandate today.</div>`, `${orders.length} to place`);
    } else {
      out += fail('The mandate', mn.error);
    }

    if (w.ok && w.data && w.data.wallet) {
      const wl = w.data.wallet;
      out += sec('Positions held — marked live', `<div class="grid">
          ${tile(money(wl.realized_pnl), 'Realised P&L', `${wl.closed_trades ?? 0} closed`, dir(wl.realized_pnl))}
          ${tile(money(wl.unrealized_pnl), 'Unrealised', `${wl.marked ?? 0}/${(wl.marked ?? 0) + (wl.unmarked ?? 0)} marked`, dir(wl.unrealized_pnl))}
          ${tile(money(wl.total_pnl), 'Total P&L', wl.total_pnl_pct != null ? pct(wl.total_pnl_pct) + ' of capital' : '', dir(wl.total_pnl))}
          ${tile(wl.win_rate != null ? wl.win_rate + '%' : '—', 'Win rate', `${wl.wins ?? 0}W / ${wl.losses ?? 0}L`)}
        </div>`);
    } else {
      out += sec('Positions held', fail('The paper wallet', (w && w.error) || 'unavailable'));
    }
    paint(out);
  };

  R['/ledger'] = async () => {
    paint(head('Ledger', 'Every signal is logged when it fires and scored when it closes. Losers included — that is the point of publishing it.') +
      sec('The record', `<div class="grid">${skel('sk-tile', 4)}</div>`) +
      sec('Engine log', skel('sk-card', 3)));

    const [h, t] = await Promise.all([get('/api/health'), get('/today.json')]);
    let out = head('Ledger', 'Every signal is logged when it fires and scored when it closes. Losers included — that is the point of publishing it.');

    if (h.ok) {
      const d = h.data;
      out += (h.stale ? staleNote(h.age) : '');
      out += sec('The record', `<div class="grid">
          ${tile(d.signals ?? '—', 'Signals logged', 'since the ledger opened', 'ac')}
          ${tile(d.open_setups ?? '—', 'Open setups', 'waiting for a trigger')}
          ${tile(d.open_positions ?? '—', 'Open positions', 'currently tracked')}
          ${tile(d.tracked_positions ?? '—', 'Tracked total', d.latest_signal_date ? 'latest ' + esc(d.latest_signal_date) : '')}
        </div>`);
    } else { out += fail('The ledger', h.error); }

    if (t.ok) {
      const log = (t.data.engine || []);
      out += sec('Engine log', log.length ? log.map(e => `
        <article class="card">
          <div class="card-h"><span class="pill pill-ac">${esc(e.tag || 'LOG')}</span>
            ${e.verdict ? `<span class="pill">${esc(e.verdict)}</span>` : ''}
            <span class="spacer"></span>
            <span class="mono" style="font-size:11px;color:var(--dim)">${esc(e.date || '')}</span></div>
          <div class="sym" style="font-family:var(--ui);font-size:14.5px;font-weight:600">${esc(e.title || '')}</div>
          <div class="card-body">${esc(String(e.body || '').slice(0, 220))}${String(e.body || '').length > 220 ? '…' : ''}</div>
        </article>`).join('') : `<div class="empty">No entries.</div>`, `${log.length} entries`);
    }
    paint(out);
  };

  R['/method'] = async () => {
    paint(head('Method', 'Where every number comes from, and how fresh it is. A dataset that is late says so here before it reaches a page.') +
      sec('Data health', `<div class="board">${skel('sk-row', 12)}</div>`));
    const dh = await get('/data-health.json');
    let out = head('Method', 'Where every number comes from, and how fresh it is. A dataset that is late says so here before it reaches a page.');
    if (!dh.ok) { paint(out + fail('Data health', dh.error)); return; }
    const d = dh.data, sets = d.datasets || [];
    out += (dh.stale ? staleNote(dh.age) : '');
    out += sec('Coverage', `<div class="grid">
        ${tile(`${d.current ?? '—'}<span style="color:var(--dim)">/${d.total ?? '—'}</span>`, 'Datasets current', '', d.current === d.total ? 'up' : 'wn')}
        ${tile(esc(String(d.worst || '—').toLowerCase()), 'Worst state', `${d.degraded ?? 0} degraded`, d.degraded ? 'wn' : 'up')}
      </div>`);

    out += sec('Every feed', `<div class="board">${sets.map(s => {
      const within = s.freshness_age_hours != null && s.expected_refresh_hours
        ? s.freshness_age_hours <= s.expected_refresh_hours : null;
      const cls = s.status === 'DEGRADED' || s.status === 'FAILED' ? 'dn' : within === false ? 'wn' : 'up';
      return `<div class="board-row">
          <span class="n">${esc(s.dataset || '')}<br><span style="font-size:11px;color:var(--dim)">${esc(s.source || '')}</span></span>
          <span class="p" style="font-size:11.5px;color:var(--muted)">${esc(s.freshness_age || '')}<br><span style="color:var(--dim)">${esc(s.expected_refresh || '')}</span></span>
          <span class="c ${cls}">${esc(s.status || '')}</span>
        </div>`; }).join('')}</div>`, `${sets.length} feeds`);
    paint(out);
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

  /* ── edition stamp ─────────────────────────────────────────────────────── */
  get('/edition.json').then(r => {
    if (r.ok && r.data && r.data.build_date) {
      document.getElementById('edition').textContent = r.data.build_date;
    }
  });

  render();
})();
