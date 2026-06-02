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


def connect(timeout: int = 30):
    """
    Returns a database connection.
    - Turso (libsql embedded replica) when TURSO_URL is set.
    - Local sqlite3 otherwise.

    The returned connection is sqlite3-API compatible in both cases.
    Call db.sync(conn) after writes to flush to Turso.
    """
    if _use_turso():
        try:
            import libsql_experimental as libsql
            conn = libsql.connect(REPLICA_DB, sync_url=TURSO_URL, auth_token=TURSO_TOKEN)
            conn.sync()   # pull latest from Turso before any operation
            return conn
        except ImportError:
            log.warning("libsql_experimental not installed — falling back to local SQLite. "
                        "Run: pip install libsql-experimental")
        except Exception as e:
            log.warning(f"Turso connect failed ({e}) — falling back to local SQLite")

    # Local SQLite fallback
    conn = sqlite3.connect(LOCAL_DB, timeout=timeout, check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=10000")
    return conn


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
