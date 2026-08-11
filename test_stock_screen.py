#!/usr/bin/env python3
"""
test_stock_screen.py — the screen's arithmetic, and the traps it must not fall in.

Two kinds of test here:

  1. INDICATORS against hand-computed values. Every one of these was checked
     with a calculator, not against the implementation, so a rewrite that
     changes an answer fails rather than moves the goalposts. The RSI case in
     particular carries its worked arithmetic in the docstring, because the
     figure floating around the internet for that dataset (70.53) is computed
     over a different window and "fixing" the code to match it would be wrong.

  2. HONESTY INVARIANTS. Missing data must not score as zero, must not sort as
     zero, must not become an imputed number, and must not silently vanish from
     a score's denominator without lowering its confidence. These are the ways
     a screen publishes something false while every test passes.

Dependency-free and offline, like test_engine_regressions.py: no network, no
database, no pytest. `python test_stock_screen.py` is the whole contract.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

import stock_screen as S
from datetime import datetime, timezone

FAILURES: list[str] = []
PASSES = 0

# static/app.js is the SOURCE. docs/app.js is a build artefact that generate.py
# overwrites from it on every run — editing docs/app.js and then generating the
# page silently reverts the edit, which is exactly what happened once here: the
# whole screener UI was written into docs/app.js, generate.py replaced the file,
# and the section shipped with a table and no behaviour. Nothing but the browser
# noticed. Every assertion below reads the source, and
# test_docs_app_js_is_a_copy_of_the_source pins the relationship itself.
APP_JS = pathlib.Path("static/app.js")
DOCS_APP_JS = pathlib.Path("docs/app.js")


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"  PASS  {name}")
    else:
        FAILURES.append(f"{name} — {detail}")
        print(f"  FAIL  {name}  {detail}")


def near(a, b, tol=1e-6):
    return a is not None and b is not None and abs(a - b) < tol


# ─────────────────────────────────────────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────────────────────────────────────────

# Wilder's own published series. 15 prices → 14 changes.
WILDER = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84,
          46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41,
          46.22, 45.64, 46.21, 46.25, 45.71, 46.45, 45.78, 45.35, 44.03,
          44.18, 44.22, 44.57, 43.42, 42.66, 43.13]


def test_rsi_matches_hand_arithmetic() -> None:
    """RSI(14) over the first 15 Wilder prices is 70.4641, computed by hand.

    Gains over the 14 changes: .06+.72+.50+.27+.32+.42+.24+.14+.67 = 3.34
    Losses:                     .25+.54+.19+.42                    = 1.40
    AG = 3.34/14 = 0.2385714   AL = 1.40/14 = 0.10
    RS = 2.3857143             RSI = 100 − 100/3.3857143 = 70.46414

    Not 70.53. That figure is this same dataset over a window shifted by one
    bar, and matching it would mean seeding the average off 15 changes.
    """
    v = S.rsi(WILDER[:15], 14)
    check("RSI(14) equals hand-computed 70.46414", near(v, 70.46414, 1e-5), f"got {v}")


def test_rsi_edges() -> None:
    check("RSI of a flat line is 50, not 0 or 100",
          S.rsi([50.0] * 40) == 50.0, f"got {S.rsi([50.0] * 40)}")
    check("RSI of only-up is 100",
          S.rsi([float(i) for i in range(1, 40)]) == 100.0)
    check("RSI with too few bars is None", S.rsi([1.0, 2.0, 3.0], 14) is None)
    # A stock that only fell must not read as neutral.
    check("RSI of only-down is 0",
          S.rsi([float(i) for i in range(40, 1, -1)]) == 0.0,
          f"got {S.rsi([float(i) for i in range(40, 1, -1)])}")


def test_sma_and_ema() -> None:
    check("SMA(10) of 1..10 is 5.5", S.sma(list(range(1, 11)), 10) == 5.5)
    check("SMA short of its window is None", S.sma([1.0, 2.0], 10) is None)
    check("SMA of an empty series is None", S.sma([], 5) is None)
    e = S.ema_series([1.0] * 30, 10)
    check("EMA of a constant stays that constant", near(e[-1], 1.0))
    check("EMA short of its window is empty", S.ema_series([1.0, 2.0], 10) == [])


def test_macd_measures_acceleration() -> None:
    """A LINEAR ramp has a constant MACD line and therefore ~zero histogram.

    Pinned because it looks wrong: a rising series with a zero histogram reads
    like a bug, and "fixing" it would mean making the histogram track level
    instead of change. On a straight line there is no acceleration to report.
    """
    ramp = [float(i) for i in range(1, 120)]
    m = S.macd(ramp)
    check("MACD line is positive on a rising series", m["macd"] > 0, f"{m['macd']}")
    check("MACD histogram is ~0 on a LINEAR ramp", abs(m["hist"]) < 1e-3, f"{m['hist']}")

    accel = [float(i * i) / 100 for i in range(1, 120)]
    check("MACD histogram is positive when the rise accelerates",
          S.macd(accel)["hist"] > 0)
    # A decaying fall is still a fall, but its momentum is improving — the line
    # stays negative while the histogram turns up. Both must be true at once.
    decay = list(reversed(accel))
    md = S.macd(decay)
    check("a decaying downtrend keeps a negative MACD line", md["macd"] < 0, f"{md['macd']}")
    check("MACD short of its window is all None", S.macd([1.0, 2.0, 3.0])["macd"] is None)


def test_atr() -> None:
    h, l, c = [102.0] * 30, [100.0] * 30, [101.0] * 30
    check("ATR of constant 2-wide bars is 2.0", near(S.atr(h, l, c, 14), 2.0))
    check("ATR short of its window is None", S.atr([1.0], [1.0], [1.0], 14) is None)
    # A gap must widen true range beyond the bar's own high−low.
    h2 = [102.0] * 20 + [120.0]
    l2 = [100.0] * 20 + [118.0]
    c2 = [101.0] * 20 + [119.0]
    check("ATR counts the gap, not just the bar range", S.atr(h2, l2, c2, 14) > 2.0)


def test_cagr_refuses_meaningless_cases() -> None:
    check("CAGR 100→200 over 3y is 25.99%", near(S.cagr(100, 200, 3), 0.259921, 1e-6))
    check("CAGR from a LOSS is None, not a huge growth rate",
          S.cagr(-50, 100, 3) is None)
    check("CAGR to a loss is None", S.cagr(100, -50, 3) is None)
    check("CAGR from zero is None", S.cagr(0, 100, 3) is None)
    check("CAGR over zero years is None", S.cagr(100, 200, 0) is None)
    check("CAGR with a missing endpoint is None", S.cagr(None, 200, 3) is None)


def test_pct_change_guards() -> None:
    check("pct_change over 5 bars of 1..11 is correct",
          near(S.pct_change([float(i) for i in range(1, 12)], 5), 11 / 6 - 1))
    check("pct_change short of its window is None", S.pct_change([1.0, 2.0], 5) is None)
    check("pct_change off a zero base is None", S.pct_change([0.0] + [5.0] * 9, 9) is None)


# ─────────────────────────────────────────────────────────────────────────────
# TECHNICALS — the edge cases a real universe actually contains
# ─────────────────────────────────────────────────────────────────────────────

def _px(n, price=100.0, vol=1000.0):
    return {"c": [price] * n, "h": [price * 1.01] * n, "l": [price * 0.99] * n,
            "v": [vol] * n, "last_date": "2026-08-11"}


def test_newly_listed_stock_gets_no_long_averages() -> None:
    """60 bars must not produce a 200-day average or a 3-year return."""
    t = S.technicals(_px(60), None)
    check("no SMA200 on 60 bars", t["sma200"] is None)
    check("no 52-week high on 60 bars", t["high52"] is None)
    check("no 1-year return on 60 bars", t["r1y"] is None)
    check("no 3-year return on 60 bars", t["r3y"] is None)
    check("SMA20 IS computed on 60 bars", t["sma20"] is not None)
    check("above_mas counts only the averages that exist", t["above_mas"] is not None)


def test_zero_volume_does_not_divide() -> None:
    t = S.technicals(_px(260, vol=0.0), None)
    check("zero volume yields no spike rather than a division",
          t["vol_spike"] is None)
    check("zero volume is not marked liquid", t["liquid"] is False)


def test_flat_series_is_not_a_breakout() -> None:
    t = S.technicals(_px(260), None)
    check("a flat series is not a 20-day breakout", t["brk20"] is False)
    check("a flat series has no MA stack", t["ma_stack"] is False)


def test_breakout_excludes_today_from_its_window() -> None:
    """Every bar is its own 20-day high. The window must exclude the last bar."""
    rising = [100.0 + i for i in range(60)]
    px = {"c": rising, "h": rising, "l": rising, "v": [1000.0] * 60,
          "last_date": "2026-08-11"}
    check("a genuine new high is a 20-day breakout", S.technicals(px, None)["brk20"] is True)
    flat_then = [100.0] * 40 + [99.0]
    px2 = {"c": flat_then, "h": flat_then, "l": flat_then, "v": [1000.0] * 41,
           "last_date": "2026-08-11"}
    check("a close below the window is not a breakout",
          S.technicals(px2, None)["brk20"] is False)


def test_relative_strength_needs_a_benchmark() -> None:
    t = S.technicals(_px(300), None)
    check("no benchmark means no relative strength, not zero",
          t["rs_1y"] is None and t["rs_3m"] is None)
    up = [100.0 + i for i in range(300)]
    bench = {"c": [100.0] * 300, "h": [100.0] * 300, "l": [100.0] * 300,
             "v": [1.0] * 300, "last_date": "2026-08-11"}
    t2 = S.technicals({"c": up, "h": up, "l": up, "v": [1000.0] * 300,
                       "last_date": "2026-08-11"}, bench)
    check("outperforming a flat index gives positive excess return",
          t2["rs_1y"] is not None and t2["rs_1y"] > 0, f"{t2['rs_1y']}")


# ─────────────────────────────────────────────────────────────────────────────
# HONESTY INVARIANTS — the ways a screen lies while passing its tests
# ─────────────────────────────────────────────────────────────────────────────

def test_band_never_rewards_missing_data() -> None:
    """The exact defect test_engine_regressions pins for the signal engine.

    `min(1.0, nan)` returns 1.0 in Python, so a NaN metric once scored FULL
    marks. Here a missing metric must return None so _blend drops it, and the
    score's confidence falls instead of its value rising.
    """
    check("_band(None) is None", S._band(None, 0, 1) is None)
    check("_band(nan) is None", S._band(float("nan"), 0, 1) is None)
    check("_band clamps above its top", S._band(99, 0, 1) == 1.0)
    check("_band clamps below its floor", S._band(-99, 0, 1) == 0.0)


def test_blend_lowers_confidence_rather_than_the_score() -> None:
    full, cf = S._blend({"a": 1.0, "b": 1.0})
    part, cp = S._blend({"a": 1.0, "b": None})
    check("a perfect pair scores 100 at full confidence", full == 100.0 and cf == 1.0)
    check("one missing input keeps the score but halves confidence",
          part == 100.0 and cp == 0.5, f"score={part} conf={cp}")
    none, cn = S._blend({"a": None, "b": None})
    check("all inputs missing scores None, not zero", none is None and cn == 0.0)


def test_bank_scores_without_roce_instead_of_being_punished() -> None:
    """A lender has no ROCE and no leverage penalty. It must still score.

    The failure this pins: treating a missing ROCE as 0 would put every bank at
    the bottom of the quality column for a reason that is not about the bank.
    """
    bank = {"sector": "Financial Services", "industry": "Banks",
            "roce_med": None, "roe_med": 0.16, "ebit_margin_med": None,
            "debt_to_equity": 8.0, "interest_cover": None}
    q = S.score_quality(bank)
    check("a bank still gets a quality score", q["score"] is not None, f"{q}")
    check("ROCE is absent rather than zero for a bank", q["parts"]["roce"] is None)
    check("leverage is exempt for a lender", q["parts"]["leverage"] is None)
    check("the bank's confidence reflects the missing inputs", q["conf"] < 0.6, f"{q['conf']}")
    # And the same numbers on a manufacturer DO carry the leverage penalty.
    mfg = dict(bank, sector="Industrials", industry="Capital Goods")
    check("8x leverage is penalised for a non-financial",
          S.score_quality(mfg)["parts"]["leverage"] == 0.0)


def test_negative_equity_is_not_a_clean_balance_sheet() -> None:
    """Vodafone Idea scored a PERFECT 100 on leverage. ROE was −96.6%.

    D/E turns negative when equity does — accumulated losses have eaten the
    net worth — and inverting the band read that as less debt than debt-free.
    The three most distressed balance sheets in the Nifty 500 (IDEA −5.38,
    GMRAIRPORT −17.45, TTML −0.95) were rating best on the component, and the
    browser's "Debt-free" preset was listing them.
    """
    distressed = {"sector": "Telecom", "industry": "Telecom Services",
                  "debt_to_equity": -5.38, "roe_med": -0.966,
                  "roce_med": None, "ebit_margin_med": None, "interest_cover": None}
    lev = S.score_quality(distressed)["parts"]["leverage"]
    check("negative D/E scores ZERO on leverage, not full marks", lev == 0.0, f"got {lev}")
    # The band is continuous, so D/E 0.04 scores 98 rather than exactly 100 —
    # only a literal zero tops out. What matters is that it is near-perfect and
    # strictly better than the distressed case.
    healthy = dict(distressed, debt_to_equity=0.04, roe_med=0.30)
    lev_ok = S.score_quality(healthy)["parts"]["leverage"]
    check("a genuinely debt-free balance sheet scores near-perfect leverage",
          lev_ok >= 95.0, f"got {lev_ok}")
    check("zero debt scores exactly full marks",
          S.score_quality(dict(healthy, debt_to_equity=0.0))["parts"]["leverage"] == 100.0)
    check("higher debt scores lower than lower debt",
          S.score_quality(dict(distressed, debt_to_equity=0.2))["parts"]["leverage"]
          > S.score_quality(dict(distressed, debt_to_equity=1.4))["parts"]["leverage"])

    r = S.ratios(None, {"debt_to_equity": -5.38, "sector": "Telecom"})
    r["industry"] = "Telecom Services"
    weaknesses = " ".join(i["t"] for i in S.swot(r, S.technicals(_px(300), None), {})["w"])
    check("negative equity is reported as a weakness",
          "negative" in weaknesses.lower(), weaknesses[:140])
    check("it is NOT described as debt-free",
          "debt-free" not in weaknesses.lower(), weaknesses[:140])

    # And the browser preset must exclude them too.
    js = APP_JS.read_text(encoding="utf-8")
    m = re.search(r"debtfree:\s*function\(r\)\{(.*?)\}", js, re.S)
    check("the debt-free preset guards against a negative ratio",
          m is not None and ">= 0" in m.group(1), m.group(1)[:90] if m else "not found")


def test_composite_renormalises_over_present_scores() -> None:
    """A missing sub-score must not drag the composite toward zero."""
    both = S._composite({"quality": {"score": 80.0}, "growth": {"score": 80.0},
                         "technical": {"score": 80.0}, "valuation": {"score": 80.0}})
    check("all four at 80 composites to 80", near(both, 80.0, 0.05), f"{both}")
    partial = S._composite({"quality": {"score": 80.0}, "growth": {"score": None},
                            "technical": {"score": None}, "valuation": {"score": None}})
    check("one score of 80 with three missing still composites to 80",
          near(partial, 80.0, 0.05), f"{partial}")
    check("no scores at all composites to None",
          S._composite({"quality": {"score": None}}) is None)


def test_a_company_with_no_statements_cannot_be_ranked() -> None:
    """CHENNPETRO ranked FIRST of 500 on the first full run.

    It publishes no annual statements, so its quality score came from one
    `.info` leverage field at confidence 0.20 and its growth score did not
    exist. Renormalising the composite over what was left — that thin number
    plus a strong chart — produced 90.0 and put a company with no accounts at
    the top of a screen whose premise is accounts.
    """
    thin = {"quality": {"score": 91.2}, "growth": {"score": None},
            "valuation": {"score": 73.5}, "technical": {"score": 98.3}}
    check("no statements means no composite at all",
          S._composite(thin, has_stmts=False) is None)
    check("the same scores DO composite when the accounts exist",
          S._composite(thin, has_stmts=True) is not None)
    # A bank has statements but no ROCE, and must keep its rank.
    bank = {"quality": {"score": 44.0}, "growth": {"score": 60.0},
            "valuation": {"score": 50.0}, "technical": {"score": 55.0}}
    check("a bank with real accounts is still ranked",
          S._composite(bank, has_stmts=True) is not None)


def test_one_off_margin_jump_is_flagged_not_celebrated() -> None:
    """JSW Dulux: FY26 EBITDA ₹668cr → ₹2,451cr on revenue that FELL.

    That is the Dulux acquisition, not the paint business, and it reads as a
    52-point margin expansion with a 96.9% ROCE. "Operating leverage is
    working" would be the most misleading line this section could print.
    """
    stmts = {"years": [
        {"fy": "FY26", "period_end": "2026-03-31", "revenue": 3580.0, "ebitda": 2451.0,
         "ebit_margin": 0.664, "roce_ic": 0.969},
        {"fy": "FY25", "period_end": "2025-03-31", "revenue": 4043.0, "ebitda": 668.0,
         "ebit_margin": 0.143, "roce_ic": 0.435},
        {"fy": "FY24", "period_end": "2024-03-31", "revenue": 3937.0, "ebitda": 666.0,
         "ebit_margin": 0.148, "roce_ic": 0.439},
        {"fy": "FY23", "period_end": "2023-03-31", "revenue": 3777.0, "ebitda": 549.0,
         "ebit_margin": 0.124, "roce_ic": 0.355},
    ], "shares_changed": False}
    r = S.ratios(stmts, None)
    r["industry"] = "Consumer Durables"
    check("the margin discontinuity is detected",
          r.get("margin_one_off") is not None and r["margin_one_off"] > 15,
          str(r.get("margin_one_off")))
    sw = S.swot(r, S.technicals(_px(300), None), {})
    strengths = " ".join(i["t"] for i in sw["s"])
    risks = " ".join(i["t"] for i in sw["t"])
    check("'operating leverage is working' is NOT claimed on a one-off",
          "operating leverage" not in strengths, strengths[:140])
    check("the discontinuity is raised as a risk instead",
          "discontinuity" in risks, risks[:160])
    ups = S.updates(r, S.technicals(_px(300), None))
    warn = [u for u in ups if u["k"] == "warn"]
    check("the update is a warning, not good news",
          any("one-off" in u["t"] or "acquisition" in u["t"] for u in warn),
          str([u["t"][:60] for u in ups]))
    # The score itself was already defended by using the median, not the year.
    check("quality scores off the 4-year median ROCE, not the 96.9% spike",
          near(r["roce_med"], 0.437, 0.01), str(r["roce_med"]))


def test_rsi_above_seventy_scores_worse_not_better() -> None:
    """Overextension must reduce the momentum component, not maximise it."""
    at65 = S.score_technical({"rsi14": 65})["parts"]["momentum"]
    at85 = S.score_technical({"rsi14": 85})["parts"]["momentum"]
    check("RSI 65 scores full momentum", at65 == 100.0, f"{at65}")
    check("RSI 85 scores WORSE than RSI 65", at85 < at65, f"65→{at65} 85→{at85}")
    at95 = S.score_technical({"rsi14": 95})["parts"]["momentum"]
    check("RSI 95 scores zero momentum", at95 == 0.0, f"{at95}")


def test_eps_growth_is_withheld_across_a_share_count_change() -> None:
    """HDFCBANK's merger halves EPS. That must never become a growth rate."""
    stmts = {"years": [
        {"fy": "FY26", "period_end": "2026-03-31", "revenue": 1000.0, "eps": 45.0,
         "ebitda": 200.0, "equity": 500.0, "roe": 0.09},
        {"fy": "FY23", "period_end": "2023-03-31", "revenue": 600.0, "eps": 88.0,
         "ebitda": 120.0, "equity": 300.0, "roe": 0.17},
    ], "shares_changed": True}
    r = S.ratios(stmts, None)
    check("EPS CAGR is withheld when the share count moved",
          r["eps_cagr3"] is None, f"{r['eps_cagr3']}")
    check("revenue CAGR is still published", r["rev_cagr3"] is not None)
    check("the growth score drops EPS from its denominator rather than scoring it 0",
          S.score_growth(r)["parts"]["eps"] is None)
    # Without the flag the same numbers DO produce a per-share rate.
    r2 = S.ratios(dict(stmts, shares_changed=False), None)
    check("EPS CAGR is published when the share count is stable",
          r2["eps_cagr3"] is not None)


def test_ratios_prefer_the_median_over_a_one_off_year() -> None:
    """ITC's demerger year reads ROE 49.6% against ~28% either side.

    Ranking on latest-ROE alone puts it top of the table for an accounting
    event. The median must be what the score consumes.
    """
    stmts = {"years": [
        {"fy": "FY26", "period_end": "2026-03-31", "roe": 0.285, "revenue": 100.0},
        {"fy": "FY25", "period_end": "2025-03-31", "roe": 0.496, "revenue": 95.0},
        {"fy": "FY24", "period_end": "2024-03-31", "roe": 0.275, "revenue": 90.0},
        {"fy": "FY23", "period_end": "2023-03-31", "roe": 0.278, "revenue": 85.0},
    ], "shares_changed": False}
    r = S.ratios(stmts, None)
    check("the median ROE ignores the one-off spike",
          near(r["roe_med"], 0.2815, 1e-4), f"{r['roe_med']}")
    check("the spike is still reported as the latest value", near(r["roe"], 0.285))
    check("quality scores off the median, not the spike",
          S.score_quality(r)["parts"]["roe"]
          == S.score_quality(dict(r, roe=0.9))["parts"]["roe"])


def test_missing_statements_produce_a_price_only_row() -> None:
    """TATAMOTORS returns no statements at all. That is a row, not a crash."""
    r = S.ratios(None, None)
    check("no statements means has_statements False", r["has_statements"] is False)
    check("no statements means no ROCE", r["roce"] is None)
    check("no statements means no year table", r["years"] == [])
    check("quality on nothing is None, not zero", S.score_quality(r)["score"] is None)
    sw = S.swot(r, S.technicals(_px(260), None), {})
    joined = " ".join(i["t"] for i in sw["t"])
    check("the SWOT says the statements are missing rather than staying silent",
          "no annual statements" in joined.lower(), joined[:120])


def test_swot_lines_always_carry_their_evidence() -> None:
    """Every claim must quote the number that produced it."""
    stmts = {"years": [
        {"fy": "FY26", "period_end": "2026-03-31", "roce_ic": 0.44, "roe": 0.32,
         "revenue": 6927.0, "ebitda": 2180.0, "ebit_margin": 0.30,
         "debt_to_equity": 0.04, "interest_cover": 40.0},
        {"fy": "FY23", "period_end": "2023-03-31", "roce_ic": 0.40, "roe": 0.29,
         "revenue": 5347.0, "ebitda": 1360.0, "ebit_margin": 0.24,
         "debt_to_equity": 0.04, "interest_cover": 35.0},
    ], "shares_changed": False}
    r = S.ratios(stmts, {"sector": "Healthcare", "market_cap_cr": 60000})
    r["industry"] = "Pharmaceuticals"
    sw = S.swot(r, S.technicals(_px(300), None), {})
    every = [i for b in sw.values() for i in b]
    check("the SWOT produced lines at all", len(every) > 0, f"{len(every)}")
    check("every SWOT line carries an evidence string",
          all(i.get("k") for i in every),
          str([i["t"][:40] for i in every if not i.get("k")]))
    strengths = " ".join(i["t"] for i in sw["s"])
    check("a 44% ROCE is reported as a strength", "capital employed" in strengths, strengths[:120])
    check("debt-free is reported as a strength", "debt-free" in strengths.lower(), strengths[:160])


def test_no_forecast_language_anywhere_in_the_output() -> None:
    """The section must not predict. Pinned as a text assertion on purpose.

    A future edit that adds "will rise", "target" or a probability to a SWOT
    line or an update turns a ranking of public data into investment advice
    from an unregistered adviser. Cheap test, expensive failure.
    """
    banned = re.compile(r"\b(will (rise|fall|reach)|target price|guaranteed|"
                        r"probability|expected return|forecast|buy now|"
                        r"sure shot|multibag)\b", re.I)
    src = pathlib.Path("stock_screen.py").read_text(encoding="utf-8")
    # Docstrings and # comments discuss these words in order to forbid them —
    # the module docstring says "there is no probability, no target" — so they
    # are stripped first. Only strings that can reach a reader are scanned.
    src = re.sub(r'""".*?"""', "", src, flags=re.S)
    src = re.sub(r"'''.*?'''", "", src, flags=re.S)
    src = re.sub(r"(?m)^\s*#.*$", "", src)
    strings = re.findall(r'f?"([^"\\\n]{12,})"', src)
    bad = [s for s in strings if banned.search(s)]
    check("no forecast or advice language in any published string",
          not bad, str(bad[:3]))


def test_compact_strips_nulls_but_keeps_falsey_numbers() -> None:
    """Zero and False are DATA. Only None and empties may be dropped."""
    row = S._compact({"a": None, "b": 0, "c": False, "d": "", "e": [],
                      "f": {}, "g": 1.5, "h": {"x": None, "y": 2}})
    check("None is dropped", "a" not in row)
    check("a zero VALUE survives", row.get("b") == 0)
    check("False survives — it is a real breakout answer", row.get("c") is False)
    check("empty string is dropped", "d" not in row)
    check("empty list is dropped", "e" not in row)
    check("nested nulls are dropped", row["h"] == {"y": 2})


def test_coverage_survives_compaction() -> None:
    """The defect that killed a completed 17-minute build at its final step.

    coverage() runs after _compact(), so a row with no ROCE has no 'roce' KEY
    at all. The original counted with `x["roce"] is not None` and raised
    KeyError on the first bank in the universe — after every fetch was done.
    """
    rows = [S._compact(r) for r in [
        {"sym": "TCS", "roce": 62.2, "has_stmts": True},
        {"sym": "HDFCBANK", "roce": None, "has_stmts": True},     # bank: no ROCE
        {"sym": "TATAMOTORS", "roce": None, "has_stmts": False},  # no statements
    ]]
    check("the compacted bank row really has no roce key", "roce" not in rows[1])
    cov = S.coverage(rows)
    check("coverage counts 3 priced", cov["priced"] == 3, str(cov))
    check("coverage counts 2 with statements", cov["statements"] == 2, str(cov))
    check("coverage counts 1 with ROCE", cov["roce"] == 1, str(cov))
    check("coverage percentages are computed", cov["roce_pct"] == 33.3, str(cov))
    check("coverage of nothing does not divide by zero",
          S.coverage([])["roce_pct"] == 0)


def test_a_new_cached_field_invalidates_old_entries() -> None:
    """Adding a field to fetch() must not leave old cache entries serving stale shape.

    When `business` was added, 31 symbols already sat in a FRESH 7-day cache
    written before the field existed. 32 of 500 rows in the screen therefore
    rendered with no company description, no error, and no way to tell — a
    partial-data state that would have healed itself within a week and been
    invisible the whole time.
    """
    import fundamentals as F
    old_shape = {"symbol": "X", "roe": 0.2,
                 "_fetched_at": datetime.now(timezone.utc).isoformat()}
    check("an entry predating the current fields is not schema-ok",
          not F._schema_ok(old_shape))
    check("...even though its timestamp is fresh", F._fresh(old_shape))
    current = dict(old_shape, business="does things", held_insiders=0.5,
                   dividend_yield=1.0)
    check("an entry with every promised field is schema-ok", F._schema_ok(current))
    # A cached MISS has no fields by design and must not be refetched forever.
    check("a cached miss stays valid",
          F._schema_ok({"_miss": True,
                        "_fetched_at": datetime.now(timezone.utc).isoformat()}))


def test_breadth_refuses_a_thin_sample() -> None:
    """A percentage off 12 rows is not a market reading."""
    thin = [{"price": 10.0, "sma50": 9.0, "sma200": 8.0} for _ in range(12)]
    b = S.breadth(thin, None)
    check("breadth is None on a 12-row sample", b["above50"] is None, str(b["above50"]))
    check("no regime label without breadth", b["label"] is None)

    wide = [{"price": 10.0, "sma20": 9.0, "sma50": 9.0, "sma200": 8.0,
             "r1w": 1.0, "r1m": 2.0} for _ in range(200)]
    b2 = S.breadth(wide, None)
    check("all-above-MAs reads 100%", b2["above50"] == 100.0, str(b2["above50"]))
    check("that classifies as STRONG BULL", b2["label"] == "STRONG BULL", str(b2["label"]))
    check("advancing counted", b2["advancing"] == 200 and b2["declining"] == 0)

    bear = [{"price": 8.0, "sma20": 9.0, "sma50": 9.0, "sma200": 10.0,
             "r1w": -1.0, "r1m": -3.0} for _ in range(200)]
    check("all-below-MAs classifies as STRONG BEAR",
          S.breadth(bear, None)["label"] == "STRONG BEAR")
    # Rows missing an average are excluded from that average's denominator, not
    # counted as below it — a recent listing has no 200DMA, not a failed one.
    mixed = wide[:100] + [{"price": 10.0, "sma20": 9.0, "sma50": 9.0} for _ in range(100)]
    check("a missing SMA200 leaves the 200DMA sample rather than failing it",
          S.breadth(mixed, None)["above200"] == 100.0)


def test_breadth_is_not_an_input_to_any_score() -> None:
    """Pinned as a design invariant, not an implementation detail.

    The screen is rebuilt weekly and breadth turns over in days. Blending it
    into the composite would let a three-week-old regime silently move this
    week's ranks with no way for a reader to tell which vintage moved them.
    """
    src = pathlib.Path("stock_screen.py").read_text(encoding="utf-8")
    comp = src[src.index("def _composite("):src.index("def build(")]
    check("_composite does not read breadth", "breadth" not in comp)
    for fn in ("def score_quality(", "def score_growth(", "def score_technical("):
        body = src[src.index(fn):]
        body = body[:body.index("\n\ndef ")]
        check(f"{fn.split('(')[0][4:]} does not read breadth", "breadth" not in body)


def test_narrative_guard_rejects_invented_numbers() -> None:
    """The whole reason the prose layer is allowed to exist.

    A model writing about a company it half-remembers produces a fluent
    sentence containing a market share or a promoter stake that is simply
    invented, and on this page that would be indistinguishable from the
    computed figures beside it.
    """
    row = {"sym": "TCS", "name": "Tata Consultancy Services", "ind": "IT",
           "has_stmts": True, "roce_med": 62.2, "roe_med": 45.9, "pe": 21.3,
           "rev_cagr": 5.8, "years": []}
    sheet, allowed = S._facts_for(row)
    check("the fact sheet carries the real figures", "62.2" in sheet and "21.3" in sheet)
    check("it does not carry figures that were never supplied", "88.4" not in allowed)

    # Grounded output survives.
    S._attach_narrative([dict(row)], ai=lambda p, max_tokens=0:
                        "Returns on capital of 62.2% sit against a PE of 21.3, "
                        "so the quality is real and only partly in the price.")
    good = dict(row)
    S._attach_narrative([good], ai=lambda p, max_tokens=0:
                        "Returns on capital of 62.2% sit against a PE of 21.3, "
                        "so the quality is real and only partly in the price.")
    check("a grounded paragraph is kept", bool(good.get("ai_view")), str(good.get("ai_view"))[:60])

    # An invented market share is rejected wholesale — no partial repair.
    bad = dict(row)
    S._attach_narrative([bad], ai=lambda p, max_tokens=0:
                        "Returns on capital of 62.2% and a 31% share of the "
                        "Indian IT market make this a compounder.")
    check("a paragraph containing an unsupplied number is DROPPED",
          not bad.get("ai_view"), str(bad.get("ai_view"))[:80])

    # Advice language is rejected even when every number checks out.
    advice = dict(row)
    S._attach_narrative([advice], ai=lambda p, max_tokens=0:
                        "With a PE of 21.3 this is a buy and the target is clear "
                        "for patient investors who want returns of 62.2%.")
    check("advice language is DROPPED even when the numbers are real",
          not advice.get("ai_view"), str(advice.get("ai_view"))[:80])

    # No key means no prose and no crash.
    quiet = dict(row)
    S._attach_narrative([quiet], ai=None)
    check("no AI callable leaves the row untouched", "ai_view" not in quiet)

    # A row with no statements is never narrated — there is nothing to ground on.
    nostmt = dict(row, has_stmts=False)
    S._attach_narrative([nostmt], ai=lambda p, max_tokens=0: "Anything at all here.")
    check("a company with no accounts gets no narrative", "ai_view" not in nostmt)


def test_universe_excludes_nse_dummy_constituents() -> None:
    """NSE's own Nifty 500 CSV ships four placeholder 'Dummy Vedanta' rows."""
    if not pathlib.Path(S.UNIVERSE_CSV).exists():
        check("universe CSV present (skipped — not in checkout)", True)
        return
    uni = S.universe()
    syms = {u["symbol"] for u in uni}
    check("the universe loaded", len(uni) > 400, f"{len(uni)} rows")
    check("no DUMMY* placeholder survives",
          not [s for s in syms if s.startswith("DUMMY")],
          str([s for s in syms if s.startswith("DUMMY")]))
    check("no placeholder DU-prefixed ISIN survives",
          not [u for u in uni if u["isin"].startswith("DU")])
    check("ISIN de-duplication left no duplicate symbols", len(syms) == len(uni))


# ─────────────────────────────────────────────────────────────────────────────
# CROSS-RENDERER CONSISTENCY
# ─────────────────────────────────────────────────────────────────────────────

def test_screen_table_columns_match() -> None:
    """The <thead>, the server-rendered row and the live JS row must agree.

    Exactly the invariant test_engine_regressions pins for the alert table, and
    for the same reason: a mismatch shifts every cell one column right in one
    renderer only, which no Python test and no HTML diff would notice.
    """
    tpl = pathlib.Path("newspaper.py").read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")

    sec = re.search(r'<table class="t" id="scrTable".*?</thead>', tpl, re.S)
    check("screen thead found", sec is not None)
    if not sec:
        return
    n_head = sec.group(0).count("<th ")

    row = re.search(r'\{% for s in stock_screen\.rows\[:25\] %\}(.*?)\{% endfor %\}',
                    tpl, re.S)
    check("server-rendered screen row found", row is not None)
    if not row:
        return
    n_row = len(re.findall(r"<td[ >]", row.group(1)))

    live = re.search(r"function rowHtml\(r\)\{(.*?)\n    \}", js, re.S)
    check("live screen row renderer found", live is not None)
    if not live:
        return
    body = live.group(1)
    # Literal <td> opens, plus one per scoreCell() call — that helper emits
    # exactly one cell and its markup lives outside this function.
    n_live = body.count("<td") + body.count("scoreCell(num")

    check("screen table columns agree across all three renderers",
          n_head == n_row == n_live,
          f"thead={n_head} ssr={n_row} live={n_live}")
    check("the empty-state colspan matches the column count",
          f'colspan="{n_head}"' in js,
          f"expected colspan={n_head}")


def test_the_screen_sheet_can_always_be_closed() -> None:
    """The screen must wire its own ✕, backdrop, Escape and popstate.

    #sheet is shared with the ledger's trade sheet, which wires all four — but
    inside wireSheet(), which only runs after the live /api layer answers. On a
    static host or during an outage those listeners do not exist, while the
    screen's sheet still opens because its data is a flat file. Verified in a
    real browser at 375px: Escape did nothing, the backdrop did nothing, the ✕
    did nothing, and body overflow stayed hidden — the reader was trapped.
    """
    js = APP_JS.read_text(encoding="utf-8")
    block = js[js.index("stock screen ══"):]
    check("the screen defines its own close function", "function closeStock(" in block)
    check("...and calls it at init, not from the fetch callback",
          "wireSheetClose();" in block)
    check("Escape closes it", re.search(r"key === 'Escape'\s*\)\s*closeStock", block) is not None)
    check("the backdrop closes it", "ev.target === box) closeStock" in block)
    check("the ✕ closes it", "getElementById('sheetX')" in block)
    check("Back closes it", "popstate" in block and "closeStock(false)" in block)
    check("closing restores body scroll",
          re.search(r"closeStock[\s\S]{0,400}body\.style\.overflow = ''", block) is not None)


def test_app_js_carries_no_jinja() -> None:
    """app.js is a static file, not a template. A Jinja tag there ships raw."""
    js = APP_JS.read_text(encoding="utf-8")
    check("no Jinja statement tags in app.js", "{%" not in js)
    check("no Jinja expression tags in app.js", "{{" not in js)


def test_the_screener_ui_lives_in_the_source_not_the_artefact() -> None:
    """The screener JS must be in static/app.js, the file generate.py copies FROM.

    Written after losing the entire UI once. It was authored into docs/app.js,
    which reads like the real file — 165KB, its own header explaining why it is
    no longer a string inside newspaper.py — but generate.py rewrites it from
    static/app.js on every run. The page then rendered a perfect table with no
    sorting, no filtering, no detail sheet and no fetch, and every Python test
    still passed because they were all reading the overwritten copy.
    """
    check("static/app.js exists and is the source", APP_JS.exists())
    src = APP_JS.read_text(encoding="utf-8")
    check("the screener block is in static/app.js",
          "stock screen" in src and "function rowHtml(r)" in src)
    check("the screener fetches its payload from static/app.js",
          "'/screen.json'" in src)
    # generate.py must still be the thing that copies it, or this test is
    # guarding a relationship that no longer exists.
    gen = pathlib.Path("generate.py").read_text(encoding="utf-8")
    check("generate.py copies static/app.js into the output",
          '"static" / "app.js"' in gen)
    if DOCS_APP_JS.exists():
        check("docs/app.js is in sync with its source "
              "(run generate.py if this fails)",
              DOCS_APP_JS.read_text(encoding="utf-8") == src,
              f"source {len(src)}B vs artefact {len(DOCS_APP_JS.read_text(encoding='utf-8'))}B")


def test_screen_json_is_allow_listed_in_all_three_places() -> None:
    """Written by generate.py, named in .vercelignore, copied by build.js.

    today.json had two of the three and 404'd in production with no error
    anywhere. This is that trap, pinned.
    """
    gen = pathlib.Path("generate.py").read_text(encoding="utf-8")
    ign = pathlib.Path(".vercelignore").read_text(encoding="utf-8")
    bld = pathlib.Path("vercel-news/build.js").read_text(encoding="utf-8")
    check("generate.py writes docs/screen.json", 'screen.json"' in gen or "screen.json'" in gen)
    check(".vercelignore allow-lists docs/screen.json by name",
          "!docs/screen.json" in ign)
    check("build.js copies screen.json into public/", '"screen.json"' in bld)


def main() -> int:
    print("stock screen — indicator arithmetic and honesty invariants\n")
    for fn in (test_rsi_matches_hand_arithmetic,
               test_rsi_edges,
               test_sma_and_ema,
               test_macd_measures_acceleration,
               test_atr,
               test_cagr_refuses_meaningless_cases,
               test_pct_change_guards,
               test_newly_listed_stock_gets_no_long_averages,
               test_zero_volume_does_not_divide,
               test_flat_series_is_not_a_breakout,
               test_breakout_excludes_today_from_its_window,
               test_relative_strength_needs_a_benchmark,
               test_band_never_rewards_missing_data,
               test_blend_lowers_confidence_rather_than_the_score,
               test_bank_scores_without_roce_instead_of_being_punished,
               test_negative_equity_is_not_a_clean_balance_sheet,
               test_composite_renormalises_over_present_scores,
               test_a_company_with_no_statements_cannot_be_ranked,
               test_one_off_margin_jump_is_flagged_not_celebrated,
               test_rsi_above_seventy_scores_worse_not_better,
               test_eps_growth_is_withheld_across_a_share_count_change,
               test_ratios_prefer_the_median_over_a_one_off_year,
               test_missing_statements_produce_a_price_only_row,
               test_swot_lines_always_carry_their_evidence,
               test_no_forecast_language_anywhere_in_the_output,
               test_compact_strips_nulls_but_keeps_falsey_numbers,
               test_coverage_survives_compaction,
               test_breadth_refuses_a_thin_sample,
               test_breadth_is_not_an_input_to_any_score,
               test_narrative_guard_rejects_invented_numbers,
               test_a_new_cached_field_invalidates_old_entries,
               test_universe_excludes_nse_dummy_constituents,
               test_screen_table_columns_match,
               test_the_screen_sheet_can_always_be_closed,
               test_app_js_carries_no_jinja,
               test_the_screener_ui_lives_in_the_source_not_the_artefact,
               test_screen_json_is_allow_listed_in_all_three_places):
        try:
            fn()
        except Exception as e:                       # noqa: BLE001
            FAILURES.append(f"{fn.__name__} raised {type(e).__name__}: {e}")
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")

    print(f"\n{PASSES} passed · {len(FAILURES)} failed")
    for f in FAILURES:
        print(f"  · {f}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
