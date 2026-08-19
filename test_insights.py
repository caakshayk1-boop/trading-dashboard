#!/usr/bin/env python3
"""
test_insights.py — findings must be rules, not vibes.

Each engine is deterministic, so each is testable exactly. What these checks
defend against is the failure mode of every "insight" feature ever shipped:
firing on everything, so the reader learns to ignore it.

Offline. No network, no pytest.

Usage:
    python3 test_insights.py
"""
from __future__ import annotations

import sys

import insights

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


def stock(**kw):
    base = {"sym": kw.pop("sym", "TEST"), "name": "Test Ltd", "comp": 50}
    base.update(kw)
    return base


def find(rows, key):
    return next((f for f in insights.hidden_findings(rows) if f["key"] == key), None)


# ── Suppression: the discipline that makes the rest worth reading ────────────

@check("two companies sharing a property is not a finding")
def _():
    rows = [stock(sym=f"S{i}", q=90, r3m=-25) for i in range(2)]
    assert find(rows, "quality_price_divergence") is None


@check("three is")
def _():
    rows = [stock(sym=f"S{i}", q=90, r3m=-25) for i in range(3)]
    f = find(rows, "quality_price_divergence")
    assert f is not None and f["count"] == 3


@check("an empty universe produces no findings at all")
def _():
    assert insights.hidden_findings([]) == []
    assert insights.hidden_findings(None) == []


# ── Missing data must never satisfy a rule ──────────────────────────────────

@check("a company with no 3-month return is not 'weak on the tape'")
def _():
    # The whole point: absent is not zero, and it is certainly not negative.
    rows = [stock(sym=f"S{i}", q=90) for i in range(5)]
    assert find(rows, "quality_price_divergence") is None


@check("a company with no margin data is not 'deteriorating'")
def _():
    rows = [stock(sym=f"S{i}", q=80, roce_trend="down") for i in range(5)]
    assert find(rows, "quality_deterioration") is None


@check("NaN and text are treated as missing, not as passing")
def _():
    rows = [stock(sym=f"S{i}", q=90, r3m=float("nan")) for i in range(5)]
    assert find(rows, "quality_price_divergence") is None
    rows = [stock(sym=f"S{i}", q=90, r3m="lots") for i in range(5)]
    assert find(rows, "quality_price_divergence") is None


# ── Each rule fires on what it says, and not on what it does not ────────────

@check("quality/price divergence needs BOTH halves")
def _():
    strong_and_weak = [stock(sym=f"A{i}", q=85, r3m=-15) for i in range(4)]
    assert find(strong_and_weak, "quality_price_divergence")["count"] == 4
    # strong but not weak
    assert find([stock(sym=f"B{i}", q=85, r3m=+15) for i in range(4)],
                "quality_price_divergence") is None
    # weak but not strong
    assert find([stock(sym=f"C{i}", q=30, r3m=-15) for i in range(4)],
                "quality_price_divergence") is None


@check("the F-score rule requires a well-EVALUATED score, not just a high one")
def _():
    # 7 out of 7 evaluated is a different claim from 7 out of 9, and a company
    # scoring 7 on two computable criteria has not passed this screen.
    ok = [stock(sym=f"A{i}", piotroski=8, piotroski_of=9, r6m=-5) for i in range(4)]
    assert find(ok, "fscore_strong_price_weak")["count"] == 4
    thin = [stock(sym=f"B{i}", piotroski=8, piotroski_of=4, r6m=-5) for i in range(4)]
    assert find(thin, "fscore_strong_price_weak") is None


@check("volume-without-price needs the price to genuinely not move")
def _():
    quiet = [stock(sym=f"A{i}", vol_spike=3, r1w=0.5) for i in range(4)]
    assert find(quiet, "volume_without_price")["count"] == 4
    loud = [stock(sym=f"B{i}", vol_spike=3, r1w=9) for i in range(4)]
    assert find(loud, "volume_without_price") is None


@check("every finding states its own rule")
def _():
    rows = [stock(sym=f"S{i}", q=90, r3m=-25, piotroski=8, piotroski_of=9, r6m=-5,
                  vol_spike=3, r1w=0.2, pat_yoy=40) for i in range(5)]
    fs = insights.hidden_findings(rows)
    assert fs
    for f in fs:
        assert f["rule"] and len(f["rule"]) > 10, f
        assert f["count"] >= insights.MIN_HITS
        assert len(f["names"]) <= insights.MAX_NAMES


@check("the printed sample is capped but the count is the true one")
def _():
    rows = [stock(sym=f"S{i}", q=90, r3m=-25) for i in range(40)]
    f = find(rows, "quality_price_divergence")
    assert f["count"] == 40 and len(f["names"]) == insights.MAX_NAMES


# ── Contradictions ──────────────────────────────────────────────────────────

@check("agreement is not a contradiction")
def _():
    calm = {"nifty_1m": 1.0, "median_1m": 1.1, "advancing": 500,
            "declining": 250, "above200": 70, "counted": 750, "label": "NEUTRAL"}
    assert insights.contradictions(calm) == []


@check("an index outrunning the median stock IS a contradiction")
def _():
    narrow = {"nifty_1m": 4.0, "median_1m": -1.0, "advancing": 300,
              "declining": 450, "above200": 45, "counted": 750, "label": "BULLISH"}
    keys = {c["key"] for c in insights.contradictions(narrow)}
    assert "narrow_index" in keys
    assert "breadth_conflict" in keys
    assert "trend_participation" in keys


@check("foreign selling into a rising index is flagged")
def _():
    b = {"nifty_1m": 2.0, "median_1m": 1.9, "advancing": 500, "declining": 200,
         "above200": 70, "counted": 750, "label": "NEUTRAL"}
    keys = {c["key"] for c in insights.contradictions(b, {"fii_cr": -2535.1})}
    assert keys == {"flow_conflict"}


@check("foreign BUYING into a rising index is not flagged")
def _():
    b = {"nifty_1m": 2.0, "median_1m": 1.9, "advancing": 500, "declining": 200,
         "above200": 70, "counted": 750, "label": "NEUTRAL"}
    assert insights.contradictions(b, {"fii_cr": 900}) == []


@check("every contradiction says what it means and what to watch")
def _():
    narrow = {"nifty_1m": 4.0, "median_1m": -1.0, "advancing": 300,
              "declining": 450, "above200": 45, "counted": 750, "label": "BULLISH"}
    for c in insights.contradictions(narrow, {"fii_cr": -100}):
        assert c["detail"] and c["means"] and c["watch"], c


@check("missing market data produces no contradictions rather than false ones")
def _():
    assert insights.contradictions({}) == []
    assert insights.contradictions(None) == []


# ── What changed ────────────────────────────────────────────────────────────

@check("what_changed reports the shape of the move, not 562 rows")
def _():
    payload = {"changes": {"compared_with": "2026-08-12", "new": 4, "moved": 562},
               "breadth": {"above200": 57.6, "advancing": 276, "declining": 469,
                           "median_1m": -0.7, "label": "NEUTRAL"},
               "rows": [{"comp": 85}, {"comp": 70}, {"comp": 50}, {"comp": 20}]}
    w = insights.what_changed(payload)
    assert w["moved"] == 562 and w["new_names"] == 4
    assert w["bands"] == {"80+": 1, "60-79": 1, "40-59": 1, "under 40": 1}


@check("no previous build means no change report, not a fabricated one")
def _():
    assert insights.what_changed({"rows": []}) is None
    assert insights.what_changed({}) is None
    assert insights.what_changed(None) is None


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
