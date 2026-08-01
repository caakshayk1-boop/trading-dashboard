#!/usr/bin/env python3
"""
fundamentals.py — company fundamentals for the signal quality gate.

Until now every scanner in this repo was pure price + volume. The only
fundamental input anywhere was a best-effort `trailingPE` inside the
multibagger scan. This module is the single source for everything else.

Design constraints, all learned the hard way:

  - yfinance `.info` is one HTTP call per symbol and Yahoo 429s on parallel
    bursts (10 concurrent chart calls poison the IP for a cooldown). So this
    fetches SEQUENTIALLY with jitter, never from a thread pool.
  - Fundamentals change quarterly, not daily. A 7-day disk cache turns a
    500-symbol scan from 500 calls into ~0 on most runs.
  - A Yahoo outage must not silently empty the scan. `prefetch()` reports the
    hit rate so the caller can tell "no signals because nothing qualified"
    apart from "no signals because the data layer was down".

Nothing here raises. Missing data returns None and the gate decides.
"""

from __future__ import annotations   # 3.9 compat — see signals/quality.py

import json
import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone

CACHE_PATH = "cache/fundamentals.json"
CACHE_TTL_DAYS = 7

# Sectors where debt/equity is a meaningless number — banks and NBFCs are
# levered by construction and a D/E gate would reject the entire sector.
_LEVERAGE_EXEMPT_SECTORS = {"Financial Services", "Financials", "Real Estate"}

log = logging.getLogger(__name__)

_cache = None


def _load_cache() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(CACHE_PATH, "r") as fh:
            _cache = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        _cache = {}
    return _cache


def _save_cache() -> None:
    if _cache is None:
        return
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(_cache, fh)
        os.replace(tmp, CACHE_PATH)
    except OSError as e:
        log.warning(f"fundamentals: cache write failed — {e}")


def _fresh(entry: dict) -> bool:
    ts = entry.get("_fetched_at")
    if not ts:
        return False
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(ts)
    except ValueError:
        return False
    return age < timedelta(days=CACHE_TTL_DAYS)


def _num(v):
    """Yahoo returns None, '', 'Infinity' and occasionally strings."""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def _next_earnings(tkr):
    """Next earnings date as a date, or None. yfinance changes this API often."""
    try:
        cal = tkr.calendar
    except Exception:
        return None
    dates = None
    if isinstance(cal, dict):
        dates = cal.get("Earnings Date")
    elif cal is not None and hasattr(cal, "empty"):
        # Older yfinance returned a DataFrame with 'Earnings Date' in the index.
        try:
            if not cal.empty and "Earnings Date" in cal.index:
                dates = list(cal.loc["Earnings Date"])
        except Exception:
            return None
    if not dates:
        return None
    if not isinstance(dates, (list, tuple)):
        dates = [dates]
    out = []
    for d in dates:
        try:
            out.append(d.date() if hasattr(d, "date") else d)
        except Exception:
            continue
    return min(out) if out else None


def fetch(symbol: str) -> dict | None:
    """One uncached network fetch. Returns the normalised dict or None."""
    try:
        import yfinance as yf
        from symbols import to_yahoo
    except ImportError:
        return None

    try:
        tkr = yf.Ticker(to_yahoo(symbol))
        info = tkr.info or {}
    except Exception as e:
        log.warning(f"fundamentals: {symbol} info fetch failed — {e}")
        return None

    if not info.get("symbol") and not info.get("longName"):
        return None  # Yahoo returns a near-empty dict for delisted/renamed tickers

    # debtToEquity arrives as a percentage (132.5 means 1.325x). Everything
    # downstream wants the ratio, so normalise once, here.
    dte_raw = _num(info.get("debtToEquity"))
    earnings = _next_earnings(tkr)

    return {
        "symbol": symbol.replace(".NS", ""),
        "sector": info.get("sector") or "",
        "industry": info.get("industry") or "",
        "market_cap_cr": (_num(info.get("marketCap")) or 0) / 1e7,   # INR → crore
        "pe": _num(info.get("trailingPE")),
        "forward_pe": _num(info.get("forwardPE")),
        "roe": _num(info.get("returnOnEquity")),                      # fraction
        "debt_to_equity": (dte_raw / 100.0) if dte_raw is not None else None,
        "revenue_growth": _num(info.get("revenueGrowth")),            # fraction, yoy
        "earnings_growth": _num(info.get("earningsGrowth")),          # fraction, yoy
        "profit_margin": _num(info.get("profitMargins")),             # fraction
        "operating_margin": _num(info.get("operatingMargins")),
        "net_income": _num(info.get("netIncomeToCommon")),
        "free_cashflow": _num(info.get("freeCashflow")),
        "book_value": _num(info.get("bookValue")),
        "price_to_book": _num(info.get("priceToBook")),
        "current_ratio": _num(info.get("currentRatio")),
        "next_earnings": earnings.isoformat() if earnings else None,
        "_fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def get(symbol: str, allow_fetch: bool = True) -> dict | None:
    """Cached lookup. Returns None when unknown and no fetch is allowed."""
    key = symbol.replace(".NS", "").upper()
    cache = _load_cache()
    entry = cache.get(key)
    if entry and _fresh(entry):
        return None if entry.get("_miss") else entry
    if not allow_fetch:
        return None
    data = fetch(key)
    cache[key] = data or {"_miss": True,
                          "_fetched_at": datetime.now(timezone.utc).isoformat()}
    _save_cache()
    return data


def prefetch(symbols, pause: float = 0.35) -> tuple[int, int]:
    """Warm the cache sequentially. Returns (hits, attempted).

    Sequential and jittered on purpose — see the module docstring. Symbols
    already cached and fresh cost nothing, so the steady-state run is fast and
    only a cold or week-old cache pays the full price.
    """
    cache = _load_cache()
    stale = [s.replace(".NS", "").upper() for s in symbols]
    stale = [s for s in stale if not (cache.get(s) and _fresh(cache[s]))]
    if not stale:
        return len(symbols), len(symbols)

    log.info(f"fundamentals: warming {len(stale)} of {len(symbols)} symbols")
    hits = 0
    for i, sym in enumerate(stale, 1):
        data = fetch(sym)
        cache[sym] = data or {"_miss": True,
                              "_fetched_at": datetime.now(timezone.utc).isoformat()}
        if data:
            hits += 1
        if i % 25 == 0:
            _save_cache()
            log.info(f"fundamentals: {i}/{len(stale)} ({hits} hits)")
        time.sleep(pause + random.uniform(0, 0.25))
    _save_cache()

    cached_ok = len(symbols) - len(stale)
    log.info(f"fundamentals: prefetch done — {hits}/{len(stale)} fetched, "
             f"{cached_ok} already cached")
    return hits + cached_ok, len(symbols)


def leverage_exempt(fund: dict | None) -> bool:
    return bool(fund) and fund.get("sector") in _LEVERAGE_EXEMPT_SECTORS
