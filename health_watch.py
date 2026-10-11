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
# The edition is STAMPED in MYT — generate.py builds with datetime.now(MYT)
# because the site is operated from Malaysia — so its date has to be read
# back in MYT. See the freshness check below for what comparing it against
# IST actually did.
MYT = timezone(timedelta(hours=8))
SITE = "https://news.askakshay.com"
ALPHA = os.environ.get("ALPHA_URL", "https://alpha.askakshay.com")
UA = {"User-Agent": "DailySignal-HealthWatch/1.0"}

# Each entry: label, url, and a predicate over the parsed body. The predicates
# are the point — a 200 that returns a degraded payload is the failure mode
# this file exists for.
CHECKS = [
    # alpha.askakshay.com is the product. Pages are static, so the checks that can catch a
    # real failure are the feeds it reads live and the quotes endpoint.
    ("alpha /", f"{ALPHA}/", None, lambda t: "<title>" in t and "alpha" in t.lower()),
    ("alpha screen", f"{ALPHA}/data/screen.json", "json", lambda j: len(j.get("rows", [])) > 500),
    ("alpha pulse", f"{ALPHA}/data/pulse.json", "json", lambda j: len(j.get("sectors", [])) > 0),
    ("alpha quotes", f"{ALPHA}/api/quotes", "json",
     lambda j: j.get("ok") and j.get("live", 0) >= 0.7 * max(1, j.get("total", 0))),
    ("alpha baro", f"{ALPHA}/data/barometer.json", "json",
     lambda j: isinstance((j.get("today") or {}).get("score"), (int, float))),
    ("alpha news", f"{ALPHA}/data/news.json", "json", lambda j: isinstance(j, list) and len(j) > 0),
    ("alpha ipo", f"{ALPHA}/data/ipo.json", "json", lambda j: isinstance(j.get("open"), list)),
    ("alpha funds", f"{ALPHA}/data/funds.json", "json", lambda j: len(j.get("categories", [])) > 0),
    # The newspaper and the careers site are still live and still watched.
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
    ("og card", f"{SITE}/og.png", None, lambda t: len(t) > 5000),
    ("sitemap", f"{SITE}/sitemap.xml", None, lambda t: "<loc>" in t),
]


# The newspaper builds once a day at 06:00 MYT (newspaper.yml, cron 0 22 UTC)
# and stamps the edition with that MYT date. Until it lands, yesterday's
# edition IS the current one. 07:30 leaves the build 90 minutes, because a
# GitHub cron routinely starts late.
EDITION_DUE_MYT = (7, 30)


def current_editions(now: datetime) -> set[str]:
    """The build dates that count as current at `now`: today's in MYT, and
    before the day's edition is due, yesterday's too.

    Comparing against today's MYT date alone failed this job twice in two
    days: the 15:00 UTC slot fired at 19:37 UTC (03:37 MYT) on 2 Oct and at
    18:23 UTC (02:23 MYT) on 3 Oct, read a correct edition from the day
    before, called it STALE and alerted the phone at night."""
    m = now.astimezone(MYT)
    ok = {m.date().isoformat()}
    if (m.hour, m.minute) < EDITION_DUE_MYT:
        ok.add((m.date() - timedelta(days=1)).isoformat())
    return ok


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
        # IST HERE FLAGGED FRESH EDITIONS AS STALE. build_date is written in
        # MYT, and MYT rolls over 2.5 hours before IST does, so for that window
        # every night the page correctly said tomorrow's date and this compared
        # it against today's. On 12 Sep 17:29Z the edition read 2026-09-13 —
        # right, it was 01:29 on the 13th in Malaysia — and this failed the job
        # against IST's 2026-09-12. Compare in the clock it was written in.
        today = datetime.now(MYT).date().isoformat()
        if built and built not in current_editions(datetime.now(MYT)):
            problems.append(f"*edition* — page still says {built}, today is {today}")
            lines.append(f"  edition      STALE ({built})")
        else:
            lines.append(f"  edition      ok ({built})")
    except Exception as e:
        problems.append(f"*edition* — unreadable: `{str(e)[:60]}`")

    # alpha's screen is rebuilt every evening, including weekends. Older than three days
    # means the pipeline behind every page on it has stopped.
    try:
        _, pulse = fetch(f"{ALPHA}/data/pulse.json", True)
        built = str(pulse.get("built_on", ""))
        age = (datetime.now(IST).date() - datetime.fromisoformat(built).date()).days
        if age > 3:
            problems.append(f"*alpha data* — screen last built {built}, {age} days ago")
            lines.append(f"  alpha data   STALE ({built})")
        else:
            lines.append(f"  alpha data   ok ({built})")
    except Exception as e:
        problems.append(f"*alpha data* — unreadable: `{str(e)[:60]}`")

    stamp = datetime.now(IST).strftime("%d %b %Y %I:%M %p IST")
    print(f"health_watch {stamp}")
    print("\n".join(lines))

    if problems:
        post(f"🚨 *alpha — health check failed*\n_{stamp}_\n\n"
             + "\n".join(f"• {p}" for p in problems)
             + "\n\n_Nothing else will tell you. That is why this exists._")
        return 1

    if "--always" in sys.argv:
        post(f"✅ *alpha healthy* — {stamp}\n"
             f"_{len(CHECKS)} checks, all passing._")
    print("all healthy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
