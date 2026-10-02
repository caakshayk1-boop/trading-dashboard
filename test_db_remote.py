"""db.py connects to Turso DIRECTLY, never through an embedded replica,
unless TURSO_MODE=replica asks for the rollback. Offline: the libsql module is
replaced with a recorder, so this asserts what db.py ASKS for.

Why it matters: the replica lived in /tmp, which is empty at the start of
every GitHub Actions run, so every run re-downloaded the whole database.
September 2026: 38.16 GB synced, $10.15 of a $16.14 bill.

    python3 test_db_remote.py
"""
import importlib
import os
import sys
import types

ok_n = bad_n = 0


def ok(name, cond, detail=""):
    global ok_n, bad_n
    if cond:
        ok_n += 1
        print(f"  PASS  {name}")
    else:
        bad_n += 1
        print(f"  FAIL  {name}  {detail}")


class FakeConn:
    def __init__(self, calls):
        self.calls = calls

    def sync(self):
        self.calls.append("sync")

    def commit(self):
        self.calls.append("commit")

    def execute(self, *a):
        self.calls.append(("execute",) + a)


def load(mode=None):
    calls = []
    fake = types.ModuleType("libsql_experimental")

    def connect(database, **kw):
        calls.append(("connect", database, tuple(sorted(kw))))
        return FakeConn(calls)

    fake.connect = connect
    sys.modules["libsql_experimental"] = fake
    os.environ["TURSO_URL"] = "libsql://example.turso.io"
    os.environ["TURSO_TOKEN"] = "placeholder"
    if mode is None:
        os.environ.pop("TURSO_MODE", None)
    else:
        os.environ["TURSO_MODE"] = mode
    import db
    return importlib.reload(db), calls


# Default: remote. One connect to the libsql:// URL, no sync_url, no local file,
# no sync() anywhere — not on connect, not on exit, not from db.sync().
db, calls = load()
c = db.connect()
ok("default mode is remote", db.TURSO_MODE == "remote" and not db._replica_mode())
ok("remote connect opens the Turso URL itself, with no sync_url",
   calls[0] == ("connect", "libsql://example.turso.io", ("auth_token",)), calls[:1])
with c:
    c.execute("insert into t values (1)")
db.sync(c)
ok("a remote connection never syncs (connect, exit or db.sync)", "sync" not in calls, calls)
ok("...and its writes are committed", calls.count("commit") >= 1, calls)
ok("no replica file is named in remote mode", all(x[1] != db.REPLICA_DB for x in calls if isinstance(x, tuple) and x[0] == "connect"))

# Rollback: TURSO_MODE=replica restores the embedded replica exactly as before.
db, calls = load("replica")
c = db.connect()
ok("replica mode opens the local replica with a sync_url",
   calls[0] == ("connect", db.REPLICA_DB, ("auth_token", "sync_url")), calls[:1])
ok("replica mode pulls on connect", calls[1] == "sync", calls)
with c:
    pass
ok("replica mode pushes on exit", calls.count("sync") == 2, calls)

# No credentials: local SQLite, untouched.
for k in ("TURSO_URL", "TURSO_TOKEN", "TURSO_MODE"):
    os.environ.pop(k, None)
import db as _db
_db = importlib.reload(_db)
lc = _db.connect()
ok("no credentials means local SQLite", not _db._use_turso() and type(lc._conn).__module__ == "sqlite3")
lc.close()

print(f"\n{ok_n} passed, {bad_n} failed")
sys.exit(1 if bad_n else 0)
