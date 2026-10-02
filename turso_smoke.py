"""Read-only check that db.py reaches Turso DIRECTLY and works the way the
jobs use it. Runs in GitHub Actions (the only place the credentials live) on
any push that touches db.py, and by hand. Writes nothing.

    TURSO_URL=... TURSO_TOKEN=... python3 turso_smoke.py
"""
import os
import sys
import time

import db

fails = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


if not db.is_turso():
    print("TURSO_URL / TURSO_TOKEN not set — nothing to check")
    sys.exit(0)

check("mode is remote, not the embedded replica", db.TURSO_MODE == "remote" and not db._replica_mode(), db.TURSO_MODE)
t0 = time.time()
c = db.connect()
check("connected directly", type(c._conn).__module__ != "sqlite3", type(c._conn).__module__)
print(f"  connect: {time.time() - t0:.2f}s")

t = time.time()
tables = [r[0] for r in c.execute("select name from sqlite_master where type='table' order by name").fetchall()]
print(f"  {len(tables)} tables listed in {time.time() - t:.2f}s")
check("the database has its tables", len(tables) >= 5, len(tables))

lat = []
for name in tables[:25]:
    t = time.time()
    n = c.execute(f'select count(*) from "{name}"').fetchone()[0]
    lat.append(time.time() - t)
    print(f"    {name:40s} {n:>9,} rows  {lat[-1] * 1000:6.0f} ms")
lat.sort()
print(f"  query latency: median {lat[len(lat) // 2] * 1000:.0f} ms, max {lat[-1] * 1000:.0f} ms")
check("a query round trip is under 2 s", lat[-1] < 2.0, f"{lat[-1]:.2f}s")

# A job can hold a connection across a slow price download. The connection
# must still answer after sitting idle.
for idle in (30, 90):
    time.sleep(idle)
    try:
        r = c.execute("select 1").fetchone()[0]
        check(f"the connection answers after {idle}s idle", r == 1)
    except Exception as e:
        check(f"the connection answers after {idle}s idle", False, f"{type(e).__name__}: {e}")

with db.connect() as c2:
    check("the context manager works (as tracker uses it)", c2.execute("select 1").fetchone()[0] == 1)
db.sync(c)
check("no replica file was created", not os.path.exists(db.REPLICA_DB))

print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'all checks passed'}")
sys.exit(1 if fails else 0)
