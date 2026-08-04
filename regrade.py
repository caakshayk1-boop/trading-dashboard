#!/usr/bin/env python3
"""
regrade.py — re-grade the closed ledger with the corrected outcome logic.

Why this exists
---------------
Every closed row in all_signals was graded by the old update_all_outcomes(),
which had three defects that all pushed the same direction:

  1. Order was discarded. The whole post-signal window collapsed into
     lo = min(all lows) / hi = max(all highs), and the stop was tested first.
     A trade that ran to target on day 2 and grazed its stop on day 30 booked
     as SL_HIT.
  2. The window had no end. yf.download(start=...) ran to today, so given
     enough sessions every position eventually touched its stop.
  3. Resolution was wrong. 366 of 578 signals are 1H, graded on DAILY candles
     starting the day *after* the signal — so the signal's own session was
     discarded entirely and intrabar sequence was invented.

tracker.py was fixed on 2026-08-05, but only for signals graded from that
point forward. The 501 rows already closed still carry the old numbers, and
they are the entire basis for every expectancy figure in the terminal.

This script re-grades them properly and reports old vs new. It writes nothing
unless --apply is passed.

The question it exists to settle
--------------------------------
The live ledger says cf_1h earns +0.458R over 355 trades (t = +5.00).
`backtest.py --asset cf` says the same family earns -0.029R over 545 trades.
Both cannot be true. cf_1h is 1H, so grading it on its own 1H bars is the only
honest way to find out.

Usage
-----
    python regrade.py                    # report only, no writes
    python regrade.py --engine cf_1h     # one engine
    python regrade.py --apply            # write corrected outcomes back
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

from symbols import to_yahoo

# Bars we can honestly grade a given signal timeframe on. A signal must be
# graded at or below its own resolution — grading a 1H setup on daily bars
# invents an intrabar sequence that the data never contained.
INTERVAL_FOR = {
    "15M": "15m", "15m": "15m",
    "1H": "1h",
    "4H": "1h",          # no native 4h from Yahoo; 1h is finer, so still honest
    "1D": "1d", "DAILY": "1d", "SWING": "1d",
    "WEEKLY": "1d", "MONTHLY": "1d",
}

# Holding limit per timeframe, in hours. Mirrors standalone_scan.MAX_HOLD_HOURS.
MAX_HOLD = {
    "15M": 6, "15m": 6,
    "1H": 48,
    "4H": 48 * 4,
    "1D": 20 * 24, "DAILY": 20 * 24, "SWING": 20 * 24,
    "WEEKLY": 20 * 24 * 7,
    "MONTHLY": 180 * 24,
}

# Yahoo caps intraday history. Beyond these windows the request silently
# returns nothing rather than erroring, so bound the ask explicitly.
INTRADAY_MAX_DAYS = {"15m": 60, "1h": 730}

FEED = os.path.join(os.path.dirname(__file__), "data", "all_signals.json")

_cache: dict[tuple[str, str], pd.DataFrame | None] = {}

# sent_at is written naive by runners executing in UTC, while the feed labels
# dates IST. Yahoo returns intraday bars in exchange time. Rather than guess,
# --shift re-runs the grade with the signal clock moved, so the conclusion can
# be checked for robustness against the ambiguity instead of resting on it.
SHIFT_HOURS = 0.0


def _f(v):
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) else x


def load_feed(path: str) -> list[dict]:
    """The producer emits bare NaN, which is not valid JSON."""
    raw = open(path).read()
    raw = re.sub(r"(?<=[:,\[])(\s*)\bNaN\b", r"\1null", raw)
    raw = re.sub(r"(?<=[:,\[])(\s*)-?\bInfinity\b", r"\1null", raw)
    return json.loads(raw)


def bars(symbol: str, interval: str, start, end) -> pd.DataFrame | None:
    """Fetch once per (symbol, interval) and slice per signal."""
    key = (symbol, interval)
    if key in _cache:
        df = _cache[key]
        return None if df is None else df

    yts = to_yahoo(symbol)
    cap = INTRADAY_MAX_DAYS.get(interval)
    lo = start
    if cap:
        earliest = datetime.now().date() - timedelta(days=cap - 2)
        lo = max(lo, earliest)
    try:
        df = yf.download(yts, start=lo.isoformat(),
                         end=(end + timedelta(days=2)).isoformat(),
                         interval=interval, progress=False,
                         auto_adjust=True, prepost=False)
    except Exception as e:
        print(f"    ! {symbol} ({yts}) {interval}: {e}", file=sys.stderr)
        df = None
    if df is not None and df.empty:
        df = None
    if df is not None and isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    _cache[key] = df
    return df


def regrade_one(sig: dict, df: pd.DataFrame) -> dict | None:
    """
    Walk bars in order from the signal timestamp. Entry must be touched before
    the trade can resolve; the first level reached afterwards decides it.
    Returns None when the window holds no usable bars.
    """
    entry = _f(sig.get("entry"))
    sl = _f(sig.get("sl"))
    t2 = _f(sig.get("target2"))
    t1 = _f(sig.get("target1"))
    target = t2 if t2 is not None else t1
    if entry is None or sl is None or target is None:
        return None

    is_long = str(sig.get("action", "BUY")).upper() != "SELL"
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    # Direction must agree with the levels, or the signal is malformed.
    if is_long and not (sl < entry < target):
        return None
    if not is_long and not (target < entry < sl):
        return None

    tf = str(sig.get("timeframe", "")).upper()
    hold_h = MAX_HOLD.get(tf, 20 * 24)

    # Start at the moment the signal actually existed, not midnight of its
    # date. cf_1h signals carry sent_at timestamps spread across the whole day
    # (03:47, 11:04, 14:39...), so a midnight start hands the grader up to 14
    # hours of price action that preceded the signal — and at 1H resolution
    # that pre-signal window is more than enough to fabricate a stop-out.
    t0 = None
    sent = sig.get("sent_at")
    if sent:
        try:
            t0 = pd.Timestamp(str(sent)).tz_localize(None)
            if SHIFT_HOURS:
                t0 = t0 + pd.Timedelta(hours=SHIFT_HOURS)
        except Exception:
            t0 = None
    if t0 is None:
        try:
            t0 = pd.Timestamp(str(sig["date"])[:10]).tz_localize(None)
        except Exception:
            return None

    idx = df.index
    naive = idx.tz_localize(None) if getattr(idx, "tz", None) is not None else idx
    window = df[(naive >= t0) & (naive <= t0 + pd.Timedelta(hours=hold_h))]
    if window.empty:
        return None

    triggered = False
    status, exit_p, ambiguous = None, None, 0
    last_close = None

    for _, bar in window.iterrows():
        hi, lo = float(bar["High"]), float(bar["Low"])
        last_close = float(bar["Close"])

        if not triggered:
            touched = lo <= entry if is_long else hi >= entry
            if not touched:
                continue
            triggered = True  # the fill bar may also resolve the trade

        hit_sl = (lo <= sl) if is_long else (hi >= sl)
        hit_t = (hi >= target) if is_long else (lo <= target)

        if hit_sl and hit_t:
            status, exit_p, ambiguous = "SL_HIT", sl, 1
        elif hit_sl:
            status, exit_p = "SL_HIT", sl
        elif hit_t:
            status, exit_p = ("T2_HIT" if t2 is not None else "T1_HIT"), target
        if status:
            break

    if not triggered:
        return {"status": "NO_FILL", "r": None, "ambiguous": 0}
    if status is None:
        status, exit_p = "TIME_STOP", last_close

    direction = 1 if is_long else -1
    r = round((exit_p - entry) / risk * direction, 3)
    return {"status": status, "r": r, "ambiguous": ambiguous}


def _is_win(status: str | None) -> bool:
    """
    Target hits only. `TIME_STOP` also begins with "T" — counting it as a win
    reported equity_measured at "100% wins, -0.261R", which is impossible and
    was the tell.
    """
    return bool(status) and status.upper() in ("T1_HIT", "T2_HIT", "T3_HIT", "TARGET_HIT")


def summarise(label: str, rows: list[tuple[float, str]], dropped: int = 0) -> str:
    rs = [r for r, _ in rows if r is not None]
    graded = [(r, s) for r, s in rows if r is not None]
    if len(rs) < 2:
        return f"{label:<18}{len(rs):>5}  insufficient"
    mean = sum(rs) / len(rs)
    sd = (sum((x - mean) ** 2 for x in rs) / (len(rs) - 1)) ** 0.5
    t = mean / (sd / math.sqrt(len(rs))) if sd else float("nan")
    wins = sum(1 for _, s in graded if _is_win(s))
    verdict = "EDGE" if t > 2 else ("BLEED" if t < -2 else "noise")
    # Coverage is part of the result. An engine re-graded on half its sample is
    # not a corrected measurement, it is a different one.
    total = len(rs) + dropped
    cov = f"{100*len(rs)/total:>4.0f}%" if total else "   —"
    return (f"{label:<18}{len(rs):>5}{cov}{100*wins/len(graded):>7.0f}%"
            f"{mean:>9.3f}{sd:>7.2f}{t:>8.2f}  {verdict}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", help="limit to one signal_type")
    ap.add_argument("--apply", action="store_true", help="write corrections back")
    ap.add_argument("--feed", default=FEED)
    ap.add_argument("--shift", type=float, default=0.0,
                    help="hours to shift the signal clock (timezone sensitivity)")
    args = ap.parse_args()

    global SHIFT_HOURS
    SHIFT_HOURS = args.shift

    signals = load_feed(args.feed)
    closed = [s for s in signals
              if s.get("status") not in ("OPEN", "CANCELLED", "VOID", "EXPIRED")]
    if args.engine:
        closed = [s for s in closed if s.get("signal_type") == args.engine]

    print(f"re-grading {len(closed)} closed signals\n")

    by_sym = defaultdict(list)
    for s in closed:
        by_sym[s["symbol"]].append(s)

    old_by_engine = defaultdict(list)
    new_by_engine = defaultdict(list)
    dropped_by_engine = defaultdict(int)
    changed = ungradeable = 0
    updates = []

    for sym, group in sorted(by_sym.items(), key=lambda kv: -len(kv[1])):
        tfs = {str(s.get("timeframe", "")).upper() for s in group}
        interval = INTERVAL_FOR.get(next(iter(tfs)), "1d") if len(tfs) == 1 else "1d"
        lo = min(pd.Timestamp(str(s["date"])[:10]) for s in group).date()
        # Extend past the last signal by its full holding limit. Ending at the
        # last signal date gave a symbol with one signal a two-day window,
        # which Yahoo answers with nothing — reported as "no price data" when
        # the real cause was the ask, not the source.
        hold_d = max(MAX_HOLD.get(tf, 20 * 24) for tf in tfs) / 24
        hi = max(pd.Timestamp(str(s["date"])[:10]) for s in group).date() \
            + timedelta(days=int(hold_d) + 5)
        df = bars(sym, interval, lo, hi)
        print(f"  {sym:<12} {len(group):>4} signals  {interval:<4} "
              f"{'no data' if df is None else str(len(df)) + ' bars'}")
        if df is None:
            ungradeable += len(group)
            for s_ in group:
                dropped_by_engine[s_.get("signal_type", "?")] += 1
                old_by_engine[s_.get("signal_type", "?")].append(
                    (_f(s_.get("r_multiple")), s_.get("status")))
            continue

        for s in group:
            eng = s.get("signal_type", "?")
            old_r = _f(s.get("r_multiple"))
            old_by_engine[eng].append((old_r, s.get("status")))

            res = regrade_one(s, df)
            if res is None or res["status"] == "NO_FILL":
                ungradeable += 1
                dropped_by_engine[eng] += 1
                continue
            new_by_engine[eng].append((res["r"], res["status"]))
            if res["status"] != s.get("status"):
                changed += 1
            updates.append((int(s["id"]), res))

    hdr = f"{'engine':<18}{'n':>5}{'cov':>5}{'win%':>7}{'meanR':>9}{'sd':>7}{'t':>8}  verdict"
    print(f"\n{'='*72}\nOLD LEDGER (as published)\n{'='*72}\n{hdr}")
    for eng in sorted(old_by_engine, key=lambda e: -len(old_by_engine[e])):
        print(summarise(eng, old_by_engine[eng]))

    print(f"\n{'='*72}\nRE-GRADED (chronological, native timeframe, bounded)\n{'='*72}\n{hdr}")
    for eng in sorted(new_by_engine, key=lambda e: -len(new_by_engine[e])):
        print(summarise(eng, new_by_engine[eng], dropped_by_engine[eng]))

    amb = sum(1 for _, r in updates if r["ambiguous"])
    print(f"\nstatus changed: {changed}/{len(updates)} graded"
          f"  ·  ambiguous bars: {amb}  ·  ungradeable: {ungradeable}")

    if args.apply:
        import db as _db
        import tracker
        tracker.init_db()
        with tracker._conn() as c:
            for sid, r in updates:
                c.execute("UPDATE all_signals SET status=?,r_multiple=?,"
                          "exit_ambiguous=?,regraded_at=? WHERE id=?",
                          (r["status"], r["r"], r["ambiguous"],
                           datetime.now().isoformat(), sid))
            c.commit()
            _db.sync(c)
        print(f"\napplied {len(updates)} corrections")
    else:
        print("\nreport only — pass --apply to write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
