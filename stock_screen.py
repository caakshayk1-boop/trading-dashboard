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

# ── UNIVERSE EXTENSION ────────────────────────────────────────────────────────
# The official Total Market list is 750 names and that is as wide as NSE
# publishes an index for. Going wider means COMPOSING a universe, which the
# comment above argues against — so the composition is kept strictly additive
# and strictly labelled: NSE's 750 remain the core, untouched and still sourced
# from the official list, and the extension is appended, marked `ext`, and named
# in the payload. The page must never call 1000 composed names "Nifty Total
# Market".
#
# Ranking is by MEDIAN DAILY TURNOVER over a short window, not market cap:
# turnover is what decides whether a screen row is actionable at all, it is
# computable from the price feed already being fetched, and market cap is not
# known until the expensive fundamentals pass that this selection exists to
# keep small.
#
# Staged deliberately. Set SCREEN_UNIVERSE_TARGET=750 to disable the extension
# entirely and return to the official list alone.
UNIVERSE_TARGET = int(os.environ.get("SCREEN_UNIVERSE_TARGET", "1000"))
EQUITY_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
EQUITY_LIST_CSV = "cache/nse_equity_list.csv"
# Only EQ. BE is the trade-for-trade surveillance segment and BZ is the
# restricted one — both are where a name lands when the exchange has a problem
# with it, and neither belongs in a screen that implies you could act on it.
EQUITY_LIST_SERIES = {"EQ"}
# Short window: this decides MEMBERSHIP, not any published number, so it wants
# recent liquidity rather than a long history. The real 4y history is fetched
# later, once, for the names that survive.
EXT_RANK_PERIOD = "3mo"
EXT_MIN_TURNOVER_CR = 1.0   # ₹1 crore median daily turnover, or it is untradeable
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


def _fetch_equity_list() -> list[dict] | None:
    """NSE's full listed-equity CSV as rows, or None. Never raises.

    A different host path from the index CSVs, and its header names carry
    LEADING SPACES — ' SERIES', ' ISIN NUMBER' — which is stripped here so no
    caller has to know that.
    """
    import urllib.request, io
    try:
        req = urllib.request.Request(EQUITY_LIST_URL, headers=NSE_HEADERS)
        raw = urllib.request.urlopen(req, timeout=45).read().decode("utf-8-sig")
    except Exception as e:
        log.warning(f"screen: NSE equity list unavailable — {e}")
        return None
    rows = [{(k or "").strip(): (v or "").strip() for k, v in r.items()}
            for r in csv.DictReader(io.StringIO(raw))]
    return rows or None


def extend_universe(core: list[dict], target: int = UNIVERSE_TARGET) -> list[dict]:
    """The most liquid listed names outside the index, up to `target` in total.

    Returns the EXTENSION ROWS ONLY — the caller appends them, so the official
    list stays exactly what NSE published. Degrades to [] on any failure: a
    750-name screen is a smaller screen, not a broken one, and this must never
    be the reason the daily build stops.
    """
    want = target - len(core)
    if want <= 0:
        return []
    rows = _fetch_equity_list()
    if not rows:
        log.warning("screen: extension skipped — equity list unavailable")
        return []

    have_sym = {r["symbol"] for r in core}
    have_isin = {r["isin"] for r in core if r.get("isin")}
    cand = []
    for r in rows:
        sym = (r.get("SYMBOL") or "").upper()
        isin = r.get("ISIN NUMBER") or ""
        if not sym or sym in have_sym or (isin and isin in have_isin):
            continue
        if (r.get("SERIES") or "") not in EQUITY_LIST_SERIES:
            continue
        # Same placeholder guard the index list needs.
        if sym.startswith("DUMMY") or isin.startswith("DU"):
            continue
        cand.append({"symbol": sym, "name": r.get("NAME OF COMPANY") or "",
                     "industry": "", "isin": isin, "tier": "", "ext": True})
    if not cand:
        return []

    log.info(f"screen: extension — ranking {len(cand)} candidates on "
             f"{EXT_RANK_PERIOD} turnover for {want} slots")
    px = fetch_prices([c["symbol"] for c in cand], period=EXT_RANK_PERIOD)

    ranked = []
    for c in cand:
        b = px.get(c["symbol"]) or {}
        closes, vols = b.get("c") or [], b.get("v") or []
        n = min(len(closes), len(vols))
        if n < 20:                     # too little history to judge liquidity
            continue
        turn = sorted((closes[i] * vols[i]) / 1e7      # rupees -> crore
                      for i in range(n) if closes[i] and vols[i])
        if not turn:
            continue
        med = turn[len(turn) // 2]
        # A median below this is a name you cannot get in or out of at the size
        # this desk works in, and screening it produces a row nobody can act on.
        if med < EXT_MIN_TURNOVER_CR:
            continue
        ranked.append((med, c))

    ranked.sort(key=lambda x: -x[0])
    out = [c for _, c in ranked[:want]]
    log.info(f"screen: extension — {len(out)} added; {len(ranked)} of "
             f"{len(cand)} cleared Rs{EXT_MIN_TURNOVER_CR}cr median turnover")
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
    and paced between batches. Downloaded raw; the close series is then split
    and dividend adjusted (see _adjusted) — which is what a multi-year return
    needs, so the 3Y figure is a total return — while hr/lr keep the prices
    that traded, which is what a 52-week high or low is.
    """
    try:
        import yfinance as yf
        from symbols import to_yahoo
    except ImportError:
        log.warning("screen: yfinance unavailable")
        return {}

    out: dict[str, dict] = {}
    pending: dict[str, tuple] = {}
    FETCH_REPORT.clear()
    tickers = {to_yahoo(s): s for s in symbols}
    keys = list(tickers)
    batches = [keys[i:i + PRICE_BATCH] for i in range(0, len(keys), PRICE_BATCH)]

    for bi, batch in enumerate(batches, 1):
        try:
            df = yf.download(batch, period=period, interval="1d", progress=False,
                             threads=False, auto_adjust=False, group_by="column")
        except Exception as e:
            log.warning(f"screen: price batch {bi}/{len(batches)} failed — {e}")
            continue
        if df is None or df.empty:
            log.warning(f"screen: price batch {bi}/{len(batches)} empty")
            continue

        for tk in batch:
            sym = tickers[tk]
            try:
                # FLAT COLUMNS ARE A SHAPE, NOT A BATCH SIZE.
                #
                # This used to branch on len(batch) == 1, on the belief that a
                # single-ticker download comes back flat. Current yfinance
                # returns (field, ticker) columns even for one ticker, so
                # sub["Close"] was a one-column DataFrame, .tolist() raised,
                # and the log.debug below swallowed it.
                #
                # It bit exactly once and silently: when the universe reached
                # 1,000 names, 1,000 + the benchmark made 25 full batches of
                # 40 and a 26th holding ^NSEI ALONE. The benchmark vanished,
                # and with it nifty_1m, nifty_1y, price_date and every
                # relative-strength figure on the screen — published as null
                # with nothing saying why. Branch on what the frame IS.
                if getattr(df.columns, "nlevels", 1) == 1:
                    raw = {f: df[f] for f in _FIELDS if f in df.columns}
                else:
                    raw = {f: df[(f, tk)] for f in _FIELDS if (f, tk) in df.columns}
                series = _adjusted(raw)
                if "Close" not in series or series["Close"].empty:
                    continue
                miss = _missing_close(raw, datetime.now(IST))
                if miss is not None:
                    pending[sym] = (tk, miss)
                idx = series["Close"].index

                # ── A CORPORATE ACTION YAHOO DID NOT ADJUST ────────────────
                #
                # A HALVING, NOT A BAND BREACH — the first threshold was wrong.
                #
                # I set this at 25% on the reasoning that NSE caps a session at
                # +/-20%. That is true for ordinary equities and NOT for names
                # in the F&O segment, which carry a flexible dynamic band. The
                # 25% rule then truncated real history: ADANIENT on the
                # Hindenburg day (-26.7%), INDUSINDBK on the derivatives
                # disclosure (-27.2%), IEX (-29.6%), RECLTD, STLTECH, TRENT —
                # seven names whose crashes are exactly the price action a
                # 200-day average should contain.
                #
                # A halving in one session is the honest line. No listed Indian
                # equity trades down 50% in a day; a split, a bonus or a
                # demerger does precisely that, and Yahoo's worst day on record
                # for a large cap here is -28%. The trade-off is stated rather
                # than hidden: TMPV's demerger at -40.1% now passes through
                # unflagged. Missing one action is cheaper than deleting a real
                # crash from seven histories.
                #
                # Live on 2026-09-13, 2 of 750 names carried one:
                #   INDIAGLYCO  2026-09-02  -78.8%   not in Yahoo's splits
                #   HEG         2026-09-07  -62.6%   not in Yahoo's splits
                #
                # Every trailing window spanning the break then mixes
                # pre-action and post-action prices. INDIAGLYCO published
                # SMA20 837.9, SMA50 1006.4 and SMA200 974.5 against a price
                # of 294.9 — arithmetically correct averages of two different
                # instruments — plus a 52-week high of 1219, "down 75.8% from
                # its high", an ATR of 25.6%, and a verdict and target ladder
                # built on all of it.
                #
                # The series is TRUNCATED at the break rather than re-adjusted.
                # Re-adjusting means inventing a ratio from the gap itself and
                # betting it was a clean split; truncating says the only thing
                # that is true — this instrument's usable history starts here —
                # and MIN_BARS then withholds every window there is not enough
                # data for, which is the behaviour young listings already get.
                _cl = series["Close"]
                _cut = 0
                for _i in range(1, len(_cl)):
                    _prev = float(_cl.iloc[_i - 1])
                    if _prev <= 0:
                        continue
                    _rt = float(_cl.iloc[_i]) / _prev
                    if _rt <= 0.50 or _rt >= 2.00:
                        _cut = _i                     # keep the LAST break
                if _cut:
                    log.warning("screen: %s has an unadjusted corporate action "
                                "on %s (x%.4f) — history truncated to %d bars",
                                sym, str(idx[_cut])[:10],
                                float(_cl.iloc[_cut]) / float(_cl.iloc[_cut - 1]),
                                len(_cl) - _cut)
                    series = {k: v.iloc[_cut:] for k, v in series.items()}
                    idx = series["Close"].index
                    if len(idx) < 2:
                        continue

                rec = {
                    "c": [float(x) for x in series["Close"].tolist()],
                    "h": [float(x) for x in series.get("High", series["Close"]).reindex(idx).ffill().tolist()],
                    "l": [float(x) for x in series.get("Low", series["Close"]).reindex(idx).ffill().tolist()],
                    # The prices that TRADED, for the 52-week range — see _adjusted.
                    "hr": [float(x) for x in series.get("HighRaw", series["Close"]).reindex(idx).ffill().tolist()],
                    "lr": [float(x) for x in series.get("LowRaw", series["Close"]).reindex(idx).ffill().tolist()],
                    "v": [float(x) for x in series.get("Volume", series["Close"] * 0).reindex(idx).fillna(0).tolist()],
                    # Dates alongside the closes so valuation_history can price a
                    # fiscal year end. Transient — never shipped in the payload.
                    "dates": [str(d)[:10] for d in idx],
                    "last_date": str(idx[-1])[:10],
                }
                out[sym] = rec
            except Exception as e:
                # The benchmark failing is not one name among a thousand: every
                # relative-strength figure depends on it. Say so at WARNING.
                (log.warning if tk == BENCHMARK else log.debug)(f"screen: {sym} price parse — {e}")

        log.info(f"screen: prices {bi}/{len(batches)} batches, {len(out)} symbols")
        if bi < len(batches):
            time.sleep(PRICE_PAUSE)
    rebuilt = _rebuild_closes(yf, pending, out) if pending else 0
    FETCH_REPORT.update(missing_close=len(pending), rebuilt=rebuilt)
    return out


# What the last fetch_prices could not repair, for coverage(): a screen built
# tonight from yesterday's prices must say so, not read as current.
FETCH_REPORT: dict = {}


# ── THE PRICES THAT TRADED, AND THE ONES A RETURN NEEDS ─────────────────────
#
# This downloaded with auto_adjust=True, which rescales every past Open, High,
# Low and Close by the dividends paid since. That is right for a return — a 3Y
# figure should be a total return — and wrong for a LEVEL. A 52-week high or
# low is a price somebody paid; NSE, TradingView and every broker publish the
# traded one. After a year of dividends the adjusted high sits below the real
# one by roughly the yield, so a 5%-yielder's "52w high" was ~5% too low and
# its brk52w could fire below the real high.
#
# So the download is raw, and the adjusted series is rebuilt here exactly as
# yfinance builds it (factor = Adj Close / Close, applied to O/H/L). Every
# consumer of c/h/l is unchanged; HighRaw/LowRaw carry the traded prices for
# the range. Without an Adj Close column the factor is 1 and the two agree.
_FIELDS = ("Open", "High", "Low", "Close", "Adj Close", "Volume")


def _adjusted(raw: dict) -> dict:
    close = raw.get("Close")
    if close is None:
        return {}
    adj = raw.get("Adj Close")
    f = (adj / close) if adj is not None else close * 0 + 1.0
    out = {"Close": (adj if adj is not None else close).dropna()}
    for k in ("Open", "High", "Low"):
        if k in raw:
            out[k] = (raw[k] * f).dropna()
    if "High" in raw:
        out["HighRaw"] = raw["High"].where(close.notna()).dropna()
    if "Low" in raw:
        out["LowRaw"] = raw["Low"].where(close.notna()).dropna()
    if "Volume" in raw:
        out["Volume"] = raw["Volume"].dropna()
    return out


# ── A FINISHED SESSION WITH NO CLOSE (1 Oct 2026) ───────────────────────────
#
# Yahoo served the session before the Gandhi Jayanti holiday with Open, High,
# Low and Volume and NO Close, for most of the universe. Every field is
# dropna()'d and aligned to Close, so the whole day vanished: the screen built
# at 03:35 IST on 2 Oct published price_date 2026-09-30, and ABLBL's 52-week
# low read ₹75.1 while it had traded at ₹73.41 on 1 Oct.
#
# The close is not invented. The session's last hourly bar (15:15–15:30 IST)
# closes at the session close, the same rebuild the private engine performs.
# A bar series that does not reach 15:15, or a close outside the day's own
# range, rebuilds nothing — the day stays out and the log says how many.
def _missing_close(raw: dict, now: datetime):
    """The last traded row when it has a range but no close, and its session
    is over; else None. Returns (date, open, high, low, volume)."""
    hi, lo, cl = raw.get("High"), raw.get("Low"), raw.get("Close")
    if hi is None or lo is None or cl is None:
        return None
    traded = hi.dropna()
    if traded.empty:
        return None
    ts = traded.index[-1]
    if not (cl.get(ts) != cl.get(ts)) or lo.get(ts) != lo.get(ts):    # NaN != NaN
        return None
    d = ts.date() if hasattr(ts, "date") else ts
    today = now.date()
    if d > today or (d == today and (now.hour, now.minute) < (15, 40)):
        return None
    op, vol = raw.get("Open"), raw.get("Volume")
    o = op.get(ts) if op is not None else None
    v = vol.get(ts) if vol is not None else None
    return (d, None if o != o else o, float(hi[ts]), float(lo[ts]), 0.0 if v is None or v != v else float(v))


def _session_close(frame, d):
    """The close of session d from hourly bars, or None if they stop short."""
    s = frame.dropna()
    if s.empty:
        return None
    ix = s.index
    ix = ix.tz_localize("UTC") if ix.tz is None else ix
    ix = ix.tz_convert("Asia/Kolkata")
    day = [(t, float(v)) for t, v in zip(ix, s.tolist()) if t.date() == d]
    if not day or (day[-1][0].hour, day[-1][0].minute) < (15, 15):
        return None
    return day[-1][1]


def _rebuild_closes(yf, pending: dict, out: dict) -> int:
    keys = [pending[s][0] for s in pending]
    sym_of = {pending[s][0]: s for s in pending}
    rebuilt = 0
    for i in range(0, len(keys), PRICE_BATCH):
        batch = keys[i:i + PRICE_BATCH]
        try:
            hf = yf.download(batch, period="5d", interval="1h", progress=False,
                             threads=False, auto_adjust=False, group_by="column")
        except Exception as e:
            log.warning(f"screen: hourly close rebuild batch failed — {e}")
            continue
        if hf is None or hf.empty:
            continue
        for tk in batch:
            sym = sym_of[tk]
            d, _o, hi, lo, vol = pending[sym][1]
            rec = out.get(sym)
            if rec is None or rec["last_date"] >= str(d):
                continue
            col = hf["Close"] if getattr(hf.columns, "nlevels", 1) == 1 else hf.get(("Close", tk))
            close = _session_close(col, d) if col is not None else None
            if close is None or not (lo - 1e-6 <= close <= hi + 1e-6):
                continue
            # The newest bar carries no dividend adjustment: adjusted == raw.
            for k, x in (("c", close), ("h", hi), ("l", lo), ("hr", hi), ("lr", lo), ("v", vol)):
                rec[k].append(x)
            rec["dates"].append(str(d))
            rec["last_date"] = str(d)
            rebuilt += 1
        if i + PRICE_BATCH < len(keys):
            time.sleep(PRICE_PAUSE)
    log.warning(f"screen: {len(pending)} symbols had a finished session with no close; "
                f"rebuilt {rebuilt} from hourly bars, {len(pending) - rebuilt} left a day behind")
    return rebuilt


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
        # Sampled every 21 trading days from the most recent bar backwards, so
        # the newest monthly close is always the latest price rather than
        # whatever a fixed calendar grid happened to land on.
        "rsi_m": (rsi(c[::-1][::21][::-1], 14) if n >= 15 * 21 else None),
        "atr14": atr(h, l, c, 14) if n >= MIN_BARS["atr"] else None,
    }
    t.update(macd(c) if n >= MIN_BARS["macd"] else
             {"macd": None, "signal": None, "hist": None, "hist_prev": None})

    t["atr_pct"] = (t["atr14"] / last) if t["atr14"] and last else None

    # ── ONE-YEAR STANDARD DEVIATION OF DAILY RETURNS, ANNUALISED ────────────
    #
    # This is the denominator NSE Indices actually uses for the Nifty500
    # Momentum 50 score: "price returns adjusted for volatility", where the
    # volatility is the standard deviation of daily returns over the trailing
    # year, annualised by sqrt(252).
    #
    # The momentum engine has been dividing by atr_pct instead — the only
    # volatility this screen carried. ATR is a range measure and standard
    # deviation is a dispersion-of-returns measure; they correlate but they are
    # not the same number, and a gapping stock ranks very differently under the
    # two. Computing it here costs nothing: the close series is already in
    # memory, so this is arithmetic on data already downloaded, not a second
    # source of truth for the same figure.
    if n >= 60:
        rets = [(c[i] / c[i - 1] - 1.0)
                for i in range(max(1, n - 250), n)
                if c[i - 1]]
        # Below ~60 observations an annualised sigma is a number with a
        # confidence interval wider than the number, so it is not published.
        t["sd1y"] = (statistics.pstdev(rets) * math.sqrt(252)
                     if len(rets) >= 60 else None)
    else:
        t["sd1y"] = None

    # Returns. Trading-day counts, not calendar — 250 bars is a year.
    for key, bars in (("r1d", 1), ("r1w", 5), ("r1m", 21), ("r3m", 63),
                      ("r6m", 126), ("r1y", 250), ("r3y", 750)):
        need = MIN_BARS.get(key, bars)
        t[key] = pct_change(c, bars) if n >= need else None
    t["r3y_cagr"] = cagr(c[-751] if n > 750 else None, last, 3.0) if n > 750 else None

    # ── 52-WEEK STRUCTURE, FROM HIGHS AND LOWS ──────────────────────────────
    #
    # This read max/min of the CLOSE series and published it as the 52-week
    # range. It is the highest CLOSE, which is a different number and always a
    # smaller one. Measured against Yahoo's own high series on the day this was
    # found:
    #
    #     FINCABLES    max close 1424.00   true 52w high 1498.00   -5.2%
    #     GMMPFAUDLR   max close 1319.20   true 52w high 1352.00   -2.5%
    #     INDIAGLYCO   max close 1203.30   true 52w high 1219.00   -1.3%
    #
    # It is not only a display fault. `brk52w` is derived from it and feeds
    # conviction.py, which scores it 1.0 and writes the words "at a 52-week
    # high" — so FINCABLES, sitting 4.9% BELOW its real high, was labelled as
    # being at it, and scored as a full breakout for closing at a new CLOSING
    # high. The lows are wrong the same way, in the flattering direction: the
    # published low is above the real one.
    hr, lr = px.get("hr") or h, px.get("lr") or l      # traded, not dividend-adjusted
    if n >= MIN_BARS["high52"]:
        wh = hr[-250:] if len(hr) >= len(c[-250:]) else c[-250:]
        wl = lr[-250:] if len(lr) >= len(c[-250:]) else c[-250:]
        hi, lo = max(wh), min(wl)
        t["high52"], t["low52"] = hi, lo
        t["from_high52"] = (last / hi - 1.0) if hi else None
        t["from_low52"] = (last / lo - 1.0) if lo else None
    else:
        t["high52"] = t["low52"] = t["from_high52"] = t["from_low52"] = None
        # ── A SHORT WINDOW IS STILL A MEASURED ONE ───────────────────────────
        #
        # Everything above this line is gated at 240 bars because it CLAIMS A
        # YEAR: brk52w, "at a 52-week high", the -30% drawdown flag. Those must
        # stay gated — a 52-week breakout on four months of data is a false
        # sentence.
        #
        # A HIGH IS NOT AN AVERAGE, though, and that is where the gate went too
        # far. The max of 96 bars is the true max of those 96 bars; nothing is
        # approximated and nothing is missing. Nulling it published "—" for a
        # fact the series plainly contains, on 69 of 983 names — LENSKART,
        # GROWW, EMMVEE and the rest of the recent listings, plus the demerged
        # tickers whose series restarts: VEDL, SKFINDIA, SKFINDUS, TIMEX.
        #
        # It is not only a blank cell. Those 69 carry a verdict with an entry,
        # a stop and a target while the screen cannot say where the price sits
        # in its own range — on a screen one of whose stated verdict reasons is
        # "Broke its 52-week high".
        #
        # So the range is published under its OWN keys, with the number of
        # sessions it covers, and every consumer must label it from that rather
        # than calling four months a year. The 52-week keys stay None.
        if n >= MIN_BARS["r1m"]:
            rwh = hr[-n:] if len(hr) >= n else c[-n:]
            rwl = lr[-n:] if len(lr) >= n else c[-n:]
            rhi, rlo = max(rwh), min(rwl)
            t["rng_hi"], t["rng_lo"] = rhi, rlo
            t["rng_from_hi"] = (last / rhi - 1.0) if rhi else None
            t["rng_sessions"] = n
        else:
            t["rng_hi"] = t["rng_lo"] = t["rng_from_hi"] = t["rng_sessions"] = None

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

def piotroski(ys: list[dict]) -> dict:
    """Piotroski F-score: the standard 9-point YoY financial-quality checklist.

    Strictly latest-vs-prior-year — this is what the score is defined as, not
    the 3-4yr _trend() used for margin trends elsewhere in this file. Each
    criterion earns 1 point when it is both computable AND true. A criterion
    that cannot be computed (a missing field for this symbol) is excluded
    from the denominator rather than counted as failed — `piotroski_of`
    reports how many of the 9 were actually evaluable, so a "5" built on 9/9
    reads differently from a "5" built on 6/9. Never present a bare score
    without checking `piotroski_of` first.

    Criterion 5 substitutes total-debt/total-assets for the textbook
    long-term-debt/total-assets — only total_debt is extracted from the
    balance sheet today (see fundamentals.py's _BALANCE_ROWS). Stated here
    rather than silently approximated as "the" Piotroski score.
    """
    out = {"piotroski": None, "piotroski_of": None, "piotroski_bits": None}
    if not ys or len(ys) < 2:
        return out

    cur, prev = ys[0], ys[1]
    score = 0
    computable = 0
    # Per-criterion outcome, in Piotroski's own order: "1" passed, "0" failed,
    # "X" could not be computed for this company.
    #
    # A bare "4/9" is not checkable by the reader and not checkable by us
    # either — it says a company failed five tests without saying which five,
    # and a deleveraging loss-maker reads identically to a profitable company
    # diluting its shareholders. Nine characters per row is the whole cost of
    # making the number explainable, and it rides in the detail payload rather
    # than the table one.
    bits = []

    def record(ok):
        """ok True/False adds to the denominator; None means not computable."""
        nonlocal score, computable
        if ok is None:
            bits.append("X")
            return
        computable += 1
        bits.append("1" if ok else "0")
        if ok:
            score += 1

    def yoy_up(key):
        a, b = _finite(cur.get(key)), _finite(prev.get(key))
        record(None if a is None or b is None else a > b)

    # 1. ROA > 0
    roa = _finite(cur.get("roa"))
    record(None if roa is None else roa > 0)
    # 2. CFO > 0
    cfo = _finite(cur.get("cfo"))
    record(None if cfo is None else cfo > 0)
    # 3. ROA increased YoY
    yoy_up("roa")
    # 4. CFO > net income (accrual quality — the profit is real cash, not
    # sitting in receivables/inventory)
    net_income = _finite(cur.get("net_income"))
    record(None if cfo is None or net_income is None else cfo > net_income)
    # 5. Debt ratio decreased YoY (total_debt/total_assets — see docstring)
    cur_assets, prev_assets = _finite(cur.get("total_assets")), _finite(prev.get("total_assets"))
    cur_debt, prev_debt = _finite(cur.get("total_debt")), _finite(prev.get("total_debt"))
    cur_dr = cur_debt / cur_assets if cur_debt is not None and cur_assets else None
    prev_dr = prev_debt / prev_assets if prev_debt is not None and prev_assets else None
    record(None if cur_dr is None or prev_dr is None else cur_dr < prev_dr)
    # 6. Current ratio increased YoY
    yoy_up("current_ratio")
    # 7. Shares outstanding did not increase YoY (no dilution)
    cur_shares, prev_shares = _finite(cur.get("shares_out")), _finite(prev.get("shares_out"))
    # <= not <: NOT issuing shares is the pass condition, so a flat count
    # passes. This is the only criterion of the nine that is not strict.
    record(None if cur_shares is None or prev_shares is None
           else cur_shares <= prev_shares)
    # 8. Gross margin increased YoY
    yoy_up("gross_margin")
    # 9. Asset turnover increased YoY
    yoy_up("asset_turnover")

    out["piotroski"] = score
    out["piotroski_of"] = computable
    out["piotroski_bits"] = "".join(bits)
    return out


# Criterion labels, in Piotroski's order. Lives next to the function that
# produces the bit string so the two cannot drift — a label list that has
# fallen out of step relabels every failure on the site.
PIOTROSKI_CRITERIA = (
    "Return on assets is positive",
    "Operating cash flow is positive",
    "Return on assets improved on last year",
    "Operating cash flow exceeds net profit",
    "Debt fell as a share of assets",
    "Current ratio improved",
    "No new shares issued",
    "Gross margin improved",
    "Asset turnover improved",
)


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
        "piotroski": None, "piotroski_of": None,
    }

    if info:
        r["debt_to_equity"] = _finite(info.get("debt_to_equity"))
        r["pe"] = _finite(info.get("pe"))
        r["pb"] = _finite(info.get("price_to_book"))
        # Belt and braces for entries cached before the fundamentals fix: a
        # zero market cap is an absence, not a measurement.
        _mc = _finite(info.get("market_cap_cr"))
        r["market_cap_cr"] = _mc if _mc is not None and _mc > 0 else None
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

    # The Magic Formula's raw inputs, latest fiscal year, in rupees. Carried
    # out of here only to be consumed by magic_formula(); never published raw.
    r["_mf"] = {"ebit": _finite(latest.get("ebit")), "debt": _finite(latest.get("total_debt")),
                "cash": _finite(latest.get("cash")), "end": latest.get("period_end")}

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
        pt = piotroski(ys)
        r["piotroski"] = pt["piotroski"]
        r["piotroski_of"] = pt["piotroski_of"]
        # The per-criterion breakdown. Copied explicitly like the two above —
        # this row is assembled field by field, so a key returned by
        # piotroski() but not named here never reaches the payload at all.
        r["piotroski_bits"] = pt["piotroski_bits"]

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
# THE MAGIC FORMULA (Joel Greenblatt, "The Little Book That Still Beats the
# Market")
# ─────────────────────────────────────────────────────────────────────────────
#
# Two ranks across the whole screen, added together; the lowest sum ranks
# first. A published formula with no tuned parameter, kept SEPARATE from the
# composite and from WEIGHTS: it is a second, declared way of ordering the same
# universe, not an input to any score here.
#
#   Return on capital  the screen's own ROCE (EBIT / invested capital, latest
#                      fiscal year). Greenblatt's book uses EBIT / (net working
#                      capital + net fixed assets); Indian screeners print ROCE,
#                      and this pipeline has no net-fixed-assets line. Stated.
#   Earnings yield     EBIT / enterprise value, with EV = market cap (today)
#                      + total debt − cash (latest balance sheet).
#
# THE LATEST YEAR, NOT THE MEDIAN. The screen's scores read multi-year medians
# so a one-off cannot top them; the formula is defined on current earnings, so
# it reads the latest year and a row whose margin jumped is FLAGGED (one_off),
# not quietly re-ranked. Excluded rows carry their reason; nothing missing is
# zero-filled into a rank. Nothing here predicts a return.
MF_MIN_MCAP_CR = 1000          # Greenblatt's floor was $50m; ~₹1,000 cr keeps out the noisiest microcaps
MF_MAX_STATEMENT_AGE_DAYS = 548   # ~18 months: an older balance sheet is not "latest"
MF_RULES = {
    "roc": "ROCE — EBIT ÷ invested capital, latest fiscal year (the screen's own figure)",
    "ey": "EBIT ÷ enterprise value; EV = market cap + total debt − cash",
    "combine": "rank each separately (1 = best), add the two ranks, lowest sum first; ties go to the higher earnings yield",
    "exclude": ("lenders, insurers and real estate (the screen's lender rule), utilities, market cap under "
                f"₹{MF_MIN_MCAP_CR:,} cr, no statements, statements older than 18 months, a one-off year, "
                "EBIT or EV not positive"),
    "source": "Joel Greenblatt, The Little Book That Still Beats the Market (2010)",
    "deviation": ("Return on capital uses ROCE on invested capital, not Greenblatt's net working capital + net fixed "
                  "assets; EBIT is the last fiscal year, not the trailing twelve months; a year whose EBIT margin moved "
                  "15+ points is unranked as a likely one-off; EV carries no minority-interest line, so a holding "
                  "company that consolidates a listed subsidiary can read cheaper than it is."),
}


def magic_formula(rows: list[dict], today=None) -> dict:
    """Rank `rows` in place (each gets `mf`) and return the payload summary.

    Pops the private `_mf` inputs from every row whether or not it ranks, so a
    raw EBIT or cash figure can never reach the published payload.
    """
    from collections import Counter
    from datetime import date as _date
    today = today or _date.today()
    why: Counter = Counter()
    elig = []
    for r in rows:
        src = r.pop("_mf", None) or {}
        mcap, roce = r.get("mcap_cr"), r.get("roce")
        reason = None
        if _is_financial({"sector": r.get("sector"), "industry": r.get("ind")}):
            reason = "lender, insurer or real estate"
        elif (r.get("sector") or "").strip().lower() == "utilities":
            reason = "utility"
        elif not src or src.get("ebit") is None:
            reason = "no statements"
        elif mcap is None:
            reason = "no market cap"
        elif mcap < MF_MIN_MCAP_CR:
            reason = f"market cap under ₹{MF_MIN_MCAP_CR:,} cr"
        elif roce is None:
            reason = "no ROCE"
        elif src.get("one_off"):
            # The screen's own rule: a one-off must not top the table. On the
            # first real build the top five were all this — KIRIINDUS at a
            # 190% earnings yield, ASHOKA at 90% — exceptional gains read as
            # operating earnings. Unranked with the reason, not quietly kept.
            reason = f"one-off year (EBIT margin moved {ONE_OFF_MARGIN_PT:.0f}+ points)"
        elif src.get("debt") is None or src.get("cash") is None:
            reason = "no debt or cash line in the balance sheet"
        else:
            try:
                age = (today - _date.fromisoformat(str(src.get("end"))[:10])).days
            except ValueError:
                age = None
            ebit = src["ebit"]
            ev = mcap * 1e7 + src["debt"] - src["cash"]
            if age is None or age > MF_MAX_STATEMENT_AGE_DAYS:
                reason = "statements older than 18 months"
            elif ebit <= 0:
                reason = "EBIT not positive"
            elif ev <= 0:
                reason = "EV not positive (cash exceeds market cap + debt)"
            elif roce <= 0:
                reason = "ROCE not positive"
            else:
                elig.append({"r": r, "roc": float(roce), "ey": ebit / ev * 100.0,
                             "ev_cr": ev / 1e7, "ebit_cr": ebit / 1e7, "one_off": bool(src.get("one_off"))})
        if reason:
            why[reason] += 1
            r["mf"] = {"rank": None, "why": reason}

    def comp_rank(key):
        # Competition ranking (1, 2, 2, 4): equal values share a rank, and the
        # sum is never helped by an arbitrary order among equals.
        order = sorted(elig, key=lambda e: -e[key])
        out, prev, rank = {}, None, 0
        for i, e in enumerate(order, 1):
            v = round(e[key], 6)
            if v != prev:
                rank, prev = i, v
            out[id(e)] = rank
        return out

    r_roc, r_ey = comp_rank("roc"), comp_rank("ey")
    for e in elig:
        e["score"] = r_roc[id(e)] + r_ey[id(e)]
    elig.sort(key=lambda e: (e["score"], -e["ey"], -e["roc"], e["r"].get("sym") or ""))
    n = len(elig)
    for i, e in enumerate(elig, 1):
        e["r"]["mf"] = {
            "rank": i, "of": n,
            "roc_rank": r_roc[id(e)], "ey_rank": r_ey[id(e)], "score": e["score"],
            "roc": round(e["roc"], 1), "ey": round(e["ey"], 2),
            "ev_cr": round(e["ev_cr"]), "ebit_cr": round(e["ebit_cr"]),
            **({"one_off": True} if e["one_off"] else {}),
        }
    return {
        "ranked": n, "universe": len(rows),
        "excluded": dict(sorted(why.items(), key=lambda kv: -kv[1])),
        "rules": MF_RULES,
        "min_mcap_cr": MF_MIN_MCAP_CR,
        "note": ("A ranking of current figures, not a prediction. The book reports a sustained edge in the US over 1988–2009; "
                 "it has also had multi-year stretches of trailing the market. Nothing here says what any "
                 "of these companies will do."),
    }


# ─────────────────────────────────────────────────────────────────────────────
# RULE-BASED SWOT
# ─────────────────────────────────────────────────────────────────────────────
#
# Every line quotes the number that produced it. That is the whole design: a
# reader who disagrees can check the arithmetic, and nothing here is generated
# text dressed up as analysis. Where the data cannot support a claim, the claim
# is absent rather than softened.

# ─────────────────────────────────────────────────────────────────────────────
# VETTED: A GATE IN FRONT OF THE SCREEN, AND THE CASE FOR AND AGAINST WHAT PASSES
# ─────────────────────────────────────────────────────────────────────────────
#
# Two questions, in this order, and they are different questions.
#
#   1. Is this row fit to be screened at all? (the GATE)  Nine checks, each of
#      which is pass, fail, not applicable, or UNMEASURED. A row whose price is
#      stale, whose statements are old, whose market cap Yahoo never sent, or
#      whose cash does not follow its profit should not be sorted alongside the
#      rest as though it were a peer. The gate holds it out WITH the reason.
#   2. For what clears, what does the data say for it and against it? (the CASE)
#      Taken from the screen's own SWOT and risk flags, each line carrying the
#      figure that raised it. Nothing here is written, estimated or predicted.
#
# What this is not. It is not a score, a ranking, a target or a recommendation,
# and it is an input to nothing: it does not touch WEIGHTS, the composite or the
# Magic Formula (it is computed BEFORE the Magic Formula and reads none of it),
# so a name cannot be lifted or sunk by being vetted. "Cleared" means "passed
# the checks below on the data we hold", and "held" means "see the stated reason".
#
# THE RISK GRADE IS NOT A GATE INPUT. It reads the same leverage, cover and cash
# fields the gate already tests, and it grades 48% of this universe HIGH (302
# of those for "ROCE falling"), so it separates weak businesses from unfit rows
# badly. A weak business belongs in the BEAR CASE, which prints the risk flags
# beside the strengths; it is not a reason to hold a row out of the comparison.
#
# UNMEASURED IS NOT A PASS. The first run of this gate found 24 names whose
# market cap was a published 0 (fundamentals.py turned a missing Yahoo field
# into a measured zero). RELIANCE and TCS were among them and read as microcaps.
# A check that cannot be measured on a core field holds the row; on a field that
# does not exist for the company (a lender has no interest cover) it is
# "not applicable" and is counted as neither pass nor fail.
VET_MIN_MCAP_CR = MF_MIN_MCAP_CR
VET_MAX_STATEMENT_AGE_DAYS = MF_MAX_STATEMENT_AGE_DAYS
VET_MIN_FISCAL_YEARS = 3
VET_MAX_DE = 2.0               # the band risk_flags itself calls "high"
VET_MIN_COVER = 3.0            # below this risk_flags calls cover "thin"
VET_MIN_CASH_CONVERSION = 0.8  # below this risk_flags calls conversion lagging
VET_PRICE_LAG_DAYS = 5         # a close older than this, against the screen's own price date, is stale
VET_MIN_APPLICABLE = 5         # a lender still faces five; fewer than this clears nothing
VET_CORE = frozenset({"stmts", "fresh", "px", "mcap", "liq"})
VET_CHECKS = (
    ("stmts", "Statements", "Three or more fiscal years of annual statements"),
    ("fresh", "Fresh statements", "Latest fiscal year ended within 18 months"),
    ("px", "Fresh price", f"Last close within {VET_PRICE_LAG_DAYS} days of the screen's price date"),
    ("mcap", "Market cap", f"Market cap published and at least \u20b9{VET_MIN_MCAP_CR:,} cr"),
    ("liq", "Tradable", "Passes the screen's liquidity floor"),
    ("lev", "Leverage", f"Debt to equity at least 0 and under {VET_MAX_DE:g} (not applicable to lenders)"),
    ("cover", "Interest cover", f"EBIT covers interest at least {VET_MIN_COVER:g} times (not applicable to lenders)"),
    ("cash", "Cash conversion", f"Median operating cash flow at least {VET_MIN_CASH_CONVERSION:.0%} of profit (not applicable to lenders)"),
    ("oneoff", "Run rate", f"Latest EBIT margin did not move {ONE_OFF_MARGIN_PT:.0f}+ points in a year (not applicable to lenders)"),
)
VET_NOTE = ("Cleared means a company passed these checks on the data held, and held means the stated reason. "
            "It is not a score, a ranking or a recommendation, it feeds no other number on this page, and "
            "nothing here predicts a company or its price.")
VET_CASE_MAX = 3


def _vet_statement_end(r: dict):
    from datetime import date as _d
    end = (r.get("_mf") or {}).get("end")
    if end:
        try:
            return _d.fromisoformat(str(end)[:10])
        except ValueError:
            pass
    m = re.match(r"FY(\d{2})$", r.get("fy") or "")
    return _d(2000 + int(m.group(1)), 3, 31) if m else None


def _vet_checks(r: dict, price_date, today) -> list[tuple[str, str, str]]:
    """[(code, 'pass'|'fail'|'na'|'unk', figure)] for one published row."""
    from datetime import date as _d
    lender = _is_financial({"sector": r.get("sector"), "industry": r.get("ind")})
    out = []

    def put(code, status, text):
        out.append((code, status, text))

    # statements
    n = r.get("fy_count")
    if r.get("has_stmts") is False:
        put("stmts", "fail", "no annual statements published")
    elif n is None:
        put("stmts", "unk", "number of fiscal years not published")
    elif n < VET_MIN_FISCAL_YEARS:
        put("stmts", "fail", f"{n} fiscal year{'s' if n != 1 else ''} of statements")
    else:
        put("stmts", "pass", f"{n} fiscal years")
    end = _vet_statement_end(r)
    if end is None:
        put("fresh", "unk", "statement date not published")
    elif (today - end).days > VET_MAX_STATEMENT_AGE_DAYS:
        put("fresh", "fail", f"latest statements ended {end:%b %Y}")
    else:
        put("fresh", "pass", f"{r.get('fy') or 'latest'} ended {end:%b %Y}")
    # price
    ld = r.get("last_date")
    try:
        ld = _d.fromisoformat(str(ld)[:10]) if ld else None
    except ValueError:
        ld = None
    if ld is None or price_date is None:
        put("px", "unk", "last close date not published")
    elif (price_date - ld).days > VET_PRICE_LAG_DAYS:
        put("px", "fail", f"last close {ld:%d %b}, screen price date {price_date:%d %b}")
    else:
        put("px", "pass", f"last close {ld:%d %b}")
    # market cap: absence and smallness are different findings
    mc = r.get("mcap_cr")
    if mc is None or mc <= 0:
        put("mcap", "fail", "market cap not published")
    elif mc < VET_MIN_MCAP_CR:
        put("mcap", "fail", f"market cap \u20b9{mc:,.0f} cr, under \u20b9{VET_MIN_MCAP_CR:,} cr")
    else:
        put("mcap", "pass", f"\u20b9{mc:,.0f} cr")
    lq = r.get("liquid")
    if lq is None:
        put("liq", "unk", "liquidity not measured")
    elif not lq:
        put("liq", "fail", f"20-day turnover \u20b9{(r.get('turnover_cr') or 0):.1f} cr a day")
    else:
        put("liq", "pass", f"turnover \u20b9{(r.get('turnover_cr') or 0):,.0f} cr a day")
    # balance sheet and cash: not defined for lenders
    de, ic, cp = r.get("de"), r.get("icover"), r.get("cfo_pat")
    if lender:
        for code in ("lev", "cover", "cash", "oneoff"):
            put(code, "na", "lender: this measure is not defined")
    else:
        if de is None:
            put("lev", "na", "debt to equity not reported")
        elif de < 0:
            put("lev", "fail", f"negative equity, D/E {de:.2f}")
        elif de >= VET_MAX_DE:
            put("lev", "fail", f"D/E {de:.2f}")
        else:
            put("lev", "pass", f"D/E {de:.2f}")
        if ic is None:
            put("cover", "na", "no interest expense reported")
        elif ic < VET_MIN_COVER:
            put("cover", "fail", f"EBIT covers interest {ic:.1f} times")
        else:
            put("cover", "pass", f"EBIT covers interest {ic:.1f} times")
        if cp is None:
            put("cash", "na", "cash flow not reported")
        elif cp < VET_MIN_CASH_CONVERSION:
            put("cash", "fail", f"CFO/PAT {cp:.2f}x median")
        else:
            put("cash", "pass", f"CFO/PAT {cp:.2f}x median")
        mf = r.get("_mf")
        if not mf:
            put("oneoff", "na", "no margin history")
        elif mf.get("one_off"):
            put("oneoff", "fail", f"EBIT margin moved {ONE_OFF_MARGIN_PT:.0f}+ points in the latest year")
        else:
            put("oneoff", "pass", "no margin discontinuity")
    return out


def _vet_case(r: dict) -> dict:
    """What the screen's own measurements say for the company and against it.

    Strengths come from the SWOT, in the order it states them. The bear case is
    the risk flags (high before medium) and then the SWOT weaknesses, de-duplicated.
    Every line is the screen's own sentence with its own figure. If no weakness
    cleared the screen's thresholds, that is stated as a fact about the
    thresholds and not as a clean bill of health.
    """
    sw = r.get("swot") or {}

    def prose(x: str) -> str:
        # The SWOT sentences carry a spaced em-dash; both sites have removed
        # that from their prose, so the case reads with a comma instead.
        return (x or "").replace(" \u2014 ", ", ")
    pro = [{"t": prose(i["t"]), "k": i.get("k", "")} for i in (sw.get("s") or [])][:VET_CASE_MAX]
    flags = sorted((f for f in (r.get("risk") or {}).get("flags") or []),
                   key=lambda f: {"high": 0, "med": 1}.get(f.get("s"), 2))
    con, seen = [], set()
    for t, k in ([(prose(f["t"]), f.get("k", "")) for f in flags]
                 + [(prose(i["t"]), i.get("k", "")) for i in (sw.get("w") or [])]):
        key = t.lower()[:40]
        if key in seen:
            continue
        seen.add(key)
        con.append({"t": t, "k": k})
        if len(con) == VET_CASE_MAX:
            break
    return {"for": pro, "against": con}


# ── LENSES AND THE EIGHT TESTS ────────────────────────────────────────────────
#
# Two further ways to read the SAME vetted set, from a creator's screening
# recipe: four themes (small cap, momentum, debt-free, dividend income) and an
# eight-rule quality filter. Both apply only to companies that cleared the
# gate, and both are descriptions, not scores: they add no point to any number.
#
# THE EIGHT RULES ASK FOR MORE HISTORY THAN THIS SCREEN HOLDS, and the page
# says so beside each one rather than quietly measuring something else under
# the original name:
#   10-year sales growth, 10-year average ROCE, 10-year return  ->  the screen
#       holds four fiscal years and four years of prices, so these are the
#       3-year compound rate, the median ROCE of the years held, and the
#       3-year annualised return.
#   Promoter holding  ->  not published here. Yahoo's "insiders" is a wider
#       bucket than SEBI's promoter definition (see fundamentals.py), so it is
#       used as a labelled proxy and never called promoter holding.
#   "Net profit > 10%"  ->  read as net profit MARGIN.
# A test no vetted company could be measured on is NOT APPLIED (and reported as
# such); a company that cannot be measured on an applied test has not passed it.
VET_EIGHT_MCAP_CR = 7000
VET_EIGHT_SALES = 10.0     # % a year
VET_EIGHT_ROCE = 15.0      # %
VET_EIGHT_INSIDERS = 50.0  # %
VET_EIGHT_RETURN = 17.0    # % a year
VET_EIGHT_DE = 0.5
VET_EIGHT_MARGIN = 10.0    # %
VET_EIGHT_OCF_YEARS = 3
VET_EIGHT = (
    ("mcap", "Market cap", f"over \u20b9{VET_EIGHT_MCAP_CR:,} cr", ""),
    ("sales", "Sales growth", f"3-year growth over {VET_EIGHT_SALES:g}% a year",
     "The rule asks for 10 years. The screen holds four fiscal years of statements, so this is the 3-year compound rate."),
    ("roce", "Return on capital", f"median ROCE over {VET_EIGHT_ROCE:g}%",
     "The rule asks for a 10-year average. This is the median of the fiscal years held, at most four."),
    ("insiders", "Insider holding", f"over {VET_EIGHT_INSIDERS:g}%",
     "The rule asks for promoter holding, which this screen does not have. This is Yahoo's insiders figure, a wider bucket than SEBI's promoters, so read it as a proxy."),
    ("ret3y", "Share return", f"3-year annualised price return over {VET_EIGHT_RETURN:g}%",
     "The rule asks for 10 years. The screen holds four years of prices, so this is 3 years, and a recent listing has none."),
    ("de", "Debt to equity", f"from 0 up to {VET_EIGHT_DE:g}",
     "Negative equity is insolvency, so it does not pass. Not defined for lenders."),
    ("margin", "Net profit", f"net profit margin over {VET_EIGHT_MARGIN:g}%",
     "The rule reads Net profit over 10%. It is read here as net profit margin."),
    ("ocf", "Operating cash flow", f"positive in each of the last {VET_EIGHT_OCF_YEARS} fiscal years",
     "Not defined for lenders."),
)
VET_LENSES = (
    ("small", "Small cap", "In the NSE Smallcap 250"),
    ("mom", "Momentum", "Above its 50 and 200-day averages, ahead of the Nifty over three months, RSI 55 to 75"),
    ("debt", "Debt-free", "Debt to equity from 0 to 0.1"),
    ("div", "Dividend income", "Dividend yield of 2% or more"),
)


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v else None


def _vet_lenses(r: dict) -> list[str]:
    """The lens codes a row belongs to. A missing input puts it in no lens."""
    out = []
    if r.get("tier") == "small":
        out.append("small")
    px, s50, s200, rs, rsi_ = (_num(r.get(k)) for k in ("price", "sma50", "sma200", "rs3m", "rsi"))
    if None not in (px, s50, s200, rs, rsi_) and px > s50 and px > s200 and rs > 0 and 55 <= rsi_ <= 75:
        out.append("mom")
    de = _num(r.get("de"))
    if de is not None and 0 <= de <= 0.1 and not _is_financial({"sector": r.get("sector"), "industry": r.get("ind")}):
        out.append("debt")
    dy = _num(r.get("div_yield"))
    if dy is not None and dy >= 2.0:
        out.append("div")
    return out


def _vet_eight(r: dict) -> dict[str, str]:
    """{code: 'pass'|'fail'|'unk'} for the eight rules, on one cleared row."""
    lender = _is_financial({"sector": r.get("sector"), "industry": r.get("ind")})

    def cmp(v, ok):
        v = _num(v)
        return "unk" if v is None else ("pass" if ok(v) else "fail")
    res = {
        "mcap": cmp(r.get("mcap_cr"), lambda v: v > VET_EIGHT_MCAP_CR),
        "sales": cmp(r.get("rev_cagr"), lambda v: v > VET_EIGHT_SALES),
        "roce": cmp(r.get("roce_med"), lambda v: v > VET_EIGHT_ROCE),
        "insiders": cmp(r.get("insiders"), lambda v: v > VET_EIGHT_INSIDERS),
        "ret3y": cmp(r.get("r3y_cagr"), lambda v: v > VET_EIGHT_RETURN),
        "de": "unk" if lender else cmp(r.get("de"), lambda v: 0 <= v < VET_EIGHT_DE),
        "margin": cmp(r.get("net_margin"), lambda v: v > VET_EIGHT_MARGIN),
    }
    cfo = [_num((y or {}).get("cfo")) for y in (r.get("years") or [])[:VET_EIGHT_OCF_YEARS]]
    if lender or len(cfo) < VET_EIGHT_OCF_YEARS or None in cfo:
        res["ocf"] = "unk"
    else:
        res["ocf"] = "pass" if all(c > 0 for c in cfo) else "fail"
    return res


def vet(rows: list[dict], price_date=None, today=None) -> dict:
    """Attach `vet` to every row and return the payload's summary.

    MUST run before magic_formula(): it reads `_mf` (which that function pops)
    for the one-off-year finding, and it must not read `mf`, so the Magic
    Formula stays an input to nothing.
    """
    from collections import Counter
    from datetime import date as _d
    today = today or _d.today()
    if price_date is None:
        dates = []
        for r in rows:
            try:
                dates.append(_d.fromisoformat(str(r.get("last_date"))[:10]))
            except (TypeError, ValueError):
                pass
        price_date = max(dates) if dates else None
    elif isinstance(price_date, str):
        price_date = _d.fromisoformat(price_date[:10])
    by_check: Counter = Counter()
    n_ok = n_held = 0
    pending = []
    for r in rows:
        res = _vet_checks(r, price_date, today)
        failed = [c for c, st, _ in res if st == "fail"]
        unmeasured = [c for c, st, _ in res if st == "unk"]
        applicable = [x for x in res if x[1] != "na"]
        p = sum(1 for _, st, _ in res if st == "pass")
        held_by = failed + [c for c in unmeasured if c in VET_CORE]
        cleared = (not failed and not [c for c in unmeasured if c in VET_CORE]
                   and len(applicable) >= VET_MIN_APPLICABLE)
        v = {"s": "cleared" if cleared else "held", "p": p, "n": len(applicable)}
        if held_by:
            v["f"] = held_by
            first = next(x for x in res if x[0] == held_by[0])
            v["w"] = first[2] if first[1] == "fail" else f"unmeasured: {first[2]}"
        elif not cleared:
            v["f"] = []
            v["w"] = f"only {len(applicable)} checks apply"
        for c in held_by:
            by_check[c] += 1
        if cleared:
            n_ok += 1
            v["c"] = _vet_case(r)
            lens = _vet_lenses(r)
            if lens:
                v["l"] = lens
            pending.append((r, v, _vet_eight(r)))
        else:
            n_held += 1
        r["vet"] = v
    # A test is APPLIED only if at least one vetted company could be measured on
    # it. One nobody could be measured on is reported as not applied; it does
    # not fail everybody.
    codes = [c for c, *_ in VET_EIGHT]
    applied = {c for c in codes if any(res[c] != "unk" for _, _, res in pending)}
    tally = {c: Counter(res[c] for _, _, res in pending) for c in codes}
    passed_all = 0
    for r, v, res in pending:
        a = [c for c in codes if c in applied]
        fails = [c for c in a if res[c] == "fail"]
        unk = [c for c in a if res[c] == "unk"]
        q = {"p": sum(1 for c in a if res[c] == "pass"), "a": bool(a) and not fails and not unk}
        if fails:
            q["f"] = fails
        if unk:
            q["u"] = unk
        v["q"] = q
        passed_all += q["a"]
    lens_n = Counter(lc for _, v, _ in pending for lc in v.get("l", []))
    return {
        "cleared": n_ok,
        "held": n_held,
        "total": len(rows),
        "min_applicable": VET_MIN_APPLICABLE,
        "by_check": dict(by_check),
        "lenses": [{"code": c, "label": lab, "rule": rule, "n": lens_n.get(c, 0)} for c, lab, rule in VET_LENSES],
        "eight": {
            "applied": len(applied),
            "passed_all": passed_all,
            "tests": [{"code": c, "label": lab, "rule": rule, "note": note, "applied": c in applied,
                       "pass": tally[c].get("pass", 0), "fail": tally[c].get("fail", 0), "unk": tally[c].get("unk", 0)}
                      for c, lab, rule, note in VET_EIGHT],
        },
        "rules": [{"code": c, "label": lab, "rule": rule, "core": c in VET_CORE} for c, lab, rule in VET_CHECKS],
        "note": VET_NOTE,
    }


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


import verdict as _verdict  # noqa: E402  (verdict layer, see verdict.py)
import targets as _targets  # noqa: E402  (resistance ladder, see targets.py)

try:                       # signals/indicators.py owns this number
    from signals.indicators import ATR_STOP_MULT as ATR_STOP_MULT_FOR_SCREEN
except Exception:
    ATR_STOP_MULT_FOR_SCREEN = 1.41

# Mirrors _tight_sl's own bounds. A screen row is published as a tradeable
# plan on 750 names, so it is held to the same limits as an engine signal.
SCREEN_SL_MAX_PCT = 0.06
SCREEN_SL_MIN_PCT = 0.015


def _ladder_for(t: dict, r: dict, px: dict) -> dict | None:
    """Target ladder for one screen row, or None when the inputs cannot support
    one. Missing ATR means no stop can be sized, and a ladder without a stop is
    three numbers with no risk attached."""
    price = t.get("price")          # technicals() names it `price`, not `last`
    atr = t.get("atr14")
    if not price or not atr or atr <= 0:
        return None

    # ── AND THE STOP IS CAPPED, WHICH IT WAS NOT ────────────────────────────
    #
    # This was a raw `price - mult * ATR` with no percentage bound, while the
    # house helper _tight_sl has capped every engine at 6% since it was
    # written. On a name that has just collapsed, ATR is enormous and the
    # result is not a swing stop: INDIAGLYCO published a plan risking 36.35%
    # of entry, HEG 28.2%, and 62 of 750 rows were outside the cap.
    #
    # The R-multiple then launders it. With risk at 112.82 a target of ₹583
    # against a ₹310 entry reads "2.42R" — a disciplined-looking number for an
    # 88% move. A ratio is only as honest as its denominator.
    #
    # Bounded here rather than by calling _tight_sl, which takes a pandas
    # low series and would need the frame this function does not hold. The
    # constants are the house ones and are named so they cannot drift quietly.
    stop_raw = price - ATR_STOP_MULT_FOR_SCREEN * atr
    # max(), not min(): for a long the WIDER stop is the LOWER number, so
    # capping the risk means refusing to go below price x (1 - max_pct).
    stop = max(stop_raw, price * (1 - SCREEN_SL_MAX_PCT))
    stop = min(stop, price * (1 - SCREEN_SL_MIN_PCT))

    # Anchors the reader already knows about, so a swing high sitting on the
    # 52-week high reads as one strong level rather than two weak ones.
    extra = []
    hi52 = t.get("high52")
    if hi52:
        extra.append(("52-week high", hi52))
    for label, key in (("200-day average", "sma200"), ("50-day average", "sma50")):
        v = t.get(key)
        if v and v > price:
            extra.append((label, v))

    # Strong fundamentals turn on a TRAIL, they do not stretch the targets.
    # The ledger's 90th-percentile excursion is 2.28R; a bigger fixed number is
    # a number that does not get hit.
    roce, cagr, de = r.get("roce"), r.get("rev_cagr3"), r.get("debt_to_equity")
    strong = ((roce or 0) >= 0.18 and (cagr or 0) >= 0.12
              and (de is None or de <= 1.0))
    why = (f"ROCE {roce:.0%}, revenue CAGR {cagr:.0%}"
           if strong and roce and cagr else "quality gate passed")

    return _targets.build_ladder(price, stop, atr, px.get("h"), extra=extra,
                                 quality={"strong": strong, "why": why})


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

    # AN UNKNOWN INDUSTRY IS NOT AN INDUSTRY. The universe extension is built
    # from NSE's equity list, which carries no industry column, so those rows
    # arrive with it blank. Bucketing them under one "—" key would hand 250
    # unrelated companies to each other as "industry peers", clear MIN_PEERS
    # comfortably, publish a median for a bucket that is not a sector, and
    # label the scope "industry" — a percentile that reads like a finding and
    # means nothing. Excluded here instead, so they find no peer pool and fall
    # through to the universe scope below, which the payload names honestly.
    # Yahoo's sector/industry is deliberately NOT used to fill the gap: it is a
    # different taxonomy from NSE's, and mixing the two invents peer groups.
    by_ind: dict[str, list[dict]] = {}
    for row in rows:
        ind = (row.get("industry") or "").strip()
        if not ind:
            continue
        by_ind.setdefault(ind, []).append(row)
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
        peers = by_ind.get((row.get("industry") or "").strip(), [])
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
    # Widen past the index, most-liquid-first, when a target above the official
    # list is set. Appended AFTER the official list so the core is byte-for-byte
    # what NSE published, and only on a fetching run — a `limit` smoke run has
    # no business spending the extension's price budget.
    core_size = len(uni)
    ext = extend_universe(uni) if (allow_fetch and not limit) else []
    if ext:
        uni = uni + ext
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
        # px carries the OHLC series. It is needed at row assembly to find the
        # swing highs a target should sit on — see targets.py. Transient: the
        # series itself is never published, only the levels derived from it.
        rows.append({"u": u, "r": r, "t": t, "px": px, "industry": u["industry"]})

    if not rows:
        return {"ok": False, "error": "no rows built"}

    # 3) Valuation needs the whole universe in memory, so it is a second pass.
    _valuation_pass(rows)

    # 4) Score, classify, describe.
    out = []
    # Distinct target-basis strings, shipped once at file level and referenced
    # by index on each row — see targets.compact().
    _basis_legend: list[str] = []
    for row in rows:
        r, t, u = row["r"], row["t"], row["u"]
        px_row = row.get("px") or {}
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
            # Popped by magic_formula() before anything is published.
            "_mf": dict(r["_mf"], one_off=r.get("margin_one_off") is not None) if r.get("_mf") else None,
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
            # ── ALREADY A PERCENTAGE, AND MULTIPLIED BY 100 ANYWAY ──────────
            #
            # _pct() converts a FRACTION to percentage points. yfinance's
            # `dividendYield` stopped being a fraction and now arrives in
            # percentage points, so this multiplied a number that was already
            # right. Measured across the 592 rows carrying the field, the
            # published column reads:
            #
            #     median 65.5    p90 268    max 1834
            #     ITC 601   COALINDIA 503   ONGC 611   VEDL 1256
            #
            # None of which is a dividend yield. Divided by 100 the same
            # distribution is median 0.66%, p90 2.68%, max 18.3%, which is what
            # the NSE actually looks like.
            #
            # Nothing else in this payload consumed it, which is why a column
            # claiming ITC pays 601% survived — it was written and never read.
            "div_yield": _round(r.get("dividend_yield"), 1, 2),
            "insiders": _pct(r.get("held_insiders")),
            "instis": _pct(r.get("held_institutions")),
            # Piotroski F-score, 0-9. Check piotroski_of before trusting the
            # score — a "5" computed from 9/9 criteria and one computed from
            # 6/9 are not the same claim.
            "piotroski": r.get("piotroski"),
            "piotroski_of": r.get("piotroski_of"),
            # Nine characters — 1 passed, 0 failed, X not computable — routed
            # to the detail payload by DETAIL_FIELDS. Without it the published
            # score is a number the reader cannot check.
            "piotroski_bits": r.get("piotroski_bits"),
            # Technicals
            "rsi": _round(t.get("rsi14"), 1, 1),
            "rsi_m": _round(t.get("rsi_m"), 1, 1),
            "sma20": _round(t.get("sma20"), 1, 1),
            "sma50": _round(t.get("sma50"), 1, 1),
            "sma200": _round(t.get("sma200"), 1, 1),
            "macd_h": _round(t.get("hist"), 1, 2),
            "atr_pct": _pct(t.get("atr_pct"), 2),
            "sd1y": _pct(t.get("sd1y"), 1),
            "above_mas": t.get("above_mas"),
            "stack": t.get("ma_stack"),
            "brk20": t.get("brk20"), "brk50": t.get("brk50"), "brk52w": t.get("brk52w"),
            "vol_spike": _round(t.get("vol_spike"), 1, 2),
            "turnover_cr": _round(t.get("turnover_cr"), 1, 1),
            "liquid": t.get("liquid"),
            "high52": _round(t.get("high52"), 1, 2),
            "low52": _round(t.get("low52"), 1, 2),
            "from_high": _pct(t.get("from_high52")),
            # The shorter window, for a name with less than a year of bars.
            # Carries its own length so nothing downstream can print it as a
            # year — see the note beside its computation.
            "rng_hi": _round(t.get("rng_hi"), 1, 2),
            "rng_lo": _round(t.get("rng_lo"), 1, 2),
            "rng_from_hi": _pct(t.get("rng_from_hi")),
            "rng_sessions": t.get("rng_sessions"),
            "r1d": _pct(t.get("r1d")),
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
            # A target ladder anchored to levels price actually turned at, each
            # carrying the measured share of closed trades that reached that
            # far. Replaces three fixed R-multiples snapped to a rolling max.
            # See targets.py for why the ledger says the old ladder was too
            # OPTIMISTIC, not too conservative.
            "lad": _targets.compact(_ladder_for(t, r, px_row), _basis_legend),
            "setup": setup_label(t, r),
            "updates": updates(r, t),
            "has_stmts": r.get("has_statements"),
            "fy_count": r.get("fy_count"),
            "shares_changed": r.get("shares_changed"),
            "next_earnings": r.get("next_earnings"),
            "last_date": t.get("last_date"),
            "news": [],
        })

    # One verdict per row, from the row that was just published — see
    # verdict.py. Deliberately runs on the FINAL dict rather than the internal
    # `r`/`t` structures, so the site, the tests and this build all read
    # identical keys and cannot drift apart.
    _verdict_tally = _verdict.annotate(out)
    print(f"[verdict] {' · '.join(f'{k} {v}' for k, v in _verdict_tally.items() if v)}")

    # ── NIFTY500 AHIMSA MEMBERSHIP ───────────────────────────────────────────
    #
    # NSE Indices launched the Nifty500 Ahimsa index on 10 July 2026: the Nifty
    # 500 filtered to companies not engaged in activities harmful to animals,
    # 326 of the 500 at launch. This attaches whether NSE put the name in it.
    #
    # IT IS MEMBERSHIP, NOT A SCORE, AND IT IS DELIBERATELY NOT ONE.
    #
    # NSE publishes the constituent list. It publishes no per-company
    # "quotient", so there is none to report — and computing one here would put
    # a number with no source beside a table where every other number has one.
    # `ahimsa` is therefore True, False or None and never a figure.
    #
    # IT IS ALSO NOT IN THE COMPOSITE, for the same reason `breadth` is not:
    # nothing measured says an Ahimsa constituent outperforms, and weighting it
    # into `comp` would assert exactly that. It is a filter the reader applies,
    # not a judgement the screen makes for them.
    #
    # None means the list could not be read, which is NOT the same as the name
    # being excluded — a stock missing from a list that failed to load is not a
    # stock NSE left out. When the fetch fails every row gets None, so a broken
    # fetch cannot silently mark all 500 names as failing an ethics test.
    from signals.universe import ahimsa_membership
    _ahimsa, _ahimsa_ok = ahimsa_membership()
    # The row key here is `sym` (set at "sym": u["symbol"] above), NOT `symbol`.
    # It read `symbol` for one build cycle, which resolves to None on every row,
    # and `None in _ahimsa` is False — so all 989 names were published as
    # FAILING an ethics screen while the index list loaded perfectly. The guard
    # directly above protects against the list failing to load; it could not see
    # a key that never existed.
    for r in out:
        r["ahimsa"] = (r.get("sym") in _ahimsa) if _ahimsa_ok else None
    _hits = sum(1 for r in out if r.get("ahimsa"))
    print(f"[ahimsa] {'list read: ' + str(len(_ahimsa)) + ' constituents' if _ahimsa_ok else 'LIST UNAVAILABLE — every row marked unknown'}"
          f"{', ' + str(_hits) + ' of ' + str(len(out)) + ' screened names are in it' if _ahimsa_ok else ''}")
    # A 326-name index and a ~1000-name NSE universe cannot be disjoint. Zero
    # overlap means the join is broken, not that nothing qualified, and the
    # difference is invisible in the payload — every row just reads False. Loud
    # here, because the last time this silently published 989 wrong answers.
    if _ahimsa_ok and _ahimsa and not _hits:
        print(f"[ahimsa] ERROR: {len(_ahimsa)} constituents and {len(out)} screened "
              f"names share NOTHING — the symbol join is broken, not the list")

    vet_meta = vet(out, price_date=None, today=datetime.now(IST).date())
    mf_meta = magic_formula(out, today=datetime.now(IST).date())
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
        # Named from what was ACTUALLY read. A composed universe must never be
        # published under the index's name: the core is NSE's list, the rest is
        # a liquidity-ranked extension, and the label says both.
        "universe": (
            (f"NSE Nifty Total Market + top-{len(ext)} by turnover"
             if ext else "NSE Nifty Total Market")
            if core_size > 600
            else "NSE Nifty 500 (fallback — Total Market unavailable)"),
        "universe_size": universe_size,
        # The split, so a reader can tell an index constituent from an extension
        # pick without inspecting every row.
        "universe_core": core_size,
        "universe_ext": len(ext),
        "count": len(out),
        "attempted": len(uni),
        "weights": WEIGHTS,
        # The target ladder's shared vocabulary: the basis strings each row
        # references by index, plus the constants a reader needs to check the
        # arithmetic. See targets.py.
        "ladder": {
            "basis": _basis_legend,
            "reach_sample": _targets.REACH_SAMPLE_N,
            "stop_atr_mult": ATR_STOP_MULT_FOR_SCREEN,
            "floors": [_targets.R1_FLOOR, _targets.R2_FLOOR, _targets.R3_FLOOR],
            "note": ("Targets sit on a level price turned at where one exists "
                     "in range, otherwise on the R floor, which is labelled. "
                     "`reach` is the measured share of closed trades that ran "
                     "at least that far — cf_1h and intraday are excluded from "
                     "that sample because their max-favourable figures are not "
                     "credible."),
        },
        # Stated at the payload level so a consumer can tell "NSE excluded it"
        # from "we could not read NSE's list" without inspecting 500 rows.
        "ahimsa": {
            "index": "Nifty500 Ahimsa",
            "source": "NSE Indices constituent list",
            "launched": "2026-07-10",
            "available": _ahimsa_ok,
            "constituents": len(_ahimsa) if _ahimsa_ok else None,
            "in_screen": sum(1 for r in out if r.get("ahimsa")) if _ahimsa_ok else None,
            "note": ("Membership in NSE's index, not a score. NSE publishes no "
                     "per-company quotient and this build computes none. It is "
                     "not an input to any score on this page."),
        },
        "coverage": cov,
        "changes": delta_meta,
        # Real breadth across the screened universe, not a proxy. Dated, and
        # deliberately not an input to any score — see breadth().
        "breadth": breadth(out, bench),
        "magic_formula": mf_meta,
        "vet": vet_meta,
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
    # A row priced to an earlier session than the newest one in the build.
    newest = max((x.get("last_date") or "" for x in rows), default="")
    behind = sum(1 for x in rows if (x.get("last_date") or "") < newest)
    return {
        "priced": n,
        "behind": behind,
        "missing_close": max(0, FETCH_REPORT.get("missing_close", 0) - FETCH_REPORT.get("rebuilt", 0)),
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
    # Nine characters explaining the F-score. In the DETAIL payload, not the
    # table: the table already carries the score itself, which is what sorts
    # and filters, and 750 x 9 bytes belongs in the half that is fetched only
    # when a reader opens a company.
    "piotroski_bits",
)


# ── THE THIRD PROJECTION: WHAT THE SIGNAL SITE'S LIGHT ROUTES READ ──────────
#
# split_payload already moved the research prose out of the table. The table it
# leaves is still 1.49 MB raw / 298 KB gzipped, and TEN routes on
# signal.askakshay.com fetch it — home, brief, ideas, radar, news, markets,
# signals, screen, stock detail. On a phone that is ~300 KB before a pixel of
# the answer renders, nine times over.
#
# Measured on the 2026-09-09 build, 750 rows, 101 fields:
#
#   dropped 29 fields no signal.js code path reads   -> 229 KB gz  (-23%)
#   + moved risk.flags and vd.f (PROSE) to detail    -> 206 KB gz  (-31%)
#
# WHY THOSE TWO IN PARTICULAR. `risk.flags` and `vd.f` are lists of sentences —
# "Only 15% of reported profit arrived as operating cash" — 750 times over.
# They are EVIDENCE, which is the third tier of the disclosure order and is
# read when somebody opens one company, not when a page lists many. The
# summary scalars a card actually prints (risk.level, risk.score, vd.c, vd.l,
# vd.k) stay in the table.
#
# WHY A NEW FILE RATHER THAN NARROWING screen.json. 14 of those 29 fields ARE
# read by static/app.js — the newspaper's own UI, a different site on the same
# payload. Narrowing the shared file to suit this site would have broken that
# one silently. The two consumers have different needs and now have different
# files; screen.json is untouched.
#
# WHAT THE KEEP LIST IS, AND WHERE IT IS CHECKED. It is every field the signal
# frontend references, and it IS maintained by hand — an earlier version of
# this note claimed it was derived and cited a test_screen_lite.py that has
# never existed in this repo, which is the worse failure of the two: a
# hand-kept list that says it is checked gets treated as if it were.
#
# The check that does exist is in the OTHER repo, test/guard.mjs, and it runs
# in that repo's deploy.yml. It reads public/signal.js against the payload and
# fails if a field the page reads is absent from the projection — the
# direction that breaks a page. The reverse (a field kept here that the page
# stopped reading) costs bytes, not correctness, and nothing measures it; the
# frontend touches essentially every field that survives, measured in a
# browser on 2026-09-19, so there is currently nothing to reclaim.
LITE_DROP_FIELDS = (
    "brk20", "brk50", "cf_conf", "cfo_cr", "delta", "ebit_margin", "em_conf",
    "fcf_cr", "fcf_margin", "g_conf", "has_stmts", "isin", "m_inv", "m_pos",
    "m_swing", "macd_h", "margin_delta", "net_margin", "pat_yoy", "piotroski_of",
    "q_conf", "rank_move", "rev_growth", "risk_lvl", "roe_med", "rs1y",
    "shares_changed", "tech_conf", "tier",
)
# Prose inside an otherwise-scalar object. Dropped from the sub-object rather
# than dropping the whole field, because the card prints its summary keys.
LITE_DROP_INNER = {"risk": ("flags",), "vd": ("f",), "vet": ("c",)}


def lite_payload(table: dict) -> dict:
    """The table again, minus what the signal site never reads and the prose.

    Takes the ALREADY-SPLIT table, not the raw build, so it can never
    accidentally re-admit a detail field.
    """
    rows = []
    for r in table.get("rows") or []:
        o = {k: v for k, v in r.items() if k not in LITE_DROP_FIELDS}
        for outer, inner in LITE_DROP_INNER.items():
            if isinstance(o.get(outer), dict):
                o[outer] = {k: v for k, v in o[outer].items() if k not in inner}
        rows.append(o)
    out = {k: v for k, v in table.items() if k != "rows"}
    out["rows"] = rows
    # Declared, so a consumer that needs a dropped field can tell it was
    # dropped rather than concluding the build lost it.
    out["is_lite"] = True
    out["lite_dropped"] = sorted(LITE_DROP_FIELDS)
    out["lite_note"] = ("Fields the signal frontend does not read, plus the "
                        "per-company prose in risk.flags and vd.f, are in "
                        "screen.json and screen-detail.json. Nothing here is "
                        "recomputed or rounded differently.")
    return out


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
