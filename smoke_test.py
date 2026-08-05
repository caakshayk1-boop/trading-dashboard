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

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "https://news.askakshay.com"

# id → minimum sections that must render. Fewer than this and a page guard has
# broken, which is invisible in the HTML diff but obvious here.
PAGES = {
    "/": {"min_sections": 8, "must": ["world", "picks", "alerts"]},
    "/desk": {"min_sections": 11, "must": ["chess", "music", "gym"]},
}



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
    print(f"smoke: {BASE}\n")
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
        print(f"    js errors       {len(r['errors'])}")
        for e in r["errors"][:4]:
            print(f"      · {e}")

        if r["errors"]:
            fails.append(f"{path}: {len(r['errors'])} JS error(s) — {r['errors'][0][:90]}")
        if len(r["sections"]) < want["min_sections"]:
            fails.append(f"{path}: {len(r['sections'])} sections, expected {want['min_sections']}")
        for sid in want["must"]:
            if sid not in r["sections"]:
                fails.append(f"{path}: section '{sid}' missing")
        if not r["navMatchesDom"]:
            fails.append(f"{path}: nav order does not match document order")
        if (r.get("spy") or {}).get("mismatched"):
            fails.append(f"{path}: nav highlight wrong at "
                         + ", ".join(r["spy"]["mismatched"][:5]))
        if not r["hasCsp"]:
            fails.append(f"{path}: no Content-Security-Policy")
        # The two that would have caught the outage.
        if path == "/" and r["tickerSegments"] < 8:
            fails.append(f"/: ticker has {r['tickerSegments']} segments, expected 8+ "
                         "(a fallback to /api/markets looks like this)")
        if path == "/" and r["mapPainted"] < 10000:
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
