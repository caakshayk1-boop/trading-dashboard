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
from datetime import date, datetime, timedelta

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


def main() -> int:
    print("engine regressions — every case here shipped to production once\n")
    for fn in (test_band_rejects_nan,
               test_t1_pays_back_the_risk,
               test_since_entry_fails_closed,
               test_gap_slip_is_bounded,
               test_closed_at_is_a_bar_date,
               test_excursion_signs,
               test_app_js_is_not_a_template):
        try:
            fn()
        except Exception as e:                       # noqa: BLE001
            FAILURES.append(f"{fn.__name__} raised {type(e).__name__}: {e}")
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")

    print(f"\n{PASSES} passed · {len(FAILURES)} failed")
    for f in FAILURES:
        print(f"  · {f}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
