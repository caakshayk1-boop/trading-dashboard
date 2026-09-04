#!/usr/bin/env python3
"""
test_engine_regressions.py — one test per defect that actually shipped.

Every case here is a real bug that reached production, published a false
number, and was found by hand. None of them were exotic; all of them were
invisible, which is the reason to pin them.

Deliberately dependency-free and offline: no network, no database, no pytest.
`python test_engine_regressions.py` is the whole contract, so it can run in any
workflow without an install step and cannot be skipped because a fixture broke.
"""
from __future__ import annotations

import math
import re
import sys
from datetime import date, datetime, timedelta, timezone

import pandas as pd

FAILURES: list[str] = []
PASSES = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"  PASS  {name}")
    else:
        FAILURES.append(f"{name} — {detail}")
        print(f"  FAIL  {name}  {detail}")


# ─────────────────────────────────────────────────────────────────────────────
# 1. NaN must score ZERO, never full marks.
#
# _band() clamps with min()/max(), and `nan < 1.0` is False — so min(1.0, nan)
# returns 1.0. A missing metric therefore scored a PERFECT component. Two
# unpriceable foreign symbols reached the top-5 ranking on ~95/100 and rendered
# as "$nan" in the published cards.
# ─────────────────────────────────────────────────────────────────────────────
def test_band_rejects_nan() -> None:
    print("\n[1] scoring — NaN must not earn a perfect band")
    import newspaper  # noqa: F401  (import cost is the point: it must stay importable)

    src = open("newspaper.py").read()
    m = re.search(r"def _band\(v, lo, hi\):(.*?)\n        ext20", src, re.S)
    if not m:
        check("_band located in newspaper.py", False, "function shape changed")
        return
    ns: dict = {"math": math}
    body = "def _band(v, lo, hi):" + m.group(1)
    exec(body.replace("\n        ", "\n    "), ns)
    band = ns["_band"]

    check("_band(nan) == 0.0", band(float("nan"), -2, 8) == 0.0,
          f"got {band(float('nan'), -2, 8)} — a missing metric is scoring full marks")
    check("_band(None) == 0.0", band(None, -2, 8) == 0.0)
    check("_band still grades normally", abs(band(3.0, -2, 8) - 0.5) < 1e-9,
          f"got {band(3.0, -2, 8)}, expected 0.5")
    check("_band clamps above hi", band(99.0, -2, 8) == 1.0)
    check("_band clamps below lo", band(-99.0, -2, 8) == 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# 2. The first target must pay back at least the risk.
#
# T1 was `levels[0]` — whatever structural level happened to be nearest — with
# no distance test, while T2 was properly gated. HINDALCO shipped a T1 0.80%
# above entry against a 4.31% stop: 0.19R. A target nobody would take, printed
# beside an R:R of 2.41 that was quoted off T2.
# ─────────────────────────────────────────────────────────────────────────────
def test_t1_pays_back_the_risk() -> None:
    print("\n[2] targets — T1 must return at least min_rr_t1")
    import cf_engine

    cfg = cf_engine.CONFIG
    check("min_rr_t1 exists and is >= 1.0", getattr(cfg, "min_rr_t1", 0) >= 1.0,
          f"min_rr_t1={getattr(cfg, 'min_rr_t1', None)}")

    # HINDALCO's real geometry on 2026-08-07.
    price, sl = 1059.6, 1013.892
    risk = price - sl
    levels = [1068.1278, 1169.897, 1240.0]      # the nearest one is 0.19R away

    t2 = next((lv for lv in levels if abs(lv - price) / risk >= cfg.min_rr), None)
    t1 = next((lv for lv in levels if abs(lv - price) / risk >= cfg.min_rr_t1), None)
    if t1 is None:
        t1 = price + cfg.min_rr_t1 * risk
    if t1 > t2:
        t1 = t2

    check("T1 is not the 0.19R level", abs(t1 - 1068.1278) > 1e-6,
          "T1 fell back to the nearest structural level regardless of distance")
    check("T1 >= 1R", (t1 - price) / risk >= cfg.min_rr_t1 - 1e-9,
          f"T1 pays {(t1 - price) / risk:.2f}R")
    check("T1 never beyond T2", t1 <= t2 + 1e-9,
          f"T1 {t1} sits past T2 {t2} — inverts the scale-out")


# ─────────────────────────────────────────────────────────────────────────────
# 3. The grading window must fail CLOSED.
#
# _since_entry() returned the ENTIRE fetched frame — up to 365 days — whenever
# it could not establish a cutoff. A pre-signal low then tripped the stop and
# the exit was booked at that old bar's open. HINDALCO, signalled 2026-08-07,
# was booked SL_HIT at 990.00: exactly the open of 2026-08-03.
# ─────────────────────────────────────────────────────────────────────────────
def test_since_entry_fails_closed() -> None:
    print("\n[3] grading window — no timestamp means no bars")
    src = open("standalone_scan.py").read()
    m = re.search(r"def _since_entry\(tick, opened_at\):.*?(?=\ndef )", src, re.S)
    if not m:
        check("_since_entry located", False, "function shape changed")
        return
    ns: dict = {"pd": pd}
    exec(m.group(0), ns)
    since = ns["_since_entry"]

    idx = pd.to_datetime(["2026-08-03", "2026-08-04", "2026-08-05",
                          "2026-08-06", "2026-08-07"]).tz_localize("Asia/Kolkata")
    df = pd.DataFrame({"Open": [990.0, 995.0, 1028.5, 1044.0, 1017.0],
                       "Low": [981.35, 993.1, 1011.3, 1016.0, 1017.0]}, index=idx)
    opened = pd.Timestamp("2026-08-07 17:06:58", tz="Asia/Kolkata")

    check("post-entry slice excludes pre-signal bars",
          990.0 not in list(since(df, opened)["Open"]),
          "the 2026-08-03 open is still reachable — this is the 990.00 bug")
    check("no timestamp -> zero bars", len(since(df, None)) == 0,
          f"returned {len(since(df, None))} bars; unbounded windows fabricate stop-outs")
    check("tz-naive index -> bounded, not dropped",
          len(since(df.tz_localize(None), opened)) == 0,
          "a naive index must still be bounded by date")


# ─────────────────────────────────────────────────────────────────────────────
# 4. A stop-out may not fill arbitrarily far through the stop.
#
# The gap model took the open of the first bar whose low breached the stop.
# With an unbounded window that bar could predate the signal — BALKRISIND was
# booked 14.29% through its stop at a price that never traded afterwards.
# ─────────────────────────────────────────────────────────────────────────────
def test_gap_slip_is_bounded() -> None:
    print("\n[4] fills — an implausible gap books the stop")
    import standalone_scan

    cap = getattr(standalone_scan, "MAX_GAP_SLIP_PCT", None)
    check("MAX_GAP_SLIP_PCT exists", cap is not None)
    if cap is None:
        return
    check("cap is a plausible overnight gap", 1.0 <= cap <= 8.0,
          f"MAX_GAP_SLIP_PCT={cap} — outside the range a real gap occupies")

    sl, exit_p = 2389.33, 2047.8            # BALKRISIND as recorded
    slip = (sl - exit_p) / sl * 100
    check("BALKRISIND's 14.29% slip would be rejected", slip > cap,
          f"slip {slip:.2f}% <= cap {cap}% — it would still be booked")

    check("a 2% gap is still allowed through",
          (2389.33 - 2341.5) / 2389.33 * 100 < cap,
          "the cap is tight enough to reject real gaps")


# ─────────────────────────────────────────────────────────────────────────────
# 5. closed_at must be the resolving bar, not the clock.
#
# It recorded datetime.now(), i.e. when the grader happened to run. 25 trades
# were stamped closed on a Saturday with the exchange shut.
# ─────────────────────────────────────────────────────────────────────────────
def test_closed_at_is_a_bar_date() -> None:
    print("\n[5] close dates — the bar, not the clock")
    src = open("tracker.py").read()
    m = re.search(r"UPDATE all_signals SET status=\?,exit_price=\?.*?WHERE id=\?\",\s*\((.*?)\)\s*\n",
                  src, re.S)
    check("closed_at no longer written from datetime.now() alone",
          m is not None and "exit_day" in m.group(1),
          "the update still passes a wall-clock timestamp for closed_at")

    check("exit_day is captured from the resolving bar",
          "exit_day = bar_day.isoformat()" in src,
          "nothing records which bar resolved the trade")

    # A trade cannot close on a day the exchange never opened.
    sat = date(2026, 8, 8)
    check("2026-08-08 is a Saturday (the date in the bad rows)", sat.weekday() == 5)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Excursions must be signed correctly.
#
# MFE is favourable movement (>= 0), MAE adverse (<= 0). Getting the sign wrong
# would invert the entire stop-vs-selection conclusion.
# ─────────────────────────────────────────────────────────────────────────────
def test_excursion_signs() -> None:
    print("\n[6] excursions — MFE positive, MAE negative")
    entry, sl = 100.0, 90.0
    risk = entry - sl
    hi, lo = 118.0, 95.0                      # long: ran to +1.8R, dipped to -0.5R
    mfe = (hi - entry) / risk
    mae = -(entry - lo) / risk
    check("MFE positive for a long that ran up", abs(mfe - 1.8) < 1e-9, f"{mfe}")
    check("MAE negative for a long that dipped", abs(mae + 0.5) < 1e-9, f"{mae}")

    # short: entry 100, stop 110, price fell to 82 and spiked to 104
    entry, sl, lo, hi = 100.0, 110.0, 82.0, 104.0
    risk = sl - entry
    mfe_s = (entry - lo) / risk
    mae_s = -(hi - entry) / risk
    check("MFE positive for a short that fell", abs(mfe_s - 1.8) < 1e-9, f"{mfe_s}")
    check("MAE negative for a short that spiked", abs(mae_s + 0.4) < 1e-9, f"{mae_s}")


# ─────────────────────────────────────────────────────────────────────────────
# 7. The page script must stay free of template syntax.
#
# app.js was extracted out of a Jinja template. A stray {{ }} would ship a
# syntax error to every visitor, and the failure mode is the whole script block
# aborting — ticker, world map and scroll spy all dead at once.
# ─────────────────────────────────────────────────────────────────────────────
def test_app_js_is_not_a_template() -> None:
    print("\n[7] page script — no template syntax, no inline copy")
    js = open("static/app.js").read()
    check("app.js contains no Jinja expression", "{{" not in js,
          "a template tag would be a syntax error in the browser")
    check("app.js contains no Jinja block", "{%" not in js)
    check("app.js is substantial", js.count("\n") > 3000,
          f"only {js.count(chr(10))} lines — did the extraction truncate?")

    page = open("newspaper.py").read()
    check("template no longer inlines the script",
          "var TV_ALIASES = {{ tv_aliases|tojson }};" not in page,
          "the inline copy is back; two copies will drift")
    check("template references the external file", 'src="/app.js' in page)
    check("template still ships the data island", 'id="tv-aliases"' in page,
          "app.js reads TV_ALIASES from this block; without it charts lose their links")


# ─────────────────────────────────────────────────────────────────────────────
# 8. The BUY band must not reach back into the bleed zone.
#
# Unlike every case above, this is not a coding defect — it is a selection
# defect, and the only one in the engine with statistical support. Buying at
# 4H RSI >= 65 returned -0.599R over 90 cf_1h trades (-5.6 SE) while the same
# engine below 65 returned +0.274R. The band allowed 75.
#
# Pinned because a band is one number in a dataclass: it is the easiest thing
# in this repo to widen "just to get more signals", and doing so silently
# reintroduces the single largest measured loss source in the ledger.
# ─────────────────────────────────────────────────────────────────────────────
def test_buy_band_excludes_the_bleed_zone() -> None:
    print("\n[8] selection — BUY must not chase 4H RSI into the bleed zone")
    import cf_engine

    cfg = cf_engine.CONFIG
    lo, hi = cfg.rsi_4h_buy
    check("BUY ceiling <= 65", hi <= 65.0,
          f"rsi_4h_buy={cfg.rsi_4h_buy} — 65+ measured -0.599R over 90 trades")
    check("BUY floor still 55", lo == 55.0,
          f"floor moved to {lo}; 55-60 is the best measured band (+0.432R)")
    check("band is non-empty", lo < hi)

    # The scorer must not award peak conviction at the edge that bleeds.
    s_at_edge = cf_engine._score(3.0, 1.5, hi, "BUY", "structure", cfg)
    s_inside = cf_engine._score(3.0, 1.5, 58.0, "BUY", "structure", cfg)
    check("conviction peaks inside the band, not at the ceiling",
          s_inside > s_at_edge,
          f"score at 58 = {s_inside}, at ceiling {hi} = {s_at_edge}")

    # A setup above the ceiling must be rejected outright, not merely scored low.
    check("rsi above ceiling is out of band", not (lo <= 70.0 <= hi),
          "70 still satisfies the buy band")


# ─────────────────────────────────────────────────────────────────────────────
# 9. Engine-log copy must survive an UNESCAPED render.
#
# generate.py builds the static site with jinja2.Template(), which defaults to
# autoescape=False; the Flask path escapes. So a literal "<" in copy renders
# correctly on localhost and silently truncates in the published page — the
# browser swallows "< 65</th><td>" as a tag. "BUY, 4H RSI < 65" shipped as
# "BUY, 4H RSI" with its whole row of numbers gone.
#
# Pinned rather than fixed at the renderer: turning autoescape on in generate.py
# is the real repair, but it changes escaping for every string on the site at
# once and needs its own verification pass.
# ─────────────────────────────────────────────────────────────────────────────
def test_engine_log_copy_is_markup_safe() -> None:
    print("\n[9] engine log — copy must not contain raw markup characters")
    import newspaper

    changes = newspaper.ENGINE_CHANGES
    check("engine log is non-empty", len(changes) > 0)

    bad: list[str] = []
    for c in changes:
        fields = [c.get("title", ""), c.get("body", ""), c.get("note", ""),
                  c.get("tag", ""), c.get("verdict", ""), c.get("date", "")]
        for label, val, n, sig in c.get("evidence", []):
            fields += [label, val, n, sig]
        for f_ in fields:
            if "<" in f_ or ">" in f_ or "&" in f_:
                bad.append(f"{c.get('date')}: {f_!r}")
    check("no raw < > or & in any entry", not bad,
          f"{len(bad)} field(s) would corrupt the built page: {bad[:3]}")

    # Every entry needs a verdict the stylesheet actually styles.
    #
    # Read from the stylesheet rather than hardcoded. The literal set said
    # {"adopted", "rejected"}, so adding a styled third verdict still failed —
    # the test was asserting its own copy of the answer instead of the thing it
    # was meant to check, and the fix for a real miss looked identical to the
    # fix for a stale test.
    styled = set(re.findall(r"\.elog-v\.([a-z-]+)\{", newspaper.TEMPLATE))
    assert styled, "no .elog-v verdict classes found in the stylesheet at all"
    unknown = [c["verdict"] for c in changes if c.get("verdict") not in styled]
    check("every verdict has a matching CSS class", not unknown,
          f"unstyled verdicts: {unknown}")

    # A log that never records a negative result is marketing, not a record.
    check("at least one rejected entry is published",
          any(c.get("verdict") == "rejected" for c in changes),
          "only adopted changes are listed; the rejected tests are the credible part")


# ─────────────────────────────────────────────────────────────────────────────
# 10. A weekly cache must not blank its section every Monday.
#
# The fund screen is cached under _week_key(), which is an ISO week PLUS
# PICKS_ENGINE. Both parts expire it for reasons that have nothing to do with
# fund data: the week rolls over every Monday, and bumping the picks engine —
# a different feature entirely — orphaned the cache as a side effect. On
# 2026-08-10 the Fund Screen was simply absent from the published page.
#
# The fallback must be bounded by the DATA's age, not by the key, and an
# unreadable timestamp must hide the section rather than publish rankings of
# unknown vintage under today's date.
# ─────────────────────────────────────────────────────────────────────────────
def test_fund_cache_survives_week_rollover() -> None:
    print("\n[10] fund screen — a week rollover must not blank the section")
    import newspaper

    cap = getattr(newspaper, "MAX_FUND_CACHE_AGE_DAYS", None)
    check("MAX_FUND_CACHE_AGE_DAYS exists", cap is not None)
    check("cap spans at least one rebuild cycle", cap is not None and 8 <= cap <= 31,
          f"cap={cap} — under 8 re-creates the Monday hole, over 31 is not a weekly screen")

    age = newspaper._payload_age_days
    now = datetime.now(timezone.utc)
    check("fresh payload reads ~0 days",
          abs(age({"generated_at": now.isoformat()})) < 0.01)
    check("8-day payload reads 8 days",
          abs(age({"generated_at": (now - timedelta(days=8)).isoformat()}) - 8) < 0.01)
    check("Z-suffixed timestamp parses",
          age({"generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}) is not None,
          "funds.build() writes isoformat()+'Z'; it must not read as unknown")
    # None means unknown, and unknown must never be treated as fresh.
    check("missing timestamp is unknown, not zero", age({}) is None)
    check("unparseable timestamp is unknown, not zero",
          age({"generated_at": "not-a-date"}) is None)

    # The nav and the section have to agree about whether funds exist.
    check("empty fund screen drops 'funds' from the nav",
          "funds" in newspaper.empty_sections({}),
          "nav would advertise a section the page does not render")
    check("populated fund screen keeps 'funds'",
          "funds" not in newspaper.empty_sections({"categories": [{"name": "Flexi Cap"}]}))


import pathlib


def _js_imports(path):
    """Real import statements only — the string "./_db.js" also appears inside
    comments explaining why NOT to import it."""
    import re as _re
    return _re.findall(r'^\s*import\s[^;]*?from\s+"([^"]+)"',
                       path.read_text(encoding="utf-8"), _re.M)


def check_node_tests_pull_no_packages():
    """newspaper.yml runs `cd vercel-news && node --test` with NO npm install.

    Anything reachable from a test file must therefore import zero packages.
    This has broken the build twice: bdca63b (which is why _badge.js exists at
    all) and again on 2026-08-18 when distinctTargets was put in _db.js. Both
    times the symptom was ERR_MODULE_NOT_FOUND on @libsql/client, and both
    times every test passed locally, because node_modules exists locally.

    Walks the import graph from each test file and fails on any bare specifier
    that is not a node: builtin.
    """
    root = pathlib.Path("vercel-news")
    if not root.is_dir():
        return []
    problems, seen = [], set()

    def walk(f, chain):
        if f in seen or not f.is_file():
            return
        seen.add(f)
        for spec in _js_imports(f):
            if spec.startswith("node:"):
                continue
            if spec.startswith("."):
                walk((f.parent / spec).resolve(), chain + [f.name])
            else:
                problems.append(f"{' -> '.join(chain + [f.name])} imports package {spec!r}")

    for t in sorted((root / "test").glob("*.test.js")):
        walk(t.resolve(), [])
    return problems


def check_injected_rv_is_revealed():
    """Every function that injects .rv markup must call reveal() on it.

    The scroll-reveal observer registers ONCE, at load, over the .rv elements
    that exist then. Anything injected afterwards was never observed and stays
    at opacity:0 permanently — the section renders, the data is correct, and
    the reader sees nothing.

    renderPaperWallet shipped exactly this on 2026-08-19: the KPI row, rules
    list and trades table were all invisible, and only the tier bars showed
    because they were the one part without .rv. Nothing failed, nothing logged.
    """
    import re as _re
    js = open("static/app.js", encoding="utf-8").read()
    problems = []
    # Each `function name(...){ ... }` at two-space indent, non-greedy to the
    # matching two-space closing brace.
    for m in _re.finditer(r"\n    function (\w+)\(([^)]*)\)\s*\{(.*?)\n    \}", js, _re.S):
        name, body = m.group(1), m.group(3)
        if name == "reveal":
            continue
        injects_rv = _re.search(r"innerHTML\s*=", body) and "rv\"" in body.replace("'", '"')
        if injects_rv and "reveal(" not in body:
            problems.append(name)
    return problems


def main() -> int:
    print("engine regressions — every case here shipped to production once\n")
    for fn in (test_band_rejects_nan,
               test_t1_pays_back_the_risk,
               test_since_entry_fails_closed,
               test_gap_slip_is_bounded,
               test_closed_at_is_a_bar_date,
               test_excursion_signs,
               test_app_js_is_not_a_template,
               test_buy_band_excludes_the_bleed_zone,
               test_engine_log_copy_is_markup_safe,
               test_fund_cache_survives_week_rollover,
               test_engines_drop_the_partial_bar,
               test_every_logging_scan_also_delivers,
               test_alert_table_columns_match,
               test_docs_files_have_all_four_allow_lists,
               test_scan_crons_match_their_slot_arms,
               test_non_trading_types_agree_across_python_and_js):
        try:
            fn()
        except Exception as e:                       # noqa: BLE001
            FAILURES.append(f"{fn.__name__} raised {type(e).__name__}: {e}")
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")

    print(f"\n{PASSES} passed · {len(FAILURES)} failed")
    for f in FAILURES:
        print(f"  · {f}")
    return 1 if FAILURES else 0



# ─────────────────────────────────────────────────────────────────────────────
# The partial last bar that turned three engines off without saying so.
#
# Yahoo appends a row for the session in progress: a real Volume with NaN
# Open/High/Low/Close. Every per-symbol engine reads `.iloc[-1]`, so `close`
# came back nan, `np.isnan(close)` was True, and the function returned None —
# for every symbol in the universe, on any day that row was present. The only
# trace in the log was "Breakouts: 0 to alert (0 raw)", which reads exactly
# like a quiet market.
#
# Measured 2026-09-04: the 2026-09-03 row was NaN-priced across the whole of
# NSE, 28 of 80 sampled names had a live pattern, and none reached the ledger.
#
# _own_frame() has fixed this since it was written — and was wired into the two
# magic analyzers and nowhere else. This test is about the WIRING, not the
# helper: it asserts every per-symbol engine passes its downloaded frame
# through it, because the failure is silent and a review will not catch it.
# ─────────────────────────────────────────────────────────────────────────────
def test_engines_drop_the_partial_bar() -> None:
    print("\n[16] a NaN-priced last bar must not silently empty an engine")
    src = open("scanner.py").read()

    for fn in ("analyze_breakout", "analyze_ohl", "_analyze_multibagger"):
        m = re.search(rf"def {re.escape(fn)}\(.*?(?=\ndef )", src, re.S)
        if not m:
            check(f"{fn} located", False, "function renamed or removed")
            continue
        body = m.group(0)
        check(f"{fn} passes its frame through _own_frame",
              "_own_frame(" in body,
              "reads .iloc[-1] off a raw yfinance frame — a partial bar returns "
              "None for the whole universe and logs nothing")

    # The helper itself must keep doing the two things the wiring relies on.
    ns: dict = {}
    m = re.search(r"def _own_frame\(.*?(?=\ndef )", src, re.S)
    check("_own_frame still exists", bool(m))
    if not m:
        return
    import pandas as pd
    exec("import pandas as pd, logging\n" + m.group(0), ns)
    own = ns["_own_frame"]

    df = pd.DataFrame({"Open": [1.0, 2.0, float("nan")],
                       "High": [1.0, 2.0, float("nan")],
                       "Low": [1.0, 2.0, float("nan")],
                       "Close": [1.0, 2.0, float("nan")],
                       "Volume": [10, 20, 30]},
                      index=pd.to_datetime(["2026-09-01", "2026-09-02", "2026-09-03"]))
    out = own(df, "X.NS")
    check("_own_frame drops the NaN-priced bar", out is not None and len(out) == 2,
          f"got {None if out is None else len(out)} rows")
    check("_own_frame leaves a usable last close",
          out is not None and float(out["Close"].iloc[-1]) == 2.0)

    # A frame that is ALL partial must come back as nothing, not as an empty
    # frame something downstream will index into.
    allnan = df.iloc[[2]]
    check("_own_frame returns None when every bar is partial",
          own(allnan, "X.NS") is None)



# ─────────────────────────────────────────────────────────────────────────────
# An engine that logs but never sends.
#
# run_momentum_scan ranked 750 names, sized them, wrote them to the ledger and
# stopped. It imported mark_alerts_sent at the top of the function and never
# called it — the fingerprint of a send block that was intended and never
# written. A name could be ranked, sized, logged and rendered on the site with
# nobody told, and every log line said the scan had succeeded, because it had.
#
# This asserts the SHAPE every scan in that file shares: if a runner logs
# signals to the ledger, it must also hand the result to _record_delivery. The
# test is about the pairing, not about Telegram — an engine may legitimately
# alert nothing, but it may not silently decide not to try.
# ─────────────────────────────────────────────────────────────────────────────
def test_every_logging_scan_also_delivers() -> None:
    print("\n[17] a scan that writes to the ledger must also record delivery")
    src = open("standalone_scan.py").read()

    runners = re.findall(r"\ndef (run_\w*scan)\(.*?(?=\ndef )", src, re.S)
    checked = 0
    for name in re.findall(r"\ndef (run_\w+)\(", src):
        m = re.search(rf"\ndef {re.escape(name)}\(.*?(?=\ndef )", src, re.S)
        if not m:
            continue
        body = m.group(0)
        logs = "log_batch_to_all_signals(" in body or "log_breakouts(" in body
        if not logs:
            continue
        checked += 1
        # Either route counts. _record_delivery is the shared helper, but
        # run_swing_scan calls mark_alerts_sent directly with three grouped
        # outcome lists — delivered, below-threshold, failed — which is the
        # same invariant expressed differently, and asserting the helper by
        # name would have flagged working code and taught nobody anything.
        records = "_record_delivery(" in body or "mark_alerts_sent(" in body
        check(f"{name} records what was delivered", records,
              "writes signals to the ledger and never records whether anyone "
              "was told — the ledger fills while the bot stays silent")

    check("found scans that write to the ledger", checked >= 4, f"checked {checked}")

    # And the specific one this test was written for must carry a real send.
    m = re.search(r"\ndef run_momentum_scan\(.*?(?=\ndef )", src, re.S)
    check("run_momentum_scan located", bool(m))
    if m:
        check("momentum sends its picks", "_send_chunked(" in m.group(0))
        # Underscores in engine names and component keys have broken Telegram
        # Markdown on this bot before. Nothing interpolated inside an italic.
        italics = re.findall(r"_[^_\n]*\{[^}]*\}[^_\n]*_", m.group(0))
        check("no interpolation inside a Telegram italic span",
              not italics, str(italics[:2]))



def test_alert_table_columns_match():
    """The SSR <thead>, the SSR rows and the live renderer must agree.

    The live layer replaces the tbody and never the thead, so a column added
    to one and not the others shifts every cell after it under the wrong
    heading — which is exactly what adding "Last" did on the first pass.
    """
    import re
    tpl = open("newspaper.py", encoding="utf-8").read()
    js = open("static/app.js", encoding="utf-8").read()

    head = re.search(r'<table class="t" id="alertTable">\s*<thead><tr>(.*?)</tr></thead>',
                     tpl, re.S)
    check("alert table thead found in the template", head is not None)
    n_head = head.group(1).count("<th ")

    row = re.search(r'\{% for a in alerts\[:20\] %\}(.*?)\{% endfor %\}', tpl, re.S)
    check("server-rendered alert row found", row is not None)
    n_row = len(re.findall(r'<td[ >]', row.group(1)))

    # `shown`, not `rows`: renderAlerts caps how many rows reach the DOM
    # (ALERT_RENDER_CAP) and maps the capped slice. Matching either name keeps
    # this from breaking on a rename that changes no columns.
    live = re.search(r"var html = (?:rows|shown)\.map\(function\(a\)\{(.*?)\}\)\.join", js, re.S)
    check("live alert row renderer found", live is not None)
    if live is None:
        return          # already reported; groups below would raise
    body = live.group(1)
    n_live = body.count("'<td") + body.count("pnlCell(a)")

    check("alert table columns agree across all three renderers",
          n_head == n_row == n_live,
          f"thead={n_head} ssr={n_row} live={n_live}")

    # Any full-width row in that tbody must span the whole table. The empty
    # state sat at colspan=15 on a 17-column table, so its message rendered
    # short of the right-hand edge.
    # Scoped to renderAlerts. app.js has other tables with other column counts
    # — the stock screen's empty state is legitimately 18 — and checking every
    # colspan in the file against the ALERT table's width fails on unrelated,
    # correct code.
    ra = re.search(r"function renderAlerts\(\)\{(.*?)\n    \}", js, re.S)
    check("renderAlerts found", ra is not None)
    if ra:
        for span in re.findall(r'colspan="(\d+)"', ra.group(1)):
            check(f"full-width alert row spans all {n_head} columns",
                  int(span) == n_head, f"found colspan={span}")


def test_docs_files_have_all_four_allow_lists():
    """Every docs/ artefact must be named in ALL FOUR places or it rots.

    A file under docs/ reaches production only if it is (1) written by
    generate.py, (2) `git add`ed by name in newspaper.yml, (3) named in
    .vercelignore and (4) copied by name in vercel-news/build.js. Miss any one
    and the build log still says the file was written, so it reads as a
    publishing problem rather than a missing filename.

    This has now bitten twice in opposite directions. today.json had 1+3+4 and
    404'd outright. screen.json/screen-detail.json had 1+3+4 and served STALE
    data instead — the runner rebuilt them, the rebase autostash restored them,
    and the runner was destroyed with the fresh file uncommitted, so production
    served whatever a human last committed by hand: a payload built 2026-08-12,
    predating the Piotroski field. The F-Score column was therefore populated
    in the server-rendered top 25 and empty across all 750 rows the instant the
    table lazy-loaded. Nothing errored anywhere.
    """
    import re
    gen = open("generate.py", encoding="utf-8").read()
    wf = open(".github/workflows/newspaper.yml", encoding="utf-8").read()
    vi = open(".vercelignore", encoding="utf-8").read()
    bj = open("vercel-news/build.js", encoding="utf-8").read()

    # What generate.py actually writes into the output directory.
    # index.html/desk.html go through a different writer, so anchor the sanity
    # check on the lazy-loaded JSON this test exists to protect.
    written = set(re.findall(r'out_dir\s*/\s*"([^"]+)"', gen))
    check("generate.py writes the lazy-loaded screen artefacts",
          {"screen.json", "screen-detail.json"} <= written,
          f"found {sorted(written)}")

    # The git add list is one shell command spanning escaped newlines.
    add = re.search(r"git add (.*?)\n\s*git diff", wf, re.S)
    check("newspaper.yml git add block found", add is not None)
    added = set(re.findall(r"docs/([^\s\\]+)", add.group(1)))

    missing = []
    for name in sorted(written):
        where = []
        if name not in added:
            where.append("newspaper.yml git add")
        if f"!docs/{name}" not in vi:
            where.append(".vercelignore")
        if f'"{name}"' not in bj:
            where.append("build.js")
        if where:
            missing.append(f"{name} -> missing from {', '.join(where)}")

    check("every file generate.py writes is in all four allow-lists",
          not missing, "; ".join(missing))


def test_scan_crons_match_their_slot_arms():
    """Every `cron:` in daily_scan.yml must have a matching case arm.

    The schedule is stored TWICE — once as `cron:` and once as a shell `case`
    on github.event.schedule — because the slot has to come from the cron that
    fired, not the wall clock (drift of 1.5-3h puts every run outside its own
    IST window). That duplication is the trap: moving a cron without moving its
    arm does not fail the workflow, it falls through to SLOT="" and quietly
    runs the clock-based fallback — the wrong scan, or none.

    Also asserts the actionable weekday scan is early enough to be tradeable:
    NSE closes at 15:30 IST, so a cron plus a full 3h drift must still land
    with at least an hour of session left.
    """
    import re
    wf = open(".github/workflows/daily_scan.yml", encoding="utf-8").read()

    crons = re.findall(r"^\s*-\s*cron:\s*'([^']+)'", wf, re.M)
    check("daily_scan.yml declares crons", bool(crons), f"found {crons}")

    case_block = re.search(r"case \"\$SCHEDULE\" in(.*?)esac", wf, re.S)
    check("slot case block found", case_block is not None)
    arms = re.findall(r'"([^"]+)"\)\s*SLOT=', case_block.group(1))

    missing = [c for c in crons if c not in arms]
    check("every cron has a slot arm", not missing,
          f"crons with no arm: {missing} (arms present: {arms})")

    orphan = [a for a in arms if a not in crons]
    check("no slot arm points at a removed cron", not orphan, f"orphans: {orphan}")

    # Tradeable-window check on the weekday intraday scan.
    CLOSE_IST_MIN = 15 * 60 + 30
    MAX_DRIFT_MIN = 3 * 60
    for c in crons:
        parts = c.split()
        if len(parts) != 5 or parts[4] != "1-5":
            continue                       # weekend / EOD slots are exempt
        ist = int(parts[1]) * 60 + int(parts[0]) + 330      # UTC -> IST
        if ist >= CLOSE_IST_MIN:
            continue                       # after the close by design (EOD)
        check(f"weekday cron '{c}' survives 3h drift with time to execute",
              ist + MAX_DRIFT_MIN <= CLOSE_IST_MIN - 60,
              f"lands {(ist + MAX_DRIFT_MIN) // 60:02d}:{(ist + MAX_DRIFT_MIN) % 60:02d} IST "
              f"worst case, close is 15:30")


def test_non_trading_types_agree_across_python_and_js():
    """tracker.EXCLUDE_FROM_EXPECTANCY and stats.js NON_TRADING must match.

    Two independent lists decide which signal types are research/allocation
    artefacts rather than trades. A type named in one and not the other means
    the site and the Telegram bot publish DIFFERENT track records from the same
    ledger — and the failure is silent, because both numbers look plausible.

    A SIP allocation has no stop and no target; if it reaches the win rate it
    counts as a trade that always wins by a hair.
    """
    import re
    js = open("vercel-news/api/stats.js", encoding="utf-8").read()
    m = re.search(r"const NON_TRADING = \[(.*?)\];", js, re.S)
    check("stats.js NON_TRADING found", m is not None)
    js_types = set(re.findall(r'"([^"]+)"', m.group(1)))

    src = open("tracker.py", encoding="utf-8").read()
    pm = re.search(r"EXCLUDE_FROM_EXPECTANCY = \((.*?)\)", src, re.S)
    check("tracker EXCLUDE_FROM_EXPECTANCY found", pm is not None)
    # Constants, not literals — resolve them from their own assignments.
    py_types = set()
    for name in re.findall(r"\b([A-Z_]+)\b", pm.group(1)):
        cm = re.search(rf'^{name} = "([^"]+)"', src, re.M)
        if cm:
            py_types.add(cm.group(1))
    py_types |= set(re.findall(r'"([^"]+)"', pm.group(1)))

    # stats.js legitimately also excludes screener types the Python tuple does
    # not name, so the assertion is one-directional: everything Python calls
    # non-trading must ALSO be excluded by the API.
    missing = py_types - js_types
    check("every Python non-trading type is excluded by stats.js",
          not missing,
          f"in tracker but not stats.js: {sorted(missing)}")

    # newspaper.yml runs the node tests with NO npm install. Anything a test
    # file reaches must import zero packages. This broke the build twice.
    rv_problems = check_injected_rv_is_revealed()
    check("every function injecting .rv markup calls reveal()",
          not rv_problems,
          f"injected but never revealed: {rv_problems}")

    pkg_problems = check_node_tests_pull_no_packages()
    check("no vercel-news test reaches a package import",
          not pkg_problems, "; ".join(pkg_problems))


if __name__ == "__main__":
    raise SystemExit(main())
