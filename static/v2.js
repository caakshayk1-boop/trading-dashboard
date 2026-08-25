/* ═══════════════════════════════════════════════════════════════════════
   v2.js — the seventeen sections of news.askakshay.com.

   The machinery is in v2-core.js, which life.js shares. This file is only
   the declarations: what each section is called, which sources it reads,
   which filters it offers, and how it draws a row.
   ═══════════════════════════════════════════════════════════════════════ */
(function () {
"use strict";
// The core carries every primitive this file is written against. If it fails
// to load — a bad deploy, a cache serving one file and not the other, an
// adblocker — the page would otherwise render as a masthead over nothing,
// which is indistinguishable from "there is no data today". It was blank for
// about a minute during the deploy that introduced this split. Say so instead.
if (!window.SD) {
  var _m = document.getElementById("main") || document.body;
  var _p = document.createElement("p");
  _p.className = "empty";
  _p.style.cssText = "max-width:60ch;margin:40px auto;padding:0 24px";
  _p.textContent = "The page script did not load, so nothing below could be built. " +
    "This is a fault on this site, not an empty day — reload, and if it persists " +
    "the ledger API is still readable directly at /api/stats and /api/signals.";
  _m.appendChild(_p);
  return;
}

var SD = window.SD;
var el = SD.el, num = SD.num, fx = SD.fx, sgn = SD.sgn, cls = SD.cls, money = SD.money;
var txt = SD.txt, clip = SD.clip, when = SD.when, cells = SD.cells, keyGrid = SD.keyGrid;
var barRows = SD.barRows, curve = SD.curve, makeRow = SD.makeRow, setCount = SD.setCount;
var section = SD.section, load = SD.load;
var DATA = SD.DATA, FAILED = SD.FAILED;

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

// 04 · FINDINGS — where open setups sit against their own levels
section({
  id: "findings", nav: "Findings", title: "Findings",
  needs: ["today", "signals"],
  lede: "Open setups joined to today's price. A setup that has already run past its entry is a different proposition from one still waiting — and the sector column is where crowding shows up.",
  filters: [
    { label: "Position", accent: true, options: [{ label: "All", value: "all" }, { label: "Past entry", value: "above" }, { label: "Still below", value: "below" }] },
    { label: "Sort", options: [{ label: "Distance", value: "dist" }, { label: "Sector", value: "sec" }] }
  ],
  render: function (host) {
    var f = this.filters;
    // open_context is {symbol: {price, sector}} — prices only. The levels live
    // on the signals feed, so the two are joined here rather than assumed to
    // be on one payload.
    var ctx = DATA.today.open_context || {};
    var open = (DATA.signals.signals || []).filter(function (s) { return txt(s.status).toUpperCase() === "OPEN"; });
    var rows = open.map(function (s) {
      var c = ctx[txt(s.symbol).toUpperCase()] || ctx[txt(s.symbol)];
      if (!c || num(c.price) == null || num(s.entry) == null) return null;
      return { s: s, price: num(c.price), sector: txt(c.sector || "—"),
               d: (num(c.price) - num(s.entry)) / num(s.entry) * 100 };
    }).filter(Boolean).filter(function (r) {
      if (f[0].value === "above") return r.d > 0;
      if (f[0].value === "below") return r.d <= 0;
      return true;
    });
    rows.sort(f[1].value === "sec"
      ? function (a, b) { return a.sector.localeCompare(b.sector) || Math.abs(b.d) - Math.abs(a.d); }
      : function (a, b) { return Math.abs(b.d) - Math.abs(a.d); });
    setCount(this, rows.length + " priced of " + open.length + " open");
    if (!rows.length) {
      host.appendChild(el("p", "empty", "No open setup carries a live price on this build. " + open.length + " setups are open; the price context covers " + Object.keys(ctx).length + " symbols, and none of them overlap today."));
      return;
    }
    // Sector concentration — the finding the table alone will not show you.
    var bySec = {};
    rows.forEach(function (r) { bySec[r.sector] = (bySec[r.sector] || 0) + 1; });
    var secs = Object.keys(bySec).map(function (k) {
      return { name: k, value: bySec[k], display: String(bySec[k]), color: "var(--blue)" };
    }).sort(function (a, b) { return b.value - a.value; });
    if (secs.length > 1) {
      var panel = el("div", "panel"); panel.style.marginBottom = "20px";
      panel.appendChild(el("h3", null, "Where the open book is crowded"));
      panel.appendChild(el("p", "sub", "Open setups by sector. Several names in one sector is one bet wearing several tickers."));
      panel.appendChild(barRows(secs, true));
      host.appendChild(panel);
    }
    var list = el("div", "rows");
    rows.slice(0, 30).forEach(function (r) {
      var s = r.s, cur = s.currency || "₹";
      list.appendChild(makeRow({
        main: function (m) {
          m.appendChild(el("span", "rsym", txt(s.symbol)));
          m.appendChild(el("span", "rtag", r.sector.slice(0, 18)));
          m.appendChild(el("span", "rmeta", txt(s.signal_type) + " · " + txt(s.date)));
        },
        nums: function (x) {
          x.appendChild(el("span", "rnum", money(r.price, cur)));
          x.appendChild(el("span", "rnum " + cls(r.d), sgn(r.d, 1, "%") + " vs entry"));
        },
        detail: function (d) {
          d.appendChild(cells([["Last", money(r.price, cur)], ["Entry", money(s.entry, cur)],
            ["Stop", money(s.sl, cur)], ["Target 1", money(s.target1, cur)],
            ["Distance", sgn(r.d, 2, "%")], ["Sector", r.sector]]));
          d.appendChild(el("p", "dnote", r.d > 0
            ? "Price has moved past the entry. Taking it now changes the reward/risk the signal was scored on — the stop is unchanged but the distance to it is not."
            : "Still below entry. The setup has not triggered and carries no capital."));
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

// 08 · FUND SCREEN
section({
  id: "funds", nav: "Fund Screen", title: "Fund Screen",
  needs: ["sip"],
  lede: "The mutual-fund screen behind the SIP buckets. Direct plans only — the expense ratio is not in any free feed, so plan type is the one cost lever that can be verified.",
  filters: [
    { label: "Bucket", accent: true, options: [{ label: "All", value: "all" }, { label: "Top ranked", value: "top" }] },
    { label: "Sort", options: [{ label: "Rank", value: "rank" }, { label: "Name", value: "name" }] }
  ],
  render: function (host) {
    var f = this.filters, S = DATA.sip || {};
    var fs = S.fund_screen || {};
    var cats = Array.isArray(fs) ? fs : (fs.categories || fs.rows || []);
    if (!cats.length) {
      setCount(this, "unavailable");
      host.appendChild(el("p", "empty", "The weekly fund screen has not populated on this build. It runs on its own clock, and an empty cache is shown as empty rather than as zero funds."));
      return;
    }
    var rows = [];
    cats.forEach(function (c) {
      (c.funds || c.rows || []).forEach(function (fn, i) {
        if (f[0].value === "top" && i > 2) return;
        rows.push({ cat: txt(c.name || c.category), fund: fn, rank: i + 1 });
      });
    });
    rows.sort(f[1].value === "name"
      ? function (a, b) { return txt(a.fund.name).localeCompare(txt(b.fund.name)); }
      : function (a, b) { return a.rank - b.rank; });
    setCount(this, rows.length + " funds · " + cats.length + " categories");
    var list = el("div", "rows");
    rows.slice(0, 30).forEach(function (r) {
      var fn = r.fund;
      list.appendChild(makeRow({
        main: function (m) {
          m.appendChild(el("span", "rtag", String(r.rank).padStart(2, "0")));
          m.appendChild(el("span", "rsym", clip(fn.name || fn.scheme, 52)));
          m.appendChild(el("span", "rmeta", r.cat));
        },
        nums: function (x) {
          x.appendChild(el("span", "rnum", fn.nav == null ? "—" : money(fn.nav)));
          x.appendChild(el("span", "rnum " + cls(fn.ret_1y != null ? fn.ret_1y : fn.cagr),
            sgn(fn.ret_1y != null ? fn.ret_1y : fn.cagr, 1, "%")));
        },
        detail: function (d) {
          d.appendChild(cells([["NAV", fn.nav == null ? "—" : money(fn.nav)],
            ["1Y", sgn(fn.ret_1y, 1, "%")], ["3Y", sgn(fn.ret_3y, 1, "%")],
            ["5Y", sgn(fn.ret_5y, 1, "%")], ["Category", r.cat], ["Rank", "#" + r.rank]]));
          d.appendChild(el("p", "dnote", "Expense ratio is not published in the free AMFI feed, so it cannot be compared here. Direct plans are chosen for that reason — the cost difference is structural rather than measured."));
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

// 10 · SWP — the other end of the same plan
section({
  id: "swp", nav: "SWP", title: "SWP",
  needs: ["sip"],
  lede: "What the corpus pays out if you stop adding and start drawing. Arithmetic on the SIP projections — a withdrawal rate, not a promise.",
  filters: [
    { label: "Draw", accent: true, options: [{ label: "4%", value: 4 }, { label: "5%", value: 5 }, { label: "6%", value: 6 }] },
    { label: "Return", options: [{ label: "12%", value: "r12" }, { label: "14%", value: "r14" }, { label: "16%", value: "r16" }] }
  ],
  render: function (host) {
    var f = this.filters, S = DATA.sip || {}, proj = (S.projections || []).slice();
    if (!proj.length) { setCount(this, "unavailable"); host.appendChild(el("p", "empty", "No projection to draw against.")); return; }
    var rate = f[0].value / 100, key = f[1].value;
    setCount(this, f[0].value + "% of corpus");
    host.appendChild(keyGrid(proj.slice(0, 4).map(function (p) {
      var corpus = num(p[key]) || 0;
      return [p.years + " years", money(Math.round(corpus * rate / 12)) + "/mo",
              "on " + money(corpus) + " at " + key.replace("r", "") + "%"];
    })));
    var panel = el("div", "panel"); panel.style.marginTop = "16px";
    panel.appendChild(el("h3", null, "Monthly draw by horizon"));
    panel.appendChild(el("p", "sub", "A " + f[0].value + "% annual withdrawal, paid monthly. The bar is the payment, not the corpus."));
    panel.appendChild(barRows(proj.map(function (p) {
      var v = Math.round((num(p[key]) || 0) * rate / 12);
      return { name: p.years + "y", value: v, display: money(v), color: "var(--blue)" };
    }), true));
    host.appendChild(panel);
    host.appendChild(el("p", "lede", "A withdrawal rate is a rule of thumb, not a guarantee. It assumes the corpus keeps earning while it is being drawn down, and it says nothing about the order returns arrive in — which is the risk that actually breaks retirement plans."));
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

// 12 · PAPER WALLET — the distinction the whole ledger rests on
section({
  id: "paperwallet", nav: "Paper Wallet", title: "Paper Wallet",
  needs: ["mandate", "tracker"],
  lede: "What the rules would have placed, against what was actually placed. The gap between these two numbers is the honest state of this operation.",
  filters: [
    { label: "Show", accent: true, options: [{ label: "The gap", value: "gap" }, { label: "Paper book", value: "book" }] },
    { label: "Detail", options: [{ label: "Summary", value: "sum" }, { label: "Per ticket", value: "each" }] }
  ],
  render: function (host) {
    var f = this.filters, M = DATA.mandate || {}, st = M.state || {}, T = DATA.tracker || {};
    var real = T.count || 0, paper = (M.admitted || []).length;
    if (f[0].value === "gap") {
      setCount(this, paper + " paper · " + real + " real");
      host.appendChild(keyGrid([
        ["Paper tickets", String(paper), "sized by the rulebook"],
        ["Real positions", String(real), "confirmed by hand"],
        ["Paper notional", money(st.deployed), "if every ticket were placed"],
        ["Real capital", real ? "—" : "₹0", real ? "see Portfolio" : "nothing committed"]
      ]));
      host.appendChild(el("p", "lede", "Nothing in this repository can place an order — the broker link is read-only. A signal becomes a position only when the order is placed manually and confirmed with /confirm on Telegram. Across the whole feed that has never happened, which is why real capital reads zero and why this section exists rather than being quietly folded into Portfolio."));
      return;
    }
    setCount(this, paper + " tickets");
    if (!paper) { host.appendChild(el("p", "empty", "The rulebook placed nothing today.")); return; }
    if (f[1].value === "sum") {
      var byH = M.by_horizon || {};
      var items = Object.keys(byH).map(function (k) {
        return { name: k.toLowerCase(), value: byH[k].notional, display: money(byH[k].notional), color: "var(--coral)" };
      });
      var panel = el("div", "panel");
      panel.appendChild(el("h3", null, "Paper notional by horizon"));
      panel.appendChild(el("p", "sub", "Where the rulebook would put the money, if it could."));
      panel.appendChild(items.length ? barRows(items, true) : el("p", "empty", "No horizon breakdown."));
      host.appendChild(panel);
      return;
    }
    var list = el("div", "rows");
    (M.admitted || []).forEach(function (t) {
      list.appendChild(makeRow({
        main: function (m) {
          m.appendChild(el("span", "rsym", t.symbol));
          m.appendChild(el("span", "rtag", "paper"));
          m.appendChild(el("span", "rmeta", t.horizon_label + " · " + t.engine));
        },
        nums: function (x) {
          x.appendChild(el("span", "rnum", money(t.notional)));
          x.appendChild(el("span", "rnum warn", "unconfirmed"));
        },
        detail: function (d) {
          d.appendChild(cells([["Would buy", t.qty + " sh"], ["At", money(t.entry)],
            ["Notional", money(t.notional)], ["Risk", money(t.risk_amount)],
            ["Status", "paper only"], ["To confirm", "/confirm " + t.symbol]]));
          d.appendChild(el("p", "dnote", "This ticket carries no capital. It is scored so the engine behind it accumulates a record, and it is excluded from deployment, heat and P&L until confirmed."));
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

function masthead() {
  var h = (SD.DATA.stats || {}).headline || {};
  var M = SD.DATA.mandate || {}, st = M.state || {};
  var kr = document.getElementById("keyrow");
  kr.innerHTML = "";
  var grid = keyGrid([
    ["Capital", M.capital ? money(M.capital) : "—", "mandate"],
    ["Deployed", st.deployed_pct == null ? "—" : fx(st.deployed_pct, 2) + "%",
      st.deployed ? money(st.deployed) + " at work" : "of the mandate"],
    ["Expectancy", h.expectancy_r == null ? "—" : sgn(h.expectancy_r, 3) + "R",
      "over " + (h.trades || 0) + " closed", cls(h.expectancy_r)],
    ["Open risk", st.heat_pct == null ? "—" : fx(st.heat_pct, 2) + "%", "of a 6% cap"]
  ]);
  while (grid.firstChild) kr.appendChild(grid.firstChild);

  var H = SD.DATA.health || {};
  if (H.signals) {
    document.getElementById("stamp").textContent =
      "Live ledger · " + H.signals + " signals · " + (H.latest_signal_date || "");
  }
  var T = (SD.DATA.stats || {}).totals;
  document.getElementById("footstamp").textContent = T
    ? "Rebuilt daily. " + T.closed + " closed of " + T.all + " scored signals, first logged " + T.first_date + "."
    : "";
}

SD.boot([
  ["markets", "/api/markets"], ["stats", "/api/stats"], ["health", "/api/health"],
  ["world", "/api/world"], ["news", "/api/news"], ["sip", "/api/sip"],
  ["tracker", "/api/tracker"], ["today", "/today.json"], ["mandate", "/mandate.json"],
  ["signals", "/api/signals?limit=300"], ["dhealth", "/data-health.json"],
  ["alerts", "/alerts.json"]
], masthead);
})();
