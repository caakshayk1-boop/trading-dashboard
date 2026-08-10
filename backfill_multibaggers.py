#!/usr/bin/env python3
"""
backfill_multibaggers.py — put past multibagger scans into the ledger.

Why this exists
---------------
The weekly multibagger scan (Saturday 09:30 IST) has always written to its own
`multibaggers` table and nowhere else. That table is REPLACED on every scan and
read by exactly one consumer: the ticker's 💎 segment, which shows the top five
by score. So names like TORNTPHARM, GABRIEL and KARURVYSYA reached the site as
a price and a target and nothing more — no entry, no stop, no scan date, no
outcome — and never appeared in the Signal Log at all. Last week's ideas were
deleted rather than resolved.

tracker.log_multibaggers() now mirrors each scan into `all_signals`, so every
scan from the next Saturday onward lands in the log by itself. This script is
for the scans that already happened: it reads whatever dates the multibaggers
table still holds and writes the matching ledger rows.

Excluded from expectancy, like ai_longterm — see tracker.EXCLUDE_FROM_EXPECTANCY
and the NON_TRADING list in vercel-news/api/stats.js. These are 6-12 month holds
off weekly bars; letting them into the R statistics would corrupt the only
honest number on the site. They appear in the Signal Log and in
/api/signals?type=multibagger, and they touch no rate.

Idempotent: rows already logged for a date are counted and skipped, so running
it twice cannot double the ledger.

Usage
-----
    python backfill_multibaggers.py            # report only
    python backfill_multibaggers.py --apply    # write the ledger rows
"""
from __future__ import annotations

import argparse
import logging

import db
import tracker

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("backfill_multibaggers")

# Columns log_multibaggers() writes, in the order _log_multibaggers_to_ledger
# expects to read them back out.
COLS = ("symbol", "price", "high_52w", "low_52w", "range_pos", "wk_rsi",
        "wk_adx", "vol_ratio", "sl", "support1", "support2",
        "target1", "target2", "target3", "rr", "score", "pe", "fno",
        "reason", "tv_link")


def scans() -> list[tuple[str, list[dict]]]:
    """Every multibagger scan still in the table, oldest first."""
    con = db.connect()
    have = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='multibaggers'"
    ).fetchall()
    if not have:
        log.warning("no multibaggers table — nothing to backfill")
        return []

    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM multibaggers ORDER BY date").fetchall()]
    out = []
    for d in dates:
        rows = con.execute(
            f"SELECT {','.join(COLS)} FROM multibaggers WHERE date=? ORDER BY score DESC",
            (d,)).fetchall()
        out.append((d, [dict(zip(COLS, r)) for r in rows]))
    return out


def already_logged(date: str) -> int:
    con = db.connect()
    return con.execute(
        "SELECT COUNT(*) FROM all_signals WHERE signal_type=? AND date=?",
        (tracker.MULTIBAGGER_SIGNAL_TYPE, date)).fetchone()[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the rows (default is report only)")
    ap.add_argument("--reset", action="store_true",
                    help="delete every multibagger row from the ledger first")
    args = ap.parse_args()

    found = scans()
    if not found:
        return 0

    # --reset exists for exactly one situation: the first run of this script
    # wrote all 56 rows stamped with the day it RAN, because
    # log_batch_to_all_signals took today and ignored the scan date. Those rows
    # are wrong and cannot be corrected in place — the fixed script writes to
    # the real scan dates and would leave the mis-dated set orphaned beside it.
    #
    # Scoped to signal_type='multibagger' and nothing else. No other engine's
    # rows are reachable from here, and multibagger rows are excluded from every
    # published rate, so this cannot move a number on the site.
    if args.reset:
        if not args.apply:
            log.warning("--reset needs --apply; nothing deleted")
        else:
            con = db.connect()
            n = con.execute("SELECT COUNT(*) FROM all_signals WHERE signal_type=?",
                            (tracker.MULTIBAGGER_SIGNAL_TYPE,)).fetchone()[0]
            con.execute("DELETE FROM all_signals WHERE signal_type=?",
                        (tracker.MULTIBAGGER_SIGNAL_TYPE,))
            con.commit()
            db.sync(con)
            log.warning(f"--reset: deleted {n} multibagger row(s) from the ledger")

    total_new = 0
    for date, rows in found:
        have = already_logged(date)
        if have:
            log.info(f"{date}  {len(rows):3} scanned  · {have} already in the ledger — skipping")
            continue

        # A scan whose rows are all identical apart from the symbol is a
        # broadcast bug, not a scan: the 2026-05-09 batch has four names
        # sharing one price, one stop and one reason string. Logging that as
        # four distinct signals would put fiction in the ledger, so it is
        # reported and skipped rather than written.
        sigs = {(r["price"], r["sl"], r["target2"]) for r in rows}
        if len(rows) > 2 and len(sigs) < len(rows) / 2:
            log.warning(f"{date}  {len(rows):3} scanned  · SKIPPED — only {len(sigs)} "
                        f"distinct level sets across {len(rows)} symbols; "
                        f"this scan is corrupt, not a signal batch")
            continue

        log.info(f"{date}  {len(rows):3} scanned  → {len(rows)} ledger rows "
                 f"({', '.join(r['symbol'] for r in rows[:5])}"
                 f"{'…' if len(rows) > 5 else ''})")
        total_new += len(rows)
        if args.apply:
            tracker._log_multibaggers_to_ledger(rows, date)

    if not args.apply:
        log.info(f"\nreport only — {total_new} row(s) would be written. "
                 f"Re-run with --apply to write them.")
    else:
        log.info(f"\nwrote {total_new} ledger row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
