#!/usr/bin/env python3
"""Slot-window regressions for standalone_scan.py.

Both defects here were silent: the workflows stayed green, the scans "ran",
and the site simply showed yesterday's signals.

1. 2026-09-04 — something dispatched slot=midday at 03:18 AM IST and again at
   04:03 AM IST. Explicit slots bypass the clock override (correct, for late
   crons), but nothing checked that the window had OPENED. Both runs marked
   midday complete for the day, so every real midday dispatch from 10:50 to
   12:50 IST stood down under --once. A full trading day produced no new
   equity signals.

   The _ORDER ladder cannot catch this: "full" is ranked above "midday" but
   covers pre-market AND post-market, so by that ordering 3 AM is "later" than
   midday. The guard has to compare against the window's opening time.

2. 10:31-10:59 IST matched no branch in _slot() and fell through to "full",
   which skips the measured equity scan. Half an hour of every trading day sat
   in a slot that does not scan.

Imported by extraction rather than by importing the module, because
standalone_scan.py opens a DB connection at import time.
"""
import ast
import sys

SRC = "standalone_scan.py"
WANT_FUNCS = {"_slot", "_before_window_opens"}
WANT_CONST = {"_SLOT_OPENS_IST"}


def _load():
    src = open(SRC).read()
    tree = ast.parse(src)
    lines = src.splitlines(True)
    chunks = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in WANT_FUNCS:
            chunks.append("".join(lines[node.lineno - 1:node.end_lineno]))
        elif isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") in WANT_CONST:
            chunks.append("".join(lines[node.lineno - 1:node.end_lineno]))
    ns = {}
    exec("".join(chunks), ns)
    missing = (WANT_FUNCS | WANT_CONST) - set(ns)
    if missing:
        raise SystemExit(f"could not extract {missing} from {SRC}")
    return ns["_slot"], ns["_before_window_opens"]


class Clock:
    """Minimal stand-in for a tz-aware datetime; weekday 3 = Thursday."""
    def __init__(self, hour, minute, wd=3):
        self.hour, self.minute, self._wd = hour, minute, wd

    def weekday(self):
        return self._wd


def main():
    _slot, _before = _load()
    passed = failed = 0

    def check(label, got, exp):
        nonlocal passed, failed
        if got == exp:
            passed += 1
            print(f"  PASS  {label}")
        else:
            failed += 1
            print(f"  FAIL  {label} — expected {exp!r}, got {got!r}")

    # ── _slot(): every trading minute belongs to a scanning slot ──────────
    for h, m, exp in [
        (9, 0, "morning"), (9, 59, "morning"), (10, 0, "morning"), (10, 30, "morning"),
        (10, 31, "midday"),   # defect 2: this was "full"
        (10, 45, "midday"), (10, 59, "midday"),
        (11, 0, "midday"), (13, 59, "midday"),
        (15, 0, "eod"), (17, 59, "eod"),
        (3, 18, "full"), (19, 0, "full"),
    ]:
        check(f"_slot {h:02d}:{m:02d} IST -> {exp}", _slot(Clock(h, m)), exp)

    check("Saturday is weekend", _slot(Clock(11, 0, wd=5)), "weekend")
    check("Sunday does not scan", _slot(Clock(11, 0, wd=6)), "none")

    # no minute of the session may land in "full"
    holes = [(h, m) for h in range(9, 18) for m in range(60)
             if _slot(Clock(h, m)) == "full"]
    check("no session minute falls through to 'full'", holes, [])

    # ── _before_window_opens(): early dispatch stands down ────────────────
    for slot, h, m, exp in [
        ("midday", 3, 18, True),    # defect 1: the run that burned today
        ("midday", 4, 3, True),
        ("midday", 10, 20, True),
        ("midday", 10, 31, False),  # window opens exactly here
        ("midday", 12, 50, False),
        ("midday", 16, 0, False),   # LATE is still allowed — that is the repair case
        ("morning", 8, 59, True),
        ("morning", 9, 0, False),
        ("eod", 14, 59, True),
        ("eod", 15, 0, False),
    ]:
        check(f"_before_window_opens {slot} @ {h:02d}:{m:02d} -> {exp}",
              _before(slot, Clock(h, m)), exp)

    # slots that are engine selectors, not times of day, are never gated
    for slot in ("full", "weekend", "holiday", "momentum", "nonsense"):
        check(f"{slot!r} is never window-gated", _before(slot, Clock(3, 0)), False)

    print(f"\n{passed} passed · {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
