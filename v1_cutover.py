"""Signal V1 is retired. This is the switch that makes it so in code, not
only in the schedule.

On 2026-10-01 Signal moved to V2: one private end-of-day engine (the
vision-engine repo) publishing one canonical plan feed, feeds/signal_v2.json,
with an empty forward record. Every V1 engine in this repo — BREACH, VECTOR,
TIDAL, LEDGE, KEEL, PIVOT, ASCENT, NORTH, GUST, BUOY/ANCHOR/BEDROCK, PLUMB,
cf_1h, commodity, ohl, the US daily picks and the Vision legacy engines — is
retired and must not file, grade into, or alert from the V1 ledger again.

Removing the crons is not enough on its own: workflow_dispatch, the Cloudflare
watchdog and a hand-run script can all still start a V1 job. So every V1 entry
point asks `v1_frozen()` first and stands down, and the one function that
writes the ledger refuses too. A test drives each of them.

The V1 rows are NOT deleted. They stay in the private database, copied into
v1_archive_* tables by .github/workflows/v1_archive.yml, which also ends open
V1 paper tracking with an archival status (no invented exits).

Rollback: set V1_UNFREEZE=1 for a deliberate restore run, or revert the
cutover commit (see vision-engine archive/signal-v1/RESTORE.md).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

# V2 Day 1 is 1 October 2026: nothing dated that day or later may come from
# V1. 00:00 IST = 18:30 UTC on 30 Sep. V2's own record of its activation lives
# in the private engine (registry/cutover.json), written once.
CUTOVER_AT = datetime(2026, 9, 30, 18, 30, tzinfo=timezone.utc)
REASON = ("Signal V1 retired 2026-10-01 — V2 publishes from the private engine "
          "(feeds/signal_v2.json). V1 jobs stand down; nothing is filed or alerted.")


def v1_frozen(now: datetime | None = None) -> bool:
    if os.environ.get("V1_UNFREEZE") == "1":
        return False
    return (now or datetime.now(timezone.utc)) >= CUTOVER_AT


def stand_down(job: str, log=None) -> bool:
    """True (and logs why) when the caller must exit without doing anything."""
    if not v1_frozen():
        return False
    (log or logging.getLogger(job)).warning(f"{job}: {REASON}")
    print(f"{job}: {REASON}", flush=True)
    return True
