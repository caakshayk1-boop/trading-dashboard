"""
signals/commodities.py — Commodity and forex signal functions.

Currently re-exported from scanner.py.
Migration target: move implementations here, remove from scanner.py.
"""

from scanner import fetch_forex_comm, _comm_weekly_bias, scan_4h

__all__ = ["fetch_forex_comm", "_comm_weekly_bias", "scan_4h"]
