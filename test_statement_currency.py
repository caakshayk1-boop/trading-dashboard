#!/usr/bin/env python3
"""
test_statement_currency.py — a dollar statement must never be published as rupee crore.

INFY's statements arrived in USD and were shown as ₹ crore, 90x too small, next to a rupee market cap. These checks pin the
reconciliation, the conversion, and the two ways it must NOT fire: a company that reports in rupees but has a real one-off gain,
and a company whose rate cannot be found for every year.

Offline: the currency and FX lookups are injected.   Usage: python test_statement_currency.py
"""
import fundamentals as F

fails = 0


def check(name, ok):
    global fails
    print(("PASS " if ok else "FAIL ") + name)
    fails += 0 if ok else 1


def stmts(pat_inr_cr, scale=1.0):
    y = lambda end, k: {"period_end": end, "fy": "FY", "revenue": 6 * pat_inr_cr * 1e7 * scale * k, "net_income": pat_inr_cr * 1e7 * scale * k,
                        "eps": 2.0 * scale * k, "total_debt": 1e9 * scale, "cash": 5e9 * scale, "roe": 0.25, "ebit_margin": 0.21, "shares_out": 4e9}
    return {"symbol": "X", "years": [y("2026-03-31", 1.0), y("2025-03-31", 0.95)]}


info = {"market_cap_cr": 414512.0, "pe": 13.9}          # implies about ₹29,800 cr profit
usd = stmts(29800 / 86.0, scale=1.0)                    # the same profit expressed in dollars (about 86 INR/USD)
rate = lambda cur, on: 86.0

check("reconciles: rupee profit near market cap / PE passes", F.reconciles(414512, 13.9, 29800e7) is True)
check("reconciles: a dollar profit 86x too small fails", F.reconciles(414512, 13.9, usd["years"][0]["net_income"]) is False)
check("reconciles: negative PE or missing figures cannot be checked", F.reconciles(1000, -5, 10) is None and F.reconciles(None, 10, 10) is None and F.reconciles(1000, 10, None) is None)

out = F.to_inr("INFY", usd, info, currency_of=lambda s: "USD", rate_of=rate)
check("USD statements are converted to rupees on every year", abs(out["years"][0]["net_income"] - usd["years"][0]["net_income"] * 86.0) < 1)
check("the converted profit now reconciles with the market", F.reconciles(414512, 13.9, out["years"][0]["net_income"]) is True)
check("balance sheet and per-share amounts are converted too", out["years"][0]["total_debt"] == usd["years"][0]["total_debt"] * 86.0 and abs(out["years"][0]["eps"] - 172.0) < 1e-6)
check("ratios and share counts are untouched", out["years"][0]["roe"] == 0.25 and out["years"][0]["ebit_margin"] == 0.21 and out["years"][0]["shares_out"] == 4e9)
check("the conversion is labelled, per year and overall", out["inr_converted"]["from"] == "USD" and out["years"][1]["fx"]["rate"] == 86.0)
check("the cached entry is never mutated", "fx" not in usd["years"][0] and "inr_converted" not in usd)

rupee_oneoff = stmts(300)                                # reconciliation fails (real one-off) but it reports in INR
check("a rupee reporter that fails the reconciliation is left exactly as it was", F.to_inr("RAYMOND", rupee_oneoff, {"market_cap_cr": 7886, "pe": 14.0}, currency_of=lambda s: "INR", rate_of=rate) is rupee_oneoff)
check("an unknown currency is never guessed", F.to_inr("X", usd, info, currency_of=lambda s: None, rate_of=rate) is usd)
check("a missing rate for any year leaves the statements unconverted", F.to_inr("INFY", usd, info, currency_of=lambda s: "USD", rate_of=lambda c, on: 86.0 if on.startswith("2026") else None) is usd)
check("statements that already reconcile are never looked up", F.to_inr("TCS", stmts(49210), {"market_cap_cr": 700000, "pe": 14.2}, currency_of=lambda s: 1 / 0, rate_of=rate) is not None)

print(f"\n{'FAILED' if fails else 'all passed'}")
raise SystemExit(1 if fails else 0)
