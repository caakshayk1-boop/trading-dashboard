#!/usr/bin/env python3
"""Tests for BUOY (4-hour candles). Written against the rule, not against a
result — none of these thresholds was chosen to make a backtest look better."""
import sys, datetime as dt, numpy as np
sys.path.insert(0, '/Users/akshaykumarkothari/Downloads/trading-dashboard')
from signals.buoy import to_4h, prepare, buoy_signal, MA_N, IST
from signals.basebreak import sma

P = F = 0
def ok(name, cond, detail=""):
    global P, F
    if cond: P += 1; print(f"  PASS  {name}")
    else:    F += 1; print(f"  FAIL  {name}  {detail}")

# ── the 4H resampler ─────────────────────────────────────────────────────────
# One NSE session: 09:15..15:15 IST, seven hourly bars.
day = dt.datetime(2026, 9, 7, 9, 15, tzinfo=IST)
sess = []
for k in range(7):
    ts = int((day + dt.timedelta(hours=k)).timestamp())
    sess.append((ts, 100 + k, 101 + k, 99 + k, 100.5 + k, 1000 * (k + 1)))
c4 = to_4h(sess)
ok("one session makes exactly two 4H candles", len(c4) == 2, f"got {len(c4)}")
ok("the first candle is 09:15-13:15 (four hourly bars)",
   c4[0][1] == 100 and c4[0][4] == 103.5, c4[0])
ok("the second is 13:15-15:30 (the remaining three)",
   c4[1][1] == 104 and c4[1][4] == 106.5, c4[1])
ok("the high is the max and the low the min across the block",
   c4[0][2] == 104 and c4[0][3] == 99, c4[0])
ok("volume is summed, not averaged",
   c4[0][5] == 1000 + 2000 + 3000 + 4000, c4[0][5])

two = sess + [(int((day + dt.timedelta(days=1, hours=k)).timestamp()),
               200 + k, 201 + k, 199 + k, 200.5 + k, 500) for k in range(7)]
ok("a second session starts a new candle, never merges with the first",
   len(to_4h(two)) == 4, len(to_4h(two)))

# ── the detector cannot see the future ───────────────────────────────────────
rng = np.random.default_rng(7)
n = 2000
c = np.maximum(np.cumsum(rng.normal(0, 1.4, n)) + 600, 40)
h = c + np.abs(rng.normal(0, 1.2, n)); l = c - np.abs(rng.normal(0, 1.2, n))
o = c + rng.normal(0, .5, n); v = rng.integers(1_000_000, 9_000_000, n).astype(float)
b = prepare(o, h, l, c, v)
i = 900
base = buoy_signal(b, i)
b2 = prepare(o.copy(), h.copy(), l.copy(), c.copy(), v.copy())
for k in ("open", "high", "low", "close", "volume"):
    b2[k][i + 1:] = b2[k][i + 1:] * 3 + 41
b2["atr14"][i + 1:] = np.nan; b2["rsi14"][i + 1:] = np.nan; b2["ma"][i + 1:] = np.nan
ok("mangling every candle after i does not change the signal at i",
   (base is None) == (buoy_signal(b2, i) is None))

# ── the average is what it claims to be ──────────────────────────────────────
ok("the average is 200 PERIODS of the 4H series, not a daily one",
   np.allclose(b["ma"][MA_N:], sma(c, MA_N)[MA_N:], equal_nan=True))
ok("it is NaN until 200 candles exist", not np.isfinite(b["ma"][:MA_N - 1]).any())

# ── levels ───────────────────────────────────────────────────────────────────
bad = fired = 0
for k in range(MA_N + 40, n):
    s = buoy_signal(b, k)
    if not s:
        continue
    fired += 1
    if not (s["sl"] < s["entry"] < s["target1"] < s["target2"] < s["target3"]):
        bad += 1
    risk = s["entry"] - s["sl"]
    if abs((s["target1"] - s["entry"]) / risk - 1.6) > 0.02:
        bad += 1
    if risk / s["entry"] > 0.25 + 1e-9:
        bad += 1
ok("levels ordered, T1 exactly 1.6R, stop never over 25%", bad == 0, f"{bad} bad of {fired}")

# ── the gates gate ───────────────────────────────────────────────────────────
flat = np.full(n, 500.0)
bf = prepare(flat, flat + 1, flat - 1, flat, v)
ok("a flat series that never fell produces nothing",
   all(buoy_signal(bf, k) is None for k in range(MA_N + 40, n, 11)))
rise = np.linspace(80, 900, n)
br = prepare(rise, rise + 1, rise - 1, rise, v)
ok("a clean uptrend never fires — there is nothing to reclaim",
   all(buoy_signal(br, k) is None for k in range(MA_N + 40, n, 11)))

print(f"\n{P} passed · {F} failed")
sys.exit(1 if F else 0)
