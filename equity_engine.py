#!/usr/bin/env python3
"""
equity_engine.py — NSE equity signals through the measured cf_engine core.

scanner.py builds equity targets off the stop distance:

    t1   = price + (price - sl) * 1.5
    t2   = price + (price - sl) * 2.5
    rr   = (t2 - price) / risk          # = 2.5, always

so every alert printed "RR 2.5" regardless of the setup and the `if rr < 2.0`
gate could never fire. This module reuses cf_engine.evaluate(), which derives
targets from real swing structure and measures R:R off them.

No signal logic is duplicated here — only the equity-specific configuration
and data plumbing. evaluate() is asset-agnostic: it takes an entry frame, a
regime frame, and a daily frame.

Horizons
--------
swing : entry daily, regime weekly, ~weeks held      (replaces the swing scan)
intra : entry 1h,    regime daily,  ~days held       (replaces the 4H scan)
"""

from dataclasses import replace

from cf_engine import CONFIG, evaluate, format_alert  # noqa: F401  (re-exported)
from symbols import to_yahoo

# Equities differ from futures in three ways that matter here:
#   - they gap overnight, so stops need more room than a 1.5x ATR floor
#   - exchange volume is real and reliable, unlike spiky futures volume,
#     so the participation gate can be stricter and actually mean something
#   - single-stock news moves are larger, so the chase guard is looser
EQUITY_CONFIG = replace(
    CONFIG,
    atr_mult_sl=2.0,
    min_sl_atr=2.0,
    min_vol_ratio=1.3,
    require_volume=True,
    max_day_move=6.0,
    min_rr=1.8,
)

# Liquid, continuously-listed names — a backtest universe, not a watchlist.
LIQUID = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR", "SBIN",
    "BHARTIARTL", "KOTAKBANK", "LT", "AXISBANK", "ASIANPAINT", "MARUTI",
    "TITAN", "SUNPHARMA", "WIPRO", "HCLTECH", "TECHM", "NESTLEIND", "ONGC",
    "TATAMOTORS", "TATASTEEL", "JSWSTEEL", "BAJFINANCE", "ADANIPORTS",
    "CIPLA", "DRREDDY", "EICHERMOT", "GRASIM", "HINDALCO",
]

_HORIZONS = {
    # horizon: (entry interval, regime interval, entry period, regime period)
    "swing": ("1d", "1wk", "5y",   "5y"),
    "intra": ("1h", "1d",  "720d", "2y"),
}


def fetch(symbol: str, horizon: str = "swing"):
    """Return (entry_frame, regime_frame, daily_frame, price) or None."""
    import yfinance as yf

    if horizon not in _HORIZONS:
        raise ValueError(f"unknown horizon {horizon!r}")
    ei, ri, ep, rp = _HORIZONS[horizon]
    t = to_yahoo(symbol)

    entry  = yf.download(t, period=ep, interval=ei, progress=False, auto_adjust=True)
    regime = yf.download(t, period=rp, interval=ri, progress=False, auto_adjust=True)
    daily  = (entry if ei == "1d" else
              yf.download(t, period="5d", interval="1d", progress=False, auto_adjust=True))

    if any(d is None or d.empty for d in (entry, regime, daily)):
        return None
    price = float(entry["Close"].to_numpy().ravel()[-1])
    return entry, regime, daily, price


def scan(symbols=None, horizon: str = "swing", cfg=None):
    """Fetch + evaluate each symbol. Returns signals sorted by conviction."""
    import logging
    cfg = cfg or EQUITY_CONFIG
    out = []
    for s in (symbols or LIQUID):
        try:
            frames = fetch(s, horizon)
            if frames is None:
                continue
            entry, regime, daily, price = frames
            sig = evaluate(s, entry, regime, daily, price=price, cfg=cfg)
            if sig:
                sig["horizon"] = horizon
                out.append(sig)
        except Exception as e:
            logging.warning(f"equity_engine: {s} failed — {e}")
    return sorted(out, key=lambda x: -x["score"])
