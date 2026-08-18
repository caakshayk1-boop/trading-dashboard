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


def _tight_sl(price: float, low_series, cur_atr: float,
              max_pct: float = 0.06, min_pct: float = 0.015) -> float:
    """Tightest logical SL respecting structure. Hard cap: max_pct%, hard floor: min_pct%."""
    swing_low  = float(low_series.rolling(5).min().iloc[-1])
    sl_struct  = swing_low - 0.25 * cur_atr
    sl_atr     = price - 1.0 * cur_atr
    sl_raw     = max(sl_struct, sl_atr)
    sl_capped  = max(sl_raw, price * (1 - max_pct))
    sl_floored = min(sl_capped, price * (1 - min_pct))
    return round(sl_floored, 2)


# Minimum spacing between consecutive targets, in ATR. Below this they are not
# distinguishable as separate exits — see the note inside _structure_targets.
MIN_TARGET_GAP_ATR = 0.5


def _structure_targets(price: float, cur_atr: float, high_series,
                       r1_mult: float = 1.5, r2_mult: float = 2.5, r3_mult: float = 4.0):
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
