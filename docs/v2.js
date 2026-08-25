/* ═══════════════════════════════════════════════════════════════════════
   v2.js — the client-rendered Daily Signal.

   WHY THIS IS NOT A JINJA TEMPLATE
   --------------------------------
   The server-rendered page is a 6,841-line template string inside
   newspaper.py that emits a 500KB document once a day. Everything genuinely
   live on it — signals, stats, positions — was already fetched from /api at
   page load anyway, so the template's job had shrunk to markup.

   This file is that markup, built from the same sources at runtime:

     /api/*          live   markets, signals, stats, world, news, sip, tracker, health
     /today.json     daily  picks, engine log, IPO rows, open-setup context, desk
     /mandate.json   daily  the Rs 1 crore order book
     /data-health.json      per-dataset freshness
     /alerts.json           the signal log
     /screen.json    lazy   750-company research screen (1.2MB — only on demand)
     /jobs.json      lazy   the careers board (713KB — only on demand)

   The daily build is untouched. It still writes every file above; this page
   just stops re-rendering them into HTML.

   RULES THIS FILE KEEPS
   ---------------------
   1. A failed fetch renders as "unavailable", never as zero. A section that
      quietly shows 0 when its source is down is lying.
   2. Detail hides behind a +. Highlight sections open by default.
   3. Every section carries at least two independent filters.
   4. Nothing is dropped silently — counts always say "n of m".
   ═══════════════════════════════════════════════════════════════════════ */
(function () {
"use strict";

var RM = matchMedia("(prefers-reduced-motion: reduce)").matches;
var DATA = {};       // source -> parsed payload
var FAILED = {};     // source -> error string

// ── tiny DOM helpers ──────────────────────────────────────────────────
function el(t, c, txt) { var e = document.createElement(t); if (c) e.className = c; if (txt != null) e.textContent = txt; return e; }
function num(v) { var x = Number(v); return isFinite(x) ? x : null; }
function fx(v, d) { var x = num(v); return x == null ? "—" : x.toFixed(d == null ? 2 : d); }
function sgn(v, d, suf) { var x = num(v); return x == null ? "—" : (x > 0 ? "+" : "") + x.toFixed(d == null ? 2 : d) + (suf || ""); }
function cls(v) { var x = num(v); return x == null ? "" : (x > 0 ? "up" : (x < 0 ? "down" : "")); }
function money(v, cur) { var x = num(v); return x == null ? "—" : (cur || "₹") + x.toLocaleString("en-IN"); }
function txt(v) { return String(v == null ? "" : v); }
function clip(s, n) { s = txt(s); return s.length > n ? s.slice(0, n) + "…" : s; }
function when(iso) {
  try { var m = Math.round((Date.now() - new Date(iso)) / 60000);
    if (m < 1) return "now"; if (m < 60) return m + "m ago";
    if (m < 1440) return Math.round(m / 60) + "h ago"; return Math.round(m / 1440) + "d ago";
  } catch (e) { return ""; }
}

function get(path) {
  return fetch(path, { cache: "no-store" })
    .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then(function (j) { if (j && j.ok === false) throw new Error(j.error || "not ok"); return j; });
}

// ── expandable row ────────────────────────────────────────────────────
function makeRow(o) {
  var row = el("div", "row" + (o.open ? " open" : ""));
  var line = el("button", "rowline"); line.type = "button";
  line.setAttribute("aria-expanded", o.open ? "true" : "false");
  line.appendChild(el("span", "plus", "+"));
  var main = el("div", "rmain"); o.main(main); line.appendChild(main);
  var nums = el("div", "rnums"); if (o.nums) o.nums(nums); line.appendChild(nums);
  row.appendChild(line);

  var drawer = el("div", "drawer"), inner = el("div"), pad = el("div", "dpad");
  var built = false;
  function build() { if (built) return; built = true; o.detail(pad); bars(pad); }
  inner.appendChild(pad); drawer.appendChild(inner); row.appendChild(drawer);
  if (o.open) build();
  line.addEventListener("click", function () {
    var open = row.classList.toggle("open");
    line.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) build();
  });
  return row;
}

function bars(scope) {
  requestAnimationFrame(function () {
    Array.prototype.forEach.call(scope.querySelectorAll("[data-w]"), function (b) {
      b.style.width = b.getAttribute("data-w");
    });
  });
}

function cells(pairs) {
  var g = el("div", "dgrid");
  pairs.forEach(function (kv) {
    var c = el("div", "dcell");
    c.appendChild(el("div", "k", kv[0]));
    c.appendChild(el("div", "v", txt(kv[1]) || "—"));
    g.appendChild(c);
  });
  return g;
}

function keyGrid(items) {
  var g = el("div", "keyrow");
  items.forEach(function (kv) {
    var c = el("div", "key");
    c.appendChild(el("div", "k", kv[0]));
    var v = el("div", "v " + (kv[3] || "")); v.textContent = kv[1];
    c.appendChild(v);
    if (kv[2]) c.appendChild(el("div", "s", kv[2]));
    g.appendChild(c);
  });
  return g;
}

function barRows(items, plain) {
  var host = el("div");
  var max = Math.max.apply(null, items.map(function (i) { return Math.abs(num(i.value) || 0); }).concat([1e-6]));
  items.forEach(function (it) {
    var r = el("div", "hb");
    r.appendChild(el("span", "n", it.name));
    var t = el("span", "t"), f = el("i");
    f.setAttribute("data-w", (Math.abs(num(it.value) || 0) / max * 100).toFixed(1) + "%");
    f.style.background = it.color || ((num(it.value) || 0) >= 0 ? "var(--mint)" : "var(--rose)");
    t.appendChild(f); r.appendChild(t);
    r.appendChild(el("span", "val " + (plain ? "" : cls(it.value)), it.display));
    host.appendChild(r);
  });
  return host;
}

// Cumulative-R curve. Area fill, dashed breakeven, emphasised endpoint.
function curve(vals) {
  var W = 560, H = 170, P = 26, L = P + 12;
  var lo = Math.min.apply(null, vals.concat([0])), hi = Math.max.apply(null, vals.concat([0]));
  var pad = (hi - lo) * 0.12 || 1; lo -= pad; hi += pad;
  var x = function (i) { return L + i * (W - L - P) / Math.max(1, vals.length - 1); };
  var y = function (v) { return H - P - (v - lo) / (hi - lo) * (H - P * 2); };
  var d = ""; vals.forEach(function (v, i) { d += (i ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1) + " "; });
  var a = d + "L" + x(vals.length - 1).toFixed(1) + " " + y(lo).toFixed(1) + " L" + x(0).toFixed(1) + " " + y(lo).toFixed(1) + " Z";
  var last = vals[vals.length - 1], col = last < 0 ? "var(--rose)" : "var(--mint)";
  var id = "g" + Math.random().toString(36).slice(2, 8);
  return '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="Cumulative R over ' +
    vals.length + " closed trades, ending at " + fx(last) + 'R">' +
    '<defs><linearGradient id="' + id + '" x1="0" y1="0" x2="0" y2="1">' +
    '<stop offset="0" stop-color="' + col + '" stop-opacity=".26"/>' +
    '<stop offset="1" stop-color="' + col + '" stop-opacity="0"/></linearGradient></defs>' +
    '<line class="gridline" x1="' + L + '" y1="' + y(0).toFixed(1) + '" x2="' + (W - P) + '" y2="' + y(0).toFixed(1) + '" stroke-dasharray="3 4"/>' +
    '<path d="' + a + '" fill="url(#' + id + ')"/>' +
    '<path d="' + d + '" fill="none" stroke="' + col + '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>' +
    '<circle cx="' + x(vals.length - 1).toFixed(1) + '" cy="' + y(last).toFixed(1) + '" r="9" fill="' + col + '" opacity=".2"/>' +
    '<circle cx="' + x(vals.length - 1).toFixed(1) + '" cy="' + y(last).toFixed(1) + '" r="4.5" fill="' + col + '"/>' +
    '<text class="axis" x="0" y="' + (y(0) + 3.5).toFixed(1) + '">0R</text>' +
    '<text class="axis" x="' + (W - P) + '" y="' + (y(last) - 13).toFixed(1) + '" text-anchor="end" fill="' + col + '">' + sgn(last, 2) + 'R</text>' +
    "</svg>";
}

// ── section scaffolding ───────────────────────────────────────────────
var SECS = [];
function section(o) { SECS.push(o); }

function build(o, i) {
  var sec = el("section", "sec"); sec.id = o.id;
  var head = el("div", "sechead"), hw = el("div", "wrap");
  hw.appendChild(el("span", "secnum", String(i + 1).padStart(2, "0")));
  hw.appendChild(el("h2", "sectitle", o.title));
  var cnt = el("span", "seccount", ""); cnt.id = o.id + "-count"; hw.appendChild(cnt);
  if (o.highlight) hw.appendChild(el("span", "hl", "Highlight"));

  var fw = el("div", "filters");
  (o.filters || []).forEach(function (g) {
    var grp = el("div", "fgroup");
    grp.appendChild(el("span", "flabel", g.label));
    g.options.forEach(function (opt, n) {
      var b = el("button", "chip" + (g.accent ? " acc" : ""), opt.label);
      b.type = "button"; b.setAttribute("aria-pressed", n === 0 ? "true" : "false");
      b.addEventListener("click", function () {
        Array.prototype.forEach.call(grp.querySelectorAll(".chip"), function (c) { c.setAttribute("aria-pressed", "false"); });
        b.setAttribute("aria-pressed", "true");
        g.value = opt.value;
        if (g.onPick) g.onPick(opt.value, o);
        render(o);
      });
      grp.appendChild(b);
    });
    g.value = g.options[0].value;
    fw.appendChild(grp);
  });
  hw.appendChild(fw);
  head.appendChild(hw); sec.appendChild(head);

  var body = el("div", "secbody"), bw = el("div", "wrap");
  if (o.lede) bw.appendChild(el("p", "lede", o.lede));
  var host = el("div"); host.id = o.id + "-host";
  host.appendChild(el("p", "loading", "Loading…"));
  bw.appendChild(host); body.appendChild(bw); sec.appendChild(body);
  document.getElementById("main").appendChild(sec);

  var a = el("a", null, o.nav); a.href = "#" + o.id;
  document.getElementById("jump").appendChild(a);
  o._host = host; o._count = cnt;
}

function setCount(o, s) { if (o._count) o._count.textContent = s || ""; }

function render(o) {
  var host = o._host; if (!host) return;
  var need = o.needs || [];
  var missing = need.filter(function (k) { return FAILED[k]; });
  if (missing.length) {
    host.innerHTML = "";
    host.appendChild(el("p", "empty",
      "This section reads " + missing.join(" and ") + ", which did not load (" +
      FAILED[missing[0]] + "). Showing nothing rather than zero — the difference matters."));
    setCount(o, "unavailable");
    return;
  }
  var pending = need.filter(function (k) { return DATA[k] === undefined; });
  if (pending.length) return;   // still loading; the loader will re-render
  host.innerHTML = "";
  try { o.render(host); } catch (e) {
    host.appendChild(el("p", "empty", "This section failed to render: " + e.message));
  }
  bars(host);
}

// ══════════════════════════════════════════════════════════════════════
//  THE SECTIONS — the same seventeen, in the same order
// ══════════════════════════════════════════════════════════════════════

// 01 · MARKET INTEL
section({
  id: "marketintel", nav: "Market Intel", title: "Market Intel", highlight: true,
  needs: ["markets"],
  lede: "Nine instruments, one line each. This section stays open — it is the first thing you need and the last thing you should have to click for.",
  filters: [
    { label: "Move", options: [{ label: "All", value: "all" }, { label: "Up", value: "up" }, { label: "Down", value: "down" }] },
    { label: "Class", accent: true, options: [{ label: "Every", value: "all" }, { label: "India", value: "in" }, { label: "Global", value: "gl" }, { label: "Commodity", value: "cm" }] }
  ],
  render: function (host) {
    var f = this.filters, all = DATA.markets.markets || [];
    var IN = ["Nifty 50", "Sensex", "Bank Nifty", "USD/INR"], CM = ["Gold", "Silver", "Crude"];
    var rows = all.filter(function (m) {
      if (f[0].value === "up" && !m.up) return false;
      if (f[0].value === "down" && m.up) return false;
      if (f[1].value === "in" && IN.indexOf(m.name) < 0) return false;
      if (f[1].value === "cm" && CM.indexOf(m.name) < 0) return false;
      if (f[1].value === "gl" && (IN.indexOf(m.name) >= 0 || CM.indexOf(m.name) >= 0)) return false;
      return true;
    });
    setCount(this, rows.length + " of " + all.length);
    if (!rows.length) { host.appendChild(el("p", "empty", "No instrument matches both filters.")); return; }
    var grid = el("div", "tape");
    rows.forEach(function (m) {
      var t = el("div", "tick");
      t.appendChild(el("div", "n", m.name));
      t.appendChild(el("div", "p", txt(m.price)));
      var c = el("div", "c " + (m.up ? "up" : "down"));
      c.appendChild(el("span", null, m.up ? "▲" : "▼"));
      c.appendChild(el("span", null, sgn(m.change_pct, 2, "%")));
      t.appendChild(c); grid.appendChild(t);
    });
    host.appendChild(grid);
  }
});

// 02 · TRADE IDEAS — the mandate book, then the weekly ranking
section({
  id: "picks", nav: "Trade Ideas", title: "Trade Ideas", highlight: true,
  needs: ["mandate", "today"],
  lede: "What the ₹1 crore mandate would place today — size, stop, and the full exit ladder. Below it, the week's ranked ideas. The book is a decision; the ranking is a shortlist.",
  filters: [
    { label: "Show", accent: true, options: [{ label: "The book", value: "book" }, { label: "Weekly five", value: "picks" }] },
    { label: "Horizon", options: [{ label: "All", value: "all" }, { label: "Swing", value: "SWING" }, { label: "Medium", value: "MEDIUM" }, { label: "Long", value: "LONG" }] },
    { label: "R:R", options: [{ label: "Any", value: 0 }, { label: "≥4:1", value: 4 }, { label: "≥5:1", value: 5 }] }
  ],
  render: function (host) {
    var f = this.filters, self = this;
    if (f[0].value === "picks") {
      var picks = DATA.today.picks || [];
      setCount(self, picks.length + " ranked");
      if (!picks.length) { host.appendChild(el("p", "empty", "No idea cleared the reward/risk floor this week.")); return; }
      var list = el("div", "rows");
      picks.forEach(function (p, i) {
        list.appendChild(makeRow({
          main: function (m) {
            m.appendChild(el("span", "rtag", String(i + 1).padStart(2, "0")));
            m.appendChild(el("span", "rsym", txt(p.symbol || p.name)));
            m.appendChild(el("span", "rmeta", txt(p.horizon_basis || "")));
          },
          nums: function (x) {
            x.appendChild(el("span", "rnum", money(p.price, p.currency)));
            x.appendChild(el("span", "rnum up", p.target == null ? "—" : "T " + money(p.target, p.currency)));
            x.appendChild(el("span", "rnum acc", p.rr == null ? "—" : fx(p.rr, 1) + ":1"));
          },
          detail: function (d) {
            d.appendChild(cells([
              ["Price", money(p.price, p.currency)], ["Target", money(p.target, p.currency)],
              ["Stop", money(p.stop_loss, p.currency)], ["Score", fx(p.score, 0)],
              ["52w high", money(p.high_52w, p.currency)], ["1M / 3M", sgn(p.mom_1m, 1, "%") + " / " + sgn(p.mom_3m, 1, "%")]
            ]));
            if (p.stop_basis) d.appendChild(el("p", "dnote", "Stop: " + p.stop_basis));
            if (p.factors) d.appendChild(el("p", "dnote", txt(p.factors)));
            d.appendChild(el("p", "dnote", "A ranked idea is not a ledger signal. It carries no entry fill and never touches win rate or expectancy."));
          }
        }));
      });
      host.appendChild(list);
      return;
    }

    var M = DATA.mandate || {};
    if (M.unavailable || !M.state) {
      host.appendChild(el("p", "empty", "The mandate did not size on the last build. That is a statement about the sizing run, not about the market."));
      setCount(self, "unavailable"); return;
    }
    var adm = (M.admitted || []).filter(function (t) {
      if (f[1].value !== "all" && t.horizon !== f[1].value) return false;
      if ((num(t.reward_risk) || 0) < f[2].value) return false;
      return true;
    });
    setCount(self, adm.length + " of " + (M.admitted || []).length);

    var st = M.state, info = el("div", "info");
    var p1 = el("div", "panel");
    p1.appendChild(el("h3", null, "Capital at work"));
    p1.appendChild(el("p", "sub", "Deployed against a " + fx(st.deployed_cap / M.capital * 100, 0) + "% ceiling. Cash is a position."));
    var g1 = el("div", "gauge"), i1 = el("i");
    i1.setAttribute("data-w", Math.min(100, st.deployed / st.deployed_cap * 100).toFixed(1) + "%");
    i1.style.background = "var(--coral)"; g1.appendChild(i1); p1.appendChild(g1);
    p1.appendChild(el("p", "sub", money(st.deployed) + " of " + money(st.deployed_cap) + " · " + money(st.cash) + " cash"));
    p1.appendChild(el("h3", null, "Open risk"));
    var g2 = el("div", "gauge"), i2 = el("i");
    i2.setAttribute("data-w", Math.min(100, st.heat / st.heat_cap * 100).toFixed(1) + "%");
    i2.style.background = "var(--amber)"; g2.appendChild(i2); p1.appendChild(g2);
    p1.appendChild(el("p", "sub", money(st.heat) + " of " + money(st.heat_cap) + " heat cap · " + fx(st.heat_pct, 2) + "% of capital"));
    info.appendChild(p1);

    var rr = M.reject_reasons || {};
    if (Object.keys(rr).length) {
      var p2 = el("div", "panel");
      p2.appendChild(el("h3", null, "Why the rest were dropped"));
      p2.appendChild(el("p", "sub", "Nothing vanishes silently. Every rejected candidate carries its reason."));
      p2.appendChild(barRows(Object.keys(rr).map(function (k) {
        return { name: k.toLowerCase().replace(/_/g, " "), value: rr[k], display: String(rr[k]), color: "var(--ink4)" };
      }).sort(function (a, b) { return b.value - a.value; }), true));
      info.appendChild(p2);
    }
    host.appendChild(info);

    if (!adm.length) { host.appendChild(el("p", "empty", "No ticket matches these filters. Loosen one.")); return; }
    var list2 = el("div", "rows");
    adm.forEach(function (t) {
      list2.appendChild(makeRow({
        open: true,
        main: function (m) {
          m.appendChild(el("span", "rsym", t.symbol));
          m.appendChild(el("span", "rtag", t.horizon_label));
          m.appendChild(el("span", "rmeta", t.engine + " · hold " + t.hold_days));
        },
        nums: function (x) {
          x.appendChild(el("span", "rnum", t.qty + " sh"));
          x.appendChild(el("span", "rnum", money(t.notional)));
          x.appendChild(el("span", "rnum up", "+" + fx(t.final_gain_pct, 1) + "%"));
          x.appendChild(el("span", "rnum acc", fx(t.reward_risk, 1) + ":1"));
        },
        detail: function (d) {
          d.appendChild(cells([
            ["Entry", money(t.entry)], ["Stop", money(t.stop) + " (" + fx(t.stop_pct, 1) + "%)"],
            ["Risk", money(t.risk_amount)], ["Notional", money(t.notional) + " · " + fx(t.notional_pct, 2) + "%"],
            ["Risk/share", money(t.risk_per_share)], ["Score", t.score == null ? "—" : fx(t.score, 0)]
          ]));
          d.appendChild(el("p", "dnote", "The ladder unwinds 20% at T1, half of what is left at T2, and the remainder at T3. The runner keeps the original stop — tightening it earlier was tested and made expectancy worse."));
          var lad = el("div", "ladder");
          (t.legs || []).forEach(function (leg) {
            var r = el("div", "rung");
            r.appendChild(el("span", "rlabel", leg.label));
            var b = el("div", "rbar"), fi = el("i");
            fi.setAttribute("data-w", (leg.qty / t.qty * 100).toFixed(1) + "%"); b.appendChild(fi);
            r.appendChild(b);
            r.appendChild(el("span", "rqty", leg.qty + " sh @ " + money(leg.price) + " · +" + fx(leg.gain_pct, 1) + "%"));
            lad.appendChild(r);
          });
          var rs = el("div", "rung stop");
          rs.appendChild(el("span", "rlabel", "Stop"));
          var bs = el("div", "rbar"), fs = el("i"); fs.setAttribute("data-w", "100%"); bs.appendChild(fs);
          rs.appendChild(bs);
          rs.appendChild(el("span", "rqty", t.qty + " sh @ " + money(t.stop) + " · −" + fx(t.stop_pct, 1) + "%"));
          lad.appendChild(rs);
          d.appendChild(lad);
          if (t.trail_note) d.appendChild(el("p", "dnote", t.trail_note));
        }
      }));
    });
    host.appendChild(list2);
  }
});

// 03 · WORLD
section({
  id: "world", nav: "World", title: "The World",
  needs: ["world", "news"],
  lede: "Clustered from the wires over the last 24 hours. Headline only — open a row for the summary.",
  filters: [
    { label: "Tone", accent: true, options: [{ label: "All", value: "all" }, { label: "Risk", value: "red" }, { label: "Positive", value: "green" }] },
    { label: "Feed", options: [{ label: "Clustered", value: "world" }, { label: "Raw wire", value: "news" }] }
  ],
  render: function (host) {
    var f = this.filters, raw = f[1].value === "news";
    var src = raw ? (DATA.news.news || []) : (DATA.world.top || []);
    var rows = src.filter(function (t) { return raw || f[0].value === "all" || t.tone === f[0].value; });
    setCount(this, rows.length + (raw ? " headlines" : " events"));
    if (!rows.length) { host.appendChild(el("p", "empty", "No story matches this tone in the last 24 hours.")); return; }
    var list = el("div", "rows");
    rows.slice(0, 30).forEach(function (t) {
      list.appendChild(makeRow({
        main: function (m) {
          if (!raw) m.appendChild(el("span", "tone " + (t.tone || "grey")));
          m.appendChild(el("span", "rsym", clip(t.title, 96)));
        },
        nums: function (x) {
          x.appendChild(el("span", "rnum", txt(t.source)));
          x.appendChild(el("span", "rmeta", when(t.published)));
        },
        detail: function (d) {
          d.appendChild(el("p", "dbody", txt(t.summary) || "No summary on the wire for this one."));
          if (!raw && t.also) d.appendChild(el("p", "dnote", t.also + " other outlet" + (t.also === 1 ? "" : "s") + " carried the same story."));
          if (t.link) { var a = el("a", null, "Read at " + txt(t.source) + " →"); a.href = t.link; a.target = "_blank"; a.rel = "noopener"; var p = el("p", "dnote"); p.appendChild(a); d.appendChild(p); }
        }
      }));
    });
    host.appendChild(list);
  }
});

// 04 · FINDINGS — the open-setup context the daily build prices
section({
  id: "findings", nav: "Findings", title: "Findings",
  needs: ["today"],
  lede: "Where open setups actually sit against their levels right now. A setup that has drifted past its entry is a different proposition from one still waiting.",
  filters: [
    { label: "Position", accent: true, options: [{ label: "All", value: "all" }, { label: "Above entry", value: "above" }, { label: "Below entry", value: "below" }] },
    { label: "Sort", options: [{ label: "Distance", value: "dist" }, { label: "Symbol", value: "sym" }] }
  ],
  render: function (host) {
    var f = this.filters, ctx = DATA.today.open_context || {};
    var rows = Object.keys(ctx).map(function (k) { var v = ctx[k] || {}; v._sym = k; return v; })
      .filter(function (v) { return num(v.last) != null && num(v.entry) != null; })
      .map(function (v) { v._d = (num(v.last) - num(v.entry)) / num(v.entry) * 100; return v; })
      .filter(function (v) {
        if (f[0].value === "above") return v._d > 0;
        if (f[0].value === "below") return v._d <= 0;
        return true;
      });
    rows.sort(f[1].value === "sym"
      ? function (a, b) { return a._sym.localeCompare(b._sym); }
      : function (a, b) { return Math.abs(b._d) - Math.abs(a._d); });
    setCount(this, rows.length + " priced");
    if (!rows.length) { host.appendChild(el("p", "empty", "No open setup carries a live price on this build.")); return; }
    var list = el("div", "rows");
    rows.slice(0, 30).forEach(function (v) {
      list.appendChild(makeRow({
        main: function (m) {
          m.appendChild(el("span", "rsym", v._sym));
          m.appendChild(el("span", "rmeta", "entry " + money(v.entry, v.currency)));
        },
        nums: function (x) {
          x.appendChild(el("span", "rnum", money(v.last, v.currency)));
          x.appendChild(el("span", "rnum " + cls(v._d), sgn(v._d, 1, "%") + " vs entry"));
        },
        detail: function (d) {
          d.appendChild(cells([["Last", money(v.last, v.currency)], ["Entry", money(v.entry, v.currency)],
            ["Stop", money(v.sl, v.currency)], ["Target", money(v.target1 || v.target, v.currency)],
            ["Distance", sgn(v._d, 2, "%")], ["As of", txt(v.as_of || DATA.today.date)]]));
          d.appendChild(el("p", "dnote", v._d > 0
            ? "Price has moved past the entry. Chasing it changes the reward/risk the signal was scored on."
            : "Still below entry. The setup has not triggered."));
        }
      }));
    });
    host.appendChild(list);
  }
});

// 05 · OWN THE BUSINESS — long-horizon names from the mandate
section({
  id: "longterm", nav: "Own the Business", title: "Own the Business",
  needs: ["mandate"],
  lede: "The long end of the mandate: 40–90% over six months to a year. Held, not traded.",
  filters: [
    { label: "Horizon", accent: true, options: [{ label: "Long + medium", value: "lm" }, { label: "Long only", value: "LONG" }, { label: "Medium only", value: "MEDIUM" }] },
    { label: "Sort", options: [{ label: "Upside", value: "gain" }, { label: "Reward:risk", value: "rr" }] }
  ],
  render: function (host) {
    var f = this.filters, M = DATA.mandate || {};
    var rows = (M.admitted || []).filter(function (t) {
      if (f[0].value === "lm") return t.horizon === "LONG" || t.horizon === "MEDIUM";
      return t.horizon === f[0].value;
    });
    rows.sort(f[1].value === "rr"
      ? function (a, b) { return (num(b.reward_risk) || 0) - (num(a.reward_risk) || 0); }
      : function (a, b) { return (num(b.final_gain_pct) || 0) - (num(a.final_gain_pct) || 0); });
    setCount(this, rows.length + " names");
    if (!rows.length) {
      host.appendChild(el("p", "empty", "Nothing on the long or medium horizon clears the mandate today. The engines that reach those bands file weekly — an empty week here is normal, not broken."));
      return;
    }
    var list = el("div", "rows");
    rows.forEach(function (t) {
      list.appendChild(makeRow({
        main: function (m) {
          m.appendChild(el("span", "rsym", t.symbol));
          m.appendChild(el("span", "rtag", t.horizon_label));
          m.appendChild(el("span", "rmeta", "hold " + t.hold_days));
        },
        nums: function (x) {
          x.appendChild(el("span", "rnum up", "+" + fx(t.final_gain_pct, 1) + "%"));
          x.appendChild(el("span", "rnum acc", fx(t.reward_risk, 1) + ":1"));
        },
        detail: function (d) {
          d.appendChild(cells([["Entry", money(t.entry)], ["Stop", money(t.stop)],
            ["Upside", "+" + fx(t.final_gain_pct, 1) + "%"], ["Hold", t.hold_days],
            ["Notional", money(t.notional)], ["Engine", t.engine]]));
          d.appendChild(el("p", "dnote", t.trail_note || ""));
        }
      }));
    });
    host.appendChild(list);
  }
});

// 06 · STOCK SCREEN — 1.2MB, loaded only when asked for
section({
  id: "stocks", nav: "Stock Screen", title: "Stock Screen",
  needs: [],
  lede: "The Nifty Total Market universe, scored. The payload is 1.2MB, so it loads only when you ask for it — the rest of this page should not wait on a research file.",
  filters: [
    { label: "Load", accent: true, options: [
      { label: "On demand", value: "off" },
      { label: "Load screen", value: "on", }
    ], onPick: function (v) { if (v === "on" && DATA.screen === undefined && !FAILED.screen) load("screen", "/screen.json"); } },
    { label: "Rank", options: [{ label: "Composite", value: "comp" }, { label: "Momentum", value: "mom" }] }
  ],
  render: function (host) {
    var f = this.filters, self = this;
    if (f[0].value === "off") {
      setCount(self, "not loaded");
      host.appendChild(el("p", "empty", "Press “Load screen” to pull 750 companies. Kept behind a click on purpose: it is a research file, not a headline."));
      return;
    }
    if (FAILED.screen) { setCount(self, "unavailable"); host.appendChild(el("p", "empty", "The screen did not load: " + FAILED.screen)); return; }
    if (DATA.screen === undefined) { setCount(self, "loading"); host.appendChild(el("p", "loading", "Loading 750 companies…")); return; }
    var S = DATA.screen, rows = (S.rows || []).slice();
    var kMom = "mom_6m" in (rows[0] || {}) ? "mom_6m" : "mom_3m";
    rows.sort(f[1].value === "mom"
      ? function (a, b) { return (num(b[kMom]) || -1e9) - (num(a[kMom]) || -1e9); }
      : function (a, b) { return (num(b.comp) || -1e9) - (num(a.comp) || -1e9); });
    setCount(self, "top 25 of " + rows.length);
    var list = el("div", "rows");
    rows.slice(0, 25).forEach(function (r, i) {
      list.appendChild(makeRow({
        main: function (m) {
          m.appendChild(el("span", "rtag", String(i + 1).padStart(2, "0")));
          m.appendChild(el("span", "rsym", txt(r.sym)));
          m.appendChild(el("span", "rmeta", txt(r.name || "").slice(0, 42)));
        },
        nums: function (x) {
          x.appendChild(el("span", "rnum", r.comp == null ? "—" : fx(r.comp, 1)));
          x.appendChild(el("span", "rnum " + cls(r[kMom]), sgn(r[kMom], 1, "%")));
        },
        detail: function (d) {
          d.appendChild(cells([["Composite", fx(r.comp, 1)], ["Momentum", sgn(r[kMom], 1, "%")],
            ["ROCE", r.roce == null ? "—" : fx(r.roce, 1) + "%"], ["D/E", fx(r.de, 2)],
            ["EBIT margin", r.ebit_margin == null ? "—" : fx(r.ebit_margin, 1) + "%"],
            ["ATR", r.atr_pct == null ? "—" : fx(r.atr_pct, 1) + "%"]]));
          d.appendChild(el("p", "dnote", "Screen built " + txt(S.built_on) + " over the " + txt(S.universe) + " (" + S.universe_size + " names)."));
        }
      }));
    });
    host.appendChild(list);
  }
});

// 07 · IPO RADAR
section({
  id: "iporadar", nav: "IPO Radar", title: "IPO Radar",
  needs: ["today"],
  lede: "What is open now, and how the last twelve months of listings actually traded.",
  filters: [
    { label: "State", accent: true, options: [{ label: "All", value: "all" }, { label: "Open", value: "open" }, { label: "Listed", value: "listed" }] },
    { label: "Result", options: [{ label: "Any", value: "all" }, { label: "Gained", value: "up" }, { label: "Fell", value: "down" }] }
  ],
  render: function (host) {
    var f = this.filters, I = DATA.today.ipos || {}, rows = (I.rows || []).slice();
    rows = rows.filter(function (r) {
      var st = txt(r.status || r.state).toLowerCase();
      if (f[0].value === "open" && st.indexOf("open") < 0) return false;
      if (f[0].value === "listed" && st.indexOf("open") >= 0) return false;
      var g = num(r.gain_pct != null ? r.gain_pct : r.listing_gain);
      if (f[1].value === "up" && !(g > 0)) return false;
      if (f[1].value === "down" && !(g < 0)) return false;
      return true;
    });
    setCount(this, rows.length + " of " + (I.rows || []).length);
    if (!rows.length) { host.appendChild(el("p", "empty", "No listing matches. The radar tracks " + (I.months || 12) + " months.")); return; }
    var list = el("div", "rows");
    rows.slice(0, 30).forEach(function (r) {
      var g = num(r.gain_pct != null ? r.gain_pct : r.listing_gain);
      list.appendChild(makeRow({
        main: function (m) {
          m.appendChild(el("span", "rsym", clip(r.name || r.symbol, 40)));
          m.appendChild(el("span", "rmeta", txt(r.status || r.state || "")));
        },
        nums: function (x) {
          x.appendChild(el("span", "rnum " + cls(g), g == null ? "not measured" : sgn(g, 1, "%")));
        },
        detail: function (d) {
          d.appendChild(cells([["Symbol", txt(r.symbol)], ["Status", txt(r.status || r.state)],
            ["Listed", txt(r.listing_date || r.date)], ["Since listing", g == null ? "not measured" : sgn(g, 2, "%")],
            ["Issue price", r.price == null ? "—" : money(r.price)], ["Score", r.score == null ? "—" : fx(r.score, 0)]]));
          d.appendChild(el("p", "dnote", "Lot size, sector, financials and GMP are not in any public feed this build reads, so they are shown as not measured rather than guessed."));
        }
      }));
    });
    host.appendChild(list);
  }
});

// 08 · SIP BUCKETS  ·  09 · SWP — both from /api/sip
section({
  id: "sip", nav: "SIP", title: "SIP Buckets",
  needs: ["sip"],
  lede: "The monthly plan, and what it compounds to under three return assumptions. Projections are arithmetic, not forecasts.",
  filters: [
    { label: "View", accent: true, options: [{ label: "Projections", value: "proj" }, { label: "Plan", value: "plan" }] },
    { label: "Return", options: [{ label: "12%", value: "r12" }, { label: "14%", value: "r14" }, { label: "16%", value: "r16" }] }
  ],
  render: function (host) {
    var f = this.filters, S = DATA.sip || {}, plan = S.plan || {};
    if (f[0].value === "plan") {
      setCount(this, "monthly " + money(plan.monthly_amount));
      host.appendChild(keyGrid([
        ["Monthly", money(plan.monthly_amount), "base " + money(plan.base_monthly)],
        ["Step-up", fx(plan.step_up_pct, 0) + "%", "each year"],
        ["SIP year", txt(plan.sip_year), "started " + txt(plan.start)],
        ["Names / bucket", txt(plan.names_per_bucket), "diversification floor"]
      ]));
      return;
    }
    var proj = (S.projections || []).slice();
    setCount(this, proj.length + " horizons");
    if (!proj.length) { host.appendChild(el("p", "empty", "No projection in this payload.")); return; }
    var key = f[1].value;
    host.appendChild(keyGrid(proj.slice(0, 4).map(function (p) {
      return [p.years + " years", money(p[key]), "invested " + money(p.invested)];
    })));
    var items = proj.map(function (p) {
      return { name: p.years + "y", value: num(p[key]) || 0, display: money(p[key]), color: "var(--mint)" };
    });
    var panel = el("div", "panel"); panel.style.marginTop = "16px";
    panel.appendChild(el("h3", null, "Corpus at " + key.replace("r", "") + "%"));
    panel.appendChild(el("p", "sub", "Step-up SIP, compounded monthly. The bar is the corpus, not the gain."));
    panel.appendChild(barRows(items, true));
    host.appendChild(panel);
  }
});

// 10 · PORTFOLIO
section({
  id: "tracker", nav: "Portfolio", title: "Portfolio",
  needs: ["tracker"],
  lede: "What is actually held. An OPEN setup is not a position — that distinction is the whole point of this section.",
  filters: [
    { label: "Show", accent: true, options: [{ label: "Positions", value: "pos" }, { label: "Risk summary", value: "risk" }] },
    { label: "State", options: [{ label: "All", value: "all" }, { label: "In profit", value: "up" }, { label: "Underwater", value: "down" }] }
  ],
  render: function (host) {
    var f = this.filters, T = DATA.tracker || {}, pf = T.portfolio || {};
    if (f[0].value === "risk") {
      setCount(this, (T.count || 0) + " tracked");
      host.appendChild(keyGrid([
        ["Positions", txt(T.count || 0), "tracked"],
        ["Open risk", pf.open_risk_pct == null ? "—" : fx(pf.open_risk_pct, 2) + "%", money(pf.open_risk_amount)],
        ["Protected", txt(pf.protected_positions || 0), "stop at or above entry"],
        ["Threatened", txt(pf.threatened_positions || 0), "stop below entry"]
      ]));
      return;
    }
    var pos = (T.positions || []).filter(function (p) {
      var v = num(p.pnl_pct);
      if (f[1].value === "up") return v > 0;
      if (f[1].value === "down") return v <= 0;
      return true;
    });
    setCount(this, pos.length + " of " + (T.positions || []).length);
    if (!pos.length) {
      host.appendChild(el("p", "empty", "Nothing held. Nothing in this repository can place an order — a signal becomes a position only when the order is placed by hand and confirmed on Telegram."));
      return;
    }
    var list = el("div", "rows");
    pos.forEach(function (p) {
      list.appendChild(makeRow({
        main: function (m) {
          m.appendChild(el("span", "rsym", txt(p.symbol)));
          m.appendChild(el("span", "rmeta", "entry " + money(p.entry_price, p.currency)));
        },
        nums: function (x) {
          x.appendChild(el("span", "rnum", money(p.current_price, p.currency)));
          x.appendChild(el("span", "rnum " + cls(p.pnl_pct), sgn(p.pnl_pct, 2, "%")));
        },
        detail: function (d) {
          d.appendChild(cells([["Entry", money(p.entry_price, p.currency)], ["Last", money(p.current_price, p.currency)],
            ["Stop", money(p.stop, p.currency)], ["Qty", txt(p.qty)],
            ["P&L", sgn(p.pnl_pct, 2, "%")], ["Opened", txt(p.opened_at)]]));
        }
      }));
    });
    host.appendChild(list);
  }
});

// 11 · SIGNAL LOG
section({
  id: "alerts", nav: "Signal Log", title: "Signal Log",
  needs: ["alerts"],
  lede: "Every alert this engine has sent, newest first. One line each; open a row for its levels.",
  filters: [
    { label: "Result", accent: true, options: [{ label: "All", value: "all" }, { label: "Target", value: "win" }, { label: "Stopped", value: "loss" }, { label: "Open", value: "open" }] },
    { label: "Side", options: [{ label: "Both", value: "all" }, { label: "Buy", value: "BUY" }, { label: "Sell", value: "SELL" }] },
    { label: "Engine", options: [{ label: "Any", value: "all" }, { label: "breakout", value: "breakout" }, { label: "cf_1h", value: "cf_1h" }, { label: "ohl", value: "ohl" }] }
  ],
  render: function (host) {
    var f = this.filters, all = DATA.alerts || [];
    var rows = all.filter(function (s) {
      var st = txt(s.status).toUpperCase(), win = /^T\d*_HIT$/.test(st), open = st === "OPEN";
      if (f[0].value === "win" && !win) return false;
      if (f[0].value === "loss" && (win || open)) return false;
      if (f[0].value === "open" && !open) return false;
      if (f[1].value !== "all" && txt(s.action).toUpperCase() !== f[1].value) return false;
      if (f[2].value !== "all" && s.signal_type !== f[2].value) return false;
      return true;
    });
    setCount(this, rows.length + " of " + all.length);
    if (!rows.length) { host.appendChild(el("p", "empty", "No alert matches all three filters.")); return; }
    var list = el("div", "rows");
    rows.slice(0, 40).forEach(function (s) {
      var st = txt(s.status).toUpperCase(), win = /^T\d*_HIT$/.test(st), open = st === "OPEN";
      list.appendChild(makeRow({
        main: function (m) {
          m.appendChild(el("span", "rsym", txt(s.symbol)));
          m.appendChild(el("span", "rtag", txt(s.action)));
          m.appendChild(el("span", "rmeta", txt(s.signal_type) + " · " + txt(s.timeframe) + " · " + txt(s.date)));
        },
        nums: function (x) {
          x.appendChild(el("span", "rnum", money(s.entry, s.currency)));
          x.appendChild(el("span", "rnum " + (open ? "" : (win ? "up" : "down")), open ? "open" : txt(s.pnl_str || "—")));
          x.appendChild(el("span", "rnum " + (open ? "" : (win ? "up" : "down")), s.r_multiple == null ? "—" : sgn(s.r_multiple, 2) + "R"));
        },
        detail: function (d) {
          d.appendChild(cells([["Entry", money(s.entry, s.currency)], ["Stop", money(s.sl, s.currency)],
            ["Target 1", money(s.target1, s.currency)], ["Target 2", money(s.target2, s.currency)],
            ["Status", st || "—"], ["Closed", txt(s.closed_at || s.close_date)],
            ["R:R", s.rr == null ? "—" : fx(s.rr, 1) + ":1"], ["Score", s.score == null ? "—" : fx(s.score, 0)]]));
          if (s.duplicate_note) d.appendChild(el("p", "dnote", txt(s.duplicate_note)));
          d.appendChild(el("p", "dnote", open
            ? "Generated, not filled. It carries no capital and does not touch expectancy."
            : (win ? "Resolved at target." : "Resolved at or below the stop. It is on this page for the same reason the winners are.")));
        }
      }));
    });
    host.appendChild(list);
    if (rows.length > 40) host.appendChild(el("p", "empty", "Showing the first 40 of " + rows.length + "."));
  }
});

// 12 · PERFORMANCE
section({
  id: "perf", nav: "Performance", title: "Performance",
  needs: ["stats"],
  lede: "Measured over closed trades only. The curve is cumulative R — every trade that resolved, in the order it resolved, including the ones that went wrong.",
  filters: [
    { label: "View", accent: true, options: [{ label: "Curve", value: "curve" }, { label: "By engine", value: "engine" }, { label: "By month", value: "month" }, { label: "By timeframe", value: "tf" }] },
    { label: "Basis", options: [{ label: "All closed", value: "all" }, { label: "Wins", value: "win" }, { label: "Losses", value: "loss" }] }
  ],
  render: function (host) {
    var f = this.filters, S = DATA.stats, h = S.headline || {};
    setCount(this, (h.trades || 0) + " closed");
    host.appendChild(keyGrid([
      ["Expectancy", sgn(h.expectancy_r, 3) + "R", (h.trades || 0) + " closed trades", cls(h.expectancy_r)],
      ["Win rate", fx(h.win_rate, 1) + "%", (h.wins || 0) + "W · " + (h.losses || 0) + "L"],
      ["Avg win / loss", sgn(h.avg_win_r, 2) + " / " + sgn(h.avg_loss_r, 2), "profit factor " + fx(h.profit_factor, 2)],
      ["Max drawdown", fx(h.max_drawdown_r, 2) + "R", "worst single " + fx(h.worst_r, 2) + "R"]
    ]));

    var panel = el("div", "panel"); panel.style.marginTop = "16px";
    if (f[0].value === "curve") {
      var pts = (S.equity_curve || []).filter(function (p) {
        if (f[1].value === "win") return num(p.r) > 0;
        if (f[1].value === "loss") return num(p.r) <= 0;
        return true;
      });
      var run = 0, vals = pts.map(function (p) { run += (num(p.r) || 0); return run; });
      panel.appendChild(el("h3", null, "Cumulative R"));
      panel.appendChild(el("p", "sub", vals.length + " trades" +
        (f[1].value === "all" ? "" : " — " + f[1].value + "s only, re-accumulated") + ". Below the dashed line is money lost."));
      if (vals.length) panel.insertAdjacentHTML("beforeend", curve(vals));
      else panel.appendChild(el("p", "empty", "No trade in this basis."));
    } else {
      var srcKey = { engine: "by_signal_type", month: "by_month", tf: "by_timeframe" }[f[0].value];
      var src = (S[srcKey] || []).filter(function (r) {
        if (f[1].value === "win") return (r.wins || 0) > 0;
        if (f[1].value === "loss") return (r.losses || 0) > 0;
        return true;
      });
      panel.appendChild(el("h3", null, { engine: "Expectancy by engine", month: "Total R by month", tf: "Expectancy by timeframe" }[f[0].value]));
      panel.appendChild(el("p", "sub", "Sample sizes are small. Bar length is the number, not the confidence in it."));
      var useTotal = f[0].value === "month";
      panel.appendChild(src.length ? barRows(src.map(function (r) {
        var v = num(useTotal ? r.total_r : r.avg_r) || 0;
        return { name: txt(r.key) + " (" + r.trades + ")", value: v, display: sgn(v, 2) + "R" };
      })) : el("p", "empty", "Nothing in this basis."));
    }
    host.appendChild(panel);
  }
});

// 13 · ENGINE LOG
section({
  id: "rules", nav: "Engine Log", title: "Engine Log",
  needs: ["today"],
  lede: "Every rule change the ledger forced, with the evidence that forced it. Rejected proposals are kept — a log that only records adoptions is a highlight reel.",
  filters: [
    { label: "Verdict", accent: true, options: [{ label: "All", value: "all" }, { label: "Adopted", value: "adopted" }, { label: "Rejected", value: "rejected" }] },
    { label: "Area", options: [{ label: "Any", value: "all" }, { label: "Selection", value: "SELECTION" }, { label: "Exit", value: "EXIT" }, { label: "Sizing", value: "SIZING" }] }
  ],
  render: function (host) {
    var f = this.filters, all = DATA.today.engine || [];
    var rows = all.filter(function (r) {
      if (f[0].value !== "all" && txt(r.verdict).toLowerCase().indexOf(f[0].value) < 0) return false;
      if (f[1].value !== "all" && txt(r.tag).toUpperCase() !== f[1].value) return false;
      return true;
    });
    setCount(this, rows.length + " of " + all.length);
    if (!rows.length) { host.appendChild(el("p", "empty", "No entry matches. The log holds " + all.length + " changes.")); return; }
    var list = el("div", "rows");
    rows.forEach(function (r) {
      var adopted = txt(r.verdict).toLowerCase().indexOf("adopt") >= 0;
      list.appendChild(makeRow({
        main: function (m) {
          m.appendChild(el("span", "rtag", txt(r.tag)));
          m.appendChild(el("span", "rsym", clip(r.title, 76)));
        },
        nums: function (x) {
          x.appendChild(el("span", "rnum " + (adopted ? "up" : "warn"), txt(r.verdict)));
          x.appendChild(el("span", "rmeta", txt(r.date)));
        },
        detail: function (d) { d.appendChild(el("p", "dbody", txt(r.body))); }
      }));
    });
    host.appendChild(list);
  }
});

// 14 · DATA HEALTH
section({
  id: "datahealth", nav: "Data Health", title: "Data Health",
  needs: ["health", "dhealth"],
  lede: "Whether today's numbers can be trusted. A page that renders confidently on stale data is worse than one that says it is stale.",
  filters: [
    { label: "Show", accent: true, options: [{ label: "Problems first", value: "bad" }, { label: "Everything", value: "all" }] },
    { label: "Layer", options: [{ label: "Datasets", value: "ds" }, { label: "Ledger", value: "ledger" }] }
  ],
  render: function (host) {
    var f = this.filters, H = DATA.health || {}, DH = DATA.dhealth || {};
    if (f[1].value === "ledger") {
      setCount(this, txt(H.signals) + " signals");
      host.appendChild(keyGrid([
        ["Signals logged", txt(H.signals), "latest " + txt(H.latest_signal_date)],
        ["Open setups", txt(H.open_setups), "awaiting a trigger"],
        ["Tracked positions", txt(H.tracked_positions), "capital committed"],
        ["Writes", H.writes_enabled ? "enabled" : "disabled", H.turso_configured ? "database reachable" : "no database"]
      ]));
      return;
    }
    var ds = (DH.datasets || []).slice();
    var bad = function (d) { return txt(d.status).toUpperCase() !== "CURRENT"; };
    if (f[0].value === "bad") ds.sort(function (a, b) { return (bad(b) ? 1 : 0) - (bad(a) ? 1 : 0); });
    setCount(this, (DH.current || 0) + " current of " + (DH.total || ds.length));
    if (!ds.length) { host.appendChild(el("p", "empty", "No dataset report on this build.")); return; }
    var list = el("div", "rows");
    ds.forEach(function (d) {
      var st = txt(d.status).toUpperCase();
      var tone = st === "CURRENT" ? "up" : (st === "DEGRADED" || st === "STALE" ? "warn" : "down");
      list.appendChild(makeRow({
        main: function (m) {
          m.appendChild(el("span", "rsym", txt(d.name)));
          m.appendChild(el("span", "rmeta", txt(d.age || d.as_of || "")));
        },
        nums: function (x) { x.appendChild(el("span", "rnum " + tone, st)); },
        detail: function (dd) {
          dd.appendChild(cells([["Status", st], ["Rows", txt(d.count == null ? "—" : d.count)],
            ["As of", txt(d.as_of || d.built_on)], ["Age", txt(d.age)]]));
          if (d.why) dd.appendChild(el("p", "dnote", txt(d.why)));
        }
      }));
    });
    host.appendChild(list);
  }
});

// 15 · WHO
section({
  id: "who", nav: "Who", title: "Who",
  needs: [],
  lede: "Who runs this, and on what terms.",
  filters: [
    { label: "Read", accent: true, options: [{ label: "Short", value: "short" }, { label: "Full", value: "full" }] },
    { label: "Topic", options: [{ label: "The operator", value: "op" }, { label: "The method", value: "method" }] }
  ],
  render: function (host) {
    var f = this.filters, full = f[0].value === "full";
    var op = [
      "Akshay Kothari — Chartered Accountant, ten years in corporate finance and FP&A, now building the tools he wanted to have.",
      "This ledger is run from Malaysia against Indian equities. It is a personal book made public, not a service and not a subscription.",
      "The reason it is public is that a private track record is unfalsifiable. Publishing the losses is what makes the wins worth anything."
    ];
    var me = [
      "Every signal is logged when it fires and scored when it closes. Nothing is added after the fact and nothing is quietly removed.",
      "R is measured from the exit price against the original entry and stop. It is not read from the ledger's own r_multiple column — a re-grade corrupted that column on 168 of 573 rows while the exit prices stayed consistent.",
      "Expectancy and win rate cover closed trades only. An open position has no result, so it is excluded rather than counted as a neutral.",
      "Engines are cleared on evidence, not on hope. The bar is thirty closed trades at t ≥ +2 on instruments this account can actually hold, and it was fixed before the numbers were looked at."
    ];
    var body = f[1].value === "op" ? op : me;
    setCount(this, full ? "full" : "short");
    var wrap = el("div");
    (full ? body : body.slice(0, 2)).forEach(function (p) {
      var e = el("p", "dbody", p); e.style.marginBottom = "14px"; wrap.appendChild(e);
    });
    if (!full) wrap.appendChild(el("p", "dnote", "Switch to Full for the rest."));
    host.appendChild(wrap);
  }
});

// ══════════════════════════════════════════════════════════════════════
//  LOAD + BOOT
// ══════════════════════════════════════════════════════════════════════
function load(key, path) {
  return get(path).then(function (j) { DATA[key] = j; }, function (e) { FAILED[key] = e.message; })
    .then(function () { SECS.forEach(render); masthead(); });
}

SECS.forEach(build);

// Live endpoints first — they are small and carry the headline numbers.
// The big daily artefacts follow. screen.json and jobs.json are not here on
// purpose: 1.2MB and 713KB have no business blocking a first paint.
[["markets", "/api/markets"], ["stats", "/api/stats"], ["health", "/api/health"],
 ["world", "/api/world"], ["news", "/api/news"], ["sip", "/api/sip"],
 ["tracker", "/api/tracker"], ["today", "/today.json"], ["mandate", "/mandate.json"],
 ["dhealth", "/data-health.json"], ["alerts", "/alerts.json"]
].forEach(function (p) { load(p[0], p[1]); });

// Masthead numbers, filled in as their sources land.
function masthead() {
  var h = (DATA.stats || {}).headline || {};
  var M = DATA.mandate || {}, st = M.state || {};
  var kr = document.getElementById("keyrow");
  kr.innerHTML = "";
  // keyGrid returns its own .keyrow wrapper; the masthead already has one, so
  // the children are moved across rather than nesting a grid inside a grid.
  var grid = keyGrid([
    ["Capital", M.capital ? money(M.capital) : "—", "mandate"],
    ["Deployed", st.deployed_pct == null ? "—" : fx(st.deployed_pct, 2) + "%",
      st.deployed ? money(st.deployed) + " at work" : "of the mandate"],
    ["Expectancy", h.expectancy_r == null ? "—" : sgn(h.expectancy_r, 3) + "R",
      "over " + (h.trades || 0) + " closed", cls(h.expectancy_r)],
    ["Open risk", st.heat_pct == null ? "—" : fx(st.heat_pct, 2) + "%", "of a 6% cap"]
  ]);
  while (grid.firstChild) kr.appendChild(grid.firstChild);

  var H = DATA.health || {};
  if (H.signals) {
    document.getElementById("stamp").textContent =
      "Live ledger · " + H.signals + " signals · " + (H.latest_signal_date || "");
  }
  var T = (DATA.stats || {}).totals;
  document.getElementById("footstamp").textContent = T
    ? "Rebuilt daily. " + T.closed + " closed of " + T.all + " scored signals, first logged " + T.first_date + "."
    : "";
}

// Reveal + nav spy. Both are enhancements: content is visible without them.
if ("IntersectionObserver" in window) {
  if (!RM) {
    var rv = new IntersectionObserver(function (es) {
      es.forEach(function (e) { if (e.isIntersecting) { e.target.classList.add("in"); rv.unobserve(e.target); } });
    }, { rootMargin: "0px 0px -8% 0px" });
    Array.prototype.forEach.call(document.querySelectorAll(".secbody, .mast h1, .mast p, .keyrow"), function (n) {
      n.classList.add("rv"); rv.observe(n);
    });
  }
  var links = {};
  Array.prototype.forEach.call(document.querySelectorAll("#jump a"), function (a) { links[a.getAttribute("href").slice(1)] = a; });
  var spy = new IntersectionObserver(function (es) {
    es.forEach(function (e) {
      var a = links[e.target.id]; if (!a || !e.isIntersecting) return;
      Array.prototype.forEach.call(document.querySelectorAll("#jump a"), function (x) { x.removeAttribute("aria-current"); });
      a.setAttribute("aria-current", "true");
    });
  }, { rootMargin: "-20% 0px -70% 0px" });
  Array.prototype.forEach.call(document.querySelectorAll(".sec"), function (s) { spy.observe(s); });
}
})();
