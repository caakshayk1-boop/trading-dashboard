"""
signals/regime.py — Market regime detection.

Currently re-exported from scanner.py.
Migration target: move implementations here, remove from scanner.py.
"""

from scanner import regime_filter, count_hh_hl

__all__ = ["regime_filter", "count_hh_hl"]
