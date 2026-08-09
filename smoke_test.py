#!/usr/bin/env python3
"""
smoke_test.py — does the page a browser renders actually work?

Why this exists
---------------
On 2026-08-05 a one-line change shipped `querySelector("/desk")` into the nav
scroll spy. That throws a SyntaxError, which aborted the entire script block:
the segmented ticker silently fell back to an older nine-instrument route, the
world map stayed blank, the music crates and command palette never registered.

Every existing gate passed. Python compiled. The learning banks parsed. The
alert pipeline went eleven for eleven. Both APIs returned 200 with correct
payloads. The build was green and the page was dead, because nothing in this
repo had ever looked at a rendered page.

That is what this closes. It is deliberately not a full test suite — it asserts
the handful of things whose absence means the page is broken, and it fails the
build when they are missing.

Usage:
    python smoke_test.py                      # against production
    python smoke_test.py http://localhost:4321
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

# --pre runs against a LOCAL server serving freshly generated docs/, BEFORE
# anything is committed. It drops the assertions that need the live /api layer
# (ticker segments, world map) and keeps the ones that catch a dead page — JS
# errors, missing sections, broken scroll spy.
#
# This exists because the full check runs after publish, so a red result has
# always meant "the broken page is already live". On 2026-08-08 the main page
# served with an aborted script block for ~7 minutes for exactly that reason.
PRE = "--pre" in sys.argv
_args = [a for a in sys.argv[1:] if not a.startswith("--")]
BASE = _args[0].rstrip("/") if _args else "https://news.askakshay.com"

# id → minimum sections that must render. Fewer than this and a page guard has
# broken, which is invisible in the HTML diff but obvious here.
PAGES = {
    "/": {"min_sections": 8, "must": ["world", "picks", "alerts"]},
    "/desk": {"min_sections": 11, "must": ["chess", "music", "gym"]},
}

# python -m http.server has no extensionless routing, so /desk is a 404 there
# while Vercel rewrites it to desk.html. Ask for the file directly in --pre.
if PRE:
    PAGES = {("/desk.html" if k == "/desk" else k): v for k, v in PAGES.items()}



def main() -> int:
    # A real file beside this one, not a temp: it resolves node_modules from
    # the repo, and the assertions belong in review like any other code.
    script = str(pathlib.Path(__file__).parent / "smoke_runner.cjs")
    if not pathlib.Path(script).exists():
        print("SKIP  smoke_runner.cjs missing")
        return 0

    try:
        p = subprocess.run(["node", script, BASE, json.dumps(PAGES)],
                           capture_output=True, text=True, timeout=180)
    except FileNotFoundError:
        print("SKIP  node not available")
        return 0
    except subprocess.TimeoutExpired:
        print("FAIL  smoke run timed out")
        return 1

    if p.returncode != 0:
        print(f"FAIL  runner: {(p.stderr or '').strip()[:400]}")
        return 1

    try:
        data = json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        print(f"FAIL  unparseable output: {p.stdout[:300]}")
        return 1

    fails = []
    print(f"smoke{' [pre-publish]' if PRE else ''}: {BASE}\n")
    for path, r in data["pages"].items():
        want = r["want"]
        print(f"  {path}")
        print(f"    title           {r['title'][:62]}")
        print(f"    sections        {len(r['sections'])} (need {want['min_sections']})")
        print(f"    nav == dom      {r['navMatchesDom']}")
        print(f"    ticker segments {r['tickerSegments']}")
        print(f"    ticker items    {r['tickerItems']}")
        print(f"    map pixels      {r['mapPainted']}")
        print(f"    dom nodes       {r['domNodes']}")
        spy = r.get("spy") or {}
        print(f"    scroll spy      {spy.get('checked', 0)} checked, "
              f"{len(spy.get('mismatched', []))} wrong "
              f"{','.join(spy.get('mismatched', [])[:4])}")
        print(f"    js errors       {len(r['errors'])}"
              + (f"  ({len([e for e in r['errors'] if not e.startswith('console:')])} fatal)"
                 if PRE else ""))
        for e in r["errors"][:4]:
            print(f"      · {e}")

        # In --pre the page is served from a plain static folder: there is no
        # /api layer and no Vercel rewrite, so every live call and the /desk
        # route 404. Those are console resource errors, not JS exceptions, and
        # failing on them would make the gate cry wolf until it got disabled.
        # An uncaught exception (pageerror) is fatal in both modes — that is
        # the class that aborts the whole script block.
        errs = [e for e in r["errors"]
                if not PRE or not e.startswith("console:")]
        if errs:
            fails.append(f"{path}: {len(errs)} JS error(s) — {errs[0][:90]}")
        if len(r["sections"]) < want["min_sections"]:
            fails.append(f"{path}: {len(r['sections'])} sections, expected {want['min_sections']}")
        for sid in want["must"]:
            if sid not in r["sections"]:
                fails.append(f"{path}: section '{sid}' missing")
        if not r["navMatchesDom"]:
            fails.append(f"{path}: nav order does not match document order")
        if not PRE and (r.get("spy") or {}).get("mismatched"):
            fails.append(f"{path}: nav highlight wrong at "
                         + ", ".join(r["spy"]["mismatched"][:5]))
        if not r["hasCsp"]:
            fails.append(f"{path}: no Content-Security-Policy")
        # The two that would have caught the outage. Both need the live API,
        # so they are skipped in --pre where only the static shell exists.
        if not PRE and path == "/" and r["tickerSegments"] < 8:
            fails.append(f"/: ticker has {r['tickerSegments']} segments, expected 8+ "
                         "(a fallback to /api/markets looks like this)")
        if not PRE and path == "/" and r["mapPainted"] < 10000:
            fails.append(f"/: world map painted {r['mapPainted']} px, expected 10000+")
        print()

    if fails:
        print("FAILED")
        for f in fails:
            print(f"  · {f}")
        return 1
    print("smoke: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
