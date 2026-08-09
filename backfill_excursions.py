#!/usr/bin/env python3
"""
backfill_excursions.py — how far did each closed trade run before it resolved?

Why this matters more than it sounds
------------------------------------
max_profit_pct and max_drawdown_pct existed as columns and were NULL on all 605
rows, so the ledger recorded that a trade lost 1R and nothing else. That single
number cannot distinguish the two failures that need opposite fixes:

  · a loser that ran +1.8R in favour and then turned  -> the STOP is wrong
    (or there is no trail), the entry was fine
  · a loser that never traded above entry             -> the SELECTION is wrong,
    the stop is irrelevant

Both look identical afterwards: a column of -1R. Excursion is what separates
them, and it is the difference between fixing an exit rule and fixing an engine.

Measured in R, not percent, because that is the only unit comparable across a
90-rupee stock and a 4,000-dollar ounce of gold.

Usage
-----
    python backfill_excursions.py            # report only
    python backfill_excursions.py --apply    # write MFE/MAE back
"""
from __future__ import annotations

import argparse
import logging
import statistics
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

import tracker
from symbols import to_yahoo

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("mfe")

CLOSED = ("SL_HIT", "STOPPED", "T1_HIT", "T2_HIT", "TARGET_HIT", "TIME_STOP", "EXPIRED")


def _f(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def excursions(sym, sig_date, closed_at, timeframe, entry, sl, is_long):
    """(MFE, MAE) in R over the trade's life. None when it cannot be measured."""
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    try:
        start = datetime.fromisoformat(str(sig_date)[:10]).date() + timedelta(days=1)
    except ValueError:
        return None

    # Bound by the close date when there is one, else by the strategy horizon.
    # An unbounded window would report the best price the symbol ever reached
    # after the signal, which is a fact about the symbol, not about the trade.
    end = None
    if closed_at:
        try:
            end = datetime.fromisoformat(str(closed_at)[:10]).date() + timedelta(days=1)
        except ValueError:
            end = None
    if end is None:
        hold = tracker._max_hold_hours(str(timeframe or ""))
        end = start + timedelta(days=int(hold / 24 * 1.6) + 3)
    end = min(end, datetime.now().date() + timedelta(days=1))
    if start >= end:
        return None

    try:
        df = yf.download(to_yahoo(sym), start=start.isoformat(), end=end.isoformat(),
                         interval="1d", progress=False, auto_adjust=True)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    hi, lo = float(df["High"].max()), float(df["Low"].min())
    mfe = (hi - entry) / risk if is_long else (entry - lo) / risk
    mae = (entry - lo) / risk if is_long else (hi - entry) / risk
    # MAE is stored negative: it is movement against the position.
    return round(max(mfe, 0.0), 3), round(-max(mae, 0.0), 3)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="cap rows (for a quick look)")
    args = ap.parse_args()

    tracker.init_db()
    in_list = ",".join(f"'{s}'" for s in CLOSED)
    with tracker._conn() as c:
        rows = pd.read_sql(
            f"SELECT id, symbol, date, closed_at, timeframe, action, entry, sl, "
            f"status, r_multiple, signal_type FROM all_signals "
            f"WHERE upper(coalesce(status,'')) IN ({in_list}) "
            f"AND max_profit_pct IS NULL", c
        ).to_dict("records")
    if args.limit:
        rows = rows[: args.limit]

    log.info(f"measuring {len(rows)} closed trades\n")
    out, skipped = [], 0
    for s in rows:
        entry, sl = _f(s["entry"]), _f(s["sl"])
        if entry is None or sl is None:
            skipped += 1
            continue
        is_long = str(s.get("action", "BUY")).upper() != "SELL"
        e = excursions(s["symbol"], s["date"], s["closed_at"], s["timeframe"],
                       entry, sl, is_long)
        if e is None:
            skipped += 1
            continue
        out.append((s, e[0], e[1]))

    log.info(f"measured {len(out)} · {skipped} unmeasurable\n")
    if not out:
        return 0

    losers = [(s, m, a) for s, m, a in out if (_f(s["r_multiple"]) or 0) < 0]
    # The headline question: of the trades that lost, how many had been in
    # profit by a full R at some point?
    rescuable = [x for x in losers if x[1] >= 1.0]
    mfes = [m for _s, m, _a in losers]
    log.info("── losers ─────────────────────────────────────────────")
    log.info(f"  {len(losers)} losing trades measured")
    if mfes:
        log.info(f"  median MFE before the loss : {statistics.median(mfes):+.2f}R")
        log.info(f"  reached +1R in favour first: {len(rescuable)} "
                 f"({len(rescuable) / len(losers) * 100:.0f}%)")
        log.info(f"  never went green at all    : "
                 f"{sum(1 for m in mfes if m <= 0.01)} "
                 f"({sum(1 for m in mfes if m <= 0.01) / len(losers) * 100:.0f}%)")
    log.info("")
    log.info("  A high 'reached +1R first' share is a STOP/trail problem.")
    log.info("  A high 'never went green' share is a SELECTION problem.")

    if not args.apply:
        log.info("\nreport only — pass --apply to write")
        return 0

    with tracker._conn() as c:
        for s, mfe, mae in out:
            c.execute("UPDATE all_signals SET max_profit_pct=?, max_drawdown_pct=? "
                      "WHERE id=?", (mfe, mae, int(s["id"])))
        c.commit()
        tracker._db.sync(c)
    log.info(f"\nwrote MFE/MAE to {len(out)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
