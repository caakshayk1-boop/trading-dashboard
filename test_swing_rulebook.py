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
    base = dict(id=1, symbol="TESTCO", signal_type="magicmagic", date="2026-09-10",
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
# breakout is a CANDIDATE now: +0.022R over 96 closed, the largest clean
# sample in the review. Flat, not losing, so it sizes on paper.
t, r = RB.size_signal(sig(signal_type="breakout"), {})
ok("breakout is a candidate and sizes", t is not None, r.reason if r else "sized")
# The retired ones, and the reason each is out.
for eng in ("ohl", "equity_measured", "top5_pick", "sip_bucket"):
    t, r = RB.size_signal(sig(signal_type=eng), {})
    ok(f"{eng} is retired", r and r.reason == "OUT_OF_MANDATE", r.detail[:52] if r else "sized")
# Out of mandate on the instrument or the clock, whatever the record says.
for eng in ("cf_1h", "commodity", "intraday", "4h", "ai_4h"):
    t, r = RB.size_signal(sig(signal_type=eng), {})
    ok(f"{eng} is out of mandate", r and r.reason == "OUT_OF_MANDATE")
ok("nothing is funded", RB.FUNDED == {})
# Three, and the list SHRANK rather than grew. magic and ai_longterm were
# retired on the site; magicmagic replaced magic as the live TIDAL band, which
# is a correction. LEDGE, KEEL and VECTOR are published by the site and
# deliberately still unsized — see UNSIZED_BUT_PUBLISHED. Sizing them is a
# decision about money and is Akshay's to make, not a side effect of this fix.
ok("the candidate list is a correction, not an expansion",
   set(RB.CANDIDATE) == {"magicmagic", "multibagger", "breakout"},
   str(sorted(RB.CANDIDATE)))
ok("a published engine this book does not size stays UNKNOWN",
   all(RB.tier_of(e) == "UNKNOWN" for e in ("ledge", "keel", "momentum_quant")),
   str({e: RB.tier_of(e) for e in ("ledge", "keel", "momentum_quant")}))
# Pinned deliberately. The decision not to size these was taken on 2026-09-19
# against a book measuring -0.765R at t=-2.79 — see the note beside
# UNSIZED_BUT_PUBLISHED. Adding one to CANDIDATE should require editing this
# line, which is the point: it makes the change visible in a diff instead of
# arriving as a quiet three-position increase in the admitted book.
ok("every engine named unsized is in fact unsized",
   all(e not in RB.CANDIDATE and e not in RB.FUNDED
       for e in RB.UNSIZED_BUT_PUBLISHED),
   str([e for e in RB.UNSIZED_BUT_PUBLISHED
        if e in RB.CANDIDATE or e in RB.FUNDED]))
ok("every engine sits in exactly one tier",
   all(RB.tier_of(e) != "UNKNOWN" for e in
       ["magic","multibagger","ai_longterm","breakout","ohl","equity_measured",
        "top5_pick","magicmagic","sip_bucket","ai_daily","cf_1h","commodity",
        "intraday","4h","ai_4h"]))
ok("an unknown engine is never sized", RB.tier_of("brand_new") == "UNKNOWN")
# THE PAIR WAS THE WRONG WAY ROUND AND CAPITAL FOLLOWED IT. This rulebook had
# magic as a candidate and magicmagic retired as its duplicate; the site
# decided the opposite on 2026-09-18, on the measured ground that >15% off the
# high admits everything 20-40% does plus a shallower tail. mandate.json's
# admitted list was carrying a magic position — capital sized to an engine no
# page will show. The site's retirement is binding on this file now.
t, r = RB.size_signal(sig(signal_type="magic"), {})
ok("a site-retired engine is never sized",
   r is not None and t is None, r.reason if r else "SIZED — capital on a dead engine")
ok("and it is refused for being retired, not for some weaker reason",
   r and "retired on the site" in (r.detail or ""), r.detail[:60] if r else "")
t, r = RB.size_signal(sig(signal_type="magicmagic"), {})
ok("the live TIDAL band is sized", t is not None, r.detail[:52] if r else "sized")
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

# ── AND A PRICE THAT IS MERELY CLOSE IS THE SAME FAULT ──────────────────────
#
# The collapse above only caught EXACTLY repeated prices. SPLPETRO published
# T1 938.02, T2 951.57 and T3 965.13 against 104.36 of risk — three different
# numbers, 0.13R apart — so all three survived and the book sized three exit
# legs at what is, in the units that matter, one price. The site's read layer
# blanks a pair that close, so the SAME row rendered on the ledger card as
# "All at 938.02 · the only target": the book and the page described one
# signal two incompatible ways, and only the page was right.
#
# 0.5R, the same constant as scanner.MIN_TARGET_GAP_R and the MIN_TARGET_GAP_R
# in vercel-news/api/_levels.js. A generator, a sizer and a renderer that
# disagree about what counts as a target produce exactly this.
ok("the sizer and the site agree on the constant", RB.MIN_TARGET_GAP_R == 0.5)
near = RB.distinct_targets(771.05, 666.69, [938.02, 951.57, 965.13], True)
ok("targets inside 0.5R of the one before are not separate exits",
   near == [938.02, None, None], str(near))
wide = RB.distinct_targets(2100.2, 1900.88, [2345.82, 2530.04, 2714.26], True)
ok("targets a real distance apart are all kept", wide == [2345.82, 2530.04, 2714.26])
ok("the inner target is the one kept, never the outer",
   RB.distinct_targets(100.0, 90.0, [120.0, 122.0], True) == [120.0, None])
ok("no stop means no risk to measure against, so nothing is dropped",
   RB.distinct_targets(100.0, 100.0, [120.0, 121.0], True) == [120.0, 121.0])
crammed = RB.build_ladder(771.05, 666.69, [938.02, 951.57, 965.13], 100, True)
ok("one target is one leg carrying the whole position",
   len(crammed) == 1 and crammed[0].qty == 100, f"{len(crammed)} legs")

# THE HEADLINE NUMBER MUST BELONG TO AN EXIT THE BOOK ACTUALLY PLACES.
# `final`, final_gain_pct and reward_risk read the raw target3, so a ticket
# could be admitted on a band and a ratio measured to a level build_ladder was
# about to collapse away.
# 80 of risk, so the floor is 40. T3 is 10 above T2 and is dropped; the band
# and the ratio must then read 1400, not the 1410 no leg is placed at.
t3, r3 = RB.size_signal(sig(target1=1150.0, target2=1400.0, target3=1410.0), {})
ok("the published targets are the ones that survive the collapse",
   t3 and t3.targets == [1150.0, 1400.0],
   str(t3.targets) if t3 else "rejected: " + r3.reason)
ok("the final gain is measured to the last leg, not to a dropped target",
   t3 and abs(t3.final_gain_pct - 40.0) < 0.05, str(t3.final_gain_pct) if t3 else "")
ok("every published target has a leg, and every leg a target",
   t3 and len(t3.legs) == len(t3.targets), f"{len(t3.legs)} vs {len(t3.targets)}" if t3 else "")

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

# One name, one ticket — even across two engines. Both must be engines that
# actually size, or the second is dropped as retired and nothing is deduped.
# magicmagic, not magic — the line above is exactly the trap this hit. magic
# was retired on the site on 2026-09-18 and no longer sizes, so the pair
# stopped exercising the dedupe and quietly tested nothing.
book = RB.build_book([sig(id=1, symbol="PAYTM", signal_type="magicmagic"),
                      sig(id=2, symbol="PAYTM", signal_type="breakout")], {})
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
