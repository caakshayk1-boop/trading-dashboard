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


def test_explicit_gate_blocks_and_is_applied_everywhere() -> None:
    """One gate, every ingestion path. Fails closed.

    Broadening the content sources is what made this necessary: seven finance
    feeds could not surface adult or graphic material, and twenty YouTube
    channels plus general-interest essay publishers can. A topical gate is not a
    safety gate — "sex scandal wipes 20% off the share price" clears every
    finance token test there is.
    """
    from content_cache import is_explicit
    must_block = [
        "The OnlyFans economy explained",
        "A second sexual revolution",
        "Sex scandal wipes 20% off the share price",   # clears the finance gate
        "Nude photos leaked in the breach",
        "Satta matka betting tips for today",
        "Double your money guaranteed profit",
        "Report on underage exploitation",
        "Graphic footage of the beheading",
    ]
    for t in must_block:
        check(f"blocked: {t[:38]}", is_explicit(t), "LEAKED")

    # Short words are word-boundary matched, so these must NOT trip it.
    must_pass = [
        "Essex County pension fund returns",
        "A unisex clothing brand files for IPO",
        "Grape harvest hits a record in Nashik",
        "Sextant navigation and the history of longitude",
        "Protein restriction in modern kidney care",
        "Why UPI MDR affects your monthly SIP costs",
    ]
    for t in must_pass:
        check(f"passed:  {t[:38]}", not is_explicit(t), "FALSE POSITIVE")

    check("the gate reads every field it is given, not just the title",
          is_explicit("Quarterly market update", "the CEO's sex tape leaked"))

    # And it is actually WIRED IN at all three ingestion points.
    cc = pathlib.Path("content_cache.py").read_text(encoding="utf-8")
    pod = pathlib.Path("podcasts.py").read_text(encoding="utf-8")
    check("smart reads apply the gate", "if is_explicit(title, summary)" in cc)
    check("the news wire applies the gate",
          cc.count("is_explicit(title, summary)") >= 2, str(cc.count("is_explicit(title, summary)")))
    check("podcasts apply it to the title", "_explicit(title)" in pod)
    check("podcasts apply it to the description too", "_explicit(prose)" in pod)
    check("podcasts keep a working fallback if the import fails",
          "def _explicit(" in pod and "onlyfans" in pod.lower())


def test_time_stop_horizon_comes_from_the_signal() -> None:
    """29 multibagger ideas were force-closed after 32-60 days of a 365-day hold.

    MAX_HOLD_HOURS was keyed by timeframe and had no "1W" entry, so every
    weekly-bar signal fell through to a 20-day default. GLAND was filed
    2026-07-11 with metadata {"engine":"multibagger","horizon":"6-12 months"}
    and booked EXPIRED on 2026-08-11 at +14.86%. The row stated its horizon
    twice and the time stop read a lookup table instead.
    """
    import standalone_scan as SS
    f = SS._max_hold_hours

    check("multibagger on 1W gets a year, not 20 days",
          f("1W", engine="multibagger") == 365 * 24, str(f("1W", engine="multibagger")))
    check("ai_longterm on 1W gets three years",
          f("1W", engine="ai_longterm") == 3 * 365 * 24)
    check("the engine outranks the timeframe",
          f("15m", engine="multibagger") == 365 * 24)
    check("a stated horizon is parsed at its UPPER bound",
          f("1W", horizon="6-12 months") == 12 * 30 * 24,
          str(f("1W", horizon="6-12 months")))
    check("'2-3 years' parses to three years", f("1W", horizon="2-3 years") == 3 * 365 * 24)

    # The two spellings that were missing entirely.
    check("'1W' now resolves at all", f("1W") == 20 * 24 * 7, str(f("1W")))
    check("an unknown timeframe REFUSES to expire rather than defaulting to 20d",
          f("LONG") is None and f("banana") is None and f("") is None)

    # And the horizons that feed the measured edge are untouched.
    check("1H still 48h", f("1H") == 48)
    check("4H still 8 days", f("4H") == 48 * 4)
    check("SWING still 20 days", f("SWING") == 20 * 24)
    check("15m still 6h", f("15m") == 6)
    check("Monthly still 180 days", f("Monthly") == 180 * 24)

    # The repair only ever targets EXPIRED rows closed early.
    import fix_horizons as FH
    rows = [
        {"id": 1, "symbol": "GLAND", "status": "EXPIRED", "signal_type": "multibagger",
         "timeframe": "1W", "metadata": '{"engine":"multibagger","horizon":"6-12 months"}',
         "date": "2026-07-11", "closed_at": "2026-08-11T13:09:57+05:30", "pnl_pct": 14.86},
        {"id": 2, "symbol": "FOO", "status": "EXPIRED", "signal_type": "cf_1h",
         "timeframe": "1H", "metadata": "{}",
         "date": "2026-08-01", "closed_at": "2026-08-11T00:00:00+05:30", "pnl_pct": -1.0},
        {"id": 3, "symbol": "BAR", "status": "SL_HIT", "signal_type": "multibagger",
         "timeframe": "1W", "metadata": '{"engine":"multibagger"}',
         "date": "2026-07-11", "closed_at": "2026-08-11T00:00:00+05:30", "pnl_pct": -9.0},
    ]
    ids = {w["id"] for w in FH.audit(rows)}
    check("the early-closed multibagger is flagged", 1 in ids)
    check("a legitimately expired 1H trade is NOT flagged", 2 not in ids)
    check("a row that HIT ITS STOP is never touched", 3 not in ids, str(ids))


def test_trend_names_a_peak_instead_of_calling_it_rising() -> None:
    """The SWOT and the AI view contradicted each other on the live page.

    Zydus ROCE runs FY23 14.5 -> FY26 18.0 with a median of 19.15. Comparing
    only the endpoints said RISING, so the SWOT printed "return on capital is
    improving year on year" directly above an analyst view that correctly said
    18.0% is below the 3-year median and capital efficiency is weakening. Both
    were arithmetically true and together they were nonsense.
    """
    zydus = [18.0, 22.6, 20.3, 14.5]          # newest first
    check("a series above its start but below its median reads 'peaked'",
          S._trend(zydus) == "peaked", str(S._trend(zydus)))
    check("a steadily rising series still reads 'rising'",
          S._trend([30, 25, 20, 15]) == "rising")
    check("a steadily falling series still reads 'falling'",
          S._trend([15, 20, 25, 30]) == "falling")
    check("a genuinely flat series still reads 'flat'",
          S._trend([20, 20.5, 19.8, 20.1]) == "flat")
    check("a full round trip reads 'peaked', not 'flat'",
          S._trend([15, 25, 24, 14.5]) == "peaked")
    check("too few points is still None", S._trend([18, 20]) is None)

    # And a peaked ROCE must be a WEAKNESS, never the "improving" strength.
    stmts = {"years": [
        {"fy": "FY26", "period_end": "2026-03-31", "roce_ic": 0.180, "revenue": 100.0},
        {"fy": "FY25", "period_end": "2025-03-31", "roce_ic": 0.226, "revenue": 95.0},
        {"fy": "FY24", "period_end": "2024-03-31", "roce_ic": 0.203, "revenue": 90.0},
        {"fy": "FY23", "period_end": "2023-03-31", "roce_ic": 0.145, "revenue": 85.0},
    ], "shares_changed": False}
    r = S.ratios(stmts, None)
    r["industry"] = "Pharmaceuticals"
    check("the ratio block reports the peak", r["roce_trend"] == "peaked",
          str(r["roce_trend"]))
    sw = S.swot(r, S.technicals(_px(300), None), {})
    strengths = " ".join(i["t"] for i in sw["s"]).lower()
    weaknesses = " ".join(i["t"] for i in sw["w"]).lower()
    check("'improving year on year' is NOT claimed on a peaked series",
          "improving year on year" not in strengths, strengths[:120])
    check("being off the peak is reported as a weakness",
          "off its peak" in weaknesses, weaknesses[:140])


def test_universe_is_the_official_total_market_list() -> None:
    """750 real names, and a shrink must never be silent.

    There is no "Nifty 1000". NSE's widest published equity index is Total
    Market at 752, which is exactly Nifty 500 + Microcap 250 and carries two
    DUMMY placeholders. Composing a universe by hand instead would drift from
    the index it claims to be at the next rebalance, and then every breadth
    number is measured against something that does not exist.
    """
    p = pathlib.Path(S.UNIVERSE_CSV)
    check("the Total Market list is the configured universe",
          "totalmarket" in S.UNIVERSE_URL)
    check("a 500-name fallback exists for when NSE refuses",
          S.UNIVERSE_FALLBACK_CSV.endswith("nifty500.csv"))
    if not p.exists():
        check("universe CSV present (skipped — not in checkout)", True)
        return
    uni = S.universe()                        # no refresh: offline test
    check("the universe is ~750 names", 700 < len(uni) <= 760, f"{len(uni)}")
    check("no DUMMY placeholder survives",
          not [u for u in uni if u["symbol"].startswith("DUMMY")])
    check("no duplicate symbols", len({u["symbol"] for u in uni}) == len(uni))

    # The universe file must be COMMITTED, not gitignored. NSE 403s datacenter
    # IPs, so a CI run that cannot fetch it would silently screen 500 instead of
    # 750 with nothing in the log to say the universe shrank.
    ign = pathlib.Path(".gitignore").read_text(encoding="utf-8")
    check("the universe file is un-ignored so CI actually gets it",
          "!cache/nifty_total_market.csv" in ign)
    src = pathlib.Path("stock_screen.py").read_text(encoding="utf-8")
    check("a fallback to the 500 list is logged as an ERROR, not a warning",
          "log.error(f\"screen: {path} missing" in src)
    check("the payload names the list actually read, not the one intended",
          "fallback — Total Market unavailable" in src)


def test_telegram_screen_command_matches_the_browser_presets() -> None:
    """The same preset word must select the same companies in both places.

    Otherwise /screen cheap and the website's "Cheap & good" button disagree
    about what cheap means, and the bot quietly becomes a second, different
    product.
    """
    import telegram_bot as TB
    js = APP_JS.read_text(encoding="utf-8")
    block = js[js.index("var PRESETS = {"):js.index("function capBand(")]
    for name in ("quality", "cheap", "growth", "breakout", "rs", "oversold", "debtfree"):
        # the browser calls it cheapquality; the bot shortens it for typing
        js_name = "cheapquality" if name == "cheap" else name
        check(f"preset '{name}' exists in the browser too", js_name + ":" in block)
        check(f"preset '{name}' exists in the bot", name in TB._SCREEN_PRESETS)

    rows = [
        {"sym": "A", "q": 70, "rev_cagr": 15, "de": 0.05, "rsi": 30, "tier": "micro",
         "pe_pctile": 80, "rs1y": 20, "brk20": True, "comp": 70},
        {"sym": "B", "q": 40, "rev_cagr": 2, "de": -5.0, "rsi": 60, "tier": "large",
         "pe_pctile": 10, "rs1y": -20, "comp": 30},
    ]
    P = TB._SCREEN_PRESETS
    check("quality selects the good one", [r["sym"] for r in rows if P["quality"](r)] == ["A"])
    check("debtfree excludes NEGATIVE equity, same as the browser",
          [r["sym"] for r in rows if P["debtfree"](r)] == ["A"])
    check("oversold selects RSI under 35", [r["sym"] for r in rows if P["oversold"](r)] == ["A"])
    check("micro selects on NSE index membership",
          [r["sym"] for r in rows if P["micro"](r)] == ["A"])
    check("an unknown symbol reply does not raise",
          isinstance(TB._screen_reply("/screen ZZZNOTREAL"), str))


def test_ranking_modes_actually_reorder_the_table() -> None:
    """A good company and a good thing to buy today are different questions.

    The whole point of modes: if investor and swing produce the same ordering
    then they are decoration, not a feature. This asserts the ORDER FLIPS for a
    pair chosen to separate them — an excellent, fully-priced business with a
    weak chart against a mediocre one breaking out.
    """
    quality_expensive = {"quality": {"score": 95.0}, "growth": {"score": 30.0},
                        "valuation": {"score": 10.0}, "technical": {"score": 25.0}}
    weak_hot = {"quality": {"score": 35.0}, "growth": {"score": 55.0},
                "valuation": {"score": 50.0}, "technical": {"score": 98.0}}
    a, b = S.mode_scores(quality_expensive), S.mode_scores(weak_hot)

    check("investor prefers the quality business", a["investor"] > b["investor"],
          f"{a['investor']} vs {b['investor']}")
    check("swing prefers the breakout", b["swing"] > a["swing"],
          f"{b['swing']} vs {a['swing']}")
    check("so the ranking ORDER genuinely flips between modes",
          (a["investor"] > b["investor"]) and (b["swing"] > a["swing"]))
    check("every mode is present", set(a) == {"balanced", "investor", "positional", "swing"})
    check("no statements means no mode score at all",
          all(v is None for v in S.mode_scores(quality_expensive, has_stmts=False).values()))

    # Weights are declared in ONE place over a shared component set, so a
    # component added later appears in every mode that names it.
    src = pathlib.Path("stock_screen.py").read_text(encoding="utf-8")
    # The invariant is that no MODE has its own scoring function — components
    # may be added freely, which is the whole point of a registry. An earlier
    # version of this counted `def score_` and capped it at 4, which failed the
    # moment cash flow and earnings momentum landed: it was testing that the
    # design was never extended rather than that it was respected.
    check("modes are weight sets over shared components", "MODES = {" in src)
    check("every mode weight set is a dict of component names",
          all(isinstance(w, dict) and w for w in S.MODES.values()))
    for m in S.MODES:
        check(f"mode '{m}' has no scoring function of its own",
              f"def score_{m}(" not in src)
    check("every weighted component has exactly one scoring function",
          all(f"def score_{c}(" in src or c in ("valuation",)
              for w in S.MODES.values() for c in w),
          str([c for w in S.MODES.values() for c in w
               if f"def score_{c}(" not in src and c != "valuation"]))
    for comp in ("quality", "growth", "valuation", "technical"):
        check(f"'{comp}' is weighted by at least one mode",
              any(comp in w for w in S.MODES.values()))
    # These two are declared now and land with items 2 and 1; the modes must
    # already name them so nothing needs rewiring when they arrive.
    check("cashflow is already reserved by the investor mode",
          "cashflow" in S.MODES["investor"])
    check("earnings_momentum is already reserved by positional and swing",
          "earnings_momentum" in S.MODES["positional"]
          and "earnings_momentum" in S.MODES["swing"])

    # The browser must offer the same modes, keyed the same way.
    js = APP_JS.read_text(encoding="utf-8")
    for key in ("m_inv", "m_pos", "m_swing"):
        check(f"the browser knows '{key}'", key in js)
    import telegram_bot as TB
    check("the bot offers the same three modes",
          set(TB._SCREEN_MODES.values()) == {"m_inv", "m_pos", "m_swing"})


def test_universe_label_is_read_before_the_limit_truncates() -> None:
    """A `limit=120` smoke run reported "Total Market unavailable" and was wrong.

    The label was derived from len(uni) AFTER the limit sliced it, so any capped
    run published provenance claiming NSE had refused. Provenance that lies
    under a smoke test is worse than no provenance.
    """
    src = pathlib.Path("stock_screen.py").read_text(encoding="utf-8")
    i_size = src.index("universe_size = len(uni)")
    i_trim = src.index("uni = uni[:limit]")
    check("universe_size is captured BEFORE the limit truncates", i_size < i_trim)
    check("the label is computed from universe_size, not len(uni)",
          'if universe_size > 600' in src)
    check("the payload also carries the raw size", '"universe_size": universe_size' in src)


def test_risk_is_a_level_not_another_arbitrary_score() -> None:
    """LOW/MEDIUM/HIGH, and every flag names the number that raised it.

    A "risk score of 62" is unreadable without also knowing which direction is
    better, and every extra arbitrary index is one more number nobody can act
    on. The tally is internal; the LEVEL is what the page shows.
    """
    distressed = {"sector": "Industrials", "industry": "Capital Goods",
                  "debt_to_equity": 2.4, "interest_cover": 1.2, "current_ratio": 0.8,
                  "roce_trend": "falling", "roce": 0.06, "roce_med": 0.14,
                  "ebit_margin_trend": "falling", "rev_cagr3": -0.03,
                  "has_statements": True, "cagr_span": 3}
    t_bad = {"rsi14": 80, "price": 300.0, "sma200": 200.0, "atr_pct": 0.06,
             "liquid": False, "turnover_cr": 0.4, "from_high52": -0.45,
             "high52": 545.0, "sma20": 290.0, "sma50": 280.0, "atr14": 18.0}
    rk = S.risk_flags(distressed, t_bad, {"pe_pctile": 10})
    check("a distressed name reads HIGH", rk["level"] == "HIGH", rk["level"])
    check("its flags are enumerated", len(rk["flags"]) >= 8, str(len(rk["flags"])))
    check("EVERY flag carries the number that raised it",
          all(f.get("k") for f in rk["flags"]),
          str([f["t"][:30] for f in rk["flags"] if not f.get("k")]))
    check("the worst flags sort first", rk["flags"][0]["s"] == "high")
    check("insolvency and thin trading are both caught",
          any("interest" in f["t"] for f in rk["flags"])
          and any("Thinly traded" in f["t"] for f in rk["flags"]))

    healthy = {"sector": "Healthcare", "industry": "Pharma", "debt_to_equity": 0.05,
               "interest_cover": 40.0, "current_ratio": 2.4, "roce_trend": "rising",
               "roce": 0.30, "roce_med": 0.28, "rev_cagr3": 0.24,
               "ebitda_cagr3": 0.33, "has_statements": True, "cagr_span": 3,
               "pe": 24.0}
    t_good = {"rsi14": 62, "price": 100.0, "sma200": 80.0, "sma20": 95.0,
              "sma50": 90.0, "atr_pct": 0.02, "liquid": True, "turnover_cr": 30.0,
              "from_high52": -0.02, "high52": 102.0, "atr14": 2.0,
              "vol_spike": 1.8, "above_mas": 3, "ma_stack": True, "rs_1y": 0.28,
              "brk50": True}
    ok = S.risk_flags(healthy, t_good, {"pe_pctile": 75})
    check("a clean name reads LOW with no flags",
          ok["level"] == "LOW" and not ok["flags"], f"{ok['level']} {len(ok['flags'])}")
    check("negative equity is the single worst balance-sheet flag",
          any(f["s"] == "high" and "insolvent" in f["t"]
              for f in S.risk_flags(dict(healthy, debt_to_equity=-2.0),
                                    t_good, {})["flags"]))
    check("a company with no statements is flagged high for it",
          any("No annual statements" in f["t"]
              for f in S.risk_flags(dict(healthy, has_statements=False),
                                    t_good, {})["flags"]))

    # WHY NOW is separate from the SWOT on purpose.
    w = S.why_now(healthy, t_good, {"pe_pctile": 75, "peers": 30})
    check("why-now produces lines for a genuine setup", len(w) >= 5, str(len(w)))
    check("every why-now line carries its evidence too", all(i.get("k") for i in w))
    quiet = S.why_now(healthy, {"price": 100.0, "rsi14": 50}, {})
    check("a company doing nothing this week gets FEWER why-now lines",
          len(quiet) < len(w), f"{len(quiet)} vs {len(w)}")


def test_price_location_gives_levels_not_a_target() -> None:
    """No target price, ever. There is no validated predictive model here.

    "BUY 1183 / TARGET 1275" would be fabricated precision dressed as analysis.
    Every number returned must be a level already visible on the chart.
    """
    t = {"price": 100.0, "sma20": 95.0, "sma50": 90.0, "sma200": 80.0,
         "atr14": 2.0, "high52": 102.0}
    L = S.price_location(t)
    check("a preferred ZONE, not a price", L.get("zone_lo") == 90.0 and L.get("zone_hi") == 95.0,
          str(L))
    check("invalidation is an actual moving average", L.get("invalidation") == 80.0)
    check("and it says which one", L.get("invalidation_basis") == "200-day average")
    check("confirmation is one ATR beyond, from real levels", L.get("confirm") == 102.0)
    check("no 'target' key exists at all", "target" not in L and "buy" not in L)
    check("no price means no location block", S.price_location({}) == {})
    # A stock below its averages has no pullback zone to offer.
    below = S.price_location({"price": 70.0, "sma20": 95.0, "sma50": 90.0,
                              "sma200": 80.0, "atr14": 2.0})
    check("a broken chart offers no preferred zone", "zone_lo" not in below, str(below))

    src = pathlib.Path("stock_screen.py").read_text(encoding="utf-8")
    strings = re.findall(r'"([^"\\\n]{10,})"', src)
    banned = [s for s in strings if re.search(r"\btarget price\b|\bbuy at\b", s, re.I)]
    check("no target-price language in any published string", not banned, str(banned[:2]))


def test_earnings_momentum_measures_direction_not_level() -> None:
    """A 25% compounder slowing and a 12% one speeding up have the same CAGR.

    That is the gap this fills: `growth` scores the LEVEL of compounding, and the
    four-year table shows what happened, but neither can say whether the latest
    year beat the trajectory that produced it.
    """
    accel = [{"fy": "FY26", "rev_cr": 1250, "ebitda_cr": 300, "pat_cr": 200,
              "eps": 20, "ebit_margin": 22.0},
             {"fy": "FY25", "rev_cr": 1000, "ebitda_cr": 230, "pat_cr": 150,
              "eps": 15, "ebit_margin": 20.5},
             {"fy": "FY24", "rev_cr": 900, "ebitda_cr": 200, "pat_cr": 130,
              "eps": 13, "ebit_margin": 20.0},
             {"fy": "FY23", "rev_cr": 850, "ebitda_cr": 185, "pat_cr": 120,
              "eps": 12, "ebit_margin": 19.8}]
    decel = [{"fy": "FY26", "rev_cr": 1030, "ebitda_cr": 205, "pat_cr": 130,
              "eps": 13, "ebit_margin": 18.0},
             {"fy": "FY25", "rev_cr": 1000, "ebitda_cr": 230, "pat_cr": 150,
              "eps": 15, "ebit_margin": 20.5},
             {"fy": "FY24", "rev_cr": 800, "ebitda_cr": 180, "pat_cr": 115,
              "eps": 11, "ebit_margin": 20.0},
             {"fy": "FY23", "rev_cr": 650, "ebitda_cr": 140, "pat_cr": 90,
              "eps": 9, "ebit_margin": 19.5}]
    a, d = S.earnings_momentum(accel), S.earnings_momentum(decel)
    check("speeding up is labelled accelerating", a["label"] == "accelerating", a["label"])
    check("slowing down is labelled decelerating", d["label"] == "decelerating", d["label"])
    check("the decelerating one grew 25% LAST year", near(d["prior_rev_yoy"], 0.25, 0.01))
    check("...and only 3% this year", near(d["rev_yoy"], 0.03, 0.01))
    check("margin delta is in percentage points", near(a["margin_delta"], 1.5, 0.01))
    check("a slowing compounder scores BELOW an accelerating slower one",
          S.score_earnings_momentum(d)["score"] < S.score_earnings_momentum(a)["score"])
    check("fewer than three years produces no label",
          S.earnings_momentum(accel[:2])["label"] is None)
    check("no years at all is safe", S.earnings_momentum([])["label"] is None)
    check("a sign flip yields no growth rate, same as cagr()",
          S.earnings_momentum([{"rev_cr": 100, "ebitda_cr": -10},
                               {"rev_cr": 90, "ebitda_cr": -20},
                               {"rev_cr": 80, "ebitda_cr": 5}])["ebitda_yoy"] is None)


def test_cash_flow_catches_accounting_earnings() -> None:
    """ROCE and margins both come off the income statement. This is the money.

    A company can look excellent on every other component while collecting very
    little of what it books, and no ratio in quality/growth/valuation can see it.
    """
    base = {"sector": "Industrials", "industry": "Capital Goods",
            "has_statements": True, "roce": 0.22, "roce_med": 0.22,
            "rev_cagr3": 0.18, "debt_to_equity": 0.3, "interest_cover": 12.0,
            "cagr_span": 3}
    t = {"price": 100.0, "rsi14": 60, "liquid": True, "turnover_cr": 20.0,
         "sma200": 85.0, "atr_pct": 0.02}
    real = dict(base, cfo_pat=1.05, fcf_pat=0.75, fcf_margin=0.16)
    paper = dict(base, cfo_pat=0.42, fcf_pat=0.05, fcf_margin=0.01)
    burn = dict(base, cfo_pat=-0.30, fcf_pat=-0.5, fcf_margin=-0.05)

    check("a real compounder scores high on cash",
          S.score_cashflow(real)["score"] > 70, str(S.score_cashflow(real)["score"]))
    check("paper earnings score near zero",
          S.score_cashflow(paper)["score"] < 15, str(S.score_cashflow(paper)["score"]))
    check("paper earnings raise a HIGH flag",
          any(f["s"] == "high" and "on paper" in f["t"]
              for f in S.risk_flags(paper, t, {})["flags"]))
    check("cash-burning operations raise their own flag",
          any("consumed cash" in f["t"] for f in S.risk_flags(burn, t, {})["flags"]))
    check("good conversion is a stated strength",
          any("operating cash" in i["t"] for i in S.swot(real, t, {})["s"]))
    # "Only -30% of profit arrives as cash, the rest is in receivables" is
    # nonsense: a negative ratio means cash was BURNED, a different statement.
    burn_w = " ".join(i["t"] for i in S.swot(burn, t, {})["w"])
    check("a negative ratio is described as burning cash, not as a small share",
          "consumed cash" in burn_w and "the rest is sitting" not in burn_w, burn_w[:120])
    check("lenders are exempt from the cash flags",
          not [f for f in S.risk_flags(dict(base, sector="Financial Services",
                                            industry="Banks", cfo_pat=0.3),
                                       t, {})["flags"] if "cash" in f["t"].lower()])
    check("cashflow is the component the investor mode weights",
          "cashflow" in S.MODES["investor"] and "cashflow" not in S.MODES["swing"])


def test_deltas_separate_unchanged_from_never_seen() -> None:
    """"Unchanged" and "new" are different facts and zero would hide the better one."""
    prev = {"built_on": "2026-08-04", "rows": [
        {"sym": "AAA", "comp": 61.0, "q": 70.0, "roce": 18.0},
        {"sym": "BBB", "comp": 80.0, "q": 85.0, "roce": 30.0},
        {"sym": "CCC", "comp": 50.0, "q": 40.0, "roce": 10.0}]}
    rows = [{"sym": "BBB", "comp": 81.0, "q": 85.0, "roce": 31.0},
            {"sym": "AAA", "comp": 78.0, "q": 76.0, "roce": 21.0},
            {"sym": "DDD", "comp": 70.0, "q": 72.0, "roce": 25.0},
            {"sym": "CCC", "comp": 50.0, "q": 40.0, "roce": 10.0}]
    meta = S.attach_deltas(rows, prev)
    by = {r["sym"]: r for r in rows}
    check("the summary names the build it compared against",
          meta["compared_with"] == "2026-08-04")
    check("a 17-point gain is recorded", by["AAA"]["delta"]["comp"] == 17.0,
          str(by["AAA"].get("delta")))
    check("only components that MOVED get a delta", "q" not in by["BBB"]["delta"])
    check("a genuinely unchanged row carries no delta at all", "delta" not in by["CCC"])
    check("a symbol not in the previous build is NEW, not a zero delta",
          by["DDD"].get("is_new") is True and "delta" not in by["DDD"])
    check("rank movement is stored as places GAINED",
          by["CCC"].get("rank_move") == -1, str(by["CCC"].get("rank_move")))
    check("no previous build means everything is new",
          S.attach_deltas([{"sym": "X", "comp": 1}], None)["new"] == 1)
    check("deltas are computed BEFORE compaction strips the nulls",
          pathlib.Path("stock_screen.py").read_text(encoding="utf-8")
          .index("attach_deltas(out, prev)")
          < pathlib.Path("stock_screen.py").read_text(encoding="utf-8")
          .index("out = [_compact(r) for r in out]"))


def test_no_text_builder_raises_on_sparse_data() -> None:
    """Every text-producing function, over every combination of missing fields.

    Written after `or 0` coercion killed a completed 35-minute build at row ~400:

        if (r.get("ebitda_cagr3") or 0) > (r.get("rev_cagr3") or 0) + 0.05:
            add(..., f"EBITDA CAGR {r['ebitda_cagr3']:.1%} vs {r['rev_cagr3']:.1%}")

    `(0.20 or 0) > (None or 0) + 0.05` is TRUE, so a company with EBITDA growth
    and no revenue figure passed the guard and then formatted None. The guard and
    the formatter disagreed about which fields were present.

    Real rows are sparse in every imaginable combination — 750 companies
    guarantee it — so this brute-forces the sparsity rather than trusting each
    guard to have been written correctly.
    """
    import itertools
    numeric = ["roce", "roce_med", "roe", "roe_med", "ebit_margin",
               "ebit_margin_med", "debt_to_equity", "interest_cover",
               "current_ratio", "rev_cagr3", "ebitda_cagr3", "eps_cagr3",
               "rev_growth_latest", "pe", "pb", "cfo_pat", "fcf_pat",
               "fcf_margin", "margin_one_off"]
    tech_keys = ["price", "rsi14", "sma20", "sma50", "sma200", "atr14",
                 "atr_pct", "vol_spike", "high52", "low52", "from_high52",
                 "rs_1y", "rs_3m", "above_mas", "turnover_cr"]

    full_r = {k: 0.2 for k in numeric}
    full_r.update({"sector": "Industrials", "industry": "Capital Goods",
                   "has_statements": True, "cagr_span": 3, "fy_count": 4,
                   "roce_basis": "invested capital", "shares_changed": False,
                   "roce_trend": "rising", "ebit_margin_trend": "flat",
                   "years": [{"fy": "FY26", "rev_cr": 100, "ebitda_cr": 20,
                              "pat_cr": 10, "eps": 5, "ebit_margin": 20.0,
                              "roce": 20.0, "de": 0.3},
                             {"fy": "FY25", "rev_cr": 90, "ebitda_cr": 17,
                              "pat_cr": 9, "eps": 4.5, "ebit_margin": 19.0,
                              "roce": 19.0, "de": 0.3},
                             {"fy": "FY24", "rev_cr": 80, "ebitda_cr": 15,
                              "pat_cr": 8, "eps": 4, "ebit_margin": 18.5,
                              "roce": 18.0, "de": 0.3}]})
    full_t = {k: 1.0 for k in tech_keys}
    full_t.update({"price": 100.0, "rsi14": 60.0, "sma200": 80.0, "sma50": 90.0,
                   "sma20": 95.0, "high52": 105.0, "above_mas": 3,
                   "ma_stack": True, "liquid": True, "brk20": True,
                   "brk50": False, "brk52w": False, "hist": 0.5,
                   "hist_prev": 0.2, "from_high52": -0.05, "turnover_cr": 20.0})

    failures = []
    # One field missing at a time — this is where the guard/formatter mismatches
    # live, and it is exhaustive over the fields that feed a format string.
    for key in numeric + tech_keys:
        r = dict(full_r)
        t = dict(full_t)
        r.pop(key, None)
        t.pop(key, None)
        for name, fn in (("swot", lambda: S.swot(r, t, {"pe_pctile": 50, "peers": 20})),
                         ("why_now", lambda: S.why_now(r, t, {"pe_pctile": 80, "peers": 20})),
                         ("risk_flags", lambda: S.risk_flags(r, t, {"pe_pctile": 10})),
                         ("updates", lambda: S.updates(r, t)),
                         ("setup_label", lambda: S.setup_label(t, r)),
                         ("price_location", lambda: S.price_location(t)),
                         ("earnings_momentum", lambda: S.earnings_momentum(r.get("years") or [])),
                         ("score_cashflow", lambda: S.score_cashflow(r)),
                         ("score_quality", lambda: S.score_quality(r)),
                         ("score_technical", lambda: S.score_technical(t))):
            try:
                fn()
            except Exception as e:                       # noqa: BLE001
                failures.append(f"{name} missing={key}: {type(e).__name__}: {e}")

    # And the fully-empty case, which is what a no-statements row really is.
    for name, fn in (("swot", lambda: S.swot({}, {}, {})),
                     ("why_now", lambda: S.why_now({}, {}, {})),
                     ("risk_flags", lambda: S.risk_flags({}, {}, {})),
                     ("updates", lambda: S.updates({}, {})),
                     ("setup_label", lambda: S.setup_label({}, {})),
                     ("price_location", lambda: S.price_location({}))):
        try:
            fn()
        except Exception as e:                           # noqa: BLE001
            failures.append(f"{name} on empty dicts: {type(e).__name__}: {e}")

    check(f"no text builder raises on any single missing field "
          f"({len(numeric + tech_keys)} fields x 10 builders)",
          not failures, "; ".join(failures[:4]))


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
               test_explicit_gate_blocks_and_is_applied_everywhere,
               test_time_stop_horizon_comes_from_the_signal,
               test_trend_names_a_peak_instead_of_calling_it_rising,
               test_universe_is_the_official_total_market_list,
               test_telegram_screen_command_matches_the_browser_presets,
               test_ranking_modes_actually_reorder_the_table,
               test_universe_label_is_read_before_the_limit_truncates,
               test_risk_is_a_level_not_another_arbitrary_score,
               test_price_location_gives_levels_not_a_target,
               test_earnings_momentum_measures_direction_not_level,
               test_cash_flow_catches_accounting_earnings,
               test_deltas_separate_unchanged_from_never_seen,
               test_no_text_builder_raises_on_sparse_data,
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
