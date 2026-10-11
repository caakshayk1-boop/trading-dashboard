#!/usr/bin/env python3
"""test_stock_alerts.py — the Telegram alerts name alpha only and say what CHANGED, never what to do.

No network, no credentials, no pytest. Usage: python test_stock_alerts.py
"""
import inspect
import re
import sys
from datetime import date

import stock_alerts as A

A.SITE = "https://alpha.askakshay.com"   # pinned: the test must not depend on the caller's environment

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  {detail}"))
    ok, fail = ok + bool(cond), fail + (not cond)


M = date.today().month
HIT = A.SEASON_HIT + 5


def season(*syms_hits):
    arr = [None] * 12
    stocks = {}
    for sym, hit in syms_hits:
        a = list(arr)
        a[M - 1] = [hit, 4.2, 11]
        stocks[sym] = {"m": a}
    return {"stocks": stocks}


rows = [
    {"sym": "AAA", "name": "Aaa Ltd", "vet": {"s": "cleared"}},
    {"sym": "BBB", "name": "Bbb Ltd", "vet": {"s": "held"}},          # high hit rate but not vetted
    {"sym": "CCC", "name": "Ccc Ltd", "vet": {"s": "cleared"}},        # vetted but below the threshold
]
blocks = A.seasonal_window(rows, season(("AAA", HIT), ("BBB", HIT), ("CCC", A.SEASON_HIT - 10)), {})
text = "\n".join(blocks)
check("a vetted name above the hit rate is announced", "AAA" in text)
check("a name that is not vetted is never announced", "BBB" not in text)
check("a vetted name below the hit rate is not announced", "CCC" not in text)
check("links go to the company page on alpha", "alpha\\.askakshay\\.com/company/?s=AAA" in text, text[:300])
check("no screen 'call' is quoted", not re.search(r"\b(BUY|AVOID|WATCH|WAIT)\b", text))
check("announced once a month, not again", A.seasonal_window(rows, season(("AAA", HIT)), {"season_month": date.today().strftime("%Y-%m")}) == [])

baro = {"today": {"stage": {"k": "early", "t": "Slightly cheaper"}, "score": 29, "band": {"t": "Weak"},
                  "drawdown_pct": 15.7, "above_200dma_pct": 46}}
check("first sight of a stage is recorded, not announced", A.stage_change(baro, {}) == [])
moved = "\n".join(A.stage_change(baro, {"stage": "calm"}))
check("a stage move is announced with the barometer", "Slightly cheaper" in moved and "29/100" in moved)
check("a stage move links to alpha", "alpha\\.askakshay\\.com" in moved)
check("an unchanged stage says nothing", A.stage_change(baro, {"stage": "early"}) == [])

src = inspect.getsource(A)
check("the screen-call alert is gone", not hasattr(A, "verdict_changes"))
code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
check("no retired host appears in the alert code", not re.search(r"(signal|vision|gems|terminal|trade)\.askakshay", code))

print(f"\n{ok} passed · {fail} failed")
sys.exit(1 if fail else 0)
