#!/usr/bin/env python3
"""
purge_void_cancelled.py — permanently remove VOID/CANCELLED rows from all_signals.

Why this exists
----------------
VOID and CANCELLED signals are withdrawn or never-valid setups — not trades
that happened, and already excluded from every expectancy/win-rate figure
(badgeOf() classifies them separately from win/loss/open). Akshay asked for
them gone from the ledger entirely, not just hidden from the UI's default
view (2026-08-16).

Every matching row is dumped to data/backups/ BEFORE deletion — same
convention as the 2026-08-08 pre-regrade backup — so an accidental
misclassification is still recoverable from git history even though the
live table row is gone. Turso is the source of truth, so like regrade.py
this has to run where TURSO_URL/TURSO_TOKEN are the real production
secrets — a local run without them touches nothing.

Writes nothing unless --apply is passed.

Usage
-----
    python purge_void_cancelled.py            # report only, no writes
    python purge_void_cancelled.py --apply     # back up, then delete
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone

import db as _db
import tracker

STATUSES = ("VOID", "CANCELLED")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="delete after backing up (default: report only)")
    args = p.parse_args()

    tracker.init_db()
    placeholders = ",".join("?" * len(STATUSES))

    with _db.connect() as c:
        cur = c.execute(
            f"SELECT * FROM all_signals WHERE upper(COALESCE(status,'')) IN ({placeholders})",
            STATUSES,
        )
        # Not dict(row) — libsql's row type under Turso doesn't support the
        # mapping protocol sqlite3.Row does locally, so that only ever worked
        # against the local-fallback DB and TypeErrors against production.
        # Same cursor.description + zip pattern dedupe_positions.py already
        # uses for exactly this reason.
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, v)) for v in cur.fetchall()]

    by_status = {}
    for r in rows:
        s = (r.get("status") or "").upper()
        by_status[s] = by_status.get(s, 0) + 1

    print(f"Found {len(rows)} row(s) to purge: {by_status}")
    if not rows:
        print("Nothing to do.")
        return 0

    os.makedirs("data/backups", exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    backup_path = f"data/backups/all_signals_void_cancelled_deleted_{stamp}.json"
    with open(backup_path, "w") as f:
        json.dump(rows, f, indent=2, default=str)
    print(f"Backed up {len(rows)} row(s) to {backup_path}")

    if not args.apply:
        print("Dry run — no rows deleted. Re-run with --apply to delete.")
        return 0

    # libsql_experimental's execute() requires a tuple for parameters, not a
    # list — the SELECT above worked because STATUSES already is one; this
    # crashed with "'list' object cannot be converted to 'PyTuple'" before
    # any row was touched (caught by this same dry-run-first workflow).
    ids = tuple(r["id"] for r in rows)
    id_placeholders = ",".join("?" * len(ids))
    with _db.connect() as c:
        c.execute(f"DELETE FROM all_signals WHERE id IN ({id_placeholders})", ids)
        c.commit()
        _db.sync(c)

    print(f"Deleted {len(ids)} row(s) from all_signals.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
