/* ═══════════════════════════════════════════════════════════════════════
   v2-core.js — the shared machinery behind both sites.

   news.askakshay.com and life.askakshay.com render from one design system
   and one set of primitives. This file holds them so a change to a row, a
   filter or a chart lands on both — the two pages differ only in which
   sections they declare and which sources those sections read.

   Exposes window.SD:
     SD.section(o)   declare a section
     SD.boot(list)   build every declared section, then load its sources
     plus the helpers sections are written against.

   RULES ENFORCED HERE, NOT PER SECTION
   ------------------------------------
   1. A failed fetch renders as "unavailable" naming the source and the
      error. Never as zero — a section quietly showing 0 when its feed is
      down is the most misleading thing either page could do.
   2. Detail hides behind a +, built lazily on first open.
   3. Counts read "n of m", so a filter can never hide rows silently.
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
//  LOAD + BOOT
// ══════════════════════════════════════════════════════════════════════
function load(key, path) {
  return get(path).then(function (j) { DATA[key] = j; }, function (e) { FAILED[key] = e.message; })
    .then(function () { SECS.forEach(render); if (MAST) MAST(); });
}

var MAST = null;

/**
 * Build every declared section, then fetch its sources.
 *
 * `sources` is ordered deliberately: the small live endpoints carrying the
 * headline numbers first, the big daily artefacts after. screen.json (1.2MB)
 * and jobs.json (713KB) are never in this list — they have no business
 * blocking a first paint and load only when a filter asks for them.
 */
function boot(sources, masthead) {
  MAST = masthead || null;
  SECS.forEach(build);
  sources.forEach(function (p) { load(p[0], p[1]); });

  // Reveal + nav spy. Both are enhancements: the content is visible without
  // them, so a missing IntersectionObserver costs an animation, not a page.
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
}

// ── exported surface ──────────────────────────────────────────────────
window.SD = {
  section: section, boot: boot,
  el: el, num: num, fx: fx, sgn: sgn, cls: cls, money: money, txt: txt,
  clip: clip, when: when, cells: cells, keyGrid: keyGrid, barRows: barRows,
  curve: curve, makeRow: makeRow, setCount: setCount, bars: bars,
  load: load, DATA: DATA, FAILED: FAILED
};
})();
