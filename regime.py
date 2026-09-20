#!/usr/bin/env python3
"""regime.py — what kind of market this is, and what the ledger says about it.

Akshay sent a Reddit "DD" and a paper on a regime-adaptive trading system and
asked what could be taken from them. Most of it cannot: Avellaneda-Stoikov is a
market-making model and this book is long-only swing; OFI and VPIN need tick
data this estate does not have and says so in signals/pivot.py; microsecond
latency is not a gap a Cloudflare Worker reading Yahoo can close. The post also
plugs a product in its first line and its own small print reads "illustrative
architecture parameters, not measured performance".

ONE IDEA IS WORTH TAKING, and this file is it: classify the REGIME, then let
the regime decide what to trust. The site already scores the market 0-100 and
names a stage; neither maps to what should actually change.

────────────────────────────────────────────────────────────────────────────
THE DIFFERENCE BETWEEN THIS AND THE REFERENCE
────────────────────────────────────────────────────────────────────────────
The reference ASSERTS its mapping — calm 1.0, trending 0.7, crisis 3.0, with
no sample behind any of it. This file refuses to assert one. It labels every
past day with the regime that prevailed, joins those labels to the closed
trades on their ENTRY date, and reports what each engine actually did in each
regime, with the sample size beside every figure.

Where a bucket is too thin to mean anything, it says so and prescribes
NOTHING. That is the expected outcome for most cells today: the dated ledger
runs from 2026-06-03, one engine is more than half of it, and five regimes
across three months of trades leaves cells with single-digit counts. A regime
table full of confident multipliers built on n=4 would be the most dangerous
page on this site.

────────────────────────────────────────────────────────────────────────────
THE CLASSIFIER USES ONLY WHAT IS HISTORICAL
────────────────────────────────────────────────────────────────────────────
Trend and realised volatility can be computed for any past day from one price
series. BREADTH CANNOT — pulse.json holds today's count and no history, which
barometer.py already records. So breadth is deliberately NOT an input: a
classifier that needs it could label today and never label yesterday, and a
regime you cannot backfill is a regime you cannot measure against the ledger.

Every window is trailing. The volatility percentile is measured against the
two years BEFORE each day, never against the whole series, or the label for
2026-06 would depend on prices from 2026-09.
"""
from __future__ import annotations

import json
import logging
import math
import pathlib
import statistics
from datetime import date, datetime, timezone
from engine_names import LIVE as _LIVE, RETIRED as _RETIRED

log = logging.getLogger("regime")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ROOT = pathlib.Path(__file__).resolve().parent
LEDGER = ROOT / "data" / "all_signals.json"
PULSE = ROOT / "docs" / "pulse.json"
OUT = ROOT / "docs" / "regime.json"

# Matches tracker.py's EXCLUDE_FROM_EXPECTANCY. Research and allocation
# artefacts are not trades and must not enter an expectancy figure.
# ── WHAT COUNTS AS THIS BOOK ─────────────────────────────────────────────────
#
# Was a hand-written EXCLUDE = {"multibagger", "top5_pick", "sip_bucket"}, and
# it excluded MULTIBAGGER — ASCENT — which is an engine this site publishes.
# So the regime panel was reporting a book with one of the site's own engines
# missing from it, beside a record that included it.
#
# Now derived from engine_names.LIVE, the same map signal.js is held to by
# test_engine_names.py. An engine is in this table exactly when the site
# publishes it; there is no second opinion to keep in sync.
INCLUDE = set(_LIVE)

# ── ENGINES THAT NO LONGER RUN ──────────────────────────────────────────────
# tracker.py's description map is the authority. `intraday` is NOT in here any
# more: it was brought back on 2026-09-17 as the tenth engine, at RESEARCH.
#
# It was switched off with the rest of the intraday tier on a measurement of
# that TIER — -0.005R over 583 trades against +0.171R on daily closes. That was
# right about the tier and wrong about this engine inside it: on its own
# seventeen closed trades it reads +1.472R at t=3.69, the best record on the
# board. It was retired for the company it kept.
#
# The other three stay off and are EXCLUDED from the table, because a reader
# looking at "what does this book do in this market" should see what it does,
# not a record of three things it stopped doing. That is the opposite call from
# the one made an hour earlier, and the difference is that `intraday` is now a
# live engine rather than a retired one being smuggled back in by a label.
#
# THIS LIST IS NO LONGER KEPT HERE. It was, and it went stale: on 2026-09-19
# it still named only the four intraday-era keys above while signal.js had
# retired magic, equity_measured and ai_longterm. The regime panel and the
# record beside it were therefore reporting two different populations, 11
# closed against 12, and a reader could see 9.1% and 8.3% on one screen with
# nothing to say which was this book's.
#
# engine_names.RETIRED is the one Python copy, and test_engine_names.py
# asserts it equals the JS registry in both directions.
RETIRED = {k: f"retired {v}" for k, v in _RETIRED.items()}

# ── THIS SITE'S OWN RECORD STARTS HERE ──────────────────────────────────────
# Akshay: "this also shows all from total, show only related to signal site."
#
# The ledger carries trades published by news.askakshay.com before this site
# existed, under stops this site has since said were wrong, on a ledger that
# has been re-graded twice. Every other surface counts from LAUNCH and this one
# was counting from the beginning of the file, so the regime table was reporting
# a bigger, older and different population than the record beside it.
#
# It costs almost the whole sample and that is the point: a number this site is
# accountable for, or no number.
LAUNCH = "2026-09-02"

# A cell needs this many closed trades before the page will read anything into
# it. Deliberately low as a FLOOR for showing a number at all, and far below
# the 30-at-t>=2 bar the site uses before trusting one.
MIN_CELL = 8
TRUST_N = 30
TRUST_T = 2.0

VOL_HIGH_PCTILE = 70      # above this, the market is "high volatility" for itself
VOL_CRISIS_PCTILE = 85
CRISIS_DD = 15.0          # per cent off the trailing 252-day high

REGIMES = {
    "calm_trend":   {"t": "Calm and trending",
                     "d": "Volatility below its own norm and the index above its 200-day. "
                          "The condition trend-following was designed for."},
    "calm_range":   {"t": "Calm and going nowhere",
                     "d": "Quiet, but with no trend to follow. Breakouts fail most often "
                          "here, because there is nothing behind them."},
    "highvol_trend": {"t": "Volatile but still trending",
                      "d": "Wide ranges with the long trend intact. Stops set for a calm "
                           "market get hit by noise in this one."},
    "highvol_mr":   {"t": "Volatile and broken",
                     "d": "Wide ranges with the index below its 200-day. Moves reverse; "
                          "momentum entries are at their worst here."},
    "crisis":       {"t": "Dislocation",
                     "d": "Extreme volatility and a deep drawdown together. Historically "
                          "where the worst trades and the best entries share a week."},
}


def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if x != x else x


def classify(close, sma50, sma200, vol_pctile, drawdown) -> str:
    """One label from four trailing measurements. Deterministic and ordered:
    crisis is tested first because it is a conjunction the others would each
    partially match."""
    if drawdown is not None and vol_pctile is not None \
            and drawdown >= CRISIS_DD and vol_pctile >= VOL_CRISIS_PCTILE:
        return "crisis"
    high_vol = vol_pctile is not None and vol_pctile >= VOL_HIGH_PCTILE
    above = close is not None and sma200 is not None and close > sma200
    if high_vol:
        return "highvol_trend" if above else "highvol_mr"
    stacked = above and sma50 is not None and sma200 is not None and sma50 > sma200
    return "calm_trend" if stacked else "calm_range"


def label_history(closes: list[tuple[str, float]]) -> dict[str, dict]:
    """Every day in the series, labelled with the regime prevailing THAT day.

    NO LOOKAHEAD ANYWHERE. Each day's volatility percentile is its 20-day
    realised volatility ranked against the 504 trading days before it, so the
    label for a day in June cannot move when September's prices arrive. The
    first 504 days therefore carry no label at all rather than a label built on
    a shorter and differently-scaled window.
    """
    out: dict[str, dict] = {}
    px = [c for _d, c in closes]
    rets = [None] + [math.log(px[i] / px[i - 1]) if px[i - 1] > 0 else None
                     for i in range(1, len(px))]

    vols: list[float | None] = [None] * len(px)
    for i in range(len(px)):
        w = [r for r in rets[max(0, i - 19): i + 1] if r is not None]
        vols[i] = statistics.pstdev(w) * math.sqrt(252) * 100 if len(w) >= 15 else None

    for i, (d, c) in enumerate(closes):
        if i < 504:
            continue
        hist = [v for v in vols[i - 504:i] if v is not None]
        v = vols[i]
        pct = None
        if v is not None and len(hist) >= 250:
            pct = sum(1 for x in hist if x <= v) / len(hist) * 100
        s50 = statistics.fmean(px[i - 49:i + 1]) if i >= 49 else None
        s200 = statistics.fmean(px[i - 199:i + 1]) if i >= 199 else None
        yr = px[max(0, i - 251):i + 1]
        dd = (max(yr) - c) / max(yr) * 100 if yr and max(yr) > 0 else None
        out[d] = {"regime": classify(c, s50, s200, pct, dd),
                  "close": round(c, 2),
                  "vol_ann_pct": None if v is None else round(v, 1),
                  "vol_pctile": None if pct is None else round(pct, 1),
                  "drawdown_pct": None if dd is None else round(dd, 2),
                  "above_200dma": None if s200 is None else bool(c > s200)}
    return out


def t_stat(vals: list[float]) -> float | None:
    """Mean over standard error. Below two observations there is no error to
    divide by, and a single trade has no t-statistic however good it looked."""
    if len(vals) < 2:
        return None
    sd = statistics.stdev(vals)
    if sd == 0:
        return None
    return statistics.fmean(vals) / (sd / math.sqrt(len(vals)))


def _prevailing(d: str, labels: dict[str, dict]):
    """The regime in force on a given date, including days the market was shut.

    149 of the closed trades carried a SATURDAY or SUNDAY entry date and were
    being dropped as unlabelled — the weekend scan publishes for Monday, and
    the NSE has no bar for a Saturday, so there was no label to join to. The
    conditions those decisions were taken under are Friday's, which is exactly
    what walking back to the last session gives.

    Bounded at five days so a genuinely bad date cannot silently inherit a
    label from a week earlier; beyond that it stays unlabelled and is counted.
    """
    if not d:
        return None
    hit = labels.get(d)
    if hit:
        return hit
    try:
        cur = date.fromisoformat(d)
    except ValueError:
        return None
    for back in range(1, 6):
        hit = labels.get((cur - __import__("datetime").timedelta(days=back)).isoformat())
        if hit:
            return hit
    return None


def by_regime(rows: list[dict], labels: dict[str, dict]) -> dict:
    """What each engine actually did, split by the regime its trade STARTED in.

    ENTRY DATE, NOT EXIT. The regime is a fact about the conditions a decision
    was taken under; grading it by the exit would credit a regime the trade
    merely ended in, which is the same trade labelled by its outcome.
    """
    cells: dict[str, dict[str, list[float]]] = {}
    overall: dict[str, list[float]] = {}
    unlabelled = 0
    skipped_market = 0
    skipped_retired = 0
    skipped_prelaunch = 0
    skipped_not_published = 0
    skipped_short = 0

    for r in rows:
        eng = str(r.get("signal_type") or "")
        if not eng:
            continue
        if eng in RETIRED:
            skipped_retired += 1
            continue
        if eng not in INCLUDE:
            skipped_not_published += 1
            continue
        # ── AND NOTHING SHORT, FOR THE SAME REASON THE SITE SAYS IT ─────────
        # The book is long-only: standalone_scan's longs_only() states that
        # "nothing short is put in front of a reader as an action", and the
        # site filters shorts out of its published record on the grounds that
        # it will not claim credit for a call nobody received. This table did
        # not, so a short that was never sent counted toward what "this book"
        # did in a regime. Every engine still FILES shorts and the ledger
        # still keeps every one — deleting them would destroy the evidence for
        # whether refusing them costs anything.
        if str(r.get("action") or "BUY").upper() == "SELL":
            skipped_short += 1
            continue
        # ── THE LABEL IS A NIFTY FACT. IT DESCRIBES NIFTY INSTRUMENTS. ──
        #
        # This filter is the difference between a real measurement and a
        # category error, and without it the page's headline was the error.
        # Measured before it went in: calm_range read +0.163R at t=2.52 over
        # 617 trades and looked like a finding. 355 of those were cf_1h, and
        # cf_1h turns out to be 227 COMEX commodity trades and 140 FX pairs —
        # natural gas, silver, crude, USDJPY — and not one Indian equity.
        #
        # Whether the Nifty is calm says nothing about whether natural gas
        # trends. Grouping a EURUSD trade by Indian equity volatility is
        # arithmetic performed on unrelated things, and it was producing the
        # most confident number on the page.
        #
        # 239 of the closed trades are NSE equities. That is the population
        # this label describes, and it is the only one counted here.
        if str(r.get("market") or "").upper() != "NSE" \
                or str(r.get("asset_type") or "").title() != "Equity":
            skipped_market += 1
            continue
        rm = _f(r.get("r_multiple"))
        if rm is None:
            continue
        d = str(r.get("alert_date") or r.get("date") or "")[:10]
        if d < LAUNCH:
            skipped_prelaunch += 1
            continue
        lab = _prevailing(d, labels)
        if not lab:
            unlabelled += 1
            continue
        k = lab["regime"]
        cells.setdefault(k, {}).setdefault(eng, []).append(rm)
        overall.setdefault(k, []).append(rm)

    out = {}
    for k, engines in cells.items():
        vals = overall[k]
        t = t_stat(vals)
        eng_rows = []
        # SORTED ON THE RECORD. Ordering by sample size buried the engine with
        # the best expectancy under four that lose money, purely because they
        # had filed more trades.
        for eng, v in sorted(engines.items(),
                             key=lambda x: -statistics.fmean(x[1])):
            et = t_stat(v)
            eng_rows.append({
                "engine": eng, "n": len(v),
                "retired": RETIRED.get(eng),
                "avg_r": round(statistics.fmean(v), 3),
                "win_rate": round(sum(1 for x in v if x > 0) / len(v) * 100, 1),
                "t": None if et is None else round(et, 2),
                # The site's own bar, applied per cell rather than asserted.
                "trusted": len(v) >= TRUST_N and et is not None and et >= TRUST_T,
                "readable": len(v) >= MIN_CELL,
            })
        out[k] = {
            "n": len(vals),
            "avg_r": round(statistics.fmean(vals), 3),
            "win_rate": round(sum(1 for x in vals if x > 0) / len(vals) * 100, 1),
            "t": None if t is None else round(t, 2),
            "readable": len(vals) >= MIN_CELL,
            "engines": eng_rows,
        }
    return {"cells": out, "unlabelled_trades": unlabelled,
            "excluded_non_nse": skipped_market,
            "excluded_retired": skipped_retired,
            "excluded_not_published": skipped_not_published,
            "excluded_short": skipped_short,
            "excluded_pre_launch": skipped_prelaunch,
            "launch": LAUNCH,
            "retired_engines": sorted(RETIRED),
            "population": "NSE equities only"}


def main() -> int:
    import yfinance as yf

    nif = yf.Ticker("^NSEI").history(period="10y", interval="1d", auto_adjust=False)
    if nif.empty or len(nif) < 600:
        log.error("not enough Nifty history to label a regime")
        return 1
    closes = [(d.strftime("%Y-%m-%d"), float(c))
              for d, c in zip(nif.index, nif["Close"]) if c == c]
    labels = label_history(closes)
    if not labels:
        log.error("nothing could be labelled")
        return 1

    today_key = max(labels)
    today = dict(labels[today_key])
    today["date"] = today_key
    today.update(REGIMES[today["regime"]])

    # Breadth is shown BESIDE the label, never inside it — it has no history,
    # so a classifier using it could not be measured against the ledger.
    breadth = None
    if PULSE.exists():
        try:
            b = (json.loads(PULSE.read_text()) or {}).get("breadth") or {}
            if b.get("counted"):
                breadth = {"above_200dma_pct": round(b.get("above200") or 0, 1),
                           "counted": int(b["counted"]),
                           "note": "Shown for context. Not an input to the label above, "
                                   "because pulse.json keeps no history and a regime that "
                                   "cannot be backfilled cannot be measured."}
        except Exception as e:                                   # noqa: BLE001
            log.warning(f"pulse unreadable ({e})")

    rows = []
    if LEDGER.exists():
        try:
            raw = json.loads(LEDGER.read_text())
            rows = raw if isinstance(raw, list) else (raw.get("signals") or raw.get("rows") or [])
        except Exception as e:                                   # noqa: BLE001
            log.warning(f"ledger unreadable ({e})")
    measured = by_regime(rows, labels)

    # How long each regime has run recently, so a reader can see the label is
    # not flickering day to day.
    recent = [labels[d] | {"date": d} for d in sorted(labels)[-260:]]
    run = 0
    for row in reversed(recent):
        if row["regime"] != today["regime"]:
            break
        run += 1

    spread = {}
    for row in recent:
        spread[row["regime"]] = spread.get(row["regime"], 0) + 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "today": today,
        "run_days": run,
        "last_year": spread,
        "breadth": breadth,
        "regimes": REGIMES,
        "measured": measured,
        "thresholds": {"vol_high_pctile": VOL_HIGH_PCTILE,
                       "vol_crisis_pctile": VOL_CRISIS_PCTILE,
                       "crisis_drawdown_pct": CRISIS_DD,
                       "min_cell": MIN_CELL, "trust_n": TRUST_N, "trust_t": TRUST_T},
        "caveats": [
            "The label uses only trend and realised volatility, because those are the "
            "two things that can be computed for every past day. Breadth cannot be, so "
            "it is shown beside the label and never inside it.",
            "Every window is trailing. A day's volatility percentile is ranked against "
            "the two years before it, so a label cannot change when later prices arrive.",
            "Trades are grouped by the regime they were ENTERED in, not the one they "
            "closed in — the regime is a fact about the decision, not the outcome. A "
            "trade entered on a weekend, when the weekend scan publishes for Monday, "
            "carries the last session's label rather than none.",
            f"Only trades published by THIS site are counted — the record starts "
            f"{LAUNCH}. Everything before it belongs to news.askakshay.com, under stops "
            "this site has since said were wrong, on a ledger re-graded twice. It costs "
            "almost the whole sample, which is the point: a number this site is "
            "accountable for, or no number.",
            "Only engines that still publish are counted. intraday was brought back on "
            "2026-09-17 as the tenth engine at RESEARCH tier — it had been switched off "
            "with the whole intraday tier on that tier's -0.005R over 583 trades, and on "
            "its own seventeen it reads +1.472R at t=3.69. The other three retired "
            "engines stay out: a reader asking what this book does in this market should "
            "see what it does, not three things it stopped doing.",
            "Only NSE EQUITY trades are counted. The label is derived from the Nifty, "
            "and 397 of the closed trades are COMEX commodities or FX pairs — gold, "
            "crude, USDJPY — for which an Indian equity regime says nothing. Including "
            "them made the headline cell read +0.163R at t=2.52; they are the reason it "
            "did.",
            f"A cell needs {MIN_CELL} closed trades before any figure is shown, and the "
            f"site's actual bar for trusting one is {TRUST_N} closed at t >= {TRUST_T}. "
            "Most cells will not clear either, and the page says so rather than "
            "printing a multiplier nothing supports.",
        ],
        "history": [{"date": d, "regime": labels[d]["regime"]} for d in sorted(labels)[-500:]],
    }, indent=2), encoding="utf-8")

    log.info(f"{today_key}: {today['t']} ({today['regime']}), running {run} days")
    log.info(f"  vol {today['vol_ann_pct']}% ann, {today['vol_pctile']}th pctile · "
             f"drawdown {today['drawdown_pct']}%")
    log.info(f"  last 260 sessions: {spread}")
    for k, c in sorted(measured["cells"].items(), key=lambda x: -x[1]["n"]):
        flag = "" if c["readable"] else "  (TOO THIN TO READ)"
        log.info(f"  {k:<14} n={c['n']:<4} avg {c['avg_r']:+.3f}R  "
                 f"t={c['t']}  win {c['win_rate']}%{flag}")
    log.info(f"  {measured['unlabelled_trades']} trades fell outside the labelled window")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
