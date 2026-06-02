#!/usr/bin/env python3
"""
scheduled_tasks_runner.py — GitHub Actions entry point for scheduled tasks.

Usage:
    python scheduled_tasks_runner.py cf_scan
    python scheduled_tasks_runner.py daily_brief

Replaces Railway bot scheduler. Called from .github/workflows/scheduled_tasks.yml.
"""

import sys
import logging
from datetime import datetime, timezone, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


def run_cf_scan():
    from claude_bot import _scan_commodity_forex
    ts = datetime.now(IST).strftime("%d %b %Y %I:%M %p IST")
    log.info(f"Running CF scan at {ts}")
    _scan_commodity_forex(ts, chat_id=None)
    log.info("CF scan complete")


def run_daily_brief():
    from daily_brief import send_brief
    log.info("Running daily brief")
    send_brief()
    log.info("Daily brief sent")


if __name__ == "__main__":
    task = sys.argv[1] if len(sys.argv) > 1 else "auto"

    if task == "cf_scan":
        run_cf_scan()
    elif task == "daily_brief":
        run_daily_brief()
    else:
        # Auto-detect by IST hour
        now_ist = datetime.now(IST)
        if now_ist.hour == 6 and now_ist.minute < 15:
            run_daily_brief()
        else:
            run_cf_scan()
