"""
BUOY — the 200-period average, reclaimed on the FOUR-HOUR candle, with
momentum already diverging.

WHAT IT HUNTS
A stock that has fallen a long way, stopped falling, and has just closed a
4-hour candle back above its 200-period moving average, with a bullish RSI
divergence behind it. On a 4H chart 200 periods is about a hundred trading
sessions — five months — so it is the long-term line on that timeframe, and
the first 4H close back above it is the moment "this is a downtrend" stops
being true.

4H CANDLES ON NSE, AND WHY TWO A DAY
The session is 09:15-15:30 IST and Yahoo returns SEVEN hourly bars for it:
09:15 through 15:15, the last a fifteen-minute stub. So a session makes two
candles — 09:15-13:15, a true four hours, and 13:15-15:30, two and a quarter.
That is the convention TradingView uses and `to_4h()` below reproduces it. Two
candles a day, not one and a half, and the split is stated because a resampler
that guesses it silently is a resampler nobody can check.

THE RESULT, WHICH IS A NULL — AND THE SAMPLE IS NOW BIG ENOUGH TO SAY SO
=======================================================================
188 liquid names, two years, walk-forward, entry at the next candle's OPEN,
stop checked before target, unresolved trades excluded. Every combination that
was run is here, not the best one:

  200-PERIOD 4H MA + RSI divergence   n= 36  +0.012R  t=+0.06  win 50.0%
  200-PERIOD 4H MA alone              n=348  +0.010R  t=+0.16  win 44.0%
  200-DAY MA       + RSI divergence   n= 26  -0.055R  t=-0.27  win 46.2%
  200-DAY MA       alone              n=245  -0.015R  t=-0.21  win 46.5%

And the exit is not what is killing it. On the largest sample:

  T1 1.6R / 30 sessions   n=350  -0.017R  t=-0.33  95% CI [-0.120, +0.086]
  T2 2.5R / 30 sessions   n=348  +0.010R  t=+0.16  95% CI [-0.108, +0.128]
  T3 3.3R / 30 sessions   n=346  -0.002R  t=-0.04  95% CI [-0.124, +0.119]
  T2 2.5R / 15 sessions   n=367  +0.011R  t=+0.23  95% CI [-0.082, +0.104]
  T2 2.5R / 60 sessions   n=331  +0.072R  t=+0.98  95% CI [-0.073, +0.218]

**THE CONFIDENCE INTERVAL IS THE FINDING.** At n=348 the interval on the house
exit is [-0.108, +0.128]. This book clears an engine at t>=2, which on this
sample would need roughly +0.13R — the top of that interval. So the honest
statement is no longer "too few trades to tell": it is that IF this rule has an
edge, it is smaller than the bar this book requires. That is a real answer, and
it is a negative one.

ONE VARIANT LOOKS BETTER AND SHOULD BE IGNORED
The divergence lane exited at T1 measured +0.208R at t=+1.09 (n=36), the best
row in the study. It is also the widest interval in it, [-0.165, +0.580], and
it is one of EIGHT variants tested — at that count a t near 1.1 is what chance
produces. Reporting it as promising would be exactly the post-hoc pick this
file exists to avoid. It is written down so nobody rediscovers it and believes
it.

THE DIVERGENCE FILTER STILL DOES NOT EARN ITS CUT
348 signals to 36 — 90% discarded — to move expectancy by 0.002R. Same finding
as the hourly build (217 -> 18 for 0.009R). It is kept because it is the rule
that was asked for and because n=36 cannot rule anything out on its own; it is
NOT kept because it was shown to work.

Superseded: the first build ran on 1-HOUR candles against the 200-DAY average
and measured +0.074R at t=+0.23 over 18 trades. The spec was 4-hour candles.
The hourly numbers are kept in git history, not here.

STATUS: RESEARCH. Nothing here is cleared for capital, and on this evidence
nothing should be.
"""
from __future__ import annotations
import numpy as np

from signals.basebreak import (
    rsi, atr, sma, swing_lows, _liquid,
    FROM_HIGH_MIN, MIN_TURNOVER_CR, ATR_FLOOR_MULT,
)

# ── tunables ─────────────────────────────────────────────────────────────────
MA_N              = 200     # PERIODS of 4H — about 100 sessions
BELOW_MIN_BARS    = 10      # ~5 sessions under the line before the reclaim
RECLAIM_MAX_ATR   = 1.2     # a close this far above the line is already gone
DIV_MIN_GAP_B     = 6       # 4H bars between the two lows of a divergence (~3 sessions)
DIV_MAX_GAP_B     = 120     # ~60 sessions; beyond that they are unrelated lows
RSI_DIV_MIN_LIFT  = 2.0     # RSI must be this much higher at the lower low
VOL_MULT          = 1.2     # against the 20-hour average
STOP_ATR_MULT     = 1.41    # the house multiple, from signals/indicators.py
TARGET_R          = (1.6, 2.5, 3.3)   # the house ladder


import datetime as _dt

IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))


def to_4h(rows):
    """Hourly OHLCV -> 4-hour candles, NSE convention.

    rows: [(ts, o, h, l, c, v), ...] in ascending time.
    The session splits at 13:15 IST, giving 09:15-13:15 and 13:15-15:30 — two
    candles a day. A resampler that guesses the split silently is one nobody
    can check, so it is written here and asserted in test_buoy.py.
    """
    out, cur, key = [], None, None
    for t_, o, h, l, c, v in rows:
        d = _dt.datetime.fromtimestamp(t_, IST)
        half = 0 if (d.hour < 13 or (d.hour == 13 and d.minute < 15)) else 1
        k = (d.date(), half)
        if k != key:
            if cur:
                out.append(tuple(cur))
            cur, key = [t_, o, h, l, c, v], k
        else:
            cur[2] = max(cur[2], h)
            cur[3] = min(cur[3], l)
            cur[4] = c
            cur[5] += v
    if cur:
        out.append(tuple(cur))
    return out


def prepare(o, h, l, c, v) -> dict:
    """Indicator arrays for a 4H series. The average is 200 PERIODS of 4H —
    about a hundred sessions — not the 200-DAY average: measured against each
    other, the period average was the better of two nulls (+0.010R at n=348
    against -0.015R at n=245) and it is the natural reading of "MA 200" on a
    4H chart."""
    o, h, l, c, v = (np.asarray(x, dtype=float) for x in (o, h, l, c, v))
    return {"open": o, "high": h, "low": l, "close": c, "volume": v,
            "atr14": atr(h, l, c, 14), "rsi14": rsi(c, 14),
            "ma": sma(c, MA_N)}


def buoy_signal(bars: dict, i: int) -> dict | None:
    """The first 4H close back above the 200-period average for a fallen name,
    with a bullish RSI divergence already in place behind it.

    Sees bars[:i+1] and nothing after i.
    """
    h, l, c, v = bars["high"], bars["low"], bars["close"], bars["volume"]
    r, a, ma = bars["rsi14"], bars["atr14"], bars["ma"]
    if i < MA_N + 30:
        return None
    if not (np.isfinite(ma[i]) and np.isfinite(a[i]) and np.isfinite(r[i]) and a[i] > 0):
        return None

    ok, turn = _liquid(c, v, i, window=20)
    if not ok:
        return None

    # ── it has to have fallen ────────────────────────────────────────────────
    look = c[max(0, i - 500):i + 1]           # ~250 sessions of 4H candles
    hi52 = float(look.max())
    if not hi52 or (hi52 - c[i]) / hi52 < FROM_HIGH_MIN:
        return None

    # ── the reclaim, and it must be the FIRST one ────────────────────────────
    if not (c[i] > ma[i] and c[i - 1] <= ma[i - 1]):
        return None
    prev, pm = c[i - BELOW_MIN_BARS:i], ma[i - BELOW_MIN_BARS:i]
    if len(prev) < BELOW_MIN_BARS or not np.all(np.isfinite(pm)):
        return None
    if not np.all(prev <= pm):
        return None                            # it was not decisively below

    # A close already far above the line is a gap, not a reclaim.
    if (c[i] - ma[i]) > RECLAIM_MAX_ATR * a[i]:
        return None

    if v[i] < VOL_MULT * float(np.mean(v[max(0, i - 20):i])):
        return None

    # ── the divergence, behind it ────────────────────────────────────────────
    piv = [p for p in swing_lows(l[:i + 1]) if p <= i - 3]
    if len(piv) < 2:
        return None
    lo2 = piv[-1]
    cand = [p for p in piv[:-1] if DIV_MIN_GAP_B <= (lo2 - p) <= DIV_MAX_GAP_B]
    if not cand:
        return None
    lo1 = cand[-1]
    if not (l[lo2] < l[lo1]):
        return None                            # no lower low, no divergence
    if not (np.isfinite(r[lo1]) and np.isfinite(r[lo2])
            and r[lo2] > r[lo1] + RSI_DIV_MIN_LIFT):
        return None                            # momentum did not diverge

    # ── levels ───────────────────────────────────────────────────────────────
    # The stop is STRUCTURAL — under the low that made the divergence — floored
    # at 1.41 ATR so it can never sit inside the noise. That floor is the same
    # fault this repo already found twice: breakout at 0.29 ATR stopped out
    # 90.9% of the time, and ohl at 0.78 ATR 91.7%.
    entry = float(c[i])
    struct = float(l[lo2])
    stop = min(struct, entry - ATR_FLOOR_MULT * STOP_ATR_MULT * float(a[i]))
    risk = entry - stop
    if risk <= 0 or risk / entry > 0.25:
        return None                            # a 25%+ stop is not a swing trade
    return {
        "engine": "buoy", "i": i, "entry": entry, "sl": round(stop, 2),
        "target1": round(entry + TARGET_R[0] * risk, 2),
        "target2": round(entry + TARGET_R[1] * risk, 2),
        "target3": round(entry + TARGET_R[2] * risk, 2),
        "rr": round(TARGET_R[0], 2), "ma": round(float(ma[i]), 2),
        "from_high_pct": round((hi52 - c[i]) / hi52 * 100, 2),
        "rsi": round(float(r[i]), 1),
        "rsi_lift": round(float(r[lo2] - r[lo1]), 1),
        "turnover_cr": round(turn, 2), "struct_low": round(struct, 2),
        "risk_pct": round(risk / entry * 100, 2),
    }


DETECTORS = {"buoy": buoy_signal}

# ── WHAT IT MEASURED, INCLUDING THE PART THAT ARGUES AGAINST IT ──────────────
#
# backtest_buoy.py, 188 liquid names, two years of hourly bars, walk-forward,
# entry at the next bar's OPEN, stop checked before target, unresolved trades
# excluded rather than booked flat. Every variant that was run is here, not the
# best one:
#
#   as specified (divergence, 240h window, 3-bar pivots)  n= 18  +0.074R  t=+0.23
#   wider divergence window (480h)                        n= 18  +0.074R  t=+0.23
#   wider pivots (6-bar)                                  n= 29  -0.134R  t=-0.60
#   wider window AND wider pivots                         n= 29  -0.134R  t=-0.60
#   NO divergence filter — the reclaim alone              n=217  +0.065R  t=+0.77
#
# THREE THINGS THIS SAYS, AND ONE IT DOES NOT.
#
# 1. Nothing clears the bar. This book requires 30 closed trades at t>=2 before
#    an engine sees capital. The best t here is +0.77 on the widest sample and
#    +0.23 on the rule as written. Neither is distinguishable from zero.
#
# 2. THE RSI DIVERGENCE FILTER DOES NOT EARN ITS CUT. It removes 199 of 217
#    trades — 92% of the sample — and moves expectancy from +0.065R to +0.074R.
#    Nine thousandths of an R for nine tenths of the data is not a filter, it is
#    a coincidence with a rationale attached. It is kept because it is the rule
#    that was asked for and because n=18 cannot rule anything out either; it is
#    NOT kept because it was shown to work.
#
# 3. The n=18 result is not robust. Changing the pivot width from 3 bars to 6 —
#    a cosmetic choice about what counts as a swing low on an hourly chart —
#    flips it from +0.074R to -0.134R and nearly doubles the sample. A rule
#    whose sign depends on that is measuring noise.
#
# What it does NOT say is that the idea is wrong. n=18 over two years is too
# few signals to conclude anything, in either direction. The honest position is
# that this engine is UNPROVEN and is being run forward to collect a sample —
# which is what RESEARCH means here and why it publishes a watchlist rather
# than signals.
ENGINE_STATUS = {"buoy": "RESEARCH"}

BACKTEST = {
    "timeframe": "4H", "names": 188, "window": "2y",
    "basis": "entry next candle open, T2 2.5R exit, stop first, 30-session cap",
    # the rule as specified
    # backtest_buoy.py, the production harness (one position per symbol at a
    # time, which the research sweep did not enforce — hence 35 here and 36
    # there). The difference is one trade and no conclusion.
    "n": 35, "exp": 0.002, "t": 0.01, "win": 48.6, "pf": 1.00,
    "ci_lo": -0.364, "ci_hi": 0.389,
    "maxdd": -10.28, "median_stop_pct": 8.7, "median_hold_sessions": 26,
    # the same rule without the divergence filter — the large sample
    "n_no_div": 348, "exp_no_div": 0.010, "t_no_div": 0.16,
    "ci_lo_no_div": -0.108, "ci_hi_no_div": 0.128,
    # the alternative reading of "MA 200", measured and rejected
    "n_dma": 245, "exp_dma": -0.015, "t_dma": -0.21,
}
