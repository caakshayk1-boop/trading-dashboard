#!/usr/bin/env python3
"""
symbols.py — single source of truth for symbol → Yahoo Finance ticker.

Why this exists: six separate call sites each did

    sym_yf = symbol if symbol.endswith(".NS") else symbol + ".NS"

which silently turned every commodity and forex symbol into a dead NSE
ticker — GOLD.NS, SILVER.NS, XAUUSD.NS, CRUDE.NS, NATGAS.NS, USDINR.NS.
Those fetches all fail, so gold/silver prices and trade outcomes never
refreshed. A single production scan on 2026-07-29 logged 1,976 failed
fetches from this one bug.

Route every symbol through to_yahoo() instead of appending ".NS".
"""

# Canonical name → Yahoo ticker, for everything that is NOT an NSE equity.
NON_EQUITY = {
    # Precious metals — COMEX futures
    "GOLD":     "GC=F",  "XAUUSD": "GC=F",  "GC": "GC=F",
    "SILVER":   "SI=F",  "XAGUSD": "SI=F",  "SI": "SI=F",
    # Energy
    "CRUDE":    "CL=F",  "WTIUSD": "CL=F",  "WTI": "CL=F",
    "BRENT":    "BZ=F",  "BRNUSD": "BZ=F",
    "NATGAS":   "NG=F",  "NGAS":   "NG=F",
    "COPPER":   "HG=F",
    # Forex
    "USDINR": "INR=X",    "USD/INR": "INR=X",
    "EURINR": "EURINR=X", "EUR/INR": "EURINR=X",
    "GBPINR": "GBPINR=X", "GBP/INR": "GBPINR=X",
    "EURUSD": "EURUSD=X", "EUR/USD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X", "GBP/USD": "GBPUSD=X",
    "AUDUSD": "AUDUSD=X", "AUD/USD": "AUDUSD=X",
    "USDJPY": "USDJPY=X", "USD/JPY": "USDJPY=X",
    # Indices
    "NIFTY":     "^NSEI",    "NIFTY50": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "SENSEX":    "^BSESN",
}

# Suffixes that already identify a market — never append to these.
_EXPLICIT = ("=F", "=X", ".NS", ".BO", ".BSE")


def to_yahoo(symbol: str) -> str:
    """Map a canonical symbol to its Yahoo Finance ticker.

    NSE equities get ".NS". Commodities, forex and indices get their real
    ticker. Anything already carrying an explicit market marker (=F, =X,
    .NS, or a leading ^) is returned untouched.
    """
    if not symbol:
        return symbol
    s = symbol.strip().upper()
    if s.startswith("^") or s.endswith(_EXPLICIT):
        return s
    if s in NON_EQUITY:
        return NON_EQUITY[s]
    return s + ".NS"


def is_equity(symbol: str) -> bool:
    """True if the symbol resolves to an NSE equity."""
    return to_yahoo(symbol).endswith(".NS")
