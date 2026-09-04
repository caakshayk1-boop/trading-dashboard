#!/usr/bin/env python3
"""Regressions for verdict.py.

Most of these are real names from the 2026-09-04 build that the first version
got WRONG, and would have published confidently. They are kept as fixtures
because each one is a different way for a ratio to lie:

  INDIGO    CFO +23,470 cr on PAT -2,392 cr. CFO/PAT reads -3.61 because the
            DENOMINATOR is negative, not because cash went missing.
  M&M       cfo_pat is a MEDIAN. FY23/FY24 negative, FY26 +11,657 cr on
            17,099 cr of profit. The company fixed it; the median lagged.
  GRASIM    CFO negative four years running — Aditya Birla Capital's loan book
            consolidates in, and loan growth is an operating outflow.
  HDFCBANK  no ROCE at all. 56 of 121 financials have none, which made a
            long-term verdict structurally unreachable for every bank.
  INDIGO    D/E 11.15 — aircraft leases under Ind AS 116, not borrowings.

Run: python3 test_verdict.py
"""
import sys

import verdict as V


def base(**kw):
    """A liquid, unremarkable mid-cap. Every test overrides only what it tests."""
    row = {
        "sym": "TEST", "sector": "Industrials", "ind": "Capital Goods",
        "tier": "mid", "price": 100.0, "liquid": True, "turnover_cr": 50.0,
        "has_stmts": True, "fy_count": 5,
        "roce": 20.0, "roe": 18.0, "rev_cagr": 15.0, "de": 0.5, "icover": 8.0,
        "cfo_cr": 500.0, "cfo_pat": 0.9, "pat_yoy": 10.0, "v": 50.0, "pe": 22.0,
        "roce_trend": "rising",
        "stack": True, "above_mas": 3, "rs3m": 5.0, "em_label": "accelerating",
        "em": 60.0, "rsi": 55.0, "sma20": 98.0, "sma50": 95.0, "sma200": 88.0,
        "atr_pct": 3.0, "vol_spike": 1.0, "from_high": -8.0, "high52": 110.0,
        "brk20": False, "brk50": False, "brk52w": False,
        "m_inv": 70.0, "m_pos": 60.0, "m_swing": 40.0,
        "q_conf": 1.0, "g_conf": 1.0, "v_conf": 1.0, "tech_conf": 1.0, "em_conf": 1.0,
    }
    row.update(kw)
    return row


P = F = 0


def check(label, got, exp):
    global P, F
    if got == exp:
        P += 1
        print(f"  PASS  {label}")
    else:
        F += 1
        print(f"  FAIL  {label} — expected {exp!r}, got {got!r}")


def call_of(**kw):
    return V.verdict(base(**kw))["call"]


def main():
    print("── AVOID is structural only ─────────────────────────────────")
    check("illiquid -> AVOID", call_of(liquid=False, turnover_cr=0.2), "AVOID")
    check("negative equity -> AVOID", call_of(de=-1.4), "AVOID")
    check("thin cover on high debt -> AVOID", call_of(icover=0.9, de=3.0), "AVOID")
    check("thin cover on LOW debt is not AVOID", call_of(icover=0.9, de=0.4) != "AVOID", True)
    check("high debt with healthy cover is not AVOID", call_of(de=3.0, icover=9.0) != "AVOID", True)

    print("\n── the CFO/PAT sign trap (INDIGO) ───────────────────────────")
    indigo = base(cfo_cr=23470.0, cfo_pat=-3.61, pat_yoy=-130.0, icover=5.0, de=1.2)
    v = V.verdict(indigo)
    check("positive CFO with negative PAT is NOT avoided", v["call"] != "AVOID", True)
    check("...and raises no cash red flag", V.cash_flag(indigo), None)

    print("\n── the stale-median trap (M&M) ──────────────────────────────")
    mm = base(cfo_cr=11657.0, cfo_pat=-0.13, roce=15.5)
    check("latest-year cash decides, not the median", V.verdict(mm)["call"], "BUY")
    check("...no cash red flag when last year was positive", V.cash_flag(mm), None)

    print("\n── genuine cash burn still flags (GRASIM-shaped) ────────────")
    burn = base(cfo_cr=-17810.0, cfo_pat=-2.75)
    check("negative latest CFO raises the flag", V.cash_flag(burn) is not None, True)
    check("...but does not by itself mean AVOID", V.verdict(burn)["call"] != "AVOID", True)
    check("...and blocks the long-term thesis", "long term" not in
          [h for h in V.HORIZONS if V._thesis_long(burn)[0] and h == "long term"], True)
    check("...and the flag is carried on the verdict",
          bool(V.verdict(burn)["red_flags"]), True)

    print("\n── lenders are judged on ROE, not ROCE (HDFCBANK) ───────────")
    bank = base(sector="Financial Services", ind="Banks", roce=None, roe=19.0,
                de=7.0, cfo_cr=-4000.0, cfo_pat=-1.2, icover=1.2)
    check("a lender is not avoided for its loan book", V.verdict(bank)["call"] != "AVOID", True)
    check("a lender raises no cash red flag", V.cash_flag(bank), None)
    ok, reasons, _, _ = V._thesis_long(bank)
    check("ROE satisfies the return bar for a lender", ok, True)
    check("...and the reason says ROE", any("ROE" in r for r in reasons), True)
    weak = base(sector="Financial Services", ind="Banks", roce=None, roe=8.6)
    check("a weak lender still fails the bar", V._thesis_long(weak)[0], False)

    print("\n── Ind AS 116 leases (INDIGO D/E 11) ────────────────────────")
    air = base(sector="Industrials", ind="Airlines", icover=1.2, de=4.0)
    check("lease-heavy D/E 4.0 clears the higher bar", V.verdict(air)["call"] != "AVOID", True)
    check("lease-heavy D/E 11 still trips it", call_of(ind="Airlines", icover=0.7, de=11.15), "AVOID")
    flags = V.verdict(base(ind="Airlines", icover=0.7, de=11.15))["red_flags"]
    check("...and the evidence names Ind AS 116",
          any("Ind AS 116" in f["evidence"] for f in flags), True)
    check("a manufacturer at D/E 4.0 is still avoided", call_of(icover=1.2, de=4.0), "AVOID")

    print("\n── horizons ─────────────────────────────────────────────────")
    check("quality compounder -> BUY long term",
          V.verdict(base(m_inv=80, m_pos=10, m_swing=10))["horizon"], "long term")
    check("trend leader -> positional",
          V.verdict(base(roce=8.0, rev_cagr=2.0, m_pos=80, m_inv=10, m_swing=10))["horizon"],
          "positional")
    swing = base(roce=8.0, rev_cagr=2.0, rs3m=-4.0, em_label="decelerating",
                 brk20=True, vol_spike=2.1, atr_pct=3.5, turnover_cr=40.0,
                 m_swing=80, m_inv=10, m_pos=10)
    check("breakout on volume -> swing", V.verdict(swing)["horizon"], "swing")
    check("...and it is a BUY", V.verdict(swing)["call"], "BUY")
    check("breakout without volume is not a swing BUY",
          V.verdict(dict(swing, vol_spike=1.0))["call"] != "BUY", True)
    check("thin turnover kills the swing",
          V.verdict(dict(swing, turnover_cr=3.0))["call"] != "BUY", True)
    check("ATR too wide kills the swing",
          V.verdict(dict(swing, atr_pct=12.0))["call"] != "BUY", True)

    print("\n── WAIT means right thesis, wrong entry ─────────────────────")
    hot = base(rsi=81.0)
    check("overbought turns BUY into WAIT", V.verdict(hot)["call"], "WAIT")
    check("...and names a trigger", bool(V.verdict(hot)["trigger"]), True)
    check("...and keeps the thesis reasons", len(V.verdict(hot)["because"]) >= 3, True)
    ext = base(price=140.0, sma50=100.0, rsi=68.0)
    check("40% above the 50-day is WAIT", V.verdict(ext)["call"], "WAIT")
    check("...trigger names the 50-day", "50-day" in (V.verdict(ext)["trigger"] or ""), True)

    print("\n── missing data never passes ────────────────────────────────")
    check("no statements -> long term unreachable",
          V._thesis_long(base(has_stmts=False))[0], False)
    check("one year of statements -> unreachable",
          V._thesis_long(base(fy_count=1))[0], False)
    check("missing ROCE blocks, does not default to 0",
          "ROCE not computable" in V._thesis_long(base(roce=None))[3], True)
    check("missing RS blocks positional", V._thesis_positional(base(rs3m=None))[0], False)

    print("\n── every verdict is well-formed ─────────────────────────────")
    for kw in ({}, {"liquid": False}, {"rsi": 81.0}, {"roce": 2.0, "rev_cagr": 1.0},
               {"cfo_cr": -900.0}, {"de": -1.0}, {"roce": None, "rev_cagr": None}):
        v = V.verdict(base(**kw))
        check(f"shape {kw or 'baseline'}",
              v["call"] in V.CALLS
              and isinstance(v["because"], list) and isinstance(v["against"], list)
              and isinstance(v["red_flags"], list)
              and v["confidence"] in ("high", "medium", "low")
              and (v["horizon"] in V.HORIZONS or v["horizon"] is None),
              True)

    print("\n── every reason carries a number ────────────────────────────")
    bad = []
    for kw in ({}, {"rsi": 81.0}, {"roce": 8.0, "rev_cagr": 2.0}, {"cfo_cr": -900.0}):
        v = V.verdict(base(**kw))
        for txt in v["because"] + v["against"]:
            if not any(c.isdigit() for c in txt):
                bad.append(txt)
    check("no reason is a bare assertion", bad, [])

    print(f"\n{P} passed · {F} failed")
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
