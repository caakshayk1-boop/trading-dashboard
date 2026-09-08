"""
BUOY — the 200-day average, reclaimed on the hourly chart, with momentum
already diverging.

WHAT IT HUNTS
A stock that has fallen a long way, stopped falling, and has just traded back
above its 200-DAY moving average — caught on the HOURLY chart rather than
waiting for a daily close to confirm it. The 200-day average is the line most
of the market uses to say "this is a downtrend"; the first hour a beaten-down
name closes back above it is the moment that description stops being true.

WHY TWO CLOCKS, AND WHY THAT IS THE WHOLE POINT
The average is a DAILY series. The trigger is an HOURLY close. That is not an
accident of implementation, it is the engine:

  · A 200-period average of HOURLY bars covers about thirty trading days. It is
    a one-month average wearing a long-term average's name, and reclaiming it
    says almost nothing.
  · The 200-DAY average watched on hourly bars is the real long-term line,
    crossed with a six-times-finer trigger. A daily-close rule sees the reclaim
    once, at 15:30, already extended; the hourly rule sees the first hour it
    happens.

So the daily 200-day average is computed on daily closes and then carried
forward onto the hourly timeline, each hour holding the last COMPLETED day's
value. Recomputing it from the current, unfinished day would be lookahead of
the most ordinary kind: the average would move during the session on
information the trade could not have had.

DIVERGENCE IS THE FILTER, NOT THE TRIGGER
A 200-day reclaim on its own fires constantly in a rising market and most of
those are not recoveries, they are stocks that never really fell. Two gates fix
that, and both come from KEEL, which measured them on this same book:

  · The name must be at least FROM_HIGH_MIN off its 52-week high — no fall, no
    recovery to trade.
  · Price must have made a LOWER low while RSI made a HIGHER low: the selling
    was already losing force before the line was reclaimed. A divergence alone
    is not a trade and can persist for months, which is why it is the filter
    and the reclaim is the trigger.

HOW IT DIFFERS FROM KEEL, WHICH IS ALREADY IN THIS REPO
KEEL is daily and its reclaim level is the earlier SWING LOW. BUOY is hourly
and its reclaim level is the 200-DAY AVERAGE. They can both fire on one name
and they are not the same statement: KEEL says the last leg down has been
undone, BUOY says the market's own definition of the downtrend has been.

STATUS: RESEARCH. Nothing here is cleared for capital and the backtest that
would decide that is backtest_buoy.py. No number from this file should appear
on the site without the sample size beside it.
"""
from __future__ import annotations
import numpy as np

from signals.basebreak import (
    rsi, atr, sma, swing_lows, _liquid,
    FROM_HIGH_MIN, MIN_TURNOVER_CR, ATR_FLOOR_MULT,
)

# ── tunables ─────────────────────────────────────────────────────────────────
DMA_N             = 200     # the DAILY average being reclaimed
BELOW_MIN_HOURS   = 30      # hours it must have spent under the line first
RECLAIM_MAX_ATR   = 1.2     # a close this far above the line is already gone
DIV_MIN_GAP_H     = 12      # hourly bars between the two lows of a divergence
DIV_MAX_GAP_H     = 240     # ~35 sessions; beyond that they are unrelated lows
RSI_DIV_MIN_LIFT  = 2.0     # RSI must be this much higher at the lower low
VOL_MULT          = 1.2     # against the 20-hour average
STOP_ATR_MULT     = 1.41    # the house multiple, from signals/indicators.py
TARGET_R          = (1.6, 2.5, 3.3)   # the house ladder


def dma_on_hours(hour_ts: np.ndarray, day_ts: np.ndarray, day_close: np.ndarray,
                 n: int = DMA_N) -> np.ndarray:
    """The 200-DAY average, carried onto an hourly timeline.

    Each hour takes the average as of the last day that had already CLOSED
    before that hour began. Using the current day's own average would let a
    trade at 10:00 know where the day finished.
    """
    d = sma(np.asarray(day_close, dtype=float), n)
    day_ts = np.asarray(day_ts)
    out = np.full(len(hour_ts), np.nan)
    # A day's value becomes usable only AFTER that day ends. Yahoo stamps a
    # daily bar at the session open, so the day is complete one day later.
    usable_from = day_ts + 86400
    j = -1
    for i, t in enumerate(hour_ts):
        while j + 1 < len(day_ts) and usable_from[j + 1] <= t:
            j += 1
        if j >= 0 and np.isfinite(d[j]):
            out[i] = d[j]
    return out


def prepare(o, h, l, c, v, day_ts, day_close) -> dict:
    """Indicator arrays for the hourly series, plus the daily average."""
    o, h, l, c, v = (np.asarray(x, dtype=float) for x in (o, h, l, c, v))
    return {"open": o, "high": h, "low": l, "close": c, "volume": v,
            "atr14": atr(h, l, c, 14), "rsi14": rsi(c, 14),
            "dma": None, "_day_ts": np.asarray(day_ts), "_day_c": np.asarray(day_close, dtype=float)}


def attach_dma(bars: dict, hour_ts: np.ndarray) -> dict:
    bars["dma"] = dma_on_hours(hour_ts, bars["_day_ts"], bars["_day_c"])
    return bars


def buoy_signal(bars: dict, i: int) -> dict | None:
    """The first hour a fallen name closes back above its 200-DAY average,
    with a bullish RSI divergence already in place behind it.

    Sees bars[:i+1] and nothing after i.
    """
    h, l, c, v = bars["high"], bars["low"], bars["close"], bars["volume"]
    r, a, dma = bars["rsi14"], bars["atr14"], bars["dma"]
    if i < 260 or dma is None:
        return None
    if not (np.isfinite(dma[i]) and np.isfinite(a[i]) and np.isfinite(r[i]) and a[i] > 0):
        return None

    ok, turn = _liquid(c, v, i, window=20)
    if not ok:
        return None

    # ── it has to have fallen ────────────────────────────────────────────────
    look = c[max(0, i - 1600):i + 1]          # ~52 weeks of hours
    hi52 = float(look.max())
    if not hi52 or (hi52 - c[i]) / hi52 < FROM_HIGH_MIN:
        return None

    # ── the reclaim, and it must be the FIRST one ────────────────────────────
    if not (c[i] > dma[i] and c[i - 1] <= dma[i - 1]):
        return None
    prev = c[i - BELOW_MIN_HOURS:i]
    pdma = dma[i - BELOW_MIN_HOURS:i]
    if len(prev) < BELOW_MIN_HOURS or not np.all(np.isfinite(pdma)):
        return None
    if not np.all(prev <= pdma):
        return None                            # it was not decisively below

    # A close already far above the line is a gap, not a reclaim.
    if (c[i] - dma[i]) > RECLAIM_MAX_ATR * a[i]:
        return None

    if v[i] < VOL_MULT * float(np.mean(v[max(0, i - 20):i])):
        return None

    # ── the divergence, behind it ────────────────────────────────────────────
    piv = [p for p in swing_lows(l[:i + 1]) if p <= i - 3]
    if len(piv) < 2:
        return None
    lo2 = piv[-1]
    cand = [p for p in piv[:-1] if DIV_MIN_GAP_H <= (lo2 - p) <= DIV_MAX_GAP_H]
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
        "engine": "buoy",
        "i": i,
        "entry": entry,
        "sl": round(stop, 2),
        "target1": round(entry + TARGET_R[0] * risk, 2),
        "target2": round(entry + TARGET_R[1] * risk, 2),
        "target3": round(entry + TARGET_R[2] * risk, 2),
        "rr": round(TARGET_R[0], 2),
        "dma": round(float(dma[i]), 2),
        "from_high_pct": round((hi52 - c[i]) / hi52 * 100, 2),
        "rsi": round(float(r[i]), 1),
        "rsi_lift": round(float(r[lo2] - r[lo1]), 1),
        "turnover_cr": round(turn, 2),
        "struct_low": round(struct, 2),
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
    "n": 18, "exp": 0.074, "t": 0.23, "win": 44.4,
    "n_no_div": 217, "exp_no_div": 0.065, "t_no_div": 0.77,
    "names": 188, "window": "2y hourly", "basis": "entry next bar open, T2 exit, stop first",
}
