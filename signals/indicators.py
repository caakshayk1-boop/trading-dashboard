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
    return t1_raw, t2_raw, t3_raw


__all__ = ["ema", "rsi", "adx", "atr", "macd_line", "macd_signal", "obv",
           "_tight_sl", "_structure_targets"]
