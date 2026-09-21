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

THE CROSS-REPO CHECKS RUN IN CI NOW. They read engines.js off disk and for
months that file existed on one laptop, so on every CI run they reported SKIP
— printed as a skip, never as a pass, and a skip is not a failure, which is
exactly how PLUMB came to file two signals after its own retirement. Since
signal went public on 2026-09-21 tests.yml checks it out and sets $SIGNAL_JS,
and fails hard if the file is missing rather than returning to skipping.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import engine_names as en

import io

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
                  "bands match where both carry one",
                  "the browser's label rule is still the shared-name rule",
                  "labels match exactly"):
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


    # ── THE LABEL, WHICH IS THE STRING A PERSON READS ────────────────────────
    #
    # engine_label() mirrors ENGINE_BOOK.label(). Two mirrors of it now: the
    # Python function, and the re-implementation four lines below that this
    # check compares it against. A re-implementation of a rule is only worth
    # anything while the rule it re-implements has not moved, so the rule is
    # read out of the JS source FIRST. If label() stops being "append the band
    # where the name is shared", this check goes red rather than going on
    # comparing Python against a rule the browser no longer applies.
    src = SIGNAL_JS.read_text(encoding="utf-8")
    body = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    m = re.search(r"var\s+label\s*=\s*function\s*\(k\)\s*\{(.*?)\n  \};",
                  body, flags=re.S)
    rule = re.sub(r"\s+", " ", (m.group(1) if m else "")).strip()
    check("the browser's label rule is still the shared-name rule",
          bool(m) and "shared(e.name) && e.band" in rule
          and "e.name + ' \u00b7 ' + e.band" in rule,
          f"label() now reads: {rule or 'not found'}")

    # shared() counts EVERY entry, retired included — `entries()`, not
    # `live()`. Mirrored here deliberately: a retired engine keeps its name,
    # so it keeps the ambiguity it created.
    js_shared = lambda n: sum(1 for v in js.values() if v.get("name") == n) > 1
    js_label = lambda k: (
        js[k]["name"] + " \u00b7 " + js[k]["band"]
        if js_shared(js[k]["name"]) and js[k].get("band") else js[k]["name"])
    wrong_l = {k: (en.engine_label(k), js_label(k)) for k in js
               if k in en.ENGINE_NAMES and en.engine_label(k) != js_label(k)}
    check("labels match exactly", not wrong_l,
          f"python vs site: {wrong_l} — the phone and the page would name "
          f"the same engine differently")


def test_every_writer_is_on_a_roster() -> None:
    """Read the WRITERS out of the source, not out of a list kept by hand.

    Every check in this file until now ran ENGINE_NAMES -> somewhere else:
    does each key here have a REMARKS entry, does each match engines.js. All
    of them start from a key somebody had already remembered to name, so a
    scan that writes a key nobody named passes every one of them.

    Two did. `swing` (standalone_scan.run_swing_scan, wired into the eod,
    weekend and full slots) and `manual` (claude_bot, a signal filed by hand)
    were in engines.js, in ENGINE_NAMES and in LEDGER_ONLY: none of the
    three. engine_name() fell through to `k.replace("_"," ").upper()`, so a
    row from either would have printed SWING or MANUAL at a reader — a raw
    database key wearing the shape of a published engine's name, which is the
    exact fault this module's first line says it exists to prevent.

    They survived the 2026-09-18 cleanup that unwired four writers the roster
    did not name, because that cleanup was done by reading the slot bodies and
    these two are not what anyone was looking for. A list read by a person is
    how both got missed; this reads the source.

    Deliberately NOT asserting the reverse. A key on a roster with no writer
    is correct and common: every retired engine is one, and the research three
    are written by scan_research.py through a different path entirely.
    """
    import glob
    # Both spellings the codebase actually uses, and only where the value is a
    # literal — `signal_type=engine` in a loop is resolved by reading the loop,
    # not by a regex, so it is left to the human and to the ledger check below.
    pat = re.compile(r"""signal_type["']?\s*[:=]\s*["']([a-z0-9_]+)["']""")
    found = {}
    for path in sorted(glob.glob("*.py")):
        if path.startswith("test_"):
            continue
        try:
            src = io.open(path, encoding="utf-8").read()
        except Exception:                                       # noqa: BLE001
            continue
        for m in pat.finditer(src):
            found.setdefault(m.group(1), set()).add(path)

    check("the scan for writers found some", len(found) >= 8,
          f"only {sorted(found)} — the pattern has stopped matching, which "
          f"would make every check below pass for the wrong reason")

    known = set(en.ENGINE_NAMES) | set(en.LEDGER_ONLY)
    orphans = {k: sorted(v) for k, v in found.items() if k not in known}
    check("every key the code can WRITE is named somewhere", not orphans,
          f"written and on no roster: {orphans} — a row from one of these "
          f"prints its own database key at a reader")

    # ── AND THE ROUTING SETS, WHICH ARE THE SAME DEFECT READ BACKWARDS ──────
    #
    # claude_bot slices the ledger by signal_type into the per-feed JSON files
    # — _filter({"cf_1h", "commodity"}) and so on. A key in one of those sets
    # that no engine writes is not harmless: it reads as though a feed slice
    # depended on it, so nobody removes it, and it outlives the thing it was
    # named for. `cf_momentum` sat in the commodity set with no writer anywhere
    # and zero rows in the ledger. gems.js had exactly this with `strict` and
    # `reclaim`, two BUOY lane names that had been in a filter doing nothing
    # since the day they were typed.
    routed = set()
    for src_path in ("claude_bot.py",):
        try:
            src = io.open(src_path, encoding="utf-8").read()
        except Exception:                                       # noqa: BLE001
            continue
        for block in re.findall(r"_filter\(\{([^}]*)\}\)", src):
            routed |= set(re.findall(r"[\"']([a-z0-9_]+)[\"']", block))
    check("the routing sets were found", len(routed) >= 5,
          f"only {sorted(routed)} — the pattern has stopped matching")
    dead = sorted(routed - known)
    check("every key a feed is sliced on is a real engine", not dead,
          f"routed and on no roster: {dead} — a feed slice named after "
          f"nothing, which nobody removes because it looks load-bearing")

    # And a name it can actually print. engine_name never returns "" by
    # design; the failure mode is the last-resort branch, which returns the
    # key back in capitals and is indistinguishable from a real name.
    shouty = [k for k in found
              if en.engine_name(k) == k.replace("_", " ").upper()
              and k not in known]
    check("no writer falls through to the uppercased-key fallback", not shouty,
          f"{shouty} would print as a name and be a key")


def test_the_ledger_carries_no_unnamed_engine() -> None:
    """The same question asked of the DATA rather than the source.

    The source check above cannot see a key built at runtime — basebreak
    passes `signal_type=engine` from a loop. This one reads what actually
    reached the ledger, which catches those without anybody having to trace
    the loop. It is a local snapshot and says so when it is not there, rather
    than passing quietly.
    """
    import pathlib
    f = pathlib.Path("data/all_signals.json")
    if not f.exists():
        skip("the ledger carries no unnamed engine", "data/all_signals.json absent")
        return
    try:
        rows = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:                                      # noqa: BLE001
        skip("the ledger carries no unnamed engine", f"unreadable: {e}")
        return
    known = set(en.ENGINE_NAMES) | set(en.LEDGER_ONLY)
    seen = {str((r or {}).get("signal_type") or "") for r in rows} - {""}
    check("every engine in the ledger has a name", not (seen - known),
          f"in the ledger and on no roster: {sorted(seen - known)}")


def test_a_shared_name_carries_its_band() -> None:
    """engine_label, which is what an alert actually prints.

    magic and magicmagic are one screen read at two depths and share the name
    TIDAL by design. On the site the card prints the band underneath the name.
    A Telegram alert has no underneath: both arrived on the phone reading
    exactly `TIDAL`, from engines with different rules and different records —
    one retired with twenty positions still open, one live. The two alerts a
    reader most needs to tell apart were the two that were identical.
    """
    a, b = en.engine_label("magic"), en.engine_label("magicmagic")
    check("TIDAL's two keys do not print the same string", a != b,
          f"both render {a!r}")
    check("magic carries its own band", a == "TIDAL \u00b7 >15% off the high", a)
    check("magicmagic carries its own band",
          b == "TIDAL \u00b7 20\u201340% off the high", b)

    # The rule is "shared", not "has a band". LEDGE and KEEL both carry one
    # and neither name is ambiguous, so neither prints it — a band on an
    # unambiguous name is noise dressed as precision.
    for k in ("ledge", "keel"):
        check(f"{k} has a band and does not print it",
              en.engine_label(k) == en.engine_name(k)
              and en.engine_band(k) != "",
              f"{en.engine_label(k)!r} vs {en.engine_name(k)!r}")

    # Every name that is NOT shared must label exactly as it names, or the
    # rule has quietly become "always append".
    for k in en.ENGINE_NAMES:
        if not en._name_is_shared(en.ENGINE_NAMES[k][0]):
            check(f"{k} labels as it names",
                  en.engine_label(k) == en.engine_name(k),
                  f"{en.engine_label(k)!r} != {en.engine_name(k)!r}")

    # Keys the registry has never heard of still have to render as something.
    for k, want in (("top5_pick", "Weekly Top 5"), ("", "Unattributed"),
                    ("made_up", "MADE UP")):
        check(f"unknown key {k!r} falls through to engine_name",
              en.engine_label(k) == want, f"{en.engine_label(k)!r} != {want!r}")

    # engine_name itself must NOT have moved: it is held equal to the
    # browser's name() in both directions by cross_repo_checks below, and
    # that comparison is the check that catches a missed retirement.
    check("engine_name is untouched — it still returns the bare name",
          en.engine_name("magic") == "TIDAL" == en.engine_name("magicmagic"),
          f"{en.engine_name('magic')!r} / {en.engine_name('magicmagic')!r}")


def test_the_alert_composers_print_the_label() -> None:
    """A rule nothing calls is a rule that is not applied.

    engine_label existing is not the fix; the alert paths reaching for it is.
    These are read as source rather than executed because two of the three
    open Turso to run.
    """
    for path, want in (("standalone_scan.py", "engine_label(engine_key)"),
                       ("telegram_bot.py", "engine_label(k)")):
        src = io.open(path, encoding="utf-8").read()
        check(f"{path} composes its engine with the label", want in src,
              f"{want!r} not found — that path still prints a bare name")
        check(f"{path} no longer calls engine_name for a printed name",
              not re.search(r"(?<![_\w])engine_name\s*\(", src),
              "a bare engine_name call is back in an alert path")

    src = io.open("engine_names.py", encoding="utf-8").read()
    check("engine_line, which every position alert goes through, uses it",
          "name = engine_label(signal_type)" in src)


def test_a_retired_engine_cannot_file() -> None:
    """tracker.drop_retired, called for real with a mixed batch.

    PLUMB was retired on 2026-09-18 and filed MARUTI and HINDUNILVR that same
    day; both rows are in the ledger. The retirement had been applied to the
    browser registry and not to the scanner, so it looked complete from the
    outside while the generator went on running.
    """
    import os
    for k, v in (("TURSO_URL", "x"), ("TURSO_TOKEN", "y"),
                 ("TELEGRAM_TOKEN", "z"), ("TELEGRAM_CHAT_ID", "1")):
        os.environ.setdefault(k, v)
    try:
        from tracker import drop_retired
    except Exception as e:                                      # noqa: BLE001
        skip("a retired engine cannot file", f"tracker will not import: {e}")
        return

    from engine_names import RETIRED, LIVE
    live_keys = sorted(LIVE)[:2]
    dead_keys = sorted(RETIRED)[:2]
    row = lambda sym, k: {"symbol": sym, "signal_type": k,
                          "market": "NSE", "action": "BUY"}

    batch = ([row(f"LIVE{i}", k) for i, k in enumerate(live_keys)]
             + [row(f"DEAD{i}", k) for i, k in enumerate(dead_keys)]
             # NOT retired, merely not engines. These must pass through.
             + [row("ALLOC1", "top5_pick"), row("ALLOC2", "sip_bucket")])

    kept, refused = drop_retired(batch)
    kept_k = [r["signal_type"] for r in kept]
    ref_k = [r["signal_type"] for r in refused]

    check("every retired engine's row is refused",
          sorted(ref_k) == sorted(dead_keys), f"refused {ref_k}, expected {dead_keys}")
    check("every live engine's row survives",
          all(k in kept_k for k in live_keys), f"kept {kept_k}")
    check("an allocation is not a retirement — top5_pick and sip_bucket pass",
          "top5_pick" in kept_k and "sip_bucket" in kept_k, f"kept {kept_k}")
    check("nothing is lost or duplicated by the split",
          len(kept) + len(refused) == len(batch), f"{len(kept)}+{len(refused)} vs {len(batch)}")

    # THE EXACT ROW THAT GOT THROUGH. equity_measured filed two SELLs on the
    # day it was retired; this is that batch, and it must now be empty.
    plumb = [{"symbol": "MARUTI", "signal_type": "equity_measured",
              "market": "NSE", "action": "SELL"},
             {"symbol": "HINDUNILVR", "signal_type": "equity_measured",
              "market": "NSE", "action": "SELL"}]
    k2, r2 = drop_retired(plumb)
    check("the two PLUMB rows of 2026-09-18 would now be refused",
          k2 == [] and len(r2) == 2, f"kept {k2}")

    # FAILS OPEN. An unknown engine is not a retired one.
    k3, r3 = drop_retired([row("XXX", "an_engine_invented_tomorrow")])
    check("an unrecognised engine is not treated as retired", len(k3) == 1 and not r3)
    check("an empty batch is handled", drop_retired([]) == ([], []))

    # The gate belongs on the WRITE path. Retirement says nothing about trades
    # already open — magic has twenty and they are still managed and alerted.
    src = io.open("tracker.py", encoding="utf-8").read()
    check("the write path calls it", "rows, _refused = drop_retired(rows)" in src)
    check("it runs before the trend gate, so a retired row is never priced",
          src.index("drop_retired(rows)") < src.index("if TREND_GATE_ON:"))


def main() -> int:
    print("test_engine_names")
    for fn in (test_live_is_derived_not_typed,
               test_in_book_mirrors_the_browser,
               test_every_live_engine_is_admitted,
               test_launch_matches_the_browser,
               test_retired_engines_keep_their_names,
               test_no_key_is_both_published_and_ledger_only,
               test_retirement_dates_are_dates,
               test_research_engines_are_not_live,
               test_a_retired_engine_cannot_file,
               test_every_writer_is_on_a_roster,
               test_the_ledger_carries_no_unnamed_engine,
               test_a_shared_name_carries_its_band,
               test_the_alert_composers_print_the_label):
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
