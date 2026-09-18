#!/usr/bin/env python3
"""
test_engine_names.py — the mirror test engine_names.py's docstring promised.

WHY THIS FILE EXISTS
--------------------
engine_names.py states in its own header that it is "a MIRROR of the JS
registry, and mirrors drift, so ... test_engine_names.py asserts every key
here exists in tracker.REMARKS".

That file did not exist. The sentence had been in the docstring since the
module was written, and every reader took the guarantee at face value. What
the missing test let through, found 2026-09-19:

  * signal.js retired magic, equity_measured and ai_longterm. Python did not
    know, so the bot and the brief still treated all three as live.
  * The site promoted `intraday` to a published engine named GUST. Python
    still listed it under LEDGER_ONLY — "in the ledger, not on the floor".
  * `pivot` / PIVOT had NEVER been in the Python mirror at all.
  * regime.py kept a SECOND retirement list, four keys stale since August.

What a reader saw: a front page reporting 65 published / 12 closed / 8.3% won
/ -0.746R — about twenty of those rows from engines the same page said were
retired — printed beside a regime panel reporting 11 closed / 9.1% on a
different population, with nothing to say which was the record. Neither was.
The correct population is 45 published, 11 closed, 9.1% won, -0.723R, t=-2.22.

A comment cannot hold two files in agreement. This can.

Dependency-free and offline, like test_engine_regressions.py beside it:
`python3 test_engine_names.py` is the whole contract.

ONE HONEST LIMIT, stated rather than implied. The cross-repo checks read
signal.js off disk. In this repo's CI that file is not checked out, so they
SKIP — and a skip is printed as a skip, never as a pass. They are a local
guard on the machine where the registry is actually edited, which is where
the drift happens; CI still runs every Python-internal check below.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import engine_names as en

FAILURES: list[str] = []
PASSES = 0
SKIPS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"  PASS  {name}")
    else:
        FAILURES.append(f"{name} — {detail}")
        print(f"  FAIL  {name}  {detail}")


def skip(name: str, why: str) -> None:
    global SKIPS
    SKIPS += 1
    print(f"  SKIP  {name}  ({why})")


# engines.js, not signal.js: the registry moved there on 2026-09-19 so the
# two browser bundles (signal.askakshay.com and gems.askakshay.com) stop
# keeping separate copies of it. It is pure data by design, which is also what
# makes it parseable from here.
SIGNAL_JS = Path(os.environ.get(
    "SIGNAL_JS",
    Path.home() / "Workspace/Apps/Websites/signal/public/engines.js"))


def js_registry() -> dict:
    """REGISTRY out of engines.js, as a dict.

    Parsed, not imported: this is Python reading a JavaScript object literal
    and there is no JS runtime here. The literal is plain data — strings,
    booleans, null — so a brace-balanced slice plus a few source-level
    substitutions reach valid JSON — plus one fold for the "..." + "..."
    concatenation `hunts` uses to stay inside the line width, which is the
    value of those two literals and not an evaluation. Anything MORE than
    that still raises rather than matching less of the object, because a
    registry that is no longer plain data needs a person, not a wider regex.
    """
    src = SIGNAL_JS.read_text(encoding="utf-8")
    start = src.index("{", src.index("var REGISTRY"))
    depth, end, in_str, i = 0, None, None, start
    while i < len(src):
        c = src[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
        elif c in "'\"":
            in_str = c
        elif src.startswith("/*", i):
            i = src.index("*/", i) + 2
            continue
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
        i += 1
    if end is None:
        raise ValueError("REGISTRY braces never closed")

    body = src[start:end]
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    body = re.sub(r"([{,]\s*)([A-Za-z_]\w*)\s*:", r'\1"\2":', body)
    body = body.replace("'", '"')
    # The one expression the literal does contain: `hunts` runs past the line
    # width and is written as "..." + "...". Two adjacent string literals joined
    # by + ARE their concatenation, so folding them is the value, not a guess.
    # Nothing else is evaluated — anything more than this still raises.
    body = re.sub(r'"\s*\+\s*"', "", body)
    body = re.sub(r",(\s*[}\]])", r"\1", body)
    return json.loads(body)


# ── the Python mirror, on its own ────────────────────────────────────────────

def test_live_is_derived_not_typed() -> None:
    expected = {k for k in en.ENGINE_NAMES
                if k not in en.RETIRED and k not in en.RESEARCH}
    check("LIVE is derived from ENGINE_NAMES, not typed a second time",
          set(en.LIVE) == expected,
          f"derived {sorted(expected)} vs LIVE {sorted(en.LIVE)}")


def test_retired_engines_keep_their_names() -> None:
    lost = [k for k in en.RETIRED
            if k not in en.LEDGER_ONLY and k not in en.ENGINE_NAMES]
    check("a retired engine keeps its published name",
          not lost, f"retired and un-nameable: {lost}")


def test_no_key_is_both_published_and_ledger_only() -> None:
    overlap = sorted(set(en.ENGINE_NAMES) & set(en.LEDGER_ONLY))
    check("no key is both a published engine and ledger-only",
          not overlap, f"claimed as both: {overlap}")


def test_retirement_dates_are_dates() -> None:
    bad = {k: v for k, v in en.RETIRED.items()
           if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(v))}
    check("every retirement carries a real date", not bad, f"{bad}")


def test_in_book_mirrors_the_browser() -> None:
    """engine_names.in_book must accept and reject exactly what
    ENGINE_BOOK.inBook does in engines.js — live engine, long only,
    rupee-priced, on or after LAUNCH.

    This is the predicate behind Telegram's /performance. Before it existed,
    that command called get_performance(), which counts "ALL signal types" —
    retired engines, pre-launch history, shorts nobody was sent, and COMEX
    gold in dollars — so the phone got a different record from every page.
    """
    base = {"signal_type": "keel", "action": "BUY", "currency": "\u20b9",
            "date": "2026-09-10"}
    cases = [
        (dict(base), True, "a live engine's long rupee trade after launch"),
        (dict(base, signal_type="magic"), False, "a retired engine"),
        (dict(base, signal_type="equity_measured"), False, "a retired engine"),
        (dict(base, signal_type="commodity"), False, "news.askakshay.com's engine"),
        (dict(base, signal_type="top5_pick"), False, "news.askakshay.com's engine"),
        (dict(base, action="SELL"), False, "a short, which was never sent"),
        (dict(base, currency="$"), False, "a dollar-priced row"),
        (dict(base, date="2026-09-01"), False, "the day before launch"),
        (dict(base, date=en.LAUNCH), True, "launch day itself is inclusive"),
    ]
    bad = [why for row, want, why in cases if en.in_book(row) is not want]
    check("in_book accepts and rejects the right rows", not bad,
          f"wrong verdict for: {bad}")


def test_every_live_engine_is_admitted() -> None:
    """A live engine that in_book rejects would vanish from the record
    silently — the same shape of bug, pointed the other way."""
    rejected = [k for k in en.LIVE
                if not en.in_book({"signal_type": k, "action": "BUY",
                                   "currency": "\u20b9", "date": "2026-09-10"})]
    check("every live engine is admitted to the book", not rejected,
          f"live but rejected: {rejected}")


def test_launch_matches_the_browser() -> None:
    if not SIGNAL_JS.exists():
        skip("LAUNCH matches the browser", f"engines.js not at {SIGNAL_JS}")
        return
    src = SIGNAL_JS.read_text(encoding="utf-8")
    m = re.search(r"var LAUNCH = '([0-9-]+)'", src)
    check("LAUNCH matches the browser", bool(m) and m.group(1) == en.LAUNCH,
          f"engines.js {m.group(1) if m else '(not found)'} vs python {en.LAUNCH}")


def test_research_engines_are_not_live() -> None:
    leaked = sorted(set(en.RESEARCH) & set(en.LIVE))
    check("no research-floor engine is counted as live",
          not leaked, f"leaked onto the floor: {leaked}")


# ── the mirror against the thing it mirrors ──────────────────────────────────

def cross_repo_checks() -> None:
    if not SIGNAL_JS.exists():
        for n in ("every JS engine is known to Python",
                  "names match exactly",
                  "retirement agrees in both directions",
                  "both sites publish the same engines",
                  "bands match where both carry one"):
            skip(n, f"signal.js not at {SIGNAL_JS}")
        return

    js = js_registry()

    unknown = [k for k in js
               if k not in en.ENGINE_NAMES and k not in en.LEDGER_ONLY]
    check("every JS engine is known to Python", not unknown,
          f"in signal.js and in no Python list: {unknown} "
          f"(an alert for one prints the raw key at a person)")

    wrong = {k: (en.ENGINE_NAMES[k][0], v["name"]) for k, v in js.items()
             if k in en.ENGINE_NAMES and en.ENGINE_NAMES[k][0] != v["name"]}
    check("names match exactly", not wrong,
          f"python vs site: {wrong}")

    js_ret = {k: v["retired"] for k, v in js.items() if v.get("retired")}
    py_ret = {k: v for k, v in en.RETIRED.items() if k in js}
    check("retirement agrees in both directions", js_ret == py_ret,
          f"signal.js {sorted(js_ret)} vs python {sorted(py_ret)}")

    js_live = {k for k, v in js.items() if not v.get("retired")}
    check("both sites publish the same engines", js_live == set(en.LIVE),
          f"only in signal.js: {sorted(js_live - set(en.LIVE))}; "
          f"only in python: {sorted(set(en.LIVE) - js_live)}")

    bands = {}
    for k, v in js.items():
        if k not in en.ENGINE_NAMES:
            continue
        py_b, js_b = en.ENGINE_NAMES[k][3], v.get("band")
        if py_b != js_b:
            bands[k] = (py_b, js_b)
    check("bands match where both carry one", not bands,
          f"TIDAL's two keys are told apart by band alone: {bands}")


def main() -> int:
    print("test_engine_names")
    for fn in (test_live_is_derived_not_typed,
               test_in_book_mirrors_the_browser,
               test_every_live_engine_is_admitted,
               test_launch_matches_the_browser,
               test_retired_engines_keep_their_names,
               test_no_key_is_both_published_and_ledger_only,
               test_retirement_dates_are_dates,
               test_research_engines_are_not_live):
        try:
            fn()
        except Exception as e:                                  # noqa: BLE001
            FAILURES.append(f"{fn.__name__} raised {type(e).__name__}: {e}")
            print(f"  FAIL  {fn.__name__}  raised {type(e).__name__}: {e}")
    try:
        cross_repo_checks()
    except Exception as e:                                      # noqa: BLE001
        FAILURES.append(f"cross_repo_checks raised {type(e).__name__}: {e}")
        print(f"  FAIL  cross_repo_checks  raised {type(e).__name__}: {e}")

    print(f"\n{PASSES} passed, {len(FAILURES)} failed, {SKIPS} skipped")
    if FAILURES:
        print("\nFAILURES")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
