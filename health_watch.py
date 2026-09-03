#!/usr/bin/env python3
"""
health_watch.py — is production actually up, and is anything quietly degrading?

Why this exists
---------------
Three failures in one week shared a shape: a fallback fired, the system kept
running, and nothing said so.

  · Groq decommissioned a model  → every AI line fell back to a template
  · Bot handlers renamed         → three commands replied with an ImportError
  · One invalid CSS selector     → the ticker fell back to an older route

Each looked like normal output. None raised. The codebase is unusually good at
not crashing, and that is exactly why silent degradation is its failure mode.

This runs on a schedule and shouts once when something is wrong. It is
deliberately noisy about the specific things that have actually broken here
rather than generically checking "is it 200".

Usage:
    python health_watch.py            # check, alert on problems
    python health_watch.py --always   # alert even when healthy (for testing)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
SITE = "https://news.askakshay.com"
UA = {"User-Agent": "DailySignal-HealthWatch/1.0"}

# Each entry: label, url, and a predicate over the parsed body. The predicates
# are the point — a 200 that returns a degraded payload is the failure mode
# this file exists for.
CHECKS = [
    # `wmCanvas` was the world map's canvas. The map was deliberately removed
    # on 2026-08-26 and this check kept asserting it, so every run since has
    # reported "200 but the payload is degraded" — a false alarm about a
    # feature that was supposed to be gone. The smoke test had the same stale
    # assertion and was fixed; this file was missed.
    #
    # Replaced with elements that are load-bearing and are not going anywhere:
    # the ticker rail, and The Record band, which is the page's whole argument.
    # A health check must assert what MUST be true, never what merely happened
    # to be true the day it was written.
    ("page /", f"{SITE}/", None,
     lambda t: 'id="tickRail"' in t and 'id="record"' in t),
    # /desk WAS RETIRED ON 3 SEPTEMBER and this check outlived it, so the
    # health watch alerted twice a day about a page that had been deliberately
    # removed — "200 but the payload is degraded" at 12:56, then "404" once the
    # rebuild dropped the file. Both alerts were correct about the page and
    # wrong about there being a problem.
    #
    # An alert that fires for a change somebody made on purpose is worse than
    # no alert: it teaches the reader to ignore the channel, and the next
    # message on it will be the real one.
    #
    # The Life sections moved to career.askakshay.com, which is a separate
    # Worker with its own deploy check (scripts/check.mjs), so it is watched
    # where it lives rather than from here.
    ("career.askakshay.com", "https://career.askakshay.com", None,
     lambda t: "The Campaign" in t and 'id="scrollprog"' in t),
    ("ticker", f"{SITE}/api/ticker", "json",
     lambda j: j.get("ok") and len(j.get("segments", [])) >= 8),
    ("world", f"{SITE}/api/world", "json",
     lambda j: j.get("ok") and len(j.get("countries", [])) >= 5),
    ("stats", f"{SITE}/api/stats", "json",
     lambda j: j.get("ok") and (j.get("headline") or {}).get("trades", 0) > 0),
    ("health", f"{SITE}/api/health", "json",
     lambda j: j.get("ok") and j.get("signals", 0) > 0),
    ("og card", f"{SITE}/og.png", None, lambda t: len(t) > 5000),
    ("sitemap", f"{SITE}/sitemap.xml", None, lambda t: "<loc>" in t),
]


def fetch(url: str, as_json: bool):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        body = r.read()
        if as_json:
            return r.status, json.loads(body.decode())
        try:
            return r.status, body.decode("utf-8", "replace")
        except Exception:
            return r.status, body


def post(text: str) -> None:
    token = os.environ.get("TELEGRAM_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        print("(no telegram creds — printing only)")
        print(text)
        return
    data = json.dumps({"chat_id": chat, "text": text,
                       "parse_mode": "Markdown"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"telegram send failed: {e}")


def main() -> int:
    problems, lines = [], []

    for label, url, kind, ok in CHECKS:
        try:
            status, body = fetch(url, kind == "json")
        except Exception as e:
            problems.append(f"*{label}* — unreachable: `{str(e)[:70]}`")
            lines.append(f"  {label:<12} UNREACHABLE  {str(e)[:50]}")
            continue

        if status != 200:
            problems.append(f"*{label}* — HTTP {status}")
            lines.append(f"  {label:<12} HTTP {status}")
            continue

        try:
            healthy = bool(ok(body))
        except Exception as e:
            healthy = False
            lines.append(f"  {label:<12} check raised: {str(e)[:40]}")

        if not healthy:
            # This is the important branch. A 200 whose body is degraded is
            # precisely what every silent failure this week looked like.
            problems.append(f"*{label}* — 200 but the payload is degraded")
            lines.append(f"  {label:<12} DEGRADED (200)")
        else:
            lines.append(f"  {label:<12} ok")

    # Is the edition actually today's? A build that stopped running looks
    # completely healthy until you notice the date.
    try:
        _, ed = fetch(f"{SITE}/edition.json", True)
        built = str(ed.get("build_date", ""))
        today = datetime.now(IST).date().isoformat()
        if built and built != today:
            problems.append(f"*edition* — page still says {built}, today is {today}")
            lines.append(f"  edition      STALE ({built})")
        else:
            lines.append(f"  edition      ok ({built})")
    except Exception as e:
        problems.append(f"*edition* — unreadable: `{str(e)[:60]}`")

    stamp = datetime.now(IST).strftime("%d %b %Y %I:%M %p IST")
    print(f"health_watch {stamp}")
    print("\n".join(lines))

    if problems:
        post(f"🚨 *Daily Signal — health check failed*\n_{stamp}_\n\n"
             + "\n".join(f"• {p}" for p in problems)
             + "\n\n_Nothing else will tell you. That is why this exists._")
        return 1

    if "--always" in sys.argv:
        post(f"✅ *Daily Signal healthy* — {stamp}\n"
             f"_{len(CHECKS)} checks, all passing._")
    print("all healthy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
