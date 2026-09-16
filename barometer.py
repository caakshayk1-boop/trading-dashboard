#!/usr/bin/env python3
"""barometer.py — the market reading, recorded daily and scored afterwards.

Akshay asked what smart feature was missing. This is it, and the argument for
it is the same one the whole site is built on:

    Every SIGNAL here is scored when it closes, losses included. The barometer
    says "genuinely cheap" or "nothing on sale" and NOTHING EVER CHECKS WHETHER
    IT WAS RIGHT. It was the one claim on the site with no accountability
    behind it — which is precisely the thing this site exists not to do.

So the reading is written down every day, and once three, six and twelve months
have passed, what the index actually DID after each reading is written down
beside it. Within a year the page can say "when this read 'genuinely cheap',
the index was up X% a year later, over N readings" — or that it was not, which
is the more valuable answer and the one a framework like this usually buries.

────────────────────────────────────────────────────────────────────────────
ONE DEFINITION OF THE SCORE, AND THIS FILE IS IT
────────────────────────────────────────────────────────────────────────────
The barometer was first written in signal.js, computed in the browser. Keeping
a Python copy for the history would mean two implementations of one formula,
drifting apart the first time either is touched — the exact fault this estate
has been bitten by repeatedly (four engines each with their own target ladder;
a null guard fixed in one of three places).

So this file owns it. It publishes docs/barometer.json carrying today's score,
its components, and the history; the website RENDERS that file rather than
recomputing anything. The client keeps a fallback for the day before this job
first runs, and that fallback is the only copy of the arithmetic that still
exists anywhere else.

WEIGHTS ARE PUBLISHED, not hidden, for the reason the page states: a score
whose derivation you cannot see is a horoscope.
"""
from __future__ import annotations

import json
import logging
import pathlib
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger("barometer")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ROOT = pathlib.Path(__file__).resolve().parent
PULSE = ROOT / "docs" / "pulse.json"
OUT = ROOT / "docs" / "barometer.json"

WEIGHTS = {"trend": 30, "breadth": 30, "participation": 15,
           "volatility": 15, "highs": 10}

# The windows the outcome is measured over. Trading days, not calendar days,
# because the index only moves on the days it trades.
HORIZONS = {"m3": 63, "m6": 126, "m12": 252}


def _n(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def vix_score(v: float) -> int:
    """Banded, not scaled. 11 and 13 are the same market; 13 and 30 are not,
    and a linear stretch between two arbitrary ends would say otherwise."""
    return 85 if v < 12 else 70 if v < 16 else 50 if v < 20 else 28 if v < 26 else 10


def vix_word(v: float) -> str:
    return ("very calm" if v < 12 else "normal" if v < 16 else
            "unsettled" if v < 20 else "jumpy" if v < 26 else "stressed")


def band_of(score: int) -> dict:
    if score >= 70: return {"k": "strong", "t": "Strong", "c": "up"}
    if score >= 55: return {"k": "firm", "t": "Firm", "c": "up"}
    if score >= 40: return {"k": "mixed", "t": "Mixed", "c": ""}
    if score >= 25: return {"k": "weak", "t": "Weak", "c": "dn"}
    return {"k": "poor", "t": "Poor", "c": "dn"}


def stage_of(drawdown: float, above_pct: float) -> dict:
    """What the fall has actually put on offer — the second reading, which
    moves AGAINST the first by design. A barometer alone calls conditions poor
    at exactly the moment this should be saying it is the entry."""
    if drawdown >= 25 and above_pct <= 25:
        return {"k": "deep", "t": "Genuinely cheap", "c": "up"}
    if drawdown >= 15 and above_pct <= 40:
        return {"k": "real", "t": "Worth buying in instalments", "c": "up"}
    if drawdown >= 10:
        return {"k": "early", "t": "Slightly cheaper", "c": ""}
    return {"k": "none", "t": "Nothing on sale", "c": ""}


def compute(nifty_close, nifty_hi, nifty_lo, breadth: dict, vix) -> dict | None:
    counted = _n(breadth.get("counted"))
    if not counted:
        return None
    c, hi, lo, v = (_n(nifty_close), _n(nifty_hi), _n(nifty_lo), _n(vix))
    parts = []

    if None not in (c, hi, lo) and hi > lo:
        pos = (c - lo) / (hi - lo) * 100
        parts.append({"key": "trend", "label": "Where the index sits",
                      "score": round(pos, 1), "weight": WEIGHTS["trend"],
                      "detail": f"Nifty is {pos:.0f}% up its own 52-week range, "
                                f"{abs((c - hi) / hi * 100):.1f}% below the high"})
    above = _n(breadth.get("above_200dma"))
    above_pct = None if above is None else above / counted * 100
    if above_pct is not None:
        parts.append({"key": "breadth", "label": "Names above their 200-day",
                      "score": round(above_pct, 1), "weight": WEIGHTS["breadth"],
                      "detail": f"{int(above)} of {int(counted)} hold their 200-day average"})
    up = _n(breadth.get("up"))
    if up is not None:
        parts.append({"key": "participation", "label": "Advancing today",
                      "score": round(up / counted * 100, 1), "weight": WEIGHTS["participation"],
                      "detail": f"{int(up)} of {int(counted)} advanced"})
    if v is not None:
        parts.append({"key": "volatility", "label": "Volatility",
                      "score": vix_score(v), "weight": WEIGHTS["volatility"],
                      "detail": f"India VIX at {v:.2f} — {vix_word(v)}"})
    hi52 = _n(breadth.get("at_52w_high"))
    if hi52 is not None:
        # Capped: 5% of names at a one-year high is already broad, so the
        # scale tops out there rather than at an unreachable 100%.
        parts.append({"key": "highs", "label": "Names at a 52-week high",
                      "score": round(min(100.0, hi52 / counted * 100 / 5 * 100), 1),
                      "weight": WEIGHTS["highs"],
                      "detail": f"{int(hi52)} of {int(counted)} at a 52-week high"})

    wsum = sum(p["weight"] for p in parts)
    if not wsum:
        return None
    score = round(sum(p["score"] * p["weight"] for p in parts) / wsum)
    dd = None if None in (c, hi) else abs((c - hi) / hi * 100)
    stage = stage_of(dd, above_pct) if (dd is not None and above_pct is not None) else None
    return {"score": score, "band": band_of(score), "parts": parts,
            "stage": stage, "counted": int(counted),
            "nifty": c, "drawdown_pct": None if dd is None else round(dd, 2),
            "above_200dma_pct": None if above_pct is None else round(above_pct, 1),
            "vix": v}


def outcomes(history: list[dict], closes: dict[str, float]) -> dict:
    """What the index DID after each past reading, grouped by what it said.

    ONLY WHERE THE FUTURE HAS ACTUALLY HAPPENED. A reading from last week has
    no three-month outcome and is left out of the count rather than filled with
    a partial one — a running total that silently mixes horizons is how a
    framework flatters itself.
    """
    dates = sorted(closes)
    idx = {d: i for i, d in enumerate(dates)}
    by_stage: dict[str, dict] = {}

    for row in history:
        st = (row.get("stage") or {}).get("k")
        d = row.get("date")
        if not st or d not in idx:
            continue
        start = closes[d]
        if not start:
            continue
        bucket = by_stage.setdefault(st, {"label": (row.get("stage") or {}).get("t", st),
                                          "readings": 0, "m3": [], "m6": [], "m12": []})
        bucket["readings"] += 1
        for key, bars in HORIZONS.items():
            j = idx[d] + bars
            if j < len(dates):
                bucket[key].append((closes[dates[j]] - start) / start * 100)

    out = {}
    for st, b in by_stage.items():
        row = {"label": b["label"], "readings": b["readings"]}
        for key in HORIZONS:
            vals = b[key]
            row[key] = None if not vals else {
                "n": len(vals),
                "avg": round(sum(vals) / len(vals), 1),
                "hit": round(sum(1 for x in vals if x > 0) / len(vals) * 100),
            }
        out[st] = row
    return out


def main() -> int:
    import yfinance as yf

    if not PULSE.exists():
        log.error("docs/pulse.json is missing — breadth is half the score")
        return 1
    breadth = (json.loads(PULSE.read_text()) or {}).get("breadth") or {}

    nif = yf.Ticker("^NSEI").history(period="2y", interval="1d", auto_adjust=False)
    vixh = yf.Ticker("^INDIAVIX").history(period="5d", interval="1d", auto_adjust=False)
    if nif.empty:
        log.error("no Nifty history — cannot score")
        return 1

    last = nif.iloc[-1]
    yr = nif.tail(252)
    today = compute(float(last["Close"]), float(yr["High"].max()),
                    float(yr["Low"].min()), breadth,
                    None if vixh.empty else float(vixh["Close"].iloc[-1]))
    if not today:
        log.error("could not compute a score")
        return 1

    prev = {}
    if OUT.exists():
        try:
            prev = json.loads(OUT.read_text()) or {}
        except Exception as e:                                   # noqa: BLE001
            log.warning(f"existing file unreadable ({e}) — starting a new history")

    history = [h for h in (prev.get("history") or []) if h.get("date")]
    stamp = str(date.today())
    history = [h for h in history if h["date"] != stamp]        # one row per day
    history.append({"date": stamp, "score": today["score"],
                    "band": today["band"]["k"], "stage": today["stage"],
                    "nifty": today["nifty"],
                    "drawdown_pct": today["drawdown_pct"],
                    "above_200dma_pct": today["above_200dma_pct"],
                    "vix": today["vix"]})
    history.sort(key=lambda h: h["date"])

    closes = {d.strftime("%Y-%m-%d"): float(c)
              for d, c in zip(nif.index, nif["Close"]) if c == c}
    scored = outcomes(history, closes)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "weights": WEIGHTS,
        "today": today,
        "history": history[-800:],        # a little over three years of readings
        "outcomes": scored,
        "horizons": {"m3": "3 months", "m6": "6 months", "m12": "12 months"},
    }, indent=2), encoding="utf-8")

    log.info(f"{stamp}: {today['score']}/100 {today['band']['t']} · "
             f"{(today['stage'] or {}).get('t')} · history {len(history)} rows")
    for st, row in scored.items():
        done = [f"{k} n={row[k]['n']} avg{row[k]['avg']:+.1f}%" for k in HORIZONS if row.get(k)]
        log.info(f"  {row['label']}: {row['readings']} readings" +
                 (" · " + ", ".join(done) if done else " · no outcome yet"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
