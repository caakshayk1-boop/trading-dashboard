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


# ── Target SPACING, not just target ORDER ────────────────────────────────────
# The validator above rejects targets that are equal or inverted. It cannot
# see the failure found live on 2026-08-18: TECHM published T1 1673.09 and
# T2 1678.17 against 23.88 of risk — correctly ordered, strictly increasing,
# and 0.2R apart. Ten of 157 open signals had it.
#
# Two targets 0.2R apart are one target printed twice. _structure_targets
# snaps T1 and T2 to nearby resistance independently, and nothing stopped both
# snapping onto the same wall.

import pandas as pd

from signals.indicators import MIN_TARGET_GAP_ATR, _structure_targets

TARGET_CHECKS = []


def tcheck(name):
    def deco(fn):
        TARGET_CHECKS.append((name, fn))
        return fn
    return deco


def highs(*peaks, base=1600.0, n=21):
    """A high series with resistance peaks planted where the snap will find
    them — rolling(20).max() and rolling(10).max().iloc[-2]."""
    ser = [base] * n
    for i, p in enumerate(peaks):
        ser[9 + i * 10] = p
    return pd.Series(ser)


@tcheck("the TECHM shape no longer collapses T1 and T2")
def _():
    # Two resistances five rupees apart, which is what produced 1673/1678.
    t1, t2, t3 = _structure_targets(1592.0, 54.0, highs(1681.5, 1686.6))
    risk = 1592.0 - 1568.12
    assert (t2 - t1) / risk >= 0.5, f"T1 {t1} T2 {t2} are {(t2 - t1) / risk:.2f}R apart"


@tcheck("consecutive targets are always at least the ATR floor apart")
def _():
    for atr in (5.0, 22.5, 54.0, 180.0):
        for peaks in ((1681.5, 1686.6), (1650.0, 1652.0), (1700.0, 1701.0)):
            t1, t2, t3 = _structure_targets(1592.0, atr, highs(*peaks))
            gap = MIN_TARGET_GAP_ATR * atr
            assert t2 - t1 >= gap - 0.01, f"atr={atr} peaks={peaks}: T1 {t1} T2 {t2}"
            assert t3 - t2 >= gap - 0.01, f"atr={atr} peaks={peaks}: T2 {t2} T3 {t3}"


@tcheck("targets stay strictly increasing — the existing invariant survives")
def _():
    for atr in (5.0, 54.0, 180.0):
        t1, t2, t3 = _structure_targets(1592.0, atr, highs(1681.5, 1686.6))
        assert 1592.0 < t1 < t2 < t3, (t1, t2, t3)


@tcheck("a clean structure is left alone — the floor only binds when it must")
def _():
    # No resistance anywhere near the raw R-multiples, so nothing snaps and
    # the 1.5 / 2.5 / 4.0 ATR ladder must come through untouched.
    t1, t2, t3 = _structure_targets(1000.0, 20.0, highs(3000.0, 3100.0))
    assert (t1, t2, t3) == (1030.0, 1050.0, 1080.0), (t1, t2, t3)


@tcheck("the floor pushes the OUTER target out, never pulls the inner one in")
def _():
    # T1 is anchored to the nearest real resistance and is the target most
    # likely to fill. Widening the gap must not move it.
    peaks = (1681.5, 1686.6)
    t1, _t2, _t3 = _structure_targets(1592.0, 54.0, highs(*peaks))
    assert abs(t1 - 1678.17) < 0.02, t1


@tcheck("a zero ATR cannot produce three identical targets")
def _():
    # Degenerate but reachable on a halted or untraded name. The floor is
    # zero here, so this pins that the result is still ordered, not equal.
    t1, t2, t3 = _structure_targets(100.0, 0.0, highs(500.0, 510.0))
    assert t1 <= t2 <= t3


# ── Allocations are not trades ───────────────────────────────────────────────

@tcheck("SIP allocations are exempt from the ordering gate")
def _():
    from tracker import ALLOCATION_TYPES, SIP_SIGNAL_TYPE
    assert SIP_SIGNAL_TYPE in ALLOCATION_TYPES


@tcheck("the SIP mirror writes NULL levels, not a manufactured ±0.1% ladder")
def _():
    """COALINDIA went into the ledger at entry 407.10, stop 406.69, T1 407.51.

    Numbers nobody decided, shaped like a trade plan so the ordering gate
    would accept them. A reader could not tell them from a real plan.
    """
    src = (__import__("pathlib").Path(__file__).parent / "tracker.py").read_text()
    block = src[src.index("An allocation has no stop or target"):]
    block = block[:block.index('"rr": None')]
    assert '"sl": None' in block, "the SIP mirror still writes a stop"
    for frag in ("0.999", "1.001", "1.002"):
        assert frag not in block, f"the ±0.1% placeholder {frag} is still there"


@tcheck("the gate still applies in full to anything that IS a trade")
def _():
    # The exemption must be narrow. A breakout with a missing stop stays
    # rejected — that is the SONACOMS-class bug this file exists for.
    ok, _ = v("BUY", 100, None, 110, 120)
    assert not ok


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

    for name, fn in TARGET_CHECKS:
        try:
            fn()
        except AssertionError as e:
            print(f"  FAIL  {name}  ({e})"); failed += 1
        except Exception as e:
            print(f"  ERROR {name}  ({type(e).__name__}: {e})"); failed += 1
        else:
            print(f"  PASS  {name}"); passed += 1

    print(f"\n{passed} passed · {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
