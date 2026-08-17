#!/usr/bin/env python3
"""
test_signal_ordering.py — the hard invariant gate every signal must clear
before it reaches all_signals.

Covers the exact bug this was written for: SONACOMS and HINDALCO both
published with target1 == target2 (screenshots, 2026-08-14), traced to
_structure_targets() in signals/indicators.py. This test does not fix that
function — it proves the independent gate in tracker.py catches the bad
output regardless of what the generator does, now or after a future change.

Usage:
    python test_signal_ordering.py
"""
from __future__ import annotations

import sys
from tracker import _validate_signal_ordering as v

CASES = [
    # (name, args, expect_ok)
    ("SONACOMS bug: target1 == target2",
     ("BUY", 792.0, 780.12, 832.72, 832.72), False),
    ("HINDALCO bug: target1 == target2 (different symbol, same defect)",
     ("BUY", 1029.50, 1014.06, 1082.51, 1082.51), False),
    ("healthy LONG", ("BUY", 100, 90, 110, 120), True),
    ("healthy SHORT", ("SELL", 100, 110, 90, 80), True),
    ("missing stop-loss", ("BUY", 100, None, 110, 120), False),
    ("NaN target1", ("BUY", 100, 90, float("nan"), 120), False),
    ("negative stop-loss", ("BUY", 100, -5, 110, 120), False),
    ("zero entry price", ("BUY", 0, 90, 110, 120), False),
    ("inverted LONG: stop above entry", ("BUY", 100, 105, 110, 120), False),
    ("inverted LONG: target1 below entry", ("BUY", 100, 90, 95, 120), False),
    ("inverted SHORT: stop below entry", ("SELL", 100, 95, 90, 80), False),
    ("LONG with valid target3", ("BUY", 100, 90, 110, 120, 130), True),
    # target3 == target2 is the established "no distinct third target"
    # convention (run_4h_scan and others pass t3=t2 on purpose) — MUST be
    # accepted. A strict t2<t3 here broke this on 2026-08-17: it rejected
    # every 4h/intraday signal and, because this validator is exercised by
    # test_alert_pipeline.py's pre-flight fixtures BEFORE the real scan
    # runs, took down the entire day's signal generation.
    ("LONG target3 == target2 is the documented no-third-target convention, not a bug",
     ("BUY", 100, 90, 110, 120, 120), True),
    ("LONG target3 strictly worse than target2 IS still a real bug — stays rejected",
     ("BUY", 100, 90, 110, 120, 115), False),
    ("SHORT with valid target3", ("SELL", 100, 110, 90, 80, 70), True),
    ("SHORT target3 == target2 is also the no-third-target convention, not a bug",
     ("SELL", 100, 110, 90, 80, 80), True),
    ("SHORT target3 strictly worse than target2 IS still a real bug — stays rejected",
     ("SELL", 100, 110, 90, 80, 85), False),
    ("boundary: target1 one paisa below target2 (should pass, not equal)",
     ("BUY", 100, 90, 110.00, 110.01), True),
]


def main() -> int:
    passed = failed = 0
    for name, args, expect_ok in CASES:
        ok, reason = v(*args)
        got_ok = bool(ok)
        if got_ok == expect_ok:
            print(f"  PASS  {name}")
            passed += 1
        else:
            print(f"  FAIL  {name}  (expected ok={expect_ok}, got ok={got_ok}, reason={reason!r})")
            failed += 1

    print(f"\n{passed} passed · {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
