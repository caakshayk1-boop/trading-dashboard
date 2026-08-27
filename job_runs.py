"""job_runs — did this job run, when, and did it work.

Extracted from newspaper.py on 2026-08-27 for one reason: importing
newspaper.py pulls in flask, and the jobs that most need to record their own
outcome are cron jobs that do not install a web framework. The live proof:

    brief_already_sent(morning): No module named 'flask' — assuming NOT sent
    daily_brief: could not record the morning send (No module named 'flask')

Both halves of the brief's catch-up guard failed on an import. It failed OPEN,
so nothing went missing — but a guard that can never find a record is a guard
that can never stand down, and it would have re-sent the morning brief on
every catch-up cron, every day.

Depends on db.py and nothing else. Anything that can reach the database can
record what it did.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import db as _db_mod

log = logging.getLogger(__name__)


def _migrate(con) -> None:
    """job_runs, plus the two columns that make a partial build legible.

    ALTER per column inside its own try because SQLite has no ADD COLUMN IF
    NOT EXISTS and this runs on every write. Idempotent by construction.
    """
    con.execute("""CREATE TABLE IF NOT EXISTS job_runs (
        job TEXT PRIMARY KEY, run_at TEXT, status TEXT, detail TEXT)""")
    for ddl in ("ALTER TABLE job_runs ADD COLUMN records INTEGER",
                "ALTER TABLE job_runs ADD COLUMN expected INTEGER"):
        try:
            con.execute(ddl)
        except Exception:                               # noqa: BLE001
            pass


def record(job: str, status: str, detail: str = "",
           records: int | None = None, expected: int | None = None) -> bool:
    """Record the outcome of one attempt. Returns whether it was stored.

    Returns a bool rather than None because a caller that is about to rely on
    the record — a catch-up guard, say — needs to know the write landed. The
    old signature returned nothing, so a failed write was indistinguishable
    from a successful one at the call site.
    """
    try:
        con = _db_mod.connect()
        _migrate(con)
        con.execute(
            "INSERT OR REPLACE INTO job_runs "
            "(job, run_at, status, detail, records, expected) VALUES (?,?,?,?,?,?)",
            (job, datetime.now(timezone.utc).isoformat(), status, (detail or "")[:500],
             records, expected))
        con.commit()
        _db_mod.sync(con)
        return True
    except Exception as e:                              # noqa: BLE001
        log.warning("job_runs.record(%s): %s", job, e)
        return False


def latest(job: str) -> dict:
    """The last recorded attempt for `job`, or {} if there is none."""
    try:
        con = _db_mod.connect()
        _migrate(con)
        row = con.execute(
            "SELECT run_at, status, detail, records, expected "
            "FROM job_runs WHERE job=?", (job,)).fetchone()
        if not row:
            return {}
        return {"run_at": row[0], "status": row[1], "detail": row[2],
                "records": row[3], "expected": row[4]}
    except Exception as e:                              # noqa: BLE001
        log.warning("job_runs.latest(%s): %s", job, e)
        return {}
