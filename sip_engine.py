#!/usr/bin/env python3
"""
sip_engine.py — monthly SIP bucket construction and tracking.

Plan this implements
--------------------
₹10,000/month, stepped up 10% every year, one new bucket per month, each
bucket split across N names and tracked with its own cost basis. Buckets are
never merged: a blended average hides that the March bucket is up 40% and the
July bucket is down 12%, which is exactly what you need to see to know whether
the ranking is working.

    year 1   ₹10,000/mo      year 5   ₹14,641/mo     year 15  ₹37,975/mo
    year 3   ₹12,100/mo      year 10  ₹23,579/mo     year 20  ₹61,159/mo

What this does and does not do
------------------------------
It RANKS and PROPOSES. Holdings are written with status='proposed' and stay
that way until something explicitly marks them held — the engine never claims
a position was taken. Confirming a buy is a decision this module does not make.

Factors are limited to what can actually be computed from the available data.
The plan called for interest coverage, promoter holding and pledge percentage,
and PE against a stock's own 5-year median; Yahoo exposes none of those, so
they are omitted rather than approximated with something that would look
precise and be wrong. What remains:

    25%  return on equity
    20%  growth        (revenue yoy + earnings yoy)
    15%  balance sheet (debt/equity)
    15%  cash          (free cash flow positive)
    15%  valuation     (PE percentile against the scored universe)
    10%  margins       (net profit margin)

Valuation is relative to the universe scored in the same run, not to history,
so it says "cheap compared to the other candidates today" — which is the claim
the data supports.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone

import db as _db

log = logging.getLogger(__name__)

BASE_MONTHLY = 10000.0
ANNUAL_STEP_UP = 0.10
NAMES_PER_BUCKET = 4
SIP_START = date(2026, 8, 1)          # year 1 begins here; step-ups key off it

_IST = timezone(timedelta(hours=5, minutes=30))

# Hard vetoes — a name failing any of these is not ranked at all.
MIN_MARKET_CAP_CR = 5000.0
MAX_DEBT_TO_EQUITY = 1.5              # skipped for lenders
MAX_PE = 80.0

WEIGHTS = {
    "roe": 0.25,
    "growth": 0.20,
    "balance_sheet": 0.15,
    "cash": 0.15,
    "valuation": 0.15,
    "margins": 0.10,
}

_LENDERS = {"Financial Services", "Financials", "Real Estate"}


# ── schema ───────────────────────────────────────────────────────────────────

def init_sip_db():
    with _db.connect() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS sip_buckets (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            bucket          TEXT NOT NULL UNIQUE,   -- '2026-08'
            created_at      TEXT NOT NULL,
            monthly_amount  REAL NOT NULL,
            sip_year        INTEGER,
            status          TEXT DEFAULT 'active',
            note            TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS sip_holdings (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            bucket       TEXT NOT NULL,
            symbol       TEXT NOT NULL,
            allocated    REAL NOT NULL,
            ref_price    REAL,
            buy_price    REAL,
            qty          REAL,
            bought_at    TEXT,
            score        REAL,
            rank         INTEGER,
            rationale    TEXT,
            status       TEXT DEFAULT 'proposed',   -- proposed | held | exited
            exit_price   REAL,
            exited_at    TEXT,
            last_price   REAL,
            last_price_at TEXT
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sip_hold_bucket ON sip_holdings(bucket)")
        for col, sql in [
            ("last_price",    "ALTER TABLE sip_holdings ADD COLUMN last_price REAL"),
            ("last_price_at", "ALTER TABLE sip_holdings ADD COLUMN last_price_at TEXT"),
        ]:
            try:
                have = [r[1] for r in c.execute("PRAGMA table_info(sip_holdings)").fetchall()]
                if col not in have:
                    c.execute(sql)
            except Exception as e:
                log.warning(f"sip migrate {col}: {e}")
        c.commit()
        _db.sync(c)


def refresh_prices() -> int:
    """Mark every distinct SIP symbol to market. Returns rows updated.

    The site cannot call Yahoo (no network from the API layer, and it would be
    one call per pageview), so the last price is written here by the scheduled
    job and simply read back.
    """
    import yfinance as yf
    from symbols import to_yahoo

    init_sip_db()
    with _db.connect() as c:
        syms = [r[0] for r in c.execute(
            "SELECT DISTINCT symbol FROM sip_holdings WHERE status!='exited'").fetchall()]
    if not syms:
        return 0

    now, updated = datetime.now(_IST).isoformat(), 0
    for s in syms:
        try:
            df = yf.download(to_yahoo(s), period="5d", interval="1d",
                             progress=False, auto_adjust=True)
            if df is None or df.empty:
                continue
            px = float(df["Close"].to_numpy().ravel()[-1])
            if px != px or px <= 0:
                continue
        except Exception as e:
            log.warning(f"sip price {s}: {e}")
            continue
        with _db.connect() as c:
            c.execute("""UPDATE sip_holdings SET last_price=?, last_price_at=?
                         WHERE symbol=? AND status!='exited'""", (px, now, s))
            c.commit()
            _db.sync(c)
        updated += 1
    log.info(f"sip: refreshed {updated}/{len(syms)} prices")
    return updated


# ── the plan arithmetic ──────────────────────────────────────────────────────

def sip_year(on: date | None = None) -> int:
    """1-based SIP year. Year 1 is the twelve months from SIP_START."""
    on = on or datetime.now(_IST).date()
    months = (on.year - SIP_START.year) * 12 + (on.month - SIP_START.month)
    return max(1, months // 12 + 1)


def monthly_amount(on: date | None = None) -> float:
    """₹10,000 stepped up 10% on each SIP anniversary."""
    return round(BASE_MONTHLY * (1 + ANNUAL_STEP_UP) ** (sip_year(on) - 1), 2)


def projection(years: int, annual_return: float) -> dict:
    """Corpus and invested totals for the step-up plan. Pure arithmetic."""
    m, corpus, invested = BASE_MONTHLY, 0.0, 0.0
    mr = (1 + annual_return) ** (1 / 12) - 1
    for _ in range(years):
        for _ in range(12):
            corpus = corpus * (1 + mr) + m
            invested += m
        m *= 1 + ANNUAL_STEP_UP
    return {"years": years, "annual_return": annual_return,
            "invested": round(invested), "corpus": round(corpus),
            "gain": round(corpus - invested)}


# ── ranking ──────────────────────────────────────────────────────────────────

def _clamp01(v):
    return 0.0 if v < 0 else 1.0 if v > 1 else v


def _percentile_rank(value, population):
    """Fraction of the population this value beats. Lower PE ⇒ higher score."""
    if value is None or not population:
        return None
    below = sum(1 for p in population if p > value)
    return below / len(population)


def _vetoed(f) -> str | None:
    """Reason this name is excluded outright, or None."""
    mc = f.get("market_cap_cr")
    if mc is None or mc < MIN_MARKET_CAP_CR:
        return f"market cap {'unknown' if mc is None else f'₹{mc:,.0f}cr'} below ₹{MIN_MARKET_CAP_CR:,.0f}cr"
    pat = f.get("net_income")
    if pat is not None and pat <= 0:
        return "loss-making (trailing PAT ≤ 0)"
    pe = f.get("pe")
    if pe is None or pe <= 0:
        return "no meaningful PE"
    if pe > MAX_PE:
        return f"PE {pe:.0f}x above {MAX_PE:.0f}x"
    dte = f.get("debt_to_equity")
    if f.get("sector") not in _LENDERS and dte is not None and dte > MAX_DEBT_TO_EQUITY:
        return f"D/E {dte:.2f} above {MAX_DEBT_TO_EQUITY}"
    return None


def score_universe(symbols=None, limit_universe: int | None = None) -> list[dict]:
    """Fetch fundamentals, veto, then score. Returns ranked candidates."""
    import fundamentals as fnd

    if symbols is None:
        from signals.universe import load_nifty500
        symbols = [s.replace(".NS", "") for s in load_nifty500()]
    if limit_universe:
        symbols = symbols[:limit_universe]

    hits, tried = fnd.prefetch(symbols)
    log.info(f"sip: fundamentals {hits}/{tried}")

    rows, pes = [], []
    for s in symbols:
        f = fnd.get(s, allow_fetch=False)
        if not f:
            continue
        veto = _vetoed(f)
        if veto:
            log.debug(f"sip veto {s}: {veto}")
            continue
        rows.append(f)
        pes.append(f["pe"])

    scored = []
    for f in rows:
        # Each factor is (value, has_data). Yahoo's coverage of Indian tickers
        # is patchy — returnOnEquity is absent for most NSE names. Scoring a
        # missing factor as zero would rank companies by how well Yahoo covers
        # them rather than by quality, so absent factors are dropped from the
        # weighted sum and the remaining weights renormalised.
        parts, notes = {}, []

        roe = f.get("roe")
        parts["roe"] = (_clamp01((roe or 0) / 0.25), roe is not None)
        if roe is not None:
            notes.append(f"ROE {roe:.0%}")

        rev, eps = f.get("revenue_growth"), f.get("earnings_growth")
        gvals = [v for v in (rev, eps) if v is not None]
        parts["growth"] = (_clamp01(sum(gvals) / len(gvals) / 0.20) if gvals else 0.0,
                           bool(gvals))
        if rev is not None:
            notes.append(f"rev {rev:+.0%}")
        if eps is not None:
            notes.append(f"PAT {eps:+.0%}")

        dte = f.get("debt_to_equity")
        if f.get("sector") in _LENDERS:
            # Leverage IS the business model for a lender, so a neutral 0.5 is
            # a deliberate score rather than missing data — it stays weighted.
            parts["balance_sheet"] = (0.5, True)
            notes.append("lender — D/E n/a")
        elif dte is None:
            parts["balance_sheet"] = (0.0, False)
        else:
            parts["balance_sheet"] = (_clamp01(1 - dte / MAX_DEBT_TO_EQUITY), True)
            notes.append(f"D/E {dte:.2f}")

        fcf = f.get("free_cashflow")
        parts["cash"] = ((1.0 if fcf > 0 else 0.0), True) if fcf is not None else (0.0, False)
        if fcf is not None:
            notes.append("FCF+" if fcf > 0 else "FCF−")

        pr = _percentile_rank(f.get("pe"), pes)
        parts["valuation"] = (pr, True) if pr is not None else (0.0, False)
        notes.append(f"PE {f['pe']:.0f}x")

        pm = f.get("profit_margin")
        parts["margins"] = (_clamp01((pm or 0) / 0.20), pm is not None)
        if pm is not None:
            notes.append(f"margin {pm:.0%}")

        avail = sum(WEIGHTS[k] for k, (_v, ok) in parts.items() if ok)
        total = (sum(v * WEIGHTS[k] for k, (v, ok) in parts.items() if ok) / avail
                 if avail > 0 else 0.0)
        coverage = round(avail * 100)
        if coverage < 100:
            notes.append(f"{coverage}% factor coverage")

        scored.append({
            "symbol": f["symbol"],
            "sector": f.get("sector") or "—",
            "score": round(total * 100, 1),
            "coverage": coverage,
            "components": {k: (round(v * 100, 1) if ok else None)
                           for k, (v, ok) in parts.items()},
            "pe": f.get("pe"),
            "roe": roe,
            "market_cap_cr": f.get("market_cap_cr"),
            "rationale": " · ".join(notes),
        })

    scored.sort(key=lambda x: -x["score"])
    log.info(f"sip: scored {len(scored)} of {len(symbols)} after vetoes")
    return scored


# ── buckets ──────────────────────────────────────────────────────────────────

def bucket_name(on: date | None = None) -> str:
    on = on or datetime.now(_IST).date()
    return on.strftime("%Y-%m")


def build_bucket(on: date | None = None, names: int = NAMES_PER_BUCKET,
                 dry_run: bool = False, candidates=None) -> dict:
    """Propose this month's bucket. Idempotent — an existing bucket is returned
    as-is rather than rebuilt, so re-running the monthly job cannot double-book
    or silently re-pick different names for a month already committed."""
    init_sip_db()
    on = on or datetime.now(_IST).date()
    bname = bucket_name(on)

    with _db.connect() as c:
        existing = c.execute("SELECT bucket FROM sip_buckets WHERE bucket=?",
                             (bname,)).fetchone()
    if existing:
        log.info(f"sip: bucket {bname} already exists — returning it unchanged")
        return get_bucket(bname)

    amount = monthly_amount(on)
    ranked = candidates if candidates is not None else score_universe()
    # One name per sector: four picks that are all private banks is one bet,
    # not four.
    picks, seen_sectors = [], set()
    for r in ranked:
        if r["sector"] in seen_sectors and len(seen_sectors) < names:
            continue
        picks.append(r)
        seen_sectors.add(r["sector"])
        if len(picks) >= names:
            break

    per = round(amount / max(len(picks), 1), 2)
    proposal = {
        "bucket": bname, "monthly_amount": amount, "sip_year": sip_year(on),
        "per_name": per, "status": "proposed",
        "holdings": [{
            "symbol": p["symbol"], "allocated": per, "score": p["score"],
            "rank": i + 1, "sector": p["sector"], "rationale": p["rationale"],
        } for i, p in enumerate(picks)],
    }
    if dry_run:
        return proposal

    with _db.connect() as c:
        c.execute("""INSERT INTO sip_buckets
                     (bucket, created_at, monthly_amount, sip_year, status)
                     VALUES (?,?,?,?,'active')""",
                  (bname, datetime.now(_IST).isoformat(), amount, sip_year(on)))
        for h in proposal["holdings"]:
            c.execute("""INSERT INTO sip_holdings
                         (bucket, symbol, allocated, score, rank, rationale, status)
                         VALUES (?,?,?,?,?,?,'proposed')""",
                      (bname, h["symbol"], h["allocated"], h["score"],
                       h["rank"], h["rationale"]))
        c.commit()
        _db.sync(c)
    log.info(f"sip: bucket {bname} created — ₹{amount:,.0f} across "
             f"{len(picks)} names ({', '.join(p['symbol'] for p in picks)})")
    return get_bucket(bname)


def get_bucket(bname: str) -> dict:
    with _db.connect() as c:
        b = c.execute("""SELECT bucket, created_at, monthly_amount, sip_year, status
                         FROM sip_buckets WHERE bucket=?""", (bname,)).fetchone()
        if not b:
            return {}
        hs = c.execute("""SELECT symbol, allocated, ref_price, buy_price, qty,
                                 bought_at, score, rank, rationale, status,
                                 exit_price, exited_at, last_price, last_price_at
                          FROM sip_holdings WHERE bucket=? ORDER BY rank""",
                       (bname,)).fetchall()
    return {
        "bucket": b[0], "created_at": b[1], "monthly_amount": b[2],
        "sip_year": b[3], "status": b[4],
        "holdings": [{
            "symbol": h[0], "allocated": h[1], "ref_price": h[2], "buy_price": h[3],
            "qty": h[4], "bought_at": h[5], "score": h[6], "rank": h[7],
            "rationale": h[8], "status": h[9], "exit_price": h[10],
            "exited_at": h[11], "last_price": h[12], "last_price_at": h[13],
        } for h in hs],
    }


def list_buckets() -> list[dict]:
    init_sip_db()
    with _db.connect() as c:
        rows = c.execute("SELECT bucket FROM sip_buckets ORDER BY bucket DESC").fetchall()
    return [get_bucket(r[0]) for r in rows]


# ── returns ──────────────────────────────────────────────────────────────────

def xirr(cashflows, guess: float = 0.15):
    """Annualised money-weighted return for (date, amount) pairs.

    Outflows negative, inflows positive. Newton's method, bisection fallback —
    Newton diverges on the short, lumpy cashflow series a young SIP produces.
    Returns None when the series cannot define a rate.
    """
    if len(cashflows) < 2:
        return None
    flows = sorted(cashflows, key=lambda x: x[0])
    t0 = flows[0][0]
    years = [((d - t0).days / 365.0, a) for d, a in flows]
    if not (any(a < 0 for _, a in years) and any(a > 0 for _, a in years)):
        return None

    def npv(r):
        if r <= -0.999999:
            return float("inf")
        return sum(a / (1 + r) ** t for t, a in years)

    r = guess
    for _ in range(80):
        f = npv(r)
        if abs(f) < 1e-7:
            return r
        dr = 1e-6
        d = (npv(r + dr) - f) / dr
        if d == 0:
            break
        step = f / d
        r -= step
        if r <= -0.999999:
            break
        if abs(step) < 1e-9:
            return r

    lo, hi = -0.9999, 10.0
    flo, fhi = npv(lo), npv(hi)
    if flo * fhi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        fm = npv(mid)
        if abs(fm) < 1e-7:
            return mid
        if flo * fm < 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2


def bucket_performance(bname: str, prices: dict) -> dict:
    """Cost, value and XIRR for one bucket. `prices` maps symbol → last price.

    Only holdings actually marked held count — a proposed name has no money in
    it and including it would report a return on a position never taken.
    """
    b = get_bucket(bname)
    if not b:
        return {}
    invested = value = 0.0
    flows, lines = [], []
    for h in b["holdings"]:
        if h["status"] != "held" or not h.get("qty") or not h.get("buy_price"):
            lines.append({**h, "invested": 0, "value": None, "pnl_pct": None})
            continue
        cost = h["qty"] * h["buy_price"]
        last = prices.get(h["symbol"])
        val = h["qty"] * last if last else cost
        invested += cost
        value += val
        try:
            d = datetime.fromisoformat(str(h["bought_at"])[:10]).date()
            flows.append((d, -cost))
        except (TypeError, ValueError):
            pass
        lines.append({**h, "invested": round(cost, 2), "value": round(val, 2),
                      "pnl_pct": round((val / cost - 1) * 100, 2) if cost else None})

    if flows:
        flows.append((datetime.now(_IST).date(), value))
    r = xirr(flows) if len(flows) > 1 else None
    return {
        "bucket": bname, "monthly_amount": b["monthly_amount"],
        "sip_year": b["sip_year"], "created_at": b["created_at"],
        "invested": round(invested, 2), "value": round(value, 2),
        "pnl": round(value - invested, 2),
        "pnl_pct": round((value / invested - 1) * 100, 2) if invested else None,
        "xirr_pct": round(r * 100, 2) if r is not None else None,
        "holdings": lines,
    }


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if "--project" in sys.argv:
        print(f"{'Yr':<4}{'Monthly':>10}{'Invested':>14}{'@10%':>14}{'@12%':>14}{'@14%':>14}")
        for y in (1, 3, 5, 7, 10, 15, 20, 25):
            m = BASE_MONTHLY * (1 + ANNUAL_STEP_UP) ** (y - 1)
            a, b_, c_ = (projection(y, r) for r in (0.10, 0.12, 0.14))
            print(f"{y:<4}{m:>10,.0f}{a['invested']:>14,.0f}"
                  f"{a['corpus']:>14,.0f}{b_['corpus']:>14,.0f}{c_['corpus']:>14,.0f}")
    else:
        out = build_bucket(dry_run="--dry-run" in sys.argv)
        print(json.dumps(out, indent=2, default=str))
