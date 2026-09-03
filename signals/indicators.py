"""signals/indicators.py — Technical indicator functions."""

import ta as ta_lib
import pandas as pd


def ema(series, n):
    return ta_lib.trend.EMAIndicator(series, window=n).ema_indicator()

def rsi(series, n=14):
    return ta_lib.momentum.RSIIndicator(series, window=n).rsi()

def adx(high, low, close, n=14):
    return ta_lib.trend.ADXIndicator(high, low, close, window=n).adx()

def atr(high, low, close, n=14):
    return ta_lib.volatility.AverageTrueRange(high, low, close, window=n).average_true_range()

def macd_line(series):
    return ta_lib.trend.MACD(series).macd()

def macd_signal(series):
    return ta_lib.trend.MACD(series).macd_signal()

def obv(close, volume):
    return ta_lib.volume.OnBalanceVolumeIndicator(close, volume).on_balance_volume()


# ── THE HOUSE STOP AND TARGET LADDER, IN ONE PLACE ──────────────────────────
#
# Set 2026-09-03 on the operator's instruction: stops ~6% tighter than the
# 1.5x floor introduced the day before, and a first target lifted from 1.5R to
# 1.6R with the third pulled in from 4.0R to 3.3R.
#
# WHY 1.41 IS SAFE AND 1.0 WOULD NOT BE. The measurement that produced 1.5x
# was that stops INSIDE the instrument's own ATR are taken out by noise:
# breakout sat at 0.29x and ohl at 0.78x, and they stopped out 90.9% and 91.7%
# of the time. 1.41x is 6% tighter and still comfortably outside that floor,
# so this is a trim within the safe band rather than a reversal of it. Anything
# below 1.0x would put the stop back inside a normal session's range and
# re-open the fault.
#
# The third target moving 4.0R -> 3.3R is the more consequential half. Across
# 118 closed trades exactly nine reached a second target and none reached a
# third, so 4.0R was a level the ledger has never once printed. A target that
# has never been hit is not a target, it is decoration on the card.
ATR_STOP_MULT = 1.41       # was 1.5 (itself up from a broken 1.0-and-tighter)
R1_MULT, R2_MULT, R3_MULT = 1.6, 2.5, 3.3    # was 1.5, 2.5, 4.0


def _tight_sl(price: float, low_series, cur_atr: float,
              max_pct: float = 0.06, min_pct: float = 0.015,
              atr_mult: float = ATR_STOP_MULT) -> float:
    """Stop that respects structure but is never inside the noise.

    IT USED TO TAKE THE TIGHTER OF THE TWO CANDIDATES, AND THAT WAS THE BUG.

        sl_raw = max(sl_struct, sl_atr)      # max() for a long = the HIGHER
                                             # stop = the TIGHTER one

    Selecting the tighter of a structural and a volatility stop selects, every
    time, for the one closer to price — so the stop landed inside the range the
    instrument covers in a normal session and was taken out by noise rather
    than by the trade being wrong.

    This is not a new diagnosis. cf_engine.py was rewritten for exactly this
    fault and its docstring still records it: "Stops are floored, not tightened.
    The old code took the TIGHTER of the structural and ATR stop (max() for
    BUY, min() for SELL), which selects for stops inside the noise... We take
    the WIDER, then enforce a hard ATR floor." That fix was applied to one
    engine and never reached this shared helper, which is what ohl, breakout
    and the 4H engine all call.

    MEASURED, on 109 closed trades re-walked bar by bar (exit_rules_v2.py),
    with stop distance compared to each name's own ATR scaled to its horizon:

        engine     stop/ATR   stop-out rate
        breakout      0.29x       90.9%
        ohl           0.78x       91.7%
        magic         1.22x       85.7%
        multibagger   1.57x       72.7%
        equity_meas.  1.94x       54.5%

    Monotonic, and cf_1h — the one engine that already had the floor — is the
    only engine in the ledger with a positive baseline expectancy (+0.088R).

    WHAT THIS DOES NOT DO. Widening moved measured expectancy from -0.522R to
    -0.342R at a 2x stop. That is a real improvement and it is still a losing
    system at t = -4.74. This removes a defect; it does not create an edge, and
    nothing here should be read as having made these engines profitable.

    atr_mult   the volatility floor, in ATR. 1.5 is the smallest multiple that
               put every engine above 1.0x measured, and 1.0 was what produced
               the stop-out rates above.
    max_pct    hard cap. Binds on longer horizons, where one ATR is already a
               large percentage — callers on weekly bars must raise it or the
               cap silently reimposes the tight stop this fixes.
    min_pct    absolute floor for a very quiet name. With the wider stop it
               rarely binds; before this change it was binding on nearly every
               ohl and breakout signal, which is why their median stop sat at
               1.50% against a 1.5% floor.
    """
    swing_low  = float(low_series.rolling(5).min().iloc[-1])
    sl_struct  = swing_low - 0.25 * cur_atr
    sl_atr     = price - atr_mult * cur_atr
    # THE WIDER of the two, i.e. the LOWER price for a long.
    sl_raw     = min(sl_struct, sl_atr)
    sl_capped  = max(sl_raw, price * (1 - max_pct))
    sl_floored = min(sl_capped, price * (1 - min_pct))
    return round(sl_floored, 2)


# Minimum spacing between consecutive targets, in ATR. Below this they are not
# distinguishable as separate exits — see the note inside _structure_targets.
MIN_TARGET_GAP_ATR = 0.5


def _structure_targets(price: float, cur_atr: float, high_series,
                       r1_mult: float = R1_MULT, r2_mult: float = R2_MULT,
                       r3_mult: float = R3_MULT):
    """Targets anchored to price structure + R-multiples, snapped to resistance."""
    res20  = float(high_series.rolling(20).max().iloc[-1])
    res10  = float(high_series.rolling(10).max().iloc[-2]) if len(high_series) > 11 else price * 1.05
    t1_raw = round(price + r1_mult * cur_atr, 2)
    t2_raw = round(price + r2_mult * cur_atr, 2)
    t3_raw = round(price + r3_mult * cur_atr, 2)
    for res in sorted([res10, res20]):
        if t1_raw * 0.985 <= res <= t1_raw * 1.02:
            t1_raw = round(res * 0.995, 2)
            break
    if t2_raw * 0.985 <= res20 <= t2_raw * 1.03:
        t2_raw = round(res20 * 0.995, 2)

    # Keep the targets far enough apart to BE separate targets.
    #
    # The two snaps above run independently, and nothing stopped them landing
    # on the same wall. Live on 2026-08-18: TECHM published entry 1592, stop
    # 1568.12, T1 1673.09 and T2 1678.17 — five rupees apart on 24 rupees of
    # risk, or 0.2R. Ten of 157 open signals had the same collapse.
    #
    # Two targets 0.2R apart are one target printed twice. The reader books
    # partial profit at T1 and the "second" target is already hit, so the
    # ladder the signal claims to offer does not exist.
    #
    # The floor is in ATR, not percent, because ATR is what the R-multiples
    # above are built from — a half-ATR is a real move on this instrument,
    # while "1%" means something different for a ₹90 stock and a ₹9,000 one.
    #
    # Pushing the outer target OUT rather than pulling the inner one in: T1 is
    # the one anchored to the nearest real resistance, and it is the target
    # most likely to actually fill.
    gap = MIN_TARGET_GAP_ATR * cur_atr
    if t2_raw - t1_raw < gap:
        t2_raw = round(t1_raw + gap, 2)
    if t3_raw - t2_raw < gap:
        t3_raw = round(t2_raw + gap, 2)
    return t1_raw, t2_raw, t3_raw


__all__ = ["ema", "rsi", "adx", "atr", "macd_line", "macd_signal", "obv",
           "_tight_sl", "_structure_targets"]
