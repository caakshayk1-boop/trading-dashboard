"""Signal V1 → V2 migration, inside the private database. Repeatable and reversible.

    python v1_archive.py archive            copy every V1 table to v1_archive_<table>,
                                            verify count + checksum, record a manifest
    python v1_archive.py verify             re-verify every archive against its source
    python v1_archive.py end-tracking       [--apply] OPEN V1 rows -> status ARCHIVED,
                                            lifecycle ARCHIVED_V1. Refuses unless the
                                            all_signals archive verifies first.
    python v1_archive.py restore-open       [--apply] undo end-tracking from the archive
    python v1_archive.py restore-table T    [--apply] replace table T from v1_archive_T

What it never does: delete a V1 row, invent an exit price, P&L or R, or touch
a V2 store (V2 lives in the private engine and feeds/signal_v2.json).

Idempotent. An archive that already exists is VERIFIED, never overwritten, so a
re-run after V1 rows were archived cannot replace the pre-migration copy with a
post-migration one. Prints counts and checksums only, never row contents, so
the public workflow log carries nothing private.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone

TABLES = ("all_signals", "signals", "signals_4h", "breakouts", "commodity_signals", "multibaggers",
          "signal_versions")
ARCHIVED_STATUS, ARCHIVED_LC = "ARCHIVED", "ARCHIVED_V1"


def _connect():
    import db
    return db.connect()


def _sync(c):
    try:
        import db
        db.sync(c)
    except Exception:
        pass


def _exists(c, t):
    return c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone() is not None


def _cols(c, t):
    return [r[1] for r in c.execute(f'PRAGMA table_info("{t}")').fetchall()]


def checksum(c, t, cols=None):
    """Order-independent: sha256 over the SORTED per-row hashes, so a copy made
    with a different physical order still verifies."""
    cols = cols or _cols(c, t)
    sel = ", ".join(f'"{x}"' for x in cols)
    hs = sorted(hashlib.sha256(json.dumps(list(r), default=str).encode()).hexdigest()
                for r in c.execute(f'SELECT {sel} FROM "{t}"').fetchall())
    return len(hs), hashlib.sha256("".join(hs).encode()).hexdigest()


def _manifest(c):
    c.execute("""CREATE TABLE IF NOT EXISTS v1_archive_manifest (
        tbl TEXT PRIMARY KEY, rows INTEGER, checksum TEXT, made_at TEXT)""")


def archive(c, log=print) -> int:
    _manifest(c)
    bad = 0
    for t in TABLES:
        if not _exists(c, t):
            log(f"  {t}: absent — nothing to archive")
            continue
        a = f"v1_archive_{t}"
        if not _exists(c, a):
            c.execute(f'CREATE TABLE "{a}" AS SELECT * FROM "{t}"')
            n, h = checksum(c, a)
            c.execute("INSERT OR REPLACE INTO v1_archive_manifest VALUES (?,?,?,?)",
                      (t, n, h, datetime.now(timezone.utc).isoformat(timespec="seconds")))
            c.commit()
            log(f"  {t}: archived {n} rows -> {a}")
        bad += _verify_one(c, t, log, against_source=True)
    _sync(c)
    return bad


def _verify_one(c, t, log, against_source):
    a = f"v1_archive_{t}"
    m = c.execute("SELECT rows, checksum FROM v1_archive_manifest WHERE tbl=?", (t,)).fetchone()
    n, h = checksum(c, a)
    ok = m is not None and (n, h) == (m[0], m[1])
    msg = f"  {t}: archive {n} rows sha256 {h[:16]} — manifest {'match' if ok else 'MISMATCH'}"
    if against_source:
        # the source may have moved on since (end-tracking changes status), so
        # only a never-migrated table is compared byte-for-byte
        sn, sh = checksum(c, t, _cols(c, a))
        if t != "all_signals" or not c.execute(
                "SELECT 1 FROM all_signals WHERE lifecycle_status=? LIMIT 1", (ARCHIVED_LC,)).fetchone():
            same = (sn, sh) == (n, h)
            ok = ok and same
            msg += f"; source {sn} rows {'equal' if same else 'DIFFERENT'}"
    log(msg)
    return 0 if ok else 1


def verify(c, log=print) -> int:
    _manifest(c)
    return sum(_verify_one(c, t, log, against_source=True) for t in TABLES
               if _exists(c, f"v1_archive_{t}"))


def end_tracking(c, apply=False, log=print) -> int:
    if not _exists(c, "v1_archive_all_signals") or _verify_one(c, "all_signals", log, against_source=False):
        log("end-tracking REFUSED: all_signals has no verified archive — run `archive` first")
        return 1
    n = c.execute("SELECT COUNT(*) FROM all_signals WHERE status='OPEN'").fetchone()[0]
    log(f"  open V1 rows to end with an archival status: {n}")
    if apply and n:
        c.execute("UPDATE all_signals SET status=?, lifecycle_status=? WHERE status='OPEN'",
                  (ARCHIVED_STATUS, ARCHIVED_LC))
        c.commit()
        _sync(c)
        left = c.execute("SELECT COUNT(*) FROM all_signals WHERE status='OPEN'").fetchone()[0]
        log(f"  done: {n} archived, {left} still OPEN; no exit, P&L or R was written")
        return 1 if left else 0
    return 0


def restore_open(c, apply=False, log=print) -> int:
    n = c.execute("SELECT COUNT(*) FROM all_signals WHERE lifecycle_status=?", (ARCHIVED_LC,)).fetchone()[0]
    log(f"  rows to restore from v1_archive_all_signals: {n}")
    if apply and n:
        c.execute("""UPDATE all_signals SET
            status = (SELECT a.status FROM v1_archive_all_signals a WHERE a.id = all_signals.id),
            lifecycle_status = (SELECT a.lifecycle_status FROM v1_archive_all_signals a WHERE a.id = all_signals.id)
            WHERE lifecycle_status = ?""", (ARCHIVED_LC,))
        c.commit()
        _sync(c)
    return 0


def restore_table(c, t, apply=False, log=print) -> int:
    if t not in TABLES or not _exists(c, f"v1_archive_{t}"):
        log(f"  restore-table REFUSED: no archive for {t}")
        return 1
    if _verify_one(c, t, log, against_source=False):
        log("  restore-table REFUSED: the archive does not match its manifest")
        return 1
    if apply:
        cols = ", ".join(f'"{x}"' for x in _cols(c, f"v1_archive_{t}"))
        c.execute(f'DELETE FROM "{t}"')
        c.execute(f'INSERT INTO "{t}" ({cols}) SELECT {cols} FROM "v1_archive_{t}"')
        c.commit()
        same = checksum(c, t, _cols(c, f"v1_archive_{t}")) == checksum(c, f"v1_archive_{t}")
        log(f"  {t}: restored, checksum {'equal' if same else 'DIFFERENT'}")
        _sync(c)
        return 0 if same else 1
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["archive", "verify", "end-tracking", "restore-open", "restore-table"])
    ap.add_argument("table", nargs="?")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    c = _connect()
    try:
        if a.mode == "archive":
            return archive(c)
        if a.mode == "verify":
            return verify(c)
        if a.mode == "end-tracking":
            return end_tracking(c, a.apply)
        if a.mode == "restore-open":
            return restore_open(c, a.apply)
        return restore_table(c, a.table, a.apply)
    finally:
        try:
            c.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
