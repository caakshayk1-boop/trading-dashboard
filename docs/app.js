/*
 * app.js — the browser half of The Daily Signal.
 *
 * This lived as a 3,265-line string inside newspaper.py, which meant no
 * linter, type checker, bundler, formatter or test runner could see any of
 * it. Three production incidents in one week came from that: an out-of-scope
 * el() call that aborted the whole block and killed the ticker, world map and
 * scroll-spy; a modal defined in a branch that never runs; and a modal sealed
 * inside main's stacking context. None were subtle — they were invisible,
 * which is a different problem.
 *
 * It is an ordinary file now. `node --check` gates it in CI and a real linter
 * can be pointed at it.
 *
 * Two things to know before editing:
 *
 *  1. Loaded with `defer`, so it runs after the document is parsed. The old
 *     inline copy ran mid-parse and several bugs came from touching nodes
 *     that did not exist yet. Do not "optimise" this back into a blocking tag.
 *
 *  2. Server data arrives through a JSON <script> block, not string
 *     interpolation. This file is no longer a template and must never
 *     contain a Jinja tag.
 */

/* Read the server payload the template renders beside this script. */
var TV_ALIASES = (function () {
  try {
    var n = document.getElementById('tv-aliases');
    return n ? JSON.parse(n.textContent) : {};
  } catch (e) { return {}; }
})();

(function(){
  var RM = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ── scroll progress + fab ── */
  var prog = document.getElementById('prog'), fab = document.getElementById('fab'), ticking = false;
  function onScroll(){
    var h = document.documentElement.scrollHeight - window.innerHeight;
    prog.style.width = (h > 0 ? (window.scrollY / h) * 100 : 0) + '%';
    fab.classList.toggle('on', window.scrollY > 700);
    ticking = false;
  }
  window.addEventListener('scroll', function(){
    if (!ticking){ ticking = true; requestAnimationFrame(onScroll); }
  }, {passive:true});
  onScroll();
  fab.addEventListener('click', function(){ window.scrollTo({top:0, behavior: RM ? 'auto' : 'smooth'}); });

  /* ── reveal on scroll ── */
  var revs = document.querySelectorAll('.rv');
  if (RM || !('IntersectionObserver' in window)){
    revs.forEach(function(e){ e.classList.add('in'); });
  } else {
    // threshold must stay 0 (any pixel intersecting), not a ratio. A ratio of
    // 0.05 is unsatisfiable for anything taller than 20x the viewport, and the
    // signal log grows past that as soon as the live feed loads a few hundred
    // rows — the table would then never reveal and the section would render blank.
    var ro = new IntersectionObserver(function(entries){
      entries.forEach(function(en){
        if (en.isIntersecting){ en.target.classList.add('in'); ro.unobserve(en.target); }
      });
    }, {rootMargin:'0px 0px -8% 0px', threshold:0});
    revs.forEach(function(e){ ro.observe(e); });
  }

  /* ── count-up ── */
  function countUp(el){
    var target = parseFloat(el.dataset.count) || 0,
        suffix = el.dataset.suffix || '',
        total  = el.dataset.total ? '/' + el.dataset.total : '',
        dur = 1100, t0 = null;
    if (RM){ el.textContent = target + suffix + total; return; }
    function step(ts){
      // The live layer may replace this number mid-animation with the real
      // value from the ledger. It marks the node when it does; keep animating
      // past that and the page settles back on the stale snapshot figure.
      if (el.dataset.live) return;
      if (!t0) t0 = ts;
      var p = Math.min((ts - t0) / dur, 1),
          e = 1 - Math.pow(1 - p, 3);
      el.textContent = Math.round(target * e) + suffix + total;
      if (p < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }
  var nums = document.querySelectorAll('[data-count]');
  if (!('IntersectionObserver' in window)){
    nums.forEach(countUp);
  } else {
    var co = new IntersectionObserver(function(entries){
      entries.forEach(function(en){
        if (en.isIntersecting){ countUp(en.target); co.unobserve(en.target); }
      });
    }, {threshold:0.4});
    nums.forEach(function(e){ co.observe(e); });
  }



  /* ══════════════ COMMAND PALETTE ══════════════
     Sections come from the nav that is already on the page; symbols are
     harvested from the signal table once the ledger loads. Nothing extra is
     fetched — the palette is a view over data the page already has. */
  (function(){
    var box = document.getElementById('cmdk');
    if (!box) return;
    // esc() is defined inside the live-layer IIFE, which is a different scope.
    // Referencing it from here threw on every keystroke and the palette
    // silently returned nothing — its own copy, because a shared helper that
    // is not actually shared is worse than a duplicated four-liner.
    function esc(v){
      return String(v == null ? '' : v)
        .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }
    var input = document.getElementById('cmdkIn'),
        list  = document.getElementById('cmdkList'),
        open  = document.getElementById('cmdkOpen');
    var items = [], results = [], sel = 0;

    function collect(){
      items = [];
      document.querySelectorAll('.nav-in a[href^="#"]').forEach(function(a){
        items.push({kind:'Go', label:a.textContent.replace(/^\d+/,'').trim(),
                    meta:'section', href:a.getAttribute('href')});
      });
      var other = document.querySelector('.nav-other');
      if (other) items.push({kind:'Page', label:other.textContent.replace('→','').trim(),
                             meta:other.getAttribute('href'), href:other.getAttribute('href'), hard:true});
      // Symbols from whatever the ledger has rendered.
      var seen = {};
      document.querySelectorAll('#alertTable tbody tr td:nth-child(2) .sym, .lt .sym, .pick .sym')
        .forEach(function(el){
          var sym = el.textContent.trim();
          if (!sym || seen[sym]) return;
          seen[sym] = 1;
          items.push({kind:'Symbol', label:sym, meta:'filter the ledger', sym:sym});
        });
    }

    function score(q, s){
      s = s.toLowerCase();
      if (s.indexOf(q) === 0) return 0;      // prefix beats substring
      var i = s.indexOf(q);
      return i === -1 ? -1 : i + 1;
    }

    function render(){
      var q = input.value.trim().toLowerCase();
      results = !q ? items.slice(0, 12)
        : items.map(function(it){ return {it:it, s:score(q, it.label)}; })
               .filter(function(r){ return r.s >= 0; })
               .sort(function(a,b){ return a.s - b.s; })
               .slice(0, 12).map(function(r){ return r.it; });
      sel = 0;
      if (!results.length){
        list.innerHTML = '<div class="cmdk-empty">Nothing matches &ldquo;' + esc(input.value) + '&rdquo;</div>';
        return;
      }
      list.innerHTML = results.map(function(it, i){
        return '<li role="option" data-i="' + i + '" aria-selected="' + (i === 0) + '">' +
               '<span class="k">' + esc(it.kind) + '</span>' +
               '<span class="t">' + esc(it.label) + '</span>' +
               '<span class="m">' + esc(it.meta || '') + '</span></li>';
      }).join('');
      list.querySelectorAll('li').forEach(function(li){
        li.addEventListener('click', function(){ go(+li.dataset.i); });
        li.addEventListener('mousemove', function(){ move(+li.dataset.i - sel); });
      });
    }

    function move(d){
      if (!results.length) return;
      sel = (sel + d + results.length) % results.length;
      list.querySelectorAll('li').forEach(function(li, i){
        li.setAttribute('aria-selected', i === sel);
        if (i === sel) li.scrollIntoView({block:'nearest'});
      });
    }

    function go(i){
      var it = results[i];
      if (!it) return;
      close();
      if (it.hard){ location.href = it.href; return; }
      if (it.href){
        var t = document.querySelector(it.href);
        if (t) t.scrollIntoView({behavior:'smooth', block:'start'});
        return;
      }
      if (it.sym){
        // Reuse the ledger's own search box so filtering stays one code path.
        var box2 = document.getElementById('alertSearch');
        if (box2){
          box2.value = it.sym;
          box2.dispatchEvent(new Event('input', {bubbles:true}));
          var sec = document.getElementById('alerts');
          if (sec) sec.scrollIntoView({behavior:'smooth', block:'start'});
        }
      }
    }

    function show(){
      collect();
      box.hidden = false;
      input.value = '';
      render();
      setTimeout(function(){ input.focus(); }, 20);
    }
    function close(){ box.hidden = true; }

    document.addEventListener('keydown', function(e){
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k'){ e.preventDefault(); box.hidden ? show() : close(); return; }
      if (box.hidden) return;
      if (e.key === 'Escape'){ e.preventDefault(); close(); }
      else if (e.key === 'ArrowDown'){ e.preventDefault(); move(1); }
      else if (e.key === 'ArrowUp'){ e.preventDefault(); move(-1); }
      else if (e.key === 'Enter'){ e.preventDefault(); go(sel); }
    });
    input.addEventListener('input', render);
    if (open) open.addEventListener('click', show);
    box.querySelector('[data-close]').addEventListener('click', close);
  })();

  /* ── music crates ── */
  document.querySelectorAll('.crate-more').forEach(function(btn){
    btn.addEventListener('click', function(){
      var crate = btn.closest('.crate');
      var open = crate.classList.toggle('open');
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
      var n = crate.querySelectorAll('.trk').length;
      btn.innerHTML = open ? 'Show fewer &uarr;' : 'Show all ' + n + ' &darr;';
    });
  });

  /* ── SWP: accumulate, then draw down ──
     Self-contained and API-free on purpose: this is arithmetic, not data, so
     it must behave identically on the static 6 AM build and on the live host.
     Nothing here touches the ledger. */
  (function(){
    var root = document.getElementById('swp');
    if (!root) return;

    var mode = 'nominal';

    function val(id, dflt){
      var e = document.getElementById('swp' + id);
      var v = e ? parseFloat(e.value) : NaN;
      return isFinite(v) ? v : dflt;
    }

    // Indian grouping, and crore/lakh past the point where digits stop being
    // readable. A retirement corpus written as 21837450 is a number nobody can
    // hold in their head; ₹2.18 Cr is.
    function inr(v){
      if (v === null || v === undefined || !isFinite(v)) return '—';
      var n = Math.round(v), sign = n < 0 ? '-' : '';
      n = Math.abs(n);
      if (n >= 1e7) return sign + '₹' + (n / 1e7).toFixed(2) + ' Cr';
      if (n >= 1e5) return sign + '₹' + (n / 1e5).toFixed(2) + ' L';
      return sign + '₹' + n.toLocaleString('en-IN');
    }

    /* One year of drawdown, month by month.

       The tax handling is the part that matters. A withdrawal is not pure
       gain: it is part return-of-capital and part profit, in the same ratio
       the whole corpus is. That is proportional cost-basis depletion, and it
       means only `gainFrac` of each rupee is taxable. To actually LAND the
       target amount in the bank, the gross redemption has to be scaled up by
       1/(1 - gainFrac*tax) — otherwise the plan silently delivers less every
       month and the corpus lasts longer on paper than in life. */
    function drawdown(c0, basisRatio, o){
      var corpus = c0, basis = c0 * basisRatio;
      var netDraw = o.firstNet, rows = [], deadAt = null;

      for (var y = 0; y < o.years; y++){
        var paid = 0;
        for (var m = 0; m < 12; m++){
          corpus = corpus * (1 + o.mPost);
          if (corpus <= 0){ corpus = 0; break; }

          var gainFrac = Math.max(0, (corpus - basis) / corpus);
          var eff = 1 - gainFrac * o.tax;
          if (eff < 0.01) eff = 0.01;          // guard a 100% tax input

          var gross = netDraw / eff;
          var last = false;
          if (gross >= corpus){ gross = corpus; last = true; }

          // Basis must be reduced using the corpus BEFORE the withdrawal —
          // that is what makes the ratio proportional.
          var basisShare = gross * (basis / corpus);
          corpus -= gross;
          basis = Math.max(0, basis - basisShare);
          paid += gross * eff;                 // what reached the bank
          if (corpus <= 1e-6){ corpus = 0; last = true; }
          if (last) break;
        }
        rows.push({ age: o.retAge + y + 1, corpus: corpus, flow: paid, phase: 'dec' });
        if (corpus <= 0 && deadAt === null) deadAt = o.retAge + y + 1;
        netDraw = netDraw * (1 + o.infl);
      }
      return { rows: rows, deadAt: deadAt, survives: deadAt === null };
    }

    function model(){
      var curAge = Math.round(val('CurAge', 34));
      var retAge = Math.round(val('RetAge', 55));
      var endAge = Math.round(val('EndAge', 90));
      if (retAge <= curAge) retAge = curAge + 1;
      if (endAge <= retAge) endAge = retAge + 1;

      var infl  = val('Infl', 6) / 100;
      var tax   = val('Tax', 12.5) / 100;
      var step  = val('Step', 10) / 100;
      // Monthly rate from the EFFECTIVE annual rate. annual/12 is the common
      // shortcut and it is wrong: 12/12 = 1% a month compounds to 12.68% a
      // year, which inflates a 20-year corpus by a double-digit percentage.
      var mPre  = Math.pow(1 + val('RetPre', 12) / 100, 1 / 12) - 1;
      var mPost = Math.pow(1 + val('RetPost', 8) / 100, 1 / 12) - 1;

      var corpus = val('Corpus', 0), basis = corpus, sip = val('Sip', 0);
      var series = [{ age: curAge, corpus: corpus, flow: 0, phase: 'acc' }];

      for (var y = 0; y < retAge - curAge; y++){
        var put = 0;
        for (var m = 0; m < 12; m++){
          corpus = corpus * (1 + mPre) + sip;
          basis += sip;
          put += sip;
        }
        series.push({ age: curAge + y + 1, corpus: corpus, flow: put, phase: 'acc' });
        sip = sip * (1 + step);
      }

      var atRet = corpus;
      var basisRatio = atRet > 0 ? Math.min(1, basis / atRet) : 1;

      // Entered in today's money, so it has to be inflated to the retirement
      // year before the first cheque is written.
      var firstNet = val('Draw', 0) * Math.pow(1 + infl, retAge - curAge);
      var o = { years: endAge - retAge, retAge: retAge, mPost: mPost,
                infl: infl, tax: tax, firstNet: firstNet };

      var dd = drawdown(atRet, basisRatio, o);

      // Smallest retirement corpus that survives to endAge, by bisection.
      // Solved rather than derived because the tax gross-up makes the
      // withdrawal depend on the corpus, so there is no clean closed form.
      var lo = 0, hi = Math.max(1e7, firstNet * 12 * o.years * 3);
      if (drawdown(hi, basisRatio, o).survives){
        for (var i = 0; i < 44; i++){
          var mid = (lo + hi) / 2;
          if (drawdown(mid, basisRatio, o).survives) hi = mid; else lo = mid;
        }
      } else { hi = NaN; }

      return { series: series.concat(dd.rows), atRet: atRet, firstNet: firstNet,
               required: hi, deadAt: dd.deadAt, survives: dd.survives,
               curAge: curAge, retAge: retAge, endAge: endAge, infl: infl };
    }

    var CW = 760, CH = 190;

    function chart(R){
      var svg = document.getElementById('swpChart');
      if (!svg) return;
      var span = R.endAge - R.curAge;
      var maxC = Math.max.apply(null, R.series.map(function(d){ return d.corpus; }));
      if (!(maxC > 0)) maxC = 1;

      function px(age){ return (age - R.curAge) / span * CW; }
      function py(c){ return CH - (c / maxC) * CH; }
      function real(d){ return d.corpus / Math.pow(1 + R.infl, d.age - R.curAge); }

      var line = R.series.map(function(d){ return px(d.age) + ',' + py(d.corpus); }).join(' ');
      var rline = R.series.map(function(d){ return px(d.age) + ',' + py(real(d)); }).join(' ');
      var area = '0,' + CH + ' ' + line + ' ' + CW + ',' + CH;

      var g = '<polygon points="' + area + '" fill="var(--blue)" opacity="0.10"></polygon>' +
              '<polyline points="' + line + '" fill="none" stroke="var(--blue)" stroke-width="2"></polyline>' +
              '<polyline points="' + rline + '" fill="none" stroke="var(--dim)" stroke-width="1.2" ' +
                'stroke-dasharray="4 3"></polyline>' +
              '<line x1="' + px(R.retAge) + '" y1="0" x2="' + px(R.retAge) + '" y2="' + CH +
                '" stroke="var(--lime)" stroke-width="1"></line>';
      if (!R.survives && R.deadAt !== null){
        g += '<line x1="' + px(R.deadAt) + '" y1="0" x2="' + px(R.deadAt) + '" y2="' + CH +
             '" stroke="var(--down)" stroke-width="1.5"></line>';
      }
      g += '<line x1="0" y1="' + CH + '" x2="' + CW + '" y2="' + CH +
           '" stroke="var(--line)" stroke-width="1"></line>';
      [0, 0.25, 0.5, 0.75, 1].forEach(function(f){
        var age = Math.round(R.curAge + f * span);
        g += '<text x="' + (f * CW) + '" y="' + (CH + 18) + '" font-size="11" fill="var(--dim)" ' +
             'font-family="var(--mono)" text-anchor="' +
             (f === 0 ? 'start' : f === 1 ? 'end' : 'middle') + '">' + age + '</text>';
      });
      svg.innerHTML = '<title id="swpChartT">Corpus rises to ' + inr(R.atRet) + ' at age ' +
                      R.retAge + ', then declines</title>' + g;

      var pk = document.getElementById('swpPeak');
      if (pk) pk.textContent = 'peak ' + inr(maxC);
    }

    function table(R){
      var tb = document.querySelector('#swpTbl tbody');
      if (!tb) return;
      var html = '';
      R.series.slice(1).forEach(function(d){
        var k = mode === 'real' ? Math.pow(1 + R.infl, d.age - R.curAge) : 1;
        var yr = (new Date()).getFullYear() + Math.round(d.age - R.curAge);
        var isRet = d.age === R.retAge;
        var broke = d.phase === 'dec' && d.corpus <= 0;
        var flow = d.phase === 'acc'
          ? '+' + inr(d.flow / k)
          : (broke && d.flow <= 0 ? '—' : '−' + inr(d.flow / k));
        html += '<tr class="' + (isRet ? 'ret' : broke ? 'dead' : '') + '">' +
                  '<td>' + Math.round(d.age) + '</td>' +
                  '<td class="mono-dim">' + yr + '</td>' +
                  '<td class="' + (d.phase === 'acc' ? 'up' : 'dn') + '">' + flow + '</td>' +
                  '<td class="num">' + (broke ? 'nil' : inr(d.corpus / k)) + '</td>' +
                '</tr>';
      });
      tb.innerHTML = html;
    }

    function render(){
      var R = model();
      var set = function(id, txt){
        var e = document.getElementById(id); if (e) e.textContent = txt;
      };
      set('swpKCorpus', inr(R.atRet));
      set('swpKNeed', isFinite(R.required) ? inr(R.required) : 'unreachable');
      set('swpKDraw', inr(R.firstNet));
      set('swpKLast', R.survives ? 'age ' + R.endAge + ' ✓' : 'age ' + R.deadAt);

      // The collapsed summary. Written on every render, not just on collapse,
      // so the strip cannot drift from the panel it is summarising.
      set('swpSumCorpus', inr(R.atRet));
      set('swpSumAge', R.retAge);
      set('swpSumDraw', inr(R.firstNet));
      // document.getElementById, NOT the el() helper: el() is defined inside
      // the ledger IIFE further down and does not exist in this scope. This
      // one call threw "el is not defined", which aborted the whole script
      // block on / — dead ticker, dead world map, dead scroll spy — while
      // /desk was untouched because it has no SWP section.
      var sl = document.getElementById('swpSumLast');
      if (sl){
        sl.textContent = R.survives ? 'lasts to ' + R.endAge
                                    : 'runs out at ' + R.deadAt;
        sl.className = R.survives ? 'ok' : 'short';
      }

      var v = document.getElementById('swpVerdict');
      if (v){
        if (R.survives){
          var spare = R.atRet - R.required;
          v.className = 'swp-verdict';
          v.textContent = 'Lasts to ' + R.endAge + '. At ' + R.retAge + ' you need ' +
            inr(R.required) + ' and the plan reaches ' + inr(R.atRet) + ' — ' +
            inr(Math.abs(spare)) + (spare >= 0 ? ' clear.' : ' short.');
        } else {
          v.className = 'swp-verdict short';
          v.textContent = 'Runs out at ' + R.deadAt + ', ' + (R.endAge - R.deadAt) +
            ' years early. You reach ' + inr(R.atRet) + ' at ' + R.retAge +
            (isFinite(R.required)
              ? ' against ' + inr(R.required) + ' needed — short by ' +
                inr(R.required - R.atRet) + '.'
              : ' and no corpus survives this withdrawal at this return.');
        }
      }
      chart(R);
      table(R);
    }

    /* ── collapse / expand ──
       The state is remembered, because someone who opens this to re-plan
       usually opens it again the next few mornings, and re-expanding it every
       time is friction for exactly the person the section is for. */
    var body = document.getElementById('swpBody'),
        exp  = document.getElementById('swpExpand'),
        LS   = 'ds_swp_open';

    function setOpen(open, persist){
      if (!body || !exp) return;
      body.hidden = !open;
      exp.setAttribute('aria-expanded', open ? 'true' : 'false');
      exp.innerHTML = open ? 'Hide the workings &uarr;' : 'Adjust the plan &darr;';
      if (persist){ try { localStorage.setItem(LS, open ? '1' : '0'); } catch(e){} }
    }

    if (exp){
      exp.addEventListener('click', function(){
        setOpen(body.hidden, true);
        // Re-render on open: the chart is an SVG sized by its container, and
        // one laid out while display:none has no width to measure.
        if (!body.hidden) render();
      });
    }
    var saved = '0';
    try { saved = localStorage.getItem(LS) || '0'; } catch(e){}
    setOpen(saved === '1', false);

    root.querySelectorAll('.swp-in input').forEach(function(i){
      i.addEventListener('input', render);
    });
    root.querySelectorAll('.swp-toggle button').forEach(function(b){
      b.addEventListener('click', function(){
        mode = b.dataset.mode;
        root.querySelectorAll('.swp-toggle button').forEach(function(x){
          x.classList.toggle('on', x === b);
        });
        render();
      });
    });

    render();
  })();

  /* ── liked songs ──
     Deliberately self-contained rather than folded into the ledger block
     below. Music is a /desk section and the ledger block early-returns on
     /desk, so hanging this off start() would wire it on the one page that does
     not have a music section and skip the one that does. It needs the edit key
     and one endpoint; that is small enough to carry its own helpers. */
  (function(){
    var crates = document.querySelector('.crates');
    if (!crates) return;                        // not the music page

    var note  = document.getElementById('likeNote');
    var shelf = document.getElementById('likedCrate');
    var list  = document.getElementById('likedList');
    var count = document.getElementById('likedCt');
    var liked = Object.create(null);             // "title artist" -> true

    function key(t, a){ return (t || '') + ' ' + (a || ''); }
    function editKey(){
      try { return localStorage.getItem('ds_edit_key') || ''; } catch(e){ return ''; }
    }
    function esc(s){
      return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){
        return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
      });
    }
    function say(msg, isErr){
      if (!note) return;
      note.className = 'crate-note' + (isErr ? ' err' : '');
      note.textContent = msg || '';
    }

    // Reflect state onto every copy of a track. The same song can appear in
    // more than one crate and again in the Liked shelf; liking it in one place
    // has to light it up everywhere, or the shelf and the crate disagree.
    function paint(){
      document.querySelectorAll('.trk').forEach(function(li){
        var b = li.querySelector('.lk');
        if (!b) return;
        var on = !!liked[key(li.dataset.title, li.dataset.artist)];
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
        b.title = on ? 'Remove from my songs' : 'Save to my songs';
      });
    }

    function renderShelf(songs){
      if (!shelf || !list) return;
      if (!songs.length){ shelf.style.display = 'none'; return; }
      shelf.style.display = '';
      if (count) count.textContent = songs.length;
      var TOP = 5, html = '';
      songs.forEach(function(s, i){
        // The like row stores only title/artist/url. Find the same track in a
        // crate above and borrow its embed, so a liked track plays in place
        // like any other row. Cheaper and more honest than widening the
        // stored schema: the crates are the source of truth for what a track
        // IS, the shelf only records that you liked it — which also means a
        // re-pinned id reaches the Liked crate for free.
        var m = /[?&]v=([\w-]{6,})/.exec(s.url || '');
        var vid = m ? m[1] : '';
        var src = null;
        document.querySelectorAll('.crate:not([data-crate="liked"]) .trk')
          .forEach(function(n){ if (!src && n.dataset.title === s.title) src = n; });
        var embed = src ? (src.dataset.embed || '') : '';
        var appleU = src ? (src.dataset.apple || '') : '';
        html += '<li class="trk' + (i >= TOP ? ' more' : '') + '"' +
                ' data-title="' + esc(s.title) + '"' +
                ' data-artist="' + esc(s.artist) + '"' +
                ' data-url="' + esc(s.url) + '"' +
                ' data-embed="' + esc(embed) + '"' +
                ' data-apple="' + esc(appleU) + '"' +
                ' data-vid="' + esc(vid) + '">' +
                  '<span class="no">' + ('0' + (i + 1)).slice(-2) + '</span>' +
                  (s.url ? '<a href="' + esc(s.url) + '" target="_blank" rel="noopener">'
                         : '<a>') +
                    '<span class="ti">' + esc(s.title) + '</span>' +
                    '<span class="ar">' + esc(s.artist) + '</span>' +
                  '</a>' +
                  '<button type="button" class="pl" title="Play here"' +
                  ' aria-label="Play ' + esc(s.title) + ' on this page">&#9654;</button>' +
                  '<button type="button" class="lk" aria-pressed="true"' +
                  ' title="Remove from my songs"' +
                  ' aria-label="Remove ' + esc(s.title) + ' from my songs">&hearts;</button>' +
                '</li>';
      });
      list.innerHTML = html;
      var more = document.getElementById('likedMore');
      if (more){
        var extra = songs.length > TOP;
        more.style.display = extra ? '' : 'none';
        if (extra && shelf.className.indexOf('open') < 0){
          more.innerHTML = 'Show all ' + songs.length + ' &darr;';
        }
      }
    }

    function load(){
      fetch('/api/music')
        .then(function(r){ return r.json(); })
        .then(function(j){
          if (!j || !j.ok) return;
          liked = Object.create(null);
          (j.songs || []).forEach(function(s){ liked[key(s.title, s.artist)] = true; });
          renderShelf(j.songs || []);
          paint();
        })
        .catch(function(){ /* static host: the hearts still render, inert */ });
    }

    /* ── the player ──
       One iframe, created on first play and reused after that. It is built
       here rather than sitting in the HTML with a src so that a reader who
       never presses play never contacts Google at all. youtube-nocookie.com
       for the same reason. */
    var pBox = document.getElementById('player'),
        pFrame = document.getElementById('playerF'),
        pTitle = document.getElementById('playerT'),
        pYt = document.getElementById('playerY'),
        playing = null;

    function play(li){
      var embed = li.dataset.embed;
      // No embed means no pinned id — a search-link-only track. Let the anchor
      // do its normal thing.
      if (!embed || !pBox) return false;

      var t = li.dataset.title || '', a = li.dataset.artist || '';
      pTitle.textContent = a ? t + ' — ' + a : t;
      pYt.href = li.dataset.url || '#';
      // Secondary link, only for the tracks that have one.
      var ap = document.getElementById('playerA');
      if (ap){
        var au = li.dataset.apple || '';
        ap.href = au || '#';
        ap.style.display = au ? '' : 'none';
      }
      // Replacing the whole node rather than reassigning src: swapping src on
      // a live media iframe can leave the previous track's audio running, so
      // the shelf ends up playing two songs at once.
      pFrame.innerHTML = '';
      var f = document.createElement('iframe');
      // rel=0 keeps the end screen to this channel, modestbranding trims the
      // chrome. The player stays visible at its documented minimum size —
      // YouTube's embed terms require that, so "audio only" here means the
      // dock is small and out of the way, not that the video is hidden.
      f.src = embed + '?autoplay=1&rel=0&modestbranding=1&playsinline=1';
      f.title = pTitle.textContent;
      f.allow = 'autoplay; encrypted-media; picture-in-picture';
      f.setAttribute('allowfullscreen', '');
      f.referrerPolicy = 'strict-origin-when-cross-origin';
      pFrame.appendChild(f);
      pBox.hidden = false;

      if (playing) playing.classList.remove('on');
      var b = li.querySelector('.pl');
      if (b){ b.classList.add('on'); playing = b; }
      say('Playing. Full track, free, no account.');
      return true;
    }

    function stop(){
      if (!pBox) return;
      pFrame.innerHTML = '';           // kills the audio
      pBox.hidden = true;
      if (playing){ playing.classList.remove('on'); playing = null; }
    }

    var px = document.getElementById('playerX');
    if (px) px.addEventListener('click', stop);
    document.addEventListener('keydown', function(e){
      if (e.key === 'Escape' && pBox && !pBox.hidden) stop();
    });

    // One delegated listener on the container, so tracks rendered into the
    // Liked shelf after load are wired for free.
    crates.addEventListener('click', function(ev){
      if (!ev.target.closest) return;

      // Play: the ▶ button, or the title itself. preventDefault happens only
      // when play() actually took it, so an unpinned track still opens YouTube
      // and cmd/ctrl-click keeps working as a normal link.
      var hit = ev.target.closest('.pl') || ev.target.closest('.trk a');
      if (hit && !ev.target.closest('.lk')){
        if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button > 0) return;
        var row = hit.closest('.trk');
        if (row && play(row)){ ev.preventDefault(); return; }
        return;
      }

      var btn = ev.target.closest('.lk');
      if (!btn) return;
      ev.preventDefault();

      var li = btn.closest('.trk');
      if (!li) return;
      var title = li.dataset.title || '', artist = li.dataset.artist || '';
      var on = btn.getAttribute('aria-pressed') === 'true';

      if (!editKey()){
        say('Set your edit key in the Portfolio section to save songs.', true);
        return;
      }

      btn.classList.add('busy');
      fetch('/api/music', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'x-edit-key': editKey() },
        body: JSON.stringify({
          action: on ? 'unlike' : 'like',
          title: title, artist: artist,
          url: li.dataset.url || '',
          crate: (li.closest('.crate') || {}).dataset ? li.closest('.crate').dataset.crate : ''
        })
      })
        .then(function(r){ return r.json(); })
        .then(function(j){
          btn.classList.remove('busy');
          if (!j || !j.ok){
            say((j && j.error) || 'Could not save that.', true);
            return;
          }
          say(j.liked ? '♥ Saved to my songs.' : 'Removed from my songs.');
          load();                                   // resync from the store
        })
        .catch(function(){
          btn.classList.remove('busy');
          say('Network error — not saved.', true);
        });
    });

    load();
  })();

  /* ── nav active section ── */
  // In-page anchors ONLY. The cross-page link added by the split has
  // href="/desk", and feeding that to querySelector throws
  // "'/desk' is not a valid selector" — a SyntaxError that aborted this whole
  // script block, taking the ticker, the world map, the music crates and the
  // command palette down with it. One bad selector, an entire dead page.
  var links = [].slice.call(document.querySelectorAll('.nav a[href^="#"]')),
      secs  = links.map(function(a){ return document.querySelector(a.getAttribute('href')); });
  // The sticky stack's real height, measured rather than assumed. It was
  // hardcoded at 200px while the stack grew to topbar + nav + livebar + ticker
  // and then gained an edition bar, so every offset derived from it was short.
  // Re-measured on demand because the stack collapses on mobile scroll and the
  // edition bar can appear at any time.
  function headH(){
    var stack = document.querySelector('.headstack');
    return stack ? stack.getBoundingClientRect().height : 160;
  }
  // Anchor jumps land under a header of exactly the same height.
  function syncScrollPad(){
    document.documentElement.style.setProperty('--headh', Math.round(headH()) + 'px');
  }
  syncScrollPad();
  window.addEventListener('resize', syncScrollPad, {passive:true});

  function setActive(){
    var best = -1;
    // Distance from the top of the DOCUMENT. offsetTop is measured against the
    // offsetParent, and <main> is position:relative — so every section read
    // ~2000px short of its real position while being compared against
    // window.scrollY. That is the mismatch that put the highlight a whole
    // section ahead of the reader.
    // A few px past the header so a section counts as current the moment its
    // heading clears the chrome, not when its top edge does.
    var y = window.scrollY + headH() + 12;
    secs.forEach(function(s, i){
      // Sections stay display:none until the live API confirms there is
      // anything to put in them. A hidden section reports top 0, which
      // otherwise makes it match at every scroll position and pins the
      // highlight to whichever hidden section sits latest in the nav.
      if (!s || !s.getClientRects().length) return;
      if (s.getBoundingClientRect().top + window.scrollY <= y) best = i;
    });
    if (best < 0) best = 0;
    links.forEach(function(a, i){ a.classList.toggle('on', i === best); });
    var el = links[best];
    if (el && el.offsetLeft !== undefined){
      var bar = document.getElementById('navin');
      if (el.offsetLeft < bar.scrollLeft || el.offsetLeft > bar.scrollLeft + bar.clientWidth - 100){
        bar.scrollTo({left: el.offsetLeft - 20, behavior:'smooth'});
      }
    }
  }
  window.addEventListener('scroll', setActive, {passive:true});
  setActive();

  /* ── alert filters ── */
  document.querySelectorAll('.fbtn').forEach(function(b){
    b.addEventListener('click', function(){
      document.querySelectorAll('.fbtn').forEach(function(x){ x.classList.remove('on'); });
      b.classList.add('on');
      var f = b.dataset.f;
      document.querySelectorAll('#alertTable tbody tr').forEach(function(r){
        r.style.display = (f === 'all' || r.dataset.badge === f) ? '' : 'none';
      });
    });
  });

  /* ── tab groups (desk, way) ──
     Scoped to the owning <section>. The previous version cleared '.pane'
     document-wide, so a second tab group anywhere on the page would blank the
     first group's open pane on every click. */
  document.querySelectorAll('.tabs').forEach(function(group){
    var sec = group.closest('section') || document;
    group.querySelectorAll('.tab').forEach(function(t){
      t.addEventListener('click', function(){
        group.querySelectorAll('.tab').forEach(function(x){ x.classList.remove('on'); });
        sec.querySelectorAll('.pane').forEach(function(p){ p.classList.remove('on'); });
        t.classList.add('on');
        var pane = sec.querySelector('#' + t.dataset.p) || document.getElementById(t.dataset.p);
        if (pane) pane.classList.add('on');
      });
    });
  });

  /* ── practice streak + weekly review (localStorage, no server) ──
     The published site is static GitHub Pages, so there is nowhere to persist
     server-side. State is per-browser and per-device by design; the review
     exports to Markdown so anything worth keeping leaves the browser. */
  (function(){
    var TRACKS = [
      ["minimalism","🪶 Minimalism"], ["etiquette","🤝 Etiquette"],
      ["stillness","🧘 Stillness"],   ["model","⚙️ Model"],
      ["arabic","🇦🇪 Arabic"],        ["drill","🎯 Drill"],
      ["health","💪 Health"]
    ];
    var KEY = "tds.practice.v1";

    function todayKey(d){
      d = d || new Date();
      return d.getFullYear() + "-" + String(d.getMonth()+1).padStart(2,"0")
           + "-" + String(d.getDate()).padStart(2,"0");
    }
    function load(){
      try { return JSON.parse(localStorage.getItem(KEY) || "{}") || {}; }
      catch(e){ return {}; }
    }
    function save(d){
      try { localStorage.setItem(KEY, JSON.stringify(d)); } catch(e){}
    }
    function dayCount(store, key){
      var v = store[key];
      return (v && v.length) ? v.length : 0;
    }
    /* Streak counts back from today. Today not yet ticked does not break it —
       the day is still in progress; yesterday empty does. */
    function streak(store){
      var n = 0, d = new Date();
      if (dayCount(store, todayKey(d)) === 0) d.setDate(d.getDate() - 1);
      for(;;){
        if (dayCount(store, todayKey(d)) === 0) break;
        n++; d.setDate(d.getDate() - 1);
        if (n > 3650) break;
      }
      return n;
    }
    function best(store){
      var keys = Object.keys(store).filter(function(k){ return dayCount(store,k) > 0; }).sort();
      var run = 0, top = 0, prev = null;
      keys.forEach(function(k){
        var d = new Date(k + "T00:00:00");
        if (prev && Math.round((d - prev) / 86400000) === 1) run++; else run = 1;
        if (run > top) top = run;
        prev = d;
      });
      return top;
    }

    var box = document.getElementById("streakBox");
    if (box){
      box.hidden = false;
      var store = load();
      var checks = document.getElementById("stkChecks");

      TRACKS.forEach(function(t){
        var id = t[0], label = t[1];
        var lab = document.createElement("label");
        lab.className = "stk-c";
        var cb = document.createElement("input");
        cb.type = "checkbox"; cb.dataset.track = id;
        var span = document.createElement("span"); span.textContent = label;
        lab.appendChild(cb); lab.appendChild(span);
        cb.addEventListener("change", function(){
          var st = load(), k = todayKey(), cur = st[k] || [];
          if (cb.checked){ if (cur.indexOf(id) < 0) cur.push(id); }
          else { cur = cur.filter(function(x){ return x !== id; }); }
          if (cur.length) st[k] = cur; else delete st[k];
          save(st); render();
        });
        checks.appendChild(lab);
      });

      function render(){
        var st = load(), today = st[todayKey()] || [];
        checks.querySelectorAll("input").forEach(function(cb){
          cb.checked = today.indexOf(cb.dataset.track) >= 0;
          cb.parentNode.classList.toggle("done", cb.checked);
        });
        document.getElementById("stkCur").textContent  = streak(st);
        document.getElementById("stkBest").textContent = Math.max(best(st), streak(st));

        var strip = document.getElementById("stkStrip");
        strip.innerHTML = "";
        var hit = 0;
        for (var i = 29; i >= 0; i--){
          var d = new Date(); d.setDate(d.getDate() - i);
          var k = todayKey(d), c = dayCount(st, k);
          if (c > 0) hit++;
          var cell = document.createElement("div");
          cell.className = "stk-d" + (c >= 5 ? " p3" : c >= 3 ? " p2" : c > 0 ? " p1" : "")
                         + (i === 0 ? " today" : "");
          cell.title = k + " — " + c + " of " + TRACKS.length;
          strip.appendChild(cell);
        }
        document.getElementById("stkRate").textContent = Math.round(hit / 30 * 100) + "%";
      }
      render();
    }

    /* ── weekly review ── */
    var grid = document.getElementById("reviewGrid");
    if (grid){
      var week = grid.dataset.week;
      var RKEY = "tds.review." + week;
      var fields = ["rvNumbers","rvWins","rvMisses","rvAnswer","rvChange"];
      var saved = {};
      try { saved = JSON.parse(localStorage.getItem(RKEY) || "{}") || {}; } catch(e){}

      function status(){
        var filled = fields.filter(function(id){
          var el = document.getElementById(id);
          return el && el.value.trim().length > 0;
        }).length;
        var el = document.getElementById("rvStatus");
        el.textContent = filled === 0 ? "Not started"
          : filled === fields.length ? ("Complete · saved for " + week)
          : (filled + " of " + fields.length + " · saved for " + week);
      }

      fields.forEach(function(id){
        var el = document.getElementById(id);
        if (!el) return;
        if (saved[id]) el.value = saved[id];
        el.addEventListener("input", function(){
          var cur = {};
          fields.forEach(function(f){
            var e = document.getElementById(f);
            if (e && e.value.trim()) cur[f] = e.value;
          });
          try { localStorage.setItem(RKEY, JSON.stringify(cur)); } catch(e){}
          status();
        });
      });
      status();

      var copyBtn = document.getElementById("rvCopy");
      if (copyBtn){
        copyBtn.addEventListener("click", function(){
          function val(id){ var e = document.getElementById(id); return e ? e.value.trim() : ""; }
          var q = grid.parentNode.querySelector(".deep-q h3");
          var md = "## Weekly Review — " + week + "\n\n"
            + "### The numbers\n" + (val("rvNumbers") || "—") + "\n\n"
            + "### Wins\n" + (val("rvWins") || "—") + "\n\n"
            + "### Misses\n" + (val("rvMisses") || "—") + "\n\n"
            + "### " + (q ? q.textContent : "This week's question") + "\n"
            + (val("rvAnswer") || "—") + "\n\n"
            + "### One change for next week\n" + (val("rvChange") || "—") + "\n";
          function done(ok){
            copyBtn.textContent = ok ? "Copied" : "Copy failed";
            setTimeout(function(){ copyBtn.textContent = "Copy week as Markdown"; }, 1800);
          }
          if (navigator.clipboard && navigator.clipboard.writeText){
            navigator.clipboard.writeText(md).then(function(){ done(true); },
                                                   function(){ done(false); });
          } else {
            var ta = document.createElement("textarea");
            ta.value = md; document.body.appendChild(ta); ta.select();
            var ok = false;
            try { ok = document.execCommand("copy"); } catch(e){}
            document.body.removeChild(ta); done(ok);
          }
        });
      }
    }
  })();

  /* ═══════════════════════════════════════════════════════════════════
     LIVE LAYER

     The page shell is rebuilt once a day. The trading data is not — it
     comes from /api, which reads the same Turso ledger the scanner writes
     to, so a signal that fired ten minutes ago shows up on the next load.

     If /api is unreachable (plain static host, or the deploy lost its env
     vars) everything below no-ops and the server-rendered snapshot stays
     exactly as it is. Nothing here is allowed to break the daily paper.
     ═══════════════════════════════════════════════════════════════════ */
  (function(){
    var API      = '/api';
    var KEY_LS   = 'ds_edit_key';
    var live     = false;
    var allRows  = [];      // last signal set pulled from /api/signals
    var archDate = null;    // when set, we are looking at one archived day

    function el(id){ return document.getElementById(id); }

    // The scroll-reveal IntersectionObserver registers once, at load, over the
    // .rv elements that exist and are laid out at that moment. Anything the
    // live layer un-hides or injects afterwards was never observed, so it
    // would sit at opacity:0 for good. Every render path calls this.
    function reveal(root){
      if (!root) return;
      var nodes = [];
      if (root.classList && root.classList.contains('rv')) nodes.push(root);
      root.querySelectorAll('.rv').forEach(function(n){ nodes.push(n); });
      nodes.forEach(function(n){
        // Snap, never animate. These nodes appear in response to an API call,
        // not to a scroll, so there is no reveal to choreograph — and a .75s
        // opacity transition on a signal table tens of thousands of pixels tall
        // means compositing a layer that size, which is where it gets stuck.
        n.style.transition = 'none';
        n.classList.add('in');
      });
    }
    function fmt(n, d){
      if (n === null || n === undefined || !isFinite(n)) return '—';
      return Number(n).toFixed(d === undefined ? 2 : d);
    }
    function esc(s){
      return String(s === null || s === undefined ? '' : s)
        .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
        .replace(/"/g,'&quot;');
    }
    // Currency comes from the row, not from a hardcoded ₹. The ledger mixes
    // NSE equities with commodities and FX quoted in dollars, and every one of
    // them used to render as rupees — Brent at "₹83.59", gold at "₹4,135.80".
    function money(v, cur){
      if (v === null || v === undefined) return '—';
      return (cur === undefined || cur === null ? '₹' : cur) + fmt(v, 2);
    }
    function editKey(){ try { return localStorage.getItem(KEY_LS) || ''; } catch(e){ return ''; } }

    function api(path, opts){
      opts = opts || {};
      var h = opts.headers || {};
      if (opts.method === 'POST'){
        h['Content-Type'] = 'application/json';
        h['x-edit-key'] = editKey();
      }
      opts.headers = h;
      return fetch(API + path, opts).then(function(r){
        return r.json().catch(function(){ return { ok:false, error:'bad response' }; })
                .then(function(j){ if (!r.ok && j.ok === undefined) j.ok = false; return j; });
      });
    }

    function bar(state, msg){
      var b = el('livebar'); if (!b) return;
      b.classList.add('on');
      b.classList.remove('stale','off');
      if (state !== 'live') b.classList.add(state);
      el('livemsg').textContent = msg;
    }

    /* ── boot: is there a ledger behind this page? ── */
    api('/health').then(function(h){
      if (!h || !h.ok) throw new Error(h && h.error ? h.error : 'unreachable');
      live = true;
      var stamp = h.latest_signal_date || '—';
      // "0 open positions" used to sit directly above a table of 67 rows
      // badged OPEN. Both numbers were right and meant different things: a
      // tracked position is one you hold, an OPEN signal is a setup that has
      // not resolved. Naming them differently is the whole fix.
      var tracked = (h.tracked_positions !== undefined ? h.tracked_positions
                                                       : h.open_positions);
      // One engine now, so the bar states ONE count — the v2 one. h.signals
      // is every row ever written including the pre-gate population, so
      // leaving it in put "605 signals · 30 logged" side by side and invited
      // the reader to wonder which was the record. It is 30.
      var logged = (h.by_version && h.by_version.v2 !== undefined)
                 ? h.by_version.v2 : (h.signals || 0);
      var otxt = (h.open_setups || 0) + ' open setups';
      if (h.open_by_version && h.open_by_version.v2 !== undefined){
        otxt = h.open_by_version.v2 + ' open setups';
      }
      // Count the same population the table below shows. The tile read 47
      // (every version ever logged) directly above a live bar reading
      // "17 gated open / 30 legacy open" and a table listing 17 — three
      // numbers for one question. v2 is the gated engine and the only one
      // still generating setups, so that is the honest headline; v1 is a
      // frozen legacy population that nothing acts on any more.
      var heroOpen = h.open_setups || 0, openLbl = 'Open Setups';
      if (h.open_by_version && h.open_by_version.v2 !== undefined){
        heroOpen = h.open_by_version.v2;
        // No "(gated)" qualifier any more — there is nothing to distinguish it
        // from, so the label would only raise a question the page no longer
        // answers.
        openLbl = 'Open Setups';
      }
      setKpi('heroOpen', heroOpen);
      var hk = el('heroOpenK'); if (hk) hk.textContent = openLbl;
      bar('live', 'LIVE LEDGER · ' + logged + ' signals' +
                  ' · latest ' + stamp +
                  ' · ' + tracked + ' tracked · ' + otxt +
                  (h.writes_enabled ? '' : ' · read-only (EDIT_KEY not set)'));
      // Wiring the page is NOT part of deciding whether the ledger is up. The
      // health check has already answered that. Left unguarded, any DOM error
      // inside start() lands in the .catch() below and gets reported as
      // "live ledger unavailable (...)" over a live API — which is exactly how
      // /desk spent a day claiming the ledger was down because it has no
      // #perf section to show. A broken widget is a console error, not an
      // outage notice.
      try {
        start();
      } catch (err) {
        console.error('ledger UI failed to wire (the API is fine):', err);
      }
    }).catch(function(e){
      // Reached only when /health itself failed: static host, or the API is
      // down. Say so plainly instead of letting the page pretend a 6 AM
      // snapshot is live data.
      bar('off', 'STATIC SNAPSHOT · rebuilt daily at 6:00 AM IST · live ledger unavailable (' + e.message + ')');
      staticFallback();
      var rb = el('liverefresh'); if (rb) rb.style.display = 'none';
    });

    /* ── static fallback: keep the old behaviour, minus the alert() popup ── */
    function staticFallback(){
      document.querySelectorAll('form[action^="/tracker"], form[action^="/api"]').forEach(function(f){
        f.addEventListener('submit', function(ev){
          ev.preventDefault();
          var note = f.querySelector('.formnote');
          if (!note){
            note = document.createElement('div');
            note.className = 'formnote';
            note.style.cssText = 'margin-top:10px;font-size:12px;color:var(--gold)';
            f.appendChild(note);
          }
          note.textContent = 'This build has no ledger behind it — open news.askakshay.com to edit the book.';
        });
      });
      document.querySelectorAll('a[href^="/tracker"], a[href^="/api"]').forEach(function(a){
        a.addEventListener('click', function(ev){ ev.preventDefault(); });
        a.style.opacity = '.3'; a.style.cursor = 'not-allowed';
      });
    }

    // Null-safe show/hide. This script block is shared by both pages, but the
    // ledger sections (perf, alerts, tracker, archive) are main-page only —
    // SECTION_MAP puts them on "main" and /desk gets languages, chess and
    // music instead. Every el() below used to be dereferenced blind, so on
    // /desk the first line threw and the health .catch() below reported a
    // perfectly healthy API as "live ledger unavailable".
    function show(id, val){
      var n = el(id);
      if (n) n.style.display = val;
      return n;
    }

    function start(){
      // Does this page carry the ledger UI at all? /desk does not, and there
      // is nothing to wire there — but the ledger itself is fine, so this must
      // not be reported as an outage.
      if (!el('perf') && !el('tracker') && !el('alerts')) return;

      show('perf', '');
      show('archWrap', '');
      show('alertCtl', 'flex');
      // Only meaningful with an API behind the page — a static host has one
      // baked-in snapshot and nothing to switch between.
      show('alertVer', 'flex');
      show('posStatic', 'none');
      show('posLive', '');
      show('posHistBtn', '');
      var kb = el('keybox'); if (kb) kb.classList.toggle('on', !editKey());

      // Flask-only controls. There is no /tracker/* on the serverless host, so
      // rather than leave buttons that 404, hide them — "Closed positions"
      // above already replaces the exit-history link.
      document.querySelectorAll('form[action^="/tracker/obsidian"], a[href^="/tracker/history"]')
        .forEach(function(n){ n.style.display = 'none'; });

      ensureAlertTable();
      ['perf','archWrap','alertCtl','alerts','tracker'].forEach(function(id){ reveal(el(id)); });
      wireKey();
      wireTracker();
      wireAlertControls();
      wireSheet();
      wirePerfControls();

      // Context first, then the rows — otherwise the first render has no
      // prices and every distance cell is blank until something re-renders.
      loadOpenCtx().then(loadSignals);
      loadArchive();
      loadStats();
      loadPositions();
      loadSip();
      loadLongTerm();

      var lr = el('liverefresh');
      if (lr) lr.addEventListener('click', function(){
        loadSignals(); loadStats(); loadPositions(); loadSip();
      });

      // Deep link: /day/2026-07-31 opens straight into that day's archive.
      var m = location.pathname.match(/^\/day\/(\d{4}-\d{2}-\d{2})/);
      if (m) selectDay(m[1]);

      // ?signal=<id> opens that trade's sheet. Deferred until the rows are in
      // memory — the sheet renders from allRows, not from a second fetch, so
      // it cannot show a different number from the table behind it.
      var sid = new URLSearchParams(location.search).get('signal');
      if (sid) pendingSheet = sid;
    }

    /* ═══════ edit key ═══════ */
    function wireKey(){
      el('keySave').addEventListener('click', function(){
        var v = el('keyInput').value.trim();
        if (!v) return;
        try { localStorage.setItem(KEY_LS, v); } catch(e){}
        el('keybox').classList.remove('on');
        loadPositions();
      });
      el('keyInput').addEventListener('keydown', function(ev){
        if (ev.key === 'Enter') el('keySave').click();
      });
    }

    /* ═══════ positions ═══════ */
    var showingHistory = false;

    function loadPositions(){
      var box = el('posLive');
      box.innerHTML = '<div class="empty">Loading the book…</div>';
      api('/tracker' + (showingHistory ? '?history=1' : '')).then(function(j){
        if (!j.ok) { box.innerHTML = '<div class="empty">Could not load positions: ' + esc(j.error) + '</div>'; return; }
        renderPositions(j);
      });
    }

    function renderPositions(j){
      var box = el('posLive');
      var rows = j.positions || [];
      // Deliberately does NOT touch heroOpen. That tile counts unresolved
      // SETUPS in the ledger; this list is the tracked book. Writing the book
      // count there is what put "0 Open Positions" above five OPEN rows.
      if (!rows.length){
        box.innerHTML = '<div class="empty">' +
          (showingHistory ? 'No closed positions yet.'
                          : 'No open positions. Hit <strong style="color:var(--lime)">+ Track</strong> on any trade idea, or add one below.') +
          '</div>';
        return;
      }
      var warn = rows.filter(function(r){ return r.alert; });
      var html = '';
      if (warn.length && !showingHistory){
        html += '<div class="ctlbar"><span class="ghost" style="margin-left:0;color:var(--gold)">⚠ ' +
                warn.length + ' position' + (warn.length > 1 ? 's need' : ' needs') + ' attention — ' +
                esc(warn.map(function(r){ return r.symbol + ' (' + r.alert.replace('-',' ') + ')'; }).join(', ')) +
                '</span></div>';
      }
      html += '<div class="tw"><table class="t" style="min-width:880px"><thead><tr>' +
              '<th scope="col">Symbol</th><th scope="col">Entry</th><th scope="col">Current</th><th scope="col">Target</th><th scope="col">Stop</th>' +
              '<th scope="col">P&amp;L</th><th scope="col">R</th><th scope="col">Horizon</th><th scope="col">Thesis</th><th scope="col">Added</th><th scope="col"></th>' +
              '</tr></thead><tbody>';
      rows.forEach(function(r){
        var cur = r.currency || '₹';
        html += '<tr>' +
          '<td><strong class="sym">' + esc(r.symbol) + '</strong>' +
            (r.alert ? '<span class="pos-alert ' + r.alert + '">' + r.alert.replace('-',' ') + '</span>' : '') + '</td>' +
          '<td class="num">' + cur + fmt(r.entry_price) + '</td>' +
          '<td class="num ' + (r.winning ? 'up' : 'dn') + '">' + cur + fmt(r.current_price) + '</td>' +
          '<td class="num up">' + (r.target_price ? cur + fmt(r.target_price) : '—') + '</td>' +
          '<td class="num dn">' + (r.stop_loss ? cur + fmt(r.stop_loss) : '—') + '</td>' +
          '<td class="' + (r.winning ? 'pnl-u' : 'pnl-d') + '">' +
            (r.pnl_pct === null ? '—' : (r.pnl_pct > 0 ? '+' : '') + fmt(r.pnl_pct, 2) + '%') + '</td>' +
          '<td class="num">' + (r.r_multiple === null ? '—' : fmt(r.r_multiple, 2) + 'R') + '</td>' +
          '<td class="mono-dim">' + esc(r.timeframe) + '</td>' +
          '<td style="font-size:12px;color:var(--muted);max-width:220px">' + esc((r.thesis || '').slice(0, 70)) + '</td>' +
          '<td class="mono-dim">' + esc(r.added_date) + '</td>' +
          '<td>' + (showingHistory ? '<span class="mono-dim">' + esc(r.status) + '</span>'
                                   : '<button type="button" class="btn-gh" data-exit="' + r.id + '">Exit</button>') + '</td>' +
          '</tr>';
      });
      html += '</tbody></table></div>';
      box.innerHTML = html;
      reveal(box);

      box.querySelectorAll('[data-exit]').forEach(function(b){
        b.addEventListener('click', function(){
          if (!confirm('Close this position?')) return;
          b.disabled = true; b.textContent = '…';
          api('/tracker', { method:'POST', body: JSON.stringify({ action:'exit', id: Number(b.dataset.exit) }) })
            .then(function(r){
              if (!r.ok){ b.disabled = false; b.textContent = 'Exit'; keyError(r.error); return; }
              loadPositions();
            });
        });
      });
    }

    function keyError(msg){
      el('keybox').classList.add('on');
      el('keybox').querySelector('span').textContent = msg || 'Write refused.';
    }

    function wireTracker(){
      el('posHistBtn').addEventListener('click', function(){
        showingHistory = !showingHistory;
        this.textContent = showingHistory ? 'Open positions' : 'Closed positions';
        loadPositions();
      });

      // Both the manual form and every "+ Track" button on the trade ideas
      // become real writes instead of dead POSTs to a server that isn't there.
      document.querySelectorAll('form[action^="/tracker/add"]').forEach(function(f){
        f.addEventListener('submit', function(ev){
          ev.preventDefault();
          var d = {};
          new FormData(f).forEach(function(v, k){ d[k] = v; });
          var btn = f.querySelector('button[type=submit]');
          var label = btn ? btn.textContent : '';
          if (btn){ btn.disabled = true; btn.textContent = 'Saving…'; }
          api('/tracker', { method:'POST', body: JSON.stringify(d) }).then(function(r){
            if (btn){ btn.disabled = false; btn.textContent = r.ok ? '✓ Tracked' : label; }
            if (!r.ok){ keyError(r.error); return; }
            f.reset();
            loadPositions();
            if (btn) setTimeout(function(){ btn.textContent = label; }, 2200);
          });
        });
      });
    }

    /* ═══════ signal feed + search ═══════ */

    // The KPI row, filter buttons and table only exist when the daily build
    // found signals. If the build ran while the ledger was unreachable, the
    // section holds an empty-state div instead — rebuild the scaffolding so
    // live data still has somewhere to land.
    function ensureAlertTable(){
      if (document.querySelector('#alertTable tbody')) return;
      var sec = el('alerts');
      var empty = sec.querySelector('.empty');
      var host = document.createElement('div');
      host.innerHTML =
        '<div class="kpi-row rv">' +
          '<div class="kpi"><div class="v up" id="kpiWin">0</div><div class="k">Targets Hit</div></div>' +
          '<div class="kpi"><div class="v dn" id="kpiLoss">0</div><div class="k">Stops Hit</div></div>' +
          '<div class="kpi"><div class="v" id="kpiOpen" style="color:var(--blue)">0</div><div class="k">Open</div></div>' +
          '<div class="kpi"><div class="v" id="kpiRate" style="color:var(--lime)">—</div><div class="k">Win Rate</div></div>' +
          '<div class="kpi"><div class="v" id="kpiTotal">0</div><div class="k">Total Signals</div></div>' +
        '</div>' +
        '<div class="filters rv">' +
          '<button class="fbtn on" data-f="all">All</button>' +
          '<button class="fbtn" data-f="open">Open</button>' +
          '<button class="fbtn" data-f="win">Target Hit</button>' +
          '<button class="fbtn" data-f="loss">Stop Hit</button>' +
          '<button class="fbtn" data-f="cancelled">Cancelled</button>' +
        '</div>' +
        // No engine switch. One engine, one record — see the note on
        // #alertVer above.
        ''  +
        '<div class="tw tw-tall rv"><table class="t" id="alertTable"><thead><tr>' +
          '<th scope="col">Date</th><th scope="col">Symbol</th><th scope="col">Signal</th><th scope="col">TF</th><th scope="col">Grade</th><th scope="col">Entry</th><th scope="col">SL</th>' +
          '<th scope="col">T1</th><th scope="col">T2</th><th scope="col">RR</th><th scope="col">B/E WR</th>' +
          '<th scope="col">Last</th><th scope="col">Exit</th><th scope="col">P&amp;L</th><th scope="col">Closed</th><th scope="col">Status</th>' +
        '</tr></thead><tbody></tbody></table></div>';
        // No #sheet here — it is in the static section markup. Two copies
        // would give two elements with the same id and openSheet() would fill
        // whichever the DOM handed back first.
      if (empty) empty.replaceWith(host); else sec.appendChild(host);
      reveal(host);
    }

    function activeVersion(){
      var on = document.querySelector('.fbtn.on[data-v]');
      return on ? on.dataset.v : 'v2';
    }

    function loadSignals(){
      var qs = archDate ? '?date=' + archDate + '&limit=2000' : '?limit=800';
      qs += '&version=' + activeVersion();
      api('/signals' + qs).then(function(j){
        if (!j.ok) return;
        allRows = j.signals || [];
        fillTfSelect(el('alertTfSel'), allRows);
        fillEngSelect(el('alertEngSel'), allRows);
        renderAlerts();
        paintHeat(allRows);
        if (pendingSheet){ var sid = pendingSheet; pendingSheet = null; openSheet(sid); }
      });
    }

    function fillTfSelect(sel, rows){
      if (!sel || sel.dataset.filled) return;
      var seen = {};
      rows.forEach(function(r){ if (r.timeframe) seen[r.timeframe] = 1; });
      Object.keys(seen).sort().forEach(function(tf){
        var o = document.createElement('option');
        o.value = tf; o.textContent = tf; sel.appendChild(o);
      });
      sel.dataset.filled = '1';
    }

    function fillEngSelect(sel, rows){
      if (!sel || sel.dataset.filled) return;
      var seen = {};
      rows.forEach(function(r){ if (r.signal_type) seen[r.signal_type] = 1; });
      Object.keys(seen).sort().forEach(function(t){
        var o = document.createElement('option');
        o.value = t; o.textContent = t; sel.appendChild(o);
      });
      sel.dataset.filled = '1';
    }

    function activeBadge(){
      var on = document.querySelector('.fbtn.on[data-f]');
      return on ? on.dataset.f : 'all';
    }

    // Mirrors newspaper.tv_alert_symbol(). Commodities, FX and crypto are not
    // NSE symbols; prefixing them anyway produced chart links that 404.
    function tvSym(sym, cur){
      var s = (sym || '').toUpperCase();
      if (TV_ALIASES[s]) return TV_ALIASES[s];
      return cur === '\u20b9' || !cur ? 'NSE:' + s : '';
    }
    function symCell(sym, cur){
      var tv = tvSym(sym, cur);
      if (!tv) return esc(sym);          // no chart beats a broken chart
      return '<a class="sym" href="https://www.tradingview.com/chart/?symbol=' +
             encodeURIComponent(tv) + '" target="_blank" rel="noopener">' +
             esc(sym) + '</a>';
    }

    /* ══════════ the trade sheet ══════════
       One trade, in full: the levels drawn to scale, the R arithmetic that
       produced the number in the table, and the lifecycle stamps.

       The levels diagram is deliberately NOT a price chart. Drawing candles
       would mean fetching OHLC per trade, and the ledger has no bar data — a
       chart built from entry/stop/exit alone would be an illustration of four
       numbers pretending to be a price path. The scale bar shows exactly what
       is known: where the levels sat relative to each other, and where the
       exit landed among them. TradingView is one click away for the real
       chart. */
    function sheetFor(a){
      var cur = a.currency;
      var risk = (a.entry !== null && a.sl !== null) ? Math.abs(a.entry - a.sl) : null;
      var isLong = (a.action || 'BUY').toUpperCase() !== 'SELL';

      function rOf(px){
        if (px === null || !risk) return null;
        return (isLong ? (px - a.entry) : (a.entry - px)) / risk;
      }
      function rTxt(px){
        var r = rOf(px);
        return r === null ? '' : (r > 0 ? '+' : '') + fmt(r, 2) + 'R';
      }

      // Scale every level onto one axis so the geometry is visible: a target
      // sitting a fifth of the way to the stop LOOKS wrong here, which is the
      // whole point — that is how the 0.19R first targets hid in plain sight.
      var pts = [a.entry, a.sl, a.target1, a.target2, a.exit_price]
                  .filter(function(v){ return v !== null && isFinite(v); });
      var lo = Math.min.apply(null, pts), hi = Math.max.apply(null, pts);
      var span = (hi - lo) || 1;
      function pos(v){ return ((v - lo) / span) * 100; }

      function marker(label, v, cls){
        if (v === null || !isFinite(v)) return '';
        // A label is 74px wide and centred on its tick, so one sitting at 0%
        // or 100% hangs off the axis and collides with its neighbour — EXIT
        // and T2 overlapped at the right edge. Anchor the end labels inward
        // instead of centring them; the tick itself stays exact.
        var pc = pos(v), edge = pc < 8 ? ' at-start' : pc > 92 ? ' at-end' : '';
        return '<div class="lv ' + cls + edge + '" style="left:' + pc.toFixed(1) + '%">' +
               '<i></i><span class="lv-l">' + label + '</span>' +
               '<span class="lv-v">' + money(v, cur) + '</span>' +
               '<span class="lv-r">' + rTxt(v) + '</span></div>';
      }

      // Lifecycle. Only stamps that exist are shown — an empty step is a fact
      // about the pipeline (nothing recorded it) and inventing one would be
      // the opposite of an audit trail.
      var steps = [
        ['Generated', a.date, 'the engine produced it'],
        ['Sent', (a.sent_at || '').slice(0, 16).replace('T', ' '), 'delivered to Telegram'],
        ['Entry touched', (a.entry_triggered_at || '').slice(0, 10),
         a.fill_type ? ('fill: ' + esc(a.fill_type)) : 'price traded through the entry'],
        ['Closed', a.closed_at, a.status ? esc(a.status) : '']
      ].filter(function(x){ return x[1]; });

      /* Every engine already stores what it gated on, in metadata, and the
         sheet showed none of it — so a row said WHAT fired and never WHY.
         That was worst for the weekly multibagger scan, which reached the
         site as a name and a target in the ticker and nothing else.

         Deliberately generic: any engine that writes metadata gets this block
         for free. LABELS maps the keys worth naming; anything else is skipped
         rather than dumped, because a sheet full of raw JSON keys is not an
         explanation either. */
      var LABELS = {
        range_pos: '% of 52w range', wk_rsi: 'Weekly RSI', wk_adx: 'Weekly ADX',
        vol_ratio: 'Volume vs 20d', high_52w: '52w high', low_52w: '52w low',
        support1: 'Support 1', support2: 'Support 2', pe: 'P/E',
        horizon: 'Horizon', cadence: 'Scan cadence', sector: 'Sector',
        fund_score: 'Fundamental score', tech_score: 'Technical score',
        coverage: 'Data coverage'
      };
      function why(a){
        var m = a.metadata || {};
        var rows = Object.keys(LABELS).filter(function(k){
          var v = m[k];
          return v !== null && v !== undefined && v !== '';
        }).map(function(k){
          var v = m[k];
          if (typeof v === 'number') v = fmt(v, Math.abs(v) >= 100 ? 0 : 2);
          return '<div class="wy-row"><span class="wy-k">' + esc(LABELS[k]) +
                 '</span><span class="wy-v">' + esc(v) + '</span></div>';
        });
        // reason / rationale is the engine's own sentence. It leads, because
        // it is the part a human actually reads.
        var prose = m.reason || m.rationale || m.thesis || '';
        if (!rows.length && !prose) return '';
        return '<div class="sheet-why">' +
          '<h4>Why this fired' + (m.engine ? ' · ' + esc(m.engine) : '') + '</h4>' +
          (prose ? '<p class="wy-p">' + esc(prose) + '</p>' : '') +
          (rows.length ? '<div class="wy-grid">' + rows.join('') + '</div>' : '') +
          (m.fno ? '<p class="wy-p mono-dim">In the F&amp;O segment.</p>' : '') +
        '</div>';
      }

      var flags = [];
      if (a.exit_ambiguous) flags.push('One bar touched BOTH stop and target. Daily data cannot say which came first, so the STOP was booked — the unflattering assumption, counted rather than hidden.');
      if (a.regraded_at) flags.push('This outcome was corrected on ' + esc(a.regraded_at.slice(0, 10)) + ' by a later audit.');
      if (a.badge === 'open') flags.push('Still open. Nothing here is a result yet.');

      var tv = tvSym(a.symbol, cur);

      return '<div class="sheet-h">' +
          '<div><span class="sheet-sym">' + esc(a.symbol) + '</span> ' +
            '<span class="badge badge-' + a.badge + '">' + esc(a.status || a.badge) + '</span></div>' +
          '<div class="mono-dim">' + esc(a.signal_type || '') + ' · ' + esc(a.timeframe || '') +
            (a.grade ? ' · grade ' + esc(a.grade) : '') + '</div>' +
        '</div>' +

        '<div class="sheet-kpi">' +
          '<div><b class="' + (a.r_multiple > 0 ? 'up' : a.r_multiple < 0 ? 'dn' : '') + '">' +
            (a.r_multiple === null ? '—' : (a.r_multiple > 0 ? '+' : '') + fmt(a.r_multiple, 2) + 'R') +
            '</b><span>outcome</span></div>' +
          '<div><b>' + (a.rr === null ? '—' : fmt(a.rr, 2) + 'x') + '</b><span>R:R to T2</span></div>' +
          '<div><b>' + (risk === null ? '—' : money(risk, cur)) + '</b><span>risk / unit</span></div>' +
          '<div><b>' + beWr(a) + '</b><span>break-even WR</span></div>' +
        '</div>' +

        '<div class="scale">' +
          marker('Stop', a.sl, 'sl') + marker('Entry', a.entry, 'en') +
          marker('T1', a.target1, 't1') + marker('T2', a.target2, 't2') +
          marker('Exit', a.exit_price, 'ex') +
        '</div>' +
        '<p class="scale-note">Levels to scale. Not a price chart — the ledger stores levels, not bars.</p>' +

        '<div class="sheet-tl">' + steps.map(function(st){
          return '<div class="tl-row"><span class="tl-k">' + st[0] + '</span>' +
                 '<span class="tl-v">' + esc(st[1]) + '</span>' +
                 '<span class="tl-w">' + st[2] + '</span></div>';
        }).join('') + '</div>' +

        (flags.length ? '<div class="sheet-flags">' + flags.map(function(f){
          return '<p>⚠ ' + f + '</p>'; }).join('') + '</div>' : '') +

        why(a) +

        // ── size it ──
        // The gap between "here is a setup" and "here is what I do" is one
        // division nobody does under pressure, and getting it wrong is how a
        // short went 88x. Quantity comes from RISK, never from capital: you
        // size so that a stop costs a fixed fraction of the book, and the
        // capital required falls out of that — not the other way round.
        (risk ? '<div class="sizer" data-entry="' + a.entry + '" data-risk="' + risk + '">' +
          '<div class="sizer-h">Size it</div>' +
          '<div class="sizer-in">' +
            '<label>Capital ₹<input type="number" class="szCap" value="500000" min="0" step="10000"></label>' +
            '<label>Risk %<input type="number" class="szPct" value="1" min="0.1" max="5" step="0.1"></label>' +
          '</div>' +
          '<div class="sizer-out"></div>' +
        '</div>' : '') +

        (tv ? '<a class="btn btn-sm" target="_blank" rel="noopener" href="https://www.tradingview.com/chart/?symbol=' +
              encodeURIComponent(tv) + '">Open the real chart on TradingView →</a>' : '');
    }

    /* Position sizing, live inside the sheet.
       Persisted in localStorage because capital and risk tolerance do not
       change per trade — retyping them on every sheet is how people stop
       using the calculator and start guessing. */
    var SZ_LS = 'ds_size_v1';
    function sizerSaved(){
      try { return JSON.parse(localStorage.getItem(SZ_LS) || '{}'); } catch(e){ return {}; }
    }
    function wireSizer(root){
      var box = root.querySelector('.sizer');
      if (!box) return;
      var cap = box.querySelector('.szCap'), pct = box.querySelector('.szPct'),
          out = box.querySelector('.sizer-out');
      var saved = sizerSaved();
      if (saved.cap) cap.value = saved.cap;
      if (saved.pct) pct.value = saved.pct;

      function calc(){
        var C = parseFloat(cap.value), P = parseFloat(pct.value);
        var entry = parseFloat(box.dataset.entry), perUnit = parseFloat(box.dataset.risk);
        if (!isFinite(C) || !isFinite(P) || !isFinite(perUnit) || perUnit <= 0){
          out.innerHTML = ''; return;
        }
        try { localStorage.setItem(SZ_LS, JSON.stringify({ cap: C, pct: P })); } catch(e){}

        var riskAmt = C * (P / 100);
        var qty = Math.floor(riskAmt / perUnit);
        var deployed = qty * entry;
        // Sizing off risk can demand more capital than exists — a tight stop
        // on an expensive share is exactly that case. Say so instead of
        // printing a quantity that cannot be bought.
        var over = deployed > C;
        out.innerHTML =
          '<div class="sz-row"><span>Quantity</span><b>' + qty.toLocaleString('en-IN') + '</b></div>' +
          '<div class="sz-row"><span>Capital deployed</span><b>₹' +
            Math.round(deployed).toLocaleString('en-IN') + '</b></div>' +
          '<div class="sz-row"><span>Risked if stopped</span><b class="dn">₹' +
            Math.round(qty * perUnit).toLocaleString('en-IN') + '</b></div>' +
          (over
            ? '<p class="sz-warn">That is ₹' + Math.round(deployed - C).toLocaleString('en-IN') +
              ' more than the capital entered. The stop is tight relative to the price, so a ' +
              P + '% risk needs a bigger book than you have. Size down or skip it.</p>'
            : '<p class="sz-note">Quantity is derived from RISK, not capital. ' +
              'Whole units only — no fractional shares.</p>');
      }
      cap.addEventListener('input', calc);
      pct.addEventListener('input', calc);
      calc();
    }

    function openSheet(id){
      var a = null;
      for (var i = 0; i < allRows.length; i++){
        if (String(allRows[i].id) === String(id)) { a = allRows[i]; break; }
      }
      var box = el('sheet');
      if (!a || !box) return;
      el('sheetBody').innerHTML = sheetFor(a);
      wireSizer(el('sheetBody'));
      box.hidden = false;
      document.body.style.overflow = 'hidden';
      // Deep link without a navigation, so Back closes the sheet.
      try { history.pushState({ sheet: id }, '', '?signal=' + encodeURIComponent(id)); } catch(e){}
    }

    function closeSheet(push){
      var box = el('sheet');
      if (!box || box.hidden) return;
      box.hidden = true;
      document.body.style.overflow = '';
      if (push !== false){
        try { history.pushState({}, '', location.pathname); } catch(e){}
      }
    }

    function wireSheet(){
      var tbl = el('alertTable');
      if (tbl) tbl.addEventListener('click', function(ev){
        var tr = ev.target.closest ? ev.target.closest('tr[data-sid]') : null;
        // A click on the symbol's TradingView link is not a request for the
        // sheet; let the anchor win.
        if (!tr || !tr.dataset.sid || (ev.target.closest && ev.target.closest('a'))) return;
        openSheet(tr.dataset.sid);
      });
      var x = el('sheetX'); if (x) x.addEventListener('click', function(){ closeSheet(); });
      var sh = el('sheet');
      if (sh) sh.addEventListener('click', function(ev){
        if (ev.target === sh) closeSheet();     // click the backdrop
      });
      document.addEventListener('keydown', function(e){
        if (e.key === 'Escape') closeSheet();
      });
      window.addEventListener('popstate', function(){ closeSheet(false); });
    }

    /* ══════════ portfolio heat ══════════
       Every open setup, priced as risk. One trade at 1% is nothing; twenty
       open setups at 1% each is the whole book on the line, and the ledger
       has been showing 20 open at once. This is the number that says whether
       "take the signals" is a plan or a margin call — and it is deliberately
       counted on OPEN setups, not on positions held, because that is what a
       reader of this page is being handed. */
    function paintHeat(rows){
      var box = el('heat');
      if (!box) return;
      var open = rows.filter(function(r){ return r.badge === 'open'; });
      if (!open.length){ box.innerHTML = ''; return; }

      var saved = sizerSaved();
      var pct = saved.pct || 1;
      var total = open.length * pct;

      function tally(keyOf){
        var m = {};
        open.forEach(function(r){ var k = keyOf(r) || 'unknown'; m[k] = (m[k] || 0) + 1; });
        return Object.keys(m).sort(function(a, b){ return m[b] - m[a]; })
                 .map(function(k){ return { k: k, n: m[k] }; });
      }
      var byEngine = tally(function(r){ return r.signal_type; });
      var bySector = tally(function(r){
        var c = OPEN_CTX[r.symbol]; return c && c.sector ? c.sector : '';
      });

      // The concentration that actually matters. Twenty setups at 1% is 20%
      // of the book ONLY if they move independently — and they do not. The
      // largest single cluster is the number that decides how bad a bad day
      // gets, so it is stated separately rather than buried in a list.
      var topSector = bySector.filter(function(x){ return x.k !== 'unknown'; })[0];
      var topEngine = byEngine[0];
      var clusterPct = topSector ? topSector.n * pct : 0;
      var hot = total > 6;

      function line(list, noun){
        return list.slice(0, 4).map(function(x){
          return x.n + ' ' + esc(x.k);
        }).join(' · ') + (list.length > 4 ? ' · +' + (list.length - 4) + ' more' : '');
      }

      box.innerHTML =
        '<div class="heat-h"><span class="eyebrow">Portfolio heat</span>' +
          '<span class="eyebrow">' + open.length + ' open · ' + pct + '% each</span></div>' +
        '<div class="heat-n ' + (hot ? 'hot' : '') + '">' + fmt(total, 1) + '%</div>' +
        '<div class="heat-g">' +
          '<div><span>By engine</span><b>' + line(byEngine) + '</b></div>' +
          (bySector.length && topSector
            ? '<div><span>By sector</span><b>' + line(bySector) + '</b></div>' : '') +
        '</div>' +
        '<p class="heat-w">' +
          'Taking every open setup at ' + pct + '% risks ' + fmt(total, 1) +
          '% of the book at once' + (hot ? ' — above the ~6% where a correlated move stops being a bad week' : '') + '. ' +
          (topEngine && topEngine.n > 1
            ? 'But ' + topEngine.n + ' of them come from <b>' + esc(topEngine.k) +
              '</b> alone, so they are not ' + open.length + ' independent bets. '
            : '') +
          (clusterPct >= 3
            ? 'The largest sector cluster is <b>' + esc(topSector.k) + '</b> at ' +
              topSector.n + ' setups — ' + fmt(clusterPct, 1) +
              '% of the book riding one sector move.'
            : topSector
              ? 'No single sector holds more than ' + fmt(clusterPct, 1) + '% of the book.'
              : '') +
        '</p>';
    }

    /* Live price + sector per open setup, written by the 6 AM build into
       today.json. A static file rather than an endpoint because the Vercel
       Hobby plan is already at its 12-function ceiling, and because the
       browser cannot do a sector lookup at all. Empty on a static host, which
       degrades to exactly what the page showed before. */
    var OPEN_CTX = {};
    function loadOpenCtx(){
      return fetch('/today.json', { cache: 'no-store' })
        .then(function(r){ return r.ok ? r.json() : null; })
        .then(function(j){ if (j && j.open_context) OPEN_CTX = j.open_context; })
        .catch(function(){ /* static host: distance and sector simply absent */ });
    }

    var pendingSheet = null;

    // Called by paintTicker each time live quotes land (first load, then every
    // 5 minutes). Repaints Last and running P&L without refetching the ledger.
    window.__onLedgerPx = function(){
      if (document.querySelector('#alertTable tbody')) renderAlerts();
    };

    function renderAlerts(){
      var tbody = document.querySelector('#alertTable tbody');
      if (!tbody) return;
      var q     = (el('alertSearch').value || '').trim().toUpperCase();
      var from  = el('alertFrom').value;
      var to    = el('alertTo').value;
      var tf    = el('alertTfSel').value;
      var eng   = (el('alertEngSel') || {}).value || '';
      var badge = activeBadge();

      var rows = allRows.filter(function(r){
        if (badge !== 'all' && r.badge !== badge) return false;
        if (q && r.symbol.toUpperCase().indexOf(q) === -1) return false;
        if (from && r.date < from) return false;
        if (to   && r.date > to)   return false;
        if (tf   && r.timeframe !== tf) return false;
        if (eng  && r.signal_type !== eng) return false;
        return true;
      });

      // 800 rows of innerHTML is fine; building them one node at a time is not.
      var html = rows.map(function(a){
        var badgeTxt = a.badge === 'win' ? '✅ Win' : a.badge === 'loss' ? '❌ Stop'
                     : a.badge === 'open' ? '🔵 Open' : (a.status || '—');
        return '<tr data-badge="' + a.badge + '" data-sid="' + (a.id === null ? '' : a.id) +
               '" class="' + (a.id === null ? '' : 'clickable') + '">' +
          '<td class="mono-dim">' + esc(a.date) + '</td>' +
          '<td>' + symCell(a.symbol, a.currency) + '</td>' +
          '<td class="' + (a.action === 'BUY' ? 'up' : 'dn') + '" style="font-weight:600">' + esc(a.action) +
              (a.signal_type ? '<span class="mono-dim" style="font-size:10px"> · ' + esc(a.signal_type) + '</span>' : '') + '</td>' +
          '<td class="mono-dim">' + esc(a.timeframe || '—') + '</td>' +
          '<td class="mono-dim">' + gradeCell(a) + '</td>' +
          '<td class="num">' + money(a.entry, a.currency) + distCell(a) + '</td>' +
          '<td class="num dn">' + money(a.sl, a.currency) + '</td>' +
          '<td class="num up">' + money(a.target1, a.currency) + '</td>' +
          '<td class="num up">' + money(a.target2, a.currency) + '</td>' +
          '<td class="num" style="color:var(--gold)">' + (a.rr === null ? '—' : fmt(a.rr, 1) + 'x') + '</td>' +
          // The win rate this setup needs just to break even, 1/(1+R). Shown
          // next to R:R because the two are the same fact and only one of them
          // is obvious. v1 rows predate the stored column, so derive it from
          // R:R — those are precisely the rows worth seeing it on.
          '<td class="num mono-dim">' + beWr(a) + '</td>' +
          '<td class="num">' + lastCell(a) + '</td>' +
          '<td class="num">' + money(a.exit_price, a.currency) + '</td>' +
          pnlCell(a) +
          '<td class="mono-dim">' + esc(a.closed_at) + '</td>' +
          '<td><span class="badge badge-' + a.badge + '">' + badgeTxt + '</span></td>' +
          '</tr>';
      }).join('');

      // The empty state has to name the actual reason. It used to always blame
      // the v2 gate — so with an archive day still selected, three live gated
      // signals sitting in the ledger read as "no gated signals yet".
      var why;
      if (archDate){
        why = 'No signals on ' + esc(archDate) + ' for this filter. ' +
              '<a href="#" id="archClear" style="color:var(--lime)">Show all days</a> to see the rest.';
      } else if (from || to || tf || eng || el('alertSearch').value.trim()){
        why = 'Nothing matches those filters.';
      } else if (activeVersion() === 'v2'){
        why = 'No gated signals yet. The v2 engine publishes only setups that clear ' +
              'their engine’s measured break-even R:R — quiet is the intended ' +
              'state.';
      } else {
        why = 'Nothing matches those filters.';
      }
      tbody.innerHTML = html ||
        '<tr><td colspan="15" style="padding:26px;text-align:center;color:var(--dim)">' +
        why + '</td></tr>';
      var clear = document.getElementById('archClear');
      if (clear) clear.addEventListener('click', function(ev){ ev.preventDefault(); selectDay(null); });

      // Spell out the full span. The table always held every signal, but with
      // the newest first it read as though the history stopped a week back.
      var span = '';
      if (allRows.length){
        var ds = allRows.map(function(r){ return r.date; }).filter(Boolean).sort();
        var uniq = ds.filter(function(v, i){ return ds.indexOf(v) === i; });
        span = ' · ' + ds[0] + ' → ' + ds[ds.length - 1] + ' · ' + uniq.length + ' trading days';
      }
      el('alertCount').textContent = rows.length + ' of ' + allRows.length + ' shown' + span +
        (archDate ? ' · archive ' + archDate : '');

      // KPI row reflects what is actually on screen.
      var w = rows.filter(function(r){ return r.badge === 'win';  }).length;
      var l = rows.filter(function(r){ return r.badge === 'loss'; }).length;
      var o = rows.filter(function(r){ return r.badge === 'open'; }).length;
      setKpi('kpiWin', w); setKpi('kpiLoss', l); setKpi('kpiOpen', o);
      setKpi('kpiRate', (w + l) ? Math.round(w / (w + l) * 100) + '%' : '—');
      setKpi('kpiTotal', rows.length);
    }

    function setKpi(id, v){
      var n = el(id); if (!n) return;
      // data-count drives a 1.1s count-up animation that writes textContent on
      // every frame. Removing the attribute is not enough — an already-running
      // loop captured its target and would animate straight over this value.
      // The flag makes that loop stand down.
      n.dataset.live = '1';
      n.removeAttribute('data-count');
      n.textContent = v;
    }

    function wireAlertControls(){
      ['alertSearch','alertFrom','alertTo','alertTfSel','alertEngSel'].forEach(function(id){
        var n = el(id); if (!n) return;
        n.addEventListener('input', renderAlerts);
        n.addEventListener('change', renderAlerts);
      });
      // Take over the badge buttons. The original handler shows/hides rows
      // directly, which would fight with the search filter, so it is unbound
      // by cloning the node.
      document.querySelectorAll('.fbtn[data-f]').forEach(function(b){
        var c = b.cloneNode(true);
        b.parentNode.replaceChild(c, b);
        c.addEventListener('click', function(){
          document.querySelectorAll('.fbtn[data-f]').forEach(function(x){ x.classList.remove('on'); });
          c.classList.add('on');
          renderAlerts();
        });
      });
      // Engine-version buttons re-query the API rather than filtering in the
      // browser: the 800-row page would otherwise be drawn entirely from
      // whichever version happened to fill it.
      document.querySelectorAll('.fbtn[data-v]').forEach(function(b){
        b.addEventListener('click', function(){
          document.querySelectorAll('.fbtn[data-v]').forEach(function(x){ x.classList.remove('on'); });
          b.classList.add('on');
          loadSignals();
        });
      });
    }



    /* ═══════ subscribe ═══════
       The whole conversion surface. Posts to /api/subscribe, which stores to
       the same Turso database the ledger uses — no third-party ESP, so the
       list stays exportable and no visitor data leaves the origin. */
    (function(){
      var opened = Date.now();
      document.querySelectorAll('.sub-form').forEach(function(f){
        var msg = f.parentElement.querySelector('.sub-msg');
        var btn = f.querySelector('button');
        var input = f.querySelector('input[type=email]');
        var label = btn.textContent;

        f.addEventListener('submit', function(ev){
          ev.preventDefault();
          var email = (input.value || '').trim();
          if (!email){ show('err', 'Enter an email address.'); input.focus(); return; }

          btn.disabled = true; btn.textContent = 'Sending…'; show('', '');
          fetch(API + '/subscribe', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              email: email,
              company: f.querySelector('input[name=company]').value,   // honeypot
              source: f.closest('.sub-cta').dataset.src || 'unknown',
              elapsed: (Date.now() - opened) / 1000
            })
          }).then(function(r){ return r.json().catch(function(){ return {ok:false}; }); })
            .then(function(j){
              btn.disabled = false; btn.textContent = label;
              if (j && j.ok){
                f.reset();
                show('ok', "You're on. First edition lands at 6 AM IST.");
              } else {
                show('err', (j && j.error) || 'Could not save that — try again.');
              }
            })
            .catch(function(){
              btn.disabled = false; btn.textContent = label;
              show('err', 'Network error. Try again in a moment.');
            });
        });

        function show(kind, text){
          if (!msg) return;
          msg.className = 'sub-msg' + (kind ? ' ' + kind : '');
          msg.textContent = text;
        }
      });
    })();


    /* ═══════ hero equity curve ═══════
       The record, above the fold. Same data as the performance section below;
       this one just has to be readable in one glance. */
    function paintHeroCurve(j){
      var box = el('heroCurve');
      if (!box || !j || !j.ok) return;
      var pts = (j.equity_curve || []).filter(function(p){ return isFinite(p.cum_r); });
      if (pts.length < 5) return;

      var W = 600, H = 96, PAD = 6;
      var ys = pts.map(function(p){ return p.cum_r; });
      var lo = Math.min.apply(null, ys), hi = Math.max.apply(null, ys);
      if (hi === lo) hi = lo + 1;
      var x = function(i){ return (i / (pts.length - 1)) * W; };
      var y = function(v){ return PAD + (1 - (v - lo) / (hi - lo)) * (H - PAD * 2); };

      var d = '';
      pts.forEach(function(p, i){ d += (i ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(p.cum_r).toFixed(1) + ' '; });
      var line = el('hcLine'), area = el('hcArea'), dot = el('hcDot');
      line.setAttribute('d', d);
      area.setAttribute('d', d + 'L' + W + ' ' + H + ' L0 ' + H + ' Z');
      dot.setAttribute('cx', x(pts.length - 1).toFixed(1));
      dot.setAttribute('cy', y(ys[ys.length - 1]).toFixed(1));

      // Dash length drives the draw-in. Measured, not guessed, because the path
      // length depends on how volatile the curve actually is.
      try { line.style.setProperty('--len', Math.ceil(line.getTotalLength())); } catch(e){}

      var last = ys[ys.length - 1];
      var tot = el('hcTotal');
      tot.textContent = (last > 0 ? '+' : '') + last.toFixed(1) + 'R';
      tot.style.color = last >= 0 ? 'var(--lime)' : 'var(--down)';
      line.setAttribute('stroke', last >= 0 ? 'var(--lime)' : 'var(--down)');
      dot.setAttribute('fill', last >= 0 ? 'var(--lime)' : 'var(--down)');
      el('hcFrom').textContent = pts[0].date || '';
      el('hcTo').textContent = (pts[pts.length - 1].date || '') + ' · ' + pts.length + ' closed';
      box.hidden = false;
    }

    /* ═══════ long-term conviction ═══════ */
    function loadLongTerm(){
      api('/signals?type=ai_longterm&version=v2&limit=40').then(function(j){
        if (!j || !j.ok) return;
        var rows = j.signals || [];
        var sec = el('longterm'); if (!sec) return;
        if (!rows.length) return;             // keeps the explanatory empty state

        // Newest run only, and one card per company. Filtering on date alone
        // was not enough: two runs on the same day — the weekly scan plus an
        // on-demand one — both stamp today, so the section rendered ten cards
        // for seven companies with three shown twice. Rows arrive newest-first,
        // so the first sighting of a symbol is the current view of it.
        var newest = rows[0].date;
        var seenSym = {};
        rows = rows.filter(function(r){
          if (r.date !== newest) return false;
          if (seenSym[r.symbol]) return false;
          seenSym[r.symbol] = 1;
          return true;
        });

        el('ltBody').innerHTML =
          '<div class="ltgrid">' + rows.map(function(r, i){
            var m = r.metadata || {};
            var up1 = r.entry ? (r.target1 / r.entry - 1) * 100 : null;
            var up2 = r.entry ? (r.target2 / r.entry - 1) * 100 : null;
            var dn  = r.entry ? (1 - r.sl / r.entry) * 100 : null;
            return '<div class="lt rv" style="--d:' + (i * 0.06) + 's">' +
              '<div class="lt-h">' +
                '<div><a class="sym" href="https://www.tradingview.com/chart/?symbol=NSE:' +
                  encodeURIComponent(r.symbol) + '" target="_blank" rel="noopener">' +
                  esc(r.symbol) + '</a>' +
                  '<div class="sec-l">' + esc(m.sector || '') + '</div></div>' +
                '<span class="tag">' + fmt(r.score, 0) + '/100' +
                  (r.grade ? ' · ' + esc(r.grade) : '') + '</span>' +
              '</div>' +
              '<div class="px">' + money(r.entry, r.currency) + '</div>' +
              '<div class="scorebar" style="--w:' + Math.min(100, r.score || 0) + '%"><i></i></div>' +
              (m.thesis ? '<div class="th">' + esc(m.thesis) + '</div>' : '') +
              (m.rationale ? '<div class="facts">' + esc(m.rationale) + '</div>' : '') +
              '<div class="lvl">' +
                '<div><div class="k">🎯 T1</div><div class="v up">' + money(r.target1, r.currency) +
                  (up1 === null ? '' : '<span class="pc">+' + fmt(up1, 0) + '%</span>') + '</div></div>' +
                '<div><div class="k">🎯 T2</div><div class="v up">' + money(r.target2, r.currency) +
                  (up2 === null ? '' : '<span class="pc">+' + fmt(up2, 0) + '%</span>') + '</div></div>' +
                '<div><div class="k">🛡 Structure</div><div class="v dn">' + money(r.sl, r.currency) +
                  (dn === null ? '' : '<span class="pc">−' + fmt(dn, 0) + '%</span>') + '</div></div>' +
              '</div>' +
              '<div class="lt-f">' +
                'business ' + fmt(m.fund_score, 0) + ' · chart ' + fmt(m.tech_score, 0) +
                ' · ' + fmt(m.coverage, 0) + '% factor coverage · ' + esc(m.horizon || '2-3 years') +
              '</div>' +
            '</div>';
          }).join('') + '</div>' +
          '<p class="note rv" style="margin-top:14px;color:var(--dim);font-size:12px">' +
          'Ranked ' + esc(newest) + '. The stop is the 200-day structure, not a trade stop — ' +
          'it marks where the reason for owning it stopped being true. Not SEBI advice.</p>';
        reveal(sec);
      }).catch(function(){ /* no API — section stays hidden */ });
    }

    /* ═══════ SIP buckets ═══════ */
    function inr(v, d){
      if (v === null || v === undefined || !isFinite(v)) return '—';
      return '₹' + Number(v).toLocaleString('en-IN', {
        minimumFractionDigits: d === undefined ? 0 : d,
        maximumFractionDigits: d === undefined ? 0 : d });
    }

    function loadSip(){
      api('/sip').then(function(j){
        if (!j || !j.ok) return;
        var sec = el('sip'); if (!sec) return;
        sec.style.display = '';

        var p = j.plan || {};
        el('sipPlan').textContent = '₹' + Number(p.base_monthly || 0).toLocaleString('en-IN') +
          '/mo base · +' + (p.step_up_pct || 0) + '%/yr · SIP year ' + (p.sip_year || 1);
        el('sipMonthly').textContent = inr(p.monthly_amount);

        var t = j.totals || {};
        el('sipBuckets').textContent = t.buckets || 0;
        el('sipInvested').textContent = inr(t.invested);
        el('sipValue').textContent = inr(t.value);
        var pn = el('sipPnl');
        pn.textContent = (t.pnl === null || t.pnl === undefined) ? '—'
          : inr(t.pnl) + (t.pnl_pct === null ? '' : ' (' + fmt(t.pnl_pct, 1) + '%)');
        pn.className = 'v ' + (t.pnl > 0 ? 'up' : t.pnl < 0 ? 'dn' : '');

        // Projection table is pure arithmetic and always renders, even before
        // the first bucket exists — the plan is worth seeing on day one.
        var pb = document.querySelector('#sipProj tbody');
        if (pb) pb.innerHTML = (j.projections || []).map(function(r){
          // 18 years is when the daughter hits college age — the one horizon
          // here anchored to a date rather than a round number, so it is
          // marked rather than left to blend into the others.
          var mark = r.years === 18
            ? ' <span class="mono-dim" style="font-size:10px;color:var(--gold)">college</span>' : '';
          return '<tr><td class="mono-dim">' + r.years + mark + '</td>' +
            '<td class="num">' + inr(r.monthly) + '</td>' +
            '<td class="num mono-dim">' + inr(r.invested) + '</td>' +
            '<td class="num">' + inr(r.r12) + '</td>' +
            '<td class="num up">' + inr(r.r14) + '</td>' +
            '<td class="num up">' + inr(r.r16) + '</td></tr>';
        }).join('');

        var body = el('sipBody');
        if (!j.ready || !(j.buckets || []).length){
          body.innerHTML = '<div class="empty rv">' + esc(j.message ||
            'No buckets yet. The first one is proposed on the next monthly run.') +
            '</div>';
          reveal(sec); return;
        }

        body.innerHTML = (j.buckets || []).map(function(b){
          var hs = (b.holdings || []).map(function(h){
            var live = h.status === 'held' && h.qty && h.buy_price;
            var val  = live ? h.qty * (h.last_price || h.buy_price) : null;
            var cost = live ? h.qty * h.buy_price : null;
            var pct  = live && cost ? (val / cost - 1) * 100 : null;
            // Qty is the whole point of the rework: the bucket now proposes a
            // buyable number of shares, not a rupee slice that may not cover
            // one share of the name it is pointed at.
            return '<tr>' +
              '<td class="mono-dim">' + (h.rank || '') + '</td>' +
              '<td><a class="sym" href="https://www.tradingview.com/chart/?symbol=NSE:' +
                  encodeURIComponent(h.symbol) + '" target="_blank" rel="noopener">' +
                  esc(h.symbol) + '</a></td>' +
              '<td class="num">' + fmt(h.score, 1) + '</td>' +
              '<td class="num">' + (h.ref_price ? inr(h.ref_price, 2) : '—') + '</td>' +
              '<td class="num" style="font-weight:700">' +
                  (h.proposed_qty ? h.proposed_qty + '<span class="mono-dim" style="font-size:10px"> sh</span>' : '—') + '</td>' +
              '<td class="num">' + inr(h.allocated) + '</td>' +
              '<td class="num">' + (h.buy_price ? inr(h.buy_price, 2) : '—') + '</td>' +
              '<td class="num">' + (h.last_price ? inr(h.last_price, 2) : '—') + '</td>' +
              '<td class="' + (pct > 0 ? 'pnl-u' : pct < 0 ? 'pnl-d' : 'num') + '">' +
                  (pct === null ? '—' : (pct > 0 ? '+' : '') + fmt(pct, 1) + '%') + '</td>' +
              '<td><span class="badge badge-' + (h.status === 'held' ? 'open' : 'cancelled') +
                  '">' + esc(h.status) + '</span></td>' +
              '<td class="mono-dim" style="font-size:10px">' + esc(h.rationale || '') + '</td>' +
              '</tr>';
          }).join('');

          var xir = b.xirr_pct === null || b.xirr_pct === undefined
            ? '' : ' · XIRR ' + fmt(b.xirr_pct, 1) + '%';
          var pl  = b.pnl_pct === null || b.pnl_pct === undefined
            ? '' : ' · ' + (b.pnl > 0 ? '+' : '') + fmt(b.pnl_pct, 1) + '%';
          // Whole shares never spend the month to the rupee. Show the gap
          // rather than let the numbers quietly not add up.
          var left = (b.cash_left > 0)
            ? ' · ' + inr(b.proposed_cost) + ' deployable, ' + inr(b.cash_left) + ' idle' : '';
          return '<div class="rv" style="margin-bottom:26px">' +
            '<div style="display:flex;justify-content:space-between;align-items:baseline;' +
                 'flex-wrap:wrap;gap:8px;margin-bottom:8px">' +
              '<strong style="font-family:var(--mono);font-size:13px;letter-spacing:1px">' +
                esc(b.bucket) + '</strong>' +
              '<span class="mono-dim" style="font-size:11px">' +
                inr(b.monthly_amount) + ' · year ' + b.sip_year + ' · ' +
                b.held + '/' + b.names + ' held' + left + pl + xir + '</span>' +
            '</div>' +
            '<div class="tw"><table class="t" style="min-width:960px"><thead><tr>' +
              '<th scope="col">#</th><th scope="col">Symbol</th><th scope="col">Score</th><th scope="col">Ref px</th><th scope="col">Buy qty</th>' +
              '<th scope="col">Cost</th><th scope="col">Bought at</th>' +
              '<th scope="col">Last</th><th scope="col">P&amp;L</th><th scope="col">Status</th><th scope="col">Why</th>' +
            '</tr></thead><tbody>' + hs + '</tbody></table></div></div>';
        }).join('');
        reveal(sec);
      }).catch(function(){ /* no /api/sip on a static host — section stays hidden */ });
    }

    // Break-even win rate: stored on v2 rows, derived from R:R on older ones.
    // Red once it exceeds the ~37% this system actually wins, which is the
    // whole reason the gate exists.
    /* How far price sits from the entry, for setups that have not filled.
       A table of twenty open setups implies twenty things you could do today;
       most have already moved past their entry or are nowhere near it. Only
       shown on OPEN rows — on a closed trade the distance is history. */
    /* ── live price + running P&L on OPEN rows ──────────────────────────────
       An open row showed an entry and then a dash for Last, P&L and Exit, so
       the log said what was signalled and never what it had done since.

       Two sources, in order: /api/ticker's `ledger` map (fetched live, this
       page load) and then OPEN_CTX from today.json (the 6 AM snapshot). The
       fallback is labelled as such in the tooltip, because a stale price
       wearing a live label is worse than no price. */
    /* Read through window rather than captured into a local: the ticker
       fetch lives in a different IIFE and resolves on its own clock, before
       or after this table first paints. A shared object plus a redraw hook
       means neither has to know the other's timing. */
    function lastPx(a){
      var l = (window.__ledgerPx || {})[a.symbol];
      if (l && isFinite(l.price)) return { price: l.price, live: true };
      var c = OPEN_CTX[a.symbol];
      if (c && isFinite(c.price)) return { price: c.price, live: false };
      return null;
    }

    function lastCell(a){
      if (a.badge !== 'open') return '—';
      var p = lastPx(a);
      if (!p) return '—';
      return '<span title="' + (p.live ? 'Live quote' : '6:00 AM snapshot — no live quote for this symbol') + '"' +
             (p.live ? '' : ' class="mono-dim"') + '>' + money(p.price, a.currency) + '</span>';
    }

    /* Realised P&L for a closed row; UNREALISED for an open one, marked so the
       two can never be read as the same thing. The ledger's own pnl_pct is the
       realised number and stays authoritative wherever it exists. */
    function pnlCell(a){
      if (a.badge !== 'open' || a.pnl_pct !== null && a.pnl_pct !== undefined){
        return '<td class="' + (a.pnl_pct > 0 ? 'pnl-u' : a.pnl_pct < 0 ? 'pnl-d' : 'num') +
               '">' + esc(a.pnl_str) + '</td>';
      }
      var p = lastPx(a);
      if (!p || !a.entry) return '<td class="num">—</td>';
      var d = (p.price - a.entry) / a.entry * 100;
      // A short profits when price falls. Signing this off `action` rather
      // than assuming long is the difference between a hedge reading +4% and
      // -4% on the same move.
      if ((a.action || 'BUY').toUpperCase() === 'SELL') d = -d;
      return '<td class="' + (d > 0 ? 'pnl-u' : d < 0 ? 'pnl-d' : 'num') + '">' +
             '<span title="Unrealised — still open' + (p.live ? '' : ', priced off the 6 AM snapshot') + '">' +
             (d > 0 ? '+' : '') + fmt(d, 2) + '%' +
             '<span class="mono-dim" style="font-size:9px;display:block;line-height:1">open</span>' +
             '</span></td>';
    }

    function distCell(a){
      if (a.badge !== 'open') return '';
      var c = OPEN_CTX[a.symbol];
      if (!c || !c.price || !a.entry) return '';
      var d = (c.price - a.entry) / a.entry * 100;
      var isLong = (a.action || 'BUY').toUpperCase() !== 'SELL';
      // "Away" means price still has to come back TO the entry to fill.
      // Positive d on a long means price has run above the entry.
      var away = isLong ? d : -d;
      var cls = Math.abs(away) <= 1 ? 'up' : Math.abs(away) <= 4 ? '' : 'dn';
      return '<div class="dist ' + cls + '" title="Last ' + money(c.price, a.currency) +
             ' — ' + (away >= 0 ? 'above' : 'below') + ' entry">' +
             (away >= 0 ? '+' : '') + fmt(away, 1) + '%</div>';
    }

    function beWr(a){
      var v = (a.breakeven_wr === null || a.breakeven_wr === undefined)
            ? (a.rr > 0 ? 100 / (1 + a.rr) : null)
            : a.breakeven_wr;
      if (v === null) return '—';
      return '<span style="color:' + (v > 37 ? 'var(--down)' : 'var(--dim)') + '">' +
             fmt(v, 0) + '%</span>';
    }

    // A/B/UNVERIFIED from signals/quality.py. UNVERIFIED means the setup
    // cleared every price gate but Yahoo had no fundamentals for it — worth
    // showing as distinct from a clean pass rather than silently equal to one.
    function gradeCell(a){
      if (!a.grade) return '<span style="color:var(--dim)">—</span>';
      var col = a.grade === 'A' ? 'var(--lime)'
              : a.grade === 'B' ? 'var(--gold)' : 'var(--dim)';
      var txt = a.grade === 'UNVERIFIED' ? 'UNVER' : a.grade;
      return '<span style="color:' + col + ';font-weight:700">' + esc(txt) + '</span>';
    }

    /* ═══════ archive ═══════ */
    function loadArchive(){
      api('/archive?limit=1000').then(function(j){
        if (!j.ok) return;
        var strip = el('archStrip');
        strip.innerHTML = (j.days || []).map(function(d){
          var rTxt = d.total_r === null ? '' :
            '<div class="r ' + (d.total_r >= 0 ? 'up' : 'dn') + '">' +
            (d.total_r > 0 ? '+' : '') + fmt(d.total_r, 1) + 'R</div>';
          return '<div class="arch-day" data-date="' + d.date + '" title="' + d.date + ' · ' +
                 d.wins + 'W / ' + d.losses + 'L / ' + d.open + ' open">' +
                 '<div class="d">' + d.date.slice(5) + '</div>' +
                 '<div class="n">' + d.signals + '</div>' + rTxt + '</div>';
        }).join('');
        // Only ~16 chips fit on a desktop screen and ~5 on a phone, which made
        // a 57-day archive look like one week. Say the span out loud and offer
        // month jumps so the depth is discoverable without horizontal scrolling.
        var days = j.days || [];
        if (days.length){
          var span = el('archSpan');
          if (span) span.textContent = 'ARCHIVE — ' + days.length + ' trading days, ' +
            days[days.length - 1].date + ' → ' + days[0].date + ' · tap a day';
          var months = [];
          days.forEach(function(d){
            var m = d.date.slice(0, 7);
            if (months.indexOf(m) === -1) months.push(m);
          });
          var mEl = el('archMonths');
          if (mEl) {
            mEl.innerHTML = months.map(function(m){
              return '<button type="button" class="fbtn" data-m="' + m + '">' + m + '</button>';
            }).join('');
            mEl.querySelectorAll('button').forEach(function(b){
              b.addEventListener('click', function(){
                var first = strip.querySelector('.arch-day[data-date^="' + b.dataset.m + '"]');
                if (first) strip.scrollLeft = first.offsetLeft - 12;
              });
            });
          }
        }
        reveal(el('archWrap'));
        strip.querySelectorAll('.arch-day').forEach(function(n){
          n.addEventListener('click', function(){ selectDay(n.dataset.date); });
        });
      });
      el('archAll').addEventListener('click', function(){ selectDay(null); });
    }

    function selectDay(date){
      archDate = date;
      document.querySelectorAll('.arch-day').forEach(function(n){
        n.classList.toggle('on', !!date && n.dataset.date === date);
      });
      // "Show all days" shipped with class="fbtn on" hardcoded, so it was lit
      // permanently — including while a single archived day was filtering the
      // table down to nothing. The control said one thing and the query did
      // another. It now reflects the actual state.
      var all = el('archAll');
      if (all) all.classList.toggle('on', !date);
      if (date && history.replaceState) history.replaceState({}, '', '/day/' + date);
      else if (history.replaceState) history.replaceState({}, '', '/');
      loadSignals();
      document.getElementById('alerts').scrollIntoView({ behavior:'smooth', block:'start' });
    }

    /* ═══════ performance ═══════ */
    function wirePerfControls(){
      ['perfTf','perfRange'].forEach(function(id){
        el(id).addEventListener('change', loadStats);
      });
    }

    function loadStats(){
      var qs = [];
      var tf = el('perfTf').value;
      var days = el('perfRange').value;
      if (tf) qs.push('tf=' + encodeURIComponent(tf));
      if (days){
        var d = new Date(Date.now() - Number(days) * 86400000);
        qs.push('from=' + d.toISOString().slice(0, 10));
      }
      api('/stats' + (qs.length ? '?' + qs.join('&') : '')).then(function(j){
        if (!j.ok) return;
        paintHeroCurve(j);
        fillTfSelect(el('perfTf'), (j.by_timeframe || []).map(function(b){ return { timeframe: b.key }; }));
        renderStats(j);
      });
    }

    // Below this many closed trades, an expectancy figure is noise wearing a
    // decimal point. The page still shows the number — hiding it would be its
    // own kind of dishonesty — but says outright that it does not mean
    // anything yet. Starting the record fresh on the gated engine is exactly
    // the moment this matters: five trades at +0.14R is not an edge, and the
    // whole point of this ledger is not to claim one before it is earned.
    var MIN_N_FOR_EDGE = 30;

    /* ══════════ the underwater curve ══════════
       The equity curve alone answers "did it make money". It cannot answer the
       question that actually decides whether an edge is tradeable: how deep
       does it go against you, and for how long do you sit there?

       A +0.2R expectancy that spends four months underwater is abandoned by
       most people before it pays. Drawdown is the number that gets a system
       switched off, so it belongs on the page beside the one that sells it. */
    function paintUnderwater(j){
      var box = el('perfUw');
      if (!box) return;
      var pts = (j.equity_curve || []).filter(function(p){ return isFinite(p.cum_r); });
      if (pts.length < 5){ box.innerHTML = ''; return; }

      var peak = -Infinity, dd = [], worst = 0, worstAt = null;
      var underFrom = null, longest = 0, longestFrom = null, curRun = 0;
      for (var i = 0; i < pts.length; i++){
        var c = pts[i].cum_r;
        if (c > peak){ peak = c; }
        var d = c - peak;                       // <= 0, in R
        dd.push(d);
        if (d < worst){ worst = d; worstAt = pts[i].date; }
        // Time underwater, counted in TRADES rather than days: the ledger's
        // clock is trades, and a quiet week is not recovery.
        if (d < 0){
          if (underFrom === null) underFrom = pts[i].date;
          curRun++;
          if (curRun > longest){ longest = curRun; longestFrom = underFrom; }
        } else { underFrom = null; curRun = 0; }
      }

      var W = 600, H = 70;
      var lo = Math.min.apply(null, dd) || -1;
      var x = function(i){ return (i / (dd.length - 1)) * W; };
      var y = function(d){ return (d / lo) * H; };      // 0 at top, worst at bottom
      var path = dd.map(function(d, i){ return x(i).toFixed(1) + ',' + y(d).toFixed(1); }).join(' ');

      box.innerHTML =
        '<div class="uw-h"><span class="eyebrow">Underwater — every closed trade</span>' +
          '<span class="eyebrow">worst ' + fmt(worst, 2) + 'R</span></div>' +
        '<svg viewBox="0 0 ' + W + ' ' + (H + 2) + '" width="100%" role="img" ' +
          'aria-label="Drawdown from peak, worst ' + fmt(worst, 2) + 'R">' +
          '<polygon points="0,0 ' + path + ' ' + W + ',0" fill="var(--down)" opacity="0.16"></polygon>' +
          '<polyline points="' + path + '" fill="none" stroke="var(--down)" stroke-width="1.4"></polyline>' +
          '<line x1="0" y1="0" x2="' + W + '" y2="0" stroke="var(--line2)" stroke-width="1"></line>' +
        '</svg>' +
        '<div class="uw-f">' +
          '<span>Deepest <b>' + fmt(worst, 2) + 'R</b>' + (worstAt ? ' on ' + esc(worstAt) : '') + '</span>' +
          '<span>Longest stretch underwater <b>' + longest + ' trades</b>' +
            (longestFrom ? ' from ' + esc(longestFrom) : '') + '</span>' +
          '<span class="mono-dim">Zero means a new equity high.</span>' +
        '</div>';
    }

    function renderStats(j){
      var h = j.headline, t = j.totals;
      el('perfBasis').textContent = t.closed + ' closed of ' + t.all + ' signals · ' +
        (t.first_date || '—') + ' → ' + (t.last_date || '—');

      var warn = el('perfThin');
      if (warn){
        var thin = (t.closed || 0) < MIN_N_FOR_EDGE;
        warn.style.display = thin ? '' : 'none';
        if (thin){
          warn.textContent = 'Only ' + (t.closed || 0) + ' closed trade' +
            ((t.closed === 1) ? '' : 's') + '. These figures are not an edge ' +
            'measurement yet — at this sample size one trade moves them ' +
            'materially. Treat them as a running tally, not a result. ' +
            MIN_N_FOR_EDGE + '+ closed before any of it means something.';
        }
      }

      // The hero rail is baked at 6 AM. Left alone it would greet you with a
      // win rate from yesterday's snapshot, which is the exact staleness this
      // whole layer exists to kill.
      setKpi('heroRate',  h.win_rate === null ? '—' : h.win_rate + '%');
      setKpi('heroTotal', t.all);
      // The sample the hero number rests on, stated beside the hero number.
      // Without it the rail read "66.7% Signal Win Rate" in the accent colour
      // off THREE closed trades, while the caveat that says so lived nine
      // sections below the fold. A headline rate and its sample size are one
      // fact; splitting them across a scroll is how the page overstated itself.
      var hn = el('heroRateNote'), hr = el('heroRate');
      var nClosed = h.trades || 0, thinN = nClosed < MIN_N_FOR_EDGE;
      if (hn){
        hn.textContent = nClosed + ' closed' + (thinN ? ' · too few to measure' : '');
      }
      // Colour is a claim. Reserve the accent for a rate that has earned it.
      if (hr) hr.style.color = thinN ? 'var(--muted)' : 'var(--lime)';

      function cell(v, k, sub, colour){
        return '<div class="perf-cell"><div class="v"' + (colour ? ' style="color:' + colour + '"' : '') + '>' +
               v + '</div><div class="k">' + k + '</div>' +
               (sub ? '<div class="sub">' + sub + '</div>' : '') + '</div>';
      }
      var expColour = h.expectancy_r === null ? '' : (h.expectancy_r >= 0 ? 'var(--up)' : 'var(--down)');
      el('perfGrid').innerHTML =
        cell(h.win_rate === null ? '—' : h.win_rate + '%', 'Win rate', h.wins + 'W / ' + h.losses + 'L',
             thinN ? 'var(--muted)' : 'var(--lime)') +
        cell(h.expectancy_r === null ? '—' : (h.expectancy_r > 0 ? '+' : '') + fmt(h.expectancy_r, 3) + 'R',
             'Expectancy / trade', 'the number that decides everything', expColour) +
        cell(h.profit_factor === null ? '—' : fmt(h.profit_factor, 2), 'Profit factor', 'gross win ÷ gross loss') +
        cell(h.avg_win_r === null ? '—' : '+' + fmt(h.avg_win_r, 2) + 'R', 'Avg win', '', 'var(--up)') +
        cell(h.avg_loss_r === null ? '—' : fmt(h.avg_loss_r, 2) + 'R', 'Avg loss', '', 'var(--down)') +
        cell(h.max_drawdown_r === null ? '—' : fmt(h.max_drawdown_r, 2) + 'R', 'Max drawdown', 'peak to trough', 'var(--gold)') +
        cell(t.open, 'Open now', 'excluded from every rate above', 'var(--blue)') +
        cell(h.trades, 'Closed trades', 'the sample this rests on');


      function table(title, rows, labelWord){
        if (!rows || !rows.length) return '';
        return '<div class="brk-card"><h4>' + title + '</h4>' +
          rows.slice(0, 8).map(function(b){
            var col = b.total_r === null ? '' : (b.total_r >= 0 ? 'var(--up)' : 'var(--down)');
            return '<div class="brk-row"><span class="kk">' + esc(b.key) + '</span>' +
              '<span class="nn">' + b.trades + ' ' + labelWord + ' · ' + fmt(b.win_rate, 0) + '%</span>' +
              '<span class="rr" style="color:' + col + '">' +
              (b.total_r === null ? '—' : (b.total_r > 0 ? '+' : '') + fmt(b.total_r, 1) + 'R') + '</span></div>';
          }).join('') + '</div>';
      }
      // The per-engine R:R floor each engine must clear. Published because a
      // floor nobody can see is a claim, not a control — and these are per
      // engine on purpose: a single global floor is what let a 0.19R first
      // target through.
      // Each engine's MEASURED break-even R:R beside the floor actually
      // enforced. Published because a floor nobody can see is a claim rather
      // than a control, and because the two numbers together say the thing
      // that matters: an engine winning 23.9% of the time needs 3.18x just to
      // break even, so a setup at 2x is a loss with extra steps. Per engine on
      // purpose — one global floor is exactly what let a 0.19R target through.
      function floors(rows){
        if (!rows || !rows.length) return '';
        return '<div class="brk-card"><h4>Engine R:R floors</h4>' +
          rows.slice(0, 10).map(function(f){
            var thin = f.status && f.status !== 'active';
            var be = f.breakeven_rr === null || f.breakeven_rr === undefined
                   ? '—' : fmt(f.breakeven_rr, 2) + 'x';
            return '<div class="brk-row"><span class="kk">' + esc(f.key) +
              (thin ? ' <span class="mono-dim" style="font-size:10px">' + esc(f.status) + '</span>' : '') +
              '</span>' +
              '<span class="nn">' + f.trades + ' tr · wins ' + fmt(f.win_rate, 0) + '%</span>' +
              '<span class="rr" style="color:var(--gold)">needs ' + be +
                ' · floor ' + fmt(f.floor, 2) + 'x</span></div>';
          }).join('') + '</div>';
      }

      el('perfBrk').innerHTML =
        table('By engine', j.by_signal_type, 'tr') +
        table('By timeframe', j.by_timeframe, 'tr') +
        table('By month', j.by_month, 'tr') +
        table('Top symbols · 5+ closed trades', j.by_symbol, 'tr') +
        floors(j.engine_floors);

      paintUnderwater(j);
      reveal(el('perf'));
    }

    // Hand-rolled SVG path — no chart library, nothing to load, works offline.
  })();

  /* ══════════════ MIND GYM ══════════════
     Five drills, one featured per day. Everything is generated from a seed
     derived from the date, so the set is identical on every device all day and
     changes at midnight IST — no content to author, no endpoint to call, and
     it works unchanged on the static host. Scores live in localStorage. */
  (function(){
    var stage = document.getElementById('gymStage');
    if (!stage) return;
    var tabsEl = document.getElementById('gymTabs'),
        scoreEl = document.getElementById('gymScore'),
        LS = 'ds_gym_v1';

    /* ── deterministic randomness ──
       mulberry32 off a date-derived seed. Same day → same questions. */
    function seedFor(dayKey, salt){
      var h = 2166136261;
      var str = dayKey + '|' + salt;
      for (var i = 0; i < str.length; i++){
        h ^= str.charCodeAt(i); h = Math.imul(h, 16777619);
      }
      return h >>> 0;
    }
    function rng(seed){
      return function(){
        seed |= 0; seed = seed + 0x6D2B79F5 | 0;
        var t = Math.imul(seed ^ seed >>> 15, 1 | seed);
        t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
        return ((t ^ t >>> 14) >>> 0) / 4294967296;
      };
    }
    var R = {
      int: function(r, a, b){ return a + Math.floor(r() * (b - a + 1)); },
      pick: function(r, arr){ return arr[Math.floor(r() * arr.length)]; },
      shuffle: function(r, arr){
        var a = arr.slice();
        for (var i = a.length - 1; i > 0; i--){
          var j = Math.floor(r() * (i + 1)); var t = a[i]; a[i] = a[j]; a[j] = t;
        }
        return a;
      }
    };

    function istDayKey(){
      var n = new Date();
      var ist = new Date(n.getTime() + (n.getTimezoneOffset() + 330) * 60000);
      return ist.getFullYear() + '-' + ('0' + (ist.getMonth() + 1)).slice(-2) + '-' + ('0' + ist.getDate()).slice(-2);
    }
    var DAY = istDayKey();

    function store(){ try { return JSON.parse(localStorage.getItem(LS) || '{}') || {}; } catch(e){ return {}; } }
    function save(d){ try { localStorage.setItem(LS, JSON.stringify(d)); } catch(e){} }

    function esc(s){
      return String(s == null ? '' : s)
        .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }
    var fmt = function(v, d){
      return Number(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
    };

    /* ═══ the drills ═══
       Each returns { rounds: [...] }, and each round renders itself and calls
       done(correct) exactly once. */

    // 1 · MENTAL MATH — the arithmetic an FP&A review actually needs at speed.
    function mathRounds(r){
      var out = [];
      for (var i = 0; i < 8; i++){
        var kind = R.pick(r, ['pct', 'margin', 'growth', 'markup', 'share']);
        var q, ans, note;
        if (kind === 'pct'){
          var base = R.int(r, 20, 95) * 100, p = R.pick(r, [5, 8, 12, 15, 18, 25, 40]);
          q = p + '% of ' + fmt(base, 0); ans = base * p / 100;
          note = fmt(base,0) + ' × ' + p + '/100';
        } else if (kind === 'margin'){
          var rev = R.int(r, 40, 90) * 100, cost = Math.round(rev * (R.int(r, 45, 85) / 100));
          q = 'Revenue ' + fmt(rev,0) + ', cost ' + fmt(cost,0) + ' — gross margin %';
          ans = Math.round((rev - cost) / rev * 1000) / 10;
          note = '(' + fmt(rev,0) + ' − ' + fmt(cost,0) + ') ÷ ' + fmt(rev,0);
        } else if (kind === 'growth'){
          var a = R.int(r, 20, 60) * 10, g = R.pick(r, [10, 15, 20, 25, 30, 50]);
          q = fmt(a,0) + ' grows ' + g + '% — new value';
          ans = Math.round(a * (1 + g / 100) * 100) / 100;
          note = fmt(a,0) + ' × ' + (1 + g/100);
        } else if (kind === 'markup'){
          var c2 = R.int(r, 100, 900), m = R.pick(r, [20, 25, 30, 40, 50]);
          q = 'Cost ' + fmt(c2,0) + ', markup ' + m + '% — selling price';
          ans = Math.round(c2 * (1 + m / 100) * 100) / 100;
          note = fmt(c2,0) + ' × ' + (1 + m/100);
        } else {
          var whole = R.int(r, 30, 90) * 100, part = Math.round(whole * (R.int(r, 10, 60) / 100));
          q = fmt(part,0) + ' out of ' + fmt(whole,0) + ' — what %';
          ans = Math.round(part / whole * 1000) / 10;
          note = fmt(part,0) + ' ÷ ' + fmt(whole,0) + ' × 100';
        }
        out.push({ type: 'input', q: q, ans: ans, tol: 0.02, note: note, unit: kind === 'margin' || kind === 'share' ? '%' : '' });
      }
      return out;
    }

    // 2 · R:R AND SIZING — the two numbers that decide whether a trade is takeable.
    function deskRounds(r){
      var syms = ['RELIANCE','TCS','INFY','HDFCBANK','GOLD','CRUDE','NIFTY','SILVER'];
      var out = [];
      for (var i = 0; i < 7; i++){
        var sym = R.pick(r, syms), entry = R.int(r, 100, 3000);
        var riskPct = R.int(r, 2, 6), rewardMult = R.pick(r, [1.5, 2, 2.5, 3]);
        var sl = Math.round(entry * (1 - riskPct / 100) * 100) / 100;
        var risk = entry - sl;
        if (i % 2 === 0){
          var tgt = Math.round((entry + risk * rewardMult) * 100) / 100;
          out.push({ type: 'input',
            q: sym + ' — entry ' + fmt(entry,0) + ', stop ' + fmt(sl,2) + ', target ' + fmt(tgt,2) + '. R:R?',
            ans: rewardMult, tol: 0.06,
            note: 'Reward ' + fmt(tgt - entry,2) + ' ÷ risk ' + fmt(risk,2), unit: 'R' });
        } else {
          var cap = R.pick(r, [100000, 200000, 500000]), riskBudget = R.pick(r, [0.5, 1, 2]);
          var qty = Math.floor(cap * riskBudget / 100 / risk);
          out.push({ type: 'input',
            q: 'Capital ' + fmt(cap,0) + ', risking ' + riskBudget + '% per trade. ' + sym +
               ' entry ' + fmt(entry,0) + ', stop ' + fmt(sl,2) + '. Position size?',
            ans: qty, tol: 0.04,
            note: fmt(cap * riskBudget / 100, 0) + ' risk ÷ ' + fmt(risk,2) + ' per share', unit: 'sh' });
        }
      }
      return out;
    }

    // 3 · ESTIMATION — order of magnitude. Scored on being in the right band,
    // because that is the skill; the exact figure is not the point.
    var FERMI = [
      ['Seconds in a 30-day month', 2592000],
      ['Heartbeats in an average year', 36792000],
      ['Litres of water in an Olympic pool', 2500000],
      ['Words in a 300-page novel', 90000],
      ['Grains of rice in a 1 kg bag', 50000],
      ['People who fly commercially worldwide each day', 12000000],
      ['Weight of a fully loaded 747 in kg', 400000],
      ['Kilometres from Mumbai to Dubai', 1930],
      ['Petrol stations in India', 90000],
      ['Cups of coffee drunk worldwide per day', 2250000000],
      ['Steps in 10 kilometres of walking', 13000],
      ['Bricks in a typical 2-storey house', 30000]
    ];
    function fermiRounds(r){
      return R.shuffle(r, FERMI).slice(0, 6).map(function(f){
        return { type: 'input', q: f[0], ans: f[1], logTol: 0.35,
                 note: 'Order of magnitude is the win — within ~2× counts.', unit: '' };
      });
    }

    // 4 · NUMBER RECALL — working memory, lengthening each round.
    function recallRounds(r){
      var out = [];
      for (var len = 4; len <= 9; len++){
        var digits = '';
        for (var i = 0; i < len; i++) digits += R.int(r, 0, 9);
        out.push({ type: 'recall', digits: digits, q: 'Memorise ' + len + ' digits' });
      }
      return out;
    }

    // 5 · MARKET LOGIC — multiple choice on things that cost money to get wrong.
    var LOGIC = [
      ['A stock falls 50%. What gain gets you back to breakeven?', ['100%','50%','75%','200%'], 0,
       'Down 50% halves the base. Doubling the half is +100%. Losses need bigger wins than they look.'],
      ['Two funds: A returns +30%, −20%. B returns +5%, +5%. Which ends higher?', ['B','A','Same','Need more info'], 0,
       'A: 1.30 × 0.80 = 1.04. B: 1.05 × 1.05 = 1.1025. Volatility drags compounding.'],
      ['Win rate 30%, average win 3R, average loss 1R. Expectancy?', ['+0.2R','−0.1R','+0.9R','0R'], 0,
       '0.30 × 3 − 0.70 × 1 = +0.2R. A 30% win rate can print money.'],
      ['₹1L at 12% for 30 years vs ₹3L at 8% for 30 years. Which is larger?', ['₹1L at 12%','₹3L at 8%','Equal','Depends on tax'], 0,
       '₹1L × 1.12³⁰ ≈ ₹30L. ₹3L × 1.08³⁰ ≈ ₹30.2L — near dead heat. Rate rivals principal over long horizons.'],
      ['Which costs more over 20 years on a ₹50L corpus?', ['1.5% expense ratio','₹50,000 one-time fee','A 10% single-year drawdown','A 2-year late start'], 0,
       'A 1.5% drag compounds to roughly a quarter of the corpus. Recurring costs beat one-off shocks.'],
      ['Position risks 1% of capital. How many consecutive losses to halve the account?', ['~69','~50','~100','~35'], 0,
       'ln(0.5)/ln(0.99) ≈ 69. Small fixed risk is remarkably hard to kill.'],
      ['EBITDA rises, operating cash flow falls. Most likely cause?', ['Working capital blew out','Sales fell','Depreciation rose','Interest rose'], 0,
       'EBITDA ignores working capital. Receivables and inventory absorb the cash.'],
      ['A 1.5 R:R setup needs what win rate to break even?', ['40%','50%','33%','60%'], 0,
       '1/(1+1.5) = 40%. Below that the edge is negative no matter how good it feels.'],
      ['₹10,000/month SIP at 12% for 10 years ends near', ['₹23L','₹12L','₹18L','₹35L'], 0,
       'Roughly ₹23.2L on ₹12L invested. The second decade is where it gets interesting.'],
      ['Rupee falls from 83 to 95 per dollar. A US-earning exporter sees', ['Higher rupee revenue','Lower rupee revenue','No change','Lower margins'], 0,
       'Each dollar converts to more rupees. Importers take the opposite hit.']
    ];
    function logicRounds(r){
      return R.shuffle(r, LOGIC).slice(0, 6).map(function(l){
        var opts = l[1].map(function(t, i){ return { t: t, ok: i === l[2] }; });
        return { type: 'choice', q: l[0], opts: R.shuffle(r, opts), note: l[3] };
      });
    }

    var GAMES = [
      { id:'math',    name:'Mental Math',   icon:'🔢', build: mathRounds,   blurb:'Percentages, margins, growth. No calculator.' },
      { id:'desk',    name:'Desk Math',     icon:'🎯', build: deskRounds,   blurb:'R:R and position sizing — the two that decide the trade.' },
      { id:'logic',   name:'Market Logic',  icon:'🧠', build: logicRounds,  blurb:'Things that cost money to get wrong.' },
      { id:'fermi',   name:'Estimation',    icon:'📐', build: fermiRounds,  blurb:'Order of magnitude beats precision.' },
      { id:'recall',  name:'Number Recall', icon:'🧩', build: recallRounds, blurb:'Working memory, four digits up to nine.' }
    ];

    // Featured game rotates by day, so the default changes every morning.
    var dayIndex = Math.abs(seedFor(DAY, 'featured')) % GAMES.length;
    var current = GAMES[dayIndex], rounds = [], idx = 0, correct = 0, startedAt = 0;

    function tabs(){
      tabsEl.innerHTML = GAMES.map(function(g, i){
        return '<button class="gym-tab' + (g.id === current.id ? ' on' : '') + '" data-g="' + g.id + '">' +
               (i === dayIndex ? '<span class="tdot" title="Today\'s pick"></span>' : '') +
               g.icon + ' ' + esc(g.name) + '</button>';
      }).join('');
      Array.prototype.forEach.call(tabsEl.querySelectorAll('.gym-tab'), function(b){
        b.addEventListener('click', function(){
          var g = GAMES.filter(function(x){ return x.id === b.dataset.g; })[0];
          if (g) { current = g; start(); }
        });
      });
    }

    function start(){
      rounds = current.build(rng(seedFor(DAY, current.id)));
      idx = 0; correct = 0; startedAt = Date.now();
      tabs(); render();
    }

    function meta(){
      return '<div class="gym-meta"><span>' + esc(current.icon + ' ' + current.name.toUpperCase()) +
             ' · ' + esc(current.blurb) + '</span>' +
             '<span class="prog">' + Math.min(idx + 1, rounds.length) + ' / ' + rounds.length + '</span></div>';
    }

    function finish(){
      var secs = Math.round((Date.now() - startedAt) / 1000);
      var pct = Math.round(correct / rounds.length * 100);
      var d = store(); d[DAY] = d[DAY] || {};
      var prev = d[DAY][current.id];
      // Keep the day's best, not the latest — replaying should not punish you.
      if (!prev || correct > prev.correct) d[DAY][current.id] = { correct: correct, total: rounds.length, secs: secs };
      save(d);

      var verdict = pct === 100 ? 'Clean sweep.' : pct >= 70 ? 'Solid.' : pct >= 40 ? 'Middling.' : 'Rough one.';
      stage.innerHTML = meta() +
        '<div class="gym-q">' + correct + ' / ' + rounds.length + ' <span style="color:var(--muted);font-size:.55em">' + verdict + '</span></div>' +
        '<div class="gym-sub">' + secs + ' seconds · ' + pct + '% · ' +
          (prev && correct <= prev.correct ? 'today\'s best stands at ' + prev.correct : 'new best for today') + '</div>' +
        '<div class="gym-input"><button class="gym-btn" id="gymAgain">Run it again</button>' +
        '<button class="gym-btn ghost" id="gymNext">Next drill →</button></div>';
      document.getElementById('gymAgain').addEventListener('click', start);
      document.getElementById('gymNext').addEventListener('click', function(){
        current = GAMES[(GAMES.indexOf(current) + 1) % GAMES.length]; start();
      });
      paintScore();
    }

    // The result used to erase itself on a timer — 550ms when right, 1.9s when
    // wrong. Both are shorter than it takes to read a sentence, so the next
    // question replaced the explanation before you had seen it, and the drill
    // taught nothing. The reader advances it now; nothing moves on its own.
    function next(ok){
      if (ok) correct++;
      idx++;
      var last = idx >= rounds.length;
      var go = document.createElement('div');
      go.className = 'gym-input';
      go.style.marginTop = '14px';
      go.innerHTML = '<button class="gym-btn" id="gymNextQ">' +
        (last ? 'See the score →' : 'Next question →') +
        '</button><span class="mono-dim" style="font-size:11px;align-self:center">' +
        (last ? '' : 'or press Enter') + '</span>';
      stage.appendChild(go);

      var fired = false;
      var advance = function(){
        if (fired) return; fired = true;
        document.removeEventListener('keydown', onKey);
        last ? finish() : render();
      };
      var onKey = function(e){
        if (e.key === 'Enter' || e.key === ' '){ e.preventDefault(); advance(); }
      };
      document.getElementById('gymNextQ').addEventListener('click', advance);
      document.getElementById('gymNextQ').focus();
      // Bind on the next tick. Answering with Enter is still dispatching that
      // keydown when this runs, and a listener added mid-dispatch on an
      // ancestor still receives it — so binding synchronously would skip the
      // feedback with the very keystroke that produced it.
      setTimeout(function(){ document.addEventListener('keydown', onKey); }, 0);
    }

    function feedback(ok, msg){
      var fb = document.createElement('div');
      fb.className = 'gym-fb ' + (ok ? 'good' : 'bad');
      fb.innerHTML = msg;
      stage.appendChild(fb);
    }

    function render(){
      var q = rounds[idx];
      if (!q) return finish();

      if (q.type === 'choice'){
        stage.innerHTML = meta() + '<div class="gym-q">' + esc(q.q) + '</div>' +
          '<div class="gym-opts">' + q.opts.map(function(o, i){
            return '<button class="gym-opt" data-i="' + i + '">' + esc(o.t) + '</button>';
          }).join('') + '</div>';
        Array.prototype.forEach.call(stage.querySelectorAll('.gym-opt'), function(b){
          b.addEventListener('click', function(){
            var o = q.opts[+b.dataset.i], ok = o.ok;
            Array.prototype.forEach.call(stage.querySelectorAll('.gym-opt'), function(x, j){
              x.disabled = true;
              if (q.opts[j].ok) x.classList.add('right');
            });
            if (!ok) b.classList.add('wrong');
            feedback(ok, (ok ? '<b>Right.</b> ' : '<b>No.</b> ') + esc(q.note));
            next(ok);
          });
        });
        return;
      }

      if (q.type === 'recall'){
        stage.innerHTML = meta() + '<div class="gym-q">' + esc(q.q) + '</div>' +
          '<div class="gym-sub">It disappears in ' + (1.2 + q.digits.length * 0.35).toFixed(1) + ' seconds.</div>' +
          '<div class="gym-prompt" id="gymDigits">' + esc(q.digits) + '</div>';
        setTimeout(function(){
          stage.innerHTML = meta() + '<div class="gym-q">Type it back</div>' +
            '<div class="gym-input"><input id="gymIn" inputmode="numeric" autocomplete="off" placeholder="' +
            q.digits.length + ' digits">' +
            '<button class="gym-btn" id="gymGo">Check</button></div>';
          wireInput(function(val){
            var ok = val.replace(/\D/g, '') === q.digits;
            feedback(ok, ok ? '<b>Correct.</b> ' + esc(q.digits)
                            : '<b>It was ' + esc(q.digits) + '.</b> You typed ' + esc(val || '—') + '.');
            next(ok);
          });
        }, (1.2 + q.digits.length * 0.35) * 1000);
        return;
      }

      // numeric input
      stage.innerHTML = meta() + '<div class="gym-q">' + esc(q.q) + '</div>' +
        '<div class="gym-input"><input id="gymIn" inputmode="decimal" autocomplete="off" placeholder="Your answer' +
        (q.unit ? ' (' + esc(q.unit) + ')' : '') + '">' +
        '<button class="gym-btn" id="gymGo">Check</button></div>';
      wireInput(function(val){
        var got = parseFloat(String(val).replace(/[, ]/g, ''));
        var ok;
        if (!isFinite(got)) ok = false;
        else if (q.logTol){
          // Estimation is judged on log distance — within about 2× is a pass.
          ok = got > 0 && Math.abs(Math.log10(got) - Math.log10(q.ans)) <= q.logTol;
        } else {
          ok = Math.abs(got - q.ans) <= Math.max(Math.abs(q.ans) * q.tol, 0.01);
        }
        var shown = q.ans >= 1000 ? fmt(q.ans, 0) : fmt(q.ans, Math.abs(q.ans % 1) > 0 ? 2 : 0);
        feedback(ok, (ok ? '<b>Right.</b> ' : '<b>Answer: ' + shown + (q.unit ? ' ' + esc(q.unit) : '') + '.</b> ') +
                     (q.note ? esc(q.note) : ''));
        next(ok);
      });
    }

    function wireInput(check){
      var inp = document.getElementById('gymIn'), go = document.getElementById('gymGo');
      if (!inp || !go) return;
      inp.focus();
      var fired = false;
      var submit = function(){
        if (fired) return; fired = true;
        go.disabled = true; inp.disabled = true;
        check(inp.value.trim());
      };
      go.addEventListener('click', submit);
      inp.addEventListener('keydown', function(e){ if (e.key === 'Enter') submit(); });
    }

    function paintScore(){
      var d = store(), today = d[DAY] || {};
      var done = Object.keys(today).length;
      var got = 0, tot = 0;
      for (var k in today){ got += today[k].correct; tot += today[k].total; }

      // Streak: consecutive days ending today with at least one drill played.
      var streak = 0;
      for (var i = 0; i < 400; i++){
        var t = new Date(Date.now() - i * 86400000);
        var ist = new Date(t.getTime() + (t.getTimezoneOffset() + 330) * 60000);
        var key = ist.getFullYear() + '-' + ('0'+(ist.getMonth()+1)).slice(-2) + '-' + ('0'+ist.getDate()).slice(-2);
        if (d[key] && Object.keys(d[key]).length) streak++;
        else if (i > 0) break;
      }

      scoreEl.innerHTML =
        '<div><div class="v" style="color:var(--lime)">' + done + '/' + GAMES.length + '</div><div class="k">Drills today</div></div>' +
        '<div><div class="v">' + (tot ? Math.round(got / tot * 100) : 0) + '%</div><div class="k">Accuracy today</div></div>' +
        '<div><div class="v" style="color:var(--gold)">' + streak + '</div><div class="k">Day streak</div></div>' +
        '<div><div class="v" style="color:var(--blue)">' + Object.keys(d).length + '</div><div class="k">Days trained</div></div>';
    }

    start(); paintScore();
  })();

  /* ══════════════ LIVE CLOCK · MARKETS · NEWS ══════════════
     The daily shell freezes at 06:00 IST. These three keep the page honest
     without a rebuild: the clock ticks, prices refresh every 5 minutes, the
     wires every 3 hours. All of it degrades to the baked-in values if /api
     is not there (GitHub Pages), so nothing here can leave a blank section. */
  (function(){
    var API = '/api';

    /* ── IST clock: real time, not build time ── */
    var clock = document.getElementById('istClock');
    if (clock){
      var tick = function(){
        var n = new Date();
        // IST is UTC+5:30 with no DST, so a fixed offset is exact.
        var ist = new Date(n.getTime() + (n.getTimezoneOffset() + 330) * 60000);
        var hh = ('0' + ist.getHours()).slice(-2),
            mm = ('0' + ist.getMinutes()).slice(-2),
            ss = ('0' + ist.getSeconds()).slice(-2);
        clock.innerHTML = '<i></i>' + hh + ':' + mm + ':' + ss + ' IST';
      };
      tick(); setInterval(tick, 1000);
    }

    function get(path){
      return fetch(API + path, { cache: 'no-store' })
        .then(function(r){ return r.ok ? r.json() : null; })
        .catch(function(){ return null; });
    }
    function esc(s){
      return String(s == null ? '' : s)
        .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }

    /* ── ticker: every 5 minutes ──
       One rail, eleven segments, ordered by when each market opens in IST:
       Asia 05:30 → India 09:15 → Europe 12:30 → US 19:00, then the things that
       never close (commodities, FX, crypto) and finally the ledger's own
       multibagger ideas. Each segment gets a coloured head so 110 instruments
       read as a board rather than a stream. */
    var SEGCOLOR = {
      asia:'var(--blue)', india:'var(--lime)', gainers:'var(--up)', losers:'var(--down)',
      europe:'var(--violet)', us:'var(--blue)', ustop:'var(--blue)',
      commodities:'var(--gold)', fx:'var(--violet)', crypto:'var(--gold)',
      multibagger:'var(--lime)'
    };

    function paintTicker(j){
      if (!j || !j.ok || !j.segments || !j.segments.length) return;
      var rail = document.getElementById('tickRail');
      if (!rail) return;

      var items = 0, html = '';
      // Duplicated once: the marquee translates -50%, so the strip must hold
      // exactly two copies to loop without a visible seam.
      for (var d = 0; d < 2; d++){
        j.segments.forEach(function(s){
          var col = SEGCOLOR[s.key] || 'var(--lime)';
          html += '<div class="tseg" style="--sc:' + col + '">' +
                    '<span class="ic">' + esc(s.icon) + '</span>' +
                    '<span class="lb">' + esc(s.label) + '</span></div>';
          s.items.forEach(function(m){
            if (!d) items++;
            var chg = (m.change_pct > 0 ? '+' : '') + m.change_pct.toFixed(2) + '%';
            html += '<div class="ti" style="--sc:' + col + '">' +
                      '<span class="n">' + esc(m.name) + '</span>' +
                      '<span class="p">' + esc(m.price) + '</span>' +
                      '<span class="c ' + (m.up ? 'up' : 'dn') + '">' +
                        (m.up ? '▲' : '▼') + ' ' + chg + '</span>' +
                      (m.note ? '<span class="note">' + esc(m.note) + '</span>' : '') +
                    '</div>';
          });
        });
      }
      rail.innerHTML = html;

      // Speed is measured in pixels per second, not seconds per item.
      //
      // Both earlier attempts tuned "seconds per instrument" (1.6s, then
      // 1.15s), which only controls speed if every item is the same width.
      // They are not — a rendered .ti averages ~215px, so 1.15s/item was
      // really ~186 px/s: about three times reading speed, and no amount of
      // fiddling with that constant would have made it legible because the
      // constant was never the speed.
      //
      // Measure the strip and divide. scrollWidth holds two copies (the
      // marquee translates -50%), so half of it is one loop's distance. The
      // result is a genuinely constant scroll rate whatever the board size or
      // the screen width, tunable by exactly one honest number.
      var PX_PER_SEC = 80;              // ~2.7s for an instrument to cross a fixed point
      var loopPx = rail.scrollWidth / 2;
      if (loopPx > 0){
        rail.style.setProperty('--tickdur', Math.round(loopPx / PX_PER_SEC) + 's');
      } else {
        // Called before layout (display:none, or a hidden tab). Fall back to
        // the old count-based estimate rather than divide by zero.
        rail.style.setProperty('--tickdur', Math.max(45, Math.round(items * 2.7)) + 's');
      }

      // Same guard the ledger's setKpi() uses. Writing textContent alone is not
      // enough: the hero count-up animation writes this node every frame for
      // 1.1s and settles it back on the 6 AM snapshot figure — which is how it
      // ended up reading 0/1 next to a rail carrying 46 live instruments.
      var adv = document.querySelector('.statrail .stat .v[data-total]');
      if (adv){
        adv.dataset.live = '1';
        adv.removeAttribute('data-count');
        adv.textContent = j.advancing + '/' + j.total;
      }

      // Live quotes for open ledger rows travel on this same response — see
      // the note in api/ticker.js on why they are not their own endpoint.
      // Published before the redraw so the table reads the new map, not the
      // one it painted with.
      if (j.ledger){
        window.__ledgerPx = j.ledger;
        if (window.__onLedgerPx) window.__onLedgerPx();
      }
    }

    function loadTicker(){
      // /api/ticker is the current route. /markets is the old nine-instrument
      // one, kept as a fallback so an older deploy still shows a live rail.
      get('/ticker').then(function(j){
        if (j && j.ok) return paintTicker(j);
        return get('/markets').then(function(m){
          if (!m || !m.ok || !m.markets) return;
          paintTicker({ ok:true, advancing:m.advancing, total:m.total,
                        segments:[{ key:'india', label:'Markets', icon:'📈',
                                    items:m.markets.filter(function(x){ return x.price_raw !== null; }) }] });
        });
      });
    }
    loadTicker();
    setInterval(loadTicker, 5 * 60 * 1000);

    /* ── edition freshness ──
       The shell is rebuilt once a day and cached in the tab that loaded it.
       Nothing told a long-lived tab that a newer edition existed, so an
       overnight tab kept showing yesterday's masthead and yesterday's hero
       numbers while a fresh tab beside it showed today's — two views of the
       same URL, one minute apart, disagreeing. Poll the build id, say so, and
       reload silently only when the tab is not being looked at. */
    (function(){
      var bar = document.getElementById('editionbar');
      if (!bar) return;
      var mine = bar.dataset.build || '';
      if (!mine) return;                       // pre-stamp build, nothing to compare

      var notified = false;
      function check(){
        if (notified) return;
        fetch('/edition.json?t=' + Date.now(), { cache: 'no-store' })
          .then(function(r){ return r.ok ? r.json() : null; })
          .then(function(j){
            if (!j || !j.build_id || j.build_id === mine) return;
            notified = true;
            // Nothing is lost by reloading a tab nobody is looking at. A tab
            // in the foreground may have a drill in progress or a filter set,
            // so that one gets asked.
            //
            // Reload AT MOST ONCE per build id. Without that guard this is a
            // hot loop: if the CDN serves a shell whose stamp still differs
            // from edition.json — which happens for real during a deploy, and
            // happened here in testing — the reloaded page fails the same
            // check and reloads again, forever, on a tab nobody can see.
            var KEY = 'ds_edition_reloaded';
            var tried = null;
            try { tried = sessionStorage.getItem(KEY); } catch(e){}
            if (document.visibilityState === 'hidden' && tried !== j.build_id){
              try { sessionStorage.setItem(KEY, j.build_id); } catch(e){}
              location.reload();
              return;
            }
            // Either we are being watched, or we already reloaded for this
            // edition and the host is still serving the old shell. Ask.
            // Name the edition being offered. "A newer edition was published"
            // gave the reader nothing to weigh the reload against — a banner
            // that cannot say what it is offering reads as chrome.
            var when = document.getElementById('editionWhen');
            if (when && j.build_date) when.textContent = ' · ' + j.build_date;
            bar.classList.add('on');
          })
          .catch(function(){ /* offline or a host without edition.json */ });
      }
      document.getElementById('editionReload')
        .addEventListener('click', function(){ location.reload(); });
      // On focus as well as on a timer: coming back to the tab in the morning
      // is exactly when the answer has changed.
      document.addEventListener('visibilitychange', function(){
        if (document.visibilityState === 'visible') check();
      });
      check();
      setInterval(check, 10 * 60 * 1000);
    })();


    /* ── mobile: collapse the header chrome on scroll-down ──
       Keeps the ticker pinned while reading. The rail was hidden outright on
       phones for a while; that saved 30px and cost the one row people open
       this page for. */
    (function(){
      var stack = document.querySelector('.headstack');
      if (!stack) return;
      var last = window.scrollY, ticking = false;
      function apply(){
        ticking = false;
        if (!window.matchMedia('(max-width:560px)').matches){
          stack.classList.remove('compact'); last = window.scrollY; return;
        }
        var y = window.scrollY, dy = y - last;
        if (Math.abs(dy) < 6) return;          // ignore jitter and rubber-band
        if (y < 120) stack.classList.remove('compact');
        else if (dy > 0) stack.classList.add('compact');
        else stack.classList.remove('compact');
        last = y;
      }
      window.addEventListener('scroll', function(){
        if (!ticking){ ticking = true; requestAnimationFrame(apply); }
      }, {passive:true});
    })();

    // A marquee you cannot stop is a marquee you cannot read.
    (function(){
      var wrap = document.getElementById('tickWrap'), btn = document.getElementById('tickHold');
      if (!wrap || !btn) return;
      btn.addEventListener('click', function(){
        var held = wrap.classList.toggle('hold');
        btn.textContent = held ? 'Play' : 'Pause';
        btn.setAttribute('aria-pressed', held ? 'true' : 'false');
      });
    })();

    /* ── news: every 3 hours ── */
    function paintNews(j){
      if (!j || !j.ok || !j.news || j.news.length < 3) return;
      var sec = document.getElementById('world');
      if (!sec) return;
      var lead = j.news[0], rest = j.news.slice(1, 13);

      var ago = function(iso){
        if (!iso) return '';
        var mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
        if (mins < 1)  return 'just now';
        if (mins < 60) return mins + 'm ago';
        var h = Math.round(mins / 60);
        return h < 24 ? h + 'h ago' : Math.round(h / 24) + 'd ago';
      };
      // Reuses .ncard from the stylesheet rather than inventing classes, so
      // live cards are pixel-identical to the ones the daily build renders.
      var card = function(n, i){
        var link = n.link
          ? '<a href="' + esc(n.link) + '" target="_blank" rel="noopener">' + esc(n.title) + '</a>'
          : esc(n.title);
        return '<div class="ncard rv in" style="--d:' + (i * 0.04) + 's">' +
                 '<span class="s">' + esc(n.source) + '</span>' +
                 '<h3>' + link + '</h3>' +
                 (n.summary ? '<p>' + esc(n.summary.slice(0, 150)) + '</p>' : '') +
                 '<div class="ts">' + esc(ago(n.published)) + '</div>' +
               '</div>';
      };

      var host = document.getElementById('newsLive');
      if (!host){
        host = document.createElement('div');
        host.id = 'newsLive';
        // Replace whatever the daily build rendered — the lead block and the
        // grid both — with one live grid.
        var olds = sec.querySelectorAll('.news-grid, .two, .empty');
        if (olds.length){ olds[0].replaceWith(host); for (var k = 1; k < olds.length; k++) olds[k].remove(); }
        else sec.appendChild(host);
      }
      host.className = 'news-grid';
      host.innerHTML = [lead].concat(rest).map(card).join('');

      var d = sec.querySelector('.sdesc');
      if (d) d.textContent = 'Wires only, deduplicated. ' + j.count + ' stories from ' +
                             j.sources_ok + ' feeds, refreshed every 3 hours.';
    }


    /* ══════════════ WORLD MAP + 24h DESK ══════════════
       One fetch drives both: the map dots and the headline list underneath
       are the same 24-hour window, so they can never disagree. */
    var WM_W = 156, WM_H = 66, WM_LAT_TOP = 84, WM_LAT_BOT = -60;

    function wmDrawLand(){
      var host = document.getElementById('worldMap');
      var cv = document.getElementById('wmCanvas');
      if (!host || !cv || cv.dataset.drawn) return;
      var enc = host.dataset.mask || '';
      if (!enc) return;
      var ctx = cv.getContext('2d');
      if (!ctx) return;
      // 4 device px per grid cell, so the dots stay crisp when the map is
      // scaled up to full width.
      var S = cv.width / WM_W;
      ctx.fillStyle = '#31537d';
      ctx.globalAlpha = 0.55;
      // Run-length decode: alternating water/land counts in base16, water first.
      var runs = enc.split('.'), on = false, i = 0;
      for (var k = 0; k < runs.length; k++){
        var n = parseInt(runs[k], 16) || 0;
        if (on){
          for (var j = 0; j < n; j++){
            var idx = i + j, x = (idx % WM_W) * S, y = ((idx / WM_W) | 0) * S;
            ctx.fillRect(x, y, S * 0.85, S * 0.85);
          }
        }
        i += n; on = !on;
      }
      ctx.globalAlpha = 1;
      cv.dataset.drawn = '1';
    }

    function wmProject(lat, lon){
      return {
        x: (lon + 180) / 360 * WM_W,
        y: (WM_LAT_TOP - lat) / (WM_LAT_TOP - WM_LAT_BOT) * WM_H
      };
    }

    function paintWorld(j){
      if (!j || !j.ok) return;
      wmDrawLand();

      var dots = document.getElementById('wmDots');
      if (dots){
        var html = '';
        (j.countries || []).forEach(function(c, i){
          var p = wmProject(c.lat, c.lon);
          // Size carries volume, colour carries tone. A 40-story day in the US
          // must not swamp a 4-story war, so it is a log-ish scale.
          var r = Math.min(2.6, 0.8 + Math.log(1 + c.count) * 0.55);
          if (c.tone !== 'blue'){
            html += '<circle class="halo ' + c.tone + '" cx="' + p.x.toFixed(2) +
                    '" cy="' + p.y.toFixed(2) + '" r="1.5"/>';
          }
          html += '<circle class="ev ' + c.tone + '" style="--d:' + (i * 0.035).toFixed(2) +
                  's" cx="' + p.x.toFixed(2) + '" cy="' +
                  p.y.toFixed(2) + '" r="' + r.toFixed(2) + '"' +
                  ' data-c="' + esc(c.name) + '" data-n="' + c.count +
                  '" data-t="' + esc((c.top && c.top.title) || '') + '"' +
                  ' data-url="' + esc((c.top && c.top.link) || '') + '"' +
                  ' role="link" tabindex="0"' +
                  ' data-tone="' + c.tone + '"><title>' + esc(c.name) + ' · ' +
                  c.count + ' stories — open the story</title></circle>';
        });
        dots.innerHTML = html;
        wmWireTips();
      }

      var foot = document.getElementById('wmFoot');
      if (foot){
        var t = j.totals || {};
        foot.textContent = j.count + ' stories · ' + (j.countries || []).length +
          ' countries · ' + (t.red || 0) + ' escalating, ' + (t.green || 0) + ' improving · ' +
          j.sources_ok + '/' + j.sources_total + ' feeds · tagging is keyword-based and approximate';
      }

      // Headlines: same window, same fetch.
      var sec = document.getElementById('world');
      var host = document.getElementById('worldLive');
      if (!host && sec){
        host = document.createElement('div');
        host.id = 'worldLive';
        var olds = sec.querySelectorAll('.lead, .news-grid, .empty');
        if (olds.length){ olds[0].replaceWith(host); for (var m = 1; m < olds.length; m++) olds[m].remove(); }
        else sec.appendChild(host);
      }
      if (host){
        host.className = 'news-grid';
        host.innerHTML = (j.top || []).map(function(n, i){
          var link = n.link
            ? '<a href="' + esc(n.link) + '" target="_blank" rel="noopener">' + esc(n.title) + '</a>'
            : esc(n.title);
          var flag = n.tone === 'red' ? '<span class="tone red">▲ escalation</span>'
                   : n.tone === 'green' ? '<span class="tone green">▼ good news</span>' : '';
          return '<div class="ncard rv in" style="--d:' + (i * 0.04) + 's">' +
                   '<span class="s">' + esc(n.source) + '</span>' + flag +
                   '<h3>' + link + '</h3>' +
                   (n.summary ? '<p>' + esc(n.summary.slice(0, 150)) + '</p>' : '') +
                   '<div class="ts">' + esc(wmAgo(n.published)) +
                   (n.places && n.places.length ? ' · ' + esc(n.places.join(', ')) : '') +
                   '</div></div>';
        }).join('');
      }

      var d = document.getElementById('worldDesc');
      if (d) d.textContent = 'Wires only, deduplicated, last ' + j.window_hours +
        ' hours. ' + j.count + ' stories from ' + j.sources_ok + ' feeds, refreshed every 15 minutes.';
    }

    function wmAgo(iso){
      if (!iso) return '';
      var mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
      if (mins < 1) return 'just now';
      if (mins < 60) return mins + 'm ago';
      var h = Math.round(mins / 60);
      return h < 24 ? h + 'h ago' : Math.round(h / 24) + 'd ago';
    }

    function wmWireTips(){
      var tip = document.getElementById('wmTip'), wrap = document.getElementById('worldMap');
      if (!tip || !wrap) return;
      // A dot that shows a headline on hover and does nothing on click is a
      // dead end — the headline is the thing you want to read.
      function openStory(c){
        var u = c.getAttribute('data-url');
        if (u) window.open(u, '_blank', 'noopener');
      }
      wrap.querySelectorAll('circle.ev').forEach(function(c){
        c.addEventListener('click', function(){ openStory(c); });
        c.addEventListener('keydown', function(e){
          if (e.key === 'Enter' || e.key === ' '){ e.preventDefault(); openStory(c); }
        });
        c.addEventListener('mouseenter', function(ev){
          tip.innerHTML = '<div class="c">' + esc(c.dataset.c) + '</div>' +
                          '<div class="h">' + esc(c.dataset.t || '—') + '</div>' +
                          '<div class="m">' + c.dataset.n + ' stories · ' + c.dataset.tone +
                          (c.getAttribute('data-url') ? ' · click to read' : '') + '</div>';
          tip.hidden = false;
          var b = wrap.getBoundingClientRect(), r = c.getBoundingClientRect();
          tip.style.left = Math.max(6, Math.min(b.width - 300, r.left - b.left - 140)) + 'px';
          tip.style.top  = Math.max(6, r.top - b.top - tip.offsetHeight - 10) + 'px';
        });
        c.addEventListener('mouseleave', function(){ tip.hidden = true; });
      });
    }


    // Night band. Solar noon sits at the meridian where local time is 12:00,
    // so the dark half is centred on the antimeridian of that longitude. Not a
    // decoration — it is why Asia is quiet when New York is loud.
    function wmNight(){
      var el = document.getElementById('wmNight');
      if (!el) return;
      var utcH = new Date().getUTCHours() + new Date().getUTCMinutes() / 60;
      var noonLon = (12 - utcH) * 15;                 // degrees east of Greenwich
      var midnight = noonLon + 180;
      while (midnight > 180) midnight -= 360;
      while (midnight < -180) midnight += 360;
      var centre = (midnight + 180) / 360 * 100;      // % across the map
      var left = centre - 25, width = 50;

      // The night side wraps around the antimeridian. Drawn as one band it
      // simply clips at the edge of the map and the Pacific stays lit at 3am.
      // Anything past an edge is drawn again on the opposite side.
      var el2 = document.getElementById('wmNight2');
      el.style.left = Math.max(0, left) + '%';
      el.style.width = (left < 0 ? width + left : Math.min(width, 100 - left)) + '%';
      if (el2){
        if (left < 0){
          el2.style.display = ''; el2.style.left = (100 + left) + '%'; el2.style.width = (-left) + '%';
        } else if (left + width > 100){
          el2.style.display = ''; el2.style.left = '0%'; el2.style.width = (left + width - 100) + '%';
        } else {
          el2.style.display = 'none';
        }
      }
    }
    wmNight();
    setInterval(wmNight, 10 * 60 * 1000);

    function loadWorld(){
      get('/world?hours=24&limit=15').then(function(j){
        if (j && j.ok) return paintWorld(j);
        // No /api/world on this deploy — fall back to the older news route.
        loadNews();
      });
    }
    wmDrawLand();
    loadWorld();
    setInterval(loadWorld, 15 * 60 * 1000);

    // /api/world supersedes /api/news for this section: same headlines, but
    // scoped to a real 24h window and carrying the geo tags the map needs.
    // paintNews stays as the fallback for a deploy without /api/world.
    // The old /api/news painter, kept only as a fallback for a deploy that
    // predates /api/world. It used to be gated behind a probe request that
    // fetched a full world pipeline run purely to test for existence — 3 KB and
    // 280 ms thrown away on every page load. loadWorld()'s own failure is the
    // signal now, so the fallback costs nothing until it is needed.
    function loadNews(){ get('/news?limit=14').then(paintNews); }
  })();

  /* No whole-page reload. The clock ticks, markets and news refresh on their
     own timers, and the ledger sections pull on demand — reloading every five
     minutes would only throw away scroll position and any game in progress. */
})();
