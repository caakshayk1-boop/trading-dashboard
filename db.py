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
    def __init__(self, conn, turso: bool = False):
        self._conn  = conn
        self._turso = turso

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
                except Exception:
                    pass
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


# Row factory — works for both libsql and sqlite3
Row = sqlite3.Row


def is_turso() -> bool:
    return _use_turso()
