#!/usr/bin/env python3
"""Tests for the alert log. The dedupe rule is the whole point: if it is wrong
the forward sample inflates by however often the cron happens to run."""
import os, sys, json, tempfile
from datetime import datetime, timezone, timedelta
sys.path.insert(0, '/Users/akshaykumarkothari/Downloads/trading-dashboard')
import alert_log as AL

P = F = 0
def ok(n, c, d=""):
    global P, F
    if c: P += 1; print(f"  PASS  {n}")
    else: F += 1; print(f"  FAIL  {n}  {d}")

tmp = tempfile.mkdtemp()
AL.LOG = os.path.join(tmp, "alerts_log.json")
T0 = datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc)
A = [{"symbol": "SAIL", "lane": "strict", "entry": 100.0, "sl": 94.0},
     {"symbol": "MCX",  "lane": "reclaim", "entry": 2800.0, "sl": 2650.0}]

added, skipped = AL.record(A, "buoy", now=T0)
ok("a first scan logs every alert", len(added) == 2 and skipped == 0, (len(added), skipped))

added, skipped = AL.record(A, "buoy", now=T0 + timedelta(hours=4))
ok("the same scan four hours later adds NOTHING", len(added) == 0 and skipped == 2,
   (len(added), skipped))

rows = AL.load()["rows"]
ok("the repeat increments seen_count instead of adding a row",
   len(rows) == 2 and all(r["seen_count"] == 2 for r in rows), [r["seen_count"] for r in rows])

# a re-price inside the tolerance is the same setup
added, _ = AL.record([{"symbol": "SAIL", "lane": "strict", "entry": 100.3, "sl": 94.0}],
                     "buoy", now=T0 + timedelta(days=1))
ok("a 0.3% re-price is the same setup, not a new one", len(added) == 0, added)

# outside the tolerance it is a different level
added, _ = AL.record([{"symbol": "SAIL", "lane": "strict", "entry": 118.0, "sl": 110.0}],
                     "buoy", now=T0 + timedelta(days=1))
ok("a materially different level IS a new alert", len(added) == 1, added)

# a different lane on the same name is a different statement
added, _ = AL.record([{"symbol": "SAIL", "lane": "reclaim", "entry": 100.0, "sl": 94.0}],
                     "buoy", now=T0 + timedelta(days=1))
ok("the same name in the other lane is its own alert", len(added) == 1)

# a different engine is never deduped against another
added, _ = AL.record([{"symbol": "SAIL", "lane": "strict", "entry": 100.0, "sl": 94.0}],
                     "anchor", now=T0 + timedelta(days=1))
ok("another engine's alert on the same name is its own row", len(added) == 1)

# past the window it is a genuine re-alert, and it says what it repeats
added, _ = AL.record([{"symbol": "MCX", "lane": "reclaim", "entry": 2800.0, "sl": 2650.0}],
                     "buoy", now=T0 + timedelta(days=AL.DEDUPE_DAYS + 1))
ok("a re-alert after the window gets its own row", len(added) == 1)
ok("...and points back at the first", bool(added and added[0].get("repeat_of")), added)
ok("...with the gap recorded", bool(added and added[0].get("days_since_last") >= AL.DEDUPE_DAYS),
   added[0].get("days_since_last") if added else None)

# a corrupt file must not lose the scan
open(AL.LOG, "w").write("{not json")
added, _ = AL.record(A, "buoy", now=T0 + timedelta(days=30))
ok("a corrupt log is set aside, not silently overwritten",
   len(added) == 2 and os.path.exists(AL.LOG + ".corrupt"))

d = AL.load()
ok("the file stays valid JSON with counts per engine",
   d["ok"] and isinstance(d.get("counts"), dict) and d["counts"].get("buoy", 0) > 0, d.get("counts"))

print(f"\n{P} passed · {F} failed")
sys.exit(1 if F else 0)
