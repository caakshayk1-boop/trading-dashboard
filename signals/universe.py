"""
signals/universe.py — Stock universe loaders + trading calendar.

Currently re-exported from scanner.py.
Migration target: move implementations here, remove from scanner.py.
"""

from scanner import (
    is_trading_day,
    FNO_ELIGIBLE,
    load_nifty500,
    load_nifty200,
    _next_thursday,
    _last_thursday_of_month,
    _smart_expiry,
    _fno_suggest,
)

__all__ = [
    "is_trading_day", "FNO_ELIGIBLE", "load_nifty500", "load_nifty200",
    "_next_thursday", "_last_thursday_of_month", "_smart_expiry", "_fno_suggest",
]
