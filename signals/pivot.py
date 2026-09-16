"""
PIVOT — location, then context, then confirmation.

Built 2026-09-17 from two pages Akshay sent:

    "Location tells you WHERE. Context tells you WHY. Orderflow tells you WHEN.
     Master these three and stop taking trades just because a candle looked
     good."

The framework is right and it is the order that matters: most engines in this
repo start at the trigger and work backwards. This one refuses to look at a
trigger until the location and the context have both passed, which is the
entire point of the three-step sequence.

────────────────────────────────────────────────────────────────────────────
WHAT THIS ENGINE CANNOT DO, STATED FIRST
────────────────────────────────────────────────────────────────────────────
The second page asks for Delta, CVD, Volume Profile, Footprint and a Heatmap
of resting orders — absorption, liquidity pulls, liquidity sweeps.

EVERY ONE OF THOSE NEEDS LEVEL 2 DEPTH AND TICK DATA. This repo has daily
OHLCV from Yahoo and nothing else. There is no bid, no ask, no book, no
per-trade aggressor flag, and therefore no delta and no cumulative delta.

So the confirmation stage here is built from DAILY-BAR PROXIES and is labelled
that way everywhere it surfaces — in the code, in the signal's own reason
string, and on the website. The proxies are honest about what they are:

    close position in the day's range   a crude read on who won the session
    volume vs its own 20-day average    participation, not aggression
    the day's range vs ATR              expansion, not absorption

A daily bar can tell you the sellers lost control of a session. It cannot tell
you a 400-lot bid absorbed 2,000 lots at one price. Calling the first thing
the second would be inventing data, and this file will not do it.

If real orderflow is wanted, it needs a paid depth feed and a different
architecture. That is a decision, not an oversight.

────────────────────────────────────────────────────────────────────────────
THE THREE GATES
────────────────────────────────────────────────────────────────────────────
LOCATION    Price must be AT a level the market has already reacted to —
            the 200-day average, the top of a multi-week range, or a prior
            swing high it is retesting. Not "in an uptrend": AT something.
            This is the gate that makes the stop small and obvious, because
            the level itself is the invalidation.

CONTEXT     The higher timeframe must agree. Weekly trend up (price over its
            own 50-week equivalent, 250 bars), and the name must not already
            be extended — this engine is buying a reaction at a level, not
            chasing a move that has happened.

CONFIRM     The session must show the reaction actually occurred: the close in
            the top third of the day's range, participation above the 20-day
            norm, and a range that expanded rather than drifted.

ALL THREE, IN ORDER. A name that fails LOCATION is never scored on the others,
so a strong trend at no particular level produces nothing. That is deliberate:
"stop taking trades just because a candle looked good" is the instruction.

NO LOOKAHEAD. Every function reads bars[:i+1] and nothing after i.
"""
from __future__ import annotations

import numpy as np

# ── tunables, in one place ───────────────────────────────────────────────────
MIN_BARS          = 260     # a year of context, plus room for the 250-bar mean
NEAR_LEVEL_ATR    = 0.60    # "at" a level means within 0.6 ATR of it
SWING_LEFT        = 5
SWING_RIGHT       = 5
RANGE_MIN_BARS    = 15      # a range shorter than this is noise, not a shelf
EXTENDED_ATR      = 4.0     # more than this above the 50-day is already gone
MIN_TURNOVER_CR   = 3.0     # a level means nothing in a name nobody trades

# Confirmation thresholds. Deliberately mild: this stage exists to reject a
# reaction that did not happen, not to demand a spectacular one.
CLOSE_IN_TOP      = 0.66    # close in the top third of the day's range
VOL_MIN           = 1.20    # participation vs the 20-day average
RANGE_EXPAND      = 0.90    # the day's range vs ATR


def sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def atr(high, low, close, n: int = 14) -> np.ndarray:
    prev = np.roll(close, 1)
    prev[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    return sma(tr, n)


def swing_highs(high: np.ndarray, left: int = SWING_LEFT, right: int = SWING_RIGHT) -> list[int]:
    """Indices of confirmed swing highs. A pivot needs `right` bars AFTER it to
    be confirmed, so the newest one is never within `right` bars of the end —
    which is exactly what stops this from reading the future."""
    out = []
    for j in range(left, len(high) - right):
        w = high[j - left:j + right + 1]
        if np.isfinite(w).all() and high[j] == w.max() and (w == high[j]).sum() == 1:
            out.append(j)
    return out


def _location(bars: dict, i: int) -> tuple[str, float] | None:
    """WHERE. Returns (what the level is, the level) or None.

    Three kinds, in the order they are worth having. The first that matches
    wins — a name sitting on two of them is not twice as good, and pretending
    otherwise would double-count one fact.
    """
    h, l, c = bars["high"], bars["low"], bars["close"]
    a = bars["atr14"][i]
    if not np.isfinite(a) or a <= 0:
        return None
    near = NEAR_LEVEL_ATR * a

    # 1. THE 200-DAY, APPROACHED FROM ABOVE. The most-watched level there is,
    #    and the one where a reaction is most likely to be other people's
    #    orders rather than coincidence. From ABOVE only: a name under its
    #    200-day meeting it from below is meeting resistance, not support.
    s200 = bars["sma200"][i]
    if np.isfinite(s200) and c[i] >= s200 and abs(c[i] - s200) <= near:
        return ("the 200-day average", float(s200))

    # 2. THE TOP OF A MULTI-WEEK SHELF. Price has spent RANGE_MIN_BARS inside a
    #    band and is now at its ceiling. The ceiling is where the sellers were.
    look = 60
    lo = max(0, i - look)
    win_h, win_l = h[lo:i + 1], l[lo:i + 1]
    if len(win_h) >= RANGE_MIN_BARS and np.isfinite(win_h).all():
        top, bottom = float(win_h.max()), float(win_l.min())
        if top > bottom:
            depth = (top - bottom) / a
            # A band more than 8 ATR deep is a trend, not a shelf.
            if depth <= 8.0 and abs(c[i] - top) <= near:
                return ("the top of its %d-day range" % len(win_h), top)

    # 3. A PRIOR SWING HIGH BEING RETESTED. Confirmed pivots only, and the most
    #    recent one that price is actually near.
    for j in reversed(swing_highs(h[:i + 1])):
        if i - j < SWING_RIGHT + 1:
            continue
        if abs(c[i] - h[j]) <= near:
            return ("a swing high from %d sessions ago" % (i - j), float(h[j]))
        if h[j] > c[i] + 3 * a:
            break                      # levels far overhead are not this trade
    return None


def _context(bars: dict, i: int) -> tuple[bool, str]:
    """WHY. The higher-timeframe story, and whether it agrees."""
    c = bars["close"]
    a = bars["atr14"][i]
    s50, s200, s250 = bars["sma50"][i], bars["sma200"][i], bars["sma250"][i]

    if not (np.isfinite(s50) and np.isfinite(s200) and np.isfinite(s250)):
        return (False, "not enough history")

    # Weekly trend, read on the daily series: 250 bars is the year, and price
    # over it is the simplest honest statement that buyers have had control.
    if c[i] <= s250:
        return (False, "below its one-year mean — the higher timeframe disagrees")

    # The 50 over the 200 is the structure underneath that.
    if s50 <= s200:
        return (False, "50-day still under the 200-day")

    # ALREADY EXTENDED IS A DISQUALIFIER, NOT A DISCOUNT. The whole argument
    # for buying at a level is that the invalidation is close. A name four ATR
    # above its 50-day has no such level anywhere near it.
    if np.isfinite(a) and a > 0 and (c[i] - s50) / a > EXTENDED_ATR:
        return (False, "already %.1f ATR above its 50-day" % ((c[i] - s50) / a))

    return (True, "above its one-year mean with the 50-day over the 200-day")


def _confirmation(bars: dict, i: int) -> tuple[bool, str]:
    """WHEN — as far as a daily bar can answer it.

    NOT ORDERFLOW. See the module docstring. These are the three things a
    single daily bar can honestly say about who won the session.
    """
    o, h, l, c, v = bars["open"], bars["high"], bars["low"], bars["close"], bars["volume"]
    rng = h[i] - l[i]
    if rng <= 0:
        return (False, "no range")

    pos = (c[i] - l[i]) / rng
    if pos < CLOSE_IN_TOP:
        return (False, "closed in the %s of its range" %
                ("bottom third" if pos < 0.34 else "middle"))

    vavg = float(np.nanmean(v[max(0, i - 19):i + 1]))
    vr = v[i] / vavg if vavg > 0 else 0.0
    if vr < VOL_MIN:
        return (False, "volume only %.2fx its 20-day average" % vr)

    a = bars["atr14"][i]
    if np.isfinite(a) and a > 0 and rng / a < RANGE_EXPAND:
        return (False, "the day's range never expanded")

    return (True, "closed in the top %d%% of its range on %.2fx volume, "
                  "range %.1fx ATR — a daily-bar read, not orderflow"
                  % (round(pos * 100), vr, rng / a if np.isfinite(a) and a > 0 else 0))


def pivot_signal(bars: dict, i: int) -> dict | None:
    """One signal, or nothing. Reads bars[:i+1] only.

    The gates run in order and the function returns at the first failure, so a
    name that is nowhere near a level is never scored on its trend.
    """
    c, v = bars["close"], bars["volume"]
    if i < MIN_BARS or not np.isfinite(bars["atr14"][i]):
        return None

    # Liquidity first — it is the cheapest check and a level in an untraded
    # name is a level nobody defended.
    turn_cr = float(np.nanmean(c[max(0, i - 19):i + 1] * v[max(0, i - 19):i + 1])) / 1e7
    if not np.isfinite(turn_cr) or turn_cr < MIN_TURNOVER_CR:
        return None

    # ── 1. LOCATION ─────────────────────────────────────────────────────────
    loc = _location(bars, i)
    if not loc:
        return None
    what, level = loc

    # ── 2. CONTEXT ──────────────────────────────────────────────────────────
    ctx_ok, ctx_why = _context(bars, i)
    if not ctx_ok:
        return None

    # ── 3. CONFIRMATION ─────────────────────────────────────────────────────
    cfm_ok, cfm_why = _confirmation(bars, i)
    if not cfm_ok:
        return None

    a = float(bars["atr14"][i])
    entry = float(c[i])

    # THE STOP IS THE LEVEL, NOT A MULTIPLE. That is the whole reason for
    # insisting on a location: the level that made this a trade is the level
    # that says it was wrong. Floored at 1 ATR so a price sitting exactly on
    # its 200-day does not produce a two-rupee stop and a fictional R:R.
    stop = min(level, entry - a)
    risk = entry - stop
    if risk <= 0:
        return None

    # ── TARGETS COME FROM THE HOUSE LADDER, NEVER FROM THIS FILE ────────────
    # Four engines have been caught publishing a first target worth less than
    # the risk, each having built its own ladder for its own good reason.
    # enforce_r_floor is the one place that decides, and it also carries the
    # per-engine floor and the MAX_T1_R ceiling, so a PIVOT signal cannot
    # publish a target this book has never reached.
    try:
        from signals.indicators import R1_MULT, R2_MULT, R3_MULT, enforce_r_floor
        t1, t2, t3 = (entry + R1_MULT * risk,
                      entry + R2_MULT * risk,
                      entry + R3_MULT * risk)
        t1, t2, t3 = enforce_r_floor(entry, stop, t1, t2, t3, "BUY", "pivot")
    except Exception:
        # A ladder this engine invented itself would be exactly the drift the
        # shared function exists to stop, so no fallback ladder: no targets.
        return None

    return {
        "engine": "pivot",
        "entry": entry,
        "stop": stop,
        "risk": risk,
        "t1": float(t1), "t2": float(t2), "t3": float(t3),
        "level": level,
        "location": what,
        "context": ctx_why,
        "confirmation": cfm_why,
        "turnover_cr": turn_cr,
        # One sentence in the order the framework runs, so the reason a signal
        # exists reads the same way the engine thinks.
        "reason": "At %s (%.2f); %s; %s" % (what, level, ctx_why, cfm_why),
    }


def prepare(o, h, l, c, v) -> dict:
    """Attach the arrays the detector expects. Same contract as basebreak."""
    o, h, l, c, v = (np.asarray(x, dtype=float) for x in (o, h, l, c, v))
    return {"open": o, "high": h, "low": l, "close": c, "volume": v,
            "atr14": atr(h, l, c, 14),
            "sma50": sma(c, 50), "sma200": sma(c, 200), "sma250": sma(c, 250)}


DETECTORS = {"pivot": pivot_signal}
