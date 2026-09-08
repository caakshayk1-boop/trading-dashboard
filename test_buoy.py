#!/usr/bin/env python3
"""Tests for BUOY. Written before the backtest, so no number here was chosen
to make a result look better."""
import sys, numpy as np
sys.path.insert(0, '/Users/akshaykumarkothari/Downloads/trading-dashboard')
from signals.buoy import buoy_signal, dma_on_hours, prepare, attach_dma, DMA_N

P = F = 0
def ok(name, cond, detail=""):
    global P, F
    if cond: P += 1; print(f"  PASS  {name}")
    else:    F += 1; print(f"  FAIL  {name}  {detail}")

DAY = 86400
HOUR = 3600

# ── the daily average carried onto hours ─────────────────────────────────────
day_ts = np.array([i * DAY for i in range(400)])
day_c  = np.arange(400, dtype=float) + 100.0
hour_ts = np.array([d * DAY + h * HOUR for d in range(400) for h in range(6)])
carried = dma_on_hours(hour_ts, day_ts, day_c, n=DMA_N)

ok("the average is NaN until 200 days exist",
   not np.isfinite(carried[:DMA_N * 6 - 6]).any())

# Every hour inside day d must carry a value derived only from days that have
# already ENDED — never from day d itself.
bad = 0
from signals.basebreak import sma
truth = sma(day_c, DMA_N)
for d in range(DMA_N + 2, 400):
    want = truth[d - 1]                     # last COMPLETED day
    for h in range(6):
        got = carried[d * 6 + h]
        if np.isfinite(want) and abs(got - want) > 1e-9:
            bad += 1
ok("every hour uses the last COMPLETED day's average, never its own day",
   bad == 0, f"{bad} hours disagreed")

# ── the detector cannot see the future ───────────────────────────────────────
rng = np.random.default_rng(11)
n = 3000
c = np.cumsum(rng.normal(0, 1, n)) + 500
c = np.maximum(c, 50)
h = c + np.abs(rng.normal(0, 1, n)); l = c - np.abs(rng.normal(0, 1, n))
o = c + rng.normal(0, .4, n); v = rng.integers(2_000_000, 9_000_000, n).astype(float)
hts = np.array([i * HOUR for i in range(n)])
dts = np.array([i * DAY for i in range(n // 6 + 400)])
dcl = np.interp(np.arange(len(dts)), np.linspace(0, len(dts) - 1, n), c)

b = attach_dma(prepare(o, h, l, c, v, dts, dcl), hts)
i = 1200
base = buoy_signal(b, i)
# Corrupt everything after i and the answer must not change.
b2 = attach_dma(prepare(o.copy(), h.copy(), l.copy(), c.copy(), v.copy(), dts, dcl), hts)
for arr in ("open", "high", "low", "close", "volume"):
    b2[arr][i + 1:] = b2[arr][i + 1:] * 3 + 17
b2["atr14"][i + 1:] = np.nan; b2["rsi14"][i + 1:] = np.nan
after = buoy_signal(b2, i)
ok("mangling every bar after i does not change the signal at i",
   (base is None) == (after is None)
   and (base is None or base["entry"] == after["entry"] and base["sl"] == after["sl"]))

# ── the levels are internally consistent wherever it does fire ───────────────
fired, bad_lvl, bad_stop = 0, 0, 0
for i in range(300, n):
    s = buoy_signal(b, i)
    if not s:
        continue
    fired += 1
    if not (s["sl"] < s["entry"] < s["target1"] < s["target2"] < s["target3"]):
        bad_lvl += 1
    risk = s["entry"] - s["sl"]
    if abs((s["target1"] - s["entry"]) / risk - 1.6) > 0.02:
        bad_lvl += 1
    # the stop must never sit inside one ATR of entry
    if risk < b["atr14"][s["i"]] * 1.41 - 1e-6 and s["sl"] != s["struct_low"]:
        bad_stop += 1
ok("levels are ordered and T1 is exactly 1.6R", bad_lvl == 0, f"{bad_lvl} bad")
ok("the stop is never inside the ATR floor", bad_stop == 0, f"{bad_stop} bad")

# ── the gates actually gate ──────────────────────────────────────────────────
flat = np.full(n, 500.0)
bf = attach_dma(prepare(flat, flat + 1, flat - 1, flat, v, dts, np.full(len(dts), 500.0)), hts)
ok("a flat series that never fell produces nothing",
   all(buoy_signal(bf, i) is None for i in range(300, n, 7)))

# a series that is ABOVE its average throughout can never reclaim it
rise = np.linspace(100, 900, n)
br = attach_dma(prepare(rise, rise + 1, rise - 1, rise, v, dts,
                        np.linspace(100, 900, len(dts))), hts)
ok("a name in a clean uptrend never fires (nothing to reclaim)",
   all(buoy_signal(br, i) is None for i in range(300, n, 7)))

print(f"\n{P} passed · {F} failed")
sys.exit(1 if F else 0)
