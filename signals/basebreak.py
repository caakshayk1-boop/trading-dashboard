"""
BASE BREAKOUTS FROM LOWS — the LEDGE and KEEL detectors.

WHY THESE TWO ENGINES EXIST
The engines this repo already runs hunt strength that is ALREADY VISIBLE:
BREACH takes 52-week and 20-week highs, ASCENT takes names near their highs,
VECTOR ranks twelve-month momentum. Every one of them buys a stock that has
already moved. Nothing here looks for the FIRST move off a base — the point
where risk is smallest because the invalidation level is close and obvious.

LEDGE and KEEL both hunt that point, from opposite evidence:
  · LEDGE  a Darvas box — price has gone quiet in a tight range after falling,
           and then closes out of the top of it on volume.
  · KEEL   a bullish RSI divergence — price made a lower low while momentum
           made a higher one, and price has now reclaimed the level it lost.

Both refuse anything that is already extended. That is the whole point: a
breakout from a base near the lows has a stop a few percent away, and a
breakout from an all-time high has a stop wherever you care to draw it.

EVERYTHING IS CLOSE-BASIS, ENTRY AND STOP.
Measured over this ledger's own 247 closed signals, replaying the identical
bars: an intraday stop returns +0.046R (t=0.65) and a CLOSE-basis stop returns
+0.117R (t=1.63) — a paired improvement of +0.071R at t=2.53. 53 of 91
measurable stop-outs were wicks that no daily close ever confirmed, and 16 of
those went on to make +1R. GRWRHITECH is the example that prompted this: a
one-MONTH horizon signal stopped out by a single 1-hour spike to 6560.50 on a
day that closed at 6951.50, above its own stop.

NO LOOKAHEAD. Every detector reads bars[:i+1] and nothing after i. The backtest
enters at the OPEN of bar i+1, because a scan that runs on the close cannot buy
that close.
"""
from __future__ import annotations

import numpy as np

# ── tunables, in one place ───────────────────────────────────────────────────
BOX_MIN_BARS      = 10      # a range shorter than this is not a base
BOX_MAX_BARS      = 60
BOX_MAX_HEIGHT    = 0.14    # box height as a fraction of price
BOX_MIN_HEIGHT    = 0.025   # tighter than this is a data artefact, not a base
FROM_HIGH_MIN     = 0.12    # must be at least this far off the 52w high
VOL_MULT_LEDGE    = 1.5
VOL_MULT_KEEL     = 1.3
MIN_TURNOVER_CR   = 3.0     # 20-day average, in crore
ATR_FLOOR_MULT    = 1.0     # a stop closer than 1 ATR is inside the noise
RSI_DIV_MIN_GAP   = 8       # bars between the two lows of a divergence
RSI_DIV_MAX_GAP   = 60


def rsi(close: np.ndarray, n: int = 14) -> np.ndarray:
    d = np.diff(close, prepend=close[0])
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    out = np.full(close.shape, np.nan)
    if len(close) <= n:
        return out
    au, ad = up[1:n + 1].mean(), dn[1:n + 1].mean()
    for i in range(n, len(close)):
        if i > n:
            au = (au * (n - 1) + up[i]) / n
            ad = (ad * (n - 1) + dn[i]) / n
        out[i] = 100.0 if ad == 0 else 100 - 100 / (1 + au / ad)
    return out


def atr(high, low, close, n: int = 14) -> np.ndarray:
    pc = np.roll(close, 1); pc[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - pc), np.abs(low - pc)))
    out = np.full(close.shape, np.nan)
    if len(close) <= n:
        return out
    out[n] = tr[1:n + 1].mean()
    for i in range(n + 1, len(close)):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(x.shape, np.nan)
    if len(x) >= n:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def swing_lows(low: np.ndarray, left: int = 3, right: int = 3) -> list[int]:
    """Indices of pivot lows confirmed by `right` bars on the right. Because the
    confirmation is required, a pivot is only ever known `right` bars late —
    which is exactly the information a live scan would have."""
    out = []
    for i in range(left, len(low) - right):
        w = low[i - left:i + right + 1]
        if low[i] == w.min() and (w == low[i]).sum() == 1:
            out.append(i)
    return out


def _liquid(close, volume, i, window: int = 20) -> tuple[bool, float]:
    if i < window:
        return False, 0.0
    turn = (close[i - window + 1:i + 1] * volume[i - window + 1:i + 1]).mean() / 1e7
    return turn >= MIN_TURNOVER_CR, float(turn)


def darvas_box(high: np.ndarray, low: np.ndarray, i: int):
    """The tightest Darvas box ending at bar i-1, or None.

    Darvas's own rule: a box top is a high that has not been exceeded for
    several sessions, and a box floor is a low that has not been broken since.
    Walking OUTWARDS from bar i-1, the box is the longest recent stretch whose
    high and low both hold. The box must be at least BOX_MIN_BARS long, or it
    is a pause rather than a base.
    """
    best = None
    for span in range(BOX_MIN_BARS, BOX_MAX_BARS + 1):
        a, b = i - span, i          # bars [a, i-1] inclusive of a, exclusive of i
        if a < 0:
            break
        top, floor = float(high[a:b].max()), float(low[a:b].min())
        h = (top - floor) / top if top else 1.0
        if h > BOX_MAX_HEIGHT:
            break                   # widening past a real base — stop extending
        if h < BOX_MIN_HEIGHT:
            continue
        # A FLATNESS GATE WAS TRIED HERE AND REMOVED.
        # The concern was real: the longest range under the height cap can
        # swallow the tail of a decline, which puts the box top above anything
        # the consolidation actually traded and widens the stop. Requiring the
        # two halves of the box to sit within 0.35 x box height of each other
        # was measured over the same 149 names — at the 2.0x target it moved
        # LEDGE from +0.224R (t=3.20) to +0.212R (t=2.85). No benefit, one more
        # parameter, so it is not here. The height cap is doing the work.
        best = (top, floor, span)   # the longest box under the height cap wins
    return best


def ledge_signal(bars: dict, i: int) -> dict | None:
    """LEDGE — a close out of the top of a Darvas box formed off the lows.

    Reads bars[:i+1] only. `bars` carries numpy arrays: open/high/low/close/
    volume, plus precomputed atr14/sma20/sma50/rsi14.
    """
    h, l, c, v = bars["high"], bars["low"], bars["close"], bars["volume"]
    if i < 70 or not np.isfinite(bars["atr14"][i]):
        return None

    ok, turn = _liquid(c, v, i)
    if not ok:
        return None

    box = darvas_box(h, l, i)
    if not box:
        return None
    top, floor, span = box

    # THE BREAKOUT IS A CLOSE, NOT A TOUCH. A wick through the top of a box is
    # the single most common false breakout there is.
    if not (c[i] > top and c[i - 1] <= top):
        return None

    # FROM THE LOWS, NOT FROM THE HIGHS. This is the filter that separates this
    # engine from the three that already buy strength.
    look = c[max(0, i - 251):i + 1]
    hi52, lo52 = float(look.max()), float(look.min())
    from_high = (hi52 - c[i]) / hi52 if hi52 else 0.0
    if from_high < FROM_HIGH_MIN:
        return None                 # already near its highs — BREACH's job

    # Volume has to confirm. A box that leaks upward on no volume is drift.
    vavg = float(v[i - 20:i].mean())
    if not vavg or v[i] < VOL_MULT_LEDGE * vavg:
        return None

    # Not falling into the breakout: the base must be flat-to-rising, which the
    # 20-day slope over the box answers directly.
    s20 = bars["sma20"]
    if not np.isfinite(s20[i]) or not np.isfinite(s20[i - 5]):
        return None
    if s20[i] < s20[i - 5] * 0.985:
        return None

    a = float(bars["atr14"][i])
    # STRUCTURE STOP: under the box floor, which is where the idea is wrong by
    # definition — but never closer than one ATR, or it sits inside the noise.
    raw_stop = floor * 0.995
    stop = min(raw_stop, c[i] - ATR_FLOOR_MULT * a)
    risk = c[i] - stop
    if risk <= 0:
        return None

    boxh = top - floor
    return {
        "engine": "ledge", "action": "BUY", "i": i,
        "entry_ref": float(c[i]), "stop": float(stop),
        # MEASURED MOVE, AND EACH TARGET CARRIES HOW OFTEN IT IS ACTUALLY
        # REACHED. Over 188 backtested LEDGE signals: T1 was reached by 58.5%
        # of trades, T2 by 31.9%. A third target at 4x box was tested and
        # dropped — only 10.1% ever reached it, and 69% of trades timed out
        # instead, which makes it decoration rather than a target. That is the
        # same rule that removed this repo's old 4.0R third target.
        "t1": float(top + 1.0 * boxh),      # reached 58.5% of the time
        "t2": float(top + 2.0 * boxh),      # reached 31.9% of the time
        "t1_reach": 0.585, "t2_reach": 0.319,
        "rr1": float((top + boxh - c[i]) / risk),
        "rr2": float((top + 2 * boxh - c[i]) / risk),
        "why": [
            f"Closed above a {span}-bar base at {top:.1f}",
            f"Base only {(boxh/top)*100:.1f}% deep — stop sits under {floor:.1f}",
            f"Volume {v[i]/vavg:.1f}x its 20-day average",
            f"{from_high*100:.0f}% below the 52-week high — early, not extended",
        ],
        "invalidate": f"A daily CLOSE back under {stop:.1f} — the base failed",
        "meta": {"box_top": top, "box_floor": floor, "box_bars": span,
                 "from_high": from_high, "turnover_cr": turn,
                 "vol_mult": float(v[i] / vavg), "atr": a},
    }


def keel_signal(bars: dict, i: int) -> dict | None:
    """KEEL — bullish RSI divergence, reclaimed.

    Price makes a lower low than a previous swing low while RSI makes a HIGHER
    low: the selling is losing force. A divergence alone is not a trade — it can
    persist for months — so the trade is the RECLAIM: price closing back above
    the level of the earlier swing low that it undercut.
    """
    h, l, c, v = bars["high"], bars["low"], bars["close"], bars["volume"]
    r = bars["rsi14"]
    if i < 80 or not np.isfinite(bars["atr14"][i]) or not np.isfinite(r[i]):
        return None

    ok, turn = _liquid(c, v, i)
    if not ok:
        return None

    look = c[max(0, i - 251):i + 1]
    hi52 = float(look.max())
    if hi52 and (hi52 - c[i]) / hi52 < FROM_HIGH_MIN:
        return None                 # not a recovery if it never fell

    # Pivots are confirmed 3 bars late, so the most recent usable one is i-3.
    piv = [p for p in swing_lows(l[:i + 1]) if p <= i - 3]
    if len(piv) < 2:
        return None

    lo2 = piv[-1]                   # the lower low
    cand = [p for p in piv[:-1]
            if RSI_DIV_MIN_GAP <= (lo2 - p) <= RSI_DIV_MAX_GAP]
    if not cand:
        return None
    lo1 = cand[-1]                  # the earlier low it undercut

    if not (l[lo2] < l[lo1]):
        return None                 # no lower low — no divergence
    if not (np.isfinite(r[lo1]) and np.isfinite(r[lo2]) and r[lo2] > r[lo1] + 2):
        return None                 # momentum did not diverge

    # THE RECLAIM IS THE TRADE. Close back above the earlier low's level, and
    # this bar is the first to do it.
    level = float(l[lo1])
    if not (c[i] > level and c[i - 1] <= level):
        return None
    if i - lo2 > 40:
        return None                 # the divergence is stale

    # Stabilised, not still falling.
    if c[i] <= l[lo2] * 1.02:
        return None

    vavg = float(v[i - 20:i].mean())
    if not vavg or v[i] < VOL_MULT_KEEL * vavg:
        return None

    a = float(bars["atr14"][i])
    # The divergence low is the level that must hold — if it breaks, the
    # momentum argument is simply wrong.
    raw_stop = float(l[lo2]) * 0.995
    stop = min(raw_stop, c[i] - ATR_FLOOR_MULT * a)
    risk = c[i] - stop
    if risk <= 0:
        return None

    # Target the resistance that actually exists: the highest high between the
    # two lows, then the swing high before them.
    seg_hi = float(h[lo1:i + 1].max())
    t1 = max(seg_hi, c[i] + 1.6 * risk)
    # KEEL's own sweep never separated from zero (n=42, best t=1.14 at a 4R
    # target that 38% of trades reach). Its targets are therefore reference
    # levels, not a measured ladder, and the engine ships as RESEARCH.
    return {
        "engine": "keel", "action": "BUY", "i": i,
        "entry_ref": float(c[i]), "stop": float(stop),
        "t1": float(t1), "t2": float(c[i] + (t1 - c[i]) * 1.7),
        "rr1": float((t1 - c[i]) / risk),
        "why": [
            f"Price made a lower low at {l[lo2]:.1f}, RSI made a higher one "
            f"({r[lo1]:.0f} → {r[lo2]:.0f})",
            f"Reclaimed the {level:.1f} level it had lost",
            f"Volume {v[i]/vavg:.1f}x its 20-day average on the reclaim",
            f"{(hi52-c[i])/hi52*100:.0f}% below the 52-week high",
        ],
        "invalidate": f"A daily CLOSE back under {stop:.1f} — the divergence low gives way",
        "meta": {"div_low": float(l[lo2]), "prior_low": level,
                 "rsi_then": float(r[lo1]), "rsi_now": float(r[lo2]),
                 "bars_since_div": int(i - lo2), "turnover_cr": turn, "atr": a},
    }


# ── WHAT THE BACKTEST ACTUALLY SAID ──────────────────────────────────────────
# 149 liquid NSE names, 2 years of daily bars, walk-forward, entry at the next
# bar's OPEN, one position per symbol at a time, 60-day limit, stop checked
# before target. Survivorship bias is present and unfixable here: the universe
# is the 750 names screened TODAY, so companies that collapsed and dropped out
# are missing, and that flatters every long-only result below.
#
#   LEDGE  n=188  +0.224R  win 59.0%  t=3.20  PF 1.73  maxDD -7.73R
#   KEEL   n= 42  +0.125R  win 42.9%  t=0.52  PF 1.19  maxDD -7.01R
#
# The bar this repo sets before an engine may size capital is 30+ closed trades
# at t>=2. LEDGE clears it in backtest and has NOT been observed live; KEEL does
# not clear it at all. Neither is cleared for capital on a backtest alone.
ENGINE_STATUS = {
    "ledge": {"status": "PAPER", "n": 188, "expectancy": 0.224, "t": 3.20,
              "win": 59.0, "pf": 1.73, "maxdd": -7.73,
              "note": "Clears the 30-trade t>=2 bar in BACKTEST only. "
                      "No live closed trades yet."},
    "keel":  {"status": "RESEARCH", "n": 42, "expectancy": 0.125, "t": 0.52,
              "win": 42.9, "pf": 1.19, "maxdd": -7.01,
              "note": "Does not separate from zero. Published as research, "
                      "never as a call."},
}

DETECTORS = {"ledge": ledge_signal, "keel": keel_signal}


def prepare(o, h, l, c, v) -> dict:
    """Attach the indicator arrays both detectors expect."""
    o, h, l, c, v = (np.asarray(x, dtype=float) for x in (o, h, l, c, v))
    return {"open": o, "high": h, "low": l, "close": c, "volume": v,
            "atr14": atr(h, l, c, 14), "rsi14": rsi(c, 14),
            "sma20": sma(c, 20), "sma50": sma(c, 50)}
