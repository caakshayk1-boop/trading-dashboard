"""
signals/ — modular signal engine package.

Current state: thin re-exports from scanner.py (backward-compatible).
Migration plan: move actual implementations here, make scanner.py a legacy shim.

Submodules:
  signals.indicators  — ema, rsi, adx, atr, macd, obv
  signals.regime      — regime_filter, count_hh_hl
  signals.setups      — setup_pullback, setup_breakout, setup_divergence, compute_full_score
  signals.universe    — is_trading_day, FNO_ELIGIBLE, load_nifty500, load_nifty200
  signals.commodities — fetch_forex_comm, scan_4h, _comm_weekly_bias
"""

from signals.indicators  import ema, rsi, adx, atr, macd_line, macd_signal, obv
from signals.regime      import regime_filter, count_hh_hl
from signals.setups      import setup_pullback, setup_breakout, setup_divergence, compute_full_score
from signals.universe    import is_trading_day, FNO_ELIGIBLE, load_nifty500, load_nifty200
from signals.commodities import fetch_forex_comm, scan_4h

__all__ = [
    "ema", "rsi", "adx", "atr", "macd_line", "macd_signal", "obv",
    "regime_filter", "count_hh_hl",
    "setup_pullback", "setup_breakout", "setup_divergence", "compute_full_score",
    "is_trading_day", "FNO_ELIGIBLE", "load_nifty500", "load_nifty200",
    "fetch_forex_comm", "scan_4h",
]
