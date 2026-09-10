/* ════════════════════════════════════════════════════════════════════════════
   brief_fundamentals.js — the Business section of the trading brief.
   ONE implementation, loaded by both briefs.

   WHY THIS FILE EXISTS, AND WHY IT IS THE ONLY SHARED FRONTEND FILE.

   There are two briefs: signal.askakshay.com/brief (public/signal.js in the
   signal repo) and news.askakshay.com/next.html#/brief (static/next.js here).
   They are forks of one renderer and the first has moved a long way ahead.

   The signal repo's sync-data.yml used to mirror static/next.{js,css} into it
   twice a day and that step was REMOVED, with the reason written down: the two
   copies had drifted, both repos had passing tests, nothing caught it — and a
   mirror pointing inward would silently overwrite work done in the repo that
   now owns the frontend.

   That objection is about direction and about a file with two authors. It does
   not apply here. This file has exactly one author — this repo — for the same
   reason the fourteen JSON feeds do: it renders q / g / v / tech, roce, de,
   piotroski, pe_pctile and risk.flags, every one of which is computed by
   stock_screen.py twenty feet away. The renderer for those fields belongs
   beside the thing that produces them, and nothing downstream edits it.

   So: authored here, published to docs/ by generate.py, and mirrored into the
   signal repo by its sync-data.yml alongside the feeds. Both briefs call
   BriefFundamentals.render(). Neither owns a copy.

   ── IT CARRIES ITS OWN STYLES, ON PURPOSE ──────────────────────────────────

   A companion .css would need allow-listing in four places here and a fifth
   over there, and this repo has already shipped a 404 for exactly that (see
   the note in vercel-news/build.js: "a docs/ file needs allow-listing in THREE
   places... today.json had the first two and still 404'd"). One file cannot be
   half-delivered, and the markup can never reach a page whose stylesheet did
   not. The style block is injected once and is idempotent.

   Every custom property is written with a literal fallback, because the two
   sites do not share a token scale: signal.css declares --t-1..--t-10 and
   --r-1..--r-5, next.css writes its sizes as literals and has none of them. A
   bare var(--t-4) resolves to nothing on next.html and the rule is dropped.

   ── AND IT INVENTS NOTHING ─────────────────────────────────────────────────

   Every figure is read from the screen row the caller already downloaded. A
   missing measurement renders as the WORD for its absence and leaves the score
   that would have used it — it is never carried as a zero, which reads as a
   measured result of zero. That is the rule stock_screen.py holds, and this is
   the display side of it.

   Depends on nothing. No modules, no globals but its own.
   ════════════════════════════════════════════════════════════════════════════ */
(function (root) {
  'use strict';

  var STYLE_ID = 'bf-style';

  /* Own escaping rather than taking the host page's. Both callers have an
     esc(), they are not identical, and an injected one is a second thing that
     can be forgotten at a call site. */
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  /* NULL, EMPTY AND NaN ARE ALL ABSENT, AND Number(null) IS 0.
     The same coercion that put "TARGET 2  ₹0.00" on a live card would put a
     return on capital of 0.0% here, which reads as a company that earns
     nothing rather than one that did not report. */
  function num(v) {
    if (v === null || v === undefined || v === '') return null;
    var x = Number(v);
    return (typeof x === 'number' && isFinite(x)) ? x : null;
  }

  var NA = '<i class="b-na">not published</i>';

  function one(v, dp, suf) {
    var n = num(v);
    return n === null ? NA : n.toFixed(dp === undefined ? 1 : dp) + (suf || '');
  }

  function clamp100(n) { return Math.max(0, Math.min(100, n)); }

  function injectStyle(doc) {
    if (!doc || doc.getElementById(STYLE_ID)) return;
    var el = doc.createElement('style');
    el.id = STYLE_ID;
    el.textContent = CSS;
    (doc.head || doc.documentElement).appendChild(el);
  }

  var CSS = [
    /* Mobile-first throughout: every grid starts as one column and splits only
       when there is width for it. This section is read on a phone more often
       than anywhere else, and a six-column table at 390px is a horizontal
       scrollbar wearing a layout. */
    '.b-na{font-style:normal;font-family:var(--ui,system-ui);font-size:var(--t-3,12px);color:var(--b-dim,#6E7681)}',
    '.b-warn{font:500 var(--t-2,11px)/1 var(--mono,monospace);letter-spacing:.05em;',
    '  text-transform:uppercase;color:var(--b-bear,#AE3325);margin-left:6px}',

    '.bf-score{display:grid;gap:30px;margin-top:26px}',
    '@media(min-width:820px){.bf-score{grid-template-columns:230px 1fr;gap:52px;align-items:center}}',
    '.bf-big{font:400 clamp(56px,9vw,96px)/1 var(--b-serif,Georgia,serif);letter-spacing:-.02em;color:var(--b-ink,#111)}',
    '.bf-big span{font-size:.32em;color:var(--b-dim,#6E7681);letter-spacing:0}',
    '.bf-bars{display:grid;gap:16px}',
    '.bf-bar{display:grid;grid-template-columns:104px 1fr 38px;gap:14px;align-items:center}',
    '.bf-bar .n{font-size:var(--t-5,14px);color:var(--b-mut,#555)}',
    '.bf-bar .t{height:2px;background:var(--b-hi,#eee);position:relative;overflow:hidden}',
    '.bf-bar .t i{display:block;height:100%;background:var(--b-acc,#3b6fd4);width:0;',
    '  transition:width 1s cubic-bezier(.2,.8,.2,1)}',
    '.bf-bar .s{font:500 var(--t-4,13px)/1 var(--mono,monospace);text-align:right;font-variant-numeric:tabular-nums}',
    '@media(prefers-reduced-motion:reduce){.bf-bar .t i{transition:none}}',

    /* Valuation against its own record. A dot on a track, not a gauge: the
       reader is being told a position in a range, and a range is a line. */
    '.bf-pctl{margin-top:30px;padding:20px;border:1px solid var(--b-line,#e4e4e4);',
    '  border-radius:var(--r-4,12px);background:var(--b-sur,#fafafa)}',
    '.bf-pctl-h{display:flex;flex-wrap:wrap;gap:8px;justify-content:space-between;align-items:baseline}',
    '.bf-pctl-h b{font:500 var(--t-5,14px)/1.3 var(--ui,system-ui);color:var(--b-ink,#111)}',
    '.bf-pctl-h span{font:500 var(--t-4,13px)/1 var(--mono,monospace);color:var(--b-acc,#3b6fd4);font-variant-numeric:tabular-nums}',
    '.bf-pctl-t{position:relative;height:3px;background:var(--b-hi,#eee);border-radius:2px;margin:16px 0 8px}',
    '.bf-pctl-t i{position:absolute;top:50%;width:11px;height:11px;margin:-5.5px 0 0 -5.5px;',
    '  border-radius:50%;background:var(--b-acc,#3b6fd4)}',
    '.bf-pctl-l{display:flex;justify-content:space-between;font:500 var(--t-1,10px)/1 var(--mono,monospace);',
    '  letter-spacing:.05em;text-transform:uppercase;color:var(--b-dim,#6E7681)}',
    '.bf-pctl-n{margin:14px 0 0;font-size:var(--t-4,13px);line-height:1.65;color:var(--b-mut,#555)}',

    /* Six small tables rather than one wide one. Twenty rows under a single
       heading makes a reader hold the grouping in their head; six named blocks
       reflow to one column and stay legible at every width. */
    '.bf-tbls{display:grid;gap:1px;background:var(--b-line,#e4e4e4);border:1px solid var(--b-line,#e4e4e4);',
    '  border-radius:var(--r-4,12px);overflow:hidden;margin-top:30px}',
    '@media(min-width:640px){.bf-tbls{grid-template-columns:1fr 1fr}}',
    '@media(min-width:1040px){.bf-tbls{grid-template-columns:repeat(3,1fr)}}',
    '.bf-tbl{background:var(--b-card,var(--b-sur,#fff));padding:20px}',
    '.bf-tbl h4{margin:0 0 12px;font:500 var(--t-1,10px)/1 var(--mono,monospace);letter-spacing:.14em;',
    '  text-transform:uppercase;color:var(--b-dim,#6E7681)}',
    '.bf-tbl table{width:100%;border-collapse:collapse}',
    '.bf-tbl tr{border-top:1px solid var(--b-line,#e4e4e4)}',
    '.bf-tbl tr:first-child{border-top:0}',
    '.bf-tbl th{text-align:left;font:400 var(--t-4,13px)/1.4 var(--ui,system-ui);color:var(--b-mut,#555);',
    '  padding:9px 10px 9px 0;font-weight:400}',
    '.bf-tbl td{text-align:right;font:500 var(--t-4,13px)/1.4 var(--mono,monospace);color:var(--b-ink,#111);',
    '  padding:9px 0;font-variant-numeric:tabular-nums;white-space:nowrap}',

    /* The screen's objections, kept as objections. Severity is carried by a
       rule down the left AND by the word beside it — colour is never the only
       cue anywhere else on these pages and is not the only cue here. */
    '.bf-flags{margin-top:30px;padding:22px;border:1px solid var(--b-line,#e4e4e4);',
    '  border-radius:var(--r-4,12px);background:var(--b-sur,#fafafa)}',
    '.bf-flags h4{margin:0 0 14px;font:500 var(--t-1,10px)/1 var(--mono,monospace);letter-spacing:.14em;',
    '  text-transform:uppercase;color:var(--b-dim,#6E7681)}',
    '.bf-flags ul{list-style:none;margin:0;padding:0;display:grid;gap:12px}',
    '.bf-flags li{padding-left:14px;border-left:2px solid var(--b-line2,#ccc)}',
    '.bf-flags li.sev-high{border-left-color:var(--b-bear,#AE3325)}',
    '.bf-flags li.sev-med{border-left-color:var(--b-gold,#8A6208)}',
    '.bf-flags li b{display:block;font:500 var(--t-5,14px)/1.45 var(--ui,system-ui);color:var(--b-ink,#111)}',
    '.bf-flags li span{display:block;margin-top:3px;font:500 var(--t-3,12px)/1.4 var(--mono,monospace);color:var(--b-dim,#6E7681)}',
    '.bf-flags a{color:var(--b-acc,#3b6fd4);text-decoration:underline;text-underline-offset:2px}',
    '.bf-riskg{display:flex;flex-wrap:wrap;gap:10px;align-items:baseline;margin:0 0 14px}',
    '.bf-riskg b{font:600 var(--t-2,11px)/1 var(--mono,monospace);letter-spacing:.05em;',
    '  padding:4px 8px;border-radius:var(--r-1,4px);background:var(--b-hi,#eee);color:var(--b-mut,#555)}',
    '.bf-riskg b.rg-high{background:rgba(174,51,37,.14);color:var(--b-bear,#AE3325)}',
    '.bf-riskg b.rg-medium,.bf-riskg b.rg-med{background:rgba(138,98,8,.16);color:var(--b-gold,#8A6208)}',
    '.bf-riskg span{font:500 var(--t-3,12px)/1 var(--mono,monospace);color:var(--b-dim,#6E7681)}',
    '.bf-note{margin-top:20px;font-size:var(--t-3,12px);line-height:1.6;color:var(--b-dim,#6E7681);max-width:70ch}',
    '.bf-sub{font-size:var(--t-5,14px);line-height:1.6;color:var(--b-mut,#555);max-width:70ch;margin-top:12px}',
  ].join('\n');

  /* ── THE SECTION ─────────────────────────────────────────────────────────
     row   one row of the 750-name screen (screen.json or screen-lite.json)
     opts  { symbol, screenHref, headingTag }
           screenHref  where "open it on the screen" should go. The two sites
                       route differently — /screen?q=SYM against #/screen — and
                       guessing produces a link that renders the front page and
                       looks like the click was ignored. Omit it and no link is
                       offered, which is better than a wrong one.
           headingTag  'h2' by default; the callers' section headings differ. */
  function render(row, opts) {
    row = row || {};
    opts = opts || {};
    var sym = String(opts.symbol || row.sym || '');
    var H = opts.headingTag || 'h2';
    var hCls = opts.headingClass || 'b-h2';
    var h = function (t) { return '<' + H + ' class="' + hCls + '">' + t + '</' + H + '>'; };

    if (typeof document !== 'undefined') injectStyle(document);

    /* NO STATEMENTS, NO COMPOSITE, NO RANK. The screen's own rule — a company
       that reports nothing must not outrank one that does — and this page has
       to say the same thing rather than render an empty grid, which reads like
       a loading failure rather than an answer. */
    if (num(row.roce) === null && num(row.comp) === null && num(row.pe) === null) {
      return h('This company reports nothing the screen could read.') +
        '<p class="b-p">' + esc(sym) + ' carries no financial statements in the 750-name ' +
        'screen, so it has no quality, growth or valuation score and no composite. That is a ' +
        'fact about the disclosure, not about the business — and it is the reason the setup ' +
        'above is a price argument and only a price argument. A company that reports nothing ' +
        'must not be allowed to outrank one that does, so nothing is estimated here to fill ' +
        'the gap.</p>' +
        '<p class="bf-note">Fundamentals come from the same screen the rest of this site runs ' +
        'on. Missing means missing.</p>';
    }

    var BARS = [
      ['Quality', num(row.q),
        'Return on capital, margins, leverage and cash conversion, read on the multi-year median rather than the latest year.'],
      ['Growth', num(row.g),
        'Revenue, EBITDA and earnings compounded over the statement history. Per-share growth is withheld entirely where the share count moved structurally.'],
      ['Valuation', num(row.v),
        'What the multiple asks against what the business earns — scored against the screen, not against a fixed band.'],
      ['Technical', num(row.tech),
        'Trend, momentum and participation. It is the only one of the four the setup above already argued.'],
    ];
    var scored = BARS.filter(function (b) { return b[1] !== null; }).length;
    var bar = function (b) {
      return '<div class="bf-bar" title="' + esc(b[2]) + '">' +
        '<span class="n">' + esc(b[0]) + '</span>' +
        '<span class="t"><i style="width:' + (b[1] === null ? 0 : clamp100(b[1]).toFixed(1)) + '%"></i></span>' +
        '<span class="s">' + (b[1] === null ? '—' : Math.round(b[1])) + '</span></div>';
    };

    /* Valuation against ITS OWN history, not against a number somebody
       remembers. pe_pctile is where today's multiple sits in this company's
       own published range: 90 means it has been cheaper than this 90% of the
       time. */
    var pep = num(row.pe_pctile);
    var pctl = pep === null ? '' :
      '<div class="bf-pctl">' +
        '<div class="bf-pctl-h"><b>Where the multiple sits in its own range</b>' +
        '<span>' + Math.round(pep) + 'th percentile</span></div>' +
        '<div class="bf-pctl-t"><i style="left:' + clamp100(pep).toFixed(1) + '%"></i></div>' +
        '<div class="bf-pctl-l"><span>cheapest it has been</span><span>dearest</span></div>' +
        '<p class="bf-pctl-n">A price-to-earnings of ' + one(row.pe, 1) + ' today. This name has ' +
        'traded cheaper than that ' + Math.round(pep) + '% of the time in the history the screen ' +
        'holds. ' + (pep >= 80
          ? 'Buying here is buying it near the expensive end of its own record.'
          : pep <= 30
            ? 'That is the cheap end of its own record — which is a reason to look, not a reason to buy.'
            : 'That is an unremarkable place in its own record, which is the most common answer and the least interesting one.') +
        '</p></div>';

    var de = num(row.de);
    var TBL = [
      ['Returns', [
        ['Return on capital', one(row.roce, 1, '%'),
          'ROCE — EBIT over invested capital. Yahoo publishes no such field; it is computed from the statements.'],
        ['ROCE, multi-year median', one(row.roce_med, 1, '%'),
          'The median across the statement history. The score reads this, not the latest year, so a one-off cannot top the table.'],
        ['ROCE trend', row.roce_trend ? esc(String(row.roce_trend)) : NA,
          'Direction of return on capital across the years on file.'],
        ['Return on equity', one(row.roe, 1, '%'), ''],
      ]],
      ['Balance sheet', [
        ['Debt to equity', de === null ? NA
          : (de < 0 ? de.toFixed(2) + ' <b class="b-warn">negative equity</b>' : de.toFixed(2)),
          'A negative reading means negative equity, which is insolvency — it scores zero on leverage, not full marks.'],
        ['Interest cover', one(row.icover, 1, '×'), 'Operating profit against the interest bill.'],
        ['Current ratio', one(row.curr, 2, '×'), ''],
        ['Piotroski', num(row.piotroski) === null ? NA
          : Math.round(num(row.piotroski)) + ' / ' + (num(row.piotroski_of) === null ? 9 : Math.round(num(row.piotroski_of))),
          'Nine binary accounting tests. Seven or more is strong, under five is weak.'],
      ]],
      ['Growth', [
        ['Revenue CAGR', one(row.rev_cagr, 1, '%'), 'Compounded across the statement history.'],
        ['EBITDA CAGR', one(row.ebitda_cagr, 1, '%'), ''],
        ['EPS CAGR', one(row.eps_cagr, 1, '%'),
          'Withheld entirely where the share count moved structurally — a split or an issue is not earnings growth.'],
        ['Revenue, latest year', one(row.rev_yoy, 1, '%'), ''],
        ['Earnings, latest year', one(row.eps_yoy, 1, '%'), ''],
      ]],
      ['Cash', [
        ['Cash from operations / profit', one(row.cfo_pat, 2, '×'),
          'Under 1.0 means the reported profit is not arriving as cash.'],
        ['Free cash flow / profit', one(row.fcf_pat, 2, '×'), ''],
        ['Cash score', one(row.cf, 0), "The screen's own reading of cash quality, 0 to 100."],
      ]],
      ['Valuation', [
        ['Price to earnings', one(row.pe, 1, '×'), ''],
        ['Price to book', one(row.pb, 2, '×'), ''],
        /* DIVIDEND YIELD IS DELIBERATELY ABSENT. The screen's div_yield is
           Yahoo's dividendYield put through a fraction-to-percent conversion
           it no longer needs, so the column reads ITC at 601%, COALINDIA at
           503% and a universe median of 65.5%. stock_screen.py is fixed;
           rows built before that still carry the old number and this table
           will not print it. Restore the row once a clean build is served. */
        ['Effective tax rate', one(row.tax, 1, '%'),
          'A rate far from the statutory one is worth a look at the notes.'],
        ['Market capitalisation', num(row.mcap_cr) === null ? NA
          : '₹' + Math.round(num(row.mcap_cr)).toLocaleString('en-IN') + ' cr', ''],
      ]],
      ['Ownership', [
        ['Promoters', one(row.insiders, 1, '%'), ''],
        ['Institutions', one(row.instis, 1, '%'), ''],
      ]],
    ];

    var tables = '<div class="bf-tbls">' + TBL.map(function (t) {
      return '<div class="bf-tbl"><h4>' + esc(t[0]) + '</h4><table><tbody>' +
        t[1].map(function (r) {
          return '<tr' + (r[2] ? ' title="' + esc(r[2]) + '"' : '') + '>' +
            '<th scope="row">' + esc(r[0]) + '</th><td>' + r[1] + '</td></tr>';
        }).join('') + '</tbody></table></div>';
    }).join('') + '</div>';

    /* ── "NO FLAGS" AND "THE FLAGS ARE NOT IN THIS FILE" ARE DIFFERENT
       SENTENCES, AND THE FIRST DRAFT PRINTED THE WRONG ONE.

       screen-lite.json keeps risk.level and risk.score and STRIPS risk.flags
       — the per-company prose. A version that branched on flags.length alone
       published "The screen raises no risk flags on this name" for SPLPETRO,
       which carries three and is graded HIGH. That is a projection's omission
       rendered as a measured result, which is the fault these pages exist to
       avoid.

       The grade shows wherever it exists, because it survives the projection.
       The itemised objections show when the full table was loaded, and are
       named as elsewhere when it was not. */
    var flags = (row.risk && Object.prototype.toString.call(row.risk.flags) === '[object Array]')
      ? row.risk.flags : [];
    var lvl = (row.risk && row.risk.level) ? String(row.risk.level) : null;
    var rscore = row.risk ? num(row.risk.score) : null;
    var head = '<h4>What the screen flags against it</h4>' + (lvl
      ? '<p class="bf-riskg"><b class="rg-' + esc(lvl.toLowerCase()) + '">' + esc(lvl) + ' RISK</b>' +
        (rscore === null ? '' : '<span>risk score ' + rscore + '</span>') + '</p>'
      : '');

    var risk;
    if (!lvl && !flags.length) {
      risk = '<p class="bf-note">The screen published no risk grade for this name. That is a gap ' +
             'in the data, not a clean bill of health.</p>';
    } else if (flags.length) {
      risk = '<div class="bf-flags">' + head + '<ul>' + flags.map(function (f) {
        return '<li class="sev-' + esc(String(f.s || 'low')) + '"><b>' + esc(String(f.t || '')) + '</b>' +
          (f.k ? '<span>' + esc(String(f.k)) + '</span>' : '') + '</li>';
      }).join('') + '</ul><p class="bf-note">' + flags.length + ' flag' +
        (flags.length === 1 ? '' : 's') + " on this name. These are the screen's objections, " +
        'published beside its scores rather than netted off against them.</p></div>';
    } else {
      risk = '<div class="bf-flags">' + head + '<p class="bf-note">The grade above is carried by ' +
        'the light table this page loads; the itemised objections behind it are not — they are ' +
        'stripped from that projection to keep it small. This is <b>not</b> a statement that ' +
        'there are none.' +
        (opts.screenHref ? ' <a href="' + esc(opts.screenHref) + '">Open ' + esc(sym) +
          ' on the screen</a> to read them.' : '') +
        '</p></div>';
    }

    var fy = num(row.fy_count);
    var conf = num(row.v_conf);

    return h(scored === 0
        ? 'The screen holds figures for this name but scored none of them.'
        : scored < 4
          ? 'Scored on ' + scored + ' of four measures. The rest are not on file.'
          : 'What you would own, on four measures the price argument never touches.') +
      '<p class="bf-sub">Every figure below is read from the same 750-name screen this page ' +
      'already downloaded — the same numbers, the same build, no second source and no rounding ' +
      'of its own.' + (fy === null ? '' : ' ' + Math.round(fy) + ' fiscal ' +
        (Math.round(fy) === 1 ? 'year' : 'years') + ' of statements' +
        (row.fy ? ', latest ' + esc(String(row.fy)) : '') + '.') + '</p>' +

      '<div class="bf-score"><div>' +
        '<div class="bf-big">' + (num(row.comp) === null ? '—' : Math.round(num(row.comp))) +
        '<span>/100</span></div>' +
        '<p class="bf-note" style="margin-top:6px">Composite' + (num(row.comp) === null
          ? ' — unranked. A company with no statements gets no composite, so it cannot outrank one that reports.'
          : '. A declared weighting of the four scores beside it' +
            (conf === null ? '' : ', valuation carrying ' + Math.round(conf * 100) + '% confidence') + '.') +
        '</p></div>' +
        '<div class="bf-bars">' + BARS.map(bar).join('') + '</div></div>' +

      pctl + tables + risk +

      '<p class="bf-note"><b>Missing means missing.</b> A field the screen did not publish reads ' +
      '<i>not published</i> here and is left out of the score that would have used it — it is never ' +
      'carried as a zero, which would read as a measured result of zero. Fundamentals do not move ' +
      'on the day; they are as current as the last set of accounts, not as current as the price ' +
      'above them.</p>';
  }

  /* WHAT A BRIEF SHOWS WHEN THIS FILE DID NOT ARRIVE.
     It travels between two repos, so "it is not here" is a state, not an
     impossibility — and a section that vanishes silently is indistinguishable
     from one that was never meant to exist. Both callers render this instead. */
  function missingNotice() {
    return '<p class="b-p">The business section could not load. It is served as a separate ' +
      'file and that file did not arrive, so the fundamentals are not shown rather than shown ' +
      'incompletely. Everything else on this page is unaffected.</p>';
  }

  root.BriefFundamentals = {
    render: render,
    missingNotice: missingNotice,
    /* Bumped when the shape of what render() needs from a row changes, so the
       two sites can say which copy they are running rather than guessing. */
    VERSION: '1.0.0',
  };
})(typeof window !== 'undefined' ? window : globalThis);
