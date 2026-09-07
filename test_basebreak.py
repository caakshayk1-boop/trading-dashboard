#!/usr/bin/env python3
"""Tests for the LEDGE and KEEL detectors. No network — runs in milliseconds.

The one that matters most is test_no_lookahead: a detector that peeks at bars
after i produces a backtest that cannot be traded, and it is invisible in the
result because the equity curve just looks good.
"""
import sys, numpy as np
sys.path.insert(0, '/Users/akshaykumarkothari/Downloads/trading-dashboard')
from signals.basebreak import (prepare, ledge_signal, keel_signal, darvas_box,
                               rsi, atr, swing_lows, ENGINE_STATUS)

n_pass = n_fail = 0
def ok(name, cond, detail=""):
    global n_pass, n_fail
    if cond: n_pass += 1
    else:
        n_fail += 1
        print(f"  FAIL  {name}  {detail}")

def synth_box_breakout(box_bars=25, breakout=True, vol_mult=2.0):
    """A decline, a LONG flat base, then a close out of the top on volume.

    The base has to be long enough that darvas_box cannot extend backwards into
    the decline and still stay under the height cap — which is exactly how a
    real base differs from a pause in a downtrend."""
    rng = np.random.default_rng(7)
    up   = list(np.full(60, 300.0))
    fall = list(np.linspace(300, 210, 40))
    base = list(210 + rng.normal(0, 1.0, box_bars * 3))   # a genuine, long base
    c = np.array(up + fall + base, dtype=float)
    h = c + 1.2; l = c - 1.2
    v = np.full(len(c), 1_000_000.0)
    top = float((c[-box_bars * 3:] + 1.2).max())
    c = np.append(c, top + 5 if breakout else top - 2)
    h = np.append(h, top + 6); l = np.append(l, top - 2)
    v = np.append(v, 1_000_000.0 * vol_mult)
    return prepare(np.copy(c), h, l, c, v)

# ── indicators ──────────────────────────────────────────────────────────────
x = np.array([44,44.3,44.1,44.8,45.1,45.4,45.1,45.6,46.3,46.1,46.4,46.2,45.6,46.2,46.5]*4, float)
r = rsi(x, 14)
ok("RSI is bounded 0-100", np.all((r[~np.isnan(r)] >= 0) & (r[~np.isnan(r)] <= 100)))
ok("RSI needs n bars before it reports", np.isnan(r[5]))
a = atr(x+1, x-1, x, 14)
ok("ATR is positive", np.nanmin(a[~np.isnan(a)]) > 0)

lows = np.array([5,4,3,4,5,4,2,3,4,5,6], float)
ok("swing lows are confirmed pivots", 6 in swing_lows(lows, 3, 3), swing_lows(lows,3,3))

# ── the Darvas box ──────────────────────────────────────────────────────────
b = synth_box_breakout()
box = darvas_box(b["high"], b["low"], len(b["close"]) - 1)
ok("a box is found on a real base", box is not None)
if box:
    top, floor, span = box
    ok("box is tight", (top - floor) / top < 0.14, f"{(top-floor)/top:.3f}")
    ok("box spans at least the minimum", span >= 10, span)

# ── LEDGE ───────────────────────────────────────────────────────────────────
i = len(b["close"]) - 1
sig = ledge_signal(b, i)
ok("LEDGE fires on a clean box breakout", sig is not None)
if sig:
    ok("stop sits under the box floor", sig["stop"] < sig["meta"]["box_floor"] * 1.001)
    ok("stop is at least 1 ATR away",
       sig["entry_ref"] - sig["stop"] >= sig["meta"]["atr"] * 0.999,
       f'{sig["entry_ref"]-sig["stop"]:.2f} vs atr {sig["meta"]["atr"]:.2f}')
    ok("T2 is further than T1", sig["t2"] > sig["t1"])
    ok("every target carries its measured reach",
       0 < sig["t2_reach"] < sig["t1_reach"] < 1)
    ok("it explains itself", len(sig["why"]) >= 3)
    ok("it says how it can be wrong", "CLOSE" in sig["invalidate"])

nb = synth_box_breakout(breakout=False)
ok("no breakout, no signal", ledge_signal(nb, len(nb["close"]) - 1) is None)

lowvol = synth_box_breakout(vol_mult=1.0)
ok("a breakout on no volume is refused", ledge_signal(lowvol, len(lowvol["close"]) - 1) is None)

# A WICK THROUGH THE TOP IS NOT A BREAKOUT. This is the whole close-basis rule.
wick = synth_box_breakout()
wick["high"][-1] = wick["high"][-1] + 40      # huge spike
wick["close"][-1] = wick["close"][-2] - 1     # but closes back inside
ok("a wick through the box top does NOT fire",
   ledge_signal(wick, len(wick["close"]) - 1) is None)

# NEAR ITS HIGHS = not this engine's job.
top_of_range = synth_box_breakout()
top_of_range["close"][:120] = top_of_range["close"][-1] * 0.98   # no prior decline
ok("a name near its 52-week high is refused",
   ledge_signal(top_of_range, len(top_of_range["close"]) - 1) is None)

# ── no lookahead ────────────────────────────────────────────────────────────
def test_no_lookahead():
    """A detector run at bar i must give the identical answer whether or not
    bars after i exist. Anything else is a backtest that cannot be traded."""
    full = synth_box_breakout()
    i = len(full["close"]) - 1
    # append 30 wildly different future bars
    ext = {k: np.concatenate([v, v[-1] * np.linspace(1.0, 2.5, 30)]) for k, v in full.items()
           if k in ("open", "high", "low", "close", "volume")}
    ext = prepare(ext["open"], ext["high"], ext["low"], ext["close"], ext["volume"])
    a1, a2 = ledge_signal(full, i), ledge_signal(ext, i)
    same = (a1 is None) == (a2 is None) and (
        a1 is None or (abs(a1["stop"] - a2["stop"]) < 1e-6 and abs(a1["t1"] - a2["t1"]) < 1e-6))
    ok("LEDGE reads no bar after i", same)
    k1, k2 = keel_signal(full, i), keel_signal(ext, i)
    ok("KEEL reads no bar after i", (k1 is None) == (k2 is None))
test_no_lookahead()

# ── the status table must stay honest ───────────────────────────────────────
ok("no engine claims LIVE on a backtest alone",
   all(v["status"] in ("PAPER", "RESEARCH", "RETIRED") for v in ENGINE_STATUS.values()),
   {k: v["status"] for k, v in ENGINE_STATUS.items()})
ok("an engine below t=2 is not called PAPER-ready",
   ENGINE_STATUS["keel"]["status"] == "RESEARCH")

print(f"\n{n_pass}/{n_pass+n_fail} basebreak assertions pass" if not n_fail
      else f"\n{n_fail} of {n_pass+n_fail} FAILED")
sys.exit(1 if n_fail else 0)
