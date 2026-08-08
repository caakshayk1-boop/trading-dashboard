#!/usr/bin/env python3
"""
fix_phantom_exits.py — book stops at the stop when the recorded exit never traded.

Why this exists
---------------
`standalone_scan.price_alerts` modelled gap risk by taking the OPEN of the
first bar whose low breached the stop. Sound idea, but its window was not
bounded to the trade: `_since_entry()` returned the entire fetched frame — up
to 365 days — whenever it could not establish a cutoff. So the "gap open" it
booked could belong to a bar from before the signal existed.

    HINDALCO, signalled 2026-08-07, stop 1013.89, was booked SL_HIT at 990.00.
    That is exactly the OPEN of 2026-08-03, four days earlier. The lowest low
    after the signal was 1017.00. Price never traded at 990 while the trade
    was live.

`regrade.py --apply` (2026-08-08) repaired most of these, but it can only fix
what it can re-walk: rows it marks ungradeable keep their original numbers, and
HINDALCO — four bars of history — is one of them.

The test used here
------------------
Not "is the slip large?". A 2.36% slip looks survivable and HINDALCO's is
exactly that, while a genuine 4% gap in an illiquid name is real. The only
sound question is empirical:

    did price ever trade at the recorded exit, between entry and close?

If it did, the fill stands, however ugly. If it never printed, the exit is an
artifact and the trade is booked where it actually would have filled — at the
stop, for -1R.

Usage
-----
    python fix_phantom_exits.py            # report only
    python fix_phantom_exits.py --apply    # write corrections
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

import tracker
from symbols import to_yahoo

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("phantom")

CLOSED_AT_STOP = ("SL_HIT", "STOPPED")
# How far past the recorded exit a bar must reach to count as "traded there".
# Bars are OHLC, so an exact tick match is not available; a hair of tolerance
# stops floating-point noise from condemning a legitimate fill.
TOUCH_TOL = 0.001          # 0.1%


def _f(v):
    try:
        f = float(v)
        return f if f == f else None      # NaN check
    except (TypeError, ValueError):
        return None


def _window(sig) -> tuple[str, str] | None:
    """Signal date → close date, padded, as ISO strings."""
    d = str(sig.get("date") or "")[:10]
    c = str(sig.get("closed_at") or "")[:10]
    if not d:
        return None
    try:
        start = datetime.fromisoformat(d).date()
        end = datetime.fromisoformat(c).date() if c else start + timedelta(days=30)
    except ValueError:
        return None
    # One day either side: the signal's own session can fill it, and closed_at
    # was written as the grader's wall clock on 25 rows, so it can overshoot.
    return (start - timedelta(days=1)).isoformat(), (end + timedelta(days=2)).isoformat()


def traded_at(symbol: str, price: float, start: str, end: str, is_long: bool):
    """Did price reach `price` between start and end? None if no data."""
    try:
        df = yf.download(to_yahoo(symbol), start=start, end=end,
                         interval="1d", progress=False, auto_adjust=True)
    except Exception as e:
        log.warning(f"  {symbol}: download failed ({e})")
        return None
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    # A long exits downward, so it must have traded AT or BELOW the price.
    if is_long:
        return bool((df["Low"] <= price * (1 + TOUCH_TOL)).any())
    return bool((df["High"] >= price * (1 - TOUCH_TOL)).any())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write corrections")
    args = ap.parse_args()

    tracker.init_db()
    with tracker._conn() as c:
        rows = pd.read_sql(
            "SELECT id, symbol, date, closed_at, action, entry, sl, exit_price, "
            "r_multiple, pnl_pct, status, signal_type FROM all_signals "
            "WHERE upper(coalesce(status,'')) IN ('SL_HIT','STOPPED')", c
        ).to_dict("records")

    log.info(f"checking {len(rows)} stopped signals\n")
    fixes, checked, nodata = [], 0, 0

    for s in rows:
        entry, sl, ex = _f(s["entry"]), _f(s["sl"]), _f(s["exit_price"])
        if None in (entry, sl, ex):
            continue
        is_long = str(s.get("action", "BUY")).upper() != "SELL"
        # A fill at or better than the stop is not a phantom.
        beyond = (ex < sl) if is_long else (ex > sl)
        if not beyond:
            continue

        w = _window(s)
        if not w:
            continue
        checked += 1
        hit = traded_at(s["symbol"], ex, w[0], w[1], is_long)
        if hit is None:
            nodata += 1
            log.info(f"  {s['symbol']:12} {s['date']}  no data — left alone")
            continue
        if hit:
            continue        # it really did trade there

        risk = abs(entry - sl)
        if risk <= 0:
            continue
        new_r = -1.0
        new_pnl = round((sl - entry) / entry * 100 * (1 if is_long else -1), 2)
        fixes.append((s["id"], sl, new_pnl, new_r, s))
        log.info(
            f"  PHANTOM {s['symbol']:12} {s['date']} {s['signal_type']:14} "
            f"exit {ex:.2f} never traded (stop {sl:.2f}) "
            f"r {s['r_multiple']} -> {new_r}"
        )

    log.info(f"\nchecked {checked} beyond-stop exits · {nodata} lacked data · "
             f"{len(fixes)} phantom")

    if not fixes:
        return 0
    if not args.apply:
        log.info("\nreport only — pass --apply to write")
        return 0

    with tracker._conn() as c:
        for sid, sl, pnl, r, _s in fixes:
            c.execute(
                "UPDATE all_signals SET exit_price=?, pnl_pct=?, r_multiple=?, "
                "regraded_at=? WHERE id=?",
                (round(sl, 4), pnl, r, datetime.utcnow().isoformat(), int(sid))
            )
        c.commit()
        tracker._db.sync(c)
    log.info(f"\napplied {len(fixes)} corrections")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
