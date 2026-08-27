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

# Horizons and rates the site projects. 18 is the college-age horizon; 12/14/16
# brackets what Indian equity has actually delivered. Mirrored in
# vercel-news/api/sip.js — change both.
PROJECTION_YEARS = (5, 10, 15, 18)
PROJECTION_RATES = (0.12, 0.14, 0.16)

_IST = timezone(timedelta(hours=5, minutes=30))

# Hard vetoes — a name failing any of these is not ranked at all.
MIN_MARKET_CAP_CR = 5000.0
MAX_DEBT_TO_EQUITY = 1.5              # skipped for lenders
MAX_PE = 80.0

# Weights are renormalised over whatever factors Yahoo actually returned, which
# means a name with one factor present scores on that one factor alone. That is
# how JSWDULUX reached 96.2/100 on 15% coverage — a single cheap PE and nothing
# else — and outranked fully-covered names. A score built from under half the
# model is not a score, so those names are vetoed rather than ranked.
MIN_FACTOR_COVERAGE = 0.50

# How far down the ranking to price-check before giving up on filling a bucket.
# Deep enough that a slice of a few thousand rupees still finds four names.
PRICE_LOOKAHEAD = 60

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
            # Shares the proposal says to buy. Deliberately separate from `qty`,
            # which means shares actually bought — conflating a suggestion with
            # a fill is how a plan starts reporting returns it never earned.
            ("proposed_qty",  "ALTER TABLE sip_holdings ADD COLUMN proposed_qty INTEGER"),
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
        if avail < MIN_FACTOR_COVERAGE:
            log.debug(f"sip veto {f['symbol']}: {avail:.0%} factor coverage")
            continue
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


def ref_prices(symbols: list[str]) -> dict[str, float]:
    """Last close per symbol. Empty dict rather than an exception on failure."""
    if not symbols:
        return {}
    try:
        import yfinance as yf
        from symbols import to_yahoo
    except ImportError as e:
        log.warning(f"sip ref_prices: {e}")
        return {}

    out = {}
    for s in symbols:
        try:
            df = yf.download(to_yahoo(s), period="5d", interval="1d",
                             progress=False, auto_adjust=True)
            if df is None or df.empty:
                continue
            px = float(df["Close"].to_numpy().ravel()[-1])
            if px == px and px > 0:          # px == px filters NaN
                out[s] = round(px, 2)
        except Exception as e:
            log.warning(f"sip ref price {s}: {e}")
    return out


def allocate_whole_shares(picks: list[dict], budget: float,
                          prices: dict[str, float]) -> list[dict]:
    """Turn a ranked shortlist and a rupee budget into buyable share counts.

    The bucket used to divide the month evenly and stop there: ₹10,000 over
    four names is ₹2,500 each, and ₹2,500 does not buy one share of a ₹2,983
    stock. The proposal was arithmetically tidy and impossible to execute.

    So: an even split is the starting point, not the answer. Each name takes
    the whole shares its slice affords, then the unspent remainder is walked
    back down the ranking one share at a time until nothing more fits. Names
    priced above their own slice have already been filtered out upstream — a
    name that cannot take a single share does not belong in the bucket.
    """
    if not picks:
        return []

    per = budget / len(picks)
    for p in picks:
        px = prices.get(p["symbol"])
        p["ref_price"] = px
        p["proposed_qty"] = int(per // px) if px else 0
        p["allocated"] = round((p["proposed_qty"] or 0) * (px or 0), 2)

    # Spend the remainder in rank order — the highest-conviction name gets the
    # extra share, not whichever one happens to be cheapest.
    spent = sum(p["allocated"] for p in picks)
    leftover = budget - spent
    progress = True
    while progress and leftover > 0:
        progress = False
        for p in picks:
            px = p.get("ref_price")
            if px and px <= leftover:
                p["proposed_qty"] += 1
                p["allocated"] = round(p["allocated"] + px, 2)
                leftover = round(leftover - px, 2)
                progress = True

    for p in picks:
        p["cash_left"] = round(leftover, 2)
    return picks


def repair_bucket_quantities(bucket: str | None = None) -> int:
    """Price an already-committed bucket that was written without share counts.

    build_bucket() is idempotent per calendar month, and rightly so — a month
    already committed must not silently re-pick different names. But that same
    guarantee FREEZES a bucket written by the older code, which divided the
    month into equal rupee slices and stored no quantity and no price at all.
    The live 2026-08 bucket is one: four names, Rs 2,500 each, proposed_qty
    NULL, ref_price NULL. The section rendered a row of dashes and would have
    done so forever, because the only function that could fill them refuses to
    touch a month that exists.

    This fills in the missing numbers WITHOUT re-picking. The names stay
    exactly as they were recommended; they simply acquire the share counts they
    should always have had. Re-running the screen would quietly rewrite history
    for a month already published.

    A name whose share price exceeds its own slice gets qty 0 rather than being
    dropped: it WAS recommended, and a bucket that silently loses a name is a
    worse record than one that shows the recommendation could not be executed.
    JSWDULUX at Rs 2,983 in a Rs 2,500 slot is exactly that case.

    Returns the number of holdings updated.
    """
    init_sip_db()
    with _db.connect() as c:
        rows = c.execute(
            "SELECT bucket, symbol, allocated, rank FROM sip_holdings "
            "WHERE (proposed_qty IS NULL OR ref_price IS NULL) "
            + ("AND bucket = ? " if bucket else "")
            + "ORDER BY bucket, rank",
            ((bucket,) if bucket else ())).fetchall()
    if not rows:
        return 0

    by_bucket: dict[str, list] = {}
    for b, sym, alloc, rank in rows:
        by_bucket.setdefault(b, []).append(
            {"symbol": sym, "allocated": alloc, "rank": rank})

    fixed = 0
    for bname, picks in by_bucket.items():
        prices = ref_prices([p["symbol"] for p in picks])
        if not prices:
            log.warning(f"sip repair {bname}: no prices available — left as-is")
            continue
        # The budget is what the bucket was actually given, not today's
        # monthly_amount: a bucket from an earlier SIP year ran at a lower
        # step-up and repricing it at today's figure would invent money.
        budget = sum(p["allocated"] or 0 for p in picks)
        priced = [p for p in picks if prices.get(p["symbol"])]
        allocate_whole_shares(priced, budget, prices)
        with _db.connect() as c:
            for p in picks:
                px = prices.get(p["symbol"])
                c.execute(
                    "UPDATE sip_holdings SET ref_price=?, proposed_qty=?, allocated=? "
                    "WHERE bucket=? AND symbol=?",
                    (px, p.get("proposed_qty", 0), p.get("allocated", 0.0),
                     bname, p["symbol"]))
                fixed += 1
            c.commit()
            _db.sync(c)
        log.info(f"sip repair {bname}: " + ", ".join(
            f"{p['symbol']}x{p.get('proposed_qty', 0)}" for p in picks))
    return fixed


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
    per_slice = amount / max(names, 1)

    # One name per sector: four picks that are all private banks is one bet,
    # not four. And nothing priced above its own slice — a name whose share
    # price exceeds the money allotted to it cannot be bought at all, which is
    # what put ₹2,983 JSWDULUX in a ₹2,500 slot.
    # Prices for the shortlist in one pass. Priced lazily per candidate this
    # would be one Yahoo round trip per rejected name, and at a ₹2,500 slice a
    # lot of the Nifty 500 gets rejected on price alone.
    shortlist = ranked[:PRICE_LOOKAHEAD]
    prices = ref_prices([r["symbol"] for r in shortlist])

    picks, seen_sectors = [], set()
    for r in shortlist:
        if len(picks) >= names:
            break
        if r["sector"] in seen_sectors and len(seen_sectors) < names:
            continue
        px = prices.get(r["symbol"])
        if not px:
            log.debug(f"sip skip {r['symbol']}: no reference price")
            continue
        if px > per_slice:
            log.info(f"sip skip {r['symbol']}: ₹{px:,.2f}/share exceeds the "
                     f"₹{per_slice:,.0f} slice — cannot buy one share")
            continue
        r["_px"] = px
        picks.append(r)
        seen_sectors.add(r["sector"])

    if len(picks) < names:
        log.warning(f"sip: only {len(picks)}/{names} names affordable at "
                    f"₹{per_slice:,.0f} a slice from the top {len(shortlist)} ranked")

    allocate_whole_shares(picks, amount, {p["symbol"]: p["_px"] for p in picks})

    proposal = {
        "bucket": bname, "monthly_amount": amount, "sip_year": sip_year(on),
        "per_name": round(per_slice, 2), "status": "proposed",
        "cash_left": picks[0]["cash_left"] if picks else round(amount, 2),
        "holdings": [{
            "symbol": p["symbol"], "allocated": p["allocated"],
            "ref_price": p["ref_price"], "proposed_qty": p["proposed_qty"],
            "score": p["score"], "rank": i + 1, "sector": p["sector"],
            "rationale": p["rationale"],
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
                         (bucket, symbol, allocated, ref_price, proposed_qty,
                          score, rank, rationale, status)
                         VALUES (?,?,?,?,?,?,?,?,'proposed')""",
                      (bname, h["symbol"], h["allocated"], h["ref_price"],
                       h["proposed_qty"], h["score"], h["rank"], h["rationale"]))
        c.commit()
        _db.sync(c)
    log.info(f"sip: bucket {bname} created — ₹{amount:,.0f} across {len(picks)} names ("
             + ", ".join(f"{p['symbol']}×{p['proposed_qty']}" for p in picks)
             + f"), ₹{proposal['cash_left']:,.0f} unspent")

    # Mirror the allocation into the signal log. The bucket is rebuilt monthly
    # and was previously visible only as the CURRENT month's allocation —
    # there was no record of what it said in June, so no way to see how the
    # allocation drifted or whether it followed the screen.
    #
    # Allocations, not trades: they carry no stop or target and are named in
    # tracker.EXCLUDE_FROM_EXPECTANCY (and stats.js NON_TRADING) so they can
    # never enter a win rate. Best-effort — the bucket is committed above and
    # a ledger failure must not fail the monthly job.
    try:
        import tracker
        ids = tracker.log_sip_bucket([
            {"symbol": h["symbol"], "price": h["ref_price"],
             "pct": (round(h["allocated"] / amount * 100, 1) if amount else None),
             "bucket": bname}
            for h in proposal["holdings"] if h.get("ref_price")
        ], bname)
        log.info(f"sip: mirrored {len([i for i in ids if i])} allocation(s) to the ledger")
    except Exception as e:                                   # noqa: BLE001
        log.warning(f"sip: ledger mirror failed: {e}")

    return get_bucket(bname)


def get_bucket(bname: str) -> dict:
    with _db.connect() as c:
        b = c.execute("""SELECT bucket, created_at, monthly_amount, sip_year, status
                         FROM sip_buckets WHERE bucket=?""", (bname,)).fetchone()
        if not b:
            return {}
        hs = c.execute("""SELECT symbol, allocated, ref_price, buy_price, qty,
                                 bought_at, score, rank, rationale, status,
                                 exit_price, exited_at, last_price, last_price_at,
                                 proposed_qty
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
            "proposed_qty": h[14],
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
        heads = "".join(f"{f'@{r:.0%}':>14}" for r in PROJECTION_RATES)
        print(f"{'Yr':<4}{'Monthly':>10}{'Invested':>14}{heads}")
        for y in PROJECTION_YEARS:
            m = BASE_MONTHLY * (1 + ANNUAL_STEP_UP) ** (y - 1)
            rows = [projection(y, r) for r in PROJECTION_RATES]
            corpora = "".join(f"{p['corpus']:>14,.0f}" for p in rows)
            print(f"{y:<4}{m:>10,.0f}{rows[0]['invested']:>14,.0f}{corpora}")
    else:
        out = build_bucket(dry_run="--dry-run" in sys.argv)
        print(json.dumps(out, indent=2, default=str))
