#!/usr/bin/env python3
"""
backfill_remarks.py — fill all_signals.remarks on rows written before it existed.

`remarks` is what the signal log's "Relates to" column reads. Adding the column
and teaching the writers to populate it only helps rows written AFTER that
change — every row already in the ledger keeps NULL, so the column shipped
rendering a dash on every single line. A new column is not a populated column.

Values come from tracker.REMARKS, keyed on signal_type, so the backfill and the
live writers cannot describe the same engine differently.

Rows whose signal_type is not in the map are REPORTED and left alone rather
than given a generic string — an unknown engine is a gap in the map worth
seeing, not something to paper over with "Signal".

Usage
-----
    python3 backfill_remarks.py            # dry run
    python3 backfill_remarks.py --apply    # writes

Idempotent: only touches rows whose remarks is NULL/empty, so re-running is a
no-op and a hand-written remark is never overwritten.
"""
from __future__ import annotations

import sys
from collections import Counter

import db as _db
import tracker
from tracker import REMARKS


def main() -> int:
    apply = "--apply" in sys.argv

    # Apply pending migrations first. `remarks` is added by tracker.init_db()'s
    # ALTER TABLE list, and on the FIRST run nothing has called init_db()
    # against Turso yet — so this script would report "no remarks column" and
    # fail, waiting on a scan that runs on its own schedule. A backfill that
    # depends on another job having run first is a backfill that silently does
    # not happen.
    tracker.init_db()

    with _db.connect() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(all_signals)").fetchall()}
        if "remarks" not in cols:
            print("all_signals still has no `remarks` column after init_db() — "
                  "the migration did not apply")
            return 1

        rows = c.execute(
            "SELECT id, signal_type FROM all_signals "
            "WHERE remarks IS NULL OR TRIM(remarks) = ''"
        ).fetchall()

        total = c.execute("SELECT COUNT(*) FROM all_signals").fetchone()[0]
        print(f"{total} rows in all_signals, {len(rows)} with no remarks\n")
        if not rows:
            print("nothing to do")
            return 0

        by_type = Counter(r[1] for r in rows)
        known, unknown = {}, {}
        for stype, n in by_type.items():
            (known if stype in REMARKS else unknown)[stype] = n

        for stype, n in sorted(known.items(), key=lambda kv: -kv[1]):
            print(f"  {stype:18} {n:4} row(s)  ->  {REMARKS[stype]}")
        if unknown:
            print("\n  NOT IN tracker.REMARKS — left untouched, add them to the map:")
            for stype, n in sorted(unknown.items(), key=lambda kv: -kv[1]):
                print(f"    {stype!r:20} {n:4} row(s)")

        todo = [(rid, stype) for rid, stype in rows if stype in REMARKS]
        if not apply:
            print(f"\nDRY RUN — would fill {len(todo)} row(s). Re-run with --apply")
            return 0

        for rid, stype in todo:
            c.execute("UPDATE all_signals SET remarks=? WHERE id=?", (REMARKS[stype], rid))
        c.commit()
        _db.sync(c)

        left = c.execute(
            "SELECT COUNT(*) FROM all_signals "
            "WHERE remarks IS NULL OR TRIM(remarks) = ''").fetchone()[0]
        print(f"\nfilled {len(todo)} row(s); {left} still blank "
              f"({'all are unmapped engine types' if left == sum(unknown.values()) else 'CHECK THIS'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
