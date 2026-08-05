#!/usr/bin/env python3
"""
ai_longterm.py — five long-horizon NSE ideas a week, screened on the business
first and the chart second.

What this is, precisely
----------------------
Every other engine in this repo trades a setup: entry, stop, target, measured
in R over days. This one does not. It answers a different question — "which
five companies would I be happy owning for two years" — and the answer is
dominated by return on capital, growth and what you pay for it, with the chart
used only to avoid buying something in a structural downtrend.

Two things follow from that, and both are enforced in code rather than trusted
to a reader:

  1. These rows are written to the ledger with signal_type='ai_longterm' and
     are EXCLUDED from trading expectancy. A 2-year idea cannot resolve on a
     20-day horizon, so letting it into the R-multiple statistics would quietly
     corrupt the only honest number on the site. See EXCLUDE_FROM_EXPECTANCY.
  2. The SELECTION is arithmetic, not a language model. Weights below, applied
     to Yahoo fundamentals and price history. The language model writes the
     one-paragraph thesis AFTER the ranking is fixed, and cannot reorder it.
     "AI generated" here means the words are; the picks are measured.

Scoring
-------
    FUNDAMENTAL  70%        TECHNICAL  30%
      return on capital 30%   trend structure  40%   (200DMA, slope, 50>200)
      growth            25%   relative strength 30%   (6m + 12m momentum)
      margins           15%   drawdown          20%   (distance from 52w high)
      balance sheet     15%   participation     10%   (volume trend)
      valuation         15%

Absent factors are dropped and the remaining weights renormalised, then any
name whose factor coverage falls below MIN_COVERAGE is vetoed outright —
scoring a company on one datapoint out of nine is not scoring it.

Run standalone:
    python ai_longterm.py --dry-run        # rank and print, write nothing
    python ai_longterm.py                  # rank, write to the ledger, alert
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

SIGNAL_TYPE = "ai_longterm"
TIMEFRAME = "LONG"
PICKS = 5

# Signal types that measure a trade. Anything outside this set must not reach
# expectancy, win rate or R statistics. Mirrored in vercel-news/api/stats.js.
EXCLUDE_FROM_EXPECTANCY = (SIGNAL_TYPE,)

# ── hard vetoes ──────────────────────────────────────────────────────────────
MIN_MARKET_CAP_CR = 2000.0     # below this, liquidity and disclosure both thin
MAX_PE = 60.0
MIN_ROE = 0.12                 # 12% — below the cost of equity, compounding stalls
MAX_DEBT_TO_EQUITY = 1.5       # waived for lenders, where leverage IS the model
MIN_COVERAGE = 0.55            # fraction of the model that must have data
MIN_HISTORY_DAYS = 260         # need a year of price to judge a trend

# Publish fewer than five rather than pad the list. A 53/100 name with falling
# earnings is not a two-year conviction just because it came fifth — the whole
# reason the v2 gate exists on this site is that quiet beats filler.
MIN_SCORE = 60.0

_LENDERS = {"Financial Services", "Financials", "Real Estate"}

FUND_W = {"capital": 0.30, "growth": 0.25, "margins": 0.15,
          "balance": 0.15, "valuation": 0.15}
TECH_W = {"trend": 0.40, "strength": 0.30, "drawdown": 0.20, "participation": 0.10}
FUND_WEIGHT, TECH_WEIGHT = 0.70, 0.30


def _clamp01(v) -> float:
    return 0.0 if v < 0 else 1.0 if v > 1 else float(v)


def _finite(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


# ── fundamentals ─────────────────────────────────────────────────────────────

def _veto(f: dict) -> str | None:
    """Reason this company is not investable, or None."""
    mc = _finite(f.get("market_cap_cr"))
    if mc is None or mc < MIN_MARKET_CAP_CR:
        return f"market cap {'unknown' if mc is None else f'₹{mc:,.0f}cr'} < ₹{MIN_MARKET_CAP_CR:,.0f}cr"
    pat = _finite(f.get("net_income"))
    if pat is not None and pat <= 0:
        return "loss-making on trailing PAT"
    pe = _finite(f.get("pe"))
    if pe is None or pe <= 0:
        return "no meaningful PE"
    if pe > MAX_PE:
        return f"PE {pe:.0f}x > {MAX_PE:.0f}x"
    roe = _finite(f.get("roe"))
    if roe is None or roe < MIN_ROE:
        return f"ROE {'unknown' if roe is None else f'{roe:.0%}'} < {MIN_ROE:.0%}"
    dte = _finite(f.get("debt_to_equity"))
    if f.get("sector") not in _LENDERS and dte is not None and dte > MAX_DEBT_TO_EQUITY:
        return f"D/E {dte:.2f} > {MAX_DEBT_TO_EQUITY}"
    return None


def _fundamental_score(f: dict, pe_population: list) -> tuple[float, float, list]:
    """(score 0-1, coverage 0-1, human-readable notes)."""
    parts, notes = {}, []

    # Return on capital. ROE is the headline; ROA is the check on whether that
    # ROE is earned or borrowed. Averaged when both are present.
    roe, roa = _finite(f.get("roe")), _finite(f.get("return_on_assets"))
    cap = [v for v in (roe and _clamp01(roe / 0.30),
                       roa and _clamp01(roa / 0.15)) if v is not None]
    parts["capital"] = (sum(cap) / len(cap), True) if cap else (0.0, False)
    if roe is not None:
        notes.append(f"ROE {roe:.0%}")
    if roa is not None:
        notes.append(f"ROA {roa:.0%}")

    rev, eps = _finite(f.get("revenue_growth")), _finite(f.get("earnings_growth"))
    g = [v for v in (rev, eps) if v is not None]
    parts["growth"] = (_clamp01((sum(g) / len(g)) / 0.20), True) if g else (0.0, False)
    if rev is not None:
        notes.append(f"rev {rev:+.0%}")
    if eps is not None:
        notes.append(f"PAT {eps:+.0%}")

    pm, om = _finite(f.get("profit_margin")), _finite(f.get("operating_margin"))
    m = [v for v in (pm and _clamp01(pm / 0.20),
                     om and _clamp01(om / 0.25)) if v is not None]
    parts["margins"] = (sum(m) / len(m), True) if m else (0.0, False)
    if pm is not None:
        notes.append(f"margin {pm:.0%}")

    dte = _finite(f.get("debt_to_equity"))
    if f.get("sector") in _LENDERS:
        parts["balance"] = (0.5, True)          # deliberate, not missing
        notes.append("lender — D/E n/a")
    elif dte is None:
        parts["balance"] = (0.0, False)
    else:
        fcf = _finite(f.get("free_cashflow"))
        base = _clamp01(1 - dte / MAX_DEBT_TO_EQUITY)
        # Free cash flow is what actually services debt, so it modulates the
        # leverage score rather than sitting in its own bucket.
        parts["balance"] = (min(1.0, base * (1.15 if (fcf or 0) > 0 else 0.85)), True)
        notes.append(f"D/E {dte:.2f}")
        if fcf is not None:
            notes.append("FCF+" if fcf > 0 else "FCF−")

    pe = _finite(f.get("pe"))
    if pe is not None and pe_population:
        cheaper = sum(1 for p in pe_population if p > pe)
        parts["valuation"] = (cheaper / len(pe_population), True)
        notes.append(f"PE {pe:.0f}x")
    else:
        parts["valuation"] = (0.0, False)

    avail = sum(FUND_W[k] for k, (_v, ok) in parts.items() if ok)
    score = (sum(v * FUND_W[k] for k, (v, ok) in parts.items() if ok) / avail
             if avail > 0 else 0.0)
    return score, avail, notes


# ── technicals ───────────────────────────────────────────────────────────────

def _technical_score(hist) -> tuple[float | None, dict]:
    """Trend quality over a year of daily closes. None when history is short."""
    if hist is None or len(hist) < MIN_HISTORY_DAYS:
        return None, {}
    close = hist["Close"].squeeze()
    vol = hist["Volume"].squeeze()
    px = float(close.iloc[-1])

    dma50 = float(close.rolling(50).mean().iloc[-1])
    dma200 = float(close.rolling(200).mean().iloc[-1])
    dma200_prev = float(close.rolling(200).mean().iloc[-22])   # a month ago
    hi52 = float(close.tail(252).max())
    mom6 = (px / float(close.iloc[-126]) - 1) if len(close) >= 126 else 0.0
    mom12 = (px / float(close.iloc[-252]) - 1) if len(close) >= 252 else 0.0
    drawdown = (px / hi52 - 1) if hi52 else -1.0

    # Trend: above both averages, with the long one still rising. A 200DMA
    # that has rolled over is the single most reliable "not yet" in this file.
    trend = 0.0
    if px > dma200:
        trend += 0.45
    if px > dma50:
        trend += 0.20
    if dma50 > dma200:
        trend += 0.20
    if dma200 > dma200_prev:
        trend += 0.15

    strength = _clamp01((0.5 * mom6 + 0.5 * mom12 + 0.15) / 0.55)
    # Within 12% of the 52-week high scores full; 40% below scores zero.
    dd = _clamp01((drawdown + 0.40) / 0.28)
    v_recent = float(vol.tail(60).mean() or 0)
    v_base = float(vol.tail(250).mean() or 1)
    participation = _clamp01((v_recent / v_base - 0.7) / 0.6) if v_base else 0.0

    parts = {"trend": trend, "strength": strength,
             "drawdown": dd, "participation": participation}
    score = sum(parts[k] * TECH_W[k] for k in TECH_W)
    facts = {"price": round(px, 2), "dma50": round(dma50, 2), "dma200": round(dma200, 2),
             "hi52": round(hi52, 2), "mom6": round(mom6 * 100, 1),
             "mom12": round(mom12 * 100, 1), "drawdown": round(drawdown * 100, 1),
             "above_200dma": px > dma200, "dma200_rising": dma200 > dma200_prev}
    return score, facts


def _history(symbol: str):
    try:
        import yfinance as yf
        from symbols import to_yahoo
        h = yf.download(to_yahoo(symbol), period="2y", interval="1d",
                        progress=False, auto_adjust=True)
        return h if h is not None and not h.empty else None
    except Exception as e:
        log.debug(f"ai_longterm history {symbol}: {e}")
        return None


# ── ranking ──────────────────────────────────────────────────────────────────

def rank(symbols=None, limit_universe: int | None = None,
         technical_shortlist: int = 45) -> list[dict]:
    """Screen the universe and return candidates best-first."""
    import fundamentals as fnd

    if symbols is None:
        from signals.universe import load_nifty500
        symbols = [s.replace(".NS", "") for s in load_nifty500()]
    if limit_universe:
        symbols = symbols[:limit_universe]

    hits, tried = fnd.prefetch(symbols)
    log.info(f"ai_longterm: fundamentals {hits}/{tried}")

    rows, pes = [], []
    for s in symbols:
        f = fnd.get(s, allow_fetch=False)
        if not f:
            continue
        why = _veto(f)
        if why:
            log.debug(f"ai_longterm veto {s}: {why}")
            continue
        rows.append(f)
        pes.append(_finite(f.get("pe")))
    pes = [p for p in pes if p]
    log.info(f"ai_longterm: {len(rows)} of {len(symbols)} cleared the business screen")

    scored = []
    for f in rows:
        fs, cov, notes = _fundamental_score(f, pes)
        if cov < MIN_COVERAGE:
            log.debug(f"ai_longterm veto {f['symbol']}: {cov:.0%} factor coverage")
            continue
        scored.append({"symbol": f["symbol"], "sector": f.get("sector") or "—",
                       "fund_score": fs, "coverage": cov, "notes": notes,
                       "pe": _finite(f.get("pe")), "roe": _finite(f.get("roe"))})

    # Price history is the expensive call, so only the fundamentally best names
    # are charted. A company that fails the business screen does not get a
    # second chance for looking good on a chart — that ordering is the whole
    # point of a long-horizon engine.
    scored.sort(key=lambda x: -x["fund_score"])
    out = []
    for c in scored[:technical_shortlist]:
        ts, facts = _technical_score(_history(c["symbol"]))
        if ts is None:
            continue
        if not facts.get("above_200dma") or not facts.get("dma200_rising"):
            log.debug(f"ai_longterm skip {c['symbol']}: 200DMA structure")
            continue
        c["tech_score"] = ts
        c["facts"] = facts
        c["score"] = round((FUND_WEIGHT * c["fund_score"] + TECH_WEIGHT * ts) * 100, 1)
        c["rationale"] = " · ".join(c["notes"])
        out.append(c)

    out.sort(key=lambda x: -x["score"])
    log.info(f"ai_longterm: {len(out)} passed both screens")
    return out


def _one_per_sector(cands: list[dict], n: int) -> list[dict]:
    """Five names from five sectors. Concentration is a decision, not a default."""
    picks, seen = [], set()
    for c in cands:
        if c["sector"] in seen and len(picks) < n:
            continue
        picks.append(c)
        seen.add(c["sector"])
        if len(picks) >= n:
            break
    # No backfill. Repeating a sector to reach five would undo the only
    # diversification rule here, and every candidate has already cleared the
    # score floor — so a short list means the market is short of ideas, which
    # is information rather than a gap to paper over.
    return picks[:n]


def _levels(c: dict) -> dict:
    """Entry, a structural stop, and valuation-anchored targets.

    A long-horizon idea does not carry an intraday stop. The stop here is the
    200-day average less a buffer — the level at which the reason for owning it
    (an intact uptrend in a compounding business) has stopped being true.
    """
    f = c["facts"]
    px, dma200 = f["price"], f["dma200"]
    sl = round(min(dma200 * 0.92, px * 0.80), 2)
    return {"entry": px, "sl": sl,
            "t1": round(px * 1.35, 2), "t2": round(px * 1.75, 2),
            "t3": round(px * 2.50, 2),
            "rr": round((px * 1.75 - px) / max(px - sl, 0.01), 2)}


# ── AI thesis ────────────────────────────────────────────────────────────────

_THESIS_PROMPT = (
    "You are a buy-side analyst writing for a chartered accountant who reads "
    "numbers first and dislikes filler. In 2 sentences, under 45 words, state "
    "the investment case for {sym} ({sector}) over a 2-3 year horizon. "
    "Use only these measured facts: {facts}. Short sentences. No hedging, no "
    "adjectives like 'robust' or 'strong tailwinds', no disclaimer."
)


def _thesis(c: dict) -> str:
    """One paragraph from the LLM. Falls back to the measured facts themselves.

    Written after ranking, and never fed back into it — the model describes the
    decision, it does not make it.
    """
    facts = (f"{c['rationale']}; price {c['facts']['price']}, "
             f"6m {c['facts']['mom6']:+.0f}%, 12m {c['facts']['mom12']:+.0f}%, "
             f"{abs(c['facts']['drawdown']):.0f}% below 52w high")
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        return facts
    try:
        from groq import Groq
        r = Groq(api_key=key).chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": _THESIS_PROMPT.format(
                sym=c["symbol"], sector=c["sector"], facts=facts)}],
            max_tokens=110, temperature=0.4,
        )
        txt = (r.choices[0].message.content or "").strip()
        return txt or facts
    except Exception as e:
        log.warning(f"ai_longterm thesis {c['symbol']}: {e}")
        return facts


# ── build ────────────────────────────────────────────────────────────────────

def build(n: int = PICKS, dry_run: bool = False, candidates=None) -> list[dict]:
    """Rank, pick, write to the ledger. Returns the picks."""
    cands = candidates if candidates is not None else rank()
    if not cands:
        log.warning("ai_longterm: nothing cleared the screens")
        return []

    strong = [c for c in cands if c["score"] >= MIN_SCORE]
    if len(strong) < len(cands):
        log.info(f"ai_longterm: dropped {len(cands) - len(strong)} below "
                 f"{MIN_SCORE:.0f}/100")
    picks = _one_per_sector(strong, n)
    for i, c in enumerate(picks):
        c["rank"] = i + 1
        c.update(_levels(c))
        c["thesis"] = _thesis(c)

    if dry_run:
        return picks

    # One batch per day. The weekly Saturday scan and any on-demand run both
    # stamp today, so a second run appended a second five and the section
    # rendered ten cards for seven companies. The renderer dedupes defensively,
    # but the ledger should not hold the duplicates in the first place —
    # anything reading it directly would double-count.
    try:
        import db as _db
        today = datetime.now(IST).date().isoformat()
        with _db.connect() as c:
            n = c.execute(
                "SELECT COUNT(*) FROM all_signals WHERE signal_type=? AND date=?",
                (SIGNAL_TYPE, today)).fetchone()[0]
            if n:
                c.execute("DELETE FROM all_signals WHERE signal_type=? AND date=?",
                          (SIGNAL_TYPE, today))
                c.commit()
                _db.sync(c)
                log.info(f"ai_longterm: replaced {n} rows already written today")
    except Exception as e:
        log.warning(f"ai_longterm: could not clear today's rows — {e}")

    from tracker import log_batch_to_all_signals
    rows = [{
        "symbol": c["symbol"], "signal_type": SIGNAL_TYPE, "action": "BUY",
        "timeframe": TIMEFRAME, "entry": c["entry"], "sl": c["sl"],
        "t1": c["t1"], "t2": c["t2"], "t3": c["t3"], "rr": c["rr"],
        "score": c["score"], "grade": _grade(c["score"]),
        "metadata": {
            "engine": "ai_longterm", "horizon": "2-3 years",
            "generated": "AI narrative, measured selection",
            "fund_score": round(c["fund_score"] * 100, 1),
            "tech_score": round(c["tech_score"] * 100, 1),
            "coverage": round(c["coverage"] * 100),
            "sector": c["sector"], "rationale": c["rationale"],
            "thesis": c["thesis"], "facts": c["facts"],
        },
    } for c in picks]
    ids = log_batch_to_all_signals(rows)
    log.info(f"ai_longterm: wrote {len(ids)} picks to the ledger "
             f"({', '.join(c['symbol'] for c in picks)})")
    return picks


def _grade(score: float) -> str:
    return "A+" if score >= 80 else "A" if score >= 70 else "B" if score >= 60 else "C"


def to_telegram(picks: list[dict]) -> str:
    if not picks:
        return "🧠 *AI Long-Term Picks* — nothing cleared the screens this week."
    ist = datetime.now(IST).strftime("%d %b %Y")
    out = [f"🧠 *AI Long-Term Picks* ({len(picks)}) — {ist}",
           "_Business first, chart second · 2–3 year horizon_", ""]
    for c in picks:
        out.append(
            f"*{c['rank']}. {c['symbol']}* · {c['sector']}  `{c['score']}/100` {_grade(c['score'])}\n"
            f"Entry ₹{c['entry']:,.2f} · SL ₹{c['sl']:,.2f} (200DMA structure)\n"
            f"T1 ₹{c['t1']:,.2f} · T2 ₹{c['t2']:,.2f} · T3 ₹{c['t3']:,.2f}\n"
            f"_{c['rationale']}_\n"
            f"{c['thesis']}\n")
    out.append("_Selection is measured. The wording is AI. Not SEBI advice._")
    return "\n".join(out)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap the universe (for a fast local run)")
    ap.add_argument("--send", action="store_true", help="post to Telegram")
    a = ap.parse_args()

    cands = rank(limit_universe=a.limit)
    picks = build(dry_run=a.dry_run, candidates=cands)
    print(to_telegram(picks))
    if a.send and picks:
        from telegram_bot import _post
        _post(to_telegram(picks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
