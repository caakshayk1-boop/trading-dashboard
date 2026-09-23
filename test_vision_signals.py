#!/usr/bin/env python3
"""
test_vision_signals.py — the two Vision rules, their levels and their record.

The rules live in scanner.py (vision_bottom_reversal, vision_4h_breakout,
vision_levels, vision_grade); vision_scan.py fetches, files and grades. Every
bar here is synthetic and dated RELATIVE TO TODAY, never written down, so no
fixture ages out of a horizon a fortnight after it was written.

What is pinned:
  * each rule fires on a series built to meet it, and each condition, taken
    away alone, stops it firing
  * a bar the session can still change is never evaluated
  * an unmeasured input never fires a signal
  * stop < entry < T1 < T2 < T3, T1 repays at least 1.6R, stop within 6%
  * grading books the stop on a bar that touches both — never the flattering order
  * a filing's levels do not move while it is open, and the same bar is never
    filed twice
  * the feed is written outside docs/, and an empty fetch never overwrites it

Offline. No network, no pytest.

Usage:
    python3 test_vision_signals.py
"""
from __future__ import annotations

import json
import math
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
for k in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "GROQ_API_KEY"):
    os.environ.setdefault(k, "placeholder-not-a-secret")

import scanner                                   # noqa: E402
import vision_scan                               # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
CHECKS: list[tuple[str, callable]] = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


# ── Fixtures ────────────────────────────────────────────────────────────────

def _weekdays_back(n: int, end: datetime) -> list[datetime]:
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return days[::-1]


def _last_weekday() -> datetime:
    d = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def daily(closes, vol=1_000_000, end=None):
    """Daily bars at IST midnight, the way yfinance stamps NSE days."""
    end = end or _last_weekday()
    days = _weekdays_back(len(closes), end)
    rows, prev = [], closes[0]
    for d, c in zip(days, closes):
        o = prev
        rows.append((d.timestamp(), o, max(o, c) * 1.01, min(o, c) * 0.99, c, vol))
        prev = c
    return rows


def after_close(rows):
    """A clock that has seen the last bar close."""
    return datetime.fromtimestamp(rows[-1][0], IST).replace(hour=18)


def bottom_closes(end_price=124.0):
    """Down from 200 to 100 over 240 sessions, then up to `end_price` over 60:
    above the 50-day, still well below the 200-day, 24% off the low."""
    down = [200 - 100 * i / 239 for i in range(240)]
    up = [100 + (end_price - 100) * (i + 1) / 60 for i in range(60)]
    return down + up


def hourly_from_4h(candles, end=None):
    """Hourly bars that aggregate to exactly the given 4H (o, h, l, c, v)
    candles under NSE's 09:15–13:15 / 13:15–15:30 split."""
    end = end or _last_weekday()
    days = _weekdays_back(math.ceil(len(candles) / 2), end)
    slots = [((9, 15), (10, 15), (11, 15), (12, 15)), ((13, 15), (14, 15), (15, 15))]
    rows, i = [], 0
    for d in days:
        for half in slots:
            if i >= len(candles):
                break
            o, h, l, c, v = candles[i]
            n = len(half)
            for j, (hh, mm) in enumerate(half):
                ts = d.replace(hour=hh, minute=mm).timestamp()
                if j == 0:
                    rows.append((ts, o, h, l, o if n > 1 else c, v / n))
                elif j == n - 1:
                    rows.append((ts, o, max(o, c), min(o, c), c, v / n))
                else:
                    rows.append((ts, o, o, o, o, v / n))
            i += 1
    return rows


def range_candles(n=40, lo=98.0, hi=102.0, vol=400_000):
    """A quiet range, alternating up and down candles."""
    out = []
    for i in range(n):
        o, c = (lo + 1, hi - 1) if i % 2 == 0 else (hi - 1, lo + 1)
        out.append((o, hi if i % 5 == 0 else hi - 0.5, lo if i % 5 == 3 else lo + 0.5, c, vol))
    return out


BREAKOUT = (101.0, 106.2, 100.8, 106.0, 1_000_000)     # body 93%, top 4%, vol 2.5×


def after_4h(rows):
    return datetime.fromtimestamp(rows[-1][0], IST).replace(hour=16)


# ── Bottom reversal ─────────────────────────────────────────────────────────

@check("bottom: fires on a series built to meet all four conditions")
def _():
    rows = daily(bottom_closes())
    sig = scanner.vision_bottom_reversal(rows, after_close(rows))
    assert sig, "the constructed bottom did not fire"
    assert sig["engine"] == "bottom"
    c = sig["ctx"]
    assert c["up_from_low"] >= 15 and sig["entry"] > c["sma50"] and sig["entry"] < c["sma200"] and c["wrsi"] > 48, c


@check("bottom: less than 15% off the 52-week low does not fire")
def _():
    rows = daily(bottom_closes(end_price=112.0))
    assert scanner.vision_bottom_reversal(rows, after_close(rows)) is None


@check("bottom: a close ABOVE the 200-day does not fire (that is not a bottom)")
def _():
    rows = daily([100 + i * 0.3 for i in range(300)])
    assert scanner.vision_bottom_reversal(rows, after_close(rows)) is None


@check("bottom: a close BELOW the 50-day does not fire")
def _():
    cl = bottom_closes()
    cl = cl[:-3] + [cl[-4] * 0.93] * 3              # a late slump through the 50-day
    rows = daily(cl)
    sig = scanner.vision_bottom_reversal(rows, after_close(rows))
    assert sig is None, sig and sig["ctx"]


@check("bottom: weekly RSI at or below 48 does not fire")
def _():
    rows = daily(bottom_closes())
    real = scanner.rsi
    try:
        import pandas as pd
        scanner.rsi = lambda s, n=14: pd.Series([48.0] * len(s), index=s.index)
        assert scanner.vision_bottom_reversal(rows, after_close(rows)) is None
        scanner.rsi = lambda s, n=14: pd.Series([48.1] * len(s), index=s.index)
        assert scanner.vision_bottom_reversal(rows, after_close(rows)), "48.1 is above 48"
    finally:
        scanner.rsi = real


@check("bottom: under ₹5 cr a day of turnover does not fire")
def _():
    rows = daily(bottom_closes(), vol=20_000)        # ~₹0.25 cr a day
    assert scanner.vision_bottom_reversal(rows, after_close(rows)) is None


@check("bottom: fewer than 210 sessions is not enough to have a 200-day")
def _():
    rows = daily(bottom_closes()[-205:])
    assert scanner.vision_bottom_reversal(rows, after_close(rows)) is None


@check("bottom: an unmeasured close never fires")
def _():
    rows = daily(bottom_closes())
    t, o, h, l, _c, v = rows[-1]
    rows[-1] = (t, o, h, l, float("nan"), v)
    assert scanner.vision_bottom_reversal(rows, after_close(rows)) is None


@check("bottom: today's bar is not a close until 15:40 IST")
def _():
    rows = daily(bottom_closes())
    d = datetime.fromtimestamp(rows[-1][0], IST)
    assert len(scanner._vision_complete_daily(rows, d.replace(hour=11))) == len(rows) - 1
    assert len(scanner._vision_complete_daily(rows, d.replace(hour=15, minute=39))) == len(rows) - 1
    assert len(scanner._vision_complete_daily(rows, d.replace(hour=15, minute=40))) == len(rows)
    assert len(scanner._vision_complete_daily(rows, d + timedelta(days=1))) == len(rows)


@check("bottom: an intraday spike through the rule is not evaluated before the close")
def _():
    rows = daily(bottom_closes())
    d = datetime.fromtimestamp(rows[-1][0], IST) + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    live = rows + [(d.timestamp(), 124, 400, 124, 400.0, 1_000_000)]   # a live bar above the 200-day
    at_noon = scanner.vision_bottom_reversal(live, d.replace(hour=12))
    assert at_noon and at_noon["entry"] == rows[-1][4], "must read yesterday's close, not the live price"
    assert scanner.vision_bottom_reversal(live, d.replace(hour=17)) is None


@check("bottom: weekly closes take the LAST close of each ISO week")
def _():
    rows = daily([float(i) for i in range(1, 16)])
    wk = scanner._vision_weekly_closes(rows)
    by = {}
    for r in rows:
        by[datetime.fromtimestamp(r[0], IST).isocalendar()[:2]] = r[4]
    assert wk == [by[k] for k in sorted(by)] and wk[-1] == 15.0


# ── 4-hour breakout ─────────────────────────────────────────────────────────

@check("4H: fires on a solid bullish candle closing through the prior 20-candle high")
def _():
    rows = hourly_from_4h(range_candles() + [BREAKOUT])
    sig = scanner.vision_4h_breakout(rows, after_4h(rows))
    assert sig, "the constructed breakout did not fire"
    assert sig["engine"] == "brk4h" and sig["entry"] == 106.0 and sig["ctx"]["level"] == 102.0
    assert sig["candle"].endswith("IST") and "–" in sig["candle"]


@check("4H: the hourly bars really do aggregate to the intended candles")
def _():
    from signals.buoy import to_4h
    want = range_candles() + [BREAKOUT]
    got = to_4h(hourly_from_4h(want))
    assert len(got) == len(want)
    for g, w in zip(got, want):
        assert tuple(round(x, 6) for x in g[1:]) == tuple(round(x, 6) for x in w), (g, w)


@check("4H: a candle closing in the middle of its range (long upper wick) does not fire")
def _():
    o, h, l, c, v = BREAKOUT
    rows = hourly_from_4h(range_candles() + [(o, 112.0, l, c, v)])
    assert scanner.vision_4h_breakout(rows, after_4h(rows)) is None


@check("4H: a thin body (doji-like) does not fire")
def _():
    rows = hourly_from_4h(range_candles() + [(105.0, 106.2, 100.8, 106.0, 1_000_000)])
    assert scanner.vision_4h_breakout(rows, after_4h(rows)) is None


@check("4H: a red candle does not fire")
def _():
    rows = hourly_from_4h(range_candles() + [(106.2, 106.4, 100.8, 106.0, 1_000_000)])
    assert scanner.vision_4h_breakout(rows, after_4h(rows)) is None


@check("4H: the break must be FRESH — a name already closed above the level does not refire")
def _():
    rows = hourly_from_4h(range_candles() + [(101.0, 103.2, 100.8, 103.0, 1_000_000), BREAKOUT])
    assert scanner.vision_4h_breakout(rows, after_4h(rows)) is None


@check("4H: volume under 1.5× the prior average does not fire")
def _():
    o, h, l, c, _v = BREAKOUT
    rows = hourly_from_4h(range_candles() + [(o, h, l, c, 500_000)])
    assert scanner.vision_4h_breakout(rows, after_4h(rows)) is None


@check("4H: a candle whose window has not closed is not evaluated")
def _():
    from signals.buoy import to_4h
    for pre in (range_candles(40), range_candles(41)):          # the break on a morning, then an afternoon candle
        rows = hourly_from_4h(pre + [BREAKOUT])
        d = datetime.fromtimestamp(to_4h(rows)[-1][0], IST)
        end = d.replace(hour=13, minute=15) if d.hour < 13 else d.replace(hour=15, minute=30)
        assert scanner.vision_4h_breakout(rows, end + timedelta(minutes=9)) is None, d
        assert scanner.vision_4h_breakout(rows, end + timedelta(minutes=10)), d


@check("4H: first-half candle closes at 13:15, settles at 13:25")
def _():
    from signals.buoy import to_4h
    cs = to_4h(hourly_from_4h(range_candles(41)))          # odd count → ends on a morning candle
    d = datetime.fromtimestamp(cs[-1][0], IST)
    assert d.hour == 9
    assert len(scanner._vision_complete_4h(cs, d.replace(hour=13, minute=24))) == len(cs) - 1
    assert len(scanner._vision_complete_4h(cs, d.replace(hour=13, minute=25))) == len(cs)


@check("4H: too little history does not fire")
def _():
    rows = hourly_from_4h(range_candles(20) + [BREAKOUT])
    assert scanner.vision_4h_breakout(rows, after_4h(rows)) is None


@check("4H: an illiquid name does not fire")
def _():
    rows = hourly_from_4h([(o, h, l, c, v / 100) for o, h, l, c, v in range_candles() + [BREAKOUT]])
    assert scanner.vision_4h_breakout(rows, after_4h(rows)) is None


# ── Levels ──────────────────────────────────────────────────────────────────

def _both_signals():
    d = daily(bottom_closes())
    h = hourly_from_4h(range_candles() + [BREAKOUT])
    return [scanner.vision_bottom_reversal(d, after_close(d)), scanner.vision_4h_breakout(h, after_4h(h))]


@check("levels: stop < entry < T1 < T2 < T3 on both rules")
def _():
    for s in _both_signals():
        assert s["sl"] < s["entry"] < s["t1"] < s["t2"] < s["t3"], s


@check("levels: T1 repays at least 1.6× the risk; T2, T3 step above it")
def _():
    for s in _both_signals():
        r = s["entry"] - s["sl"]
        assert (s["t1"] - s["entry"]) / r >= 1.6 - 1e-6, s
        assert s["rr"][0] < s["rr"][1] < s["rr"][2], s["rr"]


@check("levels: the stop is within 6% of entry and at least 1.5% from it")
def _():
    for s in _both_signals():
        assert 1.5 - 0.01 <= s["risk_pct"] <= 6.0 + 0.01, s["risk_pct"]


@check("levels: no positive risk, no levels")
def _():
    import pandas as pd
    assert scanner.vision_levels(100.0, pd.Series([100.0] * 30), pd.Series([101.0] * 30), 0.0) is None
    assert scanner.vision_levels(float("nan"), pd.Series([99.0] * 30), pd.Series([101.0] * 30), 1.0) is None


@check("signals carry the facts that fired them, not a forecast")
def _():
    for s in _both_signals():
        assert len(s["why"]) == 4 and all(isinstance(w, str) and w for w in s["why"])
        text = json.dumps(s).lower()
        for word in ("probab", "forecast", "predict", "win rate", "expected return"):
            assert word not in text, word


# ── Grading ─────────────────────────────────────────────────────────────────

SIG = {"entry": 100.0, "sl": 95.0, "t1": 108.0, "t2": 112.5, "t3": 116.5}


def bar(h, l, c=None):
    return (0, 0, h, l, c if c is not None else (h + l) / 2, 0)


@check("grade: a bar touching BOTH the stop and T1 is booked as the stop, and flagged")
def _():
    g = scanner.vision_grade(SIG, [bar(109, 94)], 30)
    assert g["status"] == "stopped" and g["targets_hit"] == 0 and g["ambiguous"] is True, g


@check("grade: T1 then the stop is a different sentence from a stop that never worked")
def _():
    g = scanner.vision_grade(SIG, [bar(108.5, 101), bar(104, 94)], 30)
    assert g["status"] == "stopped_after_t1" and g["targets_hit"] == 1 and not g["ambiguous"], g


@check("grade: T3 closes it; one bar through T1 and T2 counts both")
def _():
    g = scanner.vision_grade(SIG, [bar(113, 101), bar(117, 110)], 30)
    assert g["status"] == "t3" and g["targets_hit"] == 3 and g["bars"] == 2, g


@check("grade: expires at its horizon at the last close, keeping targets reached")
def _():
    g = scanner.vision_grade(SIG, [bar(108.2, 101, 104)] + [bar(106, 99, 103)] * 4, 5)
    assert g["status"] == "expired" and g["targets_hit"] == 1 and g["exit"] == 103, g


@check("grade: no bars after the signal is open, not a result")
def _():
    g = scanner.vision_grade(SIG, [], 30)
    assert g["status"] == "open" and g["targets_hit"] == 0 and g["last"] is None, g


# ── Filing and the feed ─────────────────────────────────────────────────────

def _sig(engine="bottom", sym="ABC", fired="2026-01-05", entry=100.0):
    return {"engine": engine, "sym": sym, "fired_at": fired, "entry": entry, "sl": entry * 0.95,
            "t1": entry * 1.08, "t2": entry * 1.12, "t3": entry * 1.16}


@check("filing: an open signal's levels do not move when the name qualifies again")
def _():
    h, n = vision_scan.file_signals([], [_sig()], "t0")
    h, n2 = vision_scan.file_signals(h, [_sig(fired="2026-01-06", entry=103.0)], "t1")
    assert n == 1 and n2 == 0 and len(h) == 1 and h[0]["entry"] == 100.0 and h[0]["filed_at"] == "t0"


@check("filing: the same bar is never filed twice, even after its first filing closed")
def _():
    h, _ = vision_scan.file_signals([], [_sig()], "t0")
    h[0]["grade"] = {"status": "stopped", "targets_hit": 0}
    h, n = vision_scan.file_signals(h, [_sig()], "t1")
    assert n == 0 and len(h) == 1


@check("filing: a NEW bar after the last filing closed is a new setup")
def _():
    h, _ = vision_scan.file_signals([], [_sig()], "t0")
    h[0]["grade"] = {"status": "stopped", "targets_hit": 0}
    h, n = vision_scan.file_signals(h, [_sig(fired="2026-01-20")], "t1")
    assert n == 1 and len(h) == 2 and h[0]["fired_at"] == "2026-01-20"


@check("filing: the two engines file the same name independently")
def _():
    h, n = vision_scan.file_signals([], [_sig(), _sig(engine="brk4h", fired="2026-01-05T13:15+05:30")], "t0")
    assert n == 2


@check("tally: open + stopped + expired + t3 always equals filed")
def _():
    hist = [dict(_sig(sym=s), grade=g) for s, g in (
        ("A", None), ("B", {"status": "stopped", "targets_hit": 0, "ambiguous": True}),
        ("C", {"status": "stopped_after_t2", "targets_hit": 2}), ("D", {"status": "expired", "targets_hit": 1}),
        ("E", {"status": "t3", "targets_hit": 3}), ("F", {"status": "open", "targets_hit": 1}))]
    c = vision_scan.tally(hist)["bottom"]
    assert c["filed"] == 6 == c["open"] + c["stopped"] + c["expired"] + c["t3"], c
    assert c["open"] == 2 and c["stopped"] == 2 and c["t1_or_better"] == 4 and c["ambiguous"] == 1, c


@check("grading: a closed grade does not reopen on later bars")
def _():
    s = dict(_sig(), grade={"status": "stopped", "targets_hit": 0, "exit": 95.0})
    rows = daily([100.0] * 5 + [130.0] * 5)
    vision_scan.grade_history([s], {"ABC": rows}, {}, "t2")
    assert s["grade"]["status"] == "stopped" and "graded_at" not in s


@check("feed: written under feeds/, not docs/ — a docs/ commit deploys the newspaper")
def _():
    rel = pathlib.Path(vision_scan.FEED).relative_to(ROOT)
    assert rel.parts[0] == "feeds", rel


@check("feed: an empty fetch leaves the previous feed exactly as it was")
def _():
    import harvest_bars
    real_h, real_feed, real_screen = harvest_bars.harvest, vision_scan.FEED, vision_scan.SCREEN
    with tempfile.TemporaryDirectory() as td:
        feed, screen = os.path.join(td, "v.json"), os.path.join(td, "s.json")
        with open(feed, "w") as f:
            f.write('{"ok":true,"history":[1]}')
        with open(screen, "w") as f:
            json.dump({"rows": [{"sym": "ABC"}]}, f)
        try:
            vision_scan.FEED, vision_scan.SCREEN = feed, screen
            harvest_bars.harvest = lambda syms, name: ({}, {"asked": len(syms), "got": 0})
            assert vision_scan.run() == 1
            assert open(feed).read() == '{"ok":true,"history":[1]}'
        finally:
            harvest_bars.harvest, vision_scan.FEED, vision_scan.SCREEN = real_h, real_feed, real_screen


@check("feed: a full run files, grades and publishes the rules and the no-win-rate note")
def _():
    import harvest_bars
    d = daily(bottom_closes())
    h = hourly_from_4h(range_candles() + [BREAKOUT])
    real_h, real_feed, real_screen = harvest_bars.harvest, vision_scan.FEED, vision_scan.SCREEN
    with tempfile.TemporaryDirectory() as td:
        feed, screen = os.path.join(td, "v.json"), os.path.join(td, "s.json")
        with open(screen, "w") as f:
            json.dump({"rows": [{"sym": "ABC", "name": "Abc Ltd", "sector": "X"}]}, f)
        try:
            vision_scan.FEED, vision_scan.SCREEN = feed, screen
            harvest_bars.harvest = lambda syms, name: (
                ({"ABC": d} if name == "vision_d" else {"ABC": h}), {"asked": 1, "got": 1})
            assert vision_scan.run(now=after_4h(h)) == 0
            out = json.load(open(feed))
        finally:
            harvest_bars.harvest, vision_scan.FEED, vision_scan.SCREEN = real_h, real_feed, real_screen
    assert {s["engine"] for s in out["today"]} == {"bottom", "brk4h"}, out["today"]
    assert all(s.get("filed_at") for s in out["today"])
    assert set(out["rules"]) == {"bottom", "brk4h"} and out["levels"]
    assert "no win rate" in out["note"]
    assert out["counts"]["bottom"]["filed"] == 1 and out["counts"]["brk4h"]["filed"] == 1


def main() -> int:
    passed = failed = 0
    for name, fn in CHECKS:
        try:
            fn()
        except AssertionError as e:
            print(f"  FAIL  {name}  ({e})")
            failed += 1
        except Exception as e:                              # noqa: BLE001
            print(f"  ERROR {name}  ({type(e).__name__}: {e})")
            failed += 1
        else:
            print(f"  PASS  {name}")
            passed += 1
    print(f"\n{passed} passed · {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
