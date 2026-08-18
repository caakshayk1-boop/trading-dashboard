#!/usr/bin/env python3
"""
test_signal_history.py — a signal's past cannot be overwritten.

The ledger already publishes corrections openly: a re-grade moved published
expectancy from +0.090R to -0.182R and the site said so. What it could not do
was show what a signal looked like BEFORE the correction — the row was updated
in place, so the original statement was gone.

This proves the archive works, and specifically that it works through the
DATABASE rather than through caller discipline. Sixteen places update
all_signals across twelve files; a history that depends on each of them
remembering to snapshot is only as good as the least careful one.

Runs against a scratch SQLite file, not the real ledger. No network, no pytest.

Usage:
    python3 test_signal_history.py
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tracker import VERSIONED_COLUMNS, _ensure_signal_versions

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


def fresh():
    """A scratch ledger with the trigger installed."""
    con = sqlite3.connect(tempfile.mktemp(suffix=".db"))
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE all_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, signal_type TEXT,
        entry REAL, sl REAL, target1 REAL, target2 REAL, target3 REAL, rr REAL,
        status TEXT, exit_price REAL, pnl_pct REAL, r_multiple REAL,
        grade TEXT, score REAL, lifecycle_status TEXT, engine_version TEXT,
        sent_at TEXT, alert_flags TEXT)""")
    _ensure_signal_versions(con)
    con.execute("""INSERT INTO all_signals
        (id, symbol, signal_type, entry, sl, target1, target2, target3, rr,
         status, grade, score, engine_version)
        VALUES (1,'TECHM','breakout',1592,1568.12,1673.09,1678.17,1744.57,3.4,
                'OPEN','A',82,'v3')""")
    con.commit()
    return con


def versions(con, sid=1):
    return con.execute(
        "SELECT * FROM signal_versions WHERE signal_id=? ORDER BY version_id",
        (sid,)).fetchall()


@check("a fresh signal has no history — the current row IS the statement")
def _():
    con = fresh()
    assert len(versions(con)) == 0


@check("changing the status archives what the engine said before")
def _():
    con = fresh()
    con.execute("UPDATE all_signals SET status='CLOSED', exit_price=1700 WHERE id=1")
    con.commit()
    v = versions(con)
    assert len(v) == 1
    assert v[0]["status"] == "OPEN", "archived the NEW value instead of the old"
    assert v[0]["exit_price"] is None


@check("the archive keeps the ORIGINAL levels after a re-grade rewrites them")
def _():
    con = fresh()
    con.execute("UPDATE all_signals SET target2=1727.0, r_multiple=2.1 WHERE id=1")
    con.commit()
    v = versions(con)
    assert v[0]["target2"] == 1678.17, "the original target was not preserved"


@check("each successive change appends — nothing is replaced")
def _():
    con = fresh()
    con.execute("UPDATE all_signals SET status='TRIGGERED' WHERE id=1")
    con.execute("UPDATE all_signals SET status='CLOSED', pnl_pct=5.1 WHERE id=1")
    con.execute("UPDATE all_signals SET pnl_pct=4.8 WHERE id=1")
    con.commit()
    v = versions(con)
    assert len(v) == 3, f"expected 3 versions, got {len(v)}"
    assert [r["status"] for r in v] == ["OPEN", "TRIGGERED", "CLOSED"]


@check("history is ordered oldest first")
def _():
    con = fresh()
    con.execute("UPDATE all_signals SET score=70 WHERE id=1")
    con.execute("UPDATE all_signals SET score=60 WHERE id=1")
    con.commit()
    v = versions(con)
    assert [r["score"] for r in v] == [82, 70]


# ── The rule that keeps it readable ──────────────────────────────────────────

@check("a no-op update writes no version — history holds changes, not activity")
def _():
    con = fresh()
    con.execute("UPDATE all_signals SET status='OPEN', grade='A' WHERE id=1")
    con.commit()
    assert len(versions(con)) == 0


@check("message bookkeeping is not versioned")
def _():
    # sent_at and alert_flags record what happened to the MESSAGE, not what the
    # engine claimed about the trade. Versioning them buries the real changes.
    con = fresh()
    con.execute("UPDATE all_signals SET sent_at='2026-08-18T10:00', alert_flags='x' WHERE id=1")
    con.commit()
    assert len(versions(con)) == 0


@check("NULL -> value counts as a change")
def _():
    # IFNULL sentinel: a plain OLD.x IS NOT NEW.x comparison on NULLs is the
    # classic way a trigger silently skips the first exit price ever written.
    con = fresh()
    con.execute("UPDATE all_signals SET exit_price=1700 WHERE id=1")
    con.commit()
    assert len(versions(con)) == 1


@check("value -> NULL counts as a change too")
def _():
    con = fresh()
    con.execute("UPDATE all_signals SET grade=NULL WHERE id=1")
    con.commit()
    v = versions(con)
    assert len(v) == 1 and v[0]["grade"] == "A"


# ── It cannot be bypassed ────────────────────────────────────────────────────

@check("a writer that never heard of the archive is still archived")
def _():
    """The whole reason this is a trigger.

    This UPDATE mimics regrade.py: raw SQL, no import of any helper, no
    knowledge that versioning exists. It must still leave a trace.
    """
    con = fresh()
    con.execute("UPDATE all_signals SET status=?,r_multiple=?,pnl_pct=? WHERE id=?",
                ("CLOSED", -1.0, -3.2, 1))
    con.commit()
    assert len(versions(con)) == 1


@check("a multi-row update versions every row it touched")
def _():
    con = fresh()
    con.execute("""INSERT INTO all_signals (id,symbol,entry,sl,status,engine_version)
                   VALUES (2,'IOC',137.8,130,'OPEN','v3')""")
    con.commit()
    con.execute("UPDATE all_signals SET status='CANCELLED' WHERE status='OPEN'")
    con.commit()
    assert len(versions(con, 1)) == 1 and len(versions(con, 2)) == 1


@check("installing the trigger twice is safe")
def _():
    con = fresh()
    _ensure_signal_versions(con)
    _ensure_signal_versions(con)
    con.execute("UPDATE all_signals SET status='CLOSED' WHERE id=1")
    con.commit()
    # A duplicated trigger would write the same version twice.
    assert len(versions(con)) == 1


@check("every versioned column is actually stored in the archive")
def _():
    con = fresh()
    cols = {r[1] for r in con.execute("PRAGMA table_info(signal_versions)")}
    missing = [c for c in VERSIONED_COLUMNS if c not in cols]
    assert not missing, f"versioned but not archived: {missing}"


@check("deleting a signal does not erase its history")
def _():
    con = fresh()
    con.execute("UPDATE all_signals SET status='CLOSED' WHERE id=1")
    con.commit()
    con.execute("DELETE FROM all_signals WHERE id=1")
    con.commit()
    assert len(versions(con)) == 1, "history vanished with the row"


def main() -> int:
    passed = failed = 0
    for name, fn in CHECKS:
        try:
            fn()
        except AssertionError as e:
            print(f"  FAIL  {name}  ({e})"); failed += 1
        except Exception as e:
            print(f"  ERROR {name}  ({type(e).__name__}: {e})"); failed += 1
        else:
            print(f"  PASS  {name}"); passed += 1
    print(f"\n{passed} passed · {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
