#!/usr/bin/env python3
"""
test_sip_engine.py — regression cover on the SIP money-weighted return.

xirr() drives the return figure the SIP section publishes and had no test at
all. That is the worst combination in this repo: an error-prone numerical
routine (Newton's method with a bisection fallback, irregular cashflows, sign
conventions) computing a number a reader takes at face value, with nothing
asserting it is right.

The failure mode is silent. A wrong XIRR does not crash, does not log, and
does not look wrong — it just publishes a return that is not the return.

Every expected value below is derived independently of the implementation:
either from a closed-form answer, or by checking that the rate actually zeroes
the net present value of the same cashflows.

Offline. No network, no database, no pytest.

Usage:
    python3 test_sip_engine.py
"""
from __future__ import annotations

import sys
from datetime import date, timedelta

from sip_engine import xirr

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


def npv(rate: float, flows) -> float:
    """Independent NPV — the definition XIRR must satisfy, not the code's."""
    t0 = min(d for d, _ in flows)
    return sum(a / (1 + rate) ** ((d - t0).days / 365.0) for d, a in flows)


D = date(2025, 1, 1)


# ── Closed-form cases ────────────────────────────────────────────────────────

@check("doubling in exactly one year is 100%")
def _():
    r = xirr([(D, -1000), (D + timedelta(days=365), 2000)])
    assert r is not None and abs(r - 1.0) < 1e-4, r


@check("no gain over any period is 0%")
def _():
    r = xirr([(D, -1000), (D + timedelta(days=365), 1000)])
    assert r is not None and abs(r) < 1e-6, r


@check("a 50% loss in one year is -50%")
def _():
    r = xirr([(D, -1000), (D + timedelta(days=365), 500)])
    assert r is not None and abs(r + 0.5) < 1e-4, r


@check("10% over two years annualises to ~4.88%, not 5%")
def _():
    # (1.10)^(1/2) - 1 = 0.048808…  A linear halving would give 5% and be wrong.
    r = xirr([(D, -1000), (D + timedelta(days=730), 1100)])
    assert r is not None and abs(r - 0.0488088) < 1e-4, r


# ── The property that defines XIRR ───────────────────────────────────────────

@check("the returned rate actually zeroes NPV for a monthly SIP")
def _():
    """A 24-month SIP with a terminal value — the real shape of this product.

    Asserted against the DEFINITION rather than a hardcoded number, so the
    test cannot be quietly re-baselined to whatever the code happens to emit.
    """
    flows = [(D + timedelta(days=30 * i), -5000) for i in range(24)]
    flows.append((D + timedelta(days=30 * 24), 150000))
    r = xirr(flows)
    assert r is not None, "no rate found for an ordinary SIP"
    assert abs(npv(r, flows)) < 1e-2, f"rate {r} leaves NPV {npv(r, flows)}"


@check("a lumpy, irregular series still zeroes NPV")
def _():
    # Newton diverges on exactly this shape, which is why the bisection
    # fallback exists. This is the case that proves the fallback works.
    flows = [(D, -10000), (D + timedelta(days=17), -2500),
             (D + timedelta(days=400), 900), (D + timedelta(days=402), -7000),
             (D + timedelta(days=800), 24000)]
    r = xirr(flows)
    assert r is not None
    assert abs(npv(r, flows)) < 1e-2, f"rate {r} leaves NPV {npv(r, flows)}"


@check("a heavy loss returns a large negative rate that still zeroes NPV")
def _():
    flows = [(D, -100000), (D + timedelta(days=365), 20000)]
    r = xirr(flows)
    assert r is not None and r < -0.75
    assert abs(npv(r, flows)) < 1e-2


# ── Refusing to answer ───────────────────────────────────────────────────────

@check("a single cashflow has no rate — returns None, never 0")
def _():
    # 0% would read as "flat", which is a claim. None is the honest answer.
    assert xirr([(D, -1000)]) is None
    assert xirr([]) is None


@check("all-negative or all-positive flows have no rate")
def _():
    assert xirr([(D, -1000), (D + timedelta(days=365), -500)]) is None
    assert xirr([(D, 1000), (D + timedelta(days=365), 500)]) is None


@check("total loss does not return a rate below -100%")
def _():
    # A rate at or under -1 makes (1+r)^t undefined. Returning None beats
    # publishing -340%.
    r = xirr([(D, -1000), (D + timedelta(days=365), 0.01)])
    assert r is None or r > -1.0, r


# ── Ordering and sign conventions ────────────────────────────────────────────

@check("cashflow order does not change the answer")
def _():
    flows = [(D, -1000), (D + timedelta(days=200), -1000),
             (D + timedelta(days=500), 2400)]
    assert abs(xirr(flows) - xirr(list(reversed(flows)))) < 1e-9


@check("scaling every cashflow leaves the rate unchanged")
def _():
    # A rate is scale-invariant. If ₹1,000 and ₹10,00,000 SIPs of the same
    # shape returned different rates, the routine would be wrong.
    small = [(D, -1000), (D + timedelta(days=365), 1200)]
    big = [(d, a * 1000) for d, a in small]
    assert abs(xirr(small) - xirr(big)) < 1e-6


@check("same-day cashflows are handled, not divided by zero")
def _():
    r = xirr([(D, -1000), (D, -1000), (D + timedelta(days=365), 2400)])
    assert r is not None and abs(r - 0.2) < 1e-3, r


def main() -> int:
    passed = failed = 0
    for name, fn in CHECKS:
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
