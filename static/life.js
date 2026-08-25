/* ═══════════════════════════════════════════════════════════════════════
   life.js — the fifteen sections of life.askakshay.com.

   Same machinery as news.askakshay.com (v2-core.js), same design system,
   different subject. Everything here reads /today.json's `desk` block, which
   the daily build already writes, plus /jobs.json for the careers board.

   The desk block is twenty keys of prepared content — chess, wisdom, book,
   way, quote, lesson, case, fpna, cfo, money_hack, dubai, daughter,
   productivity, father, life_wisdom, spanish, vocab, speaking and the two
   interview banks. Nothing here is generated at request time; the build
   picks the day's rotation and this file renders it.
   ═══════════════════════════════════════════════════════════════════════ */
(function () {
"use strict";
var SD = window.SD;
var el = SD.el, num = SD.num, fx = SD.fx, txt = SD.txt, clip = SD.clip;
var cells = SD.cells, keyGrid = SD.keyGrid, barRows = SD.barRows;
var makeRow = SD.makeRow, setCount = SD.setCount, section = SD.section, load = SD.load;
var DATA = SD.DATA, FAILED = SD.FAILED;

function desk() { return (DATA.today || {}).desk || {}; }

/** A block of prose with a heading — the shape most of this page takes. */
function prose(host, items, opts) {
  var wrap = el("div");
  items.forEach(function (it) {
    if (!it || !it.body) return;
    var b = el("div"); b.style.marginBottom = "22px";
    if (it.title) {
      var h = el("h3"); h.style.cssText = "font:600 17px/1.3 var(--disp);margin-bottom:8px";
      h.textContent = it.title; b.appendChild(h);
    }
    var p = el("p", "dbody", it.body); p.style.maxWidth = "68ch"; b.appendChild(p);
    if (it.note) b.appendChild(el("p", "dnote", it.note));
    wrap.appendChild(b);
  });
  if (!wrap.children.length) {
    host.appendChild(el("p", "empty", (opts && opts.empty) || "Nothing prepared for today in this section."));
    return;
  }
  host.appendChild(wrap);
}

/** Two-mode filter used by most prose sections: short vs full. */
function readFilter() {
  return { label: "Read", accent: true, options: [{ label: "Short", value: "short" }, { label: "Full", value: "full" }] };
}

// ══════════════════════════════════════════════════════════════════════
// 01 · FINANCE CAREERS
// ══════════════════════════════════════════════════════════════════════
section({
  id: "careers", nav: "Careers", title: "Finance Careers", highlight: true,
  needs: [],
  lede: "Senior FP&A and finance roles in the Gulf and Malaysia, scraped daily. The board is 713KB, so it loads when you ask for it.",
  filters: [
    { label: "Board", accent: true, options: [
      { label: "On demand", value: "off" }, { label: "Load roles", value: "on" }
    ], onPick: function (v) { if (v === "on" && DATA.jobs === undefined && !FAILED.jobs) load("jobs", "/jobs.json"); } },
    { label: "Where", options: [{ label: "All", value: "all" }, { label: "Dubai / UAE", value: "ae" }, { label: "Malaysia", value: "my" }] },
    { label: "Tier", options: [{ label: "Any", value: "all" }, { label: "S-tier only", value: "S" }] }
  ],
  render: function (host) {
    var f = this.filters;
    if (f[0].value === "off") {
      setCount(this, "not loaded");
      host.appendChild(el("p", "empty", "Press “Load roles” to pull the board. Kept behind a click because it is 713KB and nothing above it should wait on a job feed."));
      return;
    }
    if (FAILED.jobs) { setCount(this, "unavailable"); host.appendChild(el("p", "empty", "The careers board did not load: " + FAILED.jobs)); return; }
    if (DATA.jobs === undefined) { setCount(this, "loading"); host.appendChild(el("p", "loading", "Loading the board…")); return; }
    var J = DATA.jobs, all = (J.jobs || []).filter(function (j) { return j.visible !== false; });
    var rows = all.filter(function (j) {
      var loc = txt(j.location || j.city).toLowerCase();
      if (f[1].value === "ae" && !/dubai|abu dhabi|uae|emirat/.test(loc)) return false;
      if (f[1].value === "my" && !/malaysia|kuala|kl\b|selangor/.test(loc)) return false;
      if (f[2].value === "S" && txt(j.tier).toUpperCase() !== "S") return false;
      return true;
    });
    setCount(this, rows.length + " of " + all.length);
    if (!rows.length) { host.appendChild(el("p", "empty", "No role matches. The board holds " + all.length + " visible roles from " + ((J.stats || {}).sources_ok || "?") + " sources.")); return; }
    var list = el("div", "rows");
    rows.slice(0, 40).forEach(function (j) {
      list.appendChild(makeRow({
        main: function (m) {
          if (txt(j.tier).toUpperCase() === "S") m.appendChild(el("span", "rtag", "S-tier"));
          m.appendChild(el("span", "rsym", clip(j.title, 62)));
          m.appendChild(el("span", "rmeta", txt(j.company)));
        },
        nums: function (x) {
          x.appendChild(el("span", "rnum", clip(j.location || j.city, 22)));
          x.appendChild(el("span", "rmeta", txt(j.source)));
        },
        detail: function (d) {
          d.appendChild(cells([["Company", txt(j.company)], ["Location", txt(j.location || j.city)],
            ["Source", txt(j.source)], ["Posted", txt(j.posted || j.date)],
            ["Tier", txt(j.tier) || "—"], ["Salary", txt(j.salary) || "not stated"]]));
          if (j.summary) d.appendChild(el("p", "dbody", clip(j.summary, 420)));
          if (j.link || j.url) {
            var a = el("a", null, "Open the posting →");
            a.href = j.link || j.url; a.target = "_blank"; a.rel = "noopener";
            var p = el("p", "dnote"); p.appendChild(a); d.appendChild(p);
          }
        }
      }));
    });
    host.appendChild(list);
    if (rows.length > 40) host.appendChild(el("p", "empty", "Showing the first 40 of " + rows.length + "."));
  }
});

// 02 · CFO TRACK
section({
  id: "interview", nav: "CFO Track", title: "CFO Track",
  needs: ["today"],
  lede: "The interview bank, and the gap between where the CV is and where the role is.",
  filters: [
    { label: "Bank", accent: true, options: [{ label: "Technical", value: "tech" }, { label: "Behavioural", value: "soft" }, { label: "The track", value: "cfo" }] },
    { label: "Answers", options: [{ label: "Hidden", value: "hide" }, { label: "Shown", value: "show" }] }
  ],
  render: function (host) {
    var f = this.filters, D = desk();
    if (f[0].value === "cfo") {
      var c = D.cfo || {};
      setCount(this, c.index ? c.index + " of " + c.total : "");
      prose(host, [{ title: c.title, body: c.body }], { empty: "No CFO-track note prepared today." });
      return;
    }
    var bank = f[0].value === "tech" ? (D.interview_tech || []) : (D.interview_soft || []);
    setCount(this, bank.length + " questions");
    if (!bank.length) { host.appendChild(el("p", "empty", "No questions in this bank today.")); return; }
    var show = f[1].value === "show";
    var list = el("div", "rows");
    bank.forEach(function (q) {
      list.appendChild(makeRow({
        open: show,
        main: function (m) { m.appendChild(el("span", "rsym", clip(q.q, 92))); },
        nums: function (x) { x.appendChild(el("span", "rmeta", show ? "answer shown" : "answer hidden")); },
        detail: function (d) { d.appendChild(el("p", "dbody", txt(q.a))); }
      }));
    });
    host.appendChild(list);
  }
});

// 03 · DAILY BRIEF — the FP&A lesson of the day
section({
  id: "brief", nav: "Daily Brief", title: "Daily Brief", highlight: true,
  needs: ["today"],
  lede: "One FP&A idea a day, and the Gulf move it serves. This section stays open — it is the reason the page gets read.",
  filters: [
    { label: "Topic", accent: true, options: [{ label: "FP&A", value: "fpna" }, { label: "Dubai", value: "dubai" }, { label: "Money", value: "money" }] },
    readFilter()
  ],
  render: function (host) {
    var f = this.filters, D = desk();
    var src = f[0].value === "fpna" ? D.fpna : (f[0].value === "dubai" ? D.dubai : D.money_hack);
    src = src || {};
    setCount(this, src.index ? src.index + " of " + src.total : "");
    var body = txt(src.body);
    if (f[1].value === "short" && body.length > 420) body = body.slice(0, 420) + "…";
    prose(host, [{ title: src.title, body: body,
      note: f[1].value === "short" && txt(src.body).length > 420 ? "Switch to Full for the rest." : "" }],
      { empty: "No note prepared for this topic today." });
    if (f[0].value === "dubai" && src.targets) {
      var t = Array.isArray(src.targets) ? src.targets : [src.targets];
      host.appendChild(keyGrid(t.slice(0, 4).map(function (x, i) {
        return ["Target " + (i + 1), txt(x), ""];
      })));
    }
    if (src.action) host.appendChild(el("p", "lede", (src.action_label ? src.action_label + ": " : "") + txt(src.action)));
  }
});

// 04 · SMART READS — the case study bank
section({
  id: "smartreads", nav: "Smart Reads", title: "Smart Reads",
  needs: ["today"],
  lede: "One case a day: what somebody did, and the part of it that transfers.",
  filters: [
    { label: "Kind", accent: true, options: [{ label: "Case study", value: "case" }, { label: "Tradition", value: "lesson" }] },
    readFilter()
  ],
  render: function (host) {
    var f = this.filters, D = desk();
    if (f[0].value === "case") {
      var c = D["case"] || {};
      setCount(this, c.title ? "today" : "");
      var story = txt(c.story);
      if (f[1].value === "short" && story.length > 460) story = story.slice(0, 460) + "…";
      prose(host, [{ title: c.title, body: story, note: c.lesson ? "The transfer: " + txt(c.lesson) : "" }],
        { empty: "No case prepared today." });
    } else {
      var l = D.lesson || {};
      setCount(this, txt(l.tradition));
      prose(host, [{ title: txt(l.tradition), body: txt(l.lesson),
        note: l.source ? "Source: " + txt(l.source) : "" }],
        { empty: "No lesson prepared today." });
    }
  }
});

// 05 · BOOK
section({
  id: "book", nav: "Book", title: "Book",
  needs: ["today"],
  lede: "The book being worked through, a chapter at a time. Not a review — the thing to do because of it.",
  filters: [
    { label: "Show", accent: true, options: [{ label: "The lesson", value: "lesson" }, { label: "The quote", value: "quote" }] },
    { label: "Action", options: [{ label: "With action", value: "yes" }, { label: "Lesson only", value: "no" }] }
  ],
  render: function (host) {
    var f = this.filters, b = desk().book || {};
    if (!b.book) { setCount(this, ""); host.appendChild(el("p", "empty", "No book entry today.")); return; }
    setCount(this, clip(b.book, 34));
    host.appendChild(cells([["Book", txt(b.book)], ["Author", txt(b.author)],
      ["Chapter", txt(b.chapter)], ["Entry", b.index == null ? "—" : "#" + b.index]]));
    var body = f[0].value === "quote" ? txt(b.key_quote) : txt(b.lesson);
    prose(host, [{ body: body }], { empty: "Nothing under this view today." });
    if (f[1].value === "yes" && b.action) host.appendChild(el("p", "lede", "Do this: " + txt(b.action)));
  }
});

// 06 · PODCASTS — the listening rotation lives in `way`
section({
  id: "podcasts", nav: "Podcasts", title: "Listening",
  needs: ["today"],
  lede: "What is on in the background, and the drill that goes with it.",
  filters: [
    { label: "Track", accent: true, options: [{ label: "Drill", value: "drill" }, { label: "Arabic", value: "arabic" }] },
    readFilter()
  ],
  render: function (host) {
    var f = this.filters, w = desk().way || {};
    var body = txt(w[f[0].value]);
    setCount(this, body ? "today" : "");
    if (f[1].value === "short" && body.length > 380) body = body.slice(0, 380) + "…";
    prose(host, [{ title: f[0].value === "drill" ? "Today's drill" : "Arabic", body: body }],
      { empty: "Nothing on the listening rotation today." });
  }
});

// 07 · LANGUAGE
section({
  id: "language", nav: "Language", title: "Language",
  needs: ["today"],
  lede: "Spanish and English, two words a day each. Said out loud or it does not count.",
  filters: [
    { label: "Language", accent: true, options: [{ label: "Spanish", value: "spanish" }, { label: "English", value: "vocab" }] },
    { label: "Show", options: [{ label: "Word only", value: "word" }, { label: "With sentence", value: "full" }] }
  ],
  render: function (host) {
    var f = this.filters, D = desk(), items = D[f[0].value] || [];
    setCount(this, items.length + " words");
    if (!items.length) { host.appendChild(el("p", "empty", "No words today.")); return; }
    var list = el("div", "rows");
    items.forEach(function (w) {
      list.appendChild(makeRow({
        open: f[1].value === "full",
        main: function (m) {
          m.appendChild(el("span", "rsym", txt(w.word)));
          if (w.say) m.appendChild(el("span", "rmeta", txt(w.say)));
        },
        nums: function (x) { x.appendChild(el("span", "rnum", clip(w.meaning, 40))); },
        detail: function (d) {
          d.appendChild(el("p", "dbody", txt(w.meaning)));
          if (w.es) d.appendChild(el("p", "dnote", txt(w.es) + (w.en ? " — " + txt(w.en) : "")));
          if (w.use) d.appendChild(el("p", "dnote", "Use it: " + txt(w.use)));
        }
      }));
    });
    host.appendChild(list);
  }
});

// 08 · FATHER
section({
  id: "father", nav: "Father", title: "Father", highlight: true,
  needs: ["today"],
  lede: "One thing to do today, sized to her actual age. Open by default — it is the section most worth not scrolling past.",
  filters: [
    { label: "Show", accent: true, options: [{ label: "Today's things", value: "do" }, { label: "Where she is", value: "age" }] },
    { label: "Detail", options: [{ label: "All", value: "all" }, { label: "First only", value: "one" }] }
  ],
  render: function (host) {
    var f = this.filters, D = desk(), dg = D.daughter || {};
    if (f[0].value === "age") {
      setCount(this, dg.heading ? txt(dg.heading) : "");
      host.appendChild(keyGrid([
        ["Age", (dg.months != null ? dg.months + "m" : "—") + (dg.days != null ? " " + dg.days + "d" : ""), "since " + txt(dg.born)],
        ["Band", txt(dg.band) || "—", "developmental stage"],
        ["Heading", txt(dg.heading) || "—", ""]
      ]));
      return;
    }
    var items = D.father || [];
    if (f[1].value === "one") items = items.slice(0, 1);
    setCount(this, items.length + " today");
    if (!items.length) { host.appendChild(el("p", "empty", "Nothing prepared today.")); return; }
    var list = el("div", "rows");
    items.forEach(function (it) {
      list.appendChild(makeRow({
        open: true,
        main: function (m) { m.appendChild(el("span", "rsym", txt(it.title))); },
        nums: function (x) { if (dg.band) x.appendChild(el("span", "rmeta", txt(dg.band))); },
        detail: function (d) {
          d.appendChild(el("p", "dbody", txt(it["do"])));
          if (it.why) d.appendChild(el("p", "dnote", txt(it.why)));
        }
      }));
    });
    host.appendChild(list);
  }
});

// 09 · WISDOM
section({
  id: "wisdom", nav: "Wisdom", title: "Wisdom",
  needs: ["today"],
  lede: "One idea, held long enough to be used rather than admired.",
  filters: [
    { label: "Source", accent: true, options: [{ label: "The rotation", value: "wisdom" }, { label: "Traditions", value: "life_wisdom" }] },
    readFilter()
  ],
  render: function (host) {
    var f = this.filters, D = desk();
    if (f[0].value === "wisdom") {
      var w = D.wisdom || {};
      setCount(this, w.index ? w.index + " of " + w.total : "");
      var body = txt(w.body);
      if (f[1].value === "short" && body.length > 400) body = body.slice(0, 400) + "…";
      prose(host, [{ title: w.title, body: body }], { empty: "Nothing today." });
      return;
    }
    var items = D.life_wisdom || [];
    setCount(this, items.length + " traditions");
    if (!items.length) { host.appendChild(el("p", "empty", "Nothing today.")); return; }
    var list = el("div", "rows");
    items.forEach(function (it) {
      list.appendChild(makeRow({
        open: f[1].value === "full",
        main: function (m) {
          m.appendChild(el("span", "rtag", txt(it.tradition)));
          m.appendChild(el("span", "rsym", txt(it.term)));
        },
        nums: function (x) { x.appendChild(el("span", "rmeta", clip(it.translation, 40))); },
        detail: function (d) {
          d.appendChild(el("p", "dbody", txt(it.translation)));
          if (it.practice || it.body) d.appendChild(el("p", "dnote", txt(it.practice || it.body)));
        }
      }));
    });
    host.appendChild(list);
  }
});

// 10 · THE MIND
section({
  id: "mind", nav: "The Mind", title: "The Mind",
  needs: ["today"],
  lede: "The quote of the day, and who is on the hook for it.",
  filters: [
    { label: "Show", accent: true, options: [{ label: "Quote", value: "quote" }, { label: "Stillness", value: "still" }] },
    readFilter()
  ],
  render: function (host) {
    var f = this.filters, D = desk();
    if (f[0].value === "quote") {
      var q = D.quote || {};
      setCount(this, q.index ? q.index + " of " + q.total : "");
      if (!q.quote) { host.appendChild(el("p", "empty", "No quote today.")); return; }
      var big = el("p"); big.style.cssText =
        "font:600 clamp(20px,2.6vw,30px)/1.35 var(--disp);letter-spacing:-.02em;max-width:22ch;margin:0 0 16px";
      big.textContent = "“" + txt(q.quote) + "”";
      host.appendChild(big);
      host.appendChild(el("p", "dnote", "— " + txt(q.name)));
      return;
    }
    var w = D.way || {};
    setCount(this, "");
    prose(host, [
      { title: "Stillness", body: txt(w.stillness) },
      f[1].value === "full" ? { title: "Minimalism", body: txt(w.minimalism) } : null,
      f[1].value === "full" ? { title: "Etiquette", body: txt(w.etiquette) } : null
    ].filter(Boolean), { empty: "Nothing on the way today." });
  }
});

// 11 · THE WAY
section({
  id: "way", nav: "The Way", title: "The Way",
  needs: ["today"],
  lede: "The standing practices. Not advice — the things that are actually done.",
  filters: [
    { label: "Practice", accent: true, options: [
      { label: "Health", value: "health" }, { label: "Model", value: "model" },
      { label: "Minimalism", value: "minimalism" }, { label: "Etiquette", value: "etiquette" }] },
    readFilter()
  ],
  render: function (host) {
    var f = this.filters, w = desk().way || {};
    var body = txt(w[f[0].value]);
    setCount(this, body ? f[0].value : "");
    if (f[1].value === "short" && body.length > 400) body = body.slice(0, 400) + "…";
    prose(host, [{ title: f[0].value.charAt(0).toUpperCase() + f[0].value.slice(1), body: body }],
      { empty: "Nothing under this practice today." });
  }
});

// 12 · THE REVIEW
section({
  id: "review", nav: "The Review", title: "The Review",
  needs: ["today"],
  lede: "The day's operating tip, and the money habit underneath it.",
  filters: [
    { label: "Show", accent: true, options: [{ label: "Productivity", value: "prod" }, { label: "Money", value: "money" }] },
    readFilter()
  ],
  render: function (host) {
    var f = this.filters, D = desk();
    if (f[0].value === "prod") {
      setCount(this, D.productivity ? "today" : "");
      prose(host, [{ title: "Today's tip", body: txt(D.productivity) }], { empty: "No tip today." });
      return;
    }
    var m = D.money_hack || {};
    setCount(this, m.title ? "today" : "");
    var body = txt(m.body);
    if (f[1].value === "short" && body.length > 400) body = body.slice(0, 400) + "…";
    prose(host, [{ title: m.title, body: body }], { empty: "No money note today." });
  }
});

// 13 · THE DESK
section({
  id: "desk", nav: "The Desk", title: "The Desk",
  needs: ["today"],
  lede: "The speaking drill, and why it is the one being run.",
  filters: [
    { label: "Show", accent: true, options: [{ label: "The drill", value: "drill" }, { label: "Why", value: "why" }] },
    readFilter()
  ],
  render: function (host) {
    var f = this.filters, s = desk().speaking || {};
    setCount(this, s.title ? "today" : "");
    var body = txt(f[0].value === "drill" ? s.drill : s.why);
    if (f[1].value === "short" && body.length > 400) body = body.slice(0, 400) + "…";
    prose(host, [{ title: s.title, body: body }], { empty: "No drill today." });
  }
});

// 14 · CHESS
section({
  id: "chess", nav: "Chess", title: "Chess",
  needs: ["today"],
  lede: "The study focus of the day. Scored against a board rather than a feeling.",
  filters: [
    { label: "Show", accent: true, options: [{ label: "Study", value: "study" }] },
    readFilter()
  ],
  render: function (host) {
    var f = this.filters, c = desk().chess || {};
    setCount(this, c.index ? c.index + " of " + c.total : "");
    var body = txt(c.body);
    if (f[1].value === "short" && body.length > 400) body = body.slice(0, 400) + "…";
    prose(host, [{ title: c.title, body: body }], { empty: "No chess note today." });
    var a = el("a", null, "Open Lichess →");
    a.href = "https://lichess.org/@/AKK_010"; a.target = "_blank"; a.rel = "noopener";
    var p = el("p", "dnote"); p.appendChild(a); host.appendChild(p);
  }
});

// 15 · MIND GYM
section({
  id: "gym", nav: "Mind Gym", title: "Mind Gym",
  needs: ["today"],
  lede: "The reps that do not belong anywhere else: a mental model, and the vocabulary to say it with.",
  filters: [
    { label: "Rep", accent: true, options: [{ label: "Model", value: "model" }, { label: "Vocabulary", value: "vocab" }] },
    { label: "Detail", options: [{ label: "Summary", value: "sum" }, { label: "Everything", value: "all" }] }
  ],
  render: function (host) {
    var f = this.filters, D = desk();
    if (f[0].value === "model") {
      var w = D.way || {};
      setCount(this, w.model ? "today" : "");
      prose(host, [{ title: "Mental model", body: txt(w.model) }], { empty: "No model today." });
      return;
    }
    var items = D.vocab || [];
    if (f[1].value === "sum") items = items.slice(0, 1);
    setCount(this, items.length + " words");
    if (!items.length) { host.appendChild(el("p", "empty", "No vocabulary today.")); return; }
    var list = el("div", "rows");
    items.forEach(function (w) {
      list.appendChild(makeRow({
        open: f[1].value === "all",
        main: function (m) {
          m.appendChild(el("span", "rsym", txt(w.word)));
          m.appendChild(el("span", "rmeta", txt(w.say)));
        },
        nums: function (x) { x.appendChild(el("span", "rnum", clip(w.meaning, 36))); },
        detail: function (d) {
          d.appendChild(el("p", "dbody", txt(w.meaning)));
          if (w.use) d.appendChild(el("p", "dnote", "Use it: " + txt(w.use)));
        }
      }));
    });
    host.appendChild(list);
  }
});

// ══════════════════════════════════════════════════════════════════════
function masthead() {
  var D = (DATA.today || {}), dg = (D.desk || {}).daughter || {};
  var kr = document.getElementById("keyrow");
  kr.innerHTML = "";
  var grid = keyGrid([
    ["Rebuilt", txt(D.date_str || D.date) || "—", "every morning"],
    ["Sections", "15", "career, learning, practice, mind"],
    ["Her age", dg.months != null ? dg.months + "m " + (dg.days || 0) + "d" : "—", txt(dg.band) || ""],
    ["Roles tracked", DATA.jobs ? String((DATA.jobs.jobs || []).length) : "on demand", "Gulf and Malaysia"]
  ]);
  while (grid.firstChild) kr.appendChild(grid.firstChild);
  if (D.date_str) document.getElementById("stamp").textContent = "The fourth pillar · " + txt(D.date_str);
  document.getElementById("footstamp").textContent =
    "Rendered from the same daily build as the ledger at news.askakshay.com.";
}

SD.boot([["today", "/today.json"]], masthead);
})();
