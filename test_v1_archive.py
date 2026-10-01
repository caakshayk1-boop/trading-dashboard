"""v1_archive.py against a scratch SQLite: archive, verify, end tracking,
restore. Offline. Run: python3 test_v1_archive.py"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import v1_archive as A  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'PASS' if cond else 'FAIL'}  {name} {'' if cond else detail}")


quiet = lambda *a: None  # noqa: E731
with tempfile.TemporaryDirectory() as td:
    c = sqlite3.connect(os.path.join(td, "t.db"))
    c.execute("CREATE TABLE all_signals (id INTEGER PRIMARY KEY, symbol TEXT, status TEXT, lifecycle_status TEXT,"
              " exit_price REAL, pnl_pct REAL, r_multiple REAL)")
    rows = [(1, "A", "OPEN", "Generated", None, None, None), (2, "B", "SL_HIT", "Generated", 95, -5, -1.0),
            (3, "C", "OPEN", "Generated", None, None, None), (4, "D", "T2_HIT", "Generated", 120, 20, 2.1)]
    c.executemany("INSERT INTO all_signals VALUES (?,?,?,?,?,?,?)", rows)
    c.execute("CREATE TABLE signals_4h (id INTEGER, x TEXT)")
    c.execute("INSERT INTO signals_4h VALUES (1,'y')")
    c.commit()
    before = A.checksum(c, "all_signals")

    check("end-tracking refuses without a verified archive", A.end_tracking(c, True, quiet) == 1)
    check("archive verifies", A.archive(c, quiet) == 0)
    check("archive is idempotent and never overwritten", A.archive(c, quiet) == 0
          and c.execute("SELECT COUNT(*) FROM v1_archive_manifest").fetchone()[0] == 2)
    check("dry run changes nothing", A.end_tracking(c, False, quiet) == 0 and A.checksum(c, "all_signals") == before)
    check("end-tracking archives the open rows", A.end_tracking(c, True, quiet) == 0)
    st = dict(c.execute("SELECT id, status FROM all_signals").fetchall())
    check("open -> ARCHIVED, closed untouched", st == {1: "ARCHIVED", 2: "SL_HIT", 3: "ARCHIVED", 4: "T2_HIT"}, st)
    ex = c.execute("SELECT exit_price, pnl_pct, r_multiple FROM all_signals WHERE id IN (1,3)").fetchall()
    check("no exit, P&L or R invented", all(x == (None, None, None) for x in ex), ex)
    check("no row deleted", c.execute("SELECT COUNT(*) FROM all_signals").fetchone()[0] == 4)
    check("archive still verifies after migration", A.verify(c, quiet) == 0)
    check("a re-run of archive keeps the PRE-migration copy",
          A.archive(c, quiet) == 0 and A.checksum(c, "v1_archive_all_signals") == before)
    A.restore_open(c, True, quiet)
    check("restore-open returns the exact pre-migration ledger", A.checksum(c, "all_signals") == before)
    c.execute("DELETE FROM signals_4h")
    c.commit()
    check("restore-table rebuilds a damaged table", A.restore_table(c, "signals_4h", True, quiet) == 0
          and c.execute("SELECT COUNT(*) FROM signals_4h").fetchone()[0] == 1)
    check("restore-table refuses an unknown table", A.restore_table(c, "users", True, quiet) == 1)
    c.close()

print(f"\n{PASS} passed · {FAIL} failed")
sys.exit(1 if FAIL else 0)
