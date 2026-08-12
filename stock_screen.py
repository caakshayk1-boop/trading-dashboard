#!/usr/bin/env python3
"""
stock_screen.py — the NSE Total Market research screen behind #stocks.

What this is
------------
Every other engine in this repo answers "what should I trade this week". This
one answers a slower question: "of 750 listed companies, which few are worth
an hour of reading, and why". So it is built on annual statements rather than
on 15-minute candles, and it runs weekly rather than daily.

It follows funds.py exactly in shape — a slow builder with its own workflow
clock, publishing a JSON payload into Turso that the daily paper only READS.
The 6 AM build must never wait on 500 sequential Yahoo fetches; that mistake
has already been made once here (see fund_screen.yml, which exists because a
weekly nice-to-have blew the daily job past its 15-minute cap).

The four scores, and why they are not one score
-----------------------------------------------
A single number would be the easy thing to publish and the wrong thing. A
company can be excellent and expensive; a chart can be strong while the
business degrades. Collapsing that into one figure destroys the only
information a reader needs to disagree with it. So:

    quality     how good the business is        (ROCE, ROE, margins, leverage)
    growth      how fast it is compounding      (3Y revenue/EBITDA/EPS CAGR)
    valuation   how it is priced vs its PEERS   (PE/PB percentile in-industry)
    technical   what the chart is doing         (MA structure, momentum, RS)

`composite` is a declared weighted blend of those four, and it always ships
beside its parts so any rank can be taken apart. The weights live in one dict
below, not scattered through the scoring functions.

Honesty rules this module will not break
----------------------------------------
  - A missing input scores None and is excluded from its parent score's
    denominator. It is never imputed, never zero-filled, and a score computed
    from two of five inputs says so via `_conf`.
  - ROCE is real arithmetic on published statements, not a proxy. Where the
    statements do not support it — every bank, because ROCE is meaningless for
    one — it stays None rather than falling back to something plausible.
  - Per-share growth is suppressed entirely when the share count moved
    structurally, because an EPS CAGR across a merger is a fabricated number.
  - Nothing here predicts. There is no probability, no target, no "will rise".
    A high composite means "ranked well on published data", and the SWOT lines
    quote the number that drove them so the reader can check the arithmetic.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import re
import statistics
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# ── universe ────────────────────────────────────────────────────────────────
#
# There is no such thing as a "Nifty 1000". NSE's widest published equity index
# is NIFTY TOTAL MARKET at 752 constituents, and it is exactly Nifty 500 plus
# Nifty Microcap 250 — verified: the 500 and the 250 are both strict subsets and
# their union is the 752 to the symbol. Two of those 752 are DUMMY placeholders,
# so 750 names are real.
#
# Using the official list rather than composing one, because a hand-composed
# universe drifts from the index it claims to be the moment NSE rebalances, and
# then every breadth number on the page is measured against something that does
# not exist.
UNIVERSE_CSV = "cache/nifty_total_market.csv"
UNIVERSE_URL = ("https://nsearchives.nseindia.com/content/indices/"
                "ind_niftytotalmarket_list.csv")
# Fallback only. scanner.py maintains this one, so it is guaranteed to be there
# even when NSE refuses us — a 500-name screen is a smaller screen, not a broken
# one, and it is a much better outcome than no section at all.
UNIVERSE_FALLBACK_CSV = "cache/nifty500.csv"
UNIVERSE_MAX_AGE_DAYS = 14      # NSE rebalances semi-annually; this is generous
# Which sub-index each name belongs to, for the tier label. Fetched only to
# annotate — membership never decides whether a symbol is screened.
TIER_LISTS = [
    ("large", "ind_nifty100list.csv"),
    ("mid",   "ind_niftymidcap150list.csv"),
    ("small", "ind_niftysmallcap250list.csv"),
    ("micro", "ind_niftymicrocap250_list.csv"),
]
NSE_HEADERS = {
    # NSE answers a bare urllib agent with a 403. Same reason content_cache
    # carries its own _UA.
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"),
    "Accept": "text/csv,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
BENCHMARK = "^NSEI"

# Composite weights. One home, on purpose — these were four magic numbers
# inside four different functions in the first draft, which made the published
# ranking impossible to explain without reading all four.
WEIGHTS = {
    "quality":   0.35,
    "growth":    0.25,
    "technical": 0.25,
    "valuation": 0.15,
}

# ── ranking modes ───────────────────────────────────────────────────────────
#
# THE POINT: a good company and a good thing to buy today are different
# questions, and one composite cannot answer both. The same business can be
# excellent and fully priced, or mediocre and setting up well. Ranking everything
# by one blend forces those two facts through one number and loses whichever one
# the reader cared about.
#
# So the weights become a MODE, and the mode is the reader's declared question:
#
#   investor    "is this a business worth owning for years"
#               → quality and price dominate; the chart barely matters
#   positional  "is this compounding AND working right now"
#               → growth and trend carry it, quality still gates it
#   swing       "is this technically actionable in the next few weeks"
#               → almost entirely the chart; fundamentals only as a floor
#
# Declared as weight sets over the SAME component registry rather than as three
# separate scoring functions, which is what keeps them honest: a component added
# later (cash flow, earnings momentum) appears in every mode that names it, and
# no mode can quietly use a metric the others cannot see.
#
# Weights within a mode do not need to sum to 1 — _composite renormalises over
# whichever components actually resolved for that company.
MODES = {
    "investor":   {"quality": 0.40, "growth": 0.20, "valuation": 0.25,
                   "technical": 0.05, "cashflow": 0.10},
    "positional": {"quality": 0.25, "growth": 0.25, "valuation": 0.10,
                   "technical": 0.25, "earnings_momentum": 0.15},
    "swing":      {"technical": 0.60, "growth": 0.10, "quality": 0.10,
                   "valuation": 0.05, "earnings_momentum": 0.15},
}
DEFAULT_MODE = "balanced"      # the WEIGHTS blend above, kept as the headline

# Bars needed before an indicator is allowed to produce a number. A 200-day
# average of 60 bars is not a 200-day average, and a newly listed stock is the
# case that exposes it.
MIN_BARS = {"sma200": 200, "sma50": 50, "sma20": 20, "rsi": 15, "macd": 26,
            "atr": 15, "r3y": 700, "r1y": 240, "r6m": 120, "r3m": 60,
            "r1m": 20, "r1w": 5, "high52": 240}

# Fetch pacing. Yahoo throttles an IP that bursts; 1,976 failed fetches in one
# 2026-07-29 scan is the documented cost of getting this wrong.
PRICE_BATCH = 40
PRICE_PAUSE = 1.2

# A one-year EBIT-margin move of this many percentage points is treated as a
# probable accounting event rather than trading performance. See updates().
ONE_OFF_MARGIN_PT = 15.0


# ─────────────────────────────────────────────────────────────────────────────
# UNIVERSE
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_index_csv(filename: str) -> list[dict] | None:
    """One NSE index CSV as rows, or None. Never raises."""
    import urllib.request
    url = ("https://nsearchives.nseindia.com/content/indices/" + filename)
    try:
        req = urllib.request.Request(url, headers=NSE_HEADERS)
        raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8-sig")
    except Exception as e:
        log.warning(f"screen: NSE {filename} unavailable — {e}")
        return None
    import io
    rows = [r for r in csv.DictReader(io.StringIO(raw)) if (r.get("Symbol") or "").strip()]
    return rows or None


def refresh_universe(path: str = UNIVERSE_CSV, max_age_days: int = UNIVERSE_MAX_AGE_DAYS) -> bool:
    """Refresh the cached constituent list if it is missing or stale.

    Returns True if a usable file is in place afterwards. A refresh failure with
    a stale-but-present file is NOT an error: an out-of-date index membership
    costs a handful of names at the edges, while refusing to run costs the whole
    section. NSE is the least reliable dependency this repo has.
    """
    try:
        age_days = (time.time() - os.path.getmtime(path)) / 86400
        if age_days < max_age_days:
            return True
    except OSError:
        age_days = None

    rows = _fetch_index_csv(os.path.basename(UNIVERSE_URL))
    if not rows:
        have = os.path.exists(path)
        log.warning("screen: universe refresh failed — "
                    + (f"using the cached list ({age_days:.0f}d old)" if have
                       else "and there is no cached list"))
        return have

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        os.replace(tmp, path)
    except OSError as e:
        log.warning(f"screen: universe write failed — {e}")
        return os.path.exists(path)
    log.info(f"screen: universe refreshed — {len(rows)} constituents")
    return True


def _tier_map() -> dict:
    """symbol -> large/mid/small/micro, from NSE's own sub-indices.

    Annotation only. A market-cap band computed from a market-cap number is
    already in the payload; this is the INDEX membership, which is what a reader
    actually means by "smallcap" in an Indian context and is not always what a
    rupee threshold says.
    """
    out: dict = {}
    for tier, fname in TIER_LISTS:
        rows = _fetch_index_csv(fname)
        if not rows:
            continue
        for r in rows:
            out.setdefault((r.get("Symbol") or "").strip().upper(), tier)
    return out


def universe(path: str = UNIVERSE_CSV, refresh: bool = False,
             tiers: bool = False) -> list[dict]:
    """NSE Nifty Total Market constituents — 752 listed, 750 real.

    Read from a cached CSV rather than fetched on every call, because NSE blocks
    unfriendly clients and a screen that cannot run without a live NSE handshake
    is a screen that stops running. `refresh=True` (the weekly build) updates the
    cache first and degrades to the stale copy on failure.
    """
    if refresh:
        refresh_universe(path)
    if not os.path.exists(path):
        # scanner.py maintains the 500 list, so it is the one that is always
        # there. Half a universe beats none — but it must never be QUIET. A run
        # that screens 500 names while the page says 750 is the kind of silent
        # shrink this repo keeps getting caught by, so it is logged as an error
        # and the payload records which list was actually used.
        log.error(f"screen: {path} missing — FALLING BACK to "
                  f"{UNIVERSE_FALLBACK_CSV}; the universe is smaller than the "
                  f"section claims")
        path = UNIVERSE_FALLBACK_CSV

    tier_of = _tier_map() if tiers else {}
    rows = []
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                sym = (r.get("Symbol") or "").strip().upper()
                if not sym:
                    continue
                # NSE's own list carries placeholder constituents — the 500 list
                # has four "Dummy Vedanta Ltd." rows (DUMMYVEDL1..4, ISINs
                # DU1…DU4) and Total Market adds DUMMYINXGN and DUMMYTRVN. They
                # are not tradeable, they 404 on every data call, and left in they
                # burn fetch budget to produce empty rows.
                if sym.startswith("DUMMY") or (r.get("ISIN Code") or "").startswith("DU"):
                    continue
                rows.append({
                    "symbol": sym,
                    "name": (r.get("Company Name") or "").strip(),
                    "industry": (r.get("Industry") or "").strip(),
                    "isin": (r.get("ISIN Code") or "").strip(),
                    "tier": tier_of.get(sym, ""),
                })
    except OSError as e:
        log.warning(f"screen: universe read failed — {e}")
        return []
    # ISIN is the canonical identifier, so a duplicated one is a duplicated
    # security (two series of the same company) and only the first survives.
    seen, out = set(), []
    for r in rows:
        key = r["isin"] or r["symbol"]
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# INDICATORS — pure functions on a list of floats
# ─────────────────────────────────────────────────────────────────────────────
#
# Deliberately plain Python on plain lists rather than pandas expressions. They
# are unit-tested in test_stock_screen.py, and a test that has to construct a
# DataFrame to check an average is a test nobody writes.

def sma(vals: list[float], n: int) -> float | None:
    if not vals or len(vals) < n:
        return None
    w = vals[-n:]
    return sum(w) / n


def ema_series(vals: list[float], n: int) -> list[float]:
    """EMA over the whole series, seeded with an SMA of the first n values."""
    if len(vals) < n:
        return []
    k = 2.0 / (n + 1.0)
    out = [sum(vals[:n]) / n]
    for v in vals[n:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(vals: list[float], n: int = 14) -> float | None:
    """Wilder's RSI. None when there is not enough history to smooth."""
    if len(vals) < n + 1:
        return None
    gains, losses = [], []
    for a, b in zip(vals, vals[1:]):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag = (ag * (n - 1) + g) / n
        al = (al * (n - 1) + l) / n
    if al < 1e-12:
        # No down move in the window. RSI is 100 by definition, not undefined —
        # but only if there was an up move at all; a flat line is neither.
        return 100.0 if ag > 1e-12 else 50.0
    rs = ag / al
    return 100.0 - (100.0 / (1.0 + rs))


def macd(vals: list[float], fast: int = 12, slow: int = 26, sig: int = 9) -> dict:
    """MACD line, signal and histogram. Values are None when unavailable."""
    empty = {"macd": None, "signal": None, "hist": None, "hist_prev": None}
    if len(vals) < slow + sig:
        return empty
    ef, es = ema_series(vals, fast), ema_series(vals, slow)
    if not ef or not es:
        return empty
    # Align: the slow EMA starts (slow-fast) samples later than the fast one.
    ef = ef[len(ef) - len(es):]
    line = [f - s for f, s in zip(ef, es)]
    sigl = ema_series(line, sig)
    if not sigl:
        return empty
    line_t = line[len(line) - len(sigl):]
    hist = [a - b for a, b in zip(line_t, sigl)]
    return {
        "macd": line_t[-1],
        "signal": sigl[-1],
        "hist": hist[-1],
        "hist_prev": hist[-2] if len(hist) > 1 else None,
    }


def atr(high: list[float], low: list[float], close: list[float], n: int = 14) -> float | None:
    """Wilder's ATR in price terms."""
    if min(len(high), len(low), len(close)) < n + 1:
        return None
    tr = []
    for i in range(1, len(close)):
        tr.append(max(high[i] - low[i],
                      abs(high[i] - close[i - 1]),
                      abs(low[i] - close[i - 1])))
    if len(tr) < n:
        return None
    a = sum(tr[:n]) / n
    for t in tr[n:]:
        a = (a * (n - 1) + t) / n
    return a


def pct_change(vals: list[float], bars: int) -> float | None:
    """Return over `bars` sessions as a fraction. None when too short."""
    if len(vals) <= bars:
        return None
    old, new = vals[-1 - bars], vals[-1]
    if old is None or new is None or abs(old) < 1e-9:
        return None
    return (new / old) - 1.0


def cagr(first: float | None, last: float | None, years: float) -> float | None:
    """Compound annual growth. None when the sign flip makes it meaningless.

    A move from a loss to a profit has no CAGR — the root of a negative number
    is not a growth rate, and reporting one is how screens end up publishing
    "+340% earnings CAGR" for a company that simply stopped losing money.
    """
    if first is None or last is None or years <= 0:
        return None
    if first <= 0 or last <= 0:
        return None
    return (last / first) ** (1.0 / years) - 1.0


def _median(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def _finite(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# ─────────────────────────────────────────────────────────────────────────────
# PRICES
# ─────────────────────────────────────────────────────────────────────────────

def fetch_prices(symbols: list[str], period: str = "4y") -> dict[str, dict]:
    """Daily OHLCV per symbol, batched. Returns {symbol: {o,h,l,c,v}}.

    Batched rather than per-symbol (one request per 40 names instead of 500),
    and paced between batches. auto_adjust=True, so the close series is split
    and dividend adjusted — which is what a multi-year return needs and means
    the 3Y figure is a total return, not price appreciation alone.
    """
    try:
        import yfinance as yf
        from symbols import to_yahoo
    except ImportError:
        log.warning("screen: yfinance unavailable")
        return {}

    out: dict[str, dict] = {}
    tickers = {to_yahoo(s): s for s in symbols}
    keys = list(tickers)
    batches = [keys[i:i + PRICE_BATCH] for i in range(0, len(keys), PRICE_BATCH)]

    for bi, batch in enumerate(batches, 1):
        try:
            df = yf.download(batch, period=period, interval="1d", progress=False,
                             threads=False, auto_adjust=True, group_by="column")
        except Exception as e:
            log.warning(f"screen: price batch {bi}/{len(batches)} failed — {e}")
            continue
        if df is None or df.empty:
            log.warning(f"screen: price batch {bi}/{len(batches)} empty")
            continue

        for tk in batch:
            sym = tickers[tk]
            try:
                # A single-ticker batch comes back with flat columns.
                if len(batch) == 1:
                    sub = df
                    cols = {c: c for c in ("Open", "High", "Low", "Close", "Volume")}
                    series = {k: sub[v].dropna() for k, v in cols.items() if v in sub}
                else:
                    series = {}
                    for f in ("Open", "High", "Low", "Close", "Volume"):
                        if (f, tk) in df.columns:
                            series[f] = df[(f, tk)].dropna()
                if "Close" not in series or series["Close"].empty:
                    continue
                idx = series["Close"].index
                rec = {
                    "c": [float(x) for x in series["Close"].tolist()],
                    "h": [float(x) for x in series.get("High", series["Close"]).reindex(idx).ffill().tolist()],
                    "l": [float(x) for x in series.get("Low", series["Close"]).reindex(idx).ffill().tolist()],
                    "v": [float(x) for x in series.get("Volume", series["Close"] * 0).reindex(idx).fillna(0).tolist()],
                    # Dates alongside the closes so valuation_history can price a
                    # fiscal year end. Transient — never shipped in the payload.
                    "dates": [str(d)[:10] for d in idx],
                    "last_date": str(idx[-1])[:10],
                }
                out[sym] = rec
            except Exception as e:
                log.debug(f"screen: {sym} price parse — {e}")

        log.info(f"screen: prices {bi}/{len(batches)} batches, {len(out)} symbols")
        if bi < len(batches):
            time.sleep(PRICE_PAUSE)
    return out


def technicals(px: dict, bench: dict | None) -> dict:
    """Chart state for one symbol. Every field is None when unsupported."""
    c, h, l, v = px["c"], px["h"], px["l"], px["v"]
    n = len(c)
    last = c[-1]

    t = {
        "price": round(last, 2),
        "bars": n,
        "last_date": px.get("last_date"),
        "sma20": sma(c, 20) if n >= MIN_BARS["sma20"] else None,
        "sma50": sma(c, 50) if n >= MIN_BARS["sma50"] else None,
        "sma200": sma(c, 200) if n >= MIN_BARS["sma200"] else None,
        "rsi14": rsi(c, 14) if n >= MIN_BARS["rsi"] else None,
        "atr14": atr(h, l, c, 14) if n >= MIN_BARS["atr"] else None,
    }
    t.update(macd(c) if n >= MIN_BARS["macd"] else
             {"macd": None, "signal": None, "hist": None, "hist_prev": None})

    t["atr_pct"] = (t["atr14"] / last) if t["atr14"] and last else None

    # Returns. Trading-day counts, not calendar — 250 bars is a year.
    for key, bars in (("r1w", 5), ("r1m", 21), ("r3m", 63),
                      ("r6m", 126), ("r1y", 250), ("r3y", 750)):
        need = MIN_BARS.get(key, bars)
        t[key] = pct_change(c, bars) if n >= need else None
    t["r3y_cagr"] = cagr(c[-751] if n > 750 else None, last, 3.0) if n > 750 else None

    # 52-week structure.
    if n >= MIN_BARS["high52"]:
        w = c[-250:]
        hi, lo = max(w), min(w)
        t["high52"], t["low52"] = hi, lo
        t["from_high52"] = (last / hi - 1.0) if hi else None
        t["from_low52"] = (last / lo - 1.0) if lo else None
    else:
        t["high52"] = t["low52"] = t["from_high52"] = t["from_low52"] = None

    # Volume. A zero or missing average must not become a division.
    av20 = sma(v, 20) if n >= 20 else None
    av50 = sma(v, 50) if n >= 50 else None
    t["volume"] = v[-1] if v else None
    t["avg_vol20"] = av20
    t["vol_spike"] = (v[-1] / av20) if av20 and av20 > 0 and v else None
    t["turnover_cr"] = (v[-1] * last / 1e7) if v and v[-1] else None
    t["liquid"] = bool(av50 and av50 * last / 1e7 >= 1.0)   # ≥₹1cr/day traded

    # Breakouts, measured against the window EXCLUDING today — otherwise every
    # bar is its own 20-day high and the signal fires constantly.
    def broke(bars):
        if n < bars + 2:
            return None
        return last > max(c[-bars - 1:-1])
    t["brk20"], t["brk50"] = broke(20), broke(50)
    t["brk52w"] = (last >= t["high52"] * 0.999) if t["high52"] else None

    # MA structure as a countable thing, so the score and the SWOT read the
    # same fact rather than each deciding what "uptrend" means.
    above = [x for x in (
        (last > t["sma20"]) if t["sma20"] else None,
        (last > t["sma50"]) if t["sma50"] else None,
        (last > t["sma200"]) if t["sma200"] else None,
    ) if x is not None]
    t["above_mas"] = sum(1 for x in above if x) if above else None
    t["ma_stack"] = (bool(t["sma20"] > t["sma50"] > t["sma200"])
                     if None not in (t["sma20"], t["sma50"], t["sma200"]) else None)

    # Relative strength against the index over the same window, in points of
    # excess return. The benchmark series is the one downloaded in this run, so
    # both sides cover identical sessions.
    t["rs_3m"] = t["rs_1y"] = None
    if bench:
        bc = bench["c"]
        for key, bars in (("rs_3m", 63), ("rs_1y", 250)):
            mine = pct_change(c, bars) if n > bars else None
            theirs = pct_change(bc, bars) if len(bc) > bars else None
            if mine is not None and theirs is not None:
                t[key] = mine - theirs
    return t


# ─────────────────────────────────────────────────────────────────────────────
# FUNDAMENTAL SHAPE
# ─────────────────────────────────────────────────────────────────────────────

def ratios(stmts: dict | None, info: dict | None) -> dict:
    """Multi-year ratio shape for one symbol.

    Reports level, 3-year median AND direction for the capital-return ratios,
    because the level alone is the least useful of the three. ITC is the case
    that forces the median: its FY25 ROE reads 49.6% against 27–28% either
    side, an artefact of the hotels demerger shrinking equity for one year, and
    any screen ranking on latest-ROE alone puts it top of the table for a
    reason that has nothing to do with the business.
    """
    r = {
        "fy": None, "years": [],
        "roce": None, "roce_med": None, "roce_trend": None,
        "roe": None, "roe_med": None, "roe_trend": None,
        "ebit_margin": None, "ebit_margin_med": None, "ebit_margin_trend": None,
        "net_margin": None, "debt_to_equity": None, "interest_cover": None,
        "current_ratio": None, "effective_tax": None,
        "rev_cagr3": None, "ebitda_cagr3": None, "eps_cagr3": None,
        "rev_growth_latest": None, "roce_basis": None,
        "shares_changed": False, "has_statements": False, "fy_count": 0,
        "margin_one_off": None,
        "cfo_pat": None, "cfo_pat_latest": None, "fcf_pat": None,
        "fcf_margin": None, "cfo": None, "fcf": None, "capex": None,
    }

    if info:
        r["debt_to_equity"] = _finite(info.get("debt_to_equity"))
        r["pe"] = _finite(info.get("pe"))
        r["pb"] = _finite(info.get("price_to_book"))
        r["market_cap_cr"] = _finite(info.get("market_cap_cr"))
        r["sector"] = info.get("sector") or ""
        r["next_earnings"] = info.get("next_earnings")
        r["held_insiders"] = _finite(info.get("held_insiders"))
        r["held_institutions"] = _finite(info.get("held_institutions"))
        r["dividend_yield"] = _finite(info.get("dividend_yield"))
        r["business"] = info.get("business") or ""
        r["website"] = info.get("website") or ""
    else:
        r.update({"pe": None, "pb": None, "market_cap_cr": None, "sector": "",
                  "next_earnings": None, "held_insiders": None,
                  "held_institutions": None, "dividend_yield": None,
                  "business": "", "website": ""})

    if not stmts or not stmts.get("years"):
        return r

    ys = stmts["years"]                     # newest first
    r["has_statements"] = True
    r["fy_count"] = len(ys)
    r["shares_changed"] = bool(stmts.get("shares_changed"))
    r["fy"] = ys[0]["fy"]
    latest = ys[0]

    # Which ROCE basis is on show. Invested Capital matches what Indian
    # screeners print (TCS: 62% vs 55% on the subtraction basis, against ~64%
    # published), so it leads and the textbook basis is the fallback.
    for basis, key in (("invested capital", "roce_ic"), ("total assets − current liabilities", "roce")):
        if _finite(latest.get(key)) is not None:
            r["roce"] = _finite(latest[key])
            r["roce_basis"] = basis
            r["roce_med"] = _median([_finite(y.get(key)) for y in ys])
            r["roce_trend"] = _trend([_finite(y.get(key)) for y in ys])
            break

    r["roe"] = _finite(latest.get("roe"))
    r["roe_med"] = _median([_finite(y.get("roe")) for y in ys])
    r["roe_trend"] = _trend([_finite(y.get("roe")) for y in ys])

    r["ebit_margin"] = _finite(latest.get("ebit_margin"))
    r["ebit_margin_med"] = _median([_finite(y.get("ebit_margin")) for y in ys])
    r["ebit_margin_trend"] = _trend([_finite(y.get("ebit_margin")) for y in ys])

    for k in ("net_margin", "interest_cover", "current_ratio", "effective_tax"):
        r[k] = _finite(latest.get(k))

    # ── cash quality ──
    # The median across years, not the latest: one good collection year proves
    # nothing, and one bad one can be a timing artefact. A business that
    # persistently converts profit to cash shows it across the whole span.
    r["cfo_pat"] = _median([_finite(y.get("cfo_pat")) for y in ys])
    r["cfo_pat_latest"] = _finite(latest.get("cfo_pat"))
    r["fcf_pat"] = _median([_finite(y.get("fcf_pat")) for y in ys])
    r["fcf_margin"] = _finite(latest.get("fcf_margin"))
    r["cfo"] = _finite(latest.get("cfo"))
    r["fcf"] = _finite(latest.get("fcf"))
    r["capex"] = _finite(latest.get("capex"))
    # Statement leverage beats the `.info` figure when both exist: it is the
    # audited balance sheet rather than a derived field, and it is the same
    # vintage as every other number in this row.
    if _finite(latest.get("debt_to_equity")) is not None:
        r["debt_to_equity"] = _finite(latest["debt_to_equity"])

    # CAGRs over the full span the statements cover — 4 fiscal years is a
    # 3-year span, and `span` is computed rather than assumed because plenty of
    # symbols return only 2 or 3 columns.
    span = len(ys) - 1
    if span >= 1:
        oldest = ys[-1]
        r["rev_cagr3"] = cagr(_finite(oldest.get("revenue")),
                              _finite(latest.get("revenue")), span)
        r["ebitda_cagr3"] = cagr(_finite(oldest.get("ebitda")),
                                 _finite(latest.get("ebitda")), span)
        # Suppressed outright on a structural share-count change. HDFCBANK's
        # EPS halves FY23→FY24 on the HDFC Ltd merger; a CAGR across that is
        # not a slow-growth signal, it is a different number of shares.
        if not r["shares_changed"]:
            r["eps_cagr3"] = cagr(_finite(oldest.get("eps")),
                                  _finite(latest.get("eps")), span)
        r["cagr_span"] = span

    if len(ys) >= 2:
        prev = _finite(ys[1].get("revenue"))
        cur = _finite(latest.get("revenue"))
        if prev and cur:
            r["rev_growth_latest"] = cur / prev - 1.0
        # Margin discontinuity in the latest year, in percentage points. Set
        # here so both swot() and updates() read one decision rather than each
        # re-deriving it from the year table.
        m0, m1 = _finite(latest.get("ebit_margin")), _finite(ys[1].get("ebit_margin"))
        if m0 is not None and m1 is not None and abs(m0 - m1) * 100 >= ONE_OFF_MARGIN_PT:
            r["margin_one_off"] = (m0 - m1) * 100

    # Compact per-year block for the detail view. Rounded here so the payload
    # does not ship 14 decimal places 500 times over.
    r["years"] = [{
        "fy": y["fy"],
        "end": y["period_end"],
        "rev_cr": _round(_finite(y.get("revenue")), 1e7, 0),
        "ebitda_cr": _round(_finite(y.get("ebitda")), 1e7, 0),
        "ebit_cr": _round(_finite(y.get("ebit")), 1e7, 0),
        "pat_cr": _round(_finite(y.get("net_income")), 1e7, 0),
        "eps": _round(_finite(y.get("eps")), 1, 2),
        "roe": _pct(y.get("roe")),
        "roce": _pct(y.get("roce_ic") if _finite(y.get("roce_ic")) is not None else y.get("roce")),
        "ebit_margin": _pct(y.get("ebit_margin")),
        "de": _round(_finite(y.get("debt_to_equity")), 1, 2),
        "cfo_cr": _round(_finite(y.get("cfo")), 1e7, 0),
        "fcf_cr": _round(_finite(y.get("fcf")), 1e7, 0),
        "cfo_pat": _round(_finite(y.get("cfo_pat")), 1, 2),
        # Signed as reported: both arrive NEGATIVE on the cash-flow statement
        # because they are outflows. capital_allocation takes abs().
        "dividends_cr": _round(_finite(y.get("dividends")), 1e7, 0),
        "buyback_cr": _round(_finite(y.get("buyback")), 1e7, 0),
        "shares_out": _finite(y.get("shares_out")),
    } for y in ys]
    return r


def earnings_momentum(ys: list[dict]) -> dict:
    """Is the business speeding up or slowing down RIGHT NOW?

    The four-year table says what happened. It cannot say whether the latest year
    is better or worse than the trajectory that produced it, and that is usually
    the more actionable question — a 25% compounder decelerating to 8% and a 12%
    compounder accelerating to 20% look identical on a CAGR column.

    Works off the same statements already fetched, so it costs nothing. Compares
    the LATEST year-on-year growth against the growth of the years before it:

        accelerating   latest YoY meaningfully above the earlier pace
        decelerating   meaningfully below
        stable         within the dead band
        None           fewer than three years, or the numbers do not support it

    `ys` is the rounded per-year block, newest first.
    """
    out = {"label": None, "rev_yoy": None, "ebitda_yoy": None, "pat_yoy": None,
           "eps_yoy": None, "margin_delta": None, "prior_rev_yoy": None}
    if not ys or len(ys) < 3:
        return out

    def yoy(key, i):
        """Growth of year i over year i+1, as a fraction."""
        try:
            cur, prev = ys[i].get(key), ys[i + 1].get(key)
        except IndexError:
            return None
        if cur is None or prev is None or prev == 0:
            return None
        # A sign flip has no meaningful growth rate, same reason cagr() refuses.
        if prev < 0 or cur < 0:
            return None
        return cur / prev - 1.0

    out["rev_yoy"] = yoy("rev_cr", 0)
    out["ebitda_yoy"] = yoy("ebitda_cr", 0)
    out["pat_yoy"] = yoy("pat_cr", 0)
    out["eps_yoy"] = yoy("eps", 0)
    out["prior_rev_yoy"] = yoy("rev_cr", 1)

    m0, m1 = ys[0].get("ebit_margin"), ys[1].get("ebit_margin")
    if m0 is not None and m1 is not None:
        out["margin_delta"] = round(m0 - m1, 1)      # already percentage points

    # Direction from revenue first — it is the least manipulable line — with
    # EBITDA as the confirming vote. Both must exist to call it.
    latest, prior = out["rev_yoy"], out["prior_rev_yoy"]
    if latest is None or prior is None:
        return out
    gap = latest - prior
    BAND = 0.05                                   # 5pt of growth, not 5%
    votes = 0
    if gap > BAND:
        votes += 1
    elif gap < -BAND:
        votes -= 1
    eb, prior_eb = out["ebitda_yoy"], yoy("ebitda_cr", 1)
    if eb is not None and prior_eb is not None:
        if eb - prior_eb > BAND:
            votes += 1
        elif eb - prior_eb < -BAND:
            votes -= 1
    out["label"] = ("accelerating" if votes > 0 else
                    "decelerating" if votes < 0 else "stable")
    return out


def score_earnings_momentum(em: dict) -> dict:
    """Momentum of the accounts, as a component the modes can weight.

    Separate from `growth`, which measures the LEVEL of compounding. A company
    can compound at 25% and be slowing; those are different facts and the modes
    weight them differently — positional and swing care about the change,
    investor mostly about the level.
    """
    parts = {
        "revenue": _band(em.get("rev_yoy"), 0.0, 0.30),
        "ebitda": _band(em.get("ebitda_yoy"), 0.0, 0.35),
        "profit": _band(em.get("pat_yoy"), 0.0, 0.35),
        # Direction, not level. This is the part `growth` cannot express.
        "direction": (None if em.get("label") is None else
                      1.0 if em["label"] == "accelerating" else
                      0.5 if em["label"] == "stable" else 0.0),
        "margin": _band(em.get("margin_delta"), -3.0, 3.0),
    }
    v, conf = _blend(parts)
    return {"score": v, "conf": conf, "parts": {k: _pct(x) for k, x in parts.items()}}


def _trend(series: list) -> str | None:
    """'rising' / 'falling' / 'flat' / 'peaked' over a newest-first series.

    'peaked' exists because the first version of this produced a real
    contradiction on the page. Zydus ROCE runs FY23 14.5% → FY26 18.0% with a
    3-year median of 20.3%: latest-vs-oldest says RISING, so the SWOT printed
    "return on capital is improving year on year" — directly above an analyst
    view that correctly said 18.0% is below the median and capital efficiency is
    weakening. Both statements were arithmetically true and together they were
    nonsense.

    A series that is above where it started but below its own median has PEAKED,
    and that is the honest word for it. Reporting only the endpoints hides the
    shape in between, which for a capital-return ratio is the whole story.
    """
    vals = [v for v in series if v is not None]
    if len(vals) < 3:
        return None
    latest, oldest = vals[0], vals[-1]
    if abs(oldest) < 1e-9:
        return None
    move = (latest - oldest) / abs(oldest)
    med = statistics.median(vals)

    if move > 0.10:
        # Up over the span — but off its own peak? Say so instead.
        if med and latest < med * 0.95:
            return "peaked"
        return "rising"
    if move < -0.10:
        return "falling"
    # Flat endpoints can still hide a round trip.
    if med and latest < med * 0.90:
        return "peaked"
    return "flat"


def _round(v, scale=1.0, dp=2):
    if v is None:
        return None
    try:
        return round(v / scale, dp)
    except (TypeError, ValueError):
        return None


def _pct(v, dp=1):
    """Fraction → percentage points, rounded. None stays None."""
    v = _finite(v)
    return None if v is None else round(v * 100, dp)


# ─────────────────────────────────────────────────────────────────────────────
# SCORING
# ─────────────────────────────────────────────────────────────────────────────
#
# Each score is the mean of the sub-scores that COULD be computed, times 100.
# `_conf` reports how many of them there were, because a quality score built
# from one input and one built from five should not look identical on the page.

def _band(v, lo, hi):
    """Linear 0–1 between lo and hi, clamped. None in, None out."""
    v = _finite(v)
    if v is None:
        return None
    if hi == lo:
        return None
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


def _blend(parts: dict) -> tuple[float | None, float]:
    """Mean of the non-None parts, and the share of parts that were present."""
    have = [v for v in parts.values() if v is not None]
    if not have:
        return None, 0.0
    return round(100.0 * sum(have) / len(have), 1), round(len(have) / len(parts), 2)


def score_quality(r: dict) -> dict:
    """How good the business is, on published statements only.

    Leans on the 3-year median rather than the latest year wherever both
    exist — see the note in ratios() about ITC's demerger year.
    """
    de = r.get("debt_to_equity")
    parts = {
        # 15% ROCE is roughly the cost of capital for an Indian corporate; 40%
        # is genuinely exceptional. Banks land None here and are scored on the
        # remaining inputs rather than penalised for being banks.
        "roce": _band(r.get("roce_med") if r.get("roce_med") is not None else r.get("roce"), 0.10, 0.40),
        "roe": _band(r.get("roe_med") if r.get("roe_med") is not None else r.get("roe"), 0.08, 0.30),
        "margin": _band(r.get("ebit_margin_med") if r.get("ebit_margin_med") is not None
                        else r.get("ebit_margin"), 0.05, 0.30),
        # Inverted: less debt scores higher. Financials are exempt because a
        # levered balance sheet IS the business model there.
        #
        # A NEGATIVE ratio scores zero, not full marks. D/E goes negative when
        # equity does — accumulated losses have eaten the net worth — and the
        # naive inverted band read that as "less debt than debt-free" and gave
        # it 1.0. Vodafone Idea (D/E −5.38, ROE −96.6%) and GMR Airports
        # (D/E −17.45) were scoring a perfect 100 on the leverage component:
        # the three most distressed balance sheets in the universe rated best.
        # Same family as the NaN-scores-full-marks defect in
        # test_engine_regressions — a sign nobody checked, clamped the wrong way.
        "leverage": (None if _is_financial(r) else
                     None if de is None else
                     0.0 if de < 0 else
                     1.0 - _band(de, 0.0, 2.0)),
        "cover": _band(r.get("interest_cover"), 2.0, 15.0),
    }
    v, conf = _blend(parts)
    return {"score": v, "conf": conf, "parts": {k: _pct(x) for k, x in parts.items()}}


def capital_allocation(ys: list[dict], r: dict) -> dict:
    """What management DID with the money, not just what it earned.

    ROCE says how well capital is being used today. This asks the prior
    question: where did the capital go, and did the returns hold as it grew.
    A company reinvesting heavily at a rising ROCE is compounding; one
    reinvesting at a falling ROCE is destroying value while looking busy, and
    the two are indistinguishable on ROCE alone.

    Returns a 0-10 score with the notes that produced it. Scored on what the
    statements support — a company with no cash-flow statement gets None rather
    than a number built on two of six inputs.
    """
    notes, pts, possible = [], 0.0, 0.0

    def add(ok, weight, good, bad):
        nonlocal pts, possible
        possible += weight
        if ok:
            pts += weight
            notes.append({"t": good, "k": "", "good": True})
        else:
            notes.append({"t": bad, "k": "", "good": False})

    # 1. Returns held or improved as capital grew — the whole question.
    trend = r.get("roce_trend")
    if trend:
        add(trend in ("rising", "flat"), 3.0,
            f"Return on capital {trend} while the business grew",
            f"Return on capital {trend} — capital added at a worse rate than before")

    # 2. Did the profit turn into cash to allocate at all?
    cp = r.get("cfo_pat")
    if cp is not None:
        add(cp >= 0.8, 2.0,
            f"{cp:.0%} of profit converts to cash available to allocate",
            f"Only {cp:.0%} of profit converts to cash — little of it is actually allocable")

    # 3. Returned to owners, or at least not quietly diluted away.
    div = _finite((ys[0] if ys else {}).get("dividends_cr"))
    buy = _finite((ys[0] if ys else {}).get("buyback_cr"))
    if div is not None or buy is not None:
        returned = abs(div or 0) + abs(buy or 0)
        add(returned > 0, 1.5,
            "Returns cash to owners through dividends or buybacks",
            "No dividend or buyback in the latest year")

    # 4. Dilution. A rising share count without matching growth is the owner
    #    paying for the growth twice.
    counts = [y.get("shares_out") for y in ys if y.get("shares_out")]
    if len(counts) >= 2 and counts[-1]:
        drift = (counts[0] - counts[-1]) / counts[-1]
        add(drift <= 0.05, 1.5,
            "Share count broadly stable — growth was not funded by dilution",
            f"Share count up {drift:.0%} over the history — growth partly funded by dilution")

    # 5. Leverage direction.
    des = [y.get("de") for y in ys if y.get("de") is not None]
    if len(des) >= 2:
        add(des[0] <= des[-1] + 0.05, 2.0,
            "Debt flat or reducing across the history",
            f"Debt/equity rose from {des[-1]:.2f} to {des[0]:.2f}")

    if possible < 5.0:            # too few inputs to call it
        return {"score": None, "notes": notes, "inputs": round(possible, 1)}
    return {"score": round(10.0 * pts / possible, 1), "notes": notes,
            "inputs": round(possible, 1)}


def valuation_history(ys: list[dict], px: dict, pe_now: float | None) -> dict:
    """PE at each fiscal year end, so "cheap" can mean cheap for THIS company.

    The peer percentile already on the page answers "cheap against its
    industry". It cannot answer "cheap against its own record", and those
    disagree constantly — a stock can be the cheapest in an expensive sector
    and still be at the top of its own ten-year range.

    Computed from the EPS series and the daily close on each fiscal year end,
    both of which are already fetched. Returns {} when either side is missing;
    a PE history built on a guessed price is worse than none.
    """
    if not ys or not px or not px.get("c"):
        return {}
    closes, dates = px["c"], px.get("dates") or []
    if len(dates) != len(closes):
        return {}
    hist = []
    for y in ys:
        eps, end = _finite(y.get("eps")), y.get("end")
        if not eps or eps <= 0 or not end:
            continue
        # Last close on or before the fiscal year end.
        price = None
        for d, c in zip(reversed(dates), reversed(closes)):
            if d <= end:
                price = c
                break
        if price:
            hist.append({"fy": y.get("fy"), "pe": round(price / eps, 1)})
    if len(hist) < 3:
        return {}
    pes = [h["pe"] for h in hist]
    med = statistics.median(pes)
    out = {"history": hist, "median": round(med, 1),
           "low": round(min(pes), 1), "high": round(max(pes), 1)}
    if pe_now and med:
        out["vs_own_median"] = round((pe_now / med - 1) * 100, 1)
        # Percentile of its own range, 100 = cheapest it has been.
        below = sum(1 for p in pes if pe_now < p)
        out["own_pctile"] = round(100.0 * below / len(pes), 0)
    return out


def score_cashflow(r: dict) -> dict:
    """Does the reported profit actually arrive as cash?

    A separate component from quality on purpose. ROCE and margins are computed
    from the income statement and balance sheet, and a company can look excellent
    on both while collecting very little of what it books — the profit sits in
    receivables or inventory instead. Nothing in the other three scores can see
    that, which is why this is the component the investor mode weights and the
    swing mode ignores.

    1.0x conversion is the reference point, not a stretch target: a business
    converting all of its profit to operating cash is doing what it should.
    """
    parts = {
        # 0.6x is poor, 1.1x is excellent. Below 0.6 the earnings are largely
        # accounting; above ~1.1 there is usually real depreciation shielding.
        "conversion": _band(r.get("cfo_pat"), 0.6, 1.1),
        # Free cash after capex — the money actually available to owners.
        "free_cash": _band(r.get("fcf_pat"), 0.2, 0.9),
        "fcf_margin": _band(r.get("fcf_margin"), 0.0, 0.20),
    }
    v, conf = _blend(parts)
    return {"score": v, "conf": conf, "parts": {k: _pct(x) for k, x in parts.items()}}


def score_growth(r: dict) -> dict:
    """How fast it is compounding, over whatever span the statements cover."""
    parts = {
        "revenue": _band(r.get("rev_cagr3"), 0.05, 0.30),
        "ebitda": _band(r.get("ebitda_cagr3"), 0.05, 0.35),
        # None rather than 0 when suppressed, so a merger does not read as
        # no growth. _blend drops it from the denominator.
        "eps": _band(r.get("eps_cagr3"), 0.05, 0.35),
        "latest": _band(r.get("rev_growth_latest"), 0.0, 0.25),
    }
    v, conf = _blend(parts)
    return {"score": v, "conf": conf, "parts": {k: _pct(x) for k, x in parts.items()}}


def score_technical(t: dict) -> dict:
    """What the chart is doing. No fundamental input reaches this score."""
    rsi_v = t.get("rsi14")
    # Not "RSI>70 is bullish". 55–70 is the momentum zone; above 75 the stock
    # is extended and a fresh entry is worse, not better, so the curve turns
    # back down instead of continuing up.
    if rsi_v is None:
        rsi_part = None
    elif rsi_v < 50:
        rsi_part = _band(rsi_v, 30, 50) * 0.5
    elif rsi_v <= 70:
        rsi_part = 1.0
    else:
        rsi_part = max(0.0, 1.0 - (rsi_v - 70) / 15.0)

    above = t.get("above_mas")
    hist, hist_prev = t.get("hist"), t.get("hist_prev")
    parts = {
        "structure": (None if above is None else
                      (above / 3.0) * (1.0 if t.get("ma_stack") else 0.75)),
        "momentum": rsi_part,
        "macd": (None if hist is None else
                 (1.0 if hist > 0 and (hist_prev is None or hist > hist_prev)
                  else 0.6 if hist > 0 else 0.2)),
        "volume": _band(t.get("vol_spike"), 0.8, 2.0),
        "breakout": (None if t.get("brk20") is None else
                     (1.0 if t.get("brk52w") else 0.85 if t.get("brk50")
                      else 0.6 if t.get("brk20") else 0.25)),
        "rs": _band(t.get("rs_1y"), -0.10, 0.30),
    }
    v, conf = _blend(parts)
    return {"score": v, "conf": conf, "parts": {k: _pct(x) for k, x in parts.items()}}


def _is_financial(r: dict) -> bool:
    s = (r.get("sector") or "") + " " + (r.get("industry") or "")
    s = s.lower()
    return any(w in s for w in ("financial", "bank", "insurance", "real estate"))


# ─────────────────────────────────────────────────────────────────────────────
# RULE-BASED SWOT
# ─────────────────────────────────────────────────────────────────────────────
#
# Every line quotes the number that produced it. That is the whole design: a
# reader who disagrees can check the arithmetic, and nothing here is generated
# text dressed up as analysis. Where the data cannot support a claim, the claim
# is absent rather than softened.

def swot(r: dict, t: dict, val: dict) -> dict:
    S, W, O, T = [], [], [], []

    def add(bucket, text, evidence):
        bucket.append({"t": text, "k": evidence})

    # ── Strengths / weaknesses: the business, from statements ──
    roce = r.get("roce_med") if r.get("roce_med") is not None else r.get("roce")
    if roce is not None:
        basis = r.get("roce_basis") or "capital employed"
        if roce >= 0.25:
            add(S, f"Earns {roce:.0%} on capital employed — well above any plausible cost of capital",
                f"ROCE {roce:.1%} (3Y median, {basis})")
        elif roce >= 0.15:
            add(S, f"ROCE of {roce:.0%} clears the cost of capital",
                f"ROCE {roce:.1%} (3Y median, {basis})")
        elif roce < 0.10:
            add(W, f"ROCE of {roce:.0%} is at or below the cost of capital — growth here consumes value",
                f"ROCE {roce:.1%} (3Y median, {basis})")
    elif _is_financial(r):
        add(O, "ROCE is not computed for lenders — capital employed is the deposit base, "
               "so judge this one on ROE and asset quality instead",
            "no current/non-current split published")

    rt = r.get("roce_trend")
    if rt == "falling":
        add(W, "Return on capital has fallen across the statement history — "
               "the business is getting less efficient, not more",
            f"ROCE trend falling over {r.get('fy_count', 0)} years")
    elif rt == "rising":
        add(S, "Return on capital is improving year on year",
            f"ROCE trend rising over {r.get('fy_count', 0)} years")
    elif rt == "peaked":
        # Deliberately a WEAKNESS, not a strength. Higher than it started and
        # below its own median means the improvement already happened and is now
        # reversing — which is the opposite of the "improving year on year" line
        # this used to print for exactly this shape.
        add(W, "Return on capital is off its peak — higher than four years ago, "
               "but below its own multi-year median, so the improvement has "
               "started to reverse",
            f"ROCE {_pct(r.get('roce'))}% latest vs {_pct(r.get('roce_med'))}% median")

    roe = r.get("roe_med") if r.get("roe_med") is not None else r.get("roe")
    if roe is not None and roe < 0.08:
        add(W, f"ROE of {roe:.0%} is below what a fixed deposit pays",
            f"ROE {roe:.1%} (3Y median)")

    de = r.get("debt_to_equity")
    if de is not None and not _is_financial(r):
        if de < 0:
            # Not a clean balance sheet — the opposite. Equity has gone
            # negative, which is what makes the ratio negative.
            add(W, "Shareholders' equity is negative — accumulated losses exceed "
                   "the capital base, so debt-to-equity is not meaningful and the "
                   "company is technically insolvent on a book basis",
                f"D/E {de:.2f} on negative net worth")
        elif de <= 0.10:
            add(S, "Effectively debt-free", f"D/E {de:.2f}")
        elif de >= 1.5:
            add(W, f"Carries {de:.1f}x debt to equity", f"D/E {de:.2f}")
    ic = r.get("interest_cover")
    if ic is not None and ic < 2.5:
        add(T, f"Operating profit covers interest only {ic:.1f}x — "
               "a bad year puts the debt service at risk",
            f"EBIT/interest {ic:.1f}x")

    # Cash conversion, both directions. The strength here is the one that
    # separates a compounder from a company that merely reports like one.
    cp = r.get("cfo_pat")
    if cp is not None and not _is_financial(r):
        if cp >= 0.95:
            add(S, f"Converts {cp:.0%} of reported profit into operating cash",
                f"CFO/PAT {cp:.2f}x (median)")
        elif cp < 0:
            # "Only -30% arrives as cash, the rest is in receivables" is
            # nonsense — a negative ratio means operations BURNED cash, which is
            # a different statement, not a smaller version of the same one.
            add(W, "Operations consumed cash over the statement history despite "
                   "reported profits",
                f"CFO/PAT {cp:.2f}x (median)")
        elif cp < 0.7:
            add(W, f"Only {cp:.0%} of profit arrives as cash — the rest is sitting "
                   "in receivables or inventory",
                f"CFO/PAT {cp:.2f}x (median)")
    fm = r.get("fcf_margin")
    if fm is not None and fm >= 0.12 and not _is_financial(r):
        add(S, f"Generates {fm:.0%} of revenue as free cash after capex",
            f"FCF margin {fm:.1%}")

    if r.get("ebit_margin_trend") == "falling":
        add(W, "Operating margin has compressed over the statement history",
            f"EBIT margin {_pct(r.get('ebit_margin'))}% latest vs "
            f"{_pct(r.get('ebit_margin_med'))}% median")

    rc = r.get("rev_cagr3")
    if rc is not None:
        span = r.get("cagr_span", 3)
        if rc >= 0.20:
            add(S, f"Revenue compounding at {rc:.0%} a year", f"{span}Y revenue CAGR {rc:.1%}")
        elif rc < 0.05:
            add(W, f"Revenue has grown {rc:.0%} a year — barely ahead of inflation",
                f"{span}Y revenue CAGR {rc:.1%}")
    # Only claimed when the latest year does NOT contain a margin discontinuity.
    # Otherwise "operating leverage is working" is describing an acquisition.
    if (r.get("ebitda_cagr3") is not None and rc is not None
            and r["ebitda_cagr3"] > rc + 0.05 and not r.get("margin_one_off")):
        add(S, "Profit is growing faster than sales — operating leverage is working",
            f"EBITDA CAGR {r['ebitda_cagr3']:.1%} vs revenue {rc:.1%}")
    if r.get("margin_one_off"):
        add(T, "The latest year contains a margin discontinuity large enough to be an "
               "acquisition, disposal or one-off gain — the headline ratios for that "
               "year are not a run rate",
            f"EBIT margin moved {r['margin_one_off']:.0f}pt year on year")

    # ── Opportunities / threats: price, and what the data cannot tell you ──
    if val.get("pe_pctile") is not None:
        p = val["pe_pctile"]
        if p >= 70:
            add(O, f"Cheaper than {p:.0f}% of its industry peers on earnings",
                f"PE {r.get('pe'):.1f} vs {val.get('peers')} peers" if r.get("pe") else "PE percentile")
        elif p <= 25:
            add(T, f"More expensive than {100 - p:.0f}% of its industry peers — "
                   "the quality may be real and already in the price",
                f"PE {r.get('pe'):.1f} vs {val.get('peers')} peers" if r.get("pe") else "PE percentile")

    if t.get("ma_stack") and t.get("above_mas") == 3:
        add(O, "Price is above the 20, 50 and 200-day averages with the stack in order",
            f"₹{t.get('price')} vs SMA200 ₹{t['sma200']:.0f}" if t.get("sma200") else "MA structure intact")
    elif t.get("above_mas") == 0:
        add(T, "Price is below all three moving averages", "0 of 3 MAs held")

    rsi_v = t.get("rsi14")
    if rsi_v is not None and rsi_v > 75:
        add(T, f"RSI at {rsi_v:.0f} — extended, and a poor level to start a position",
            f"RSI(14) {rsi_v:.1f}")
    if t.get("from_high52") is not None and t["from_high52"] <= -0.30:
        add(T, f"Down {abs(t['from_high52']):.0%} from its 52-week high",
            f"₹{t.get('price')} vs 52w high ₹{t['high52']:.0f}")

    rs = t.get("rs_1y")
    if rs is not None:
        if rs >= 0.15:
            add(O, f"Outperforming the Nifty by {rs:.0%} over a year", f"1Y excess return {rs:+.1%}")
        elif rs <= -0.15:
            add(T, f"Lagging the Nifty by {abs(rs):.0%} over a year", f"1Y excess return {rs:+.1%}")

    if t.get("atr_pct") is not None and t["atr_pct"] >= 0.04:
        add(T, f"Moves {t['atr_pct']:.1%} a day on average — position size accordingly",
            f"ATR {t['atr_pct']:.2%} of price")
    if not t.get("liquid", True):
        add(T, "Thinly traded — under ₹1cr a day, so an exit may move the price",
            f"20d avg turnover ₹{t.get('turnover_cr', 0):.1f}cr")

    # ── Data caveats belong in the SWOT, not a footnote ──
    if r.get("shares_changed"):
        add(T, "Share count changed structurally inside the statement history, so "
               "per-share growth is not comparable across it and EPS CAGR is withheld",
            "share count moved >2% year on year")
    if not r.get("has_statements"):
        add(T, "No annual statements published for this symbol by the data source — "
               "everything above is price-only",
            "statements unavailable")
    elif r.get("fy_count", 0) < 3:
        add(T, f"Only {r.get('fy_count')} fiscal years available — the trend columns are thin",
            f"{r.get('fy_count')} statement years")

    return {"s": S, "w": W, "o": O, "t": T}


# ─────────────────────────────────────────────────────────────────────────────
# RISK, WHY NOW, WHAT CAN GO WRONG
# ─────────────────────────────────────────────────────────────────────────────
#
# Deliberately NOT another 0-100 score. A "risk score of 62" tells a reader
# nothing unless they also know which direction is better, and every extra
# arbitrary index on this page is one more number nobody can act on. Risk is
# LOW / MEDIUM / HIGH, derived by counting flags that each name a real number.
#
# The severities are about CONSEQUENCE, not probability — nothing here has been
# validated as predictive and none of it is a forecast. "high" means the flag
# would materially change what a position is worth if it matters at all.

RISK_WEIGHT = {"high": 3, "med": 2, "low": 1}
# Two high flags, or a high plus two mediums, reads HIGH. Tuned to the flag set
# rather than fitted to anything.
RISK_BANDS = ((6, "HIGH"), (3, "MEDIUM"), (0, "LOW"))


def risk_flags(r: dict, t: dict, val: dict) -> dict:
    """Named risks, each carrying the figure that raised it.

    Returns {"level": "LOW|MEDIUM|HIGH", "score": n, "flags": [...]}. `score` is
    an internal tally, published only so the banding can be checked — the LEVEL
    is what the page shows.
    """
    flags = []

    def flag(sev, text, evidence):
        flags.append({"s": sev, "t": text, "k": evidence})

    # ── balance sheet ──
    de = r.get("debt_to_equity")
    if de is not None and not _is_financial(r):
        if de < 0:
            flag("high", "Negative shareholders' equity — technically insolvent "
                         "on a book basis", f"D/E {de:.2f} on negative net worth")
        elif de >= 2.0:
            flag("high", f"Carries {de:.1f}x debt to equity", f"D/E {de:.2f}")
        elif de >= 1.0:
            flag("med", f"Leverage above 1x equity", f"D/E {de:.2f}")
    ic = r.get("interest_cover")
    if ic is not None:
        if ic < 1.5:
            flag("high", "Operating profit barely covers interest",
                 f"EBIT/interest {ic:.1f}x")
        elif ic < 3.0:
            flag("med", "Thin interest cover", f"EBIT/interest {ic:.1f}x")
    cr = r.get("current_ratio")
    if cr is not None and cr < 1.0 and not _is_financial(r):
        flag("med", "Current liabilities exceed current assets",
             f"current ratio {cr:.2f}")

    # ── the business ──
    if r.get("roce_trend") == "falling":
        flag("high", "Return on capital falling across the statement history",
             f"ROCE {_pct(r.get('roce'))}% vs {_pct(r.get('roce_med'))}% median")
    elif r.get("roce_trend") == "peaked":
        flag("med", "Return on capital off its peak",
             f"ROCE {_pct(r.get('roce'))}% vs {_pct(r.get('roce_med'))}% median")
    if r.get("ebit_margin_trend") == "falling":
        flag("med", "Operating margin compressing", f"EBIT margin {_pct(r.get('ebit_margin'))}%")
    if r.get("margin_one_off"):
        flag("med", "Latest year contains a margin discontinuity, so its headline "
                    "ratios are not a run rate",
             f"EBIT margin moved {r['margin_one_off']:.0f}pt year on year")
    rc = r.get("rev_cagr3")
    if rc is not None and rc < 0:
        flag("high", "Revenue shrinking over the statement history",
             f"{r.get('cagr_span', 3)}Y revenue CAGR {rc:.1%}")

    # ── cash quality ──
    # The flag that catches an accounting-driven earnings story. Nothing in
    # ROCE, margins or growth can see this: those are all computed from the
    # income statement, and this is whether the money arrived.
    cp = r.get("cfo_pat")
    if cp is not None and not _is_financial(r):
        if cp < 0:
            flag("high", "Operations consumed cash while the company reported a "
                         "profit", f"CFO/PAT {cp:.2f}x (median)")
        elif cp < 0.6:
            flag("high", f"Only {cp:.0%} of reported profit arrived as operating "
                         f"cash — the earnings are largely on paper",
                 f"CFO/PAT {cp:.2f}x (median across the statement history)")
        elif cp < 0.8:
            flag("med", f"Cash conversion of {cp:.0%} lags the reported profit",
                 f"CFO/PAT {cp:.2f}x (median)")
    fp = r.get("fcf_pat")
    if fp is not None and fp < 0 and not _is_financial(r):
        flag("med", "No free cash flow after capex", f"FCF/PAT {fp:.2f}x")
    if r.get("shares_changed"):
        flag("med", "Share count moved structurally, so per-share history is not "
                    "comparable", "share count moved >2% year on year")
    if not r.get("has_statements"):
        flag("high", "No annual statements published for this symbol",
             "price-only row, carries no composite")

    # ── price and valuation ──
    if val.get("pe_pctile") is not None and val["pe_pctile"] <= 15:
        flag("med", f"More expensive than {100 - val['pe_pctile']:.0f}% of its "
                    f"industry peers", f"PE percentile {val['pe_pctile']:.0f}")
    rsi_v = t.get("rsi14")
    if rsi_v is not None and rsi_v > 75:
        flag("med", f"Extended at RSI {rsi_v:.0f} — a poor level to start a position",
             f"RSI(14) {rsi_v:.1f}")
    if t.get("sma200") and t.get("price"):
        ext = t["price"] / t["sma200"] - 1
        if ext >= 0.40:
            flag("med", f"Trading {ext:.0%} above its 200-day average",
                 f"₹{t['price']} vs SMA200 ₹{t['sma200']:.0f}")
    if t.get("atr_pct") is not None and t["atr_pct"] >= 0.05:
        flag("med", f"Moves {t['atr_pct']:.1%} a day on average",
             f"ATR {t['atr_pct']:.2%} of price")
    if not t.get("liquid", True):
        flag("high", "Thinly traded — an exit may move the price",
             f"20d turnover ₹{t.get('turnover_cr', 0):.1f}cr/day")
    if t.get("from_high52") is not None and t["from_high52"] <= -0.40:
        flag("med", f"Down {abs(t['from_high52']):.0%} from its 52-week high",
             f"₹{t.get('price')} vs 52w high ₹{t['high52']:.0f}")

    score = sum(RISK_WEIGHT[f["s"]] for f in flags)
    level = next(lab for cut, lab in RISK_BANDS if score >= cut)
    order = {"high": 0, "med": 1, "low": 2}
    flags.sort(key=lambda f: order[f["s"]])
    return {"level": level, "score": score, "flags": flags}


def why_now(r: dict, t: dict, val: dict) -> list[dict]:
    """The case FOR looking at this today, each line naming its number.

    Separate from the SWOT on purpose: the SWOT describes the business over
    years, this answers "why is this on the screen this week". A high-quality
    company that has done nothing for two years has plenty of strengths and no
    why-now at all, and the page should be able to say that.
    """
    out = []

    def add(text, evidence):
        out.append({"t": text, "k": evidence})

    if t.get("brk52w"):
        add("At a 52-week high", f"₹{t.get('price')} vs 52w high ₹{t['high52']:.0f}"
            if t.get("high52") else "52-week breakout")
    elif t.get("brk50"):
        add("Broke its 50-day high", "close above the prior 50-day range")
    elif t.get("brk20"):
        add("Broke its 20-day high", "close above the prior 20-day range")

    vs = t.get("vol_spike")
    if vs and vs >= 1.5:
        add(f"Volume {vs:.1f}x its 20-day average — the move is being paid for",
            f"{vs:.2f}x avg volume")

    rs = t.get("rs_1y")
    if rs is not None and rs >= 0.15:
        add(f"Outperforming the Nifty by {rs:.0%} over a year", f"1Y excess {rs:+.1%}")

    if t.get("ma_stack") and t.get("above_mas") == 3:
        add("Above the 20, 50 and 200-day averages with the stack in order",
            "3 of 3 MAs held, 20 > 50 > 200")

    roce = r.get("roce_med") if r.get("roce_med") is not None else r.get("roce")
    if roce is not None and roce >= 0.20:
        add(f"Earns {roce:.0%} on capital employed",
            f"ROCE {roce:.1%} ({r.get('roce_basis') or 'capital employed'})")
    if rc := r.get("rev_cagr3"):
        if rc >= 0.20:
            add(f"Revenue compounding at {rc:.0%} a year",
                f"{r.get('cagr_span', 3)}Y revenue CAGR {rc:.1%}")
    # BOTH sides explicitly checked, never `or 0`. `(None or 0) > 0 + 0.05` is
    # False, but `(0.20 or 0) > (None or 0) + 0.05` is TRUE — so a company with
    # EBITDA growth and no revenue figure passed the guard and then raised
    # TypeError formatting None. That killed a 35-minute build at row ~400.
    eb, rv = r.get("ebitda_cagr3"), r.get("rev_cagr3")
    if (eb is not None and rv is not None and eb > rv + 0.05
            and not r.get("margin_one_off")):
        add("Profit growing faster than sales",
            f"EBITDA CAGR {eb:.1%} vs revenue {rv:.1%}")
    if val.get("pe_pctile") is not None and val["pe_pctile"] >= 70:
        add(f"Cheaper than {val['pe_pctile']:.0f}% of its industry peers",
            f"PE {r.get('pe')} vs {val.get('peers')} peers")
    de = r.get("debt_to_equity")
    if de is not None and 0 <= de <= 0.1 and not _is_financial(r):
        add("Effectively debt-free", f"D/E {de:.2f}")

    rsi_v = t.get("rsi14")
    if rsi_v is not None and rsi_v < 35 and (roce or 0) >= 0.15:
        add(f"Oversold at RSI {rsi_v:.0f} while still earning {roce:.0%} on capital",
            f"RSI(14) {rsi_v:.1f}, ROCE {roce:.1%}")
    return out


def price_location(t: dict) -> dict:
    """Where price sits against its own structure. NOT a target or a call.

    Deliberately zones and levels rather than "BUY ₹1,183 / TARGET ₹1,275". This
    section has no validated predictive model, so a precise entry and target
    would be fabricated precision dressed as analysis. Every number here is an
    observable level already on the chart.
    """
    price, s20, s50, s200 = (t.get("price"), t.get("sma20"),
                             t.get("sma50"), t.get("sma200"))
    if price is None:
        return {}
    atr = t.get("atr14")
    out = {"price": _round(price, 1, 2)}
    # Preferred zone: between the 20 and 50-day averages when price is above
    # both — that is the ordinary pullback area, not a prediction.
    lo = min([x for x in (s20, s50) if x] or [0]) or None
    hi = max([x for x in (s20, s50) if x] or [0]) or None
    if lo and hi and price > hi:
        out["zone_lo"], out["zone_hi"] = _round(lo, 1, 1), _round(hi, 1, 1)
    if atr and price:
        # Confirmation is one ATR above the recent high, which is a level, not a
        # forecast of reaching it.
        ref = t.get("high52") if t.get("brk52w") else price
        out["confirm"] = _round(ref + atr, 1, 1)
    if s200:
        out["invalidation"] = _round(s200, 1, 1)
        out["invalidation_basis"] = "200-day average"
    elif s50:
        out["invalidation"] = _round(s50, 1, 1)
        out["invalidation_basis"] = "50-day average"
    return out


def setup_label(t: dict, r: dict) -> dict:
    """What kind of setup this is, and over what horizon. Descriptive only."""
    tags = []
    if t.get("brk52w"):
        tags.append("52W BREAKOUT")
    elif t.get("brk50"):
        tags.append("50D BREAKOUT")
    elif t.get("brk20"):
        tags.append("20D BREAKOUT")
    if t.get("vol_spike") and t["vol_spike"] >= 1.5:
        tags.append("VOLUME")
    if t.get("rsi14") is not None and t["rsi14"] < 35:
        tags.append("OVERSOLD")
    if t.get("rs_1y") is not None and t["rs_1y"] >= 0.15:
        tags.append("RS LEADER")
    if t.get("ma_stack") and t.get("above_mas") == 3:
        tags.append("TREND INTACT")

    # Horizon is about which evidence is strong, not about a holding period
    # this module could possibly know.
    horizons = []
    if (r.get("roce") or 0) >= 0.15 and (r.get("rev_cagr3") or 0) >= 0.10:
        horizons.append("long term")
    if t.get("ma_stack") and (t.get("rs_1y") or 0) > 0:
        horizons.append("positional")
    if t.get("brk20") or (t.get("vol_spike") or 0) >= 1.5:
        horizons.append("swing")
    return {"tags": tags, "horizons": horizons}


# ─────────────────────────────────────────────────────────────────────────────
# BUILD
# ─────────────────────────────────────────────────────────────────────────────

def _valuation_pass(rows: list[dict]) -> None:
    """Score valuation as a percentile INSIDE each industry, in place.

    Peer-relative rather than absolute because an absolute PE band ranks every
    FMCG name expensive and every PSU cheap, which is a sector fact rather
    than a finding. This is only possible because the whole universe is in
    memory at once — the one thing a 500-stock build can do that a per-symbol
    lookup cannot.

    Falls back to the whole universe when an industry has too few priced
    peers, and to None when even that is unavailable. Never invented.
    """
    MIN_PEERS = 5

    def pctile(v, pool):
        """Percent of pool this value is CHEAPER than. Higher = cheaper."""
        pool = [p for p in pool if p is not None and p > 0]
        if len(pool) < MIN_PEERS or v is None or v <= 0:
            return None, len(pool)
        below = sum(1 for p in pool if v < p)
        return round(100.0 * below / len(pool), 1), len(pool)

    by_ind: dict[str, list[dict]] = {}
    for row in rows:
        by_ind.setdefault(row.get("industry") or "—", []).append(row)
    all_pe = [r["r"].get("pe") for r in rows]
    all_pb = [r["r"].get("pb") for r in rows]

    # Per-industry medians for the detail sheet. Computed here because this is
    # the only place the whole universe is in memory at once — the same reason
    # the valuation percentile lives here. A ratio without a peer benchmark
    # beside it is a number the reader cannot judge: 18% ROCE is excellent in
    # cement and mediocre in software, and the median is what says which.
    #
    # Same MIN_PEERS floor as the percentile. A "median" of two companies is
    # not a benchmark, and printing one would invite exactly the comparison it
    # cannot support.
    ind_medians: dict[str, dict] = {}
    for ind, peers in by_ind.items():
        if len(peers) < MIN_PEERS:
            continue
        med = {}
        for key, src in (("roce", "roce"), ("roe", "roe"),
                         ("ebit_margin", "ebit_margin"), ("de", "debt_to_equity"),
                         ("rev_cagr", "rev_cagr3")):
            med[key] = _pct(_median([p["r"].get(src) for p in peers])) \
                if key != "de" else _round(_median([p["r"].get(src) for p in peers]), 1, 2)
        med["pe"] = _round(_median([p["r"].get("pe") for p in peers]), 1, 1)
        med["n"] = len(peers)
        ind_medians[ind] = med

    for row in rows:
        peers = by_ind.get(row.get("industry") or "—", [])
        pe_pool = [p["r"].get("pe") for p in peers]
        pb_pool = [p["r"].get("pb") for p in peers]
        scope = "industry"
        pe_p, n = pctile(row["r"].get("pe"), pe_pool)
        if pe_p is None:
            pe_p, n = pctile(row["r"].get("pe"), all_pe)
            scope = "universe"
        pb_p, _ = pctile(row["r"].get("pb"), pb_pool)
        if pb_p is None:
            pb_p, _ = pctile(row["r"].get("pb"), all_pb)

        parts = {"pe": None if pe_p is None else pe_p / 100.0,
                 "pb": None if pb_p is None else pb_p / 100.0}
        v, conf = _blend(parts)
        row["val"] = {"score": v, "conf": conf, "pe_pctile": pe_p,
                      "pb_pctile": pb_p, "peers": n, "scope": scope}
        row["ind_med"] = ind_medians.get(row.get("industry") or "—")


def _composite(scores: dict, has_stmts: bool = True,
               weights: dict | None = None) -> float | None:
    """Declared weighted blend, renormalised over the scores that exist.

    Renormalising rather than treating a missing score as zero: a bank with no
    ROCE, or a recent listing with no 3-year CAGR, would otherwise be pushed
    down the table by the absence of data rather than by anything it did.

    BUT renormalisation has a failure mode, and the first full run walked into
    it. CHENNPETRO publishes no annual statements at all, so its quality score
    came from a single `.info` leverage field (confidence 0.20) and its growth
    score did not exist. Renormalising over what was left — that one thin
    quality number plus a strong chart — composited to 90.0 and ranked it FIRST
    of five hundred. A screen whose stated premise is published accounts had
    put a company with no published accounts at the top of it.

    So: no statements, no rank. The row stays in the screen — searchable, with
    its price history, its technicals and an explicit caveat — it simply cannot
    outrank companies that do report. `None` sorts last by construction in
    build(), and the table renders it as a dash.

    This is deliberately keyed on `has_stmts` rather than on a confidence
    floor, because confidence cannot tell the two cases apart: a bank scores
    0.20 on quality too, and for a completely legitimate reason — ROCE, EBIT
    margin and interest cover are all meaningless for a lender. A bank with
    real accounts and a real ROE keeps its rank. A company with no accounts
    does not.
    """
    if not has_stmts:
        return None
    num = den = 0.0
    for k, w in (weights or WEIGHTS).items():
        s = scores.get(k, {}).get("score")
        if s is None:
            continue
        num += w * s
        den += w
    return round(num / den, 1) if den > 0 else None


def mode_scores(scores: dict, has_stmts: bool = True) -> dict:
    """The same components ranked under each mode's question.

    Returns {"balanced": x, "investor": y, "positional": z, "swing": w}. A mode
    whose components are all missing returns None for that mode rather than a
    number built on nothing — a swing score for a company with no price history
    would be an opinion, not a measurement.
    """
    out = {"balanced": _composite(scores, has_stmts)}
    for name, w in MODES.items():
        out[name] = _composite(scores, has_stmts, weights=w)
    return out


def build(limit: int | None = None, allow_fetch: bool = True,
          news_top: int = 150, narrate_top: int = 40, ai=None,
          prev: dict | None = None) -> dict:
    """Build the whole screen. Returns the publishable payload.

    `limit` truncates the universe for a fast local run. `news_top` and
    `narrate_top` bound the two per-symbol extras — headlines are one HTTP call
    each and the narrative is one model call each, so neither runs across all
    500. Both are cached, so the covered slice is much wider than it could be
    without one.
    """
    import fundamentals as F

    t0 = time.time()
    # refresh=True: the weekly build is the right place to pull a fresh
    # constituent list, and it degrades to the cached copy if NSE refuses.
    uni = universe(refresh=allow_fetch, tiers=allow_fetch)
    if not uni:
        return {"ok": False, "error": "universe unavailable"}
    # Captured BEFORE `limit` truncates, because the universe LABEL is derived
    # from it. Reading it after meant a `limit=120` smoke run reported
    # "Total Market unavailable — fell back to Nifty 500", which was false and
    # would have been published as provenance.
    universe_size = len(uni)
    if limit:
        uni = uni[:limit]
    syms = [u["symbol"] for u in uni]
    log.info(f"screen: {len(syms)} symbols")

    # 1) Prices, batched. The benchmark rides along in the same run so both
    #    sides of every relative-strength figure cover identical sessions.
    prices = fetch_prices(syms + [BENCHMARK])
    bench = prices.get(BENCHMARK)
    if not bench:
        log.warning("screen: no benchmark series — relative strength unavailable")

    # 2) Fundamentals. Both caches are warmed sequentially; steady state is
    #    nearly free because statements carry a 30-day TTL.
    if allow_fetch:
        F.prefetch(syms)
        F.prefetch_statements(syms)

    rows = []
    for u in uni:
        sym = u["symbol"]
        px = prices.get(sym)
        if not px or len(px["c"]) < 30:
            continue                       # too little price history to say anything
        info = F.get(sym, allow_fetch=False)
        stmts = F.statements(sym, allow_fetch=False)
        r = ratios(stmts, info)
        r["industry"] = u["industry"]
        t = technicals(px, bench)
        rows.append({"u": u, "r": r, "t": t, "industry": u["industry"]})

    if not rows:
        return {"ok": False, "error": "no rows built"}

    # 3) Valuation needs the whole universe in memory, so it is a second pass.
    _valuation_pass(rows)

    # 4) Score, classify, describe.
    out = []
    for row in rows:
        r, t, u = row["r"], row["t"], row["u"]
        em = earnings_momentum(r.get("years") or [])
        scores = {
            "quality": score_quality(r),
            "growth": score_growth(r),
            "earnings_momentum": score_earnings_momentum(em),
            "cashflow": score_cashflow(r),
            "technical": score_technical(t),
            "valuation": {"score": row["val"]["score"], "conf": row["val"]["conf"],
                          "parts": {"pe": row["val"]["pe_pctile"],
                                    "pb": row["val"]["pb_pctile"]}},
        }
        # With no statements there is nothing for a fundamental score to be made
        # of — CHENNPETRO's 91.2 was one `.info` leverage field wearing the word
        # "quality". Blank them rather than publish a number built on scraps.
        if not r.get("has_statements"):
            for k in ("quality", "growth", "earnings_momentum", "cashflow"):
                scores[k] = {"score": None, "conf": 0.0, "parts": scores[k]["parts"]}
        has = bool(r.get("has_statements"))
        comp = _composite(scores, has_stmts=has)
        modes = mode_scores(scores, has_stmts=has)
        rk = risk_flags(r, t, row["val"])
        # What management DID with the capital, and how the price compares
        # with this company's OWN record rather than only its peers.
        capalloc = capital_allocation(r.get("years") or [], r)
        valhist = valuation_history(r.get("years") or [],
                                    prices.get(u["symbol"]) or {}, r.get("pe"))
        out.append({
            "sym": u["symbol"],
            "name": u["name"] or u["symbol"],
            "ind": u["industry"],
            "sector": r.get("sector") or "",
            "isin": u["isin"],
            # NSE index membership, not a rupee threshold. In an Indian context
            # "smallcap" means the Smallcap 250, and that is not always what a
            # market-cap cutoff says.
            "tier": u.get("tier") or "",
            "mcap_cr": _round(r.get("market_cap_cr"), 1, 0),
            "price": t.get("price"),
            "fy": r.get("fy"),
            # Table columns — percentage points, rounded once, here.
            "roce": _pct(r.get("roce")),
            "roce_med": _pct(r.get("roce_med")),
            "roce_basis": r.get("roce_basis"),
            "roce_trend": r.get("roce_trend"),
            "roe": _pct(r.get("roe")),
            "roe_med": _pct(r.get("roe_med")),
            "ebit_margin": _pct(r.get("ebit_margin")),
            "net_margin": _pct(r.get("net_margin")),
            "de": _round(r.get("debt_to_equity"), 1, 2),
            "icover": _round(r.get("interest_cover"), 1, 1),
            "curr": _round(r.get("current_ratio"), 1, 2),
            "tax": _pct(r.get("effective_tax")),
            "rev_cagr": _pct(r.get("rev_cagr3")),
            "ebitda_cagr": _pct(r.get("ebitda_cagr3")),
            "eps_cagr": _pct(r.get("eps_cagr3")),
            "rev_growth": _pct(r.get("rev_growth_latest")),
            "pe": _round(r.get("pe"), 1, 1),
            "pb": _round(r.get("pb"), 1, 2),
            "div_yield": _pct(r.get("dividend_yield")),
            "insiders": _pct(r.get("held_insiders")),
            "instis": _pct(r.get("held_institutions")),
            # Technicals
            "rsi": _round(t.get("rsi14"), 1, 1),
            "sma20": _round(t.get("sma20"), 1, 1),
            "sma50": _round(t.get("sma50"), 1, 1),
            "sma200": _round(t.get("sma200"), 1, 1),
            "macd_h": _round(t.get("hist"), 1, 2),
            "atr_pct": _pct(t.get("atr_pct"), 2),
            "above_mas": t.get("above_mas"),
            "stack": t.get("ma_stack"),
            "brk20": t.get("brk20"), "brk50": t.get("brk50"), "brk52w": t.get("brk52w"),
            "vol_spike": _round(t.get("vol_spike"), 1, 2),
            "turnover_cr": _round(t.get("turnover_cr"), 1, 1),
            "liquid": t.get("liquid"),
            "high52": _round(t.get("high52"), 1, 1),
            "low52": _round(t.get("low52"), 1, 1),
            "from_high": _pct(t.get("from_high52")),
            "r1w": _pct(t.get("r1w")), "r1m": _pct(t.get("r1m")),
            "r3m": _pct(t.get("r3m")), "r6m": _pct(t.get("r6m")),
            "r1y": _pct(t.get("r1y")), "r3y": _pct(t.get("r3y")),
            "r3y_cagr": _pct(t.get("r3y_cagr")),
            "rs3m": _pct(t.get("rs_3m")), "rs1y": _pct(t.get("rs_1y")),
            # Scores, always with their parts
            # Earnings momentum: the DIRECTION of the accounts, which the growth
            # score (a level) cannot express. A 25% compounder slowing to 8% and
            # a 12% compounder speeding to 20% have the same CAGR column.
            "em": scores["earnings_momentum"]["score"],
            "em_conf": scores["earnings_momentum"]["conf"],
            "em_label": em.get("label"),
            "rev_yoy": _pct(em.get("rev_yoy")),
            "ebitda_yoy": _pct(em.get("ebitda_yoy")),
            "pat_yoy": _pct(em.get("pat_yoy")),
            "eps_yoy": _pct(em.get("eps_yoy")),
            "margin_delta": em.get("margin_delta"),
            "cf": scores["cashflow"]["score"],
            "cf_conf": scores["cashflow"]["conf"],
            "cfo_pat": _round(r.get("cfo_pat"), 1, 2),
            "fcf_pat": _round(r.get("fcf_pat"), 1, 2),
            "fcf_margin": _pct(r.get("fcf_margin")),
            "cfo_cr": _round(r.get("cfo"), 1e7, 0),
            "fcf_cr": _round(r.get("fcf"), 1e7, 0),
            "q": scores["quality"]["score"], "q_conf": scores["quality"]["conf"],
            "g": scores["growth"]["score"], "g_conf": scores["growth"]["conf"],
            "v": scores["valuation"]["score"], "v_conf": scores["valuation"]["conf"],
            "tech": scores["technical"]["score"], "tech_conf": scores["technical"]["conf"],
            "comp": comp,
            # The same components under each mode's question. A stock can be an
            # 82 to an investor and a 96 to a swing trader, and that difference
            # is the most useful thing on the row.
            "m_inv": modes.get("investor"),
            "m_pos": modes.get("positional"),
            "m_swing": modes.get("swing"),
            "parts": {k: scores[k]["parts"] for k in scores},
            "pe_pctile": row["val"]["pe_pctile"],
            "val_scope": row["val"]["scope"],
            "peers": row["val"]["peers"],
            # Industry medians for the same ratios the row carries, so the
            # detail sheet can put a peer benchmark beside every number.
            "ind_med": row.get("ind_med"),
            "capalloc": capalloc.get("score"),
            "capalloc_notes": capalloc.get("notes"),
            "val_hist": valhist,
            # Narrative blocks
            "business": (r.get("business") or "")[:600],
            "website": r.get("website") or "",
            "years": r.get("years") or [],
            "swot": swot(r, t, row["val"]),
            # Why look at this today, what would go wrong, and where price sits.
            # Kept separate from the SWOT: the SWOT describes the business over
            # years, why_now answers "why is this on the screen this week".
            "why_now": why_now(r, t, row["val"]),
            "risk": rk,
            # Flat numeric alongside the nested block, purely so the table can
            # SORT on it — the browser's comparator reads scalar keys, and a
            # column that cannot be sorted is a column that gets ignored.
            "risk_lvl": {"LOW": 0, "MEDIUM": 1, "HIGH": 2}.get(rk["level"]),
            "loc": price_location(t),
            "setup": setup_label(t, r),
            "updates": updates(r, t),
            "has_stmts": r.get("has_statements"),
            "fy_count": r.get("fy_count"),
            "shares_changed": r.get("shares_changed"),
            "next_earnings": r.get("next_earnings"),
            "last_date": t.get("last_date"),
            "news": [],
        })

    out.sort(key=lambda x: (x["comp"] is None, -(x["comp"] or 0)))
    # Deltas BEFORE compaction: _compact strips nulls, and a delta needs
    # both sides present to be computed at all.
    delta_meta = attach_deltas(out, prev)
    out = [_compact(r) for r in out]

    # 5) Headlines for the top slice only.
    if allow_fetch and news_top:
        _attach_news(out[:news_top])

    # 6) Narrative for a smaller top slice. Rule-based SWOT is already on every
    #    row; this only adds prose where it can be grounded.
    if allow_fetch and narrate_top and ai:
        _attach_narrative(out[:narrate_top], ai)

    now = datetime.now(IST)
    cov = coverage(out)
    payload = {
        "ok": True,
        "built_on": now.strftime("%Y-%m-%d"),
        "built_at": now.isoformat(),
        # Named `generated_at` as well because newspaper._payload_age_days
        # reads that key to decide whether a cached payload is too old to
        # publish. One vintage field, shared by every weekly artefact here.
        "generated_at": now.isoformat(),
        # Named from what was ACTUALLY read, not from what was intended. If NSE
        # refused and the run fell back to the 500 list, the page says so.
        "universe": ("NSE Nifty Total Market" if universe_size > 600
                     else "NSE Nifty 500 (fallback — Total Market unavailable)"),
        "universe_size": universe_size,
        "count": len(out),
        "attempted": len(uni),
        "weights": WEIGHTS,
        "coverage": cov,
        "changes": delta_meta,
        # Real breadth across the screened universe, not a proxy. Dated, and
        # deliberately not an input to any score — see breadth().
        "breadth": breadth(out, bench),
        "price_date": bench.get("last_date") if bench else None,
        "build_secs": round(time.time() - t0, 1),
        "rows": out,
    }
    log.info(f"screen: built {len(out)} rows in {payload['build_secs']}s — "
             f"{cov['statements']} with statements, {cov['roce']} with ROCE")
    return payload


def breadth(rows: list[dict], bench: dict | None) -> dict:
    """Market breadth measured across the whole screened universe.

    This is the one market-wide number on the page that is not a proxy. The
    regime strip at the top of the site reads the daily move of eight
    instruments; this counts how many of five hundred actual companies are
    above their own moving averages, which is what breadth means. It is free
    here because every row already carries its MA structure.

    Deliberately NOT fed into the composite. The screen is rebuilt weekly and
    breadth turns over in days, so blending it into a score would let a
    three-week-old regime silently move this week's ranks — and a reader could
    not tell which vintage moved them. It ships as dated context instead, and
    the section prints the date beside it.
    """
    def _above(key):
        """Share of companies trading above `key`, or None on too thin a sample.

        A percentage off 12 rows is not a market reading, and the 200-day case
        is exactly where the sample thins out — every recent listing is missing
        from it.
        """
        got = [r for r in rows if r.get("price") is not None and r.get(key) is not None]
        if len(got) < 50:
            return None
        return round(100.0 * sum(1 for r in got if r["price"] > r[key]) / len(got), 1)

    a20, a50, a200 = _above("sma20"), _above("sma50"), _above("sma200")

    day = [r for r in rows if r.get("r1w") is not None]
    adv = sum(1 for r in day if r["r1w"] > 0)
    dec = sum(1 for r in day if r["r1w"] < 0)

    r1m = sorted(r["r1m"] for r in rows if r.get("r1m") is not None)
    med_1m = r1m[len(r1m) // 2] if r1m else None

    hi52 = sum(1 for r in rows if r.get("brk52w"))

    # Classification off the 50- and 200-day participation, which is the pair
    # that actually separates a broad advance from a narrow one. Thresholds are
    # stated rather than fitted — nothing here has been backtested, and a
    # fitted boundary would imply it had.
    label = None
    if a50 is not None and a200 is not None:
        both = (a50 + a200) / 2
        label = ("STRONG BULL" if both >= 75 else
                 "BULL" if both >= 60 else
                 "NEUTRAL" if both >= 45 else
                 "BEAR" if both >= 30 else "STRONG BEAR")

    return {
        "above20": a20, "above50": a50, "above200": a200,
        "advancing": adv, "declining": dec, "counted": len(day),
        "median_1m": round(med_1m, 1) if med_1m is not None else None,
        "at_52w_high": hi52,
        "label": label,
        "nifty_1m": _pct(pct_change(bench["c"], 21)) if bench and len(bench["c"]) > 21 else None,
        "nifty_1y": _pct(pct_change(bench["c"], 250)) if bench and len(bench["c"]) > 250 else None,
        "as_of": (bench or {}).get("last_date"),
    }


def coverage(rows: list[dict]) -> dict:
    """How much of the universe actually has data. Reads rows with .get().

    A function rather than four lines inline because it runs AFTER _compact(),
    where every absent value is an absent KEY — and the first version indexed
    `x["roce"]` directly, which raised KeyError on the first bank in the
    universe and killed a completed 17-minute build at the final step. Nothing
    downstream of _compact may subscript a row.
    """
    n = len(rows)
    stmts = sum(1 for x in rows if x.get("has_stmts"))
    roce = sum(1 for x in rows if x.get("roce") is not None)
    return {
        "priced": n,
        "statements": stmts,
        "roce": roce,
        "statements_pct": round(100.0 * stmts / n, 1) if n else 0,
        "roce_pct": round(100.0 * roce / n, 1) if n else 0,
    }


# Components whose movement between builds is worth recording. Kept short on
# purpose: a delta on every field would double the payload to say very little.
DELTA_KEYS = ("comp", "q", "g", "v", "tech", "em", "cf",
              "m_inv", "m_pos", "m_swing", "roce", "rev_cagr", "pe", "rsi")


def attach_deltas(rows: list[dict], prev_payload: dict | None) -> dict:
    """Movement since the previous build. Mutates `rows`, returns a summary.

    Finding stocks whose numbers are IMPROVING matters more than finding ones
    that are already high — a 91 that was a 91 last month is priced, a 78 that
    was a 61 is a change. That is what this makes visible.

    Implemented as a diff against the previous cached payload rather than a new
    history table: the payload is already stored per week in Turso, so the
    previous build is already durable and a second store would be two sources of
    truth for the same numbers.

    A symbol absent from the previous build is NEW — recorded as such rather than
    given a delta of zero, because "unchanged" and "never seen" are different
    facts and zero would hide the more interesting one.
    """
    prev_rows = (prev_payload or {}).get("rows") or []
    prev = {r.get("sym"): r for r in prev_rows if r.get("sym")}
    prev_on = (prev_payload or {}).get("built_on")
    if not prev:
        return {"compared_with": None, "new": len(rows), "moved": 0}

    # Rank position, not just score — a stock can gain 2 points and lose 40
    # places if everything else gained more.
    prev_rank = {}
    ranked = [r for r in prev_rows if r.get("comp") is not None]
    ranked.sort(key=lambda r: -r["comp"])
    for i, r in enumerate(ranked, 1):
        prev_rank[r["sym"]] = i

    now_ranked = [r for r in rows if r.get("comp") is not None]
    now_ranked.sort(key=lambda r: -(r.get("comp") or 0))
    now_rank = {r["sym"]: i for i, r in enumerate(now_ranked, 1)}

    new_count = moved = 0
    for r in rows:
        p = prev.get(r["sym"])
        if not p:
            r["is_new"] = True
            new_count += 1
            continue
        d = {}
        for k in DELTA_KEYS:
            a, b = r.get(k), p.get(k)
            if a is None or b is None:
                continue
            diff = round(a - b, 1)
            if diff:
                d[k] = diff
        if d:
            r["delta"] = d
            moved += 1
        pr, nr = prev_rank.get(r["sym"]), now_rank.get(r["sym"])
        if pr and nr and pr != nr:
            # Negative means it CLIMBED (rank 40 -> 12 is -28), which reads
            # backwards, so it is stored as places gained.
            r["rank_move"] = pr - nr
    return {"compared_with": prev_on, "new": new_count, "moved": moved}


# Fields the TABLE never reads. They exist only for the detail sheet, they are
# 74% of the payload by size, and only a reader who actually opens a company
# needs them.
#
# Measured at 750 rows: the whole payload is 4.3MB raw / 860KB gzipped, and
# years+swot+business+parts+capalloc_notes alone are 2.3MB of that. Shipping it
# as one file meant everyone who scrolled to the section downloaded the full
# research report for all 750 companies in order to read a 16-column table.
#
# Split rather than trimmed, because none of it is waste — it is just not needed
# YET. Two static files, no new serverless route (Hobby caps this project at 12
# functions and it is at 12).
DETAIL_FIELDS = (
    "years", "swot", "business", "parts", "capalloc_notes", "why_now",
    "updates", "val_hist", "ind_med", "news", "ai_view", "loc", "website",
    "roce_basis", "val_scope", "peers", "capalloc",
)


def split_payload(data: dict) -> tuple[dict, dict]:
    """(table payload, detail payload keyed by symbol).

    The table keeps every scalar it sorts, filters or renders — including
    `risk` and `setup`, which are small and drive visible columns. Everything
    else moves.
    """
    table_rows, detail = [], {}
    for r in data.get("rows") or []:
        d = {k: r[k] for k in DETAIL_FIELDS if k in r}
        if d:
            detail[r["sym"]] = d
        table_rows.append({k: v for k, v in r.items() if k not in DETAIL_FIELDS})
    table = {k: v for k, v in data.items() if k != "rows"}
    table["rows"] = table_rows
    table["has_detail"] = True
    return table, {"built_on": data.get("built_on"), "detail": detail}


def _compact(row: dict) -> dict:
    """Drop null keys and empty containers from a published row.

    Purely a transport saving — 500 rows carry a lot of legitimately missing
    data (no ROCE for banks, no 3-year return for a recent listing) and
    shipping `"roce":null` 500 times costs more than the numbers do. The
    browser must therefore treat a MISSING key exactly as it treats a null
    one, which is the same code path either way for `row.roce == null`.
    """
    out = {}
    for k, v in row.items():
        if v is None or v == "" or v == [] or v == {}:
            continue
        if isinstance(v, dict):
            inner = {ik: iv for ik, iv in v.items() if iv is not None}
            if k == "swot":
                inner = {ik: iv for ik, iv in v.items() if iv}
            if not inner:
                continue
            out[k] = inner
        else:
            out[k] = v
    return out


def info_name(r: dict, u: dict) -> str:
    """Prefer NSE's company name; it is shorter and already in the CSV."""
    return u.get("name") or r.get("name") or u["symbol"]


def updates(r: dict, t: dict) -> list[dict]:
    """Deterministic 'what changed', computed from the same numbers above.

    Not headlines — inflections. A margin that has fallen three years running
    is a more useful update than a press release about it, and unlike a
    headline it can be recomputed and checked.
    """
    out = []

    def add(text, kind="info"):
        out.append({"t": text, "k": kind})

    ys = r.get("years") or []
    if len(ys) >= 2:
        cur, prev = ys[0], ys[1]
        if cur.get("rev_cr") and prev.get("rev_cr"):
            g = cur["rev_cr"] / prev["rev_cr"] - 1.0
            base = r.get("rev_cagr3")
            if base is not None and g > base + 0.05:
                add(f"{cur['fy']} revenue growth of {g:.0%} ran ahead of its "
                    f"{r.get('cagr_span', 3)}-year {base:.0%} pace", "good")
            elif base is not None and g < base - 0.05:
                add(f"{cur['fy']} revenue growth slowed to {g:.0%} from a "
                    f"{base:.0%} multi-year pace", "bad")
        if cur.get("ebit_margin") is not None and prev.get("ebit_margin") is not None:
            d = cur["ebit_margin"] - prev["ebit_margin"]
            # A one-year margin move this large is essentially never operating
            # performance. JSW Dulux prints FY26 EBITDA of ₹2,451cr against
            # ₹668cr on revenue that FELL — the Dulux acquisition, not the paint
            # business — which reads as a 52-point margin expansion and a 96.9%
            # ROCE. Calling that "operating leverage is working" would be the
            # single most misleading line this section could publish, so a move
            # past the threshold is flagged as a probable one-off instead of
            # celebrated. The scores are already defended separately: they read
            # the multi-year median, not this year.
            if abs(d) >= ONE_OFF_MARGIN_PT:
                add(f"EBIT margin moved {abs(d):.0f} points to {cur['ebit_margin']:.1f}% "
                    f"in {cur['fy']} — a swing that size is normally an acquisition, "
                    f"disposal or one-off gain rather than trading performance. "
                    f"Read the annual report before treating it as the run rate.",
                    "warn")
            elif abs(d) >= 1.0:
                add(f"EBIT margin {'expanded' if d > 0 else 'compressed'} "
                    f"{abs(d):.1f}pt to {cur['ebit_margin']:.1f}% in {cur['fy']}",
                    "good" if d > 0 else "bad")
        if cur.get("de") is not None and prev.get("de") is not None:
            d = cur["de"] - prev["de"]
            if abs(d) >= 0.15:
                add(f"Debt/equity {'rose' if d > 0 else 'fell'} to {cur['de']:.2f} "
                    f"from {prev['de']:.2f}", "bad" if d > 0 else "good")

    if r.get("next_earnings"):
        add(f"Next earnings expected {r['next_earnings']}", "info")
    if t.get("brk52w"):
        add("Trading at a 52-week high", "good")
    elif t.get("from_high52") is not None and t["from_high52"] <= -0.25:
        add(f"{abs(t['from_high52']):.0%} below its 52-week high", "bad")
    if r.get("shares_changed"):
        add("Share count changed inside the statement history — per-share "
            "comparisons across it are not valid", "warn")
    return out


NEWS_CACHE_PATH = "cache/stock_news.json"
NEWS_TTL_HOURS = 36

_news_cache = None


def _load_news_cache() -> dict:
    global _news_cache
    if _news_cache is not None:
        return _news_cache
    try:
        with open(NEWS_CACHE_PATH) as fh:
            _news_cache = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        _news_cache = {}
    return _news_cache


def _save_news_cache() -> None:
    if _news_cache is None:
        return
    os.makedirs(os.path.dirname(NEWS_CACHE_PATH), exist_ok=True)
    tmp = NEWS_CACHE_PATH + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(_news_cache, fh)
        os.replace(tmp, NEWS_CACHE_PATH)
    except OSError as e:
        log.warning(f"screen: news cache write failed — {e}")


def _news_fresh(entry: dict) -> bool:
    ts = (entry or {}).get("_at")
    if not ts:
        return False
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return False
    return age < timedelta(hours=NEWS_TTL_HOURS)


def _attach_news(rows: list[dict]) -> None:
    """Headlines per symbol, best-effort. Silence beats a broken block.

    Cached on disk with a 36-hour TTL, which is what makes covering more than a
    handful of names affordable: this is one HTTP call PER SYMBOL, so the first
    cut of this function only ran for the top 30 by composite. With the cache,
    a re-run inside a day and a half costs nothing and the covered slice can be
    several times wider — the ranking barely reshuffles between weekly builds,
    so most of those calls would have been repeats.

    Checkpointed every 20 symbols for the same reason the statement prefetch is:
    a run killed on the workflow clock must leave what it fetched behind.
    """
    try:
        import yfinance as yf
        from symbols import to_yahoo
    except ImportError:
        return
    cache = _load_news_cache()
    fetched = 0
    for i, row in enumerate(rows, 1):
        hit = cache.get(row["sym"])
        if hit and _news_fresh(hit):
            row["news"] = hit.get("items") or []
            continue
        try:
            items = yf.Ticker(to_yahoo(row["sym"])).news or []
        except Exception:
            continue
        clean = []
        for it in items[:5]:
            c = it.get("content") or it
            title = (c.get("title") or "").strip()
            if not title:
                continue
            url = ""
            for path in (("clickThroughUrl", "url"), ("canonicalUrl", "url")):
                node = c.get(path[0]) or {}
                if isinstance(node, dict) and node.get(path[1]):
                    url = node[path[1]]
                    break
            clean.append({
                "t": title[:180],
                "u": url or it.get("link") or "",
                "p": (c.get("pubDate") or "")[:10],
                "src": ((c.get("provider") or {}).get("displayName")
                        if isinstance(c.get("provider"), dict) else "") or "",
            })
        row["news"] = clean
        cache[row["sym"]] = {"items": clean,
                             "_at": datetime.now(timezone.utc).isoformat()}
        fetched += 1
        if fetched % 20 == 0:
            _save_news_cache()
            log.info(f"screen: news {i}/{len(rows)} ({fetched} fetched)")
        time.sleep(0.25)
    _save_news_cache()
    log.info(f"screen: news done — {fetched} fetched, "
             f"{len(rows) - fetched} served from cache")


# ─────────────────────────────────────────────────────────────────────────────
# NARRATIVE — prose over the same numbers, never instead of them
# ─────────────────────────────────────────────────────────────────────────────
#
# The rule-based SWOT is the primary analyst view and stays on every row. This
# adds a paragraph on top, for the top slice only, and it is allowed to exist
# only because of the guard below.
#
# THE GUARD: every number the model emits must already appear in the facts it
# was given. A model writing about a company it half-remembers will produce a
# perfectly fluent sentence containing a market share, a promoter stake or a
# target price that is simply invented, and on this page that would be
# indistinguishable from the computed numbers beside it. So the output is
# parsed for numerals and rejected outright if any of them is new.
#
# Rejection is silent and total: the row keeps its rule-based SWOT and gets no
# prose. There is no partial repair, no "clean it up and try again" — a
# paragraph that needed editing to become true is not evidence of anything.

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")

NARRATIVE_MAX_TOKENS = 190

# Seconds between model calls. Groq's free tier caps at 12,000 tokens per
# MINUTE, and a fact sheet plus completion runs ~800 tokens, so 40 unpaced calls
# 429'd on 17 of them. Worse than the loss was the accounting: those 17 were
# counted as "rejected by the guard", which reads as the model inventing numbers
# when in fact it never answered. Pace the calls and count the two apart.
NARRATIVE_PAUSE = 4.5


def _facts_for(row: dict) -> tuple[str, set]:
    """The fact sheet handed to the model, and the set of numbers in it."""
    bits = []

    def add(label, v, suf=""):
        if v is not None:
            bits.append(f"{label}: {v}{suf}")

    add("Company", row.get("name"))
    add("Industry", row.get("ind"))
    add("Latest accounts", row.get("fy"))
    add("ROCE (3Y median)", row.get("roce_med"), "%")
    add("ROCE (latest)", row.get("roce"), "%")
    add("ROE (3Y median)", row.get("roe_med"), "%")
    add("EBIT margin", row.get("ebit_margin"), "%")
    add("Debt/equity", row.get("de"))
    add("Interest cover", row.get("icover"), "x")
    add("Revenue CAGR", row.get("rev_cagr"), "%")
    add("EBITDA CAGR", row.get("ebitda_cagr"), "%")
    add("PE", row.get("pe"))
    # Phrased as a scale rather than as "cheaper than N%", because at N=0 the
    # model wrote "cheaper than 0.0% of peers" — literally correct, unreadable.
    add("Valuation rank in its industry (100 = cheapest, 0 = most expensive)",
        row.get("pe_pctile"))
    add("1-year return", row.get("r1y"), "%")
    add("Excess return vs Nifty (1y)", row.get("rs1y"), "%")
    add("RSI(14)", row.get("rsi"))
    add("Moving averages held (of 3)", row.get("above_mas"))
    for y in (row.get("years") or [])[:4]:
        bits.append(f"{y.get('fy')}: revenue {y.get('rev_cr')}cr, "
                    f"EBITDA {y.get('ebitda_cr')}cr, ROCE {y.get('roce')}%, "
                    f"EBIT margin {y.get('ebit_margin')}%")
    sheet = "\n".join(bits)
    return sheet, set(_NUM_RE.findall(sheet))


def _attach_narrative(rows: list[dict], ai=None) -> None:
    """One grounded paragraph per row, where the guard allows it.

    `ai` is injected rather than imported, exactly as podcasts.py does it, so
    this module stays runnable standalone with no key and no network beyond the
    price and statement fetches.
    """
    if not ai:
        log.info("screen: no AI callable — narrative skipped, SWOT unaffected")
        return
    written = rejected = unavailable = 0
    for i, row in enumerate(rows):
        if not row.get("has_stmts"):
            continue                          # nothing grounded to write from
        if i:
            time.sleep(NARRATIVE_PAUSE)       # see NARRATIVE_PAUSE
        sheet, allowed = _facts_for(row)
        prompt = (
            "You are writing two sentences of neutral analyst commentary for a "
            "public stock research page. Use ONLY the figures below.\n\n"
            f"{sheet}\n\n"
            "Rules, all mandatory:\n"
            "- Use ONLY numbers that appear above. Never introduce a number, "
            "percentage, price, market share or holding that is not listed.\n"
            "- No forecast, no target, no probability, no buy/sell advice.\n"
            "- Do NOT restate a figure without saying what it MEANS. The reader "
            "is already looking at a table of these numbers, so 'ROCE is 49.4%, "
            "above its median of 35%' is useless. Say what the combination "
            "implies about the business.\n"
            "- Name the single biggest TENSION between the figures — quality "
            "against price, growth against returns, the chart against the "
            "accounts.\n"
            "- Two or three sentences, under 70 words, plain English, no bullet "
            "points, no headings, no preamble, no company name in the first "
            "three words.\n"
        )
        try:
            text = (ai(prompt, max_tokens=NARRATIVE_MAX_TOKENS) or "").strip()
        except Exception as e:
            log.warning(f"screen: narrative failed for {row['sym']} — {e}")
            continue
        # An empty reply is the model NOT ANSWERING — a rate limit, a timeout, a
        # decommissioned model. It is not the guard catching a fabrication, and
        # conflating the two made a 429 storm look like 17 hallucinations.
        if not text or len(text) < 40:
            unavailable += 1
            continue
        # THE GUARD. Any numeral not in the fact sheet means the paragraph is
        # partly invented, so the whole paragraph goes.
        invented = [n for n in _NUM_RE.findall(text) if n not in allowed]
        if invented:
            rejected += 1
            log.info(f"screen: narrative for {row['sym']} rejected — "
                     f"numbers not in the facts: {invented[:4]}")
            continue
        low = text.lower()
        if any(w in low for w in ("target", "will rise", "will fall", "buy ",
                                  "sell ", "recommend", "probability", "forecast")):
            rejected += 1
            log.info(f"screen: narrative for {row['sym']} rejected — advice language")
            continue
        row["ai_view"] = text[:600]
        written += 1
    log.info(f"screen: narrative — {written} written, {rejected} rejected by the "
             f"guard, {unavailable} no answer from the model")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    limit = int(os.environ.get("SCREEN_LIMIT") or 0) or None
    data = build(limit=limit)
    if not data.get("ok"):
        print(f"FAILED: {data.get('error')}")
        return 1
    dest = os.environ.get("SCREEN_OUT")
    if dest:
        with open(dest, "w") as fh:
            json.dump(data, fh, separators=(",", ":"))
        print(f"wrote {dest} ({os.path.getsize(dest) / 1024:.0f}KB)")
    print(json.dumps({k: v for k, v in data.items() if k != "rows"}, indent=2))
    print(f"\n{'SYM':<12}{'COMP':>6}{'Q':>6}{'G':>6}{'V':>6}{'T':>6}"
          f"{'ROCE':>7}{'REVCAGR':>9}  SETUP")

    def f(v, s=""):
        # .get() everywhere, never [] — these rows have been through _compact
        # and a missing value is a missing KEY. Subscripting here is what
        # crashed a finished build twice.
        return "—".rjust(6) if v is None else f"{v}{s}".rjust(6)

    for r in data["rows"][:10]:
        print(f"{r['sym']:<12}{f(r.get('comp'))}{f(r.get('q'))}{f(r.get('g'))}"
              f"{f(r.get('v'))}{f(r.get('tech'))}{f(r.get('roce'), '%'):>7}"
              f"{f(r.get('rev_cagr'), '%'):>9}  "
              f"{','.join((r.get('setup') or {}).get('tags', [])[:2])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
