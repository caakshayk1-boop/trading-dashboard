"""
db.py — Central database connection factory.

Toggle via env vars:
  TURSO_URL   = libsql://your-db.turso.io   → uses Turso (persistent, cross-service)
  TURSO_TOKEN = your-auth-token

  If TURSO_URL is not set → falls back to local signals.db (dev / GitHub Actions).

All files use:
    import db
    con = db.connect()
    con.row_factory = db.Row
    ...
    db.sync(con)   # call after writes to push to Turso

Local replica path on Railway: /tmp/signals_replica.db  (ephemeral is fine —
Turso is the source of truth; the replica is rebuilt on each container start).
"""

from __future__ import annotations
import logging
import os
import sqlite3

log = logging.getLogger(__name__)

TURSO_URL   = os.environ.get("TURSO_URL", "")
TURSO_TOKEN = os.environ.get("TURSO_TOKEN", "")

# Local SQLite path — used when TURSO_URL is not set (dev / GitHub Actions)
_DATA_DIR = "/app/data" if os.path.isdir("/app/data") else os.path.dirname(os.path.abspath(__file__))
LOCAL_DB   = os.path.join(_DATA_DIR, "signals.db")

# Embedded replica path — used when TURSO_URL is set (Railway production)
# /tmp is fine: Turso is the source of truth, replica syncs on connect()
REPLICA_DB = "/tmp/signals_replica.db"


def _use_turso() -> bool:
    return bool(TURSO_URL and TURSO_TOKEN)


class _ConnWrapper:
    """
    Wraps a libsql connection to add context manager support.
    libsql_experimental.Connection doesn't implement __enter__/__exit__,
    but tracker.py uses `with _conn() as c:` extensively.
    """
    # Attributes belonging to the wrapper itself. Everything else set on this
    # object is meant for the connection underneath.
    _OWN = frozenset({"_conn", "_turso"})

    def __init__(self, conn, turso: bool = False):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_turso", turso)

    def __setattr__(self, name: str, value):
        """Forward attribute writes to the real connection.

        Reads were proxied, writes were not — so `con.row_factory = db.Row`
        set row_factory on the wrapper and left the connection at its default.
        Every keyed row access downstream (`row["picks"]`) then raised
        "tuple indices must be integers", inside except-blocks that turned a
        broken read into an empty result. It looked like no data rather than a
        bug, which is why it survived: the tables it touched were empty.

        Tolerant on purpose. libsql_experimental's Connection is a native type
        that rejects unknown attributes, so a hard forward would turn a silent
        no-op into an AttributeError on the Turso path — breaking the scanner
        and the newspaper build to fix a local-only annoyance. Falls back to
        the wrapper, which is exactly the old behaviour.

        Because of that fallback, callers must NOT assume row_factory took:
        read rows positionally, or map them by column name explicitly.
        """
        if name in self._OWN:
            object.__setattr__(self, name, value)
            return
        try:
            setattr(self._conn, name, value)
        except (AttributeError, TypeError):
            object.__setattr__(self, name, value)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            try:
                self._conn.commit()
            except Exception:
                pass
            if self._turso:
                try:
                    self._conn.sync()
                except Exception as e:
                    # A failed push here used to vanish with zero trace: the
                    # local replica commit still succeeds, so a caller with no
                    # explicit db.sync() check sees no exception and logs
                    # nothing either — a workflow can print "done" and exit 0
                    # while the write never reaches Turso at all (this is what
                    # happened to stock_screen.yml's 2026-08-15 run: it built
                    # fresh data, "cached" it locally, and the remote DB never
                    # saw it — the site kept serving 2026-08-12 for 5 days).
                    log.error(f"db sync failed on __exit__ — write may not have "
                              f"reached Turso: {e}")
        return False  # don't suppress exceptions

    # Proxy all other attribute access to the real connection
    def __getattr__(self, name: str):
        return getattr(self._conn, name)


def connect(timeout: int = 30) -> _ConnWrapper:
    """
    Returns a database connection wrapped for context manager support.
    - Turso (libsql embedded replica) when TURSO_URL is set.
    - Local sqlite3 otherwise.
    Call db.sync(conn) after writes to flush to Turso.
    """
    if _use_turso():
        try:
            import libsql_experimental as libsql
            conn = libsql.connect(REPLICA_DB, sync_url=TURSO_URL, auth_token=TURSO_TOKEN)
            conn.sync()   # pull latest from Turso before any operation
            return _ConnWrapper(conn, turso=True)
        except ImportError:
            log.warning("libsql_experimental not installed — falling back to local SQLite.")
        except Exception as e:
            log.warning(f"Turso connect failed ({e}) — falling back to local SQLite")

    # Local SQLite fallback
    raw = sqlite3.connect(LOCAL_DB, timeout=timeout, check_same_thread=False)
    try:
        raw.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    raw.execute("PRAGMA synchronous=NORMAL")
    raw.execute("PRAGMA cache_size=10000")
    return _ConnWrapper(raw, turso=False)


def sync(conn) -> None:
    """Push pending writes to Turso. No-op for local SQLite connections."""
    if not _use_turso():
        return
    try:
        conn.sync()
    except Exception as e:
        log.warning(f"db.sync error: {e}")


def reset_replica() -> None:
    """Delete the local embedded-replica file so the next connect() must pull
    fresh from Turso instead of reusing whatever this process already wrote.

    Every connect() in a process reuses the same REPLICA_DB path, so a
    "fresh connection" opened later in the same run is NOT independent proof
    that a write reached the remote database — it can still see the write
    purely from local replica state even if the push to Turso silently
    failed. Callers that need to verify a write actually landed on the
    remote (e.g. a publish job's read-back check) must call this first.
    """
    import glob
    for path in [REPLICA_DB] + glob.glob(REPLICA_DB + "-*"):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception as e:
            log.warning(f"reset_replica: could not remove {path}: {e}")


# Row factory — works for both libsql and sqlite3
Row = sqlite3.Row


def is_turso() -> bool:
    return _use_turso()
