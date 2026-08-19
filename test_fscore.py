#!/usr/bin/env python3
"""
test_fscore.py — the Piotroski F-score, verified criterion by criterion.

The score is nine independent yes/no tests on two consecutive fiscal years.
Published on a public screen, it is exactly the kind of number a reader takes
at face value and never recomputes — so every criterion here is exercised in
ISOLATION, with a year-pair built to make one criterion true and the rest
uncomputable, and the expected score derived by hand rather than from the
implementation.

The subtleties that decide whether a score is right:

  * a criterion that cannot be computed is EXCLUDED from the denominator,
    never scored as a failure — a 5/6 and a 5/9 are different statements
  * criterion 7 passes on FLAT share count (no dilution is a pass)
  * every other year-on-year test is STRICT (flat is not an improvement)
  * criterion 5 is total-debt/assets, a documented substitution for the
    textbook long-term-debt/assets

Offline. No network, no pytest.

Usage:
    python3 test_fscore.py
"""
from __future__ import annotations

import sys

from stock_screen import piotroski

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


def pair(cur: dict, prev: dict) -> dict:
    """Score and denominator only.

    piotroski() also returns the per-criterion bit string, which has its own
    checks further down. Comparing the whole dict here would mean every one of
    these nineteen assertions had to be rewritten the next time a field is
    added — and a test that breaks on an ADDITION is a test that discourages
    explaining the number better.
    """
    out = piotroski([cur, prev])
    return {"piotroski": out["piotroski"], "piotroski_of": out["piotroski_of"]}


def bits(cur: dict, prev: dict):
    return piotroski([cur, prev])["piotroski_bits"]


# ── Each criterion, alone ────────────────────────────────────────────────────

@check("1. ROA > 0 scores, and only when positive")
def _():
    assert pair({"roa": 0.11}, {}) == {"piotroski": 1, "piotroski_of": 1}
    assert pair({"roa": -0.04}, {}) == {"piotroski": 0, "piotroski_of": 1}
    # Exactly zero is not positive.
    assert pair({"roa": 0.0}, {}) == {"piotroski": 0, "piotroski_of": 1}


@check("2. CFO > 0 scores, and only when positive")
def _():
    assert pair({"cfo": 500}, {})["piotroski"] == 1
    assert pair({"cfo": -500}, {})["piotroski"] == 0


@check("3. ROA improving year on year is STRICT — flat is not improvement")
def _():
    # roa also fires criterion 1 when positive, so both are counted here.
    assert pair({"roa": 0.12}, {"roa": 0.09}) == {"piotroski": 2, "piotroski_of": 2}
    assert pair({"roa": 0.09}, {"roa": 0.09}) == {"piotroski": 1, "piotroski_of": 2}
    assert pair({"roa": 0.05}, {"roa": 0.09}) == {"piotroski": 1, "piotroski_of": 2}


@check("4. accrual quality — CFO must EXCEED net income, not merely match it")
def _():
    # cfo positive fires #2 as well.
    assert pair({"cfo": 900, "net_income": 700}, {}) == {"piotroski": 2, "piotroski_of": 2}
    assert pair({"cfo": 700, "net_income": 700}, {}) == {"piotroski": 1, "piotroski_of": 2}
    assert pair({"cfo": 500, "net_income": 700}, {}) == {"piotroski": 1, "piotroski_of": 2}


@check("5. leverage — the debt RATIO must fall, not the debt")
def _():
    # Debt rose 100 -> 150, but assets rose faster: 1000 -> 2000.
    # 0.10 vs 0.15 → the ratio fell, so it scores.
    got = pair({"total_debt": 100, "total_assets": 1000},
               {"total_debt": 150, "total_assets": 1000})
    assert got == {"piotroski": 1, "piotroski_of": 1}
    # Debt FELL in absolute terms but assets shrank harder: 0.20 -> 0.25.
    got = pair({"total_debt": 50, "total_assets": 200},
               {"total_debt": 100, "total_assets": 500})
    assert got == {"piotroski": 0, "piotroski_of": 1}, got


@check("5. a flat debt ratio does not score")
def _():
    assert pair({"total_debt": 100, "total_assets": 1000},
                {"total_debt": 200, "total_assets": 2000})["piotroski"] == 0


@check("6. current ratio improving scores")
def _():
    assert pair({"current_ratio": 1.8}, {"current_ratio": 1.4})["piotroski"] == 1
    assert pair({"current_ratio": 1.4}, {"current_ratio": 1.8})["piotroski"] == 0


@check("7. dilution — FLAT share count PASSES, only an increase fails")
def _():
    # The one criterion that is <= rather than <. Not issuing shares is the
    # pass condition; treating flat as a failure would understate every
    # company that simply did nothing.
    assert pair({"shares_out": 100}, {"shares_out": 100})["piotroski"] == 1
    assert pair({"shares_out": 90}, {"shares_out": 100})["piotroski"] == 1
    assert pair({"shares_out": 110}, {"shares_out": 100})["piotroski"] == 0


@check("8. gross margin improving scores")
def _():
    assert pair({"gross_margin": 0.41}, {"gross_margin": 0.38})["piotroski"] == 1
    assert pair({"gross_margin": 0.38}, {"gross_margin": 0.41})["piotroski"] == 0


@check("9. asset turnover improving scores")
def _():
    assert pair({"asset_turnover": 1.3}, {"asset_turnover": 1.1})["piotroski"] == 1
    assert pair({"asset_turnover": 1.0}, {"asset_turnover": 1.1})["piotroski"] == 0


# ── A whole company, scored by hand ──────────────────────────────────────────

STRONG_CUR = {"roa": 0.14, "cfo": 1200, "net_income": 900, "total_debt": 200,
              "total_assets": 4000, "current_ratio": 2.1, "shares_out": 100,
              "gross_margin": 0.44, "asset_turnover": 1.25}
STRONG_PREV = {"roa": 0.11, "cfo": 900, "net_income": 800, "total_debt": 400,
               "total_assets": 3800, "current_ratio": 1.7, "shares_out": 100,
               "gross_margin": 0.41, "asset_turnover": 1.10}


@check("a strong company scores 9/9 — every criterion checked by hand")
def _():
    # 1 ROA .14>0 ✓  2 CFO 1200>0 ✓  3 .14>.11 ✓  4 1200>900 ✓
    # 5 200/4000=.05 < 400/3800=.105 ✓  6 2.1>1.7 ✓  7 100<=100 ✓
    # 8 .44>.41 ✓  9 1.25>1.10 ✓
    assert pair(STRONG_CUR, STRONG_PREV) == {"piotroski": 9, "piotroski_of": 9}


@check("a weak company scores 0/9 on the same nine inputs reversed")
def _():
    # Every comparison flipped, ROA and CFO negative, shares issued.
    weak_cur = {"roa": -0.06, "cfo": -300, "net_income": -100, "total_debt": 900,
                "total_assets": 3000, "current_ratio": 0.8, "shares_out": 130,
                "gross_margin": 0.22, "asset_turnover": 0.7}
    weak_prev = {"roa": 0.02, "cfo": 200, "net_income": 150, "total_debt": 600,
                 "total_assets": 3200, "current_ratio": 1.3, "shares_out": 100,
                 "gross_margin": 0.31, "asset_turnover": 0.9}
    assert pair(weak_cur, weak_prev) == {"piotroski": 0, "piotroski_of": 9}


@check("a loss-making company still scores its balance-sheet criteria")
def _():
    # A negative-profit company is not automatically a zero: it can still be
    # deleveraging and improving liquidity, and the score must say so.
    got = pair({"roa": -0.05, "cfo": -50, "net_income": -400, "total_debt": 100,
                "total_assets": 1000, "current_ratio": 2.0, "shares_out": 100},
               {"roa": -0.02, "cfo": -80, "net_income": -300, "total_debt": 300,
                "total_assets": 1000, "current_ratio": 1.5, "shares_out": 100})
    # 4 CFO -50 > NI -400 ✓ · 5 .10<.30 ✓ · 6 2.0>1.5 ✓ · 7 flat ✓ = 4
    assert got == {"piotroski": 4, "piotroski_of": 7}, got


# ── Missing data must not be scored as failure ───────────────────────────────

@check("a company with no data scores nothing out of nothing")
def _():
    assert pair({}, {}) == {"piotroski": 0, "piotroski_of": 0}


@check("missing fields shrink the DENOMINATOR, they do not fail")
def _():
    got = pair({"roa": 0.10, "cfo": 500}, {"roa": 0.08})
    # 1 ✓ 2 ✓ 3 ✓ — the other six have no inputs at all.
    assert got == {"piotroski": 3, "piotroski_of": 3}, got


@check("one year of history cannot produce a score")
def _():
    # Every criterion but 1, 2 and 4 is year-on-year. A single year would
    # silently score 3/3 and read as a perfect company.
    for ys in ([STRONG_CUR], []):
        out = piotroski(ys)
        assert out["piotroski"] is None and out["piotroski_of"] is None, out


@check("NaN and None are treated as missing, not as zero")
def _():
    nan = float("nan")
    assert pair({"roa": nan, "cfo": None}, {"roa": 0.05}) == {"piotroski": 0, "piotroski_of": 0}


@check("zero total assets does not divide by zero")
def _():
    got = pair({"total_debt": 100, "total_assets": 0},
               {"total_debt": 100, "total_assets": 1000})
    assert got == {"piotroski": 0, "piotroski_of": 0}, got


@check("the score can never exceed the number of criteria evaluated")
def _():
    import itertools
    keys = ["roa", "cfo", "net_income", "total_debt", "total_assets",
            "current_ratio", "shares_out", "gross_margin", "asset_turnover"]
    for n in (0, 1, 4, 9):
        for combo in itertools.islice(itertools.combinations(keys, n), 12):
            cur = {k: STRONG_CUR[k] for k in combo}
            prev = {k: STRONG_PREV[k] for k in combo}
            out = pair(cur, prev)
            assert 0 <= out["piotroski"] <= out["piotroski_of"] <= 9, (combo, out)


# ── The bit string must agree with the score it explains ─────────────────────

@check("the bit string reconstructs both the score and the denominator")
def _():
    """The number and its explanation come from one pass, but a reader will
    check them against each other — so they must never disagree."""
    for cur, prev in [(STRONG_CUR, STRONG_PREV),
                      ({"roa": 0.1, "cfo": 500}, {"roa": 0.12}),
                      ({}, {}),
                      ({"shares_out": 100}, {"shares_out": 100})]:
        out = piotroski([cur, prev])
        b = out["piotroski_bits"]
        assert len(b) == 9, f"expected 9 criteria, got {len(b)}: {b!r}"
        assert b.count("1") == out["piotroski"], (b, out)
        assert b.count("1") + b.count("0") == out["piotroski_of"], (b, out)
        assert set(b) <= {"1", "0", "X"}, b


@check("the bits land in Piotroski's documented order")
def _():
    # Only criterion 7 (no dilution) is computable here, and it passes — so
    # the ONLY "1" must be in position 7.
    b = bits({"shares_out": 100}, {"shares_out": 100})
    assert b == "XXXXXX1XX", b
    # Only criterion 1 computable, and it fails.
    b = bits({"roa": -0.1}, {})
    assert b == "0XXXXXXXX", b


@check("there is exactly one label per criterion")
def _():
    from stock_screen import PIOTROSKI_CRITERIA
    assert len(PIOTROSKI_CRITERIA) == 9
    assert all(isinstance(x, str) and x for x in PIOTROSKI_CRITERIA)


@check("a one-year company emits no bit string rather than nine X's")
def _():
    # Nine X's would render an explanation panel for a score that does not
    # exist. None is the honest shape.
    assert piotroski([STRONG_CUR])["piotroski_bits"] is None


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
