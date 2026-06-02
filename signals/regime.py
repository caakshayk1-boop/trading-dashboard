"""signals/regime.py — Market regime detection."""

from signals.indicators import ema, adx


def regime_filter(close, high, low):
    """Returns (regime_dict, cur_adx). Returns (None, adx) if market is choppy."""
    cur_adx    = float(adx(high, low, close).iloc[-1])
    cur_ema200 = float(ema(close, 200).iloc[-1])
    cur_price  = float(close.iloc[-1])

    if cur_adx < 20:
        return None, cur_adx
    tradeable = "selective" if cur_adx < 30 else "strong"
    bias      = "bullish" if cur_price > cur_ema200 else "bearish"
    return {"tradeable": tradeable, "bias": bias, "adx": round(cur_adx, 1)}, cur_adx


def count_hh_hl(high, low, lookback=40):
    """Count higher-highs and higher-lows in last N bars."""
    h = high.iloc[-lookback:].values
    l = low.iloc[-lookback:].values
    swing_highs, swing_lows = [], []
    for i in range(2, len(h) - 2):
        if h[i] > h[i-1] and h[i] > h[i-2] and h[i] > h[i+1] and h[i] > h[i+2]:
            swing_highs.append(h[i])
        if l[i] < l[i-1] and l[i] < l[i-2] and l[i] < l[i+1] and l[i] < l[i+2]:
            swing_lows.append(l[i])
    hh_count = sum(1 for i in range(1, len(swing_highs)) if swing_highs[i] > swing_highs[i-1])
    hl_count  = sum(1 for i in range(1, len(swing_lows))  if swing_lows[i]  > swing_lows[i-1])
    return min(hh_count, hl_count), swing_highs, swing_lows


__all__ = ["regime_filter", "count_hh_hl"]
