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
  /* Every route repaints <main> wholesale, so listeners bound to nodes inside
   * it die with them. Listeners bound to WINDOW or DOCUMENT do not — the brief
   * binds a scroll handler and a keyboard handler that would otherwise stack
   * up one copy per repaint, and the page repaints itself every 60 seconds.
   * paint() fires a teardown first so those can remove themselves. */
  const paint = html => {
    main.dispatchEvent(new CustomEvent('sig:teardown'));
    main.innerHTML = html;
  };

  // A mono eyebrow, a serif headline, one line of standfirst — the brief's
  // masthead rhythm, now the rhythm of every route. `eyebrow` defaults to the
  // product name so a route that says nothing still gets the structure.
  const head = (title, sub, eyebrow) =>
    `<div class="route-h"><span class="eyebrow">${esc(eyebrow || 'Signal')}</span>
      <h1>${esc(title)}</h1>${sub ? `<p>${esc(sub)}</p>` : ''}</div>`;
  // `lead` is the serif line under the label: the label says what the block
  // IS, the lead says what it MEANS. Blocks with nothing to add omit it.
  const sec = (label, body, n, lead) =>
    `<section class="sec"><div class="sec-h"><h2>${esc(label)}</h2>${n ? `<span class="sec-n">${esc(n)}</span>` : ''}</div>${lead ? `<p class="sec-lead">${esc(lead)}</p>` : ''}${body}</section>`;
  const tile = (v, k, sub, cls) =>
    `<div class="tile"><div class="v ${cls || ''}">${v}</div>${sub ? `<div class="sub">${sub}</div>` : ''}<div class="k">${esc(k)}</div></div>`;

  /* ── ANIMATED NUMBER ─────────────────────────────────────────────────────
   * One implementation, used everywhere a figure is worth watching arrive.
   *
   * Rules it keeps: it animates only when the value MEANINGFULLY changes, it
   * preserves the caller's decimal precision, it uses tabular numerals so the
   * element never changes width mid-count, and it does nothing at all under
   * prefers-reduced-motion — the final value is written on the first frame.
   *
   * rAF is used because this is genuinely frame-driven, and it is safe here
   * for the same reason it is unsafe elsewhere on this site: it drives a
   * one-shot count that is allowed not to run in a hidden tab, rather than a
   * layout the page depends on. If the tab is hidden the value is simply
   * already correct.
   */
  const REDUCED = window.matchMedia && window.matchMedia('(prefers-reduced-motion:reduce)').matches;
  function countTo(el, to, opts) {
    const o = opts || {};
    const dp = o.dp == null ? 0 : o.dp;
    const pre = o.pre || '', post = o.post || '';
    const write = v => { el.textContent = pre + v.toFixed(dp) + post; };
    const from = Number(el.dataset.cv);
    if (!Number.isFinite(to)) { el.textContent = o.blank || '—'; delete el.dataset.cv; return; }
    el.dataset.cv = String(to);
    // "Meaningfully" = different at the precision actually displayed.
    if (REDUCED || !Number.isFinite(from) || from.toFixed(dp) === to.toFixed(dp)) { write(to); return; }
    const t0 = performance.now(), ms = Math.min(900, 260 + Math.abs(to - from) * 6);
    let done = false;
    const step = now => {
      const k = Math.min(1, (now - t0) / ms);
      write(from + (to - from) * (1 - Math.pow(1 - k, 3)));   // easeOutCubic
      if (k < 1) requestAnimationFrame(step); else done = true;
    };
    requestAnimationFrame(step);
    /* rAF DOES NOT RUN IN A HIDDEN TAB — not in the preview pane, and not in a
     * real reader's background tab either. Without this guard a page opened in
     * the background renders every animated figure stuck on its start value,
     * so the confidence score reads 0 and the loss reads nothing. The timer
     * keeps running where rAF does not; if the chain never finished, the final
     * value is written outright. */
    setTimeout(() => { if (!done) write(to); }, ms + 140);
  }

  /* ── SPARKLINE ───────────────────────────────────────────────────────────
   * One <path> from real daily closes. A series that is missing, too short or
   * genuinely flat draws NOTHING — a flat line reads as "this market did not
   * move", which is a different claim from "this was not measured".
   */
  const sparkline = (series, w, h) => {
    if (!Array.isArray(series) || series.length < 3) return '';
    const W = w || 96, H = h || 26;
    const lo = Math.min.apply(null, series), hi = Math.max.apply(null, series);
    const span = hi - lo;
    if (!(span > 0)) return '';
    const n = series.length, pad = 2;
    const X = i => (i / (n - 1)) * W;
    const Y = v => pad + (1 - (v - lo) / span) * (H - pad * 2);
    const d = series.map((v, i) => (i ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Y(v).toFixed(1)).join(' ');
    const cls = series[n - 1] > series[0] ? 'up' : series[n - 1] < series[0] ? 'dn' : '';
    return `<svg class="spark ${cls}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"
      aria-hidden="true" focusable="false"><path class="fill" d="${d} L${W} ${H} L0 ${H} Z"/>
      <path class="ln" d="${d}"/></svg>`;
  };

  /* ── 52-WEEK RANGE BAR ───────────────────────────────────────────────────
   * Where the price sits between the year's low and its high. Rendered only
   * when the API gave BOTH extremes; a bar drawn from a guessed range is a
   * lie with a gradient on it.
   */
  const rangeBar = r => r.range_pos == null ? '' : `<div class="rng"
      title="${esc(r.w52_low_f || '')} low · ${esc(r.w52_high_f || '')} high">
      <span class="rng-e">${esc(r.w52_low_f || '—')}</span>
      <span class="rng-t"><span class="rng-f" style="width:${r.range_pos}%"></span
        ><span class="rng-m" style="left:${r.range_pos}%"></span></span>
      <span class="rng-e">${esc(r.w52_high_f || '—')}</span></div>`;

  /* ── TIME ────────────────────────────────────────────────────────────────
   * "in 3h 12m" / "14m ago", from a real epoch. Used for the exchange session
   * window, which comes from the quote's own trading period rather than a
   * hardcoded table of market hours that goes wrong on every holiday.
   */
  const dur = secs => {
    const m = Math.round(Math.abs(secs) / 60);
    if (m < 1) return 'under a minute';
    if (m < 60) return m + 'm';
    const h = Math.floor(m / 60), rm = m % 60;
    if (h < 24) return rm ? h + 'h ' + rm + 'm' : h + 'h';
    return Math.round(h / 24) + 'd';
  };
  const clockAt = (epochSec, tz) => {
    if (!Number.isFinite(epochSec)) return '';
    try {
      return new Date(epochSec * 1000).toLocaleTimeString('en-GB',
        { timeZone: tz || undefined, hour: '2-digit', minute: '2-digit' });
    } catch (e) { return ''; }
  };

  /* Volume, at the scale a reader actually thinks in. Never "0" — an index
   * reports 0 because it has no volume, not because nothing traded. */
  const vol = v => !Number.isFinite(v) || v <= 0 ? null
    : v >= 1e9 ? (v / 1e9).toFixed(2) + 'B'
    : v >= 1e6 ? (v / 1e6).toFixed(2) + 'M'
    : v >= 1e3 ? (v / 1e3).toFixed(1) + 'K' : String(Math.round(v));

  /* ── SMART TOOLTIPS ──────────────────────────────────────────────────────
   * A real <button>, toggled by class. Hover alone is invisible to a keyboard
   * and unusable on a phone, so every tip opens on click/Enter as well and
   * closes on Escape. Definitions live in one place so the same term never
   * gets two explanations on two pages.
   */
  const TIPS = {
    range52: ['52-week range', 'Where the current price sits between the lowest and highest price of the past year. 0% is the year’s low, 100% its high.'],
    session: ['Session', 'Whether the exchange is inside its regular trading hours right now, taken from the exchange’s own published session window — not from this page’s refresh.'],
    basis: ['Price basis', 'Which feed the number came from. “Spot” means the price is a spot quote while the 52-week range belongs to the futures contract, so the two are not from the same series.'],
    rr: ['Risk / reward', 'Potential reward relative to the risk you have defined. 3 : 1 means the first target is three times as far from entry as the stop is.'],
    confidence: ['Confidence', 'A composite score built from the five components shown beside it. A component with no data is left out of the mean rather than filled in.'],
    invalidation: ['Invalidation', 'The price at which the reason for the trade no longer exists. Not a target and not a suggestion — a level at which the position is closed.'],
    expectancy: ['Expectancy', 'The average result per closed trade, measured in units of the risk taken (R). Negative expectancy means the engine is losing money per trade on this sample.'],
    regime: ['Market regime', 'Whether price is trending or ranging, and how volatile it has been, measured from the recent daily closes of this instrument.'],
    zone: ['Entry zone', 'The band of prices at which the published entry is valid. Above it the move has already happened; below it the setup has not triggered.'],
  };
  /* ONE CARD, PARENTED TO <body>, NOT ONE PER TRIGGER.
   *
   * Two problems killed the per-trigger version, and the portal fixes both.
   *
   * 1. A transformed ancestor becomes the containing block for a fixed
   *    descendant. `.sec` carries a translateY(8px) from the reveal pass, so
   *    every tooltip inside a section was positioned against the section
   *    rather than the viewport and landed thousands of pixels off screen.
   * 2. N hidden cards is N elements taking part in layout. Absolutely
   *    positioned and merely visibility:hidden, they widened the document to
   *    458px inside a 390px phone.
   *
   * A single card on <body> has no transformed ancestor and no hidden copies.
   * The trigger carries only a key; the card is filled on open. */
  const tipCard = document.createElement('div');
  tipCard.className = 'tipc';
  tipCard.id = 'tipcard';
  tipCard.setAttribute('role', 'tooltip');
  document.body.appendChild(tipCard);
  let tipOwner = null;
  let tipOpenedAt = 0;

  const tip = key => TIPS[key]
    ? `<button type="button" class="tipb" data-tip="${esc(key)}" aria-describedby="tipcard"
        aria-expanded="false" aria-label="What is ${esc(TIPS[key][0])}?">?</button>` : '';

  /* HOVER OPENS, CLICK PINS. Without the distinction the two gestures fought:
   * moving the mouse onto a help mark opened the card, and the click that
   * followed saw it already open and closed it again — so on a desktop the
   * tooltip could not be clicked open at all. Pinned cards survive the pointer
   * leaving; Escape, an outside click and a scroll all unpin. */
  let tipPinned = false;
  const closeTips = () => {
    if (tipOwner) tipOwner.setAttribute('aria-expanded', 'false');
    tipOwner = null;
    tipPinned = false;
    tipCard.classList.remove('on');
  };

  const openTip = b => {
    const t = TIPS[b.dataset.tip];
    if (!t) return;
    tipOwner = b;
    tipOpenedAt = Date.now();
    b.setAttribute('aria-expanded', 'true');
    tipCard.innerHTML = `<b>${esc(t[0])}</b>${esc(t[1])}`;
    // Inside the brief the ground is near-black, so the card inverts there.
    tipCard.classList.toggle('on-dark', !!b.closest('.brief'));
    tipCard.classList.add('on');

    const pad = 10;
    const vw = document.documentElement.clientWidth;
    const vh = document.documentElement.clientHeight;
    const tb = b.getBoundingClientRect();
    tipCard.classList.remove('below');
    tipCard.style.left = '0px'; tipCard.style.top = '0px';
    const cb = tipCard.getBoundingClientRect();
    const x = Math.max(pad, Math.min(tb.left + tb.width / 2 - cb.width / 2, vw - pad - cb.width));
    const below = tb.top - cb.height - 10 < pad;
    const y = below ? tb.bottom + 10 : tb.top - cb.height - 10;
    tipCard.classList.toggle('below', below);
    tipCard.style.left = x.toFixed(0) + 'px';
    tipCard.style.top = Math.max(pad, Math.min(y, vh - pad - cb.height)).toFixed(0) + 'px';
    tipCard.style.setProperty('--ax', (tb.left + tb.width / 2 - x).toFixed(0) + 'px');
  };

  document.addEventListener('click', ev => {
    const b = ev.target.closest && ev.target.closest('.tipb');
    const wasPinnedHere = b && b === tipOwner && tipPinned;
    closeTips();
    if (b && !wasPinnedHere) { openTip(b); tipPinned = true; }
  });
  // Hover is an ADDITION to the click behaviour, never the only way in.
  document.addEventListener('pointerover', ev => {
    const b = ev.target.closest && ev.target.closest('.tipb');
    if (b && ev.pointerType === 'mouse' && b !== tipOwner) openTip(b);
  });
  document.addEventListener('pointerout', ev => {
    const b = ev.target.closest && ev.target.closest('.tipb');
    if (b && ev.pointerType === 'mouse' && b === tipOwner && !tipPinned) closeTips();
  });
  // Keyboard users get the same card on focus, and lose it on blur.
  document.addEventListener('focusin', ev => {
    const b = ev.target.closest && ev.target.closest('.tipb');
    if (b && b !== tipOwner) openTip(b);
  });
  document.addEventListener('focusout', ev => {
    const b = ev.target.closest && ev.target.closest('.tipb');
    if (b && b === tipOwner && !tipPinned) closeTips();
  });
  document.addEventListener('keydown', ev => { if (ev.key === 'Escape') closeTips(); });
  // A fixed card cannot follow the page, so scrolling closes it rather than
  // leaving it hanging over unrelated content.
  /* The 250ms grace matters: clicking a help mark can itself scroll the page —
   * the browser brings a partly-visible trigger into view, and the brief's own
   * section jump animates for a beat afterwards. Without it the card opened and
   * closed in the same gesture and the tooltip looked broken. */
  window.addEventListener('scroll', () => {
    if (tipOwner && Date.now() - tipOpenedAt > 250) closeTips();
  }, { passive: true });

  /* ── THE LEDGER: LIVE FIRST, BUILD ARTEFACT AS THE FALLBACK ──────────────
   *
   * alerts.json is written by generate.py at build time. Everything the
   * scanner publishes AFTER that build is in Turso and invisible to this page
   * until the next one — which on 2026-08-29 meant the Saturday weekly screen
   * (12 magic + 7 magicmagic) ran at 10:48 UTC, five hours after the 05:41
   * build, and the site showed none of it. alerts.json held zero rows dated
   * that day while /api/signals held twenty.
   *
   * So the API is asked first. Every field this page reads is on it, and the
   * two it lacks — alert_date and tv — already had fallbacks at every use
   * (`r.alert_date || r.date`, and chartUrl() defaults to NSE:<symbol>).
   * It is also uncapped, where alerts.json is trimmed to 200 rows.
   *
   * alerts.json remains as the fallback rather than being deleted: if Turso is
   * unreachable the page still renders this morning's ledger instead of an
   * error. The caller is told which source answered so it can say so — a page
   * showing yesterday's data must never look like a page showing today's.
   */
  async function ledger() {
    const live = await get('/api/signals?limit=400');
    if (live.ok) {
      const rows = live.data.signals || live.data.rows || [];
      if (rows.length) return { ok: true, rows, live: true, at: live.data.generated_at };
    }
    const snap = await get('/alerts.json');
    if (!snap.ok) return { ok: false, error: live.error || snap.error };
    const rows = Array.isArray(snap.data) ? snap.data : (snap.data.rows || []);
    return { ok: true, rows, live: false, error: live.error };
  }

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
    const bl = ev.target.closest('[data-brief]');
    if (bl) { briefSym = bl.dataset.brief; return; }   // the href does the routing
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

  /* ── A BOARD ROW ─────────────────────────────────────────────────────────
   * The old row was name / price / change. Everything else on the response was
   * being discarded, so a reader could see that Nifty moved +0.35% and not
   * whether that was a fifth of the way up its year or a whisker off the high.
   *
   * Collapsed, a row carries: name, exchange session, a month of shape, the
   * live price, the day's move and the 52-week position. Expanded, it carries
   * the rest. Nothing here is computed on the client from a guess — every
   * field is either present in the payload or printed as "Not measured".
   */
  let mkSeq = 0;
  /* THE DRAWER IS BUILT ON FIRST OPEN, NOT ON PAINT.
   *
   * Rendering all sixty-six drawers up front put 4,063 nodes on the markets
   * route against 649 before — a six-fold increase for detail that nobody had
   * asked to see yet. The row's data is parked in a map and the drawer's
   * markup is produced the first time it is opened, which keeps the route at
   * roughly the DOM it had while carrying far more information than it did.
   * The map is rebuilt on every paint, so it cannot outgrow the page. */
  let MKDATA = new Map();

  const mkRow = r => {
    const id = 'mkd' + (++mkSeq);
    MKDATA.set(id, r);
    /* The chip marks what is OPEN and nothing else. A "CLOSED" badge on all
     * sixteen India rows under a segment header that already says Closed is
     * sixteen repetitions of one fact, and it crowded out the instrument name.
     * The state is still stated in words twice — in the segment header and in
     * the row's own Session field — so nothing is carried by absence alone. */
    const sess = r.session === 'open' ? `<span class="sess is-open"><i></i>Live</span>` : '';
    return `<button type="button" class="mk" aria-expanded="false" aria-controls="${id}">
        <span class="mk-n"><span class="mk-chev" aria-hidden="true">›</span>
          <span class="mk-nm">${esc(r.name || r.symbol || '')}</span>${sess}</span>
        <span class="mk-sp">${sparkline(r.trend)
          || '<span class="mk-nosp" title="No daily close history is published for this instrument">no history</span>'}</span>
        <span class="mk-rng">${rangeBar(r)}</span>
        <span class="mk-p">${esc(r.price ?? '—')}</span>
        <span class="mk-c ${dir(r.change_pct) || 'fl'}">${pct(r.change_pct)}</span>
      </button>
      <div class="mk-d" id="${id}"><div></div></div>`;
  };

  /* Everything below is either present in the payload or printed as
   * "Not measured". Nothing here is inferred on the client. */
  const mkDrawer = r => {
    const na = '<span class="v na">Not measured</span>';
    const cell = (k, v) => `<div><span class="k">${esc(k)}</span>${
      v == null || v === '' ? na : `<span class="v">${v}</span>`}</div>`;
    const signed = v => Number.isFinite(v) ? `<span class="v ${dir(v)}">${pct(v)}</span>` : na;

    // The session window is the exchange's own, so this holds on a holiday.
    const nowS = Date.now() / 1000;
    let when = null;
    if (r.session === 'open' && Number.isFinite(r.session_end))
      when = `Closes ${clockAt(r.session_end, r.tz)} · in ${dur(r.session_end - nowS)}`;
    else if (r.session === 'closed' && Number.isFinite(r.session_start))
      when = `Closed · last session opened ${clockAt(r.session_start, r.tz)}`;

    const asOf = r.as_of ? new Date(r.as_of) : null;
    const staleMin = asOf ? (Date.now() - asOf.getTime()) / 60000 : null;

    return `<div class="mk-dg">
      ${cell('52-week high', r.w52_high_f)}
      ${cell('52-week low', r.w52_low_f)}
      <div><span class="k">From 52w high</span>${signed(r.from_high_pct)}</div>
      <div><span class="k">Above 52w low</span>${signed(r.from_low_pct)}</div>
      ${cell('Position in range', r.range_pos == null ? null : r.range_pos.toFixed(1) + '%')}
      ${cell("Day's range", r.day_low && r.day_high ? `${esc(r.day_low)} – ${esc(r.day_high)}` : null)}
      <div><span class="k">Past month</span>${signed(r.trend_pct)}</div>
      ${cell('Volume', vol(r.volume))}
      ${cell('Session', when || r.session)}
      ${cell('Quoted at', asOf ? `${asOf.toLocaleTimeString('en-GB',
          { hour: '2-digit', minute: '2-digit' })} · ${dur((Date.now() - asOf.getTime()) / 1000)} ago` : null)}
      ${cell('Instrument', r.full_name ? `${esc(r.full_name)}${r.kind ? ` · ${esc(r.kind.toLowerCase())}` : ''}` : null)}
      ${cell('Currency', r.ccy)}
      ${r.range_basis === 'futures' ? `<div class="mk-foot"><b>The price and the range are
        different feeds.</b> This row's price is a spot quote; its 52-week high and low belong to the
        futures contract, which carries a cost of carry against spot. The range position is measured
        on the futures series so the two halves agree with each other.</div>` : ''}
      ${staleMin != null && staleMin > 90 && r.session === 'open' ? `<div class="mk-foot">
        This quote is <b>${dur(staleMin * 60)}</b> old while the exchange is open — the upstream feed
        has not updated it.</div>` : ''}
    </div>`;
  };

  /* One delegated toggle for every board row on the page. */
  document.addEventListener('click', ev => {
    const b = ev.target.closest && ev.target.closest('.mk');
    if (!b) return;
    const id = b.getAttribute('aria-controls');
    const d = document.getElementById(id);
    if (!d) return;
    const inner = d.firstElementChild;
    if (inner && !inner.innerHTML) {
      const r = MKDATA.get(id);
      if (r) inner.innerHTML = mkDrawer(r);
    }
    const open = b.getAttribute('aria-expanded') === 'true';
    b.setAttribute('aria-expanded', open ? 'false' : 'true');
    d.classList.toggle('open', !open);
  });

  /* The segment's own clock: how many of its markets are trading right now,
   * and when the next one opens or closes. Both from the exchange's published
   * session window, so a public holiday is handled by the data rather than by
   * a table of market hours that nobody maintains. */
  const segWhen = items => {
    const known = items.filter(x => x.session);
    if (!known.length) return '';
    const open = known.filter(x => x.session === 'open').length;
    if (open) return `<span class="when">${open} of ${known.length} trading now</span>`;
    const nowS = Date.now() / 1000;
    // The next regular open across the segment, where the feed published one
    // in the future. Yahoo's window is the CURRENT session, so a past start is
    // simply not a forecast and is skipped rather than guessed forward.
    const next = known.map(x => x.session_start).filter(t => Number.isFinite(t) && t > nowS).sort((a, b) => a - b)[0];
    return next ? `<span class="when">Opens in ${dur(next - nowS)}</span>`
                : `<span class="when">Closed</span>`;
  };

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
    paint(head('Today', 'India’s markets, in one screen — rebuilt every morning before the open.', 'The morning edition') +
      sec('The tape', `<div class="grid">${skel('sk-tile', 4)}</div>`) +
      sec('Where the money went', `<div class="sk" style="height:104px"></div>`) +
      sec('The wire', skel('sk-card', 3)));

    const [t, p, n, m] = await Promise.all(
      [get('/today.json'), get('/pulse.json'), get('/news.json'), get('/api/markets')]);
    let out = head('Today', 'India’s markets, in one screen — rebuilt every morning before the open.', 'The morning edition');
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
      out += sec('Today’s conviction', `<div class="cards-2">${c.picks.map(convictionCard).join('')}</div>` +
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
    paint(head('Markets', 'The board live, and what the 750-name screen underneath it did.', 'The board') +
      sec('Breadth', `<div class="sk" style="height:104px"></div>`) +
      sec('Sector heat', `<div class="sk" style="height:120px"></div>`) +
      sec('The board', `<div class="board">${skel('sk-row', 8)}</div>`));

    const [m, p] = await Promise.all([get('/api/markets'), get('/pulse.json')]);
    let out = head('Markets', 'The board live, and what the 750-name screen underneath it did.', 'The board');
    const pu = p.ok ? p.data : {};

    out += sec('Breadth', breadthWidget(pu.breadth) || `<div class="empty">Screen not built yet.</div>`,
      '', 'How many names went up, out of every name measured.');
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
      MKDATA = new Map();   // one map per paint; the route repaints every 60s
      out += sec('The board',
        segs.map(sg => `<div class="segh">${esc(sg.icon || '')} ${esc(sg.label)}
            ${segWhen(sg.items)}<span class="cnt">${sg.items.length}</span></div>
          <div class="board">${sg.items.map(mkRow).join('')}</div>`).join('') +
        `<p class="sec-note"><b>Every row opens.</b> The line under each name is the past month of
          real daily closes; the bar beside it is where the price sits between its own 52-week low
          and high ${tip('range52')}. Tap a row for the extremes, the day's range, volume, the exchange session ${tip('session')} and
          the exact time the quote was taken. A figure this site cannot measure says
          <b>Not measured</b> — it is never filled in.</p>`,
        `${tk.data.live ?? 0} of ${tk.data.total ?? 0} live`,
        'Forty-six instruments, each with the year behind it.');
    } else { out += sec('The board', fail('The live board', tk.error)); }

    out += sec('Biggest movers, one week', levelTable((pu.movers_up || []).slice(0, 8)),
      '', 'What actually moved, over a week rather than a day.');
    out += sec('Biggest fallers, one week', levelTable((pu.movers_dn || []).slice(0, 8)),
      '', 'The other half of the same week.');
    paint(out);
  };

  R['/ideas'] = async () => {
    paint(head('Ideas', 'Ranked names, and the orders a fully-sized book would place against them. Sizes are shown as a share of the book, so they scale to whatever you run.', 'Ranked ideas') +
      sec('Trade ideas', skel('sk-card', 3)));
    const [t, mn, p] = await Promise.all([get('/today.json'), get('/mandate.json'), get('/pulse.json')]);
    let out = head('Ideas', 'Ranked names, and the orders a fully-sized book would place against them. Sizes are shown as a share of the book, so they scale to whatever you run.', 'Ranked ideas');
    if (!t.ok) { paint(out + fail('Ideas', t.error)); return; }

    const picks = t.data.picks || [];
    out += sec('Trade ideas', picks.length ? `<div class="cards-2">${picks.map(x => ideaCard(x, false)).join('')}</div>`
      : `<div class="empty">Nothing clears the bar this week. That is a result, not a gap.</div>`,
      `${picks.length} ranked`, 'Names that cleared every floor, and the levels that define each one.');

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
    paint(head('IPO', 'Books open now, what is coming, and how the last year of listings actually did.', 'Primary market') +
      sec('Open now', skel('sk-card', 2)));
    const io = await get('/ipo.json');
    let out = head('IPO', 'Books open now, what is coming, and how the last year of listings actually did.', 'Primary market');
    if (!io.ok) { paint(out + fail('The IPO radar', io.error)); return; }
    const d = io.data, c = d.counts || {};

    out += sec('Where it stands', `<div class="grid">
        ${tile((d.open || []).length, 'Books open', 'bidding today', (d.open || []).length ? 'ac' : '')}
        ${tile((d.upcoming || []).length, 'Upcoming', 'announced, not open')}
        ${tile((d.awaiting_listing || []).length, 'Awaiting listing', 'closed, not yet traded')}
        ${tile(c.apply ?? '—', 'Rated apply', 'on public demand only', c.apply ? 'up' : '')}
      </div>`);

    out += sec('Open now', (d.open || []).length ? `<div class="cards-2">${d.open.map(ipoCard).join('')}</div>`
      : `<div class="empty">No mainboard book is open today.</div>`);

    if ((d.upcoming || []).length) out += sec('Upcoming', `<div class="cards-2">${d.upcoming.map(ipoCard).join('')}</div>`);
    if ((d.awaiting_listing || []).length)
      out += sec('Awaiting listing', `<div class="cards-2">${d.awaiting_listing.map(ipoCard).join('')}</div>`);

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
      'Every one of the 750 names, searchable. Tap any row for the full card.',
      'The full universe') + body;
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
    paint(head('Signals', intro, 'The public ledger') + skel('sk-card', 4));
    const a = await ledger();
    const base = head('Signals', intro, 'The public ledger')
      + (a.live ? '' : `<div class="note"><b>Showing this morning's snapshot, not the live ledger.</b>
          The live signal feed did not answer${a.error ? ` — ${esc(a.error)}` : ''}, so this page is
          reading the copy written at the last build. Anything the scanner has published since is
          missing from it.</div>`);
    if (!a.ok) { paint(base + fail('The signal ledger', a.error)); return; }
    /* DAY ONE IS TODAY.
     *
     * This surface starts its record from launch. Everything before it was
     * published under a different engine configuration and a ledger that has
     * been re-graded twice, and carrying that history here would mean showing
     * a win rate this site never produced.
     *
     * Nothing is deleted. The ledger still carries every earlier signal and
     * the full performance record — this is a filter on what THIS page counts,
     * not a rewrite of the ledger.
     *
     * Anything sent on or after LAUNCH counts. When there is nothing yet, the
     * page says so rather than showing an empty table that reads as a fault.
     */
    const LAUNCH = '2026-08-29';
    const dayOf = r => String(r.alert_date || r.date || '').slice(0, 10);
    const every = a.rows;
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
          ${open ? `<a class="brief-link" href="#/brief" data-brief="${esc(r.symbol)}">Full brief →</a>` : ''}
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
          in the ledger — it is simply older than this page's counting window.
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
        sec('Alerts', rows.length ? `<div class="cards-2">${rows.map(card).join('')}</div>`
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

  /* ── JOIN ────────────────────────────────────────────────────────────────
   * An honest early-access capture, not a fake login.
   *
   * It posts to /api/subscribe, which already exists and already does the
   * hard parts: RFC-ish validation, a honeypot, a minimum time-on-page, one
   * row per address, and a per-IP hourly cap enforced in SQL because lambdas
   * do not share memory. The raw IP is never stored — it is salted and hashed
   * only so the rate limit can work.
   *
   * There is deliberately no password field. A password box that does not
   * authenticate anything is the worst thing this page could ship: it teaches
   * a reader to hand over a credential to a form that cannot check it. Real
   * accounts need a session store and a thirteenth serverless function, and
   * this project is at Vercel's twelve-function cap — see the note in
   * api/signals.js. That is a decision to take deliberately, not a box to draw.
   */
  const MOUNTED_AT = Date.now();
  R['/join'] = async () => {
    paint(`<div class="join">
      <div class="join-l">
        <h1>Get it before the open.</h1>
        <p class="join-sub">One email each morning: what moved, the sector heat, the books
          open today and the names the engine put up — with the levels it put them up at.</p>
        <ul class="join-ul">
          <li><b>Free.</b> No card, no trial that expires into a charge.</li>
            <li><b>The record is public.</b> Every signal is scored when it closes — losers
            included, which is the point of publishing it.</li>
          <li><b>One email a day.</b> Unsubscribe in one click, and the list is a table
            we own rather than a mailing vendor's.</li>
        </ul>
      </div>
      <div class="join-r">
        <form id="joinF" novalidate>
          <label for="joinE">Email address</label>
          <input id="joinE" name="email" type="email" inputmode="email" autocomplete="email"
                 placeholder="you@example.com" required>
          <!-- Bots fill this. Humans never see it. -->
          <div class="hp" aria-hidden="true">
            <label for="joinW">Website</label>
            <input id="joinW" name="website" type="text" tabindex="-1" autocomplete="off">
          </div>
          <button type="submit" class="btn-primary" id="joinB">Get the morning brief</button>
          <p class="join-note" id="joinM">We send one email a day. Nothing else, ever.</p>
        </form>
      </div>
    </div>`);

    const f = document.getElementById('joinF');
    f.addEventListener('submit', async ev => {
      ev.preventDefault();
      const btn = document.getElementById('joinB');
      const msg = document.getElementById('joinM');
      const email = document.getElementById('joinE').value.trim();
      // Validate here as well as on the server: a round trip to be told the
      // address has no @ in it is a round trip wasted.
      if (!/^[^@\s]+@[^@\s.]+\.[^@\s]{2,}$/.test(email)) {
        msg.className = 'join-note bad';
        msg.textContent = 'That address does not look right — check for a typo.';
        return;
      }
      btn.disabled = true; btn.textContent = 'Sending…';
      msg.className = 'join-note'; msg.textContent = 'One moment.';
      try {
        const r = await fetch('/api/subscribe', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            email, website: document.getElementById('joinW').value,
            elapsed: Date.now() - MOUNTED_AT,
          }),
        });
        const j = await r.json().catch(() => ({}));
        if (r.ok && j.ok !== false) {
          f.innerHTML = `<div class="join-ok"><b>You are on the list.</b>
            The next brief goes out before tomorrow's open. If it does not arrive,
            check the spam folder once and mark it "not spam" — that is the only
            thing that keeps it landing.</div>`;
        } else {
          msg.className = 'join-note bad';
          msg.textContent = j.error || 'That did not go through. Try again in a moment.';
          btn.disabled = false; btn.textContent = 'Get the morning brief';
        }
      } catch (e) {
        msg.className = 'join-note bad';
        msg.textContent = 'No connection. Your address was not sent — try again.';
        btn.disabled = false; btn.textContent = 'Get the morning brief';
      }
    });
  };

  /* ══════════════════════════════════════════════════════════════════════
   * THE TRADING SIGNAL BRIEF
   *
   * A research document you can interrogate, not a page you read. Everything
   * on it is derived from three real sources — the published ledger, the
   * 750-name screen, and six months of actual daily closes — and anything
   * those three cannot answer is printed as unmeasured rather than filled in.
   *
   * WHAT CHANGED, AND WHY THE CAPTION CHANGED WITH IT.
   * This page used to carry a "level map" and a caption explaining that no
   * feed here served price history. That was true of the ROUTES, not of the
   * upstream: the same spark endpoint the market rail already calls returns a
   * close series. /api/signals?series= now exposes it, so the chart is drawn
   * from prices that happened. There are still no candles, because there is
   * still no open/high/low — and the caption says exactly that, rather than
   * implying the chart is complete.
   *
   * WHAT IS DELIBERATELY ABSENT.
   *   · Scenario probabilities. The engine publishes no probability model, so
   *     the scenarios carry the ledger's own base rate — labelled as a base
   *     rate over N closed trades — and never a per-trade percentage.
   *   · An intraday development timeline. The ledger records the signal date
   *     and the send time; it does not record "momentum confirmed at 09:24".
   *     The timeline shows what is recorded and says so.
   * ══════════════════════════════════════════════════════════════════════ */

  let briefSym = null;                 // set when arriving from a signal card
  /* WHAT THE READER IS LOOKING AT, kept across refreshes.
   *
   * briefSym was consumed on first use and set back to null. The page repaints
   * itself every 60 seconds, and on that repaint the symbol was gone — so the
   * brief fell back to "highest reward-to-risk open setup" and silently swapped
   * a different company in while someone was reading. Click JMFINANCIL, scroll
   * for a minute, find yourself reading JKTYRE.
   *
   * briefPick holds the choice for as long as the reader is on this route.
   * render() clears it when they leave, so arriving fresh still picks the best
   * available setup rather than resurrecting an old one. */
  let briefPick = null;
  let briefRange = '6mo';              // the chart window the reader last chose

  /* Realised volatility and trend persistence, both computed from the real
   * close series. Returns null rather than a number when the series is too
   * short to support one — 20 closes is the floor for a 20-day mean. */
  function regimeOf(closes) {
    if (!Array.isArray(closes) || closes.length < 30) return null;
    const rets = [];
    for (let i = 1; i < closes.length; i++) {
      if (closes[i - 1] > 0) rets.push(closes[i] / closes[i - 1] - 1);
    }
    if (rets.length < 20) return null;
    const mean = rets.reduce((a, b) => a + b, 0) / rets.length;
    const varr = rets.reduce((a, b) => a + (b - mean) * (b - mean), 0) / (rets.length - 1);
    const vol = Math.sqrt(varr) * Math.sqrt(252) * 100;      // annualised, %

    // Trend persistence: over the last 60 closes, the share that sat above
    // their own trailing 20-day mean. 50% is a coin flip and reads as a range;
    // a sustained trend pins it high or low.
    const W = 20, look = Math.min(60, closes.length - W);
    let above = 0;
    for (let i = closes.length - look; i < closes.length; i++) {
      let s = 0; for (let k = i - W; k < i; k++) s += closes[k];
      if (closes[i] > s / W) above++;
    }
    const persist = (above / look) * 100;
    const net = (closes[closes.length - 1] / closes[0] - 1) * 100;
    const trending = persist >= 66 || persist <= 34;
    return {
      vol, persist, net, look,
      label: trending ? (persist >= 66 ? 'TRENDING UP' : 'TRENDING DOWN') : 'RANGING',
      volLabel: vol >= 45 ? 'HIGH VOLATILITY' : vol >= 22 ? 'NORMAL VOLATILITY' : 'LOW VOLATILITY',
    };
  }

  /* THE CHART. One <svg> in a fixed 1000×340 user space, stretched to fit with
   * preserveAspectRatio="none" — so it fills any width at any height without a
   * measurement pass, and without ResizeObserver, which never fires in a
   * hidden tab. Strokes stay a constant visual width through
   * vector-effect:non-scaling-stroke, and every LABEL is HTML positioned by
   * percentage rather than SVG text, because stretched text is unreadable.
   *
   * The overlays are emitted hidden. The scroll story turns them on in the
   * order the argument is made. */
  function priceChart(pts, levels, cur) {
    const W = 1000, H = 340, PADT = 14, PADB = 14;
    const cs = pts.map(p => p.c);
    const lv = levels.filter(l => Number.isFinite(l.v));
    const lo = Math.min.apply(null, cs.concat(lv.map(l => l.v)));
    const hi = Math.max.apply(null, cs.concat(lv.map(l => l.v)));
    const span = (hi - lo) || 1;
    const X = i => (i / Math.max(1, pts.length - 1)) * W;
    const Y = v => PADT + (1 - (v - lo) / span) * (H - PADT - PADB);
    const yp = v => ((Y(v) / H) * 100);              // percent, for HTML labels
    const d = cs.map((v, i) => (i ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Y(v).toFixed(2)).join(' ');

    const line = (l, i) => `<line class="lvl ${l.c} b-ov" data-ov="${l.step}" x1="0" x2="${W}"
        y1="${Y(l.v).toFixed(2)}" y2="${Y(l.v).toFixed(2)}"/>`;
    const zone = (a, b, cls, step) => {
      if (!Number.isFinite(a) || !Number.isFinite(b)) return '';
      const y1 = Math.min(Y(a), Y(b)), h = Math.abs(Y(a) - Y(b));
      return `<rect class="zone ${cls} b-ov" data-ov="${step}" x="0" y="${y1.toFixed(2)}"
        width="${W}" height="${Math.max(1, h).toFixed(2)}"/>`;
    };
    const lab = l => `<span class="b-px-lab ${l.c} b-ov" data-ov="${l.step}"
        style="top:${yp(l.v).toFixed(2)}%">${esc(l.k)} <b>${esc(l.f)}</b></span>`;

    return {
      html: `<div class="b-px-c" id="pxc">
        <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true" focusable="false">
          <defs><linearGradient id="bgrad" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stop-color="#7A9BEE" stop-opacity=".16"/>
            <stop offset="100%" stop-color="#7A9BEE" stop-opacity="0"/></linearGradient></defs>
          ${zone(cur.stop, cur.entry, 'r', 6)}
          ${zone(cur.entry, cur.t2, 'g', 7)}
          <path class="parea" d="${d} L${W} ${H} L0 ${H} Z"/>
          <path class="price" d="${d}"/>
          ${lv.map(line).join('')}
          <line class="cross" id="pxcross" x1="0" x2="0" y1="0" y2="${H}" style="opacity:0"/>
        </svg>
        <div class="b-px-labs">${lv.map(lab).join('')}</div>
        <span class="b-px-dot" id="pxdot"></span>
        <div class="b-px-t" id="pxt"></div>
        <div class="b-px-hit" id="pxhit" role="img"
          aria-label="${esc(pts.length)} daily closes from ${esc(pts[0].t || '')} to ${esc(pts[pts.length - 1].t || '')}, with the published entry, stop and targets overlaid"></div>
      </div>`,
      X, Y, lo, hi, span, W, H,
    };
  }

  R['/brief'] = async () => {
    /* SKELETON, shaped like the thing that replaces it — an instrument header,
     * a metric rail, a chart. Grey boxes of the wrong shape are why a loading
     * state feels like a broken page. */
    paint(`<div class="brief"><div class="b-wrap">
      <div class="b-hero"><div class="b-eyebrow">Trading signal brief</div>
        <div class="b-sk" style="height:54px;max-width:16ch;margin-top:20px"></div>
        <div class="b-sk" style="height:20px;max-width:46ch;margin-top:18px;border:0"></div></div>
      <div class="b-sk" style="height:88px;margin-top:26px"></div>
      <div class="b-sk" style="height:300px;margin-top:26px"></div></div></div>`);

    const [a, sc, st] = await Promise.all(
      [ledger(), get('/screen.json'), get('/api/stats')]);
    if (!a.ok) { paint(fail('The signal brief', a.error)); return; }
    const rows = a.rows;
    const open = rows.filter(r => (r.badge || '').toLowerCase() === 'open'
                               && r.entry && r.sl && r.target1);
    if (!open.length) { paint(`<div class="brief"><div class="b-wrap"><div class="b-hero">
      <div class="b-eyebrow">Trading signal brief</div>
      <h1>No setup is live right now.</h1>
      <p class="b-sub">The engine publishes when a setup clears its floors, and not otherwise.
        An empty brief is a result, not a fault.</p>
      <p style="margin-top:22px"><span class="dstate nodata"><i></i>No data</span></p>
      </div></div></div>`); return; }

    if (sc.ok) SCREEN = SCREEN || (sc.data.rows || []).filter(x => x && x.sym);
    const inScreen = sym => (SCREEN || []).some(x => x.sym === sym);
    const ranked = open.slice().sort((x, y) => (y.rr || 0) - (x.rr || 0));
    const want = briefSym || briefPick;
    const askedFor = briefSym;          // set only when the reader just clicked
    briefSym = null;
    const hit = want ? open.find(r => r.symbol === want) : null;
    const sig = hit || ranked.find(r => inScreen(r.symbol)) || ranked[0];
    briefPick = sig.symbol;
    /* A requested symbol that cannot be shown is SAID, not substituted. The
     * brief needs an open row carrying an entry, a stop and a first target;
     * a name whose signal has closed, or which never published all three, has
     * no brief to show. Quietly rendering a different company instead is the
     * failure that looks most like working. */
    const missed = askedFor && !hit ? askedFor : null;
    const row = (SCREEN || []).find(x => x.sym === sig.symbol) || {};

    /* The live quote and six months of closes, fetched together. Sequential
     * awaits would put two round trips on the critical path for no reason. */
    const [q, ser] = await Promise.all([
      quotes([sig.symbol]),
      get('/api/signals?series=' + encodeURIComponent(sig.symbol) + '&range=' + briefRange),
    ]);
    const live = q[sig.symbol];
    const pts = ser.ok && Array.isArray(ser.data.points) ? ser.data.points : null;
    const closes = pts ? pts.map(p => p.c) : null;

    const cur = sig.currency || '₹';
    const N = v => Number(v);
    const entry = N(sig.entry), stop = N(sig.sl), t1 = N(sig.target1), t2 = N(sig.target2 || sig.target1);
    const last = live ? live.price : (N(row.price) || (closes ? closes[closes.length - 1] : entry));
    const isShort = /SELL|SHORT/i.test(sig.action || '');
    const risk = Math.abs(entry - stop);
    /* TWO REWARD-TO-RISK NUMBERS, BECAUSE THERE ARE TWO TARGETS.
     *
     * The ledger publishes one `rr` field and it is measured to TARGET 2. The
     * calculator on this page measures to target 1, because that is the level
     * the trade plan acts on. Printing the ledger's 4.4 in the header while the
     * calculator said 2.5 put two different answers to the same question on one
     * page with nothing to tell them apart.
     *
     * Both are now derived from the published levels — the auditable route —
     * and each says which target it belongs to. The ledger's own field is kept
     * only as a cross-check: if it disagrees with the arithmetic on its own
     * levels, the page says so rather than choosing a winner silently. */
    const rrT1 = Math.abs(t1 - entry) / (risk || 1);
    const rrT2 = Math.abs(t2 - entry) / (risk || 1);
    const rr = rrT1;
    const rrLedger = Number(sig.rr);
    // 0.15 is wider than rounding (the ledger stores one decimal) and narrower
    // than the gap between a T1 and a T2 reading on any real setup.
    const rrDisagrees = Number.isFinite(rrLedger)
      && Math.abs(rrLedger - rrT1) > 0.15 && Math.abs(rrLedger - rrT2) > 0.15;
    const f = v => Number.isFinite(v) ? cur + v.toLocaleString('en-IN', { maximumFractionDigits: 2 }) : '—';
    const dist = v => Number.isFinite(v) && Number.isFinite(last) && last
      ? `${v >= last ? '+' : '−'}${f(Math.abs(v - last)).replace(cur, cur)} · ${pct((v - last) / last * 100)}` : '—';

    /* ── DATA STATE. Six of them, and the page never wears the wrong one. */
    const sigDate = new Date((sig.alert_date || sig.date || '') + 'T00:00:00');
    const ageDays = Number.isFinite(sigDate.getTime())
      ? Math.round((Date.now() - sigDate.getTime()) / 86400000) : null;
    const state = !live ? (row.price ? 'delayed' : 'nodata')
      : ageDays != null && ageDays > 21 ? 'stale' : 'live';
    const stateChip = {
      live: ['live', 'Live', 'The price beside the entry is a live quote taken on this page load.'],
      delayed: ['delayed', 'Last close', 'The live quote did not answer. The price shown is the last close from the screen build.'],
      stale: ['stale', 'Ageing signal', `This setup was published ${ageDays} days ago and is still open. The levels stand; the thesis has had time to change.`],
      nodata: ['nodata', 'No price', 'Neither the live quote nor the screen carries a price for this name right now.'],
    }[state];

    /* ── SCORE. Five components, each a rule over the screen's own fields.
     * A score with no visible derivation is a number asking to be believed. */
    const clamp = (v, lo, hi) => Number.isFinite(v)
      ? Math.max(0, Math.min(100, (v - lo) / (hi - lo) * 100)) : null;
    const COMPS = [
      ['Structure', 'is-target', row.setup ? Math.min(100, ((row.setup.tags || []).length) * 26 + (row.brk52w ? 22 : 0)) : null,
        row.brk52w ? 'Price is at a 52-week high, and the screen tags this as a completed breakout.'
                   : `The screen tags ${(row.setup && (row.setup.tags || []).length) || 0} structural conditions on this name. A 52-week breakout is not one of them.`],
      ['Momentum', 'is-now', clamp(N(row.r1m), -6, 26),
        Number.isFinite(N(row.r1m)) ? `One month return is ${pct(N(row.r1m))}, three month ${pct(N(row.r3m))}, RSI ${row.rsi != null ? Math.round(row.rsi) : '—'}.`
                                    : 'No return history on the screen for this name, so momentum is unscored rather than assumed.'],
      ['Trend', 'is-key', row.sma200 && row.price ? clamp((row.price - row.sma200) / row.sma200 * 100, -8, 32) : null,
        row.sma200 && row.price ? `Price sits ${pct((row.price - row.sma200) / row.sma200 * 100)} against its 200-day average, with the 50-day ${row.sma50 > row.sma200 ? 'above' : 'below'} it.`
                                : 'No 200-day average on the screen for this name.'],
      ['Volume', '', clamp(N(row.vol_spike), .7, 4),
        Number.isFinite(N(row.vol_spike)) ? `Volume is running at ${N(row.vol_spike).toFixed(2)}× its own recent average. Above 1.0 means participation is confirming the move.`
                                          : 'No volume ratio published for this name.'],
      ['Risk / reward', 'is-stop', clamp(rr, 1, 5),
        `The first target is ${rr.toFixed(1)} times as far from entry as the stop is. Anything under 1.5 is a setup that has to win more often than it loses to break even.`],
    ];
    const have = COMPS.map(c => c[2]).filter(v => Number.isFinite(v));
    const score = have.length ? Math.round(have.reduce((x, y) => x + y, 0) / have.length) : null;
    const conviction = score == null ? 'UNSCORED' : score >= 70 ? 'HIGH CONVICTION'
                     : score >= 50 ? 'MODERATE CONVICTION' : 'LOW CONVICTION';

    /* ── THE LADDER. Every level on one scale, so distance is real. */
    const pts_ = [
      { k: 'Target 2', v: t2, c: 'is-target', step: 7 }, { k: 'Target 1', v: t1, c: 'is-target', step: 7 },
      { k: '52-week high', v: N(row.high52), c: 'is-key', step: 2 },
      { k: 'Now', v: last, c: 'is-now', step: 1 }, { k: 'Entry', v: entry, c: '', step: 5 },
      { k: '200-day', v: N(row.sma200), c: 'is-key', step: 3 },
      { k: 'Stop', v: stop, c: 'is-stop', step: 6 },
    ].filter(x => Number.isFinite(x.v)).sort((x, y) => y.v - x.v);
    const hiL = Math.max.apply(null, pts_.map(p => p.v)), loL = Math.min.apply(null, pts_.map(p => p.v));
    const at = v => ((hiL - v) / ((hiL - loL) || 1)) * 100;

    /* Label de-collision happens AFTER paint, in spaceLadder(), because it
     * needs the ladder's real pixel height. The first attempt did it here in
     * percent and applied it with translateY(<percent>) — which resolves
     * against the ELEMENT'S OWN height, not the container, so a -3.3% nudge on
     * a 12px label moved it a third of a pixel and the labels still sat on top
     * of each other. Percentages and transforms do not mean what they look
     * like they mean. */
    const ladder = pts_.map((p, i) => `<div class="b-lvl ${p.c}" tabindex="0" data-lvl="${esc(p.k)}"
        style="top:${at(p.v).toFixed(2)}%;animation-delay:${(i * 60)}ms">
        <span class="b-lvl-tag">${esc(p.k)}</span><span class="b-lvl-line"></span>
        <span class="b-lvl-d">${p.k === 'Now' ? 'current' : dist(p.v)}</span>
        <span class="b-lvl-val">${f(p.v)}</span></div>`).join('');

    /* ── THE CHART, from real closes. */
    const chartLevels = [
      { k: 'T2', v: t2, f: f(t2), c: 't', step: 7 }, { k: 'T1', v: t1, f: f(t1), c: 't', step: 7 },
      { k: 'ENTRY', v: entry, f: f(entry), c: 'e', step: 5 },
      { k: 'STOP', v: stop, f: f(stop), c: 's', step: 6 },
      { k: '200D', v: N(row.sma200), f: f(N(row.sma200)), c: 'k', step: 3 },
    ].filter(l => Number.isFinite(l.v));
    const CH = pts ? priceChart(pts, chartLevels, { entry, stop, t1, t2 }) : null;

    /* ── ENTRY STATE. Published levels only — no invented zone width. */
    const zoneState = (() => {
      if (!Number.isFinite(last)) return ['wait', 'Unpriced', 'No live price, so the setup cannot be placed against its own levels right now.'];
      const beyondStop = isShort ? last >= stop : last <= stop;
      if (beyondStop) return ['invalid', 'Invalidated', `Price is beyond the published stop at ${f(stop)}. The structure that produced this setup is gone.`];
      const better = isShort ? last >= entry : last <= entry;
      if (better) return ['active', 'At or better', `Price is at or better than the published entry of ${f(entry)}. The trade plan's condition is met.`];
      const wayThrough = Math.abs(last - entry) > Math.abs(t1 - entry) * 0.5;
      if (wayThrough) return ['missed', 'Extended', `Price has already travelled ${pct(Math.abs(last - entry) / entry * 100)} past the entry, more than half the distance to the first target. Entering here buys a worse price against the same stop.`];
      return ['wait', 'Waiting', `Price is past the entry but not far. The plan calls for entry at or better than ${f(entry)}, so this is a wait rather than a chase.`];
    })();
    const zLo = Math.min(stop, entry, t1, last), zHi = Math.max(stop, entry, t1, last);
    const zAt = v => ((v - zLo) / ((zHi - zLo) || 1)) * 100;

    /* ── REGIME, from the real series. */
    const reg = regimeOf(closes);

    /* ── CONFLUENCE. The same five components, expressed as a stance. */
    const stance = v => !Number.isFinite(v) ? null : v >= 60 ? 0 : v >= 40 ? 1 : 2;
    const MATRIX = COMPS.map(c => [c[0], stance(c[2]), c[3]]);

    /* ── BASE RATE, not a probability. */
    const S = st.ok ? st.data : null;
    const H = S && S.headline ? S.headline : null;
    const closedRows = rows.filter(r => r.pnl_pct != null && (r.badge || '') !== 'open').slice(0, 8);

    const thesis = [
      ['Market structure', 'The trend is doing the heavy lifting.',
        row.sma200 && row.price
          ? `Price sits ${pct((row.price - row.sma200) / row.sma200 * 100)} against its 200-day average, with the 50-day ${row.sma50 > row.sma200 ? 'above' : 'below'} it. ${row.sma50 > row.sma200 ? 'The longer structure is intact, so the setup is with the trend rather than against it.' : 'The longer structure has not confirmed, which is why this is sized as a smaller idea.'}`
          : 'Trend data is not available for this name, so the structure leg of the thesis is unscored rather than assumed.',
        [['vs 200-day', row.sma200 ? pct((row.price - row.sma200) / row.sma200 * 100) : '—'],
         ['50 vs 200', row.sma50 && row.sma200 ? (row.sma50 > row.sma200 ? 'above' : 'below') : '—'],
         ['Sector', row.sector ? esc(row.sector) : '—']]],
      ['Momentum', 'Momentum confirms the move.',
        `One-month return is ${pct(N(row.r1m))} and RSI reads ${row.rsi != null ? Math.round(row.rsi) : '—'} on the daily. ${N(row.rsi) > 70 ? 'That is extended, which argues for the entry zone rather than chasing the print.' : 'That leaves room before the move is stretched.'}`,
        [['1 month', pct(N(row.r1m))], ['3 month', pct(N(row.r3m))],
         ['RSI 14D', row.rsi != null ? Math.round(row.rsi) : '—']]],
      ['Levels', 'Price is approaching a level that matters.',
        `Entry sits at ${f(entry)} with the invalidation ${f(stop)} — ${pct(-Math.abs(risk / entry * 100))} away. The first target at ${f(t1)} is ${pct(Math.abs(t1 - entry) / entry * 100)} from entry.`,
        [['Entry', f(entry)], ['Stop', f(stop)], ['Target 1', f(t1)]]],
      ['Risk', 'The setup is attractive because the risk is defined.',
        `Reward to risk is ${rrT1.toFixed(1)} to one against the first target and ${rrT2.toFixed(1)} to one against the second. The stop is a price, not an intention: below ${f(stop)} the reason for the trade is gone and the position is closed.`,
        [['R:R to T1', rrT1.toFixed(1) + ' : 1'], ['R:R to T2', rrT2.toFixed(1) + ' : 1'], ['Risk per share', f(risk)],
         ['Daily ATR', Number.isFinite(N(row.atr_pct)) ? N(row.atr_pct).toFixed(2) + '%' : '—']]],
    ];

    const SECTIONS = [
      ['b-overview', 'Overview', '1', 'Signal', 'the setup exists'],
      ['b-chart', 'Chart', '2', 'Entry', 'where it starts'],
      ['b-thesis', 'Thesis', '3', 'Confirmation', 'why it should work'],
      ['b-plan', 'Trade plan', '4', 'Target', 'what it is worth'],
      ['b-risk', 'Risk', '5', 'Sizing', 'what it costs'],
      ['b-history', 'History', '6', 'Exit', 'what happened before'],
    ];

    paint(`<div class="brief"><div class="b-wrap">

      <header class="b-hero" id="b-overview">
        <div class="b-eyebrow">Trading signal brief</div>
        <h1>${esc(sig.symbol)} is ${last > entry ? 'holding above' : 'testing'} its entry zone.</h1>
        <p class="b-sub">A ${conviction.toLowerCase().replace(' conviction', '-conviction')} setup built from price
          structure, momentum, volume and defined risk. Every figure below comes from the published ledger,
          the same 750-name screen the rest of this site runs on, and ${pts ? `${pts.length} real daily closes` : 'the published levels'}.</p>
      </header>

      ${missed ? `<div class="b-miss"><b>${esc(missed)} has no brief to show.</b>
        A brief needs an open signal carrying an entry, a stop and a first target — that name
        has none right now, so this is the best open setup instead, not a substitute for it.</div>` : ''}

      <nav class="b-qnav" id="qnav" aria-label="Sections of this brief">
        ${SECTIONS.map(([id, lab, key]) => `<a href="#${id}" data-jump="${id}">
          <span class="kb" aria-hidden="true">${key}</span>${esc(lab)}</a>`).join('')}
      </nav>

      <section class="b-status">
        <div class="b-dir ${isShort ? 'is-short' : ''}">
          <span class="b-dir-mark"></span>
          <div><div class="b-dir-l">${isShort ? '▼ SHORT' : '▲ LONG'}</div>
            <div class="b-dir-meta">
              <span class="b-tag">${esc(sig.symbol)}</span>
              <span class="b-tag">${esc(sig.timeframe || '1D')}</span>
              <span class="b-tag"><span id="convN" data-cv="">${score == null ? '—' : '0'}</span> CONFIDENCE</span>
              <span class="dstate ${stateChip[0]}"><i></i>${esc(stateChip[1])}</span>
            </div></div>
        </div>
        <div class="b-metrics">
          <div class="b-m"><span class="k">Entry</span><span class="v">${f(entry)}</span></div>
          <div class="b-m"><span class="k">Current</span><span class="v ${last >= entry ? 'up' : 'dn'}" id="curPx">${f(last)}</span></div>
          <div class="b-m"><span class="k">Day</span><span class="v ${live && live.change_pct >= 0 ? 'up' : 'dn'}">${live && Number.isFinite(live.change_pct) ? pct(live.change_pct) : '—'}</span></div>
          <div class="b-m"><span class="k">Stop</span><span class="v dn">${f(stop)}</span></div>
          <div class="b-m"><span class="k">Target 1</span><span class="v up">${f(t1)}</span></div>
          <div class="b-m"><span class="k">Target 2</span><span class="v up">${f(t2)}</span></div>
          <div class="b-m"><span class="k">R:R to T1</span><span class="v gold">${rrT1.toFixed(1)} : 1</span></div>
          <div class="b-m"><span class="k">R:R to T2</span><span class="v gold">${rrT2.toFixed(1)} : 1</span></div>
          <div class="b-m"><span class="k">Signal age</span><span class="v">${ageDays == null ? '—' : ageDays + 'd'}</span></div>
          <div class="b-m"><span class="k">Sector</span><span class="v" style="font-family:var(--ui);font-size:14px">${esc(row.sector || 'Not on screen')}</span></div>
        </div>
      </section>
      <p class="b-p" style="margin-top:14px;font-size:13px">${esc(stateChip[2])}
        Reward to risk is shown against <b style="color:var(--b-ink)">both</b> targets, because they are
        different numbers and the trade plan acts on the first one. The ledger's own published field
        reads ${Number.isFinite(rrLedger) ? rrLedger.toFixed(1) : '—'}, which is the reading to target 2.
        ${rrDisagrees ? `<b style="color:var(--b-gold)">It agrees with neither figure computed from its own
          published levels, so the arithmetic above is what this page shows and the ledger field is the
          one to distrust.</b>` : ''}</p>

      <nav class="b-jr" id="journey" aria-label="Where you are in this brief">
        ${SECTIONS.map(([id, , , node, sub]) => `<div class="b-jn" data-node="${id}">
          <i aria-hidden="true"></i><b>${esc(node)}</b><span>${esc(sub)}</span></div>`).join('')}
      </nav>

      <section class="b-sec b-reveal">
        <div class="b-lab">The trade in one view</div>
        <h2 class="b-h2">Everything that defines the position.</h2>
        <dl class="b-view">
          <div class="b-vi"><dt>Setup</dt><dd class="txt">${esc((row.setup && (row.setup.tags || [])[0]) || sig.signal_type || 'Engine signal')}</dd></div>
          <div class="b-vi"><dt>Direction</dt><dd class="txt">${isShort ? 'Short' : 'Long'}</dd></div>
          <div class="b-vi"><dt>Entry</dt><dd>${f(entry)}</dd></div>
          <div class="b-vi"><dt>Stop</dt><dd>${f(stop)}</dd></div>
          <div class="b-vi"><dt>Target 1</dt><dd>${f(t1)}</dd></div>
          <div class="b-vi"><dt>Target 2</dt><dd>${f(t2)}</dd></div>
          <div class="b-vi"><dt>R:R to T1</dt><dd>${rrT1.toFixed(1)} : 1</dd></div>
          <div class="b-vi"><dt>Horizon</dt><dd class="txt">${esc(sig.timeframe === '1D' ? 'Swing' : (sig.timeframe || 'Swing'))}</dd></div>
        </dl>

        <div class="b-zone">
          <div class="b-zone-h">
            <span class="b-zst ${zoneState[0]}">${esc(zoneState[1])}</span>
            <span class="b-lab" style="letter-spacing:.16em">Entry state ${tip('zone')}</span>
          </div>
          <div class="b-zt">
            <span class="band" style="left:${Math.min(zAt(entry), zAt(t1)).toFixed(1)}%;width:${Math.abs(zAt(t1) - zAt(entry)).toFixed(1)}%"></span>
            <span class="stop" style="left:${zAt(stop).toFixed(1)}%"></span>
            <span class="tgt" style="left:${zAt(t1).toFixed(1)}%"></span>
            <span class="now" id="zNow" style="left:${zAt(last).toFixed(1)}%"></span>
          </div>
          <div class="b-zl"><span>Stop ${f(stop)}</span><span>Entry ${f(entry)}</span><span>Target 1 ${f(t1)}</span></div>
          <p class="b-p" style="font-size:13.5px">${esc(zoneState[2])}</p>
        </div>
      </section>

      <section class="b-sec b-reveal" id="b-chart">
        <div class="b-lab">Price structure</div>
        <h2 class="b-h2">Where price has been, and where the thesis ends.</h2>
        ${CH ? `<div class="b-px">
          <div class="b-px-h"><span>${esc(sig.symbol)} · daily closes</span>
            <span class="rgs" role="group" aria-label="Chart window">
              ${['3mo', '6mo', '1y'].map(r => `<button type="button" data-range="${r}"
                aria-pressed="${r === briefRange}">${r.replace('mo', 'M').replace('1y', '1Y')}</button>`).join('')}
            </span></div>
          ${CH.html}
          <div class="b-px-f">
            <span class="p"><i></i>Close</span><span class="e"><i></i>Entry</span>
            <span class="s"><i></i>Stop</span><span class="t"><i></i>Targets</span>
            ${Number.isFinite(N(row.sma200)) ? '<span class="k"><i></i>200-day</span>' : ''}
          </div>
        </div>
        <p class="b-cap"><b>Closing prices, not candles.</b> The line is ${pts.length} real daily closes
          from ${esc(pts[0].t || '')} to ${esc(pts[pts.length - 1].t || '')}. No feed on this site serves
          open-high-low-close data, so there are no candles and no wicks — a chart that drew them would be
          inventing the intraday range on the one page whose job is to be trusted. The levels over it are
          the published ones, on the same scale, so the distance between the stop and the target is the
          actual distance.</p>`
        : `<div class="b-px"><div class="b-px-h"><span>${esc(sig.symbol)} · daily closes</span>
             <span class="rgs"><span class="dstate nodata"><i></i>No history</span></span></div>
           <div style="padding:26px 16px"><p class="b-p" style="margin:0">The price series for this
             instrument did not load${ser.error ? ` — ${esc(ser.error)}` : ''}. The level map below is
             drawn from the published levels alone, which is what this section showed before a series
             was available at all.</p></div></div>`}

        <div class="b-chart" style="margin-top:${pts ? '26px' : '18px'}">
          <div class="b-ladder">
            <div class="b-band risk" style="top:${at(entry).toFixed(1)}%;height:${Math.abs(at(stop) - at(entry)).toFixed(1)}%"></div>
            <div class="b-band reward" style="top:${at(t2).toFixed(1)}%;height:${Math.abs(at(entry) - at(t2)).toFixed(1)}%"></div>
            ${ladder}
          </div>
          <p class="b-cap"><b>The ladder is every published level on one linear scale.</b> Hover or focus a
            level to read its distance from the current price, in currency and in per cent. Colour is never
            the only cue — each line is named.</p>
        </div>
      </section>

      <section class="b-sec b-reveal" id="b-thesis">
        <div class="b-lab">Why this trade</div>
        <h2 class="b-h2">Four things had to line up.</h2>
        <div class="b-steps">
          ${thesis.map(([lab, h, body, mini], i) => `<article class="b-step">
            <div class="b-step-n"><b>0${i + 1}</b> / ${esc(lab.toUpperCase())}</div>
            <div><h3>${esc(h)}</h3><p>${body}</p>
              <div class="b-mini">${(mini || []).map(([k, v]) =>
                `<div><span class="k">${esc(k)}</span><span class="v">${v}</span></div>`).join('')}</div>
            </div></article>`).join('')}
        </div>
      </section>

      <section class="b-sec b-reveal">
        <div class="b-lab">Signal score ${tip('confidence')}</div>
        <h2 class="b-h2">How the setup scores, component by component.</h2>
        <div class="b-conf">
          <div>
            <div class="b-dial" id="dial">
              <svg viewBox="0 0 120 120" role="img" aria-label="Confidence ${score == null ? 'unscored' : score + ' out of 100'}">
                <circle class="trk" cx="60" cy="60" r="50"/>
                ${(() => {
                  const R = 50, C = 2 * Math.PI * R;
                  const n = COMPS.length, seg = C / n, gap = 3;
                  const col = ['#4E9E72', '#7A9BEE', '#C9A961', '#98A0AB', '#C15F54'];
                  return COMPS.map(([nm, , v], i) => {
                    const len = Number.isFinite(v) ? (seg - gap) * (v / 100) : 0;
                    return `<circle class="seg" data-seg="${i}" cx="60" cy="60" r="${R}"
                      stroke="${col[i]}" stroke-dasharray="${len.toFixed(2)} ${(C - len).toFixed(2)}"
                      stroke-dashoffset="${(-i * seg).toFixed(2)}"><title>${esc(nm)}: ${Number.isFinite(v) ? Math.round(v) : 'not measured'}</title></circle>`;
                  }).join('');
                })()}
              </svg>
              <div class="b-dial-c"><b id="dialN" data-cv="">${score == null ? '—' : '0'}</b>
                <i id="dialL">${esc(conviction)}</i></div>
            </div>
            <div class="b-cbtns" role="group" aria-label="Confidence view">
              <button type="button" id="cvScore" aria-pressed="true">Score</button>
              <button type="button" id="cvComp" aria-pressed="false">Components</button>
            </div>
          </div>
          <div id="crows">
            ${COMPS.map(([nm, , v, why], i) => {
              const col = ['#4E9E72', '#7A9BEE', '#C9A961', '#98A0AB', '#C15F54'][i];
              return `<button type="button" class="b-crow" data-seg="${i}" aria-expanded="false">
                <span class="dot" style="background:${col}" aria-hidden="true"></span>
                <span><span class="nm">${esc(nm)}</span><span class="why">${esc(why)}</span>
                  <span class="tr"><i data-w="${Number.isFinite(v) ? v.toFixed(0) : 0}" style="background:${col}"></i></span></span>
                <span class="sc">${Number.isFinite(v) ? Math.round(v) : '—'}</span></button>`;
            }).join('')}
            <p class="b-p" style="font-size:13px">The score is the mean of the components that could be
              measured. A component with no data is left out rather than filled in${have.length < COMPS.length
                ? `, which is why the denominator here is <b style="color:var(--b-ink)">${have.length}</b>,
                   not ${COMPS.length} — ${COMPS.length - have.length} component${COMPS.length - have.length > 1 ? 's are' : ' is'}
                   unmeasured for this name`
                : `. All ${COMPS.length} could be measured for this name`}.</p>
          </div>
        </div>
      </section>

      <section class="b-sec b-reveal">
        <div class="b-lab">Confluence</div>
        <h2 class="b-h2">Which factors agree, and which do not.</h2>
        <div class="b-mx">
          <div class="b-mxh"><span>Factor</span><span>Bullish</span><span>Neutral</span><span>Bearish</span></div>
          ${MATRIX.map(([nm, st_, why], i) => `<button type="button" class="b-mxr" data-mx="${i}" aria-expanded="false">
            <span class="f">${esc(nm)}</span>
            ${[0, 1, 2].map(k => `<span class="c ${['bull', 'neu', 'bear'][k]} ${st_ === k ? 'hit' : ''}"
              >${st_ === k ? `<u aria-label="${['Bullish', 'Neutral', 'Bearish'][k]}"></u>` : '<u></u>'}</span>`).join('')}
            <span class="b-mxd"><span>${st_ == null ? 'Not measured — this factor has no data on the screen for this name, so it takes no stance.' : esc(why)}</span></span>
          </button>`).join('')}
        </div>
        <p class="b-p" style="font-size:13px">A stance is scored, not asserted: 60 and above reads bullish,
          40 to 60 neutral, below 40 bearish, on the same component scores shown above. Tap a row for the
          reason.</p>
      </section>

      <section class="b-sec b-reveal">
        <div class="b-lab">Market regime ${tip('regime')}</div>
        <h2 class="b-h2">Whether this is the kind of market the setup is built for.</h2>
        ${reg ? `<div class="b-rg">
          <div class="b-rgb"><h4>Structure</h4>
            <div class="b-rgv">${esc(reg.label)}</div>
            <div class="b-rgt"><i style="left:${reg.persist.toFixed(1)}%"></i></div>
            <div class="b-rgl"><span>Below the mean</span><span>Above the mean</span></div>
            <p class="b-rgn">Over the last ${reg.look} closes, price finished above its own trailing
              20-day average <b style="color:var(--b-ink)">${reg.persist.toFixed(0)}%</b> of the time.
              Sustained above 66% or below 34% reads as a trend; in between reads as a range.</p></div>
          <div class="b-rgb"><h4>Volatility</h4>
            <div class="b-rgv">${esc(reg.volLabel)}</div>
            <div class="b-rgt"><i style="left:${Math.max(0, Math.min(100, reg.vol / 60 * 100)).toFixed(1)}%"></i></div>
            <div class="b-rgl"><span>0%</span><span>60%+</span></div>
            <p class="b-rgn">Realised volatility is
              <b style="color:var(--b-ink)">${reg.vol.toFixed(1)}%</b> annualised — the standard deviation
              of daily returns across this window, scaled by the square root of 252.
              ${Number.isFinite(N(row.atr_pct)) ? `The screen's own daily ATR for this name is ${N(row.atr_pct).toFixed(2)}%.` : ''}</p></div>
        </div>`
        : `<p class="b-p">Regime is not measured for this name — it needs at least 30 daily closes and the
            series did not load. It is left blank rather than guessed from the levels.</p>`}
      </section>

      <section class="b-sec b-reveal" id="b-plan">
        <div class="b-lab">Scenarios</div>
        <h2 class="b-h2">Three ways this resolves.</h2>
        <div class="b-scb" role="group" aria-label="Scenario">
          <button type="button" class="bull" data-sc="0" aria-pressed="false">Bullish</button>
          <button type="button" class="base" data-sc="1" aria-pressed="true">Base case</button>
          <button type="button" class="bear" data-sc="2" aria-pressed="false">Bearish</button>
        </div>
        <div class="b-scp" id="scPane"></div>
        <p class="b-p" style="font-size:13px">These are scenarios, not forecasts. No probability is
          attached to any of them, because the engine publishes no probability model — what is shown
          instead is the ledger's own base rate over every closed signal, which describes the engine's
          history and not this trade.</p>
      </section>

      <section class="b-sec b-reveal">
        <div class="b-lab">Trade plan</div>
        <h2 class="b-h2">What to do, and when to stop doing it.</h2>
        <div class="b-plan">
          <div class="b-pr"><span class="st">Before entry</span>
            <span class="tx">Price holding the entry zone on a close, not an intraday wick. No entry if the stop is already broken.</span>
            <span class="px">—</span></div>
          <div class="b-pr"><span class="st">Entry</span>
            <span class="tx">${isShort ? 'Sell' : 'Buy'} at or better than the published level.</span>
            <span class="px">${f(entry)}</span></div>
          <div class="b-pr"><span class="st">Stop</span>
            <span class="tx">Invalidation. A close beyond this removes the reason for the trade.</span>
            <span class="px" style="color:var(--b-bear)">${f(stop)}</span></div>
          <div class="b-pr"><span class="st">Target 1</span>
            <span class="tx">First profit level. The published trailing rule moves the stop to entry once this prints.</span>
            <span class="px" style="color:var(--b-bull)">${f(t1)}</span></div>
          <div class="b-pr"><span class="st">Target 2</span>
            <span class="tx">Extended target, carried only by the remainder.</span>
            <span class="px" style="color:var(--b-bull)">${f(t2)}</span></div>
          <div class="b-pr is-invalid"><span class="st">Invalidation ${tip('invalidation')}</span>
            <span class="tx">Below ${f(stop)} the structure that produced this setup is gone. The position is
              closed at that price — not re-argued, not averaged into, not widened.</span>
            <span class="px" style="color:var(--b-bear)">${f(stop)}</span></div>
        </div>
      </section>

      <section class="b-sec b-reveal" id="b-risk">
        <div class="b-lab">Risk and position size ${tip('rr')}</div>
        <h2 class="b-h2">What this costs if it is wrong.</h2>
        <div class="b-rr">
          <div>
            <div class="b-rrb" id="rrBars">
              <div class="row"><span class="k">Loss</span><span class="b loss" id="barL"></span><span class="v" id="barLv" style="color:var(--b-bear)"></span></div>
              <div class="row"><span class="k">Gain T1</span><span class="b gain" id="barG"></span><span class="v" id="barGv" style="color:var(--b-bull)"></span></div>
              <div class="row"><span class="k">Gain T2</span><span class="b gain" id="barG2"></span><span class="v" id="barG2v" style="color:var(--b-bull)"></span></div>
            </div>
            <div class="b-metrics" style="margin-top:22px" id="rkOut"></div>
            <p class="b-p" style="font-size:13px">Position size is the risk amount divided by the distance
              from entry to stop, rounded down to whole shares. It is arithmetic on the numbers you set —
              not a recommendation, and it takes no account of your other positions, liquidity in the name,
              or what you can afford to lose.</p>
          </div>
          <div>
            <div class="b-sl">
              <div><label for="rkA">Account size<span class="lv" id="lvA"></span></label>
                <input id="rkA" type="number" value="1000000" min="0" step="10000"
                  style="width:100%;background:var(--b-hi);border:1px solid var(--b-line2);border-radius:7px;
                  color:var(--b-ink);font:500 15px/1 var(--mono);padding:12px 13px;min-height:44px"></div>
              <div><label for="rkP">Risk per trade<span class="lv" id="lvP"></span></label>
                <input id="rkP" type="range" min="0.1" max="5" step="0.1" value="1"></div>
              <!-- step="any", NOT a rounded step. A range input SNAPS its value to
                   min + n·step, so a step of 1.85 moved the published entry of
                   ₹1,847.40 to ₹1,847.79 the instant the page loaded — the
                   calculator then showed a risk per share that was not the
                   published one, and the simulation warning stayed silent
                   because the drift was under half a per cent. The published
                   levels have to survive first paint exactly. -->
              <div><label for="slE">Entry<span class="lv" id="lvE"></span></label>
                <input id="slE" type="range" min="${(entry * 0.85).toFixed(2)}" max="${(entry * 1.15).toFixed(2)}"
                  step="any" value="${entry}"></div>
              <div><label for="slS">Stop<span class="lv" id="lvS"></span></label>
                <input id="slS" type="range" min="${(Math.min(stop, entry) * 0.8).toFixed(2)}" max="${(Math.max(stop, entry) * 1.05).toFixed(2)}"
                  step="any" value="${stop}"></div>
              <div><label for="slT">Target 1<span class="lv" id="lvT"></span></label>
                <input id="slT" type="range" min="${(Math.min(t1, entry) * 0.95).toFixed(2)}" max="${(Math.max(t1, entry) * 1.4).toFixed(2)}"
                  step="any" value="${t1}"></div>
              <button type="button" class="b-reset" id="rkReset">Reset to published</button>
            </div>
            <p class="b-sim" id="rkSim" hidden>You are looking at a <b>simulation</b>. One or more levels
              have been moved off the published values, so the risk, reward and size below describe a trade
              this site has not signalled.</p>
          </div>
        </div>
      </section>

      <section class="b-sec b-reveal" id="b-history">
        <div class="b-lab">What the ledger records</div>
        <h2 class="b-h2">This signal's own paper trail.</h2>
        <div class="b-tl" id="tl">
          ${[
            [String(sig.alert_date || sig.date || '').slice(0, 10),
             `<b>Signal published.</b> ${esc(sig.signal_type || 'engine')} engine, ${esc(sig.timeframe || '1D')} timeframe, entry ${f(entry)} with the stop at ${f(stop)}.`],
            sig.sent_at ? [String(sig.sent_at).slice(0, 10) + ' ' + String(sig.sent_at).slice(11, 16),
             `<b>Sent.</b> The alert left the engine at this time and has not been amended since.`] : null,
            [ageDays == null ? '—' : ageDays + ' days',
             `<b>Still open.</b> The ledger carries no exit for this row, so it is marked open and counts toward no closed result yet.`],
            [f(last),
             `<b>Marked at the current price.</b> ${Number.isFinite(last) && Number.isFinite(entry)
               ? `That is ${pct((last - entry) / entry * 100)} against the published entry — unrealised, and not a booked result.` : 'No current price is available.'}`],
          ].filter(Boolean).map(([w, t]) => `<div class="b-tli"><span class="w">${esc(w)}</span><span class="t">${t}</span></div>`).join('')}
        </div>
        <p class="b-p" style="font-size:13px">The ledger records when a signal was generated, when it was
          sent, and how it closed. It does <b style="color:var(--b-ink)">not</b> record intraday development
          — there is no row saying momentum confirmed at 09:24 — so none is shown. A timeline of events that
          were never logged would be a story, not a record.</p>
      </section>

      <section class="b-sec b-reveal">
        <div class="b-lab">Signal history</div>
        <h2 class="b-h2">The record, including the part that hurts.</h2>
        ${H ? `<div class="b-metrics" style="margin-top:22px">
          <div class="b-m"><span class="k">Closed</span><span class="v">${H.trades}</span></div>
          <div class="b-m"><span class="k">Win rate</span><span class="v">${H.win_rate}%</span></div>
          <div class="b-m"><span class="k">Wins</span><span class="v up">${H.wins}</span></div>
          <div class="b-m"><span class="k">Losses</span><span class="v dn">${H.losses}</span></div>
          <div class="b-m"><span class="k">Expectancy</span><span class="v ${H.expectancy_r >= 0 ? 'up' : 'dn'}">${H.expectancy_r}R</span></div>
        </div>
        <p class="b-p">${tip('expectancy')} Measured over ${H.trades} closed signals since ${esc((S.totals || {}).first_date || '')}.
          Expectancy is <b style="color:var(--b-ink)">${H.expectancy_r}R</b>${H.expectancy_r < 0
            ? ' — the engine is currently losing money per trade on this sample, and that is published here for the same reason the winners are.'
            : ' per closed trade on this sample.'}</p>` : ''}
        ${closedRows.length ? `<div class="b-hist"><table>
          <thead><tr><th>Date</th><th>Asset</th><th>Direction</th>
            <th class="num">Entry</th><th class="num">Exit</th><th>Result</th><th class="num">P&amp;L</th></tr></thead>
          <tbody>${closedRows.map(r => `<tr>
            <td>${esc(String(r.alert_date || r.date || '').slice(0, 10))}</td>
            <td style="color:var(--b-ink)">${esc(r.symbol)}</td>
            <td>${esc(r.action || '')}</td>
            <td class="num">${esc(r.entry ?? '—')}</td>
            <td class="num">${esc(r.exit_price ?? '—')}</td>
            <td style="color:${(r.badge === 'win') ? 'var(--b-bull)' : 'var(--b-bear)'}">${esc(r.status || r.badge || '')}</td>
            <td class="num" style="color:${Number(r.pnl_pct) > 0 ? 'var(--b-bull)' : 'var(--b-bear)'}">${pct(r.pnl_pct)}</td>
          </tr>`).join('')}</tbody></table></div>` : ''}
        <div class="b-disc"><b>Past performance does not guarantee future results.</b>
          This is a published research setup, not advice and not a recommendation to buy or sell.
          Every figure is drawn from this site's own ledger, its own screen and real published closing
          prices. A signal is a thesis with a defined invalidation — it is not a forecast, and it carries
          no guarantee of any outcome. Position sizing is your decision and your risk.</div>
      </section>
    </div></div>`);

    /* ══ WIRING ═══════════════════════════════════════════════════════════
     * Everything below runs after paint and touches only opacity, transform
     * and width. No layout is animated, nothing runs on an idle loop, and
     * every listener is attached to an element that exists in this paint —
     * the route re-paints wholesale, so the old ones go with the old nodes. */

    const $ = id => document.getElementById(id);

    /* ── THE LADDER'S LABELS, SPACED IN REAL PIXELS ─────────────────────────
     * The ladder is a linear price scale, so two levels a rupee apart land two
     * pixels apart. On JKTYRE the current price (₹380.25) and the entry
     * (₹378.75) are 0.4% apart across a ₹363-599 range and their labels printed
     * exactly on top of each other.
     *
     * Only the TEXT moves. Every rule stays on its true price, because that is
     * the one claim this chart makes — that the distance between the stop and
     * the target is the actual distance. A reader still sees two lines almost
     * touching, and can now read both numbers.
     *
     * Measured rather than computed in percent: the box is clamp(300px,40vw,
     * 400px), so its height is only knowable after layout, and the required
     * gap is the label's own rendered height rather than a guessed fraction.
     */
    const spaceLadder = () => {
      const box = main.querySelector('.b-ladder');
      if (!box) return;
      const rows = [...box.querySelectorAll('.b-lvl')];   // DOM order = high → low
      if (rows.length < 2) return;
      rows.forEach(r => r.style.setProperty('--lnudge', '0px'));
      const H = box.clientHeight;
      if (!H) return;
      const tag = rows[0].querySelector('.b-lvl-tag');
      const gap = Math.max(14, (tag ? tag.getBoundingClientRect().height : 12) + 3);
      const tops = rows.map(r => parseFloat(r.style.top) / 100 * H);
      /* A label is centred on its row, so its own half-height is the margin it
       * needs at each end. Without this the bottom label hung 19px below the
       * box and sat 3px off the caption — the crowding just moved rather than
       * being resolved.
       *
       * Two passes, the standard shape: push down until nothing overlaps, then,
       * if the pile has run past the bottom, pull back up from the last row.
       * Each pass clamps to the usable band, so labels stay inside the box. */
      const half = gap / 2;
      const lo = half, hi = Math.max(half, H - half);
      const lab = tops.slice();
      lab[0] = Math.max(lo, lab[0]);
      for (let i = 1; i < lab.length; i++) {
        lab[i] = Math.max(lab[i], lab[i - 1] + gap);
      }
      if (lab[lab.length - 1] > hi) {
        lab[lab.length - 1] = hi;
        for (let i = lab.length - 2; i >= 0; i--) {
          lab[i] = Math.min(lab[i], lab[i + 1] - gap);
        }
      }
      rows.forEach((r, i) => r.style.setProperty('--lnudge', (lab[i] - tops[i]).toFixed(1) + 'px'));
    };
    setTimeout(spaceLadder, 90);
    // The box is sized in vw, so its height changes with the window. Debounced,
    // and torn down with the route — resize fires in bursts.
    let ladderT = 0;
    const onResize = () => { clearTimeout(ladderT); ladderT = setTimeout(spaceLadder, 120); };
    window.addEventListener('resize', onResize);

    /* ── numbers arrive, they do not appear ─────────────────────────────── */
    if (score != null) {
      countTo($('convN'), score, { dp: 0 });
      countTo($('dialN'), score, { dp: 0 });
    }
    setTimeout(() => {
      main.querySelectorAll('.b-crow .tr i').forEach(i => { i.style.width = i.dataset.w + '%'; });
    }, 80);

    /* ── the reveal, and the chart story it drives ──────────────────────────
     * IntersectionObserver where it works, and a hard failsafe where it does
     * not: an observer that never fires would leave the whole page at opacity
     * 0, which is a blank screen, not a subtle animation. */
    const reveals = [...main.querySelectorAll('.b-reveal')];
    const tls = [...main.querySelectorAll('.b-tli')];
    const ovs = [...main.querySelectorAll('.b-ov')];
    const revealAll = () => {
      reveals.forEach(e => e.classList.add('in'));
      tls.forEach(e => e.classList.add('in'));
      ovs.forEach(e => e.classList.add('on'));
    };
    // The chart's overlays come on in the order the argument is made: the
    // price line, then the levels that frame it, then the risk band, then the
    // reward band. Step 1 is always on — it is the price itself.
    const stepOn = n => ovs.forEach(e => { if (Number(e.dataset.ov) <= n) e.classList.add('on'); });

    if ('IntersectionObserver' in window && !REDUCED) {
      const io = new IntersectionObserver(es => es.forEach(e => {
        if (!e.isIntersecting) return;
        e.target.classList.add('in');
        // Each section that arrives advances the chart one more step.
        const step = Number(e.target.dataset.step);
        if (Number.isFinite(step)) stepOn(step);
        io.unobserve(e.target);
      }), { rootMargin: '0px 0px -12% 0px', threshold: .08 });
      reveals.forEach((e, i) => { e.dataset.step = String(3 + i); io.observe(e); });
      tls.forEach(e => io.observe(e));
      // If nothing has revealed within 2.5s the observer is not firing — a
      // hidden tab, a prerender, a browser that throttles it. Show everything.
      setTimeout(() => { if (!main.querySelector('.b-reveal.in')) revealAll(); }, 2500);
      // The chart's own levels never wait on scroll: the reader who lands on
      // #b-chart directly must see them.
      setTimeout(() => stepOn(2), 400);
    } else { revealAll(); }

    /* ── quick nav: where you are, and the keys that take you there ─────── */
    /* Three sticky layers stack above the content on a desk — the bar, the
     * route tabs and this brief's own section nav. A fixed 96px offset cleared
     * two of them and parked the section label underneath the third. The
     * offset is measured from the elements themselves so it stays correct when
     * any of the three changes height. */
    const stickyOffset = () => {
      let h = 0;
      for (const sel of ['.bar', '.tabs', '#qnav']) {
        const e = document.querySelector(sel);
        if (e && getComputedStyle(e).position === 'sticky') h += e.getBoundingClientRect().height;
      }
      return h + 18;
    };
    const jump = id => {
      const el = $(id); if (!el) return;
      const top = el.getBoundingClientRect().top + window.scrollY - stickyOffset();
      window.scrollTo({ top, behavior: REDUCED ? 'auto' : 'smooth' });
    };
    const qlinks = [...main.querySelectorAll('#qnav a')];
    const jnodes = [...main.querySelectorAll('.b-jn')];
    qlinks.forEach(a => a.addEventListener('click', ev => { ev.preventDefault(); jump(a.dataset.jump); }));

    // One scroll listener for the whole page, throttled to one frame. Reading
    // getBoundingClientRect for six elements per frame is cheap; doing it per
    // element per scroll event is not.
    let ticking = false;
    const markActive = () => {
      ticking = false;
      let active = SECTIONS[0][0];
      for (const [id] of SECTIONS) {
        const el = $(id); if (!el) continue;
        if (el.getBoundingClientRect().top <= stickyOffset() + 24) active = id;
      }
      qlinks.forEach(a => a.setAttribute('aria-current', a.dataset.jump === active ? 'true' : 'false'));
      let seen = false;
      jnodes.forEach(n => {
        const isNow = n.dataset.node === active;
        n.classList.toggle('on', isNow);
        n.classList.toggle('done', !seen && !isNow);
        if (isNow) seen = true;
      });
    };
    const onScroll = () => { if (!ticking) { ticking = true; requestAnimationFrame(markActive); } };
    window.addEventListener('scroll', onScroll, { passive: true });
    markActive();

    // 1–6 jump to a section, Escape closes any open overlay. Ignored while a
    // field has focus, so typing "1" into the account box does not navigate.
    const onKey = ev => {
      if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
      const t = ev.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
      const i = '123456'.indexOf(ev.key);
      if (i >= 0 && SECTIONS[i]) { ev.preventDefault(); jump(SECTIONS[i][0]); }
    };
    document.addEventListener('keydown', onKey);
    // The route repaints wholesale; the listener must not outlive its markup.
    main.addEventListener('sig:teardown', () => {
      window.removeEventListener('scroll', onScroll);
      window.removeEventListener('resize', onResize);
      clearTimeout(ladderT);
      document.removeEventListener('keydown', onKey);
    }, { once: true });

    /* ── the ladder reads its own distances ─────────────────────────────── */
    main.querySelectorAll('.b-lvl').forEach(el => {
      const on = () => el.classList.add('hot'), off = () => el.classList.remove('hot');
      el.addEventListener('pointerenter', on); el.addEventListener('pointerleave', off);
      el.addEventListener('focus', on); el.addEventListener('blur', off);
    });

    /* ── the chart: crosshair, read-out, and the window switch ──────────── */
    if (CH) {
      const hit = $('pxhit'), tip = $('pxt'), dot = $('pxdot'), cross = $('pxcross');
      const read = clientX => {
        const b = hit.getBoundingClientRect();
        const k = Math.max(0, Math.min(1, (clientX - b.left) / (b.width || 1)));
        const i = Math.round(k * (pts.length - 1));
        const p = pts[i];
        if (!p) return;
        const xPct = (i / Math.max(1, pts.length - 1)) * 100;
        const yPct = (CH.Y(p.c) / CH.H) * 100;
        cross.setAttribute('x1', ((i / Math.max(1, pts.length - 1)) * CH.W).toFixed(1));
        cross.setAttribute('x2', ((i / Math.max(1, pts.length - 1)) * CH.W).toFixed(1));
        cross.style.opacity = '1';
        dot.style.left = xPct + '%'; dot.style.top = yPct + '%'; dot.style.opacity = '1';
        const dpc = i > 0 && pts[i - 1].c ? (p.c / pts[i - 1].c - 1) * 100 : null;
        tip.innerHTML = `<i>${esc(p.t || '')}</i>${f(p.c)}` +
          (dpc == null ? '' : ` <em>${pct(dpc)}</em>`) +
          `<br><em>vs entry ${pct((p.c - entry) / entry * 100)}</em>`;
        tip.classList.add('on');
        // Flip the card to the other side near the right edge so it never
        // hangs off the chart.
        tip.style.left = xPct > 62 ? 'auto' : `calc(${xPct}% + 14px)`;
        tip.style.right = xPct > 62 ? `calc(${(100 - xPct)}% + 14px)` : 'auto';
      };
      const clear = () => { tip.classList.remove('on'); dot.style.opacity = '0'; cross.style.opacity = '0'; };
      hit.addEventListener('pointermove', e => read(e.clientX));
      hit.addEventListener('pointerdown', e => read(e.clientX));
      hit.addEventListener('pointerleave', clear);
      hit.addEventListener('pointercancel', clear);

      main.querySelectorAll('.b-px-h .rgs button').forEach(b =>
        b.addEventListener('click', () => { briefRange = b.dataset.range; R['/brief'](); }));
    }

    /* ── confidence: segments, rows, and the two views ──────────────────── */
    const segs = [...main.querySelectorAll('.b-dial .seg')];
    const crows = [...main.querySelectorAll('.b-crow')];
    const focusSeg = i => {
      segs.forEach((s, k) => { s.classList.toggle('hot', k === i); s.classList.toggle('dim', i != null && k !== i); });
      crows.forEach((r, k) => r.classList.toggle('dim', i != null && k !== i));
      const dl = $('dialL'), dn = $('dialN');
      if (i == null) { dl.textContent = conviction; if (score != null) countTo(dn, score, { dp: 0 }); }
      else {
        dl.textContent = COMPS[i][0].toUpperCase();
        const v = COMPS[i][2];
        if (Number.isFinite(v)) countTo(dn, Math.round(v), { dp: 0 }); else dn.textContent = '—';
      }
    };
    segs.forEach((s, i) => {
      s.addEventListener('pointerenter', () => focusSeg(i));
      s.addEventListener('pointerleave', () => focusSeg(null));
    });
    crows.forEach((r, i) => {
      r.addEventListener('pointerenter', () => focusSeg(i));
      r.addEventListener('pointerleave', () => focusSeg(null));
      r.addEventListener('click', () => {
        const open = r.classList.toggle('open');
        r.setAttribute('aria-expanded', open ? 'true' : 'false');
      });
      r.addEventListener('focus', () => focusSeg(i));
      r.addEventListener('blur', () => focusSeg(null));
    });
    const cvS = $('cvScore'), cvC = $('cvComp');
    const setView = comp => {
      cvS.setAttribute('aria-pressed', comp ? 'false' : 'true');
      cvC.setAttribute('aria-pressed', comp ? 'true' : 'false');
      segs.forEach(s => { s.style.strokeWidth = comp ? '15' : ''; });
      crows.forEach(r => {
        r.classList.toggle('open', comp);
        r.setAttribute('aria-expanded', comp ? 'true' : 'false');
      });
    };
    cvS.addEventListener('click', () => setView(false));
    cvC.addEventListener('click', () => setView(true));

    /* ── confluence rows ────────────────────────────────────────────────── */
    main.querySelectorAll('.b-mxr').forEach(r => r.addEventListener('click', () => {
      const open = r.classList.toggle('open');
      r.setAttribute('aria-expanded', open ? 'true' : 'false');
    }));

    /* ── scenarios ──────────────────────────────────────────────────────── */
    const baseRate = H && Number.isFinite(Number(H.win_rate)) ? Number(H.win_rate) : null;
    const SC = [
      ['Continuation through both targets.',
       `Price clears ${f(t1)} and carries to ${f(t2)}. That needs the structure that produced this setup to hold — the 50-day above the 200-day, volume staying at or above its recent average, and no close back under ${f(entry)}.`,
       [['Requires', `Above ${f(t1)}`], ['Target', f(t2)], ['Move from here', Number.isFinite(last) ? pct((t2 - last) / last * 100) : '—'],
        ['R multiple', ((Math.abs(t2 - entry)) / (risk || 1)).toFixed(1) + 'R']]],
      ['The published plan, run as written.',
       `Entry at ${f(entry)}, first target ${f(t1)}, stop ${f(stop)}. On the published trailing rule the stop moves to entry once ${f(t1)} prints, so the remainder rides to ${f(t2)} with no capital at risk.`,
       [['Requires', `Entry at or better than ${f(entry)}`], ['Target', f(t1)],
        ['Move from here', Number.isFinite(last) ? pct((t1 - last) / last * 100) : '—'],
        ['R multiple', rrT1.toFixed(1) + 'R']]],
      ['The stop does its job.',
       `A close beyond ${f(stop)} and the position closes for a defined loss of ${f(risk)} a share. This is the outcome the whole structure is built to make survivable: it is a known number decided before entry, not a decision taken while losing.`,
       [['Requires', `Close beyond ${f(stop)}`], ['Loss', '−' + f(risk) + ' / share'],
        ['Move from here', Number.isFinite(last) ? pct((stop - last) / last * 100) : '—'],
        ['R multiple', '−1.0R']]],
    ];
    const scPane = $('scPane');
    const drawSc = i => {
      const [h, body, grid] = SC[i];
      scPane.innerHTML = `<h4>${esc(h)}</h4><p>${esc(body)}</p>
        <div class="b-scg">${grid.map(([k, v]) => `<div><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`).join('')}
          <div><span class="k">Engine base rate</span><span class="v">${baseRate == null ? '—' : baseRate + '%'}</span></div></div>
        ${baseRate == null ? '' : `<p style="font-size:12.5px;color:var(--b-dim);margin-top:14px;line-height:1.6">
          ${baseRate}% is the share of <b style="color:var(--b-mut)">all ${H.trades} closed signals</b> that
          ended in profit. It describes the engine's history, not this trade, and it is the same number
          whichever scenario is selected.</p>`}`;
      main.querySelectorAll('.b-scb button').forEach((b, k) =>
        b.setAttribute('aria-pressed', k === i ? 'true' : 'false'));
    };
    main.querySelectorAll('.b-scb button').forEach(b =>
      b.addEventListener('click', () => drawSc(Number(b.dataset.sc))));
    drawSc(1);

    /* ── risk, reward and size — one calculator, live ───────────────────── */
    const ids = ['rkA', 'rkP', 'slE', 'slS', 'slT'];
    const calc = () => {
      const acct = Number($('rkA').value) || 0;
      const rp = Number($('rkP').value) || 0;
      const e2 = Number($('slE').value), s2 = Number($('slS').value), t1b = Number($('slT').value);
      const risk2 = Math.abs(e2 - s2);
      const rr2 = risk2 > 0 ? Math.abs(t1b - e2) / risk2 : 0;
      const amt = acct * rp / 100;
      const qty = risk2 > 0 ? Math.floor(amt / risk2) : 0;
      const loss = qty * risk2, g1 = qty * Math.abs(t1b - e2), g2 = qty * Math.abs(t2 - e2);

      $('lvA').textContent = f(acct);
      $('lvP').textContent = rp.toFixed(1) + '% · ' + f(amt);
      $('lvE').textContent = f(e2); $('lvS').textContent = f(s2); $('lvT').textContent = f(t1b);

      const peak = Math.max(loss, g1, g2, 1);
      $('barL').style.width = (loss / peak * 100).toFixed(1) + '%';
      $('barG').style.width = (g1 / peak * 100).toFixed(1) + '%';
      $('barG2').style.width = (g2 / peak * 100).toFixed(1) + '%';
      countTo($('barLv'), loss, { dp: 0, pre: '−' + cur });
      countTo($('barGv'), g1, { dp: 0, pre: '+' + cur });
      countTo($('barG2v'), g2, { dp: 0, pre: '+' + cur });

      $('rkOut').innerHTML = `
        <div class="b-m"><span class="k">Position size</span><span class="v">${qty.toLocaleString('en-IN')} sh</span></div>
        <div class="b-m"><span class="k">Notional</span><span class="v">${f(qty * e2)}</span></div>
        <div class="b-m"><span class="k">Risk amount</span><span class="v">${f(amt)}</span></div>
        <div class="b-m"><span class="k">R:R to T1</span><span class="v gold">${rr2.toFixed(1)} : 1</span></div>
        <div class="b-m"><span class="k">Risk per share</span><span class="v">${f(risk2)}</span></div>
        <div class="b-m"><span class="k">Of account</span><span class="v">${acct > 0 ? (qty * e2 / acct * 100).toFixed(1) + '%' : '—'}</span></div>`;

      // Say so, loudly, the moment the numbers stop being the published ones.
      // A tenth of a per cent, not half of one. The wider tolerance existed to
      // absorb the slider's own snapping; with step="any" there is nothing to
      // absorb, and a reader who has moved a level deserves to be told.
      const tol = 0.001 * entry;
      const moved = Math.abs(e2 - entry) > tol || Math.abs(s2 - stop) > tol
                 || Math.abs(t1b - t1) > tol;
      $('rkSim').hidden = !moved;
    };
    ids.forEach(id => $(id).addEventListener('input', calc));
    $('rkReset').addEventListener('click', () => {
      $('slE').value = entry; $('slS').value = stop; $('slT').value = t1; calc();
    });
    calc();
  };


  /* ── router ────────────────────────────────────────────────────────────── */
  const routeOf = () => {
    const h = (location.hash || '#/').replace(/^#/, '');
    return R[h] ? h : '/';
  };

  async function render() {
    const path = routeOf();
    // Leaving the brief forgets which signal was pinned, so coming back by the
    // tab picks the best current setup rather than resurrecting an old one.
    if (path !== '/brief') briefPick = null;
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
  // Light is the default; the toggle is the only thing that changes it, and
  // the choice persists. Deliberately not following prefers-color-scheme: the
  // page is designed light first, and an OS set to dark should not silently
  // serve a different design than the one a first-time reader is shown.
  const saved = (() => { try { return localStorage.getItem('sig:theme'); } catch (e) { return null; } })();
  root.setAttribute('data-theme', saved === 'dark' ? 'dark' : 'light');
  document.getElementById('themeBtn').addEventListener('click', () => {
    const next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    document.querySelector('meta[name="theme-color"]').setAttribute('content', next === 'dark' ? '#0B0F14' : '#FFFFFF');
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
