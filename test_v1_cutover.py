"""Signal V1 cannot repopulate anything after the V2 cutover.

Every V1 entry point stands down, the one function that writes the V1 ledger
refuses, and no workflow keeps a schedule that starts a V1 publisher.
Offline: nothing here touches a database, the network or Telegram.
Run: python3 test_v1_cutover.py
"""
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

os.environ.pop("V1_UNFREEZE", None)
for k in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "GROQ_API_KEY"):
    os.environ.setdefault(k, "placeholder-not-a-secret")
os.environ.pop("TURSO_URL", None)
os.environ.pop("TURSO_TOKEN", None)
REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

import v1_cutover  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


# 1. the switch
check("frozen after the cutover", v1_cutover.v1_frozen(datetime(2026, 10, 2, tzinfo=timezone.utc)))
check("not frozen before it", not v1_cutover.v1_frozen(datetime(2026, 9, 30, tzinfo=timezone.utc)))
os.environ["V1_UNFREEZE"] = "1"
check("V1_UNFREEZE=1 lifts it, for a deliberate restore only",
      not v1_cutover.v1_frozen(datetime(2026, 10, 2, tzinfo=timezone.utc)))
os.environ.pop("V1_UNFREEZE")

# 2. the ledger writers refuse, without touching the database
import tracker  # noqa: E402
called = []
tracker.init_db = lambda *a, **k: called.append("init_db")
tracker._conn = lambda: called.append("conn")
rid = tracker.log_to_all_signals("RELIANCE", "breakout", "BUY", 100, 95, 105, 110, 115, 2.0)
check("log_to_all_signals files nothing", rid is None and not called, called)
ids = tracker.log_batch_to_all_signals([{"symbol": "A"}, {"symbol": "B"}])
check("log_batch_to_all_signals files nothing and keeps alignment", ids == [None, None] and not called, called)

check("SIP mirror stands down BEFORE its DELETE", tracker.log_sip_bucket([{"x": 1}], "2026-10") == [] and not called)
check("Top-5 mirror stands down BEFORE its DELETE", tracker.log_top5_picks([{"x": 1}], "2026-W40") == [] and not called)
check("V1 grading stands down", tracker.update_all_outcomes() is None and tracker.update_outcomes() is None
      and not called)

# 3. each V1 entry point stands down before doing anything
import standalone_scan  # noqa: E402
check("standalone_scan.main stands down", standalone_scan.main() == 0)
import vision_scan  # noqa: E402
check("vision_scan.run stands down", vision_scan.run() == 0)
import ai_longterm  # noqa: E402
check("ai_longterm.main stands down", ai_longterm.main() == 0)
import scan_research  # noqa: E402
scan_research.scan_offline = lambda: (_ for _ in ()).throw(AssertionError("scanned"))
try:
    scan_research.main()
    check("scan_research.main stands down", True)
except AssertionError as e:
    check("scan_research.main stands down", False, str(e))
for task in ("cf_scan", "ai_longterm", "auto"):
    r = subprocess.run([sys.executable, os.path.join(REPO, "scheduled_tasks_runner.py"), task],
                       capture_output=True, text=True, timeout=120, env={**os.environ})
    check(f"scheduled_tasks_runner {task} stands down", r.returncode == 0 and "retired" in (r.stdout + r.stderr),
          (r.stdout + r.stderr)[-300:])

# 4. no schedule starts a V1 publisher
WF = os.path.join(REPO, ".github", "workflows")


def crons(name):
    s = open(os.path.join(WF, name), encoding="utf-8").read()
    return re.findall(r"^\s*-\s*cron:\s*['\"]([^'\"]+)['\"]", s, re.M)


for wf in ("daily_scan.yml", "research.yml", "vision_scan.yml"):
    check(f"{wf} has no schedule", crons(wf) == [], crons(wf))
st = crons("scheduled_tasks.yml")
check("scheduled_tasks.yml keeps no CF-scan cron", "30 10 * * 1-5" not in st and "30 15 * * 1-5" not in st, st)

print(f"\n{PASS} passed · {FAIL} failed")
sys.exit(1 if FAIL else 0)
