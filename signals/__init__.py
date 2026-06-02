"""
signals/ — modular signal engine package.

Implementations live here. scanner.py imports from signals/ for backward compat.

Submodules:
  signals.indicators  — ema, rsi, adx, atr, macd, obv, _tight_sl, _structure_targets
  signals.regime      — regime_filter, count_hh_hl
  signals.universe    — is_trading_day, FNO_ELIGIBLE, load_nifty500/200, expiry helpers
  signals.setups      — setup_pullback, setup_breakout, setup_divergence, compute_full_score (→ scanner.py)
  signals.commodities — fetch_forex_comm, scan_4h (→ scanner.py)
"""

from signals.indicators import (
    ema, rsi, adx, atr, macd_line, macd_signal, obv,
    _tight_sl, _structure_targets,
)
from signals.regime import regime_filter, count_hh_hl
from signals.universe import (
    is_trading_day, FNO_ELIGIBLE, load_nifty500, load_nifty200,
    _next_thursday, _last_thursday_of_month, _smart_expiry, _fno_suggest,
)

__all__ = [
    "ema", "rsi", "adx", "atr", "macd_line", "macd_signal", "obv",
    "_tight_sl", "_structure_targets",
    "regime_filter", "count_hh_hl",
    "is_trading_day", "FNO_ELIGIBLE", "load_nifty500", "load_nifty200",
    "_next_thursday", "_last_thursday_of_month", "_smart_expiry", "_fno_suggest",
]
