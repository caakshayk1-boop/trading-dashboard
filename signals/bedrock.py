"""
BEDROCK — a floor that has held, and the distance back up from it.

WHAT IT HUNTS
A stock near its 52-week LOW that has stopped making new ones, sitting on a
level it has tested more than once and not broken, turning up off it. The trade
is the first close back above the consolidation that formed on that floor.

WHY "TESTED" IS THE WHOLE ENGINE
"Good support" is not a line someone drew. It is a price the market has come
down to, refused to go through, and left — more than once. So the level here is
found, not chosen: the lowest low of the last year, and then a COUNT of how many
separate occasions price came within TOUCH_TOL of it and turned. One touch is a
low. Two is a coincidence. Three or more is a floor with buyers under it, and
the engine will not fire on fewer than MIN_TOUCHES.

Separate occasions matters: twenty consecutive bars scraping along the same low
is ONE test, not twenty, so touches are only counted when they are at least
TOUCH_GAP bars apart.

WHY THE TARGETS ARE LARGE, AND WHY THAT IS NOT THE POINT
From a 52-week low the room back to any prior structure is enormous, and the
stop — just under a floor that is right there — is small. Large R:R falls out of
the geometry. That is exactly the arithmetic that produced this book's worst
habit: a 38R target on a stock with a 5% stop, published as though the 38 meant
something.

So this file does two things about it:
  · the ladder is the house one (1.6 / 2.5 / 3.3 R), not "whatever the 200-day
    average happens to be worth in R", and
  · the STRUCTURAL ceiling — the 200-day average, the real place a recovery
    would run to — is carried as `stretch_r` for information, with its MEASURED
    reach rate beside it. A target nobody reaches is decoration, and this repo
    has already retired one 4.0R rung for exactly that reason.

WHAT WOULD MAKE IT WRONG
Buying near 52-week lows is buying what is going down. The support that has
held three times fails on the fourth more often than the pattern suggests,
because the sample that reaches you is the one where it has not failed YET.
Survivorship bias bites harder here than anywhere else on this book: the
universe is the names on the screen TODAY, so the companies whose floor gave
way and which fell out of the index are missing from every backtest below.

STATUS: whatever backtest_bedrock.py says, and nothing more.
"""
from __future__ import annotations
import numpy as np

from signals.basebreak import (
    rsi, atr, sma, swing_lows, _liquid, ATR_FLOOR_MULT,
)

# ── tunables ─────────────────────────────────────────────────────────────────
LOOKBACK        = 252     # the year the low is measured over
BOTTOM_BAND     = 0.15    # within 15% of the 52-week low counts as "at the lows"
TOUCH_TOL       = 0.03    # a low within 3% of the floor is a test of it
TOUCH_GAP       = 5       # bars apart before two touches are separate occasions
MIN_TOUCHES     = 2       # see ANCHOR_BACKTEST: three was WORSE than two, and
                          # two has twice the sample. The "one is a low, two is
                          # chance, three is a floor" line was a good sentence
                          # and the data did not support it.
BASE_MIN        = 8       # the consolidation must be at least this long
BASE_WINDOW     = 40      # ...and the break is measured over AT MOST this much of it
VOL_MULT        = 1.3     # the break has to be on real volume
STOP_ATR_MULT   = 1.41    # the house multiple
MAX_STOP_PCT    = 0.18    # wider than this and it is not a swing trade
NO_NEW_LOW_BARS = 15      # it must have stopped making new lows
TARGET_R        = (1.6, 2.5, 3.3)


def prepare(o, h, l, c, v) -> dict:
    o, h, l, c, v = (np.asarray(x, dtype=float) for x in (o, h, l, c, v))
    return {"open": o, "high": h, "low": l, "close": c, "volume": v,
            "atr14": atr(h, l, c, 14), "rsi14": rsi(c, 14),
            "sma50": sma(c, 50), "sma200": sma(c, 200)}


def floor_of(low: np.ndarray, i: int):
    """The year's low, and how many SEPARATE occasions price tested it.

    Returns (level, touches, last_touch_index) or None.
    """
    a = max(0, i - LOOKBACK)
    win = low[a:i + 1]
    if len(win) < 60:
        return None
    lvl = float(win.min())
    if lvl <= 0:
        return None
    touches, last = 0, None
    prev = -10 ** 9
    for k in range(a, i + 1):
        if low[k] <= lvl * (1 + TOUCH_TOL):
            if k - prev >= TOUCH_GAP:      # a separate occasion, not the same one
                touches += 1
            prev = k
            last = k
    return lvl, touches, last


def bedrock_signal(bars: dict, i: int) -> dict | None:
    """The first close out of a base built on a floor that has held.

    Sees bars[:i+1] and nothing after i.
    """
    h, l, c, v = bars["high"], bars["low"], bars["close"], bars["volume"]
    a, r = bars["atr14"], bars["rsi14"]
    if i < LOOKBACK + 10:
        return None
    if not (np.isfinite(a[i]) and a[i] > 0 and np.isfinite(r[i])):
        return None

    ok, turn = _liquid(c, v, i, window=20)
    if not ok:
        return None

    f = floor_of(l, i)
    if not f:
        return None
    lvl, touches, last_touch = f
    if touches < MIN_TOUCHES:
        return None                         # not a floor, just a low

    # ── it has to be AT the lows, not merely to have been there once ─────────
    if (c[i] - lvl) / lvl > BOTTOM_BAND:
        return None

    # ── and it has to have STOPPED making new ones ───────────────────────────
    if last_touch is None or (i - last_touch) < 2:
        return None                         # still on the floor this bar
    recent_low = float(l[i - NO_NEW_LOW_BARS:i + 1].min())
    if recent_low <= lvl * (1 + 1e-9):
        return None                         # a new low inside the window

    # ── the base that formed on the floor, and the break out of its top ─────
    #
    # THE BASE IS THE RECENT RANGE, NOT EVERYTHING SINCE THE FLOOR WAS TOUCHED.
    # The first version took the highest high since the last test of the floor,
    # which on a median 27-bar base — and 31% of them longer than 60 — meant
    # demanding a close above months of trade. Measured: 1,547 candidates
    # reached that gate and 51 passed it, a 97% cut, and the survivors were 8
    # trades in three years. A breakout is a close above the RECENT range; the
    # window is capped so a long base helps the setup instead of disqualifying
    # it, which is the right way round for an engine that wants a floor to have
    # held for a while.
    span = i - max(0, last_touch)
    if span < BASE_MIN:
        return None
    b0 = max(0, i - min(span, BASE_WINDOW))
    base = h[b0:i]
    if len(base) < BASE_MIN:
        return None
    top = float(base.max())
    if not (c[i] > top and c[i - 1] <= top):
        return None                         # the FIRST close through it
    if v[i] < VOL_MULT * float(np.mean(v[max(0, i - 20):i])):
        return None

    # ── levels ───────────────────────────────────────────────────────────────
    entry = float(c[i])
    stop = min(lvl * (1 - 0.005), entry - ATR_FLOOR_MULT * STOP_ATR_MULT * float(a[i]))
    risk = entry - stop
    if risk <= 0 or risk / entry > MAX_STOP_PCT:
        return None

    # The structural ceiling, carried for information with its reach measured
    # in the backtest. NOT used as a target: a number nobody reaches is
    # decoration, and this book has retired a rung for that already.
    sma200 = bars["sma200"][i]
    stretch_r = (float(sma200) - entry) / risk if np.isfinite(sma200) and sma200 > entry else None

    return {
        "engine": "bedrock", "i": i,
        "entry": round(entry, 2), "sl": round(stop, 2),
        "target1": round(entry + TARGET_R[0] * risk, 2),
        "target2": round(entry + TARGET_R[1] * risk, 2),
        "target3": round(entry + TARGET_R[2] * risk, 2),
        "rr": TARGET_R[0],
        "floor": round(lvl, 2), "touches": int(touches),
        "base_bars": int(len(base)),
        "above_floor_pct": round((entry - lvl) / lvl * 100, 2),
        "risk_pct": round(risk / entry * 100, 2),
        "rsi": round(float(r[i]), 1),
        "turnover_cr": round(turn, 2),
        "stretch_r": None if stretch_r is None else round(stretch_r, 2),
        "sma200": None if not np.isfinite(sma200) else round(float(sma200), 2),
    }


# ── ANCHOR — the same floor, bought AT it instead of after it ────────────────
#
# BEDROCK entered on a close out of the base that formed on the floor. Measured,
# that entry sat a MEDIAN 13.5% ABOVE the floor, and the stop still went under
# the floor — so risk was 12.3% of entry against a 2.2% daily ATR. Five and a
# half ATR of stop, on an engine whose whole premise was a small one.
#
# The stop rule was never the problem. The ENTRY rule was: by the time price
# closes above a 40-bar high it has already made the move the stop is sized to
# survive. Buying the test of the floor instead puts entry within a few percent
# of the stop, which is what "at the lows with a small stop" actually means.
#
# The risk this takes on, stated: buying a floor while price is still ON it is
# buying something that is still falling. BEDROCK at least waited for proof of
# a turn. ANCHOR waits only for a REVERSAL BAR — a session that traded into the
# floor zone and closed in the top third of its own range, which is the
# earliest evidence that the sellers were met. That is a weaker signal bought
# at a better price, and which of those wins is exactly what the backtest is
# for. It is not assumed.

ENTRY_BAND      = 0.04    # entry must be within this of the floor
STOP_UNDER      = 0.02    # the stop sits this far under the floor
CLOSE_STRENGTH  = 0.60    # close must be this far up the bar's own range
RSI_MAX         = 45      # it should still be beaten down, not already recovered
ANCHOR_VOL_MULT = 1.0     # not a breakout, so volume only has to be normal


def anchor_signal(bars: dict, i: int) -> dict | None:
    """A reversal bar on a floor that has held three times, bought at the floor.

    Sees bars[:i+1] and nothing after i.
    """
    h, l, c, v = bars["high"], bars["low"], bars["close"], bars["volume"]
    a, r = bars["atr14"], bars["rsi14"]
    if i < LOOKBACK + 10:
        return None
    if not (np.isfinite(a[i]) and a[i] > 0 and np.isfinite(r[i])):
        return None

    ok, turn = _liquid(c, v, i, window=20)
    if not ok:
        return None

    f = floor_of(l, i)
    if not f:
        return None
    lvl, touches, last_touch = f
    if touches < MIN_TOUCHES:
        return None

    entry = float(c[i])
    # ── AT the floor, not above it ───────────────────────────────────────────
    if not (0 <= (entry - lvl) / lvl <= ENTRY_BAND):
        return None
    # ── this bar has to have gone INTO the floor zone and come back ──────────
    if l[i] > lvl * (1 + TOUCH_TOL):
        return None
    rng = float(h[i] - l[i])
    if rng <= 0:
        return None
    if (entry - float(l[i])) / rng < CLOSE_STRENGTH:
        return None                         # closed weak: no reversal bar
    if c[i] <= c[i - 1]:
        return None                         # not an up close
    if r[i] > RSI_MAX:
        return None                         # already recovered; not a bottom
    if v[i] < ANCHOR_VOL_MULT * float(np.mean(v[max(0, i - 20):i])):
        return None

    stop = lvl * (1 - STOP_UNDER)
    risk = entry - stop
    if risk <= 0:
        return None
    # A stop inside one ATR is inside the noise — the fault this repo found in
    # breakout at 0.29 ATR (90.9% stop-outs) and ohl at 0.78 ATR (91.7%). A
    # small stop is the POINT here, so rather than widening it, a setup whose
    # floor sits inside the day's own range is simply not taken.
    if risk < float(a[i]):
        return None
    if risk / entry > 0.10:
        return None

    return {
        "engine": "anchor", "i": i,
        "entry": round(entry, 2), "sl": round(stop, 2),
        "target1": round(entry + TARGET_R[0] * risk, 2),
        "target2": round(entry + TARGET_R[1] * risk, 2),
        "target3": round(entry + TARGET_R[2] * risk, 2),
        "rr": TARGET_R[0],
        "floor": round(lvl, 2), "touches": int(touches),
        "above_floor_pct": round((entry - lvl) / lvl * 100, 2),
        "risk_pct": round(risk / entry * 100, 2),
        "risk_atr": round(risk / float(a[i]), 2),
        "rsi": round(float(r[i]), 1),
        "close_strength": round((entry - float(l[i])) / rng, 2),
        "turnover_cr": round(turn, 2),
    }


DETECTORS = {"bedrock": bedrock_signal, "anchor": anchor_signal}

# ── ANCHOR: THE ENTRY FIX WORKED. THE EDGE STILL IS NOT THERE ────────────────
#
# 192 names, 3y daily, walk-forward, entry at the next bar's OPEN, stop before
# target, unresolved excluded.
#
# WHAT THE REBUILD ACTUALLY FIXED — this part is not ambiguous:
#
#                         BEDROCK        ANCHOR
#   median stop            12.3%          5.1%   (1.92x ATR)
#   entry above the floor  13.5%          3.2%
#   best move reached 2.5R  0.0%         25.0%
#
# BEDROCK entered on a close out of the base, by which point price had left the
# floor, and then put the stop under the floor — so risk was the whole distance
# travelled since the bounce. Buying the reversal bar ON the floor gives the
# small stop the idea always needed. That is a real correction to a real
# mistake, and it is why the reach profile changed so much.
#
# WHAT IT DID NOT FIX. How many times the floor must have held, all reported:
#
#   touches   n    exp        t      win      95% CI
#      2     77   +0.038R   +0.22   31.2%   [-0.303, +0.380]
#      3     36   -0.081R   -0.32   25.0%   [-0.579, +0.416]
#      4     15   -0.003R   -0.01   26.7%   [-0.803, +0.797]
#      5      8   -0.562R   -1.29   12.5%   [-1.420, +0.295]
#
# Every interval contains zero. The best cell is +0.038R at t=+0.22, and it is
# the best of FOUR tried — at that count it is what chance looks like.
#
# MIN_TOUCHES is set to 2 for a reason independent of that ranking: it has
# twice the sample of any other value. And the ordering refutes the assumption
# the engine was built on — MORE tests of a floor measured WORSE, not better.
# A level that keeps being hit is a level under constant pressure, not one with
# buyers stacked under it.
#
# AND THE MEGA TARGETS ARE MEASURABLY WRONG. Target sweep at 3 touches:
#
#   T=1.6R  n=38  -0.206R  t=-1.07  win 28.9%
#   T=2.5R  n=36  -0.081R  t=-0.32  win 25.0%
#   T=3.3R  n=35  -0.395R  t=-1.73  win 14.3%
#   T=5.0R  n=35  -0.482R  t=-2.14  win 11.4%   <- significantly negative
#
# Stretching for the big number is the one result here that clears significance,
# and it clears it in the wrong direction. 5R was reached by 0% of trades.
#
# Break-even at a 2.5R target needs a 28.6% win rate. The best configuration
# managed 31.2% and still only reached +0.038R, which is how thin the margin is.
#
# STATUS: RESEARCH, not REJECTED. Unlike BEDROCK — where all six cells were at
# or below zero and the first rung was reached one time in eight — this sits on
# zero with a wide interval and a target that is genuinely reached a quarter of
# the time. That is not an edge. It is also not the same as being refuted.
ANCHOR_BACKTEST = {
    "names": 192, "window": "3y daily", "basis": "entry next bar open, T2 2.5R, stop first",
    "n": 77, "exp": 0.038, "t": 0.22, "win": 31.2, "ci_lo": -0.303, "ci_hi": 0.380,
    "median_stop_pct": 5.1, "median_stop_atr": 1.92, "median_above_floor_pct": 3.2,
    "reach_2_5R_pct": 25.0, "breakeven_win_pct": 28.6,
    "touches_swept": [2, 3, 4, 5], "targets_swept": [1.6, 2.5, 3.3, 5.0],
    "note": "more floor tests measured worse, not better; a 5R target is significantly negative",
}

# ── THE MEASUREMENT: IT DOES NOT WORK, AND THE TARGETS ARE NOT THERE ─────────
#
# 192 names, three years of daily bars, walk-forward, entry at the next bar's
# OPEN, stop checked before target, unresolved trades excluded. Six
# configurations of the two gates that fight each other — how far above the
# 52-week low still counts as "at the lows", and how many bars the breakout
# level is measured over. ALL SIX are here:
#
#   band  window  n    exp       t      win     reach 1.6R
#   0.15    10   16   -0.110R  -0.71   37.5%      0.0%
#   0.15    15   12   -0.067R  -0.33   50.0%      8.3%
#   0.15    40    8   -0.212R  -0.82   37.5%     12.5%
#   0.25    10   40   +0.000R  +0.00   45.0%      7.5%
#   0.25    15   34   +0.003R  +0.02   44.1%     11.8%
#   0.25    40   26   -0.046R  -0.27   42.3%     11.5%
#
# EVERY CELL IS AT OR BELOW ZERO. The best is +0.003R at t=+0.02.
#
# AND THERE ARE NO MEGA TARGETS. That was the point of the idea: a small stop
# under a floor that is right there, and the whole recovery above it. The
# geometry does promise it. The market did not deliver it — the FIRST rung,
# 1.6R, was reached by between 0% and 12.5% of trades depending on the
# configuration. The 2.5R and 3.3R rungs were reached by NONE of the 8 trades
# in the strictest cell. A ladder whose first rung is reached one time in eight
# is decoration, and this repo has already retired a rung for exactly that.
#
# The 200-day average — the obvious structural ceiling to aim at — sat a median
# of 0.4R above entry, not the many-R away the idea assumes. That is not a bug:
# a stock that has fallen for a year drags its own 200-day average down to meet
# it, so by the time price is at the lows there is no distant line left to
# recover to. The "mega target" is an artefact of imagining a flat 200-day
# average above a fallen price.
#
# SURVIVORSHIP BIAS MAKES THIS OPTIMISTIC, NOT PESSIMISTIC. The universe is the
# names on the screen TODAY. Every company whose floor gave way and which fell
# out of the index is absent. The true result for "buy the tested floor" is
# worse than what is written above, not better.
#
# So this engine is NOT published, NOT scanned, and NOT on the site. It is kept
# because the next person to have this idea — including me — should find the
# measurement before rebuilding it.
ENGINE_STATUS = {"bedrock": "REJECTED", "anchor": "RESEARCH"}

BACKTEST = {
    "verdict": "rejected on measurement",
    "names": 192, "window": "3y daily",
    "best_n": 34, "best_exp": 0.003, "best_t": 0.02, "best_win": 44.1,
    "reach_1_6R_max_pct": 12.5,
    "sma200_median_r_away": 0.4,
    "cells_at_or_below_zero": "6 of 6",
}
