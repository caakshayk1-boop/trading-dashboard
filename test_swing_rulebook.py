#!/usr/bin/env python3
"""Regression checks for the Rs 1 crore mandate. `python3 test_swing_rulebook.py`."""
import sys
import swing_rulebook as RB

fail = 0
def ok(name, cond, detail=""):
    global fail
    print(f"{'PASS' if cond else 'FAIL'}  {name}{' — ' + detail if detail else ''}")
    if not cond: fail += 1

UNI = frozenset({"PAYTM", "COFORGE", "TESTCO", "DIXON"})
RB._UNIVERSE = UNI

def sig(**kw):
    base = dict(id=1, symbol="TESTCO", signal_type="top5_pick", date="2026-08-20",
                market="NSE", timeframe="1W", action="BUY", score=80,
                entry=1000.0, sl=920.0, target1=1250.0, target2=1400.0, target3=1400.0)
    base.update(kw); return base

print("── The mandate is Indian equity only ──────────────────────")
# The feed tags MSFT and SNOW as NSE. Universe membership is the real test.
t, r = RB.size_signal(sig(symbol="MSFT", market="NSE"), {})
ok("a US ticker tagged NSE is still rejected", r and r.reason == "NOT_EQUITY_INDIA",
   r.reason if r else "sized")
t, r = RB.size_signal(sig(symbol="PAYTM.NS"), {})
ok(".NS suffix is stripped and accepted", t is not None and t.symbol == "PAYTM")
ok("no universe loaded never defaults to true",
   RB.is_indian_equity("MSFT", frozenset()) is False)

print("\n── Horizons and bands ─────────────────────────────────────")
ok("max 8 alert types", len(RB.ENGINE_HORIZON) <= RB.MAX_ALERT_TYPES,
   f"{len(RB.ENGINE_HORIZON)} mapped")
ok("no intraday engine is mapped",
   not ({"intraday", "4h", "ai_4h"} & set(RB.ENGINE_HORIZON)))
t, r = RB.size_signal(sig(target1=1050.0, target2=1100.0, target3=1100.0), {})
ok("a 10% final target misses the swing band", r and r.reason == "BELOW_BAND")
t, r = RB.size_signal(sig(target1=1500.0, target2=2000.0, target3=2000.0), {})
ok("a 100% final target is above the band", r and r.reason == "ABOVE_BAND")
t, r = RB.size_signal(sig(sl=800.0), {})
ok("a 20% stop is too wide for a swing", r and r.reason == "STOP_TOO_WIDE")
t, r = RB.size_signal(sig(signal_type="breakout"), {})
ok("breakout is out of mandate", r and r.reason == "OUT_OF_MANDATE")
t, r = RB.size_signal(sig(signal_type="magic"), {})
ok("magic is named as magicmagic's duplicate", r and r.reason == "DUPLICATE_ENGINE")
t, r = RB.size_signal(sig(timeframe="15m"), {})
ok("a 15m signal is not a swing timeframe", r and r.reason == "WRONG_TIMEFRAME")
t, r = RB.size_signal(sig(score=30), {})
ok("a low score is rejected", r and r.reason == "LOW_SCORE")

print("\n── The ladder ─────────────────────────────────────────────")
t, _ = RB.size_signal(sig(target1=1250.0, target2=1350.0, target3=1400.0), {})
ok("three distinct targets give three legs", t and len(t.legs) == 3)
ok("legs sum to the whole position",
   t and sum(l["qty"] for l in t.legs) == t.qty,
   f"{sum(l['qty'] for l in t.legs)} vs {t.qty}")
ok("first leg is 20%", t and t.legs[0]["qty"] == int(t.qty * 0.20))
ok("second leg is 40%", t and t.legs[1]["qty"] == int(t.qty * 0.40))
# target2 == target3 is common in the feed; two legs at one price is not a ladder.
t2, _ = RB.size_signal(sig(target1=1250.0, target2=1400.0, target3=1400.0), {})
ok("a repeated target price collapses to one leg", t2 and len(t2.legs) == 2)
ok("collapsed legs still sum to the position",
   t2 and sum(l["qty"] for l in t2.legs) == t2.qty)
ok("the trail engages only after T2",
   t and "until T2" in t.trail_note, t.trail_note[:60] if t else "")

print("\n── Sizing and caps ────────────────────────────────────────")
ok("capital is Rs 1 crore", RB.CAPITAL == 10_000_000)
t, _ = RB.size_signal(sig(), {})
ok("risk per trade is inside Rs 75,000",
   t and t.risk_amount <= RB.CAPITAL * RB.RISK["risk_per_trade_pct"],
   f"Rs {t.risk_amount:,}")
ok("one name is inside Rs 10,00,000",
   t and t.notional <= RB.CAPITAL * RB.RISK["max_name_pct"], f"Rs {t.notional:,}")
t, r = RB.size_signal(sig(entry=2_000_000.0, sl=1_900_000.0, target1=2_600_000.0,
                          target2=2_800_000.0, target3=2_800_000.0), {})
ok("a share pricier than the name cap does not exist", r and r.reason == "BELOW_MIN_SIZE")

# One name, one ticket — even across two engines.
book = RB.build_book([sig(id=1, symbol="PAYTM", signal_type="top5_pick"),
                      sig(id=2, symbol="PAYTM", signal_type="magicmagic")], {})
ok("the same name is not sized twice",
   len(book["admitted"]) == 1 and len(book["duplicates"]) == 1)

many = [sig(id=i, symbol=s) for i, s in enumerate(["PAYTM", "COFORGE", "TESTCO", "DIXON"])]
b2 = RB.build_book(many, {})
ok("heat never exceeds its cap",
   b2["state"]["heat"] <= b2["state"]["heat_cap"],
   f"Rs {b2['state']['heat']:,} / {b2['state']['heat_cap']:,}")
ok("deployed never exceeds its cap",
   b2["state"]["deployed"] <= b2["state"]["deployed_cap"])
ok("nothing is silently dropped",
   len(b2["admitted"]) + len(b2["deferred"]) + len(b2["duplicates"]) + len(b2["rejected"]) == len(many))

print("\nALL CHECKS PASSED" if not fail else f"\n{fail} CHECK(S) FAILED")
sys.exit(1 if fail else 0)
