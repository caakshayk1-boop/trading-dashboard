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

# Annual statements live in their own cache with a much longer TTL. `.info` is a
# quote-adjacent blob that drifts daily (price, PE, market cap); an annual report
# is published once and never changes. Sharing one 7-day TTL between them would
# re-download 500 balance sheets every week to learn nothing.
STMT_CACHE_PATH = "cache/statements.json"
STMT_CACHE_TTL_DAYS = 30

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


def _fresh(entry: dict, days: int = CACHE_TTL_DAYS) -> bool:
    ts = entry.get("_fetched_at")
    if not ts:
        return False
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(ts)
    except ValueError:
        return False
    return age < timedelta(days=days)


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

    # Business description and holding pattern ride along on the `.info` call
    # that has already happened — no extra request, and they are the only
    # non-invented answer to "what does this company actually do". Truncated
    # because the full Yahoo summary runs to paragraphs and 500 of them would
    # dominate the cache file.
    #
    # `heldPercentInsiders` is NOT promoter holding, and must never be labelled
    # as such: for Dixon it reads 40.1% against a real promoter stake nearer
    # 32%, because Yahoo's "insiders" is a wider bucket than the SEBI
    # definition. Directionally useful, wrong to the decimal.
    summary = (info.get("longBusinessSummary") or "").strip()

    return {
        "symbol": symbol.replace(".NS", ""),
        "sector": info.get("sector") or "",
        "industry": info.get("industry") or "",
        "name": info.get("longName") or info.get("shortName") or "",
        "business": summary[:600],
        "website": info.get("website") or "",
        "employees": _num(info.get("fullTimeEmployees")),
        "held_insiders": _num(info.get("heldPercentInsiders")),
        "held_institutions": _num(info.get("heldPercentInstitutions")),
        "dividend_yield": _num(info.get("dividendYield")),
        "beta": _num(info.get("beta")),
        "market_cap_cr": (_num(info.get("marketCap")) or 0) / 1e7,   # INR → crore
        "pe": _num(info.get("trailingPE")),
        "forward_pe": _num(info.get("forwardPE")),
        "roe": _num(info.get("returnOnEquity")),                      # fraction
        # Return on assets is the check on ROE: a high ROE built on leverage
        # shows up as a thin ROA. Yahoo exposes no ROCE, so this is the closest
        # honest proxy for capital efficiency. Used by ai_longterm.
        "return_on_assets": _num(info.get("returnOnAssets")),          # fraction
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


# Keys that fetch() promises. An entry missing any of them was written by an
# older version of this module and is stale whatever its timestamp says.
#
# Added after shipping `business`: 31 symbols already sat in a fresh 7-day cache
# from before the field existed, so 32 of 500 rows in the screen rendered with no
# company description and no error anywhere — a partial-data state that would
# have healed itself within a week and been invisible while it did. Any future
# field goes in this set and old entries refresh on the next run instead.
_SCHEMA_KEYS = frozenset({"business", "held_insiders", "dividend_yield"})


def _schema_ok(entry: dict) -> bool:
    return entry.get("_miss") or _SCHEMA_KEYS.issubset(entry.keys())


def get(symbol: str, allow_fetch: bool = True) -> dict | None:
    """Cached lookup. Returns None when unknown and no fetch is allowed."""
    key = symbol.replace(".NS", "").upper()
    cache = _load_cache()
    entry = cache.get(key)
    if entry and _fresh(entry) and _schema_ok(entry):
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
    stale = [s for s in stale
             if not (cache.get(s) and _fresh(cache[s]) and _schema_ok(cache[s]))]
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


# ─────────────────────────────────────────────────────────────────────────────
# ANNUAL STATEMENTS — the multi-year history `.info` cannot give you
# ─────────────────────────────────────────────────────────────────────────────
#
# `.info` carries one snapshot: this year's ROE, this year's margin. It cannot
# answer "is ROCE rising or falling", which is the only version of the question
# worth asking, and it has no ROCE at all (see the note on return_on_assets
# above — ROA was the honest proxy available at the time).
#
# The annual income statement and balance sheet DO carry it. Yahoo returns four
# fiscal years for most NSE names, and ROCE is arithmetic on lines both frames
# publish. Verified against TCS FY26: EBIT / (Total Assets − Current
# Liabilities) = 54.9%, EBIT / Invested Capital = 62.2%. Screener.in publishes
# ~64%, so the invested-capital basis is the one that matches the Indian
# convention — hence both are computed and the caller says which it shows.
#
# Three failure modes are real and all three are handled by returning None
# rather than a number, because a plausible wrong ROCE is worse than a dash:
#
#   1. NO STATEMENTS AT ALL. TATAMOTORS returns an empty frame from Yahoo.
#      Not rare enough to treat as an error — it is a coverage fact the caller
#      has to be able to report.
#   2. NO CURRENT LIABILITIES. Banks do not present a current/non-current
#      split, so the (Total Assets − Current Liabilities) denominator does not
#      exist. This is correct rather than unfortunate: ROCE is a meaningless
#      number for a bank, whose capital employed IS its deposit base.
#   3. A BROKEN PER-SHARE SERIES. HDFCBANK's EPS goes 88.68 → 44.16 between
#      FY23 and FY24, which reads as a 50% collapse and is actually the HDFC
#      Ltd merger issuing shares. Any EPS CAGR across that boundary is a lie,
#      so the share count is tracked and `shares_changed` warns the caller off
#      per-share growth for that symbol.

# Yahoo renames these rows between versions and across sectors. First match
# wins; the fallbacks are not synonyms so much as the next-best line that
# means the same thing for this purpose.
_INCOME_ROWS = {
    "revenue":     ("Total Revenue", "Operating Revenue"),
    "ebitda":      ("EBITDA", "Normalized EBITDA"),
    "ebit":        ("EBIT", "Operating Income"),
    "net_income":  ("Net Income Common Stockholders", "Net Income",
                    "Net Income Continuous Operations"),
    "eps":         ("Diluted EPS", "Basic EPS"),
    "interest":    ("Interest Expense", "Interest Expense Non Operating"),
    "tax":         ("Tax Provision",),
    "pretax":      ("Pretax Income",),
    "shares":      ("Diluted Average Shares", "Basic Average Shares"),
}

_BALANCE_ROWS = {
    "equity":              ("Stockholders Equity", "Common Stock Equity",
                            "Total Equity Gross Minority Interest"),
    "total_assets":        ("Total Assets",),
    "current_liabilities": ("Current Liabilities",),
    "current_assets":      ("Current Assets",),
    "invested_capital":    ("Invested Capital",),
    "total_debt":          ("Total Debt",),
    "cash":                ("Cash And Cash Equivalents",
                            "Cash Cash Equivalents And Short Term Investments"),
    "shares_out":          ("Ordinary Shares Number", "Share Issued"),
}

# Below this, a year-on-year move in the share count is ordinary ESOP dilution.
# Above it, something structural happened — a merger, split, bonus or large
# placement — and the per-share series either side of it is not comparable.
_SHARE_DRIFT_TOLERANCE = 0.02

_stmt_cache = None


def _load_stmt_cache() -> dict:
    global _stmt_cache
    if _stmt_cache is not None:
        return _stmt_cache
    try:
        with open(STMT_CACHE_PATH, "r") as fh:
            _stmt_cache = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        _stmt_cache = {}
    return _stmt_cache


def _save_stmt_cache() -> None:
    if _stmt_cache is None:
        return
    os.makedirs(os.path.dirname(STMT_CACHE_PATH), exist_ok=True)
    tmp = STMT_CACHE_PATH + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(_stmt_cache, fh)
        os.replace(tmp, STMT_CACHE_PATH)
    except OSError as e:
        log.warning(f"statements: cache write failed — {e}")


def _pick(frame, names):
    """First row in `names` that the frame actually has, as a Series or None."""
    if frame is None:
        return None
    try:
        if frame.empty:
            return None
        for n in names:
            if n in frame.index:
                return frame.loc[n]
    except Exception:
        return None
    return None


def _at(series, col):
    """One cell, or None. Duplicated row labels make .loc return a frame."""
    if series is None:
        return None
    try:
        v = series[col]
    except Exception:
        return None
    if hasattr(v, "iloc"):          # duplicate index label → Series, take first
        try:
            v = v.iloc[0]
        except Exception:
            return None
    return _num(v)


def _fy_label(period) -> str:
    """FY26 for a March year-end, CY25 for a December one.

    Indian companies close in March and the convention names that year by its
    end: the twelve months to 2026-03-31 are FY26. A December closer is not on
    that calendar and mislabelling it FY26 would be wrong by a quarter, so it
    gets a CY label instead and the exact period end travels alongside anyway.
    """
    try:
        y, m = period.year, period.month
    except AttributeError:
        return str(period)[:7]
    if m == 12:
        return f"CY{y % 100:02d}"
    return f"FY{(y if m <= 3 else y + 1) % 100:02d}"


def _safe_div(a, b):
    """a/b, or None when either side is missing or the denominator is ~0."""
    if a is None or b is None or abs(b) < 1e-9:
        return None
    return a / b


def fetch_statements(symbol: str) -> dict | None:
    """One uncached statements fetch. Returns the normalised dict or None.

    None means "Yahoo has no annual statements for this symbol" — a coverage
    fact, not an error. A symbol whose statements exist but whose individual
    lines are missing returns a dict with None in those slots.
    """
    try:
        import yfinance as yf
        from symbols import to_yahoo
    except ImportError:
        return None

    try:
        tkr = yf.Ticker(to_yahoo(symbol))
        income = tkr.financials
        balance = tkr.balance_sheet
    except Exception as e:
        log.warning(f"statements: {symbol} fetch failed — {e}")
        return None

    if income is None or getattr(income, "empty", True):
        return None                      # TATAMOTORS lands here

    inc = {k: _pick(income, names) for k, names in _INCOME_ROWS.items()}
    bal = {k: _pick(balance, names) for k, names in _BALANCE_ROWS.items()}

    years = []
    for col in list(income.columns)[:4]:
        v = {k: _at(s, col) for k, s in inc.items()}
        # Balance-sheet columns are the same period ends, but not always the
        # same set — a symbol can have 4 income years and 3 balance years.
        b = {k: _at(s, col) for k, s in bal.items()}

        equity = b["equity"]
        assets = b["total_assets"]
        cur_liab = b["current_liabilities"]
        ebit = v["ebit"]

        # Capital employed, two ways. The subtraction basis is the textbook
        # definition and needs a current/non-current split the banks do not
        # publish; Yahoo's own Invested Capital line is the closer match to
        # what Indian screeners print. Neither is imputed from the other.
        cap_employed = (assets - cur_liab) if None not in (assets, cur_liab) else None

        years.append({
            "fy": _fy_label(col),
            "period_end": str(col)[:10],
            "revenue": v["revenue"],
            "ebitda": v["ebitda"],
            "ebit": ebit,
            "net_income": v["net_income"],
            "eps": v["eps"],
            "interest": v["interest"],
            "tax": v["tax"],
            "pretax": v["pretax"],
            "equity": equity,
            "total_assets": assets,
            "current_liabilities": cur_liab,
            "current_assets": b["current_assets"],
            "invested_capital": b["invested_capital"],
            "total_debt": b["total_debt"],
            "cash": b["cash"],
            "shares_out": b["shares_out"] or v["shares"],
            # All fractions, not percentages — one convention through the
            # whole repo, formatted at the edge.
            "roe": _safe_div(v["net_income"], equity),
            "roce": _safe_div(ebit, cap_employed),
            "roce_ic": _safe_div(ebit, b["invested_capital"]),
            "ebit_margin": _safe_div(ebit, v["revenue"]),
            "ebitda_margin": _safe_div(v["ebitda"], v["revenue"]),
            "net_margin": _safe_div(v["net_income"], v["revenue"]),
            "debt_to_equity": _safe_div(b["total_debt"], equity),
            "current_ratio": _safe_div(b["current_assets"], cur_liab),
            "interest_cover": _safe_div(ebit, abs(v["interest"]) if v["interest"] else None),
            "effective_tax": _safe_div(v["tax"], v["pretax"]),
        })

    if not years:
        return None

    # Newest first — every consumer wants "latest" at index 0.
    years.sort(key=lambda y: y["period_end"], reverse=True)

    counts = [y["shares_out"] for y in years if y["shares_out"]]
    shares_changed = False
    for a, b_ in zip(counts, counts[1:]):
        if b_ and abs(a - b_) / b_ > _SHARE_DRIFT_TOLERANCE:
            shares_changed = True       # HDFCBANK's merger lands here
            break

    return {
        "symbol": symbol.replace(".NS", "").upper(),
        "years": years,
        "shares_changed": shares_changed,
        "_fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def statements(symbol: str, allow_fetch: bool = True) -> dict | None:
    """Cached statements lookup. Mirrors get() — misses are cached too."""
    key = symbol.replace(".NS", "").upper()
    cache = _load_stmt_cache()
    entry = cache.get(key)
    if entry and _fresh(entry, STMT_CACHE_TTL_DAYS):
        return None if entry.get("_miss") else entry
    if not allow_fetch:
        return None
    data = fetch_statements(key)
    cache[key] = data or {"_miss": True,
                          "_fetched_at": datetime.now(timezone.utc).isoformat()}
    _save_stmt_cache()
    return data


def prefetch_statements(symbols, pause: float = 0.4, checkpoint: int = 20) -> tuple[int, int]:
    """Warm the statements cache sequentially. Returns (have, attempted).

    Sequential and jittered for the same reason prefetch() is — two frames per
    symbol is two HTTP calls, and 500 symbols in a thread pool is how you get
    the IP throttled for the rest of the run.

    Checkpoints to disk every `checkpoint` symbols. A 500-symbol cold run takes
    minutes and the workflow that calls it has a wall clock; a run killed at
    minute 14 must leave the first 400 symbols cached rather than nothing, so
    the next run finishes the job instead of restarting it.
    """
    cache = _load_stmt_cache()
    wanted = [s.replace(".NS", "").upper() for s in symbols]
    stale = [s for s in wanted
             if not (cache.get(s) and _fresh(cache[s], STMT_CACHE_TTL_DAYS))]
    if not stale:
        return len(wanted), len(wanted)

    log.info(f"statements: warming {len(stale)} of {len(wanted)} symbols")
    got = 0
    for i, sym in enumerate(stale, 1):
        data = fetch_statements(sym)
        cache[sym] = data or {"_miss": True,
                              "_fetched_at": datetime.now(timezone.utc).isoformat()}
        if data:
            got += 1
        if i % checkpoint == 0:
            _save_stmt_cache()
            log.info(f"statements: {i}/{len(stale)} ({got} with data)")
        time.sleep(pause + random.uniform(0, 0.3))
    _save_stmt_cache()

    cached_ok = sum(1 for s in wanted
                    if cache.get(s) and not cache[s].get("_miss"))
    log.info(f"statements: done — {got}/{len(stale)} fetched, "
             f"{cached_ok}/{len(wanted)} symbols now have statements")
    return cached_ok, len(wanted)
