#!/usr/bin/env python3
"""
prune_deployments.py — the deployment retention policy Vercel will not let a
Hobby project set.

WHY THIS EXISTS
---------------
Functions Storage is billed as (retained deployments x bundle size x days
retained). vercel-news ships TWELVE Node function bundles on every deployment
(`lambdaRuntimeStats: {"nodejs":12}`), and the newspaper commits to docs/
roughly sixteen times a day. At ~45.5 MB of function bundles per deployment
that reached 36.4 GB against a 10 GB Hobby cap — 99.5% of all Functions
Storage on the account, with no other project above 67 MB.

The documented fix is Settings > Security > Deployment Retention Policy. That
control is NOT RENDERED on a Hobby project, and the API refuses the field:

    PATCH /v9/projects/{id}  {"deploymentExpiration": {...}}
    -> "Invalid request: should NOT have additional property deploymentExpiration"

Vercel's own Academy page hedges on where it lives ("or wherever Deployment
Retention Policy lives in the current dashboard layout"). So there is no
setting to change and no API to call. Deleting the deployments directly is the
only lever left, and doing it on a timer is what makes it a policy rather than
a one-off cleanup.

WHAT IT WILL NOT DELETE
-----------------------
The live production and preview deployments are read from the project's own
`targets` and excluded before anything else happens. Vercel independently
protects the last 10 deployments created, the last 20 production deployments in
Ready state, the last 20 non-production ones, and anything holding an alias — a
DELETE against those returns 400/403 and is counted as skipped, never retried.
Ready deployments remain restorable for 30 days under Recently Deleted.

THE RATE LIMIT IS THE WHOLE REASON FOR THE BATCHING
---------------------------------------------------
Vercel caps deletions at 200 per 10 minutes and answers the 201st with
429 `now-rm`. A loop that ignores this does not slow down — it walks the
candidate list burning entries on 429s and leaves them undeleted while
reporting progress. Each round therefore re-enumerates from the API rather
than working through a list captured once, so anything a window rejected is
simply still a candidate next round.
"""
import json, os, sys, time, urllib.request, urllib.error

PROJECT = os.environ.get("VERCEL_PROJECT_ID", "prj_gGXq1iQwlxDbMTYo6Nb8bs29Q58I")
TEAM    = os.environ.get("VERCEL_TEAM_ID",    "team_OEe6X1ItfGRVOiVUJ9L3qJ79")
TOKEN   = os.environ.get("VERCEL_TOKEN")
# Two days holds ~32 deployments at the current ~16/day, about 1.5 GB. Raising
# this is the dial to turn if rollback depth ever matters more than headroom.
DAYS    = float(os.environ.get("PRUNE_KEEP_DAYS", "2"))
BATCH, WINDOW = 190, 630

if not TOKEN:
    sys.exit("VERCEL_TOKEN is not set — refusing to run rather than deleting nothing quietly.")

H = {"Authorization": f"Bearer {TOKEN}"}

def req(url, method="GET"):
    r = urllib.request.Request(url, headers=H, method=method)
    with urllib.request.urlopen(r, timeout=30) as f:
        return json.load(f)

def live_ids():
    proj = req(f"https://api.vercel.com/v9/projects/{PROJECT}?teamId={TEAM}")
    return {d["id"] for d in (proj.get("targets") or {}).values()
            if isinstance(d, dict) and d.get("id")}

def survey(live):
    deps, until = [], None
    while True:
        url = (f"https://api.vercel.com/v6/deployments?projectId={PROJECT}"
               f"&teamId={TEAM}&limit=100" + (f"&until={until}" if until else ""))
        page = req(url)
        got = page.get("deployments", [])
        if not got:
            break
        deps += got
        until = (page.get("pagination") or {}).get("next")
        if not until:
            break
    now = time.time() * 1000
    stale = [d for d in deps
             if d["uid"] not in live and (now - d["created"]) / 86400000 > DAYS]
    return deps, stale

def main():
    live = live_ids()
    print(f"live deployments held back: {sorted(live) or 'none'}", flush=True)
    deleted = skipped = 0
    for rnd in range(1, 13):                    # 12 windows = ~2h ceiling
        deps, stale = survey(live)
        print(f"[{rnd}] deployments={len(deps)} older_than_{DAYS:g}d={len(stale)}", flush=True)
        if not stale:
            print(f"DONE deleted={deleted} skipped={skipped} remaining={len(deps)}", flush=True)
            return 0
        for d in stale[:BATCH]:
            try:
                req(f"https://api.vercel.com/v13/deployments/{d['uid']}?teamId={TEAM}",
                    method="DELETE")
                deleted += 1
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    break                        # window spent; sleep it off
                skipped += 1                      # 400/403 = Vercel-protected
            except Exception:
                skipped += 1
            time.sleep(0.12)
        _, left = survey(live)
        if not left:
            print(f"DONE deleted={deleted} skipped={skipped}", flush=True)
            return 0
        print(f"[{rnd}] deleted={deleted} skipped={skipped} left={len(left)} "
              f"— sleeping {WINDOW}s for the rate-limit window", flush=True)
        time.sleep(WINDOW)
    print(f"::warning::stopped after 12 windows with work left; "
          f"deleted={deleted} skipped={skipped}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
