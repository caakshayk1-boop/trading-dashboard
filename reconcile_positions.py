#!/usr/bin/env python3
"""
reconcile_positions.py — one-time cleanup of leaked OPEN rows in all_signals.

Why this exists
---------------
run_price_alerts had no time stop, so every signal ever filed stayed status
='OPEN' forever and was re-evaluated against the *current* day's range on every
scan. By 2026-07-30 there were 386 such rows — 311 of them 1H forex/commodity
signals, the oldest filed 2026-06-03. One scan then resolved the whole backlog
at once and sent 180+ Telegram alerts, including 60 "positions" in NATGAS and
56 in CRUDE that were really the same trade re-inserted every 4 hours.

standalone_scan.py now enforces a time stop, so going forward these expire on
schedule. But the existing backlog must not be booked as real outcomes: those
rows were never managed, many are duplicates of each other, and the price
history at their fill time cannot be honestly reconstructed. Averaging them
into expectancy would corrupt the only performance number worth having.

So: anything already past its holding limit at the time of this run is marked
VOID with r_multiple left NULL. signal_report.py excludes VOID from both the
closed and open buckets, so the ledger simply forgets them rather than lying.
Anything still inside its holding limit is left OPEN and picked up normally by
the next scan.

This script sends no Telegram messages. It is safe to run repeatedly.

Usage
-----
    python reconcile_positions.py            # dry run — report only, no writes
    python reconcile_positions.py --apply     # perform the update
"""
from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

# Kept in sync with standalone_scan.MAX_HOLD_HOURS — imported so there is one
# source of truth rather than a second copy that drifts.
try:
    from standalone_scan import MAX_HOLD_HOURS, _DEFAULT_MAX_HOLD_HOURS
except Exception:                                    # pragma: no cover
    MAX_HOLD_HOURS, _DEFAULT_MAX_HOLD_HOURS = {}, 20 * 24


def _limit(tf: str) -> int:
    return MAX_HOLD_HOURS.get((tf or "").upper(), _DEFAULT_MAX_HOLD_HOURS)


def _filed_at(row: dict) -> datetime | None:
    for key in ("sent_at", "date"):
        raw = row.get(key)
        if not raw:
            continue
        txt = str(raw)
        try:
            dt = datetime.fromisoformat(txt)
            return dt if dt.tzinfo else dt.replace(tzinfo=IST)
        except ValueError:
            pass
        for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(txt[:19], f).replace(tzinfo=IST)
            except ValueError:
                continue
    return None


def main(apply: bool) -> int:
    import tracker

    tracker.init_db()
    now = datetime.now(IST)

    with tracker._conn() as c:
        rows = c.execute(
            "SELECT id, date, sent_at, symbol, signal_type, timeframe, status "
            "FROM all_signals WHERE status IN ('OPEN','T1_HIT')"
        ).fetchall()

    cols = ["id", "date", "sent_at", "symbol", "signal_type", "timeframe", "status"]
    stale, live, undated = [], 0, 0
    by_symbol: Counter = Counter()
    by_tf: Counter = Counter()

    for r in rows:
        row = dict(zip(cols, r))
        filed = _filed_at(row)
        limit = _limit(row["timeframe"])
        if filed is None:
            undated += 1
            stale.append(row["id"])          # undateable = unmanageable
            by_symbol[row["symbol"]] += 1
            by_tf[row["timeframe"] or "?"] += 1
            continue
        age_h = (now - filed).total_seconds() / 3600.0
        if age_h > limit:
            stale.append(row["id"])
            by_symbol[row["symbol"]] += 1
            by_tf[row["timeframe"] or "?"] += 1
        else:
            live += 1

    print(f"open rows scanned : {len(rows)}")
    print(f"  past time stop  : {len(stale)}  → VOID")
    print(f"  still live       : {live}  → left OPEN for the next scan")
    print(f"  undated          : {undated}  (no sent_at/date — treated as stale)")
    if by_tf:
        print("\nby timeframe:")
        for tf, n in by_tf.most_common():
            print(f"  {tf:10s} {n:4d}  (limit {_limit(tf)}h)")
    if by_symbol:
        print("\ntop symbols:")
        for s, n in by_symbol.most_common(12):
            print(f"  {s:10s} {n:4d} duplicate open rows")

    if not stale:
        print("\nNothing to reconcile.")
        return 0

    if not apply:
        print(f"\nDRY RUN — nothing written. Re-run with --apply to void {len(stale)} rows.")
        return 0

    note = f"voided by reconcile_positions on {now.date().isoformat()} — leaked pre-time-stop"
    with tracker._conn() as c:
        for i in range(0, len(stale), 400):
            chunk = stale[i:i + 400]
            marks = ",".join("?" * len(chunk))
            c.execute(
                f"UPDATE all_signals SET status='VOID', closed_at=?, why_triggered=? "
                f"WHERE id IN ({marks}) AND status IN ('OPEN','T1_HIT')",
                [now.isoformat(), note, *chunk])
        c.commit()

    with tracker._conn() as c:
        left = c.execute(
            "SELECT COUNT(*) FROM all_signals WHERE status IN ('OPEN','T1_HIT')"
        ).fetchone()[0]
    print(f"\n✅ Voided {len(stale)} rows. Open positions remaining: {left}")
    return 0


if __name__ == "__main__":
    sys.exit(main(apply="--apply" in sys.argv))
