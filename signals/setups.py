"""
signals/setups.py — Setup detection: pullback, breakout, divergence + scoring.

Currently re-exported from scanner.py.
Migration target: move implementations here, remove from scanner.py.
"""

from scanner import (
    setup_pullback,
    setup_breakout,
    setup_divergence,
    compute_full_score,
)

__all__ = ["setup_pullback", "setup_breakout", "setup_divergence", "compute_full_score"]
