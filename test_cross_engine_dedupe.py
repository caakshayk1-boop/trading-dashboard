#!/usr/bin/env python3
"""
test_cross_engine_dedupe.py — one position, two engines, one row.

The bug this locks down, from the live ledger on 2026-08-25:

    id=930  SONACOMS  breakout  entry 808.80  sl 796.67  SL_HIT  -1.50%
    id=931  SONACOMS  ohl       entry 808.80  sl 796.67  SL_HIT  -1.50%

Identical entry, identical stop, identical outcome, two rows. Every
expectancy figure counted that single loss twice, and the crore book would
have sized it twice. is_duplicate() could not see it: it keys on
symbol + signal_type, and these are two different signal_types.

Fourteen more pairs are in the ledger — magic/magicmagic, ai_longterm/
breakout, multibagger/breakout, ai_4h/4h — so this is not one engine
misbehaving. It is the write path having no cross-engine rule at all.

What must NOT be blocked matters as much: two engines wanting the same
symbol at DIFFERENT entries are two real trades with different risk.

Usage:
    python test_cross_engine_dedupe.py
"""
from __future__ import annotations

import sqlite3
import sys

from tracker import _cross_engine_duplicate

SCHEMA = """CREATE TABLE all_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT, signal_type TEXT, symbol TEXT, entry REAL, duplicate_note TEXT)"""

TODAY = "2026-08-25"


def db():
    c = sqlite3.connect(":memory:")
    c.execute(SCHEMA)
    # The real pair, verbatim.
    c.execute("INSERT INTO all_signals (date,signal_type,symbol,entry) VALUES (?,?,?,?)",
              (TODAY, "breakout", "SONACOMS", 808.80))
    # Same symbol, same day, a genuinely different level.
    c.execute("INSERT INTO all_signals (date,signal_type,symbol,entry) VALUES (?,?,?,?)",
              (TODAY, "breakout", "TATASTEEL", 150.00))
    # Yesterday. A closed position must not block today's re-entry.
    c.execute("INSERT INTO all_signals (date,signal_type,symbol,entry) VALUES (?,?,?,?)",
              ("2026-08-24", "breakout", "INFY", 1500.00))
    c.commit()
    return c


CASES = [
    # (name, symbol, entry, signal_type, expect_blocked)
    ("SONACOMS: ohl filing breakout's exact entry is ONE position",
     "SONACOMS", 808.80, "ohl", True),
    ("float noise in the 6th decimal must not defeat the match",
     "SONACOMS", 808.799999, "ohl", True),
    ("the SAME engine re-filing is is_duplicate()'s job, not this one",
     "SONACOMS", 808.80, "breakout", False),
    ("a different level is a different trade — must be allowed",
     "TATASTEEL", 162.40, "ohl", False),
    ("2dp is the tolerance: a 10-paise gap is a distinct entry",
     "SONACOMS", 808.90, "ohl", False),
    ("yesterday's row must not block today",
     "INFY", 1500.00, "ohl", False),
    ("an untouched symbol is never blocked",
     "RELIANCE", 1400.00, "ohl", False),
    ("an allocation with no entry price cannot collide",
     "SONACOMS", None, "multibagger", False),
]


def main() -> int:
    c, failed = db(), 0
    for name, sym, entry, st, want_blocked in CASES:
        got = _cross_engine_duplicate(c, sym, entry, TODAY, st)
        blocked = got is not None
        ok = blocked == want_blocked
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            print(f"      wanted blocked={want_blocked}, got {got!r}")

    # The collision must name the engine that got there first — a log line
    # saying only "duplicate" leaves you diffing the ledger by hand.
    hit = _cross_engine_duplicate(c, "SONACOMS", 808.80, TODAY, "ohl")
    ok = hit is not None and hit[1] == "breakout"
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'}  the clash reports WHICH engine already filed it")

    print(f"\n{len(CASES) + 1 - failed}/{len(CASES) + 1} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
