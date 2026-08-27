#!/usr/bin/env python3
"""
test_header_collapse.py — the sticky header must not thrash on a trackpad.

The regression this exists for, reported 2026-08-27: "desktop website while
browsing the website too much trouble cant read properly as the screen keeps
on moving."

The header is in normal flow, so every height change MOVES the article below
it. The direction-sensitive collapse rule — collapse on scroll-down, expand on
scroll-up — was promoted from phones to every width earlier that day. A touch
swipe is directionally decisive; a wheel or trackpad is not. Every tiny
reversal re-expanded a 444px header and shoved the page under the cursor.

The fix is a monotonic rule on pointer devices: collapse past 240px, expand
below 100px, direction ignored. The 140px gap is hysteresis — without it the
boundary is itself a flicker zone.

This test measures TOGGLES over a realistic noisy trace. One toggle is a
transition the reader sees as the page moving.

Usage:
    python test_header_collapse.py
"""
from __future__ import annotations

import pathlib
import random
import sys

ROOT = pathlib.Path(__file__).resolve().parent


def trace() -> list[int]:
    """A read down the page on a trackpad: net downward, locally jittery.

    Seeded so a failure is reproducible. The jitter is the realistic part —
    momentum scrolling overshoots and corrects constantly, and it was those
    corrections, not deliberate scroll-ups, that made the page unreadable.
    """
    rnd = random.Random(20260827)
    y, out = 0, []
    for _ in range(600):
        y = max(0, y + rnd.randint(-14, 34))   # net down, frequent reversals
        out.append(y)
    return out


def toggles(rule) -> int:
    compact, n, last = False, 0, 0
    for y in trace():
        nxt = rule(y, y - last, compact)
        if nxt != compact:
            n += 1
        compact, last = nxt, y
    return n


def direction_rule(y, dy, compact):
    if abs(dy) < 6:
        return compact
    if y < 120:
        return False
    return dy > 0


def monotonic_rule(y, dy, compact):
    if y > 240:
        return True
    if y < 100:
        return False
    return compact


CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


@check("the old direction rule thrashes — this is the reported bug")
def _():
    n = toggles(direction_rule)
    assert n > 30, f"expected the old rule to thrash, got {n} toggles"


@check("the monotonic rule settles: at most a handful of moves per read")
def _():
    n = toggles(monotonic_rule)
    assert n <= 4, f"monotonic rule toggled {n} times — that is still moving"


@check("monotonic is at least 10x calmer than direction-sensitive")
def _():
    old, new = toggles(direction_rule), toggles(monotonic_rule)
    assert new * 10 <= old, f"direction {old} vs monotonic {new} — not enough better"


@check("hysteresis: nothing changes state between the two thresholds")
def _():
    # A reader parked mid-gap and jittering must see NO movement at all,
    # whichever state they arrived in.
    for start in (True, False):
        st = start
        for y in (101, 239, 150, 238, 102, 200):
            st = monotonic_rule(y, 0, st)
            assert st is start, f"state flipped at y={y} inside the hysteresis band"


@check("still collapses at all — a rule that never fires saves no space")
def _():
    st = monotonic_rule(900, 30, False)
    assert st is True
    assert monotonic_rule(10, -30, True) is False, "must reopen at the top"


@check("app.js ships the monotonic thresholds, not the direction rule")
def _():
    src = (ROOT / "static" / "app.js").read_text()
    assert "y > 240" in src and "y < 100" in src, \
        "app.js does not carry the monotonic thresholds this test validates"


@check("app.js branches on POINTER, not width alone")
def _():
    # The bug was a mouse wheel. A width-only test leaves a 1280px touchscreen
    # on the wrong rule and a 500px desktop window on the other wrong one.
    src = (ROOT / "static" / "app.js").read_text()
    assert "pointer:coarse" in src, "app.js does not consider the pointer type"


def main() -> int:
    failed = 0
    for name, fn in CHECKS:
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as e:
            print(f"FAIL  {name}\n      {e}")
            failed += 1
    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} passed")
    print(f"toggles over the same trace: direction={toggles(direction_rule)} "
          f"monotonic={toggles(monotonic_rule)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
