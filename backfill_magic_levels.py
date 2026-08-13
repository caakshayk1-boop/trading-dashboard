#!/usr/bin/env python3
"""
backfill_magic_levels.py — fill SL/T1/T2/T3 for magic/magicmagic rows that
predate scanner.magic_levels().

Why this matters
----------------
Before that fix landed, _log_magic_to_ledger wrote every candidate as
action=WATCH with sl/target1/target2/target3 all NULL — the screen's own
levels existed in the Telegram alert but never reached the ledger. Every
signal generated since the fix already carries real levels; this is only for
the rows written before it.

Historical correctness, not today's data
-----------------------------------------
A row from 2026-06-06 must get the SL/T1/T2/T3 that scanner.magic_levels
would have produced ON 2026-06-06 — the 52-week high and ATR AS OF that date,
not today's. Using current data would price the recovery with information
the original screen never had, which is a more subtle version of exactly the
false-precision problem this backfill exists to fix. Every fetch below is
bounded with start=/end= at the signal date, mirroring scan_magic()'s own
1-year window but anchored to the past instead of "now".

A row that magic_levels() rejects (the 52-week high as of that date could not
clear the stop by 1R — MAGIC_MIN_T1_R) stays NULL. That is scanner.py's own
"this was not a valid setup" rejection, applied retroactively; forcing a
number here would be exactly the fabrication the rest of this codebase
refuses to do.

Idempotent: only rows with every level NULL are selected, so a second run
touches nothing.

Usage
-----
    python backfill_magic_levels.py            # report only
    python backfill_magic_levels.py --apply    # write sl/target1/target2/target3/rr/action back
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta

import tracker
from scanner import magic_levels
from symbols import to_yahoo

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("backfill_magic")


def _f(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def levels_as_of(symbol: str, sig_date: str, price: float) -> dict | None:
    """Re-derive magic_levels() using only data available AS OF sig_date."""
    try:
        end = datetime.fromisoformat(str(sig_date)[:10]).date() + timedelta(days=1)
    except ValueError:
        return None
    start = end - timedelta(days=400)  # >1y buffer for the 50-day swing low + 14-period ATR

    try:
        import yfinance as yf
        df1y = yf.download(to_yahoo(symbol), start=start.isoformat(), end=end.isoformat(),
                            interval="1d", progress=False, auto_adjust=True)
    except Exception as e:
        log.warning(f"{symbol}: fetch failed — {e}")
        return None
    if df1y is None or df1y.empty or len(df1y) < 50:
        log.warning(f"{symbol}: insufficient history as of {sig_date} ({0 if df1y is None else len(df1y)} bars)")
        return None
    if hasattr(df1y.columns, "get_level_values"):
        df1y.columns = df1y.columns.get_level_values(0)

    hi52 = float(df1y["High"].max())
    if not (hi52 > 0):
        return None

    return magic_levels(df1y, price, hi52)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="cap rows (for a quick look)")
    args = ap.parse_args()

    tracker.init_db()
    with tracker._conn() as c:
        rows = c.execute(
            "SELECT id, symbol, date, entry, action FROM all_signals "
            "WHERE signal_type IN ('magic','magicmagic') "
            "AND sl IS NULL AND target1 IS NULL AND target2 IS NULL AND target3 IS NULL"
        ).fetchall()
    rows = [dict(zip(("id", "symbol", "date", "entry", "action"), r)) for r in rows]
    if args.limit:
        rows = rows[: args.limit]

    log.info(f"{len(rows)} magic/magicmagic rows with no levels\n")
    if not rows:
        return 0

    filled, rejected, skipped = [], [], 0
    for s in rows:
        price = _f(s["entry"])
        if price is None or price <= 0:
            skipped += 1
            continue
        lv = levels_as_of(s["symbol"], s["date"], price)
        if lv is None:
            # Either unfetchable, or magic_levels() itself rejected the setup
            # (52-week high as of that date could not clear MAGIC_MIN_T1_R).
            rejected.append(s)
            continue
        filled.append((s, lv))

    log.info(f"── result ──────────────────────────────────────")
    log.info(f"  fillable          : {len(filled)}")
    log.info(f"  rejected (no valid setup as of that date, or unfetchable) : {len(rejected)}")
    log.info(f"  skipped (no entry price on record) : {skipped}")
    log.info("")
    for s, lv in filled:
        log.info(
            f"  #{s['id']:>5} {s['symbol']:<14} {s['date'][:10]}  "
            f"entry {s['entry']:>8}  "
            f"SL {lv['sl']:>8}  T1 {lv['target1']:>8}  T2 {lv['target2']:>8}  T3 {lv['target3']:>8}  "
            f"RR {lv['rr']}"
        )
    if rejected:
        log.info("\n  rejected (left NULL, not fabricated):")
        for s in rejected:
            log.info(f"  #{s['id']:>5} {s['symbol']:<14} {s['date'][:10]}")

    if not args.apply:
        log.info("\nreport only — pass --apply to write")
        return 0

    with tracker._conn() as c:
        for s, lv in filled:
            c.execute(
                "UPDATE all_signals SET sl=?, target1=?, target2=?, target3=?, rr=?, "
                "action=? WHERE id=?",
                (lv["sl"], lv["target1"], lv["target2"], lv["target3"], lv["rr"],
                 "BUY", int(s["id"])),
            )
        c.commit()
        tracker._db.sync(c)
    log.info(f"\nwrote levels to {len(filled)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
