#!/usr/bin/env python3
"""
THE DAILY SIGNAL — Akshay's Personal Intelligence Brief
Sections: Weather · World News · Markets · Quote · Wisdom/Dad · Chess · FP&A→CFO
          Business Case Study · Top 5 Picks · Stock Tracker · Money Hack · Productivity
Refreshes at 6 AM MYT daily. Deploy: news.askakshay.com
"""
from __future__ import annotations

import os, json, math, sqlite3, logging, time, threading
from datetime import datetime, timezone, timedelta, date
from typing import Optional
import feedparser
import pandas as pd
import yfinance as yf
import requests
from flask import Flask, render_template_string, jsonify, request, redirect
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from content_cache import get_cached_markets, get_cached_jobs, get_cached_news, get_cached_quote
import daily_learning
import db as _db_mod

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

IST      = timezone(timedelta(hours=5, minutes=30))
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
PORT     = int(os.environ.get("PORT", 5050))

app = Flask(__name__)

# ─────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────

def _db():
    con = _db_mod.connect()
    con.row_factory = _db_mod.Row
    return con

def _price_unit(symbol) -> str:
    """Currency for a ledger symbol. Single source: standalone_scan._unit(),
    which itself derives the commodity set from the scanner's own table."""
    try:
        from standalone_scan import _unit
        return _unit(str(symbol or "").replace(".NS", "").replace(".BO", ""))
    except Exception:
        return "₹"


_ALL_SIGNALS_COLS: set | None = None


def _all_signals_columns() -> set:
    """Probed once per process, like vercel-news's optional() does for the
    same table. newspaper.yml runs on its own schedule, independent of the
    scanner that actually applies tracker.py's ALTER TABLE migrations — a
    column added there is not guaranteed to exist yet the first time this
    runs, and naming a missing column fails the whole query, taking the
    daily edition down with it.
    """
    global _ALL_SIGNALS_COLS
    if _ALL_SIGNALS_COLS is None:
        try:
            with _db() as con:
                _ALL_SIGNALS_COLS = {r[1] for r in con.execute("PRAGMA table_info(all_signals)").fetchall()}
        except Exception:
            _ALL_SIGNALS_COLS = set()
    return _ALL_SIGNALS_COLS


def fetch_alert_log(limit: int = 200) -> list[dict]:
    """Read Telegram alert history from all_signals table (signals.db / Turso)."""
    try:
        _cols_present = _all_signals_columns()
        has_dup_note = "duplicate_note" in _cols_present
        dup_select = ", duplicate_note" if has_dup_note else ""
        # Probed, not assumed: `remarks` arrives via ALTER TABLE and naming a
        # column that does not exist yet fails the whole query, which would
        # blank the entire signal log rather than one cell.
        has_remarks = "remarks" in _cols_present
        rmk_select = ", remarks" if has_remarks else ""
        with _db() as con:
            rows = con.execute(f"""
                SELECT date, symbol, action, timeframe, signal_type,
                       entry, sl, target1, target2, rr, score,
                       status, lifecycle_status, exit_price, pnl_pct,
                       closed_at, sent_at{dup_select}{rmk_select}
                FROM all_signals
                ORDER BY date DESC, id DESC
                LIMIT ?
            """, (limit,)).fetchall()
        cols = ["date","symbol","action","timeframe","signal_type",
                "entry","sl","target1","target2","rr","score",
                "status","lifecycle_status","exit_price","pnl_pct",
                "closed_at","sent_at"] + (["duplicate_note"] if has_dup_note else []) \
               + (["remarks"] if has_remarks else [])
        result = []
        for r in rows:
            r = dict(zip(cols, r))
            # badge colour logic
            s = (r.get("status") or "").upper()
            lc = (r.get("lifecycle_status") or "").upper()
            WIN_STATUSES  = {"TARGET_HIT", "T1_HIT", "T2_HIT", "TP1_HIT", "TP2_HIT", "PROFIT"}
            LOSS_STATUSES = {"SL_HIT", "STOPPED", "STOP_HIT", "LOSS"}
            # A real, resolved outcome (ran its course, exited on a time rule,
            # or never triggered before its window closed) — not a withdrawal.
            # Mirrors _db.js's badgeOf(); keep both in sync.
            EXPIRED_STATUSES = {"TIME_STOP", "EXPIRED"}
            if s in WIN_STATUSES or lc in WIN_STATUSES:
                badge = "win"
            elif s in LOSS_STATUSES or lc in LOSS_STATUSES:
                badge = "loss"
            elif s == "OPEN" or lc == "OPEN":
                badge = "open"
            elif s in EXPIRED_STATUSES or lc in EXPIRED_STATUSES:
                badge = "expired"
            else:
                badge = "cancelled"  # VOID, CANCELLED, and anything unrecognized
            r["badge"] = badge
            # Same symbol → currency rule the alerts and the API use. Without
            # it the 6 AM snapshot renders dollar-quoted commodities in rupees.
            r["currency"] = _price_unit(r.get("symbol"))
            # "" when there is no chart we can name. The template links only
            # when this is non-empty.
            r["tv"] = tv_alert_symbol(r.get("symbol"))
            # friendly pnl
            p = r.get("pnl_pct")
            r["pnl_str"] = (f"+{p:.1f}%" if p and p > 0 else f"{p:.1f}%") if p else "—"
            # short date
            r["alert_date"] = (r.get("date") or "")[:10]
            r["close_date"] = (r.get("closed_at") or "")[:10] or "—"
            result.append(r)
        return result
    except Exception as e:
        log.warning(f"fetch_alert_log: {e}")
        return []

def init_newspaper_db():
    with _db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS stock_tracker (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL, name TEXT, added_date TEXT,
            entry_price REAL, current_price REAL, target_price REAL,
            stop_loss REAL, thesis TEXT, timeframe TEXT,
            status TEXT DEFAULT 'active', updated_at TEXT
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS newspaper_stocks_picked (
            pick_date TEXT PRIMARY KEY, picks TEXT
        )""")
        # The fund screen is ~700 NAV downloads, so it is cached weekly like
        # the stock picks rather than recomputed per build.
        # The Daily Intelligence Brief. Keyed by IST date — one edition a day,
        # rebuilt in place by an intraday refresh rather than appended to.
        con.execute("""CREATE TABLE IF NOT EXISTS newspaper_brief (
            date TEXT PRIMARY KEY, payload TEXT
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS newspaper_funds (
            week TEXT PRIMARY KEY, payload TEXT
        )""")
        # Corporate actions / FII-DII / sector heat. Daily, keyed by date —
        # a separate workflow (market_intel.yml) populates it, same
        # separation-of-concerns as newspaper_funds: NSE's endpoints are
        # third-party calls that can hang or rate-limit, and that must
        # never take the daily paper down with it.
        con.execute("""CREATE TABLE IF NOT EXISTS newspaper_market_intel (
            date TEXT PRIMARY KEY, payload TEXT
        )""")
        # Podcasts are seven feed fetches plus an AI pass per episode — cheap,
        # but not cheap enough to repeat on a build that already has a budget,
        # and the ask was explicitly weekly.
        con.execute("""CREATE TABLE IF NOT EXISTS newspaper_podcasts (
            week TEXT PRIMARY KEY, payload TEXT
        )""")
        # The Nifty 500 research screen behind #stocks. Weekly for the same
        # reason as the fund screen: ~500 symbols × (a quote plus two statement
        # frames), fetched sequentially.
        #
        # Named `newspaper_screen` rather than the obvious "newspaper_" + stocks,
        # which would sit one suffix away from `newspaper_stocks_picked` above —
        # a different table holding the daily trade picks. Two table names that
        # differ only by a suffix is how a SELECT ends up on the wrong one.
        con.execute("""CREATE TABLE IF NOT EXISTS newspaper_screen (
            week TEXT PRIMARY KEY, payload TEXT
        )""")

# ─────────────────────────────────────────────────────────────
# GROQ AI
# ─────────────────────────────────────────────────────────────

# Groq has now decommissioned a model out from under this code TWICE:
# llama3-8b-8192 first, then llama-3.3-70b-versatile (404 "does not exist",
# observed 2026-08-17 — 40 stock narratives asked, 0 written, and every caller
# silently fell back to its canned string). Env-overridable so the next one is a
# secret change, not a code deploy.
GROQ_MODEL = os.environ.get("GROQ_MODEL") or "openai/gpt-oss-120b"

# gpt-oss is a REASONING model, and that is a live trap here. Verified against
# the API: at max_tokens=120 it spent 118 tokens reasoning, returned an empty
# string, and reported finish_reason="length" — a 200 OK carrying nothing,
# which fails more quietly than the 404 it replaced. reasoning_effort="low"
# cuts it to ~9 tokens, and REASONING_HEADROOM keeps the visible answer from
# being starved by tokens the caller never sees or asked for.
GROQ_REASONING_HEADROOM = 320


def groq_complete(prompt: str, max_tokens: int = 120) -> str:
    if not GROQ_KEY:
        return ""
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={"model": GROQ_MODEL,
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": max_tokens + GROQ_REASONING_HEADROOM,
                  "reasoning_effort": "low",
                  "temperature": 0.7},
            timeout=15,
        )
        if r.status_code == 200:
            body = r.json()
            choice = (body.get("choices") or [{}])[0]
            txt = (choice.get("message", {}).get("content") or "").strip()
            if not txt:
                # Empty 200. Say so out loud — this is the failure mode that
                # cost 40 narratives while every log line read "success".
                log.warning(
                    f"Groq {GROQ_MODEL}: empty completion "
                    f"(finish_reason={choice.get('finish_reason')}, "
                    f"reasoning_tokens="
                    f"{body.get('usage', {}).get('completion_tokens_details', {}).get('reasoning_tokens')})")
            return txt
        # Log the body, not just the code. A decommissioned model returns a 404
        # that says so in plain English, and nobody ever saw it.
        log.warning(f"Groq {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.warning(f"Groq: {e}")
    return ""

def ai_stock_thesis(symbol: str, mom_1m: float, mom_3m: float, score: int,
                    ext20: float = 0.0, vol_ratio: float = 1.0) -> str:
    """One line on why this specific name, not a sentence that fits any name.

    The old prompt passed only two momentum figures, so the model had nothing
    distinguishing to say and every card read alike. It gets the whole shape of
    the setup now — where price sits against its average, whether the one-month
    thrust is accelerating away from the three-month trend, and whether volume
    confirmed it — and is told in the prompt not to reuse a stock phrase.
    """
    accel = "accelerating" if mom_1m * 3 > mom_3m else "consolidating"
    prompt = (
        f"Indian stock {symbol}. 1-month {mom_1m:+.1f}%, 3-month {mom_3m:+.1f}% ({accel}). "
        f"Price sits {ext20:+.1f}% vs its 20-day average. Volume {vol_ratio:.2f}x its "
        f"20-day norm. Composite score {score}/100.\n"
        "Write ONE sentence, max 22 words, for a chartered accountant who reads numbers "
        "first. Lead with the most distinctive figure. Say what the setup IS, not that it "
        "is 'strong' or has 'bullish structure' — those phrases are banned. No hedging, "
        "no disclaimer, no ticker repetition."
    )
    result = groq_complete(prompt, max_tokens=70)
    if result:
        return result.strip().strip('"')
    # Fallback still varies by what is actually distinctive about the name,
    # rather than one template with a number swapped in.
    if vol_ratio >= 1.4:
        return (f"{vol_ratio:.1f}x normal volume behind a {mom_1m:+.0f}% month — "
                f"participation is confirming the move.")
    if mom_1m * 3 > mom_3m * 1.5:
        return (f"{mom_1m:+.0f}% in a month against {mom_3m:+.0f}% over three — "
                f"the trend is accelerating, not maturing.")
    if ext20 < 2:
        return (f"{mom_3m:+.0f}% over three months but only {ext20:+.1f}% above its "
                f"20-day average — extended trend, unextended price.")
    return (f"{mom_3m:+.0f}% three-month trend, {ext20:+.1f}% above the 20-day, "
            f"volume {vol_ratio:.2f}x normal.")

# ─────────────────────────────────────────────────────────────
# WEATHER — OpenMeteo (free, no key)
# ─────────────────────────────────────────────────────────────

WMO_MAP = {
    0: ("Clear Sky", "☀️"), 1: ("Mainly Clear", "🌤️"), 2: ("Partly Cloudy", "⛅"),
    3: ("Overcast", "☁️"), 45: ("Foggy", "🌫️"), 48: ("Icy Fog", "🌫️"),
    51: ("Light Drizzle", "🌦️"), 53: ("Drizzle", "🌦️"), 55: ("Heavy Drizzle", "🌧️"),
    61: ("Light Rain", "🌧️"), 63: ("Rain", "🌧️"), 65: ("Heavy Rain", "🌧️"),
    71: ("Light Snow", "🌨️"), 73: ("Snow", "❄️"), 75: ("Heavy Snow", "❄️"),
    80: ("Rain Showers", "🌦️"), 81: ("Showers", "🌧️"), 82: ("Violent Showers", "⛈️"),
    95: ("Thunderstorm", "⛈️"), 96: ("Thunderstorm+Hail", "⛈️"), 99: ("Heavy Thunderstorm", "⛈️"),
}

WEATHER_CITIES = [
    {"name": "Bikaner", "country": "IN", "lat": 28.02, "lon": 73.31, "tz": "Asia%2FKolkata"},
    {"name": "Kolkata", "country": "IN", "lat": 22.57, "lon": 88.36, "tz": "Asia%2FKolkata"},
    {"name": "Kuala Lumpur", "country": "MY", "lat": 3.14, "lon": 101.69, "tz": "Asia%2FKuala_Lumpur"},
]

def fetch_weather() -> list[dict]:
    results = []
    for c in WEATHER_CITIES:
        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={c['lat']}&longitude={c['lon']}"
            f"&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m"
            f"&daily=precipitation_probability_max,temperature_2m_max,temperature_2m_min,weather_code"
            f"&timezone={c['tz']}&forecast_days=1"
        )
        try:
            r = requests.get(url, timeout=8)
            d = r.json()
            cur   = d.get("current", {})
            daily = d.get("daily", {})
            wmo   = cur.get("weather_code", 0)
            cond, emoji = WMO_MAP.get(wmo, ("Unknown", "🌡️"))
            rain_pct = (daily.get("precipitation_probability_max") or [0])[0] or 0
            results.append({
                "city":       c["name"],
                "country":    c["country"],
                "emoji":      emoji,
                "condition":  cond,
                "temp":       round(cur.get("temperature_2m", 0), 1),
                "feels":      round(cur.get("apparent_temperature", 0), 1),
                "humidity":   cur.get("relative_humidity_2m", 0),
                "wind":       round(cur.get("wind_speed_10m", 0), 1),
                "rain_pct":   int(rain_pct),
                "temp_max":   round((daily.get("temperature_2m_max") or [0])[0], 1),
                "temp_min":   round((daily.get("temperature_2m_min") or [0])[0], 1),
                "rain_alert": rain_pct >= 60,
            })
        except Exception as e:
            log.warning(f"Weather {c['name']}: {e}")
    return results

# ─────────────────────────────────────────────────────────────
# CONTENT: GLOBAL NEWS
# ─────────────────────────────────────────────────────────────

def fetch_global_news(max_items: int = 18) -> list[dict]:
    return get_cached_news()[:max_items]

def fetch_smart_reads() -> list[dict]:
    """Longer analytical pieces from the same named mastheads as the wire."""
    from content_cache import get_cached_smart_reads
    return get_cached_smart_reads()

def fetch_dubai_jobs() -> list[dict]:
    return get_cached_jobs()

def fetch_markets() -> list[dict]:
    return get_cached_markets()


# ─────────────────────────────────────────────────────────────
# MARKET REGIME
# ─────────────────────────────────────────────────────────────
# The page showed eight instruments and eight percentages and left the reader
# to hold them in their head. This states the one thing those eight numbers
# jointly say — is risk being taken today or shed — and then shows its work,
# because an unexplained "72/100" is exactly the black-box number this site
# exists not to publish.
#
# It is deliberately not a proprietary index. It is the daily move of the same
# eight instruments in the rail, weighted, with risk assets counted positive
# and havens counted negative. Anyone can recompute it from the rail above it.
#
# (name → (side, weight, the daily % move that counts as a FULL move))
# Normalisers are per asset class on purpose: a 1.5% day in Nifty and a 1.5%
# day in USD/INR are not the same event. The rupee one is 0.4% because that is
# already a large day for it; treating them alike made FX noise dominate.
REGIME_WEIGHTS = {
    "Nifty 50": ("risk",  12, 1.5),
    "Sensex":   ("risk",   8, 1.5),
    "S&P 500":  ("risk",  12, 1.2),
    "Nasdaq":   ("risk",  12, 1.5),
    "BTC":      ("risk",   6, 3.0),
    "Gold":     ("haven",  8, 1.0),
    "USD/INR":  ("haven",  8, 0.4),   # rupee weakness reads as risk-off here
    "Crude":    ("haven",  6, 2.5),
}


def market_regime(markets: list[dict]) -> dict:
    """One risk-appetite reading, 0–100, plus the components behind it.

    50 is neutral. Every risk asset pushes up by its weight scaled by how big
    its move was against that asset's own full-move normaliser; every haven
    pushes down the same way.

    Each side is then normalised to 50 points of headroom against the weight
    ACTUALLY PRESENT on that side in this build. Two reasons, and both are
    correctness rather than taste. The raw weights sum to 50 on the risk side
    and 22 on the haven side, so an all-out risk day printed 100 while an
    equally violent flight to safety printed 28 — the same conviction reading
    as a very different number depending on which way it pointed. And when
    Yahoo drops an instrument, its weight silently leaves the scale, so the
    reading would drift toward neutral for a data outage rather than a calm
    market. Normalising per side, per build, fixes both.

    Returns {} when no instrument priced — a regime built on the fallback rows
    (change_pct 0, price "—") would read a confident neutral 50 off no data,
    which is the failure mode this codebase keeps hitting. Silence is correct.
    """
    parts = []
    raw = {"risk": 0.0, "haven": 0.0}      # signed, before normalising
    cap = {"risk": 0.0, "haven": 0.0}      # weight present on each side
    for m in markets or []:
        w = REGIME_WEIGHTS.get(m.get("name"))
        # price "—" is content_cache's fetch-failed row. It carries change_pct
        # 0, which is indistinguishable from a genuinely flat day unless the
        # price is checked too.
        if not w or m.get("price") in (None, "", "—"):
            continue
        side, weight, norm = w
        pct = float(m.get("change_pct") or 0)
        scaled = max(-1.0, min(1.0, pct / norm)) if norm else 0.0
        raw[side] += weight * scaled
        cap[side] += weight
        parts.append({"name": m["name"], "side": side, "pct": pct,
                      "weight": weight, "scaled": round(scaled, 3)})
    if not parts:
        return {}

    total = 0.0
    for side, sign in (("risk", 1), ("haven", -1)):
        if cap[side]:
            total += sign * raw[side] * (50.0 / cap[side])
    # Each part's share of the final reading, on the same normalised scale, so
    # the drivers named below are the ones that actually moved the number.
    for p in parts:
        sign = 1 if p["side"] == "risk" else -1
        p["push"] = round(sign * p["weight"] * p["scaled"] * (50.0 / cap[p["side"]]), 1)
        del p["weight"], p["scaled"]

    score = int(round(max(0.0, min(100.0, 50 + total))))
    if   score >= 70: label, tone = "Risk-on",          "up"
    elif score >= 56: label, tone = "Leaning risk-on",  "up"
    elif score >= 45: label, tone = "Mixed",            "flat"
    elif score >= 31: label, tone = "Leaning risk-off", "dn"
    else:             label, tone = "Risk-off",         "dn"

    # The two instruments that moved the reading most, either way. This is the
    # "why" — without it the number is a claim.
    #
    # Only instruments that actually moved it qualify. Sorting by abs(push)
    # alone returns whatever happened to be first on a flat day, so the page
    # would print "moved most by Nifty 50 +0.0%" — naming a driver of nothing.
    drivers = [p for p in sorted(parts, key=lambda p: abs(p["push"]), reverse=True)
               if abs(p["push"]) >= 0.5][:2]
    return {
        "score": score, "label": label, "tone": tone,
        "parts": parts, "drivers": drivers,
        "n": len(parts),
        # Under half the board priced, the reading is a couple of instruments
        # wearing a 0–100 scale. Say so rather than letting it pass as a
        # measurement of "the market".
        "thin": len(parts) < 4,
    }


def what_matters(*, regime: dict, markets: list[dict], market_intel: dict | None,
                 top5: list[dict], closed: int, winrate: float,
                 engine_changes: list[dict]) -> list[dict]:
    """The interpretation layer: at most five cards saying what today means.

    The page had the numbers and no reading of them. A reader arriving at 6 AM
    got a regime score, a nine-instrument rail, five ranked ideas and a ledger,
    and had to do the synthesis themselves — which is the work the site exists
    to have already done.

    Three rules govern every card here, and they are the reason this is Python
    and not a Groq prompt:

    ONE — DETERMINISTIC. Every card is a rule over data already on the page.
    Nothing is generated, nothing is inferred by a model, so the same build
    always produces the same reading and any card can be traced to its inputs.
    The site's own trust rule is that AI interpretation must never wear raw
    data's clothes; the cheapest way to honour it is to have no AI here.

    TWO — EVIDENCE OR SILENCE. A card is emitted only when its trigger fires.
    Four cards on a day that earned four, one on a day that earned one. Filling
    a fixed grid means inventing a reading on a quiet day, and a manufactured
    "Momentum" card on a flat tape is worse than an empty column: it is a
    confident statement about nothing.

    THREE — EVERY CARD HANDS OFF. Each carries the section that proves it.
    A claim with no route to its evidence is an assertion.

    FOUR — EVERY CARD IS FALSIFIABLE, AND SAYS WHAT IT IS. Added when these
    became decision cards rather than observations. Two more keys on every one:

      basis   FACT | MODEL | RESULT — the same provenance vocabulary the rest
              of the page uses. A price that moved is a FACT; a regime score
              or a screen rank is a MODEL; a closed trade is a RESULT. The
              reader should never have to guess which of the three they are
              being handed, and the three carry very different weight.

      unless  What would make this reading wrong. Not a disclaimer — a named
              condition a reader can watch for and check against tomorrow's
              page. A card that cannot state one is an opinion.

    Deliberately NOT added: an instruction. No card says buy, sell, apply or
    avoid. The site publishes measurements and the reasoning over them; telling
    a reader what to do with their money is a different product with different
    obligations, and it is not this one.

    Returns [] when nothing qualifies. The template renders the whole block
    only when this is non-empty.
    """
    cards: list[dict] = []
    parts = (regime or {}).get("parts") or []
    priced = [m for m in (markets or [])
              if m.get("price") not in (None, "", "—")]

    def pct(p) -> float:
        return float(p.get("pct") or 0)

    havens = sorted([p for p in parts if p["side"] == "haven"],
                    key=pct, reverse=True)
    risky  = sorted([p for p in parts if p["side"] == "risk"],
                    key=pct, reverse=True)

    # ── RISK ────────────────────────────────────────────────────────────────
    # The card the brief asks for by name: "Gold +4.26% while equities soften".
    # It fires on DIVERGENCE, not on a single move — a haven bid while risk
    # assets are also up is a liquidity story, not a risk story, and calling it
    # one would be the fake-precision the brief forbids.
    risk_dn = [p for p in risky if pct(p) < 0]
    if havens and risky and pct(havens[0]) >= 0.5 and len(risk_dn) >= max(1, len(risky) // 2):
        h = havens[0]
        cards.append({
            "kind": "risk", "tag": "Risk",
            "head": f"{h['name']} {pct(h):+.2f}% while {len(risk_dn)} of "
                    f"{len(risky)} risk assets fell",
            "why": f"A haven bid against a soft tape is what pushes the regime "
                   f"reading down. It sits at {regime['score']}/100 — "
                   f"{regime['label'].lower()}.",
            "basis": "FACT",
            "unless": f"Risk assets recovering while the haven bid holds turns "
                      f"this into a liquidity story rather than a risk one.",
            "href": "#marketintel", "cta": "Market intel",
        })
    elif regime and regime.get("score", 50) <= 30:
        cards.append({
            "kind": "risk", "tag": "Risk",
            "head": f"{regime['label']} · {regime['score']}/100",
            "why": "Risk appetite across the priced board, weighted by move "
                   "size. 50 is neutral.",
            "basis": "MODEL",
            "unless": "The score weights whatever priced this morning. A fuller "
                      "board can move it several points with nothing having "
                      "changed about risk.",
            "href": "#marketintel", "cta": "Market intel",
        })

    # ── MOMENTUM ────────────────────────────────────────────────────────────
    # The strongest risk asset, and only when it actually moved. A +0.04% "best
    # riser" is not momentum; it is the top of a flat list.
    if risky and pct(risky[0]) >= 0.5:
        r = risky[0]
        up_n = sum(1 for m in priced if float(m.get("change_pct") or 0) > 0)
        cards.append({
            "kind": "momentum", "tag": "Momentum",
            "head": f"{r['name']} {pct(r):+.2f}% — the day's strongest risk asset",
            "why": f"{up_n} of {len(priced)} priced instruments advanced. "
                   f"Breadth is what separates a move from a rotation.",
            "basis": "FACT",
            "unless": "Breadth under half the board would make this one name "
                      "moving, not momentum.",
            "href": "#world", "cta": "Full board",
        })

    # ── WATCH ───────────────────────────────────────────────────────────────
    # Priority order matters. A thin board outranks every other observation,
    # because on a thin board every other observation is weaker than it looks.
    fd = (market_intel or {}).get("fii_dii") or {}
    fii, dii = fd.get("fii_cr"), fd.get("dii_cr")
    if regime and regime.get("thin"):
        cards.append({
            "kind": "watch", "tag": "Watch",
            "head": f"Only {regime['n']} instruments priced this morning",
            "why": "Under half the board reported. Read today's regime score "
                   "as a sketch, not a measurement — including the cards "
                   "beside this one.",
            "basis": "FACT",
            "unless": "A full board tomorrow restores every reading on this "
                      "page, this one included.",
            "href": "#datahealth", "cta": "Data health",
        })
    elif (fii is not None and dii is not None
          and (fii < 0 < dii or dii < 0 < fii)
          and max(abs(fii), abs(dii)) >= 500):
        buyer, seller = ("DII", "FII") if dii > 0 else ("FII", "DII")
        cards.append({
            "kind": "watch", "tag": "Watch",
            "head": f"FII ₹{fii:+,.0f} Cr against DII ₹{dii:+,.0f} Cr",
            "why": f"{buyer} money is absorbing {seller} selling. Flows on "
                   f"opposite sides is the setup that resolves violently in "
                   f"whichever direction gives up first.",
            "basis": "FACT",
            "unless": "Both sides buying, or both selling, ends the standoff. "
                      "Which way it ends is the thing worth waiting for.",
            "href": "#marketintel", "cta": "The flows",
        })
    elif engine_changes:
        ec = engine_changes[0]
        cards.append({
            "kind": "watch", "tag": "Watch",
            "head": f"Engine changed — {ec.get('title', '')}",
            "why": f"{ec.get('date', '')} · {ec.get('tag', '')}. Every rule "
                   f"change is logged before it affects a signal, not after.",
            "basis": "FACT",
            "unless": "A rule change reaches the record only once enough "
                      "signals have closed under it. It has not yet.",
            "href": "#rules", "cta": "Engine log",
        })

    # ── OPPORTUNITY ─────────────────────────────────────────────────────────
    if top5:
        t = top5[0]
        rest = ", ".join(f"{p.get('name')} {p.get('score')}" for p in top5[1:4])
        cards.append({
            "kind": "opportunity", "tag": "Opportunity",
            "head": f"{t.get('name')} scores {t.get('score')}/100 in this week's screen",
            "why": (f"Then {rest}. " if rest else "")
                   + "Ranked once per ISO week — these are ideas, not ledger "
                     "signals, and they never touch the win rate.",
            "basis": "MODEL",
            "unless": "Re-ranked every ISO week. A name can leave these five "
                      "without anything happening to the business.",
            "href": "#picks", "cta": "All five",
        })

    # ── RECORD ──────────────────────────────────────────────────────────────
    # The site's whole argument, and the one card that is allowed to say the
    # sample is too small — because saying "20%" over four trades in the same
    # type as a measured result is the single most misleading thing this page
    # could do.
    if closed >= 30:
        cards.append({
            "kind": "record", "tag": "Record",
            "head": f"{closed} closed signals · {winrate:g}% winners",
            "why": "Logged when it fires, scored when it closes. Losers "
                   "included — that is the point of publishing it.",
            "basis": "RESULT",
            "unless": "Measured over a different window this can change sign. "
                      "The window is part of the claim, not a detail under it.",
            "href": "#perf", "cta": "Full record",
        })
    elif closed:
        cards.append({
            "kind": "record", "tag": "Record",
            "head": f"{closed} closed signal{'' if closed == 1 else 's'} — "
                    f"too few to measure",
            "why": "A win rate under 30 closed trades is a running tally, not "
                   "an edge. It is reported here as a count on purpose.",
            "basis": "RESULT",
            "unless": "Thirty closed trades is where a tally becomes a "
                      "measurement. This is not there yet.",
            "href": "#perf", "cta": "Full record",
        })

    return cards

# ─────────────────────────────────────────────────────────────
# ENTREPRENEUR QUOTES — 100 quotes
# ─────────────────────────────────────────────────────────────

ENTREPRENEUR_QUOTES = [
    ("Elon Musk", "When something is important enough, you do it even if the odds are not in your favor."),
    ("Elon Musk", "Failure is an option here. If things are not failing, you are not innovating enough."),
    ("Jeff Bezos", "Your brand is what people say about you when you're not in the room."),
    ("Jeff Bezos", "We are stubborn on vision. We are flexible on details."),
    ("Steve Jobs", "The people who are crazy enough to think they can change the world are the ones who do."),
    ("Steve Jobs", "Innovation distinguishes between a leader and a follower."),
    ("Steve Jobs", "Your time is limited, so don't waste it living someone else's life."),
    ("Warren Buffett", "Price is what you pay. Value is what you get."),
    ("Warren Buffett", "Be fearful when others are greedy and greedy when others are fearful."),
    ("Warren Buffett", "Someone is sitting in the shade today because someone planted a tree long ago."),
    ("Bill Gates", "It's fine to celebrate success but it is more important to heed the lessons of failure."),
    ("Bill Gates", "Success is a lousy teacher. It seduces smart people into thinking they can't lose."),
    ("Mark Zuckerberg", "The biggest risk is not taking any risk. In a rapidly changing world, the only strategy that is guaranteed to fail is not taking risks."),
    ("Mark Zuckerberg", "Move fast and break things. Unless you are breaking stuff, you are not moving fast enough."),
    ("Jack Ma", "Today is hard, tomorrow will be worse, but the day after tomorrow will be sunshine."),
    ("Jack Ma", "Never give up. Today is hard, tomorrow will be worse, but the day after tomorrow will be sunshine."),
    ("Richard Branson", "Clients do not come first. Employees come first. If you take care of your employees, they will take care of the clients."),
    ("Richard Branson", "Business opportunities are like buses, there's always another one coming."),
    ("Oprah Winfrey", "The biggest adventure you can take is to live the life of your dreams."),
    ("Oprah Winfrey", "You get in life what you have the courage to ask for."),
    ("Ratan Tata", "Take the stones people throw at you, and use them to build a monument."),
    ("Ratan Tata", "I don't believe in taking right decisions. I take decisions and then make them right."),
    ("Narayana Murthy", "Growth is painful. Change is painful. But nothing is as painful as staying stuck somewhere you don't belong."),
    ("Azim Premji", "When you run a business, you must build processes that outlive any individual, including yourself."),
    ("Reed Hastings", "The best thing you can do for employees is hire only high performers."),
    ("Reed Hastings", "Don't tolerate brilliant jerks. The cost to teamwork is too high."),
    ("Howard Schultz", "Dream more than others think practical. Expect more than others think possible."),
    ("Sara Blakely", "It's important to be willing to make mistakes. The worst thing that can happen is you become memorable."),
    ("Sundar Pichai", "It is important to follow your dreams and heart. Do something that excites you."),
    ("Satya Nadella", "Our industry does not respect tradition — it only respects innovation."),
    ("Satya Nadella", "Don't be a know-it-all, be a learn-it-all."),
    ("Sam Altman", "The most important work you'll ever do is thinking about what to work on. Most people skip this step."),
    ("Sam Altman", "Optimism is a competitive advantage. The world generally goes to the optimists."),
    ("Paul Graham", "Make something people want."),
    ("Paul Graham", "The way to get startup ideas is not to try to think of startup ideas. It's to look for problems."),
    ("Peter Thiel", "Competition is for losers. If you want to create and capture value, don't compete."),
    ("Naval Ravikant", "Earn with your mind, not your time."),
    ("Naval Ravikant", "Specific knowledge is knowledge that you cannot be trained for. It's found by pursuing curiosity."),
    ("Naval Ravikant", "Play long-term games with long-term people."),
    ("Dhirubhai Ambani", "If you don't build your dream, someone else will hire you to help them build theirs."),
    ("Dhirubhai Ambani", "Think big, think fast, think ahead. Ideas are no one's monopoly."),
    ("Mukesh Ambani", "Dream big but dream with your eyes open."),
    ("Indra Nooyi", "Just because you are CEO, don't think you have landed. You must continually increase your learning."),
    ("Kiran Mazumdar-Shaw", "There is no such thing as a perfect deal. You have to be willing to make compromises."),
    ("NR Narayana Murthy", "Software is a great combination between artistry and engineering."),
    ("Larry Page", "If you're changing the world, you're working on important things. You're excited to get up in the morning."),
    ("Larry Page", "You don't need to have a 100-person company to develop that idea."),
    ("Sergey Brin", "Solving big problems is easier than solving little problems."),
    ("Reid Hoffman", "If you are not embarrassed by the first version of your product, you've launched too late."),
    ("Reid Hoffman", "An entrepreneur is someone who jumps off a cliff and builds a plane on the way down."),
    ("Marc Andreessen", "Software is eating the world."),
    ("Marc Andreessen", "The most important things are the hardest to see."),
    ("Sheryl Sandberg", "In the future, there will be no female leaders. There will just be leaders."),
    ("Sheryl Sandberg", "Done is better than perfect."),
    ("Andy Grove", "Success breeds complacency. Complacency breeds failure. Only the paranoid survive."),
    ("Andy Grove", "Your time is limited, so don't waste it living someone else's life."),
    ("Charlie Munger", "Invert, always invert. Turn a situation or problem upside down."),
    ("Charlie Munger", "Show me the incentive and I'll show you the outcome."),
    ("Ray Dalio", "Pain plus reflection equals progress."),
    ("Ray Dalio", "Embrace reality and deal with it."),
    ("Tony Robbins", "The path to success is to take massive, determined action."),
    ("Gary Vaynerchuk", "Stop doing things for the short term. Build for where the world is going."),
    ("Gary Vaynerchuk", "Patience is the key. We overestimate what we can do in a year and underestimate what we can do in a decade."),
    ("Tim Ferriss", "Focus on being productive instead of busy."),
    ("Elon Musk", "I think it is possible for ordinary people to choose to be extraordinary."),
    ("Jeff Bezos", "I knew that if I failed I wouldn't regret that, but I knew the one thing I might regret is not trying."),
    ("Steve Jobs", "Quality is more important than quantity. One home run is much better than two doubles."),
    ("Warren Buffett", "Rule No.1: Never lose money. Rule No.2: Never forget Rule No.1."),
    ("Bill Gates", "If you can't make it good, at least make it look good."),
    ("Coco Chanel", "In order to be irreplaceable, one must always be different."),
    ("Henry Ford", "Whether you think you can, or you think you can't — you're right."),
    ("Walt Disney", "All our dreams can come true, if we have the courage to pursue them."),
    ("Thomas Edison", "I have not failed. I've just found 10,000 ways that won't work."),
    ("Andrew Carnegie", "Anything in life worth having is worth working for."),
    ("John D. Rockefeller", "Don't be afraid to give up the good to go for the great."),
    ("Sam Walton", "High expectations are the key to everything."),
    ("Michael Bloomberg", "In business, what's dangerous is not to evolve."),
    ("Jack Dorsey", "Make every detail perfect and limit the number of details to perfect."),
    ("Brian Chesky", "If we tried to think of a good idea, we wouldn't have been able to think of Airbnb."),
    ("Kevin Systrom", "Do what you love and the money will follow is bad advice. Do what creates the most value."),
    ("Evan Spiegel", "Because there are so many bad companies out there, it is actually quite easy to succeed."),
    ("Patrick Collison", "Move with urgency and focus. The world rewards those who ship."),
    ("Tobi Lütke", "Entrepreneurship is a personal development vehicle. Build yourself, not just the company."),
    ("Melinda Gates", "A woman with a voice is by definition a strong woman."),
    ("Malala Yousafzai", "One child, one teacher, one book, one pen can change the world."),
    ("Arianna Huffington", "Fearlessness is not the absence of fear. It's making the decision that something else is more important than fear."),
    ("Marc Benioff", "The secret to successful hiring is this: look for the people who want to change the world."),
    ("Jensen Huang", "A company that cannot build a product is just a social club."),
    ("Jensen Huang", "Suffering, in my opinion, is a prerequisite for greatness."),
    ("Daniel Zhang", "Only those who are willing to take risks will live a meaningful life."),
    ("Masayoshi Son", "Statistics are like bikinis. What they reveal is suggestive, but what they conceal is vital."),
    ("Yusaku Maezawa", "I want to create an environment where people can dream. That's worth more than any profit."),
    ("Carlos Slim", "With good humor, good sleep, and good food, one can face all miseries."),
    ("Lakshmi Mittal", "I have always believed that the true measure of success is the number of people you have helped."),
    ("Pony Ma", "Where there is a need, there is a business opportunity."),
    ("Ren Zhengfei", "Embrace competition. Be grateful for it. It forces you to become great."),
]

def get_entrepreneur_quote() -> dict:
    idx = date.today().toordinal() % len(ENTREPRENEUR_QUOTES)
    name, quote = ENTREPRENEUR_QUOTES[idx]
    return {"name": name, "quote": quote, "index": idx + 1, "total": len(ENTREPRENEUR_QUOTES)}

# ─────────────────────────────────────────────────────────────
# DAILY LESSONS FROM THE WORLD — 60 rotating
# ─────────────────────────────────────────────────────────────

WORLD_LESSONS = [
    ("Stoicism", "You have power over your mind — not outside events. Realize this, and you will find strength.", "Marcus Aurelius"),
    ("Stoicism", "Wealth consists not in having great possessions, but in having few wants.", "Epictetus"),
    ("Stoicism", "Waste no more time arguing about what a good man should be. Be one.", "Marcus Aurelius"),
    ("Stoicism", "He who fears death will never do anything worthy of a living man.", "Seneca"),
    ("Stoicism", "You become what you give your attention to.", "Epictetus"),
    ("Buddhism", "Peace comes from within. Do not seek it without.", "Buddha"),
    ("Buddhism", "The mind is everything. What you think you become.", "Buddha"),
    ("Buddhism", "In the end, only three things matter: how much you loved, how gently you lived, and how gracefully you let go.", "Buddha"),
    ("Buddhism", "You, yourself, as much as anybody in the entire universe, deserve your love and affection.", "Buddha"),
    ("Buddhism", "No one saves us but ourselves. No one can and no one may.", "Buddha"),
    ("Japanese Wisdom", "Fall seven times, stand up eight.", "Japanese Proverb"),
    ("Japanese Wisdom", "Kaizen: improve by 1% every day. 1% better every day = 37x better in a year.", "Japanese Philosophy"),
    ("Japanese Wisdom", "Ikigai: find the intersection of what you love, what you're good at, what the world needs, and what you're paid for.", "Okinawan Principle"),
    ("Japanese Wisdom", "Wabi-sabi: find beauty in imperfection. Nothing lasts, nothing is finished, nothing is perfect.", "Japanese Aesthetic"),
    ("Japanese Wisdom", "Eat until 80% full (Hara Hachi Bu). Know when to stop.", "Okinawan Wisdom"),
    ("African Wisdom", "If you want to go fast, go alone. If you want to go far, go together.", "African Proverb"),
    ("African Wisdom", "A child who is not embraced by the village will burn it down to feel its warmth.", "African Proverb"),
    ("African Wisdom", "Until the lion learns to write, every story will glorify the hunter.", "African Proverb"),
    ("African Wisdom", "The best time to plant a tree was 20 years ago. The second best time is now.", "African Proverb"),
    ("Chinese Wisdom", "The man who moves a mountain begins by carrying away small stones.", "Confucius"),
    ("Chinese Wisdom", "To know what you know and what you do not know — that is true knowledge.", "Confucius"),
    ("Chinese Wisdom", "When you realize there is nothing lacking, the whole world belongs to you.", "Lao Tzu"),
    ("Chinese Wisdom", "A journey of a thousand miles begins with a single step.", "Lao Tzu"),
    ("Chinese Wisdom", "Knowing others is wisdom. Knowing yourself is enlightenment.", "Lao Tzu"),
    ("Indian Wisdom", "Before you speak, let your words pass through three gates: Is it true? Is it necessary? Is it kind?", "Sufi Proverb"),
    ("Indian Wisdom", "The greatest sin is to think yourself weak.", "Swami Vivekananda"),
    ("Indian Wisdom", "In a gentle way, you can shake the world.", "Mahatma Gandhi"),
    ("Indian Wisdom", "Live as if you were to die tomorrow. Learn as if you were to live forever.", "Mahatma Gandhi"),
    ("Indian Wisdom", "The future depends on what you do today.", "Mahatma Gandhi"),
    ("Greek Philosophy", "The unexamined life is not worth living.", "Socrates"),
    ("Greek Philosophy", "We are what we repeatedly do. Excellence, then, is not an act, but a habit.", "Aristotle"),
    ("Greek Philosophy", "Give me a place to stand and I will move the Earth.", "Archimedes"),
    ("Persian Wisdom", "This too shall pass.", "Persian Adage"),
    ("Persian Wisdom", "Yesterday is gone. Tomorrow has not yet come. We have only today. Let us begin.", "Mother Teresa"),
    ("Confucian", "Real knowledge is to know the extent of one's ignorance.", "Confucius"),
    ("Modern Science", "Everything is a hypothesis until proven otherwise. Stay curious, stay humble.", "Scientific Method"),
    ("Finance Wisdom", "Compound interest is the eighth wonder of the world. He who understands it, earns it. He who doesn't, pays it.", "Albert Einstein"),
    ("Finance Wisdom", "The stock market is a device for transferring money from the impatient to the patient.", "Warren Buffett"),
    ("Finance Wisdom", "Risk comes from not knowing what you are doing.", "Warren Buffett"),
    ("Leadership", "A leader is best when people barely know he exists. When his work is done, they will say: we did it ourselves.", "Lao Tzu"),
    ("Leadership", "Management is doing things right. Leadership is doing the right things.", "Peter Drucker"),
    ("Leadership", "The function of leadership is to produce more leaders, not more followers.", "Ralph Nader"),
    ("Resilience", "It does not matter how slowly you go as long as you do not stop.", "Confucius"),
    ("Resilience", "Our greatest glory is not in never falling, but in rising every time we fall.", "Confucius"),
    ("Resilience", "Hardships often prepare ordinary people for an extraordinary destiny.", "C.S. Lewis"),
    ("Focus", "Concentrate all your thoughts upon the work at hand. The sun's rays do not burn until brought to a focus.", "Alexander Graham Bell"),
    ("Focus", "Beware the barrenness of a busy life.", "Socrates"),
    ("Time", "Time is the most valuable thing a man can spend.", "Theophrastus"),
    ("Time", "The two most powerful warriors are patience and time.", "Leo Tolstoy"),
    ("Gratitude", "Gratitude is not only the greatest of virtues, but the parent of all others.", "Cicero"),
    ("Gratitude", "Enough is a feast. Joy is in the journey, not the destination.", "Buddhist Teaching"),
    ("Discipline", "Discipline is the bridge between goals and accomplishment.", "Jim Rohn"),
    ("Discipline", "We must all suffer one of two things: the pain of discipline or the pain of regret.", "Jim Rohn"),
    ("Character", "Character is how you treat those who can do nothing for you.", "Unknown"),
    ("Simplicity", "Simplicity is the ultimate sophistication.", "Leonardo da Vinci"),
    ("Action", "A year from now, you will wish you had started today.", "Karen Lamb"),
    ("Courage", "Do one thing every day that scares you.", "Eleanor Roosevelt"),
    ("Learning", "Tell me and I forget. Teach me and I remember. Involve me and I learn.", "Benjamin Franklin"),
    ("Purpose", "The two most important days in your life are the day you were born and the day you find out why.", "Mark Twain"),
    ("Legacy", "Plant trees under whose shade you do not plan to sit.", "Greek Proverb"),
]

def get_world_lesson() -> dict:
    idx = (date.today().toordinal() + 3) % len(WORLD_LESSONS)
    tradition, lesson, source = WORLD_LESSONS[idx]
    return {"tradition": tradition, "lesson": lesson, "source": source}

# ─────────────────────────────────────────────────────────────
# BUSINESS CASE STUDIES — 35 rotating
# ─────────────────────────────────────────────────────────────

CASE_STUDIES = [
    ("Apple: The Return of Steve Jobs (1997)",
     "Apple was 90 days from bankruptcy. Jobs cut product lines from 350 to 10. Result: Apple went from $3B revenue to $350B+ in 15 years.",
     "Focus ruthlessly. Saying no to 1,000 things is the secret to innovation. Complexity kills companies."),
    ("Amazon: AWS — the accidental trillion-dollar business",
     "Amazon built AWS to solve internal infrastructure problems. They then sold the solution to the world. AWS now generates $90B+ revenue per year.",
     "Your internal problems are often external opportunities. The best products are built to scratch your own itch."),
    ("Netflix: DVD to Streaming (2007)",
     "Netflix had a profitable DVD business. Reed Hastings cannibalized it by betting on streaming. Blockbuster laughed. Netflix is now worth $250B+.",
     "Disrupt yourself before someone else does. The most dangerous competitor is your own comfort."),
    ("Apple iPhone Launch (2007)",
     "Jobs launched iPhone against executives who said no one would pay $499 for a phone. First year: 6.1M units. iPhone now drives 52% of Apple revenue.",
     "Solve a problem that everyone thinks is already solved. The best products make existing products look primitive."),
    ("Google AdWords — accidental business model",
     "Google's first business model failed. AdWords emerged from a small experiment in 2000. It became a $200B+ revenue machine.",
     "Product-market fit is found, not planned. Launch fast, observe behavior, double down on what works."),
    ("IKEA: Flat Pack Revolution",
     "A table leg broke during a photo shoot. An employee removed it to fit the table in a car. Ingvar Kamprad realized flat-pack was the future. IKEA now does $47B revenue.",
     "Your biggest breakthrough might come from solving a logistics problem, not a product problem."),
    ("Airbnb: From Cereal Boxes to $75B",
     "Airbnb founders were broke. They bought cereals, repackaged as 'Obama O's' and 'Cap'n McCain's' to fund the company. First investors said the idea would never work.",
     "Survive first. Every startup has a near-death moment. Resourcefulness separates those who make it."),
    ("WhatsApp: 5 engineers, $19 billion",
     "WhatsApp had 5 engineers when Facebook bought it for $19B. No ads, no marketing. Just radical focus on a single feature: messaging that works.",
     "Solve one problem perfectly. You don't need scale if you have depth. Depth creates loyalty."),
    ("Reliance Jio: Disrupting India",
     "Mukesh Ambani invested $32B to offer free calls and almost-free data in India. 400M subscribers in 2 years. Destroyed competitors who had higher cost structures.",
     "When you have capital advantage, price to destroy the market. Sometimes the best strategy is radical pricing, not incremental improvement."),
    ("Tata Nano: The lesson in positioning",
     "Tata launched Nano as 'the cheapest car' at ₹1 lakh. It failed. Indians didn't want to buy 'the cheapest car' — it felt like admitting poverty.",
     "Positioning beats features every time. Never call your product 'cheap.' Call it 'accessible' or 'smart.'"),
    ("Starbucks: Selling $6 Coffee",
     "Howard Schultz sold coffee at 5x the price of a diner. The key insight: he wasn't selling coffee, he was selling a third place — between home and work.",
     "You're never just selling the product. Understand what people are actually buying. Starbucks sells an experience, not coffee."),
    ("Nokia: The Rise and Fall",
     "Nokia had 40% global mobile market share in 2007. By 2013, market share was near zero. They failed to see that software, not hardware, was the future.",
     "Market leaders die by defending yesterday's success. The biggest threat is always your own arrogance."),
    ("Zomato: Unit Economics Before Growth",
     "Zomato spent ₹100 to acquire a customer who spent ₹80. They kept growing. The lesson came when investors demanded profitability. Now Zomato is profitable.",
     "Growth that destroys unit economics is not growth — it's subsidized revenue. Always know your LTV:CAC ratio."),
    ("Byju's: The EdTech Crash",
     "Byju's raised $5.5B, was valued at $22B, and collapsed. No focus on profitability, weak governance, rapid expansion into unproven markets.",
     "Capital is a drug. More money means more runway, but it also means you can delay facing hard truths. Profitability is always the destination."),
    ("Tesla: Make the Most Expensive Car First",
     "Tesla launched with a $100K Roadster. Then $70K Model S. Then $35K Model 3. They used premium pricing to fund mass-market production.",
     "Start expensive to build the brand, fund R&D, and attract early adopters. Then scale down. Starting cheap makes it hard to go premium."),
    ("Zoho: The Anti-VC Playbook",
     "Zoho has never taken VC money. Sridhar Vembu built a $1B+ business on profitability, not funding. Employees in rural India, $0 in advertising spend.",
     "You don't need venture capital to build a great company. Profitable growth beats loss-funded growth in the long run."),
    ("Facebook's Instagram Acquisition (2012)",
     "Facebook bought Instagram for $1B when it had 13 employees and $0 revenue. Zuckerberg saw mobile first. Instagram is now worth $100B+.",
     "Buy what threatens you before it kills you. The best M&A is strategic, not defensive."),
    ("Xiaomi: Selling Phones Like Software",
     "Xiaomi released phones at cost, made money on software and services. 5% net margin on hardware, 40%+ on MIUI ecosystem. $35B valuation in 5 years.",
     "The product can be the distribution channel. Give away the razor, sell the blades."),
    ("Flipkart vs Amazon India",
     "Flipkart was winning India. Then Amazon entered with $5B, same-day delivery, and superior tech. Walmart bought Flipkart for $16B — a defensive acquisition.",
     "Speed of execution beats being first. Amazon wasn't first in India, but they executed faster. Being early means nothing if you don't keep innovating."),
    ("Berkshire Hathaway: Float as a Business Model",
     "Buffett uses insurance float — money collected in premiums before claims — as free capital to invest. $160B float generates investment returns at zero cost.",
     "The best business models create free capital. Study how your industry's best companies actually make money — it's rarely where you think."),
    ("Paytm's IPO Disaster",
     "Paytm IPO at ₹2,150. Fell 27% on listing day. Problem: no clear path to profitability, high cash burn, and no moat in payments. Stock fell 75% from peak.",
     "A business model that relies on subsidized transactions is not a business — it's a bet on market share. Investors eventually ask: where's the profit?"),
    ("SpaceX: Reusable Rockets",
     "Every expert said reusable rockets were impossible. SpaceX landed Falcon 9 first stage in 2015. Launch cost dropped from $150M to $28M. Changed the industry.",
     "Impossible is a consensus opinion, not a fact. When everyone says something can't be done, check if they've done the math or just the tradition."),
    ("Dunzo Shutdown: Last Mile Lessons",
     "Dunzo raised $240M, burned through it all on dark stores, rider salaries, and heavy subsidies. Shut down in 2024. Unit economics never worked.",
     "Last-mile logistics is brutally hard. Before raising money, prove you can deliver profitably at small scale. Capital scales problems, not just success."),
    ("McDonald's: Real Estate, Not Burgers",
     "Ray Kroc's insight: McDonald's isn't a food company — it's a real estate company. Franchise owners pay rent on land McDonald's owns.",
     "The most valuable part of a business is often hidden. Always ask: what are we actually selling? Who really controls the profit pool in our industry?"),
    ("Infosys: The Global Delivery Model",
     "Narayana Murthy started Infosys with ₹10,000 borrowed from his wife in 1981. Built the global delivery model — offshore talent, onshore management. Revenue: $18B+.",
     "Arbitrage is a legitimate business model. Take a global problem, apply local economics, execute flawlessly. Consistency beats creativity in services."),
    ("OYO: Growth Before Foundation",
     "OYO expanded to 80+ countries in 5 years. No standardized product, operational chaos, $600M losses. Had to shut down hundreds of hotels and lay off 5,000 people.",
     "Blitzscaling works only if the unit economics work. Expand when you've proven the model, not before. Speed without structure is just expensive chaos."),
    ("Stripe: Developer-First, CEO-Last",
     "Stripe built payment APIs for developers, not CFOs. 7 lines of code to accept payments. No sales team for the first 3 years — product sold itself.",
     "The best distribution is built into the product. If developers love it, adoption spreads through word of mouth. Sell to builders, not buyers."),
    ("Slack: From Failed Game to $27B",
     "Slack's founders built a failed multiplayer game called Glitch. They salvaged the internal communication tool they'd built. Sold to Salesforce for $27B.",
     "Pivots are not failures — they're redirected learning. The most valuable thing from a failed product is often the tool you built to build it."),
    ("HDFC Bank: The Conservative Compounding Machine",
     "HDFC Bank has never posted a quarterly loss in 28 years. Never chased exotic products. Just excellent credit underwriting, low NPAs, and consistent execution.",
     "In banking and in life, the boring strategy often wins. Consistency over 20 years beats brilliance in short bursts. Compounding requires not breaking the chain."),
    ("Uber's Unit Economics Crisis",
     "Uber was losing $58 per trip in China. Lost $1B in 6 months and sold to DiDi. In the US, cost per ride was subsidized by VC money, not customer economics.",
     "When you measure success by growth, not economics, you build a machine that grows its losses. Know your unit economics before you scale, not after."),
    ("Canva: Design for the Masses",
     "Melanie Perkins was rejected by 100 VCs. Then she raised $3M. Canva now has 170M users and a $40B valuation. Key: removed complexity that designers love but others hate.",
     "Simplify what experts have made complex. The mass market doesn't want power — they want results. Dumb down the interface without dumbing down the output."),
    ("Swiggy vs Zomato: The Profitability Race",
     "Both companies burned billions. Zomato reached profitability first by cutting dark stores, focusing on Blinkit synergies, and raising average order value.",
     "In competitive markets, the winner is often whoever cuts losses fastest and finds profitability before their competitor. Endurance beats speed."),
    ("Alibaba: Jack Ma's 1001st Try",
     "Jack Ma was rejected from Harvard 10 times. KFC rejected him. Failed at multiple businesses. Alibaba launched in 1999 and became a $600B company.",
     "Resilience is the most under-rated competitive advantage. Most people quit. The gap between failure and success is usually just one more attempt."),
    ("Zerodha: Profitable Without VC",
     "Kamath brothers built Zerodha (India's largest broker) with zero external funding. Flat fee model. 7M customers. 50% EBITDA margins. No IPO, no VC pressure.",
     "You can build a dominant company without venture capital. Profitability gives you freedom. VC money gives you speed but costs you control."),
    ("Razorpay: B2B Sales Playbook",
     "Razorpay started with a landing page and no product. Collected emails. Launched 3 months later. Grew from $0 to $7.5B valuation by making checkout dead simple for developers.",
     "Validate before you build. A landing page with a waitlist tells you more than 3 months of product development."),
]

def get_case_study() -> dict:
    idx = (date.today().toordinal() + 5) % len(CASE_STUDIES)
    title, story, lesson = CASE_STUDIES[idx]
    return {"title": title, "story": story, "lesson": lesson}

# ─────────────────────────────────────────────────────────────
# FP&A DAILY LEARN
# ─────────────────────────────────────────────────────────────

FPNA_TIPS = [
    ("Zero-Based Budgeting", "Start every budget from ₹0. Justify every line. Cuts 15–30% bloat in most orgs."),
    ("Driver-Based Forecasting", "Build forecasts on business drivers (units, headcount, utilization), not historical % growth."),
    ("Rolling Forecast", "Rolling 12-month forecasts beat static annual budgets. Less time defending, more time deciding."),
    ("Variance Analysis", "Volume variance + Price/Rate variance + Mix variance = Total variance. Always decompose before presenting."),
    ("Working Capital", "DSO + DIO – DPO = Cash Conversion Cycle. Cutting CCC by 5 days can free millions."),
    ("EBITDA Bridge", "Walk from prior period: Revenue ±, COGS ±, SG&A ±, Other ±. Bridges tell the story behind the number."),
    ("Scenario Planning", "Always model 3: Base, Bull (+20%), Bear (–20%). Present the range. Executives hate surprises."),
    ("Contribution Margin", "CM = Revenue – Variable Costs. Know your CM by product, by customer, by geography."),
    ("Headcount Planning", "FTE cost = Salary × 1.3–1.5. Always model hiring lag — 60–90 days from approval to productive."),
    ("Free Cash Flow", "FCF = Net Income + D&A – Capex – ΔNWC. A company can show profit and still run out of cash."),
    ("SaaS Metrics", "ARR, MRR, Churn, NRR, CAC, LTV. In tech FP&A, know these cold."),
    ("Three-Statement Model", "P&L → Balance Sheet → Cash Flow — they must tie. If they don't, you have a bug."),
    ("Sensitivity Tables", "Use Excel's Data Table (What-If Analysis) to show EBITDA across assumptions. One table > 5 slides."),
    ("CFO Communication", "Lead with the number, then variance, then reason, then action. '₹12Cr EBITDA, ₹2Cr below plan, due to X, here's the fix.'"),
    ("80/20 of Month-End", "20% of accounts drive 80% of variance. Focus commentary there. The rest is noise."),
    ("Cost Centre vs Profit Centre", "Cost centres are budgeted, profit centres are managed to a P&L. Knowing the difference changes how you frame every problem."),
    ("Dubai FP&A Stack", "AED 30K+ roles: CA/ACCA + Power BI + SAP or Oracle + IFRS 9/16. Targets: ADNOC, Emirates, MAF, DP World."),
    ("Power BI for FP&A", "Replace Excel pivots. Connect to ERP source. Saves 5+ hours/month on month-end decks."),
    ("Financial Storytelling", "Data without narrative is noise. Frame every number: vs budget, vs prior year, vs industry."),
    ("Sensitivity Analysis", "Which assumption, if wrong, blows up your model? Identify it. Test it. Present the range."),
]

def get_fpna_tip() -> dict:
    idx = date.today().toordinal() % len(FPNA_TIPS)
    title, body = FPNA_TIPS[idx]
    return {"title": title, "body": body, "index": idx + 1, "total": len(FPNA_TIPS)}

# ─────────────────────────────────────────────────────────────
# LICHESS GAMES ANALYSIS
# ─────────────────────────────────────────────────────────────

LICHESS_USER = "AKK_010"
# Lichess 404s the default python-requests User-Agent. Not 401, not 403 — a
# flat "Not found" that is indistinguishable from a deleted account, which is
# why this looked like a credentials problem and sent the page to its
# "add LICHESS_TOKEN" notice for a token that was already set. Every call to
# lichess.org must carry this.
LICHESS_UA = {"User-Agent": "DailySignal/1.0 (+https://news.askakshay.com)"}
MY_PUZZLE_RATING = 1646

CHESS_THEME_TIPS: dict = {
    "fork":             "One piece, two threats. Find the square that attacks both simultaneously.",
    "pin":              "Pin a piece to the king or queen — it can't move without material loss.",
    "skewer":           "Attack the high-value piece; the one behind it falls when it moves.",
    "discoveredAttack": "Move one piece to unleash the attack of another behind it.",
    "mateIn1":          "One move ends it. Check every check and capture first.",
    "mateIn2":          "Force mate in two. Find the move that limits all their responses.",
    "mateIn3":          "Three-move combination. The first move must be forcing.",
    "backRankMate":     "Their king is trapped. A rook or queen on the 8th rank closes the game.",
    "sacrifice":        "Give material for a decisive positional or mating advantage.",
    "deflection":       "Lure the key defender away from its post with a forcing move.",
    "zugzwang":         "Any move they make worsens their position. Find the quiet, waiting move.",
    "endgame":          "King activity and pawn structure dominate. Technique over tactics here.",
    "quietMove":        "No captures, no checks — but the threat is overwhelming.",
    "attraction":       "Lure the king or a key piece onto a bad square with a sacrifice.",
    "advancedPawn":     "A passed pawn is a criminal that must be stopped or escorted home.",
    "doubleCheck":      "Two simultaneous checks — the king must move.",
}

TERMINATION_MAP = {
    "mate": "Checkmate", "resign": "Resignation", "timeout": "Time Out",
    "draw": "Draw", "stalemate": "Stalemate", "outoftime": "Time Out",
    "cheat": "Cheat detected", "unknownFinish": "Aborted",
}

def _lichess_activity_yesterday() -> tuple[dict, list]:
    """Returns (yest_speed_counts, trend_7d). Always works — no token needed."""
    ist = timezone(timedelta(hours=5, minutes=30))
    yest = (datetime.now(ist) - timedelta(days=1)).date()
    try:
        r = requests.get(
            f"https://lichess.org/api/user/{LICHESS_USER.lower()}/activity",
            headers={"Accept": "application/json", **LICHESS_UA}, timeout=15,
        )
        if r.status_code != 200:
            log.warning(f"Lichess activity: {r.status_code} — {r.text[:120]}")
            return {}, []
        acts = r.json()
    except Exception as e:
        log.warning(f"Lichess activity: {e}")
        return {}, []

    yest_counts: dict = {}
    trend: list = []
    for entry in acts[:7]:
        ts  = entry.get("interval", {}).get("start", 0) // 1000
        day = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        g   = entry.get("games", {})
        tw  = sum(v.get("win",0)  for v in g.values())
        tl  = sum(v.get("loss",0) for v in g.values())
        td  = sum(v.get("draw",0) for v in g.values())
        tt  = tw + tl + td
        p   = round(tw/tt*100) if tt else 0
        trend.append({"day": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m/%d"),
                      "wins": tw, "losses": tl, "draws": td, "total": tt, "pct": p})
        if day == yest:
            yest_counts = g
    return yest_counts, trend


def _lichess_export_games(since_ms: int, until_ms: int) -> list[dict]:
    """Fetch individual games via the export API.

    The token is NOT required. Lichess exports public games to anonymous
    callers; the header only raises the rate limit. Gating the whole call on
    it meant a missing secret downgraded the section to aggregate counts for
    no reason at all, and — worse — made every failure look like a missing
    token when the token was sitting right there in the job env.
    """
    token = os.environ.get("LICHESS_TOKEN", "")
    headers = {"Accept": "application/x-ndjson", **LICHESS_UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    params = {"since": since_ms, "until": until_ms,
              # evals/accuracy/division/clocks power the best-move pick, the
              # key facts and the strength estimate. All four are only
              # populated for games Lichess has actually analysed, so every
              # consumer below must degrade gracefully when they are absent.
              "opening": "true", "moves": "true", "max": 50,
              "evals": "true", "accuracy": "true",
              "division": "true", "clocks": "true"}

    # Two attempts. Lichess answers a concurrent export with
    # 429 "Please only run 1 request(s) at a time", and a streamed response
    # that was never drained keeps counting as in-flight — see the `with`
    # below, which is what stopped this endpoint leaking connections.
    for attempt in (1, 2):
        try:
            # Context-managed BECAUSE it streams. Every early return out of
            # this function used to abandon an open connection: the non-200
            # path returned without touching the body, and any exception
            # mid-iteration left it dangling. Lichess then counts the next
            # call as a second concurrent request and 429s it, so one bad
            # response poisoned every later one in the same process.
            with requests.get(
                f"https://lichess.org/api/games/user/{LICHESS_USER.lower()}",
                params=params, headers=headers, timeout=25, stream=True,
            ) as r:
                if r.status_code == 429 and attempt == 1:
                    log.warning("Lichess export: 429, retrying once in 5s")
                    time.sleep(5)
                    continue
                if r.status_code != 200:
                    # Say which failure it is. A 404 here is NOT a missing
                    # account — Lichess returns it for the default
                    # python-requests User-Agent, which is what made this
                    # look like a credentials problem for weeks.
                    log.warning(f"Lichess export: {r.status_code} — {r.text[:200]}")
                    return []
                games = []
                for line in r.iter_lines():
                    if line:
                        try:
                            games.append(json.loads(line))
                        except Exception:
                            pass
                return games
        except Exception as e:
            log.warning(f"Lichess export: {e}")
            return []
    return []


# ── Game analysis helpers ────────────────────────────────────────────────────
# Everything below derives from the Lichess `analysis` array (one entry per ply,
# each with an `eval` in centipawns from White's point of view, plus a
# `judgment` on inaccuracies/mistakes/blunders). Only present on analysed games.

def _cp(entry: dict) -> float | None:
    """Centipawn eval from White's perspective. Mate scores are clamped."""
    if not isinstance(entry, dict):
        return None
    if "eval" in entry and entry["eval"] is not None:
        return float(entry["eval"])
    if entry.get("mate") is not None:
        m = float(entry["mate"])
        return 100000.0 if m > 0 else -100000.0
    return None


def _best_move(analysis: list, all_moves: list, is_white: bool) -> dict:
    """The single strongest move I played, by eval swing in my favour.

    analysis[i] is the evaluation *after* ply i, so the effect of my move at ply
    i is analysis[i] - analysis[i-1], signed to my colour. Ignores swings that
    merely recapture material the engine already expected, by requiring the move
    to be the largest gain of the game rather than any positive gain.
    """
    if not analysis or not all_moves:
        return {}
    best, best_gain = None, 0.0
    for i in range(len(analysis)):
        # ply i is mine if (i even and I'm White) or (i odd and I'm Black)
        if (i % 2 == 0) != is_white:
            continue
        after = _cp(analysis[i])
        before = _cp(analysis[i - 1]) if i > 0 else 0.0
        if after is None or before is None:
            continue
        gain = (after - before) if is_white else (before - after)
        # A mate-in-N flip is worth surfacing but must not dwarf everything.
        gain = max(min(gain, 900.0), -900.0)
        if gain > best_gain and i < len(all_moves):
            best_gain, best = gain, {
                "san": all_moves[i],
                "move_no": i // 2 + 1,
                "gain_cp": int(round(gain)),
                "eval_after": round(after / 100.0, 2),
            }
    if not best or best_gain < 40:      # nothing decisive enough to call "best"
        return {}
    return best


def _strength_estimate(acpl: int | None, accuracy: float | None) -> int | None:
    """Rough Elo-equivalent of how well this single game was played.

    Anchored on average centipawn loss, which is the only strength proxy Lichess
    hands back. This is a coarse mapping, not a rating calculation — it says
    "you played around here today", nothing more. Displayed as an estimate and
    never as an official figure.
    """
    if acpl is None and accuracy is None:
        return None
    if acpl is not None:
        for ceiling, elo in ((10, 2500), (15, 2300), (20, 2150), (25, 2000),
                             (35, 1850), (45, 1700), (60, 1550), (80, 1400),
                             (110, 1250), (150, 1100)):
            if acpl <= ceiling:
                return elo
        return 950
    # Accuracy-only fallback, banded rather than linear: a linear fit made 82%
    # accuracy read as ~2080, which is nonsense for a club blitz game.
    for floor, elo in ((95, 2400), (90, 2100), (85, 1850), (80, 1650),
                       (75, 1500), (70, 1350), (60, 1150), (50, 1000)):
        if (accuracy or 0) >= floor:
            return elo
    return 900


def _fide_equivalent(rating, speed: str) -> int | None:
    """Approximate FIDE equivalent of a Lichess rating for this time control.

    Lichess ratings sit materially above FIDE — the gap is widest for the fast
    pools and narrows as the time control lengthens. These offsets are the
    commonly cited community approximations, not an official conversion, and
    there is no such thing as a FIDE rating for an online blitz game. Labelled
    "est." everywhere it is shown.
    """
    if not isinstance(rating, int):
        return None
    offset = {"bullet": 500, "blitz": 400, "rapid": 300,
              "classical": 250, "correspondence": 250}.get((speed or "").lower(), 400)
    return max(600, rating - offset)


def _standout(g: dict, analysis: list, is_white: bool, result: str,
              termination: str, num_moves: int, my_an: dict) -> str:
    """The one thing that made this game different from the others.

    Checked in order of how much it should override the others, so a comeback
    beats "clean game" and a blunder-fest beats a nice opening.
    """
    evals = [_cp(a) for a in analysis] if analysis else []
    evals = [e for e in evals if e is not None]
    mine = (lambda e: e) if is_white else (lambda e: -e)
    my_evals = [mine(e) for e in evals]

    worst = min(my_evals) if my_evals else None
    best_pt = max(my_evals) if my_evals else None
    blunders = (my_an or {}).get("blunder", 0)

    if "mate" in (termination or "").lower() and result == "Win":
        return f"Finished by checkmate on move {num_moves} — you converted rather than letting it drift."
    if result == "Win" and worst is not None and worst <= -300:
        return (f"A genuine comeback: the engine had you {abs(worst)/100:.1f} pawns "
                f"down at the low point and you still won.")
    if result == "Loss" and best_pt is not None and best_pt >= 300:
        return (f"You were {best_pt/100:.1f} pawns up at the peak and lost from there "
                f"— this is the game to review, not the openings.")
    if result == "Win" and blunders == 0 and worst is not None and worst > -100:
        return "Clean win — never worse than a pawn down, zero blunders. This is your template game."
    if (termination or "").lower().startswith("time") :
        return "Decided on the clock, not the board — the position was still playable when time ran out."
    if blunders >= 3:
        return f"{blunders} blunders in one game. The result is noise; the error count is the signal."
    if num_moves <= 20:
        return f"Over in {num_moves} moves — decided in the opening, so the opening is what to study."
    if num_moves >= 60:
        return f"{num_moves} moves of grinding. Endgame stamina, not opening prep, settled this one."
    return ""


def _key_facts(g: dict, my_an: dict, opp_an: dict, division: dict,
               num_moves: int, my_rating, opp_rating, speed: str) -> list:
    """Short factual bullets — only ones backed by data actually returned."""
    facts = []
    acc = (my_an or {}).get("accuracy")
    if acc is not None:
        opp_acc = (opp_an or {}).get("accuracy")
        tail = f" vs their {opp_acc:.0f}%" if opp_acc is not None else ""
        facts.append(f"Accuracy {acc:.0f}%{tail}")
    acpl = (my_an or {}).get("acpl")
    if acpl is not None:
        facts.append(f"Average centipawn loss {acpl}")
    errs = [(my_an or {}).get(k, 0) for k in ("inaccuracy", "mistake", "blunder")]
    if any(errs):
        def _p(n, one, many):
            return f"{n} {one if n == 1 else many}"
        facts.append(" · ".join([
            _p(errs[0], "inaccuracy", "inaccuracies"),
            _p(errs[1], "mistake", "mistakes"),
            _p(errs[2], "blunder", "blunders")]))
    if isinstance(my_rating, int) and isinstance(opp_rating, int):
        gap = opp_rating - my_rating
        facts.append(f"Rating gap {gap:+d} ({my_rating} vs {opp_rating})")
    if division:
        mg, eg = division.get("middlegame"), division.get("end")
        if mg:
            facts.append(f"Opening lasted {mg // 2} moves")
        if eg:
            facts.append(f"Endgame from move {eg // 2}")
        elif mg:
            facts.append("Never reached an endgame")
    facts.append(f"{num_moves} moves · {(speed or '?').title()}")
    return facts[:6]


def _parse_game(g: dict) -> dict:
    """Parse a raw Lichess game JSON into a display dict."""
    players  = g.get("players", {})
    white_id = players.get("white", {}).get("user", {}).get("name", "").lower()
    is_white = white_id == LICHESS_USER.lower()
    me       = players.get("white" if is_white else "black", {})
    opp      = players.get("black" if is_white else "white", {})

    winner = g.get("winner", "")
    status = g.get("status", "")
    if not winner or status == "draw":
        result, icon, cls = "Draw", "½", "draw"
    elif (winner == "white" and is_white) or (winner == "black" and not is_white):
        result, icon, cls = "Win", "✅", "win"
    else:
        result, icon, cls = "Loss", "❌", "loss"

    op   = g.get("opening", {}) or {}
    op_name = op.get("name", "Unknown").split(":")[0].strip()
    op_eco  = op.get("eco", "")

    moves_str = g.get("moves", "") or ""
    all_moves = moves_str.split()
    num_moves = len(all_moves) // 2

    termination = TERMINATION_MAP.get(status, status.title())
    speed       = g.get("speed", g.get("perf", "?"))

    my_rating  = me.get("rating", "?")
    opp_rating = opp.get("rating", "?")
    opponent   = opp.get("user", {}).get("name", "?")
    rating_diff = opp.get("ratingDiff", 0) or me.get("ratingDiff", 0) or 0

    # Rating diff for opponent tells us if we beat a stronger/weaker player
    opp_diff = opp.get("ratingDiff", None)
    me_diff  = me.get("ratingDiff", None)

    # ── Engine-derived detail (only for games Lichess has analysed) ──────────
    ply_analysis = g.get("analysis") or []
    my_an  = me.get("analysis") or {}
    opp_an = opp.get("analysis") or {}
    division = g.get("division") or {}

    best = _best_move(ply_analysis, all_moves, is_white)
    key_facts = _key_facts(g, my_an, opp_an, division, num_moves,
                           my_rating, opp_rating, speed)
    standout = _standout(g, ply_analysis, is_white, result, termination,
                         num_moves, my_an)
    game_strength = _strength_estimate(my_an.get("acpl"), my_an.get("accuracy"))
    est_fide      = _fide_equivalent(my_rating, speed)
    analysed      = bool(ply_analysis or my_an)

    # Groq analysis — now fed the engine's read of the game rather than a raw
    # move dump, which it could not evaluate anyway.
    analysis = ""
    if GROQ_KEY:
        prompt = (
            f"Chess. I played {('White' if is_white else 'Black')} (rated {my_rating}) "
            f"vs {opponent} ({opp_rating}). "
            f"Opening: {op_eco} {op_name}. Result: {result} by {termination} in {num_moves} moves. "
            f"Accuracy {my_an.get('accuracy','?')}%, ACPL {my_an.get('acpl','?')}, "
            f"{my_an.get('blunder',0)} blunders, {my_an.get('mistake',0)} mistakes. "
            f"Give ONE brutally honest, specific improvement in 20 words. Numbers where possible."
        )
        analysis = groq_complete(prompt, max_tokens=55)

    # Highlight flags
    is_upset   = result == "Win"  and isinstance(opp_rating, int) and isinstance(my_rating, int) and opp_rating > my_rating + 100
    is_collapse = result == "Loss" and isinstance(opp_rating, int) and isinstance(my_rating, int) and my_rating > opp_rating + 100
    is_long     = num_moves >= 40

    return {
        "id": g.get("id",""), "url": f"https://lichess.org/{g.get('id','')}",
        "result": result, "icon": icon, "cls": cls,
        "my_side": "White" if is_white else "Black",
        "my_rating": my_rating, "opp_rating": opp_rating,
        "opponent": opponent,
        "me_diff": me_diff, "opp_diff": opp_diff,
        "opening": op_name, "eco": op_eco,
        "moves": num_moves, "termination": termination,
        "speed": speed.title() if speed else "?",
        "analysis": analysis,
        "is_upset": is_upset, "is_collapse": is_collapse, "is_long": is_long,
        # Replaces the opening/final move dumps: what actually happened.
        "best_move": best, "key_facts": key_facts, "standout": standout,
        "game_strength": game_strength, "est_fide": est_fide,
        "accuracy": my_an.get("accuracy"), "acpl": my_an.get("acpl"),
        "analysed": analysed,
    }


def fetch_lichess_games() -> list[dict]:
    """
    Dual-mode:
    - Always: activity API → yesterday counts + 7-day trend (no token needed)
    - If LICHESS_TOKEN set: export API → full per-game analysis
    Returns list of game dicts. Mode stored in first dict's '_mode' key.
    """
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist)
    yest = now - timedelta(days=1)
    day_start = datetime(yest.year, yest.month, yest.day, 0, 0, 0, tzinfo=ist)
    day_end   = datetime(yest.year, yest.month, yest.day, 23, 59, 59, tzinfo=ist)

    yest_counts, trend = _lichess_activity_yesterday()

    # LAST SESSION, not "yesterday". Asking the export API for one fixed IST
    # day and giving up when it came back empty is what silently downgraded
    # this section: on 2026-08-10 the ledger had games on the 10th, the 8th
    # and the 7th and NONE on the 9th, so "yesterday" was legitimately empty
    # while the activity API — which buckets on its own boundary, not IST —
    # still reported two blitz games. Aggregate stats from one day sat above
    # a "no per-game analysis" notice caused by querying a different one.
    #
    # Pull a week, group by IST date, and show the most recent day that
    # actually has games. A chess section that goes blank because you did not
    # play on one specific date is measuring the calendar, not the chess.
    week_start = day_end - timedelta(days=7)
    raw = _lichess_export_games(
        int(week_start.timestamp() * 1000),
        int(day_end.timestamp() * 1000),
    )
    if raw:
        by_day: dict[str, list] = {}
        for g in raw:
            ts = g.get("createdAt")
            if not ts:
                continue
            d = datetime.fromtimestamp(ts / 1000, ist).strftime("%Y-%m-%d")
            by_day.setdefault(d, []).append(g)
        if by_day:
            newest = max(by_day)
            games = [_parse_game(g) for g in by_day[newest]]
            games[0]["_mode"]  = "full"
            games[0]["_trend"] = trend
            games[0]["_yest_counts"] = yest_counts
            # Which day this actually is. The heading said "yesterday"
            # unconditionally and was wrong every time the last session was
            # not yesterday.
            games[0]["_session_date"] = newest
            games[0]["_is_yesterday"] = (newest == yest.strftime("%Y-%m-%d"))
            return games

    # Activity-only mode — aggregate counts per speed
    if not yest_counts:
        return []
    result = []
    for speed, stats in yest_counts.items():
        w = stats.get("win", 0); l = stats.get("loss", 0); d = stats.get("draw", 0)
        t = w + l + d
        result.append({
            "_mode": "activity", "_trend": trend if not result else [],
            "speed": speed.title(), "wins": w, "losses": l, "draws": d,
            "total": t, "pct": round(w/t*100) if t else 0,
            "profile_url": f"https://lichess.org/@/{LICHESS_USER}",
        })
    return result


def get_lichess_summary(games: list[dict]) -> dict:
    if not games:
        return {}
    mode  = games[0].get("_mode", "activity")
    trend = games[0].get("_trend", [])
    session_date  = games[0].get("_session_date", "")
    is_yesterday  = games[0].get("_is_yesterday", True)

    if mode == "full":
        wins   = sum(1 for g in games if g["result"] == "Win")
        losses = sum(1 for g in games if g["result"] == "Loss")
        draws  = sum(1 for g in games if g["result"] == "Draw")
        total  = len(games)
        # Opening breakdown
        op_stats: dict = {}
        for g in games:
            op = g["opening"]
            if op not in op_stats:
                op_stats[op] = {"w":0,"l":0,"d":0,"eco": g["eco"]}
            op_stats[op][{"Win":"w","Loss":"l","Draw":"d"}.get(g["result"],"d")] += 1
        # Weakest opening
        weak_op = ""
        worst = None
        for op, s in op_stats.items():
            t = s["w"]+s["l"]+s["d"]
            if t >= 2:
                wr = s["w"]/t
                if worst is None or wr < worst:
                    worst = wr; weak_op = f"{s['eco']} {op} ({s['w']}/{t} = {round(wr*100)}% WR)"
        # Best opening
        best_op = ""
        best = None
        for op, s in op_stats.items():
            t = s["w"]+s["l"]+s["d"]
            if t >= 2:
                wr = s["w"]/t
                if best is None or wr > best:
                    best = wr; best_op = f"{s['eco']} {op} ({s['w']}/{t} = {round(wr*100)}% WR)"
        # Highlights
        upsets    = [g for g in games if g.get("is_upset")]
        collapses = [g for g in games if g.get("is_collapse")]
        long_games = [g for g in games if g.get("is_long")]
    else:
        wins   = sum(g["wins"]   for g in games)
        losses = sum(g["losses"] for g in games)
        draws  = sum(g["draws"]  for g in games)
        total  = wins + losses + draws
        weak_op = best_op = ""
        upsets = collapses = long_games = []

    pct  = round(wins/total*100) if total else 0
    icon = "✅" if pct >= 55 else ("⚖️" if pct >= 45 else "❌")

    # Groq session summary (full mode only)
    session_summary = ""
    if mode == "full" and GROQ_KEY and total:
        prompt = (
            f"Chess session analysis for AKK_010 (Lichess). "
            f"{total} games: {wins}W {losses}L {draws}D = {pct}% win rate. "
            f"Weakest opening: {weak_op}. Best opening: {best_op}. "
            f"Upsets (beat stronger): {len(upsets)}. Collapses (lost to weaker): {len(collapses)}. "
            f"Write a 3-sentence coach's verdict. Specific. Brutal. Actionable. No filler."
        )
        session_summary = groq_complete(prompt, max_tokens=120)

    return {
        "mode": mode, "total": total, "wins": wins, "losses": losses, "draws": draws,
        "pct": pct, "icon": icon, "score": f"{wins}/{total}",
        "trend": trend, "weak_op": weak_op, "best_op": best_op,
        "upsets": len(upsets) if mode=="full" else 0,
        "collapses": len(collapses) if mode=="full" else 0,
        "long_games": len(long_games) if mode=="full" else 0,
        "session_summary": session_summary,
        # Which day these games are actually from, and whether that is
        # yesterday. The section used to assert "yesterday" unconditionally.
        "session_date": session_date,
        "is_yesterday": is_yesterday,
        # True only when the secret is genuinely absent. The banner keyed off
        # "we ended up in activity mode", which is a different question — and
        # it told you to add a token that was already set.
        "token_missing": not bool(os.environ.get("LICHESS_TOKEN", "")),
    }

def fetch_lichess_puzzle() -> dict:
    """Daily Lichess puzzle with theme tip."""
    import re
    try:
        r = requests.get("https://lichess.org/api/puzzle/daily",
                         headers={"Accept": "application/json", **LICHESS_UA}, timeout=10)
        if r.status_code != 200:
            log.warning(f"Lichess puzzle: {r.status_code} — {r.text[:120]}")
            return {}
        data   = r.json()
        puzzle = data.get("puzzle", {})
        pid    = puzzle.get("id","")
        rating = puzzle.get("rating", 0)
        themes = [t for t in puzzle.get("themes",[])
                  if t not in ("master","masterVsMaster","puzzleOfTheDay")]
        def fmt(t): return re.sub(r'([A-Z])', r' \1', t).strip().title()
        theme_str = " · ".join(fmt(t) for t in themes[:3])
        tip = next((CHESS_THEME_TIPS[t] for t in themes if t in CHESS_THEME_TIPS),
                   "Calculate 3 moves deep before touching a piece.")
        diff = rating - MY_PUZZLE_RATING
        level = "🔴 stretch" if diff > 150 else ("🟡 at level" if diff > -150 else "🟢 comfort")
        return {"pid": pid, "rating": rating, "level": level,
                "themes": theme_str, "tip": tip,
                "url": f"https://lichess.org/training/{pid}"}
    except Exception as e:
        log.warning(f"Lichess puzzle: {e}")
        return {}

# ─────────────────────────────────────────────────────────────
# FP&A → CFO CAREER PATH
# ─────────────────────────────────────────────────────────────

CFO_PATH_LESSONS = [
    ("FC Step 1: Own the Close", "Financial Controllers close the books — every month, clean, on time, no surprises. Speed + accuracy is the baseline. If close takes 10 days, get it to 5. Automate reconciliations. Build a close checklist and hit it every time."),
    ("FC Step 2: IFRS Mastery", "IFRS 16 (leases), IFRS 9 (financial instruments), IFRS 15 (revenue recognition) — these are the FC's bread and butter in Dubai/GCC. Know them technically AND practically. Interviewers will test this. Study one standard per week."),
    ("FC Step 3: Internal Controls", "A Controller without strong controls is a liability. Know COSO framework. Segregation of duties. Maker-checker in AP/AR. SOX lite for listed companies. Document your controls. Auditors love documented processes."),
    ("FC Step 4: ERP Ownership", "SAP S/4HANA or Oracle Fusion — the FC owns the finance module. Know chart of accounts design, cost centre structure, intercompany eliminations, period-end close in the system. This separates candidates. SAP certification: ₹15K online."),
    ("FC Step 5: Treasury Basics", "Cash flow forecasting, FX hedging policy, bank relationship management, working capital optimisation. The jump from FP&A to FC often requires you to pick up treasury. Start with 13-week cash flow models."),
    ("CFO Step 1: Strategy + Finance", "CFOs translate business strategy into financial reality. They ask: what's the ROI, what's the IRR, what's the payback period? Practice building business cases. Every investment decision has a financial model behind it."),
    ("CFO Step 2: Investor Relations", "Listed company CFOs speak to markets quarterly. Practice presenting results: 'Revenue grew 18% YoY driven by X. EBITDA margin expanded 200bps due to Y. FY26 guidance: Z.' Short, precise, numbers-first. Record yourself."),
    ("CFO Step 3: M&A Fundamentals", "Basic M&A literacy: DCF valuation, EV/EBITDA multiples, due diligence process, SPA (Share Purchase Agreement), earn-out structures. You don't need to be an investment banker, but you need to read a deal memo and ask smart questions."),
    ("CFO Step 4: Board Communication", "CFOs present to the Board. Boards want: Are we on budget? What's the cash position? What risks keep you up at night? Practice speaking to non-finance people. Remove jargon. Lead with the conclusion, not the workings."),
    ("CFO Step 5: Culture + People", "The best CFOs build finance teams that the business trusts. Hire people better than you. Create a learning culture. A CFO who hoards information creates a bottleneck. One who shares insight creates leverage."),
    ("Dubai FC Reality", "AED 30K+ FC roles at ADNOC, Emirates, MAF, DP World: they want Big 4 background OR 8+ years in a listed company + CA/ACCA + ERP experience. Your edge: FP&A depth + IFRS knowledge + Power BI automation. Highlight cost savings you drove."),
    ("The CA→FC→CFO Timeline", "CA qualified → 2-3 years Big 4/audit → Senior FP&A → Finance Manager → Financial Controller (5-8 years post-qualification) → CFO (10-15 years). Dubai compresses this by 2-3 years if you hit the right company at the right growth stage."),
    ("FC Interview: Top 5 Questions", "1. Walk me through month-end close. 2. How do you handle a material variance? 3. What internal controls have you implemented? 4. Describe your IFRS 16 experience. 5. How do you present bad news to management? Prepare 2-min answers for all five."),
    ("CFO Interview: Top 5 Questions", "1. What's your capital allocation philosophy? 2. How do you manage a cash crisis? 3. Describe a time you stopped a bad investment. 4. How do you build trust with the CEO? 5. Walk me through a fundraise or M&A you led. Practice until smooth."),
    ("Power BI for Finance Leaders", "Build a CFO dashboard: P&L vs Budget, Cashflow waterfall, Working Capital trend, Revenue bridge. Link to ERP via DirectQuery. Refresh daily. Present this in interviews as your personal project. It shows initiative + technical skill + commercial sense."),
]

def get_cfo_lesson() -> dict:
    idx = (date.today().toordinal() + 11) % len(CFO_PATH_LESSONS)
    title, body = CFO_PATH_LESSONS[idx]
    return {"title": title, "body": body, "index": idx + 1, "total": len(CFO_PATH_LESSONS)}

# ─────────────────────────────────────────────────────────────
# CHESS TUTOR — Daily lesson rotating
# ─────────────────────────────────────────────────────────────

CHESS_LESSONS = [
    ("Opening Principle #1: Control the Centre", "Place pawns on e4/d4 (White) or e5/d5 (Black). Pieces control more squares from the centre. A pawn on e4 controls d5 and f5. A pawn on the edge controls only 1 square. Open with 1.e4 or 1.d4 and you immediately claim space. This is the foundation of all chess strategy."),
    ("Opening Principle #2: Develop Your Pieces", "In the first 10 moves, move each piece once. Get knights out before bishops (Ng1-f3, Nb1-c3). Don't move the same piece twice. Don't bring the queen out early — she gets chased. Goal: by move 8, have 3-4 pieces off the back rank. Development = time. Time = initiative."),
    ("Opening Principle #3: Castle Early", "King safety is non-negotiable in the opening. Castle within the first 10 moves. An uncastled king in the centre is a target. After castling, your king is protected by 3 pawns and 2 corner squares. Rooks also connect after castling. Rule: castle before you attack."),
    ("Tactics: The Pin", "A pin prevents a piece from moving because moving it would expose a more valuable piece behind it. Absolute pin = pinned to the king (piece cannot move legally). Relative pin = pinned to queen/rook (can move but it's a blunder). Identify pins in every position. Then exploit them."),
    ("Tactics: The Fork", "A fork attacks two pieces simultaneously with one piece. Knight forks are the deadliest — they move in an L-shape and cannot be blocked. Look for squares your knight can reach that simultaneously attack king + queen, or rook + rook. Practice: set up NxC5+ forking king and rook. Classic beginner trap."),
    ("Tactics: The Skewer", "A skewer is a reverse pin. A valuable piece is attacked, it moves, and the piece behind it is captured. Skewers happen on open files and diagonals. Rooks and bishops execute skewers. Pattern: Rook checks king → king moves → rook captures queen. Spot this on every open file."),
    ("Tactics: Discovered Attack", "Moving one piece reveals an attack from another. The moved piece itself may also attack something. Discovered checks are devastating — the opponent must deal with the check while you capture elsewhere. Pattern: move a blocking piece with tempo, reveal a rook/bishop attack. Double attacks are impossible to fully defend."),
    ("Endgame: King Activity", "In the endgame, the king becomes a fighting piece. Centralise your king immediately. A king on e4 controls d3, d4, d5, e3, e5, f3, f4, f5 — 8 squares. A king on a1 controls only 3. Rule: when queens are off the board, march your king to the centre. Opposition matters: two kings facing each other, one move apart — the side NOT to move has the opposition."),
    ("Endgame: Pawn Endgames", "If you're a pawn up in a king+pawn endgame, you usually win. Key concepts: 1. Opposition — control the square in front of your pawn. 2. The square of the pawn — if the opposing king can't enter the square, your pawn promotes. 3. Key squares — a pawn on e4's key squares are d6, e6, f6. Reach them with your king and you win."),
    ("Strategy: Weak Squares", "A weak square is one that cannot be defended by a pawn. d5 is weak for Black if the c6 and e6 pawns are gone or never played. Plant a knight on a weak square — it becomes an 'outpost.' From d5, a knight controls 8 squares and cannot be chased. Outpost knight + weak squares = long-term advantage."),
    ("Strategy: Open Files for Rooks", "Rooks need open files to be active. An open file has no pawns. A semi-open file has only enemy pawns. Double rooks on an open file (called 'Rooks on 7th') is a deadly battery. Strategy: open files by pawn trades, then occupy them immediately. Don't leave your rooks passive on the back rank."),
    ("Strategy: Pawn Structure", "Your pawn structure dictates your plan. Doubled pawns (two pawns on same file) = weakness. Isolated pawn = can't be defended by a pawn, needs piece protection. Passed pawn = no enemy pawn can stop it = future queen. Backward pawn = can't advance, target for opponent. Know your pawn structure and play accordingly."),
    ("Middlegame: Piece Coordination", "Good chess is about all your pieces working together. Ask: which of my pieces is worst placed? Improve it. A knight on the rim is dim (a1, h1 etc). A bishop blocked by its own pawns is a 'tall pawn.' Rooks need open files. Queen needs safety. Coordinate: every piece pointing at the same sector = overwhelming attack."),
    ("Calculation: The LPDO Rule", "LPDO = Loose Pieces Drop Off. Before every move, scan the board: which of my pieces are undefended? Which of my opponent's pieces are undefended? Undefended pieces are targets for tactics. A simple habit: after finding your move, ask 'does this leave anything undefended?' This alone prevents most blunders."),
    ("Time Management", "Chess is a timed game. Use 70-80% of your clock in complex positions, blitz transitions and endgames you know. Common mistake: spending 15 minutes in the opening, then rushing in the critical middlegame. Rule: if a position is complicated and you see candidate moves, spend the time here — not in simple positions. Clock is a resource, not just a constraint."),
    ("Opening: Sicilian Defence Basics", "After 1.e4 c5 — Black fights for the centre asymmetrically. Black gets queenside counterplay; White gets kingside attack. Most common openings at club level. Learn one variation: Sicilian Najdorf (1.e4 c5 2.Nf3 d6 3.d4 cxd4 4.Nxd4 Nf6 5.Nc3 a6). It's played by Kasparov, Fischer, Carlsen. Understand the plans before memorising moves."),
    ("Opening: Italian Game Basics", "1.e4 e5 2.Nf3 Nc6 3.Bc4. White targets f7, the weakest square in Black's position. Giuoco Piano (3...Bc5) leads to rich middlegame. Learn: castle early, play d3, develop bishop to e3, then push d4 for centre control. Safe, instructive, good for learning. Magnus Carlsen played this at the top level."),
    ("Thinking Process", "Every move: 1. What did my opponent just do? (threats?) 2. What are my candidate moves? (at least 2-3) 3. Calculate each candidate 2-3 moves deep. 4. Use LPDO check. 5. Make the move. Don't play on impulse. Even 30 seconds of structured thinking eliminates 80% of blunders. This process is the difference between 800 and 1200 Elo."),
    ("Study Plan: 15 Min/Day", "15 minutes daily beats 3 hours once a week. Split: 5 min puzzles (Chess.com/Lichess tactics trainer) + 5 min play (1 game, 10+0 or 15+10) + 5 min review (analyse your game, find the turning point). After 30 days: you'll feel patterns automatically. Tactics + play + review = the triangle of improvement."),
    ("Rating Progress Reality", "800→1000: Learn not to blunder pieces for free. 1000→1200: Basic tactics (forks, pins). 1200→1500: Strategy (plans, pawn structure). 1500→1800: Calculation depth + endgames. 1800+: Opening theory + deep calculation. Most adults plateau at 1000-1200 because they skip tactics training. Do 10 puzzles daily — minimum. No shortcut."),
]

def get_chess_lesson() -> dict:
    idx = (date.today().toordinal() + 13) % len(CHESS_LESSONS)
    title, body = CHESS_LESSONS[idx]
    return {"title": title, "body": body, "index": idx + 1, "total": len(CHESS_LESSONS)}

# ─────────────────────────────────────────────────────────────
# WISDOM: BETTER PERSON · BETTER DAD · HAPPY LIFE
# ─────────────────────────────────────────────────────────────

WISDOM_LESSONS = [
    ("Being Present", "Your daughter doesn't need a perfect father. She needs a present one. Put the phone down for 20 minutes when you get home. Look her in the eyes. Play on the floor. These moments are the whole point. Work can wait. She is growing every day whether you show up or not."),
    ("The Compound Effect of Small Moments", "You don't build a relationship in big moments. You build it in 10,000 small ones. Morning hugs. Saying her name with warmth. Listening when she babbles even though you don't understand. These stack. In 15 years, she'll feel either deeply loved or quietly neglected. Both are built today."),
    ("Regulate Yourself First", "You cannot be a calm parent if you're running on stress, sleep debt, and financial anxiety. Your regulation is their regulation — babies co-regulate with caregivers. Deep breath before you pick her up. Shoulders down. Slow exhale. Your nervous system sets the tone for the room."),
    ("Stoic Parenting", "Marcus Aurelius raised children while running an empire. His journal shows he constantly asked: am I reacting or responding? Reacting is automatic. Responding is chosen. The next time your baby cries at 2am, ask yourself: how do I want to respond to this? You always have a choice in the gap between stimulus and response."),
    ("Simplicity is the Path to Happiness", "Happiness research consistently shows: strong relationships + meaningful work + enough (not excessive) money = high life satisfaction. You're already building all three. Don't confuse complexity for progress. The man who simplifies wins. Less decisions, less noise, less comparison."),
    ("The Gratitude Practise", "Every morning, before checking your phone: name 3 specific things you're grateful for. Not 'health and family' — that's lazy. Something specific: 'my daughter smiled when she saw me yesterday,' 'I understood a balance sheet faster than last year,' 'KL at 26°C.' Specificity trains the brain to notice abundance."),
    ("Being a Better Husband", "The best fathers are also good partners. Your daughter watches how you treat her mother. That becomes her template for relationships. Simple acts: say thank you. Notice when your partner is tired. Take something off her plate without being asked. Ask about her day and actually listen. Small but it compounds."),
    ("On Anger", "Anger is information, not instruction. It tells you a boundary was crossed or a value was violated. But acting on anger rarely solves the problem. Practice: when you feel it rising, say 'I need 5 minutes' and leave the room. Return calm. Then address the issue. You can be firm without being explosive."),
    ("Financial Peace = Emotional Peace", "Money stress is the #1 relationship killer. Getting to AED 30K/month isn't just ambition — it's removing a major source of family stress. Every step toward financial stability is a step toward being a calmer, more present parent and partner. The job hunt matters. The income matters. It's not selfish. It's foundational."),
    ("The Long Game of Parenting", "You won't remember 2026 as a year. You'll remember it as the year your daughter started talking, your first steps in Dubai, the grind that changed everything. Keep a journal. Even one line per day. In 10 years, you'll read it and understand why it was worth it."),
    ("Wisdom from Fathers Before You", "Your father had flaws. His father did too. But they also gave you things you don't yet see. The goal isn't to be a perfect father — it's to be a slightly more conscious one. Break the patterns you don't want to pass on. Keep the ones that built you. Each generation improves a little. That's legacy."),
    ("On Failure", "You will fail your daughter sometimes. You'll be impatient. You'll miss something important. You'll get it wrong. What matters is the repair: the apology, the return, the consistent showing up after the mistake. Children don't need perfect parents. They need parents who can say sorry and mean it."),
    ("Rest is Productive", "High performers rest. Sleep is not laziness — it's when the brain consolidates learning, the body repairs, emotional regulation resets. Cutting sleep to work more is borrowing from tomorrow's performance to pay today's anxiety. Protect 7 hours. It will make you a better analyst, better father, better human."),
    ("The Identity Question", "Who are you when the job title is gone, when the market is closed, when your daughter is asleep? Define yourself by what you do, not what you've achieved. 'I am someone who learns daily. I love deeply. I show up.' These don't depend on AED 30K or a Dubai visa. They're yours now."),
    ("Letting Go of Control", "You can control your effort. You cannot control outcomes. The job market, the Instagram algorithm, the baby's sleep schedule — all outside your control. Epictetus: 'Seek not that the things which happen should happen as you wish; but wish the things which happen to be as they are, and you will have a tranquil flow of life.'"),
    ("Joy is a Practise", "Joy doesn't arrive when the conditions are right. It's practiced in the conditions you have. Find one thing today that is genuinely good. Savour it for 30 seconds. This is not toxic positivity — it's neurological training. Brains that practise noticing goodness get better at finding it, even in hard seasons."),
    ("On Discipline and Freedom", "The most disciplined people are the freest. Akshay who wakes at 6am, exercises, studies, then works has more hours and more energy than the one who stays up late and drags through mornings. Discipline is choosing what you want most over what you want now. It's the highest form of self-respect."),
    ("Your Daughter Will Watch Everything", "She'll watch how you handle a setback. How you speak about money. How you treat waiters. How you react when you're wrong. How you love her mother. You are her first example of what a man is. Be the man you'd want her to marry someday. That's the bar."),
]

def get_wisdom_lesson() -> dict:
    idx = (date.today().toordinal() + 17) % len(WISDOM_LESSONS)
    title, body = WISDOM_LESSONS[idx]
    return {"title": title, "body": body, "index": idx + 1, "total": len(WISDOM_LESSONS)}

# ─────────────────────────────────────────────────────────────
# BOOK LESSONS — Daily rotating, detailed
# ─────────────────────────────────────────────────────────────

BOOK_LESSONS = [
    (
        "Atomic Habits", "James Clear", "The 1% Rule: Why Small Gains Compound Into Extraordinary Results",
        "If you get 1% better every day for a year, you end up 37 times better. If you get 1% worse, you decay to nearly zero. Most people overestimate what they can do in a day and underestimate what they can do in a year of consistent 1% improvements. Clear's central insight: you don't rise to the level of your goals, you fall to the level of your systems. Goals are for direction. Systems are for progress.",
        "Habits are the compound interest of self-improvement.",
        "Identify one tiny habit you can improve by 1% today. Stack it onto an existing behaviour: after [CURRENT HABIT], I will [NEW HABIT]."
    ),
    (
        "Atomic Habits", "James Clear", "Identity-Based Habits: Become the Person First",
        "Most people start with outcomes (lose 10kg), then think about processes (exercise), and never examine identity (I am someone who moves their body daily). Clear argues you should flip it. Start with identity: who do you want to become? Every action you take is a vote for that identity. Cast enough votes and the identity solidifies. A smoker trying to quit says 'I'm trying to quit.' A non-smoker says 'I don't smoke.' Same external action, completely different internal frame — and the internal frame determines long-term behaviour.",
        "The most practical way to change who you are is to change what you do.",
        "Pick one identity statement: 'I am a daily learner.' 'I am someone who finishes what they start.' Then find one action today that votes for that identity."
    ),
    (
        "Atomic Habits", "James Clear", "The Four Laws of Behaviour Change",
        "Every habit is built on four components: Cue (make it obvious) → Craving (make it attractive) → Response (make it easy) → Reward (make it satisfying). To build a good habit, apply all four. To break a bad one, invert them: make it invisible, make it unattractive, make it difficult, make it unsatisfying. The two-minute rule: when starting a new habit, it should take less than two minutes. You're not running 5km yet — you're putting on your running shoes. The gateway habit becomes the habit.",
        "Reduce friction for good habits. Increase friction for bad ones.",
        "Apply the two-minute rule to one habit you've been failing to start. Make the first step so small it's impossible to say no."
    ),
    (
        "Deep Work", "Cal Newport", "The Shallow Work Trap: Why Being Busy Is Not the Same as Being Productive",
        "Shallow work is non-cognitively demanding tasks performed while distracted — email, meetings, admin. Deep work is professional activity performed in a state of distraction-free concentration that pushes your cognitive capabilities to their limit. These efforts create new value, improve your skill, and are hard to replicate. Newport's argument: the ability to do deep work is becoming increasingly rare at exactly the same time it is becoming increasingly valuable in the economy. The few who cultivate this skill will thrive.",
        "Two to four hours of genuine deep work produces more output than eight hours of fragmented shallow work.",
        "Schedule one 90-minute deep work block tomorrow. No phone, no email, one hard problem. Treat it like a meeting you cannot cancel."
    ),
    (
        "Deep Work", "Cal Newport", "The Any-Benefit Mindset vs The Craftsman Mindset",
        "Most people adopt the any-benefit mindset for tools: if a tool provides any benefit, use it. This is why everyone is on every social platform, attending every meeting, checking email all day. Newport argues for the craftsman mindset: identify the core factors that determine your success and happiness. Adopt a tool only if its positive impacts on these factors substantially outweigh its negative ones. Your attention is finite. Every tool that fragments it has a cost, even if it has some benefits. The question is never 'is there value here?' but 'is this the best use of my limited attention?'",
        "Your attention is your most valuable professional asset. Spend it deliberately.",
        "List your three most important professional skills. Then ask: does checking social media or attending this meeting help or hurt those skills?"
    ),
    (
        "The Psychology of Money", "Morgan Housel", "Wealth Is What You Don't See",
        "We judge wealth by what we see: cars, clothes, houses. But wealth is actually the money not spent — it's invisible. The person driving a ₹1 crore car might have zero savings. The person in the modest apartment might have ₹10 crore liquid. Housel's key insight: spending money to show people how much money you have is the fastest way to have less money. True wealth is options — the ability to wake up and say 'I can do what I want, when I want, with who I want.' That requires unspent money, not displayed money.",
        "Rich is a current income. Wealthy is accumulated assets. They are not the same.",
        "Track your net worth monthly, not your lifestyle. Net worth = financial freedom. Lifestyle = just looking free."
    ),
    (
        "The Psychology of Money", "Morgan Housel", "You Are Not a Spreadsheet. You Are a Person with Emotions.",
        "The financial advice that is mathematically optimal is often behaviourally impossible. A 100% equity portfolio maximises long-run returns — but if you panic and sell at the bottom of every crash, you'd have been better off in a balanced fund. Housel's argument: the best investment strategy is the one you can actually stick to. Reasonable > rational. A plan that accounts for human psychology beats a theoretically perfect plan that you'll abandon when the market drops 40%. Know your emotional tolerances. Build them into your plan.",
        "The highest return is earned by the investor who can survive the worst years, not the one with the best spreadsheet.",
        "Ask yourself: if my portfolio dropped 40% tomorrow, what would I do? Build a strategy you'd actually execute in that scenario."
    ),
    (
        "The Psychology of Money", "Morgan Housel", "Tail Events Drive Everything",
        "In investing and in life, a small number of events account for the majority of outcomes. Venture capital funds know that 1 investment out of 50 will return the entire fund. Amazon's success is almost entirely explained by AWS — which started as an internal experiment. Housel's lesson: you can be wrong most of the time and still be enormously successful if you're right on the few things that matter most. This means long tail bets matter. Staying in the game long enough to hit the tails matters. Avoiding ruin so you can be there for the rare extraordinary outcomes matters most.",
        "You only need to be right a few times in your life, as long as you don't catastrophically lose the ability to keep playing.",
        "Identify the two or three decisions in your career/portfolio that would be truly transformative. Optimise for those, not the daily noise."
    ),
    (
        "Zero to One", "Peter Thiel", "Competition Is for Losers",
        "Thiel's most contrarian idea: competition is not virtuous. Monopoly is. Competitive markets destroy profit margins as businesses race to the bottom on price. A monopoly earns outsized profits because it has no competition. Google has 90%+ search market share and 25%+ net margins. A restaurant in a crowded market has 5% margins. Thiel argues: don't compete. Create something so different that you have no competition. Ask not 'how do I beat my competitors?' but 'how do I build something where there are no competitors?'",
        "If you are competing, you are not monopolising. If you are not monopolising, you are not building a truly great business.",
        "Ask about your current work or business: who are my direct competitors? If you have many, that's a warning sign. Find the niche where you can be the only one."
    ),
    (
        "Zero to One", "Peter Thiel", "Secrets: What Truth Do Very Few People Agree With You On?",
        "Every great business is built on a secret — a truth that most people don't see or believe yet. PayPal's secret: people would use the internet for payments before the internet was trusted. Airbnb's secret: people would stay in strangers' homes. Tesla's secret: electric cars could be desirable, not just practical. Thiel's question for every entrepreneur: 'What important truth do very few people agree with you on?' This is the diagnostic for whether you're building something genuinely new or just another incremental improvement on an existing idea.",
        "The best companies are built on secrets. Secrets require that you do the uncomfortable work of thinking differently from the crowd.",
        "Write down one belief you hold about your industry or career that most people in your field would disagree with. Is there a business or career opportunity hidden in that belief?"
    ),
    (
        "Good to Great", "Jim Collins", "The Hedgehog Concept: Do One Thing Brilliantly",
        "The fox knows many things. The hedgehog knows one big thing. Collins studied 1,435 Fortune 500 companies over 40 years to find which ones went from good to great. The great ones were all hedgehogs: they found the intersection of three circles — what they are deeply passionate about, what they can be the best in the world at, and what drives their economic engine. They focused there relentlessly and ignored everything else. Good-to-great companies didn't diversify into new businesses. They went deeper into their hedgehog concept until they dominated it.",
        "Greatness comes from doing one thing so well that the world cannot ignore you.",
        "Draw your three circles: Passion / Best-in-world potential / Economic driver. Where they overlap is your hedgehog. Do you spend most of your time there?"
    ),
    (
        "Good to Great", "Jim Collins", "Level 5 Leadership: Humility + Will",
        "Collins expected great companies to be led by charismatic visionary CEOs. He found the opposite. Every good-to-great company had a Level 5 leader — someone who combined fierce professional will with personal humility. They were ambitious for the company, not themselves. When things went well, they looked out the window and gave credit to others. When things went wrong, they looked in the mirror and took responsibility. They were more like Lincoln than Patton — quietly determined rather than loudly inspiring.",
        "The most effective leaders are often the least visible. Ego is the enemy of great leadership.",
        "Next time something goes well on your team, actively give someone else the credit. Notice how it feels and what it does to team morale."
    ),
    (
        "Good to Great", "Jim Collins", "The Flywheel: No Single Defining Action",
        "No good-to-great company had a single defining moment, a killer strategy launch, or a magic programme. Instead, the transformation always followed a flywheel pattern: push the heavy flywheel, it barely moves, keep pushing consistently in one direction, it builds momentum, eventually it reaches a breakthrough — but there was no single push that did it. The mistake most organisations make is looking for the one big thing. The reality is thousands of consistent small things, all pointing the same direction, that eventually produce dramatic results.",
        "Sustainable success is built through consistent, compounding effort — not single dramatic breakthroughs.",
        "Identify your flywheel: what is the core loop in your career or business that, if pushed consistently, would build unstoppable momentum?"
    ),
    (
        "Thinking, Fast and Slow", "Daniel Kahneman", "System 1 vs System 2: Two Ways Your Brain Decides",
        "Kahneman's central framework: System 1 is fast, automatic, emotional, unconscious — it drives 95% of decisions. System 2 is slow, deliberate, logical, effortful — it's what we think we use but rarely do. The problem: System 1 is riddled with biases and heuristics that made sense in evolutionary environments but lead to poor decisions in modern ones. Loss aversion (losses feel 2× more painful than equivalent gains feel good), anchoring (first number heard influences all subsequent judgements), availability bias (things that come to mind easily feel more probable) — all System 1 errors. Recognition is the first step to override.",
        "You are not as rational as you think. Your brain is a pattern-matching machine, not a logic processor.",
        "Before your next important decision, write down your reasoning explicitly. Externalising it activates System 2 and reveals System 1 shortcuts."
    ),
    (
        "Thinking, Fast and Slow", "Daniel Kahneman", "The Planning Fallacy: Why Every Project Takes Longer",
        "Kahneman and Tversky proved that humans systematically underestimate how long tasks take and overestimate how much they'll accomplish. Why? We build plans based on the best-case scenario and ignore what happened on similar projects in the past (outside view). The fix: reference class forecasting. Before estimating a project's timeline, ask 'how long did similar projects actually take?' The answer from historical data is almost always longer than your optimistic internal estimate. The cure for the planning fallacy is disciplined use of the outside view.",
        "Your intuitive project estimate is probably optimistic by 50-200%. Add a buffer based on historical data, not hope.",
        "For your next project, find 3 similar past projects. Average their actual completion time. Use that as your baseline estimate."
    ),
    (
        "Principles", "Ray Dalio", "Radical Transparency and Believability-Weighted Decision Making",
        "Dalio built Bridgewater into the world's largest hedge fund on two principles: radical transparency (everyone says what they really think, no politics, no hidden agendas) and believability-weighted decisions (not all opinions are equal — weight each person's input by their track record and expertise in that specific area). The combination creates a meritocracy of ideas where the best idea wins regardless of who had it. Most organisations do the opposite: hide information, weight opinions by seniority, and let politics determine outcomes. The result is poor decisions made confidently.",
        "An organisation where people say what they think and decisions are made on merit will outperform one run on hierarchy and politics.",
        "In your next team discussion, explicitly state your confidence level and reasoning. Encourage others to challenge your view if they disagree."
    ),
    (
        "Principles", "Ray Dalio", "Pain + Reflection = Progress",
        "Dalio's formula for growth: Pain + Reflection = Progress. Every painful experience — a mistake, a failure, a loss — contains information that, if properly processed, makes you better. The mistake most people make is avoiding pain (denial, blame) or wallowing in it (self-pity, rumination) without converting it into learning. Dalio's habit after any significant setback: write down what happened, what you could have done differently, and what principle you need to update or add. He built Bridgewater's entire operating manual from 40 years of documented mistakes and learnings.",
        "Your mistakes are your most valuable teachers. Only those who extract the lesson grow faster than those who don't make mistakes at all.",
        "After your next significant failure or setback, write a post-mortem: what happened, why, what you'd do differently, what principle to add."
    ),
    (
        "The Lean Startup", "Eric Ries", "Build-Measure-Learn: Stop Building What Nobody Wants",
        "The biggest waste in startups — and in corporate innovation — is building products nobody wants. Ries's solution: the Build-Measure-Learn loop. Start with a hypothesis about what customers want. Build the minimum viable product (MVP) to test it — not a polished product, just enough to learn. Measure how real customers actually behave. Learn whether your hypothesis was right. Pivot or persevere based on evidence, not ego. The goal is to minimise the total time through the loop. Most teams optimise for building fast. They should optimise for learning fast.",
        "Speed of learning, not speed of building, is the true competitive advantage of innovative teams.",
        "Identify one assumption behind your current project. What is the cheapest, fastest test you could run to validate or invalidate it this week?"
    ),
    (
        "The Lean Startup", "Eric Ries", "Validated Learning and Vanity Metrics",
        "Ries distinguishes actionable metrics (data that changes decisions) from vanity metrics (numbers that make you feel good but don't drive decisions). Page views, social followers, downloads — these feel like traction but often obscure the real question: are people actually getting value? The metric that matters is whether customers are changing their behaviour because of your product. Cohort analysis (tracking a specific group over time) beats aggregate totals. Retention beats acquisition. Revenue per user beats total users. Always ask: if this metric goes up, do I know what to do differently?",
        "If a metric doesn't change your decisions, it's a vanity metric. Build dashboards around actionable data only.",
        "Audit your current metrics. For each one, ask: 'if this number doubles, what would I do differently?' If the answer is 'nothing,' drop the metric."
    ),
    (
        "Shoe Dog", "Phil Knight", "Just Start. Ship the Prototype.",
        "Phil Knight's memoir of building Nike is a masterclass in starting before you're ready. He started by importing Japanese running shoes from Onitsuka Tiger with no retail experience, no capital, no strategy. He sold them from the boot of his car at track meets. He kept the company alive for 12 years on the edge of bankruptcy, constantly one rejected bank loan away from collapse. His key lesson: don't wait until you have it figured out. Start with the version you can afford. Learn on real customers with real money at stake. Iterate from there. Nike's swoosh was designed by a student for $35.",
        "The perfect plan never survives first contact with reality. Start imperfectly and iterate.",
        "Identify one project you've been waiting to start until conditions are better. What is the smallest possible version you could execute this week?"
    ),
    (
        "7 Habits of Highly Effective People", "Stephen Covey", "Begin With the End in Mind",
        "Covey's second habit: all things are created twice — first in the mind, then in the physical world. Everything you build, achieve, or create begins as a mental blueprint. Most people let life happen to them rather than designing it deliberately. The exercise: write your own eulogy. What do you want people to say about you as a parent, partner, professional, community member? That vision becomes your personal mission statement. Every decision then gets filtered through it: does this action align with the person I'm trying to become? This is the difference between living by design and living by default.",
        "If you don't define success for yourself, you'll spend your life achieving someone else's definition of it.",
        "Write 3 sentences about the legacy you want to leave as a father and as a finance professional. Read them every morning this week."
    ),
    (
        "7 Habits of Highly Effective People", "Stephen Covey", "Habit 1: Be Proactive — The 90/10 Principle",
        "10% of life is what happens to you. 90% is how you respond. This is the core of proactivity: between stimulus and response, there is a gap. In that gap is your freedom to choose. Reactive people let their moods and circumstances determine their behaviour. Proactive people subordinate impulse to values. Covey's Circle of Influence vs Circle of Concern: proactive people focus on what they can control (Circle of Influence) and it expands. Reactive people focus on what they cannot control (Circle of Concern) — market conditions, other people's behaviour, the economy — and feel increasingly helpless.",
        "Focus on what you can control. Your response is always within your Circle of Influence.",
        "List 3 things worrying you. Categorise each: can I influence this or not? Only spend energy on the ones you can influence."
    ),
    (
        "7 Habits of Highly Effective People", "Stephen Covey", "Habit 4: Think Win-Win or No Deal",
        "Most people operate from scarcity — if you win, I lose. Win-win thinking comes from an abundance mentality: there is enough for everyone, and cooperation creates more value than competition. Covey's most important corollary: 'or No Deal.' If a genuine win-win solution cannot be found, the integrity move is to agree not to deal. Not every partnership, transaction, or negotiation should happen. The willingness to walk away from a bad deal is what makes you trustworthy in good ones. People who always find a way to close the deal always find a way to disadvantage one party.",
        "Win-win is not compromise. It's a creative solution where both parties genuinely benefit.",
        "In your next negotiation or conflict, explicitly ask: 'what does the other person actually need here?' Then find a solution that addresses both needs."
    ),
    (
        "The Hard Thing About Hard Things", "Ben Horowitz", "The Struggle Is Not a Bug. It's a Feature.",
        "Horowitz's book is the antidote to every startup fairytale. He describes 'the Struggle' — the period when everything is going wrong, you have no good options, you're alone, and nothing in your background prepared you for this moment. His message: the Struggle is not a sign you're doing it wrong. It's the nature of building something hard. The skills that get you through the Struggle — making decisions with incomplete information, maintaining team morale in crisis, managing your own psychology when the business is burning — cannot be learned in school. They are learned only by going through it.",
        "There are no silver bullets in building a company. Only lead bullets — doing the hard, unglamorous work when everything is broken.",
        "Identify the thing you're avoiding right now because it's uncomfortable. That avoidance is costing you more than the discomfort of doing it."
    ),
    (
        "The Hard Thing About Hard Things", "Ben Horowitz", "Peacetime CEO vs Wartime CEO",
        "Horowitz's most cited framework: peacetime CEOs follow rules, build consensus, develop people. Wartime CEOs break rules, make unilateral calls, prioritise survival. The same leadership style that builds a great culture in a period of growth will lose the company in a crisis. The mistake most leaders make: being a peacetime CEO in wartime. When the company is burning — cash running out, key customer leaving, competitor eating your lunch — you cannot run consensus meetings. You need to make fast, clear decisions, even imperfect ones, and demand execution. Know which mode you're in.",
        "Leadership style must match the situation. A peacetime approach in wartime is as dangerous as a wartime approach in peacetime.",
        "What mode is your current situation in — peacetime (optimise) or wartime (survive)? Is your leadership style calibrated to it?"
    ),
    (
        "Essentialism", "Greg McKeown", "The Disciplined Pursuit of Less",
        "Essentialism is not about doing less for the sake of it. It's about doing less so you can do the most important things better. McKeown's central question: 'Is this the most important thing I could be doing with my time right now?' Most non-essentialists say yes to almost everything — more responsibility, more projects, more commitments. They become diffuse, mediocre at many things. The essentialist says yes to almost nothing, then executes the few things with total focus and energy. The paradox: doing less produces more impact because the energy is concentrated.",
        "If you don't prioritise your life, someone else will.",
        "List everything on your current plate. Identify the one thing that, if done brilliantly, would make the most difference. Protect that first."
    ),
    (
        "Essentialism", "Greg McKeown", "The 90% Rule: If It's Not a Hell Yes, It's a No",
        "McKeown's 90% rule for decisions: when evaluating an opportunity, give it a score from 0 to 100 on fit with your most important criteria. If it scores below 90, say no. This sounds extreme. In practice, it forces clarity: most things we say yes to are 60-70% fits — good enough, not great. The 60% fits crowd out the 90% opportunities that actually advance your most important goals. Every yes is an implicit no to everything else. The question is not 'is this a good opportunity?' but 'is this the best opportunity I have right now?'",
        "Every yes costs you a no somewhere else. Make sure the trade is worth it.",
        "Apply the 90% rule to your next opportunity or request. Score it honestly. If it's below 90, practice saying a polite no."
    ),
    (
        "The ONE Thing", "Gary Keller", "What's the ONE Thing That Makes Everything Else Easier?",
        "Keller's focusing question: 'What is the ONE Thing I can do, such that by doing it, everything else becomes easier or unnecessary?' This question forces prioritisation at the deepest level. Most to-do lists are lists of tasks with no hierarchy. The focusing question demands you find the task that is the linchpin — the one that, if done, makes other tasks unnecessary or simpler. Applied to a business: the one customer that unlocks others. Applied to a career: the one skill that opens all doors. Applied to a day: the one call that makes the rest of the day productive.",
        "Extraordinary results are directly determined by how narrow you can make your focus.",
        "Every morning, before opening email, answer: 'What is the ONE Thing I can do today that would make everything else easier?' Do that first."
    ),
    (
        "Start With Why", "Simon Sinek", "The Golden Circle: Why, How, What",
        "Most companies communicate from the outside in: what they do, then how they do it, then why. Apple communicates from the inside out: why first (we believe in challenging the status quo), then how (by making beautifully designed, simple products), then what (we make computers and phones). The why speaks to the limbic brain — the emotional, decision-making centre. The what speaks to the neocortex — logical but not the driver of decisions. People don't buy what you do, they buy why you do it. This is why Apple users are more loyal than Dell users, even when specs are comparable.",
        "People don't buy what you do. They buy why you do it. And they don't care about your what until they believe your why.",
        "Write your personal WHY in one sentence: 'I exist to _____ so that _____.' Does your daily work reflect this?"
    ),
    (
        "Thinking in Bets", "Annie Duke", "Resulting: Don't Judge Decisions by Outcomes",
        "Professional poker player Annie Duke's core insight: we judge decisions by outcomes (resulting), but the quality of a decision is independent of its outcome. A bad decision can produce a good outcome (lucky). A good decision can produce a bad outcome (unlucky). If you judge your decisions by results, you'll reinforce bad thinking when it gets lucky and abandon good thinking when it gets unlucky. The fix: evaluate decisions by the quality of the reasoning and information available at the time, not by what happened. This is how professional investors, poker players, and military strategists think.",
        "A great decision made with the information available can still produce a bad outcome. That doesn't make it a bad decision.",
        "Review your last 3 major decisions. For each one, ask: was this a good decision given what I knew? Separate the quality of the process from the result."
    ),
    (
        "Thinking in Bets", "Annie Duke", "Seek Disconfirming Evidence",
        "Humans are wired for confirmation bias — we notice and remember information that confirms our existing beliefs and ignore or discount information that challenges them. Duke's remedy: actively seek people and information that disagree with you. Before committing to a position, ask 'what would have to be true for me to be wrong?' Find the strongest version of the opposing argument (steelmanning, not strawmanning). The goal is not to change your mind on everything — it's to update your beliefs proportionally to the evidence. Strong opinions, weakly held.",
        "Your best thinking comes from actively testing your beliefs against the strongest opposing evidence.",
        "Before your next important decision, write the strongest possible case for the opposite position. Does it change your thinking?"
    ),
    (
        "Outliers", "Malcolm Gladwell", "The 10,000 Hour Rule and What It Actually Means",
        "Gladwell popularised the 10,000 hour rule from Anders Ericsson's research: world-class expertise requires roughly 10,000 hours of deliberate practice. But the key word is deliberate — not just doing something for 10,000 hours, but practising with specific feedback, at the edge of your ability, working on weaknesses. The Beatles didn't just play 10,000 hours; they played 1,200 live shows in Hamburg, often 8 hours a night, which forced rapid skill development under pressure. Bill Gates didn't just use computers; he had access to one of the world's first terminals at 13 and coded obsessively for years before dropping out of Harvard.",
        "Talent is overrated. Accumulated deliberate practice, often enabled by unusual early access, is underrated.",
        "In your primary skill (FP&A), identify the one sub-skill you're weakest at. Design deliberate practice for it: feedback, difficulty, repetition."
    ),
    (
        "Mindset", "Carol Dweck", "Fixed vs Growth Mindset: The Belief That Changes Everything",
        "Dweck's 30 years of research found that children (and adults) hold one of two fundamental beliefs about their abilities. Fixed mindset: abilities are innate — you're either smart or you're not. This leads to avoiding challenges (to avoid looking dumb), giving up when things get hard, and feeling threatened by others' success. Growth mindset: abilities develop through dedication and hard work. This leads to embracing challenges, persisting through setbacks, and finding inspiration in others' success. The most important finding: the mindset is not fixed. It can be changed by learning about it.",
        "The belief that your abilities are fixed is the only thing that makes them fixed.",
        "Notice your self-talk after a failure today. Is it fixed ('I'm not good at this') or growth ('I haven't learned this yet')? The word 'yet' is the most powerful in the growth mindset."
    ),
    (
        "Grit", "Angela Duckworth", "Passion and Perseverance: The Formula That Beats Talent",
        "Duckworth's research across West Point cadets, Scripps spelling bee champions, and sales teams found that the most important predictor of success was not talent, IQ, or physical fitness. It was grit — the combination of passion for a long-term goal and the perseverance to pursue it despite obstacles and setbacks. Grit predicts success better than talent in almost every domain. The most talented people often lack grit because things have come too easily — they've never had to develop the capacity to push through failure. The grittier person with modest talent consistently outperforms the talented person who quits.",
        "Talent × effort = skill. Skill × effort = achievement. Effort counts twice.",
        "Identify one long-term goal you've been inconsistent about. What would it look like to commit to it with grit — showing up daily regardless of motivation?"
    ),
    (
        "Built to Last", "Jim Collins & Jerry Porras", "Preserve the Core, Stimulate Progress",
        "Collins and Porras studied 18 visionary companies (3M, P&G, Disney, HP) against comparison companies over 100 years. The visionary companies were not more focused on profit — they were more focused on a core ideology (core values + purpose) that never changed. But within that fixed core, they drove relentless change in strategy, tactics, products, and people. The paradox: the companies most resistant to changing their values were the most agile in changing everything else. The core ideology acted as an anchor, giving the freedom to experiment. Companies that change their values with market fashion have no anchor and drift.",
        "Know what must never change (your values and purpose). Change everything else constantly.",
        "Write your 3 non-negotiable core values. For each, ask: does my current work reflect this? Would I keep this value even if it cost me money?"
    ),
    (
        "The E-Myth Revisited", "Michael Gerber", "Work ON Your Business, Not IN It",
        "Gerber's central argument: most small businesses are started by technicians who love their craft — a baker who opens a bakery, an accountant who opens a firm. They are brilliant at the technical work but have no idea how to build a business. The fatal mistake: working in the business (doing the craft) instead of on it (building the systems). The goal is to build a business that can run without you. Every process should be documented. Every role should have a system. The business should be a franchise prototype — replicable, teachable, and not dependent on any one person's genius.",
        "If your business can't run without you, you don't own a business — you own a job.",
        "Identify one thing only you do in your work that, if systematised, could be done by someone else. Write the system this week."
    ),
    (
        "Never Split the Difference", "Chris Voss", "Tactical Empathy: The Most Powerful Negotiation Tool",
        "FBI hostage negotiator Chris Voss's core technique: tactical empathy. Not sympathy (feeling what they feel) but empathy (understanding what they feel and why). In any negotiation, the other party has emotional needs underneath their stated position. Label those emotions: 'It seems like you're frustrated by the timeline.' 'It sounds like you feel the value isn't there.' Labelling emotions defuses them and builds trust. When people feel understood, they are more open to creative solutions. The biggest mistake in negotiation is treating it as purely rational — it is almost always emotional first.",
        "The fastest path to yes in any negotiation is to make the other person feel genuinely understood first.",
        "In your next difficult conversation, try one label: 'It seems like...' or 'It sounds like...' and see what happens to the tone."
    ),
    (
        "Never Split the Difference", "Chris Voss", "The Power of No and Calibrated Questions",
        "Voss argues that 'no' is not the opposite of 'yes' in negotiation — it's the beginning. When someone says no, they feel safe. They feel in control. A fake yes (where they agree to get you off their back) is far worse than a genuine no. His technique: ask questions that invite 'no.' 'Is now a bad time to talk?' gets a more honest response than 'Is now a good time?' His other key tool: calibrated questions — open-ended questions starting with 'what' and 'how' that force the other person to problem-solve. 'How am I supposed to do that?' puts the problem back on them without being confrontational.",
        "'No' means 'I'm not comfortable yet.' Your job is to find out what would make them comfortable.",
        "In your next negotiation, replace 'Can you do X?' with 'How can we make X work?' Notice how differently people respond."
    ),
    (
        "The Innovator's Dilemma", "Clayton Christensen", "Why Great Companies Fail at Exactly the Right Moment",
        "Christensen's paradox: the best-managed companies, doing everything right — listening to customers, investing in quality, maximising margins — are the most vulnerable to disruption. Disruptive technologies start at the bottom of the market (cheaper, simpler, worse performance) serving customers who don't exist yet. They improve over time until they're good enough for mainstream customers. By then, it's too late for incumbents to respond. Nokia had better technology than Apple in 2007. Blockbuster had better retail locations than Netflix. They failed not despite their excellence but because of it — their success made them unable to cannibalise themselves.",
        "Excellence in executing today's business model can blind you to the business model that will replace it.",
        "Ask about your industry: what is the simpler, cheaper, 'worse' solution that currently serves the bottom of the market? Could it eventually serve everyone?"
    ),
    (
        "Made in America", "Sam Walton", "The 10 Rules That Built Walmart",
        "Sam Walton built the world's largest retailer from a small Arkansas five-and-dime store. His rules: commit to your business; share profits with employees; energise your associates; communicate everything; appreciate associates; celebrate successes; listen to everyone in your company; exceed customers' expectations; control expenses; swim upstream (do the opposite of what everyone else does). The most counterintuitive: Walmart succeeded by treating employees as partners and sharing information and profits broadly, while competitors treated labour as a cost to minimise. Also: Walton flew his own plane to personally visit stores until he was 70.",
        "The best business intelligence comes from people closest to the customer. Go there yourself and listen.",
        "Identify someone in your team or organisation who is closest to the customer/problem. Ask them what they're seeing that leadership doesn't know about."
    ),
    (
        "High Output Management", "Andy Grove", "The Most Valuable Output of a Manager",
        "Andy Grove, Intel CEO, wrote the definitive book on management. His central insight: the output of a manager is the output of their team and the teams they influence. You are not paid to do your own work well — you are paid to maximise the output of those around you. The most leveraged activity for a manager: training and coaching. One hour of coaching that makes 10 people 10% more effective is worth 10 times more than 10 hours of personal output. His concept of 'managerial leverage' — activities that multiply the output of many — is the framework for deciding how to spend every hour.",
        "A manager's productivity is measured by the productivity they enable in others, not by their own output.",
        "As a future financial controller: identify one skill you could teach someone on your team that would multiply your collective output."
    ),
    (
        "High Output Management", "Andy Grove", "OKRs: Objectives and Key Results",
        "Grove invented OKRs at Intel in the 1970s. John Doerr brought them to Google in 1999. The structure: Objective (qualitative, inspirational — where do we want to go?) + Key Results (quantitative, measurable — how will we know we got there?). Rules: 3-5 OKRs per quarter. Each with 3-5 key results. They should be set at 60-70% achievable — if you always hit 100%, your targets are too easy. OKRs should be public so every team member can see how their work connects to company goals. The discipline of writing OKRs forces clarity on what actually matters and exposes misalignment between teams.",
        "What gets measured gets managed. What gets publicly committed to gets done.",
        "Write one personal OKR for this quarter: Objective (where you want to be in your career by end of Q3) + 3 Key Results (how you'll know you got there)."
    ),
    (
        "The 4-Hour Work Week", "Tim Ferriss", "The 80/20 of Everything: Most Effort Is Wasted",
        "Ferriss's most useful contribution: ruthless application of Pareto's 80/20 principle to everything. 20% of customers generate 80% of revenue — and usually 120% of headaches. 20% of tasks generate 80% of results. 20% of foods cause 80% of health problems for a given person. The exercise: list every customer, task, and activity. Identify the 20% producing 80% of value. Do more of that. Then identify the 20% consuming 80% of your time and energy with minimal value. Eliminate, automate, or delegate it. Most knowledge workers are busy with the bottom 80% and neglect the top 20%.",
        "Being busy is not the same as being productive. Most of what we do doesn't matter nearly as much as we think it does.",
        "List your 10 most frequent work activities. Rank by impact. The bottom 3 — can any be eliminated, automated, or delegated?"
    ),
    (
        "Poor Charlie's Almanack", "Charlie Munger", "Mental Models: Think in Multiple Frameworks",
        "Charlie Munger's approach to decision-making: build a 'latticework of mental models' from multiple disciplines. The person who only knows accounting sees every problem through accounting. The person who knows psychology, economics, biology, physics, and history sees the problem in full. Munger's rule: you need 80-90 mental models to think well. The key ones: compound interest (mathematics), evolution (biology), supply and demand (economics), confirmation bias (psychology), systems thinking (engineering). His most important: 'I never allow myself to have an opinion I can't defend with the strongest arguments on the other side.'",
        "The person with one framework is fragile. The person with many frameworks is antifragile — they see what others miss.",
        "Pick one field outside finance to study this month: psychology, history, or biology. Look for one principle that applies directly to your FP&A work."
    ),
    (
        "Poor Charlie's Almanack", "Charlie Munger", "Inversion: Solve Problems Backwards",
        "Munger's most powerful thinking tool: inversion. Instead of asking 'how do I succeed?' ask 'what would guarantee failure, and how do I avoid that?' The great German mathematician Jacobi said: 'Invert, always invert.' Applied to business: instead of 'how do we grow revenue?' ask 'what is destroying our revenue and how do we eliminate it?' Applied to life: instead of 'how do I be happy?' ask 'what makes people miserable and how do I avoid those things?' Avoiding stupidity is often more important than pursuing brilliance.",
        "The most reliable path to success is to identify and systematically avoid the most common causes of failure.",
        "Think of your biggest current goal. List the top 5 things that would guarantee you fail at it. Now focus on eliminating those."
    ),
    (
        "Antifragile", "Nassim Taleb", "Build Systems That Gain From Disorder",
        "Taleb's central idea: the opposite of fragile is not robust (survives shocks unchanged) — it is antifragile (gets stronger from shocks). Your muscles are antifragile — stress them and they grow. Your immune system is antifragile. Most institutions are fragile — they optimise for efficiency and have no slack, so unexpected shocks destroy them. To build antifragility: have optionality (many small bets, not one big one), avoid debt (debt makes you fragile to cash flow shocks), benefit from volatility (career skills that are more valuable in crisis than in calm), and build redundancy (slack is not waste — it's the shock absorber).",
        "Don't just plan to survive crises. Build a life and career that becomes stronger because of them.",
        "Identify one area of your life (financial, professional, health) that is fragile — one shock away from breaking. What would make it antifragile?"
    ),
    (
        "The Checklist Manifesto", "Atul Gawande", "Checklists Are Not for Dumb People. They're for Experts.",
        "Surgeon Atul Gawande studied why brilliant surgeons and pilots make elementary errors. The answer: not incompetence but complexity. Modern tasks have too many steps for any brain to hold reliably under pressure. His solution, borrowed from aviation: checklists. A two-minute surgical checklist reduced surgical complications by 36% and deaths by 47% in a global study. The lesson for knowledge work: expertise does not eliminate the need for process discipline. The most error-prone moments are not the hard parts of a task — they're the routine parts where experts assume they can proceed from memory. They can't.",
        "Checklists are not about distrust of expertise. They are about respecting the limits of human memory under pressure.",
        "Identify your most error-prone recurring process (month-end close, financial review). Write a checklist for it this week. Use it next time."
    ),
    (
        "Measure What Matters", "John Doerr", "Stretch Goals Change What's Possible",
        "Doerr profiles how OKRs drove Google's 10× growth. The key principle: set 'stretch goals' that are uncomfortable — goals that you're not sure you can achieve. A goal you can definitely hit in normal circumstances is not ambitious enough. Google's rule: OKRs should score 0.6-0.7 (60-70% achieved) consistently. If you're hitting 1.0 every quarter, your targets are too conservative. This is counterintuitive for finance professionals trained to hit budgets. In innovation contexts, the right level of ambition makes success feel slightly out of reach — that tension drives breakthrough thinking, not incremental improvement.",
        "If you never miss a target, your targets are too safe. Set goals that force you to change how you work, not just do more of what you already do.",
        "Set one 10× goal for your career in the next 2 years. Not 10% better — 10× different. What would have to change for that to happen?"
    ),
]

def _way_placeholder() -> dict:
    """Empty-but-renderable Way context for the error path."""
    blank = {"title": "Loading", "body": "", "action": "", "index": 0, "total": 1}
    return {"minimalism": dict(blank), "etiquette": dict(blank),
            "stillness": dict(blank), "model": dict(blank), "drill": dict(blank),
            "health": dict(blank),
            "arabic": {"script": "", "translit": "", "meaning": "", "use": "",
                       "index": 0, "total": 1}}


def _review_placeholder() -> dict:
    from datetime import date as _d
    y, w, wd = _d.today().isocalendar()
    return {"prompt": "Loading", "why": "", "index": 0, "total": 1,
            "week": w, "year": y, "key": f"{y}-W{w:02d}", "weekday": wd,
            "days_left": 7 - wd, "is_review_day": wd >= 6}


def get_review() -> dict:
    """Weekly review frame. Never raises."""
    try:
        import way
        return way.get_review()
    except Exception as e:
        log.warning(f"review: {e}")
        return _review_placeholder()


def get_way() -> dict:
    """Daily tracks for The Way. Falls back to placeholders, never raises —
    a content module must not be able to take the whole page down."""
    try:
        import way
        return way.get_way()
    except Exception as e:
        log.warning(f"way: {e}")
        return _way_placeholder()


def get_book_lesson() -> dict:
    idx = (date.today().toordinal() + 23) % len(BOOK_LESSONS)
    book, author, chapter, lesson, key_quote, action = BOOK_LESSONS[idx]
    out = {
        "book": book, "author": author, "chapter": chapter,
        "lesson": lesson, "key_quote": key_quote, "action": action,
        "index": idx + 1, "total": len(BOOK_LESSONS),
        "crux": [], "learnings": [], "examples": [], "adapt": [],
    }
    # Book-level depth: the whole book in 10+ points, plus learnings, worked
    # examples and how it applies here. Keyed by title, so all three Atomic
    # Habits chapters share one crux instead of repeating it. Missing books
    # degrade to the chapter lesson alone.
    try:
        from book_deep import deep_for
        out.update({k: v for k, v in deep_for(book).items() if v})
    except Exception as e:
        log.warning(f"book_deep: {e}")
    return out

# ─────────────────────────────────────────────────────────────
# DAUGHTER — age derived, never hardcoded
# ─────────────────────────────────────────────────────────────

DAUGHTER_BORN = date(2025, 12, 25)

_MONTH_WORDS = ("Newborn", "One month", "Two months", "Three months", "Four months",
                "Five months", "Six months", "Seven months", "Eight months",
                "Nine months", "Ten months", "Eleven months")


def daughter_age(on: date | None = None) -> dict:
    """Her age today, in the units a parent actually uses.

    Under two years people say months; after that, years. The section heading,
    the milestone band and the Spanish sentence about her all read from here,
    so they cannot drift apart or go stale.
    """
    on = on or datetime.now(IST).date()
    months = (on.year - DAUGHTER_BORN.year) * 12 + (on.month - DAUGHTER_BORN.month)
    if on.day < DAUGHTER_BORN.day:
        months -= 1
    months = max(0, months)
    days = (on - DAUGHTER_BORN).days

    if months < 24:
        word = _MONTH_WORDS[months] if months < len(_MONTH_WORDS) else f"{months} months"
        heading = f"{word} old." if months != 1 else "One month old."
    else:
        years = months // 12
        rem = months % 12
        heading = f"{years} year{'s' if years > 1 else ''} old." if not rem \
                  else f"{years}y {rem}m old."

    # Bands match how developmental guidance is actually written.
    band = ("newborn" if months < 3 else "infant" if months < 7
            else "crawling" if months < 10 else "cruising" if months < 13
            else "toddler" if months < 24 else "preschool")
    return {"months": months, "days": days, "heading": heading, "band": band,
            "born": DAUGHTER_BORN.isoformat()}

# ─────────────────────────────────────────────────────────────
# PAGE SPLIT
#
# One page carrying nineteen sections served five unrelated readers and put a
# 19-item nav behind a horizontal scroller. The build emits two pages from the
# same template instead:
#
#   /       the financial product — markets, ideas, the book, the record
#   /desk   the practice — languages, fatherhood, chess, music, drills
#
# Same data, same styles, same components. A section belongs to exactly one
# page and the nav is generated from this list, so the two can never drift
# out of order the way the hand-written nav did.
# ─────────────────────────────────────────────────────────────

# ── Engine log ───────────────────────────────────────────────────────────────
# Every rule change made because the ledger said so, with the number that
# forced it. Server-rendered and static on purpose: this sits in its own
# always-visible section rather than inside #perf, because #perf starts
# display:none and is only revealed once the ledger API answers. On the static
# snapshot that section never appears, and a changelog nobody can read on a bad
# day is worth nothing.
#
# `verdict` is deliberately not always "adopted". A log that only records
# changes which worked is marketing; the rejected entries are the ones that
# make the adopted ones worth believing.
ENGINE_CHANGES = [
    {
        "date": "2026-08-27",
        "tag": "CADENCE",
        "verdict": "logged",
        "title": "The multibagger screen qualified nobody for four weeks",
        "body": ("Corrects what this entry said on 2026-08-27. It read \u2018the scan is "
                 "running on schedule\u2019 and blamed the fixed list on cadence. The "
                 "ledger says otherwise: the last multibagger row is 2026-08-01, "
                 "while magic, magicmagic and ai_longterm \u2014 the same Saturday job, "
                 "the same slot \u2014 all logged 2026-08-22. Twenty-six days, three "
                 "Saturdays, nothing written."),
        "evidence": [("Last multibagger row", "2026-08-01", "all_signals", "26 days"),
                     ("Siblings, same job", "2026-08-22", "all_signals", "current"),
                     ("Raw candidates today", "11", "weekly screen", "ran fine"),
                     ("Cleared the R:R floor", "1 of 11", "R:R \u2265 2.0", "10 rejected")],
        "note": ("The screen is not broken. Run on 2026-08-27 it took 11 candidates and "
                 "rejected 10 for R:R below 2.0 \u2014 CHENNPETRO 1.29, FLUOROCHEM 1.07, "
                 "GLAXO 0.59, which needs a 63% win rate merely to break even. The "
                 "defect was that a week qualifying nobody wrote nothing at all, so the "
                 "page served the 1 August list as though it were current. The attempt "
                 "is recorded either way now. The gate is NOT loosened: publishing a "
                 "0.59 R:R to keep the section busy is the exact trade this screen "
                 "exists to refuse, and an empty week is a real result."),
    },
    {
        "date": "2026-08-09",
        "tag": "SELECTION",
        "verdict": "adopted",
        "title": "Stop buying strength that has already run",
        "body": ("The 4H RSI ceiling for longs was 75. Splitting every closed trade by "
                 "the RSI recorded at entry showed the engine bleeding above 65 and "
                 "profitable below it. The ceiling is now 65."),
        # No raw "<" or "&" in any string here. generate.py renders with
        # jinja2.Template(), which defaults to autoescape=False, while the
        # Flask path escapes. A literal "BUY, 4H RSI < 65" survived locally and
        # silently truncated to "BUY, 4H RSI" in the built page, because the
        # browser read "< 65</th><td>" as a tag. Comparison words, not glyphs.
        "evidence": [("BUY, 4H RSI 65 and over", "−0.599R", "n=90", "−5.6 SE"),
                     ("BUY, 4H RSI under 65", "+0.274R", "n=67", "+1.5 SE")],
        "note": ("Holds in both halves of the sample and at every cut point tested "
                 "(60, 65, 70), so it is not one lucky threshold. The threshold was "
                 "still chosen from this sample, so treat the size as an upper bound."),
    },
    {
        "date": "2026-08-08",
        "tag": "STOPS",
        "verdict": "rejected",
        "title": "A break-even stop would not have helped",
        "body": ("The median losing trade was +1.44R in profit before it reversed, and "
                 "59% touched a full +1R first. That looks like an obvious fix: move "
                 "the stop to entry once price pays 1R. Re-walking every trade bar by "
                 "bar, with winners subject to the same rule, says otherwise."),
        "evidence": [("baseline", "+0.194R", "", ""),
                     ("break-even @1R", "+0.166R", "", "worse")],
        "note": ("The first version of this test only altered losers, which guarantees "
                 "a positive answer. Correcting it reversed the conclusion. Nothing "
                 "tested cleared one standard error, so no stop rule was adopted."),
    },
    {
        "date": "2026-08-08",
        "tag": "INTEGRITY",
        "verdict": "adopted",
        "title": "Trades were being closed at prices that never traded",
        "body": ("The grader had never resolved a single trade — an exception was being "
                 "swallowed silently — leaving a fallback path that could book an exit "
                 "at a bar from before the signal existed. Every closed trade was "
                 "re-graded against bars that actually printed."),
        "evidence": [("reopened, never truly closed", "57", "", ""),
                     ("exits corrected to a real price", "22", "", ""),
                     ("published expectancy", "+0.090R → −0.182R", "", "")],
        "note": ("This moved the headline number against me. It is published anyway, "
                 "because the earlier number was not real. Pre-correction data is kept "
                 "in the repo rather than deleted."),
    },
    {
        "date": "2026-08-07",
        "tag": "TARGETS",
        "verdict": "adopted",
        "title": "A first target must pay back the risk",
        "body": ("T1 was whichever structural level sat nearest the entry, with no "
                 "distance test. One signal shipped a T1 worth 0.19R against its own "
                 "stop, printed beside an R:R of 2.41 quoted off T2. T1 must now "
                 "return at least 1R."),
        "evidence": [("signals carrying a sub-1R T1", "26", "", "")],
        "note": "",
    },
]

# The fourth column is the nav GROUP. Eleven equally-weighted numbered links
# told a first-time reader nothing about what this page is for — Fund Screen
# and Signal Log looked like the same kind of thing, and the answer to "what do
# I use this site for?" was "read all eleven and decide". The group is a label
# only: document order is still the single sequence everything derives from,
# because nav order MUST match document order (see page_context). Consecutive
# rows sharing a group render under one heading; a group that appears twice in
# the sequence simply gets its heading twice, which is the honest rendering of
# a page whose order is fixed.
# ── EVERY NUMBER ON THE PAGE, DEFINED ONCE ──────────────────────────────────
#
# The brief was "legend badges on every metric". Stamping a coloured pill next
# to seventy figures would decorate the page without telling anyone what the
# figure MEANS, so the badge and the definition are the same object: one row
# here produces the pill wherever the metric appears AND its entry in the
# How to Read This section.
#
# `tier` is provenance, not importance, and uses the four words the legend at
# the top of the page already teaches:
#
#   fact    an observed value — a close, a flow, a filing
#   model   computed by the engine from facts
#   result  what happened to a published signal
#   view    a human opinion, labelled as one
#
# `label` must match the on-page label EXACTLY: app.js matches on it to attach
# the pill, and a near-miss silently attaches nothing.
METRICS = [
    # ── The ledger's own scoreboard. These are RESULTS: they exist only
    #    because a signal was published and then lived or died.
    {"key": "winrate", "label": "Win rate", "tier": "result",
     "what": "Closed signals that ended in profit, over all closed signals.",
     "how": "Expiries count as losses. A setup that never triggered and timed "
            "out is not a neutral event — the capital was committed and the "
            "idea did not work. Excluding them is how a 24% win rate reads as "
            "31%."},
    {"key": "expectancy", "label": "Expectancy", "tier": "result",
     "what": "Average R made per closed signal.",
     "how": "R is recomputed from the exit price against the entry and stop of "
            "that same signal, never read from the stored r_multiple column — "
            "a 2026-08-08 re-grade corrupted that column on 168 of 573 rows."},
    {"key": "rmultiple", "label": "R", "tier": "result",
     "what": "One R is the distance from entry to stop: the money at risk.",
     "how": "A trade exited at +2R made twice what it was risking. Quoting "
            "returns in R rather than rupees is what makes a 500-rupee stop "
            "and a 50,000-rupee stop comparable."},
    {"key": "drawdown", "label": "Max drawdown", "tier": "result",
     "what": "The deepest peak-to-trough fall of the running R curve.",
     "how": "Measured on closed signals in sequence. It is the number that "
            "decides whether a strategy is survivable, not the average."},

    # ── The allocator. MODEL: derived from the rules, not observed.
    {"key": "deployed", "label": "Deployed", "tier": "model",
     "what": "Capital currently sitting in open paper positions.",
     "how": "Deployed plus Cash is always the full wallet. Tier caps add to "
            "more than 100% deliberately; the binding limit is the global cap."},
    {"key": "cash", "label": "Cash", "tier": "model",
     "what": "The wallet minus everything deployed.",
     "how": "Uncommitted money waiting on a signal that clears its tier's "
            "rules. A high cash figure is the rules declining, not an error."},
    {"key": "heat", "label": "Heat", "tier": "model",
     "what": "Total capital at risk if every open stop is hit at once.",
     "how": "Sum of (entry − stop) × quantity across open positions, as a "
            "percentage of the wallet. This, not deployed capital, is the "
            "number that describes a bad day."},
    {"key": "rr", "label": "R:R", "tier": "model",
     "what": "Reward divided by risk on an unfilled idea.",
     "how": "Target distance over stop distance. Every published idea clears "
            "a 2:1 floor; the target comes from structure — a 52-week high or "
            "a measured move — never from a fixed percentage."},
    {"key": "allocated", "label": "Allocated", "tier": "model",
     "what": "What the sizing rules gave this position.",
     "how": "Tier percentage of the wallet, scaled down for grade B and C "
            "signals, then clipped by whichever cap binds first."},

    # ── The screen. FACTS off the tape, and one model on top.
    {"key": "volspike", "label": "Volume", "tier": "fact",
     "what": "The day's volume as a multiple of the name's own average.",
     "how": "2x means twice the usual number of shares changed hands. It "
            "carries no direction on its own, which is why the week's move is "
            "always printed beside it."},
    {"key": "turnover", "label": "Turnover", "tier": "fact",
     "what": "Rupees traded in the name per day.",
     "how": "The liquidity measure this build actually has. NSE's delivery "
            "percentage lives in the bhavcopy, which nothing here reads, so "
            "delivery is never filtered on and never implied."},
    {"key": "rsi", "label": "RSI", "tier": "model",
     "what": "14-period relative strength index, on DAILY bars.",
     "how": "The period and the bar were both missing from the column, which "
            "is the whole question a reader has when they see 68 — is that a "
            "day or a week? It is 14 daily closes (yfinance interval=1d, "
            "stock_screen.rsi(c, 14)). Above 70 is stretched, below 30 is "
            "washed out. Used here only as an exclusion: an idea already "
            "vertical is not published."},
    {"key": "roce", "label": "ROCE", "tier": "fact",
     "what": "Return on capital employed, from the filed statements.",
     "how": "Blank rather than zero where statements are not in the feed. "
            "'Not measured' and 'measured at zero' are different facts."},
    {"key": "fromhigh", "label": "From 52w high", "tier": "fact",
     "what": "Distance below the highest close of the last year.",
     "how": "0.0% means the name is at its own high today."},
    {"key": "median", "label": "median", "tier": "model",
     "what": "The middle name's move in a sector, not the average.",
     "how": "It separates a sector that moved from a sector where two names "
            "carried the label. The average cannot do that."},

    # ── The ledger's counts, and what is open right now.
    {"key": "totalsignals", "label": "Total Signals", "tier": "fact",
     "what": "Every signal ever published to the ledger.",
     "how": "Open and closed together. Nothing is ever deleted from this "
            "count — a signal that went wrong stays in it."},
    {"key": "targets", "label": "Targets Hit", "tier": "result",
     "what": "Closed signals that reached their published target.",
     "how": "Counted at the target actually printed at publication, never at "
            "one moved afterwards."},
    {"key": "stops", "label": "Stops Hit", "tier": "result",
     "what": "Closed signals that reached their published stop.",
     "how": "A stop-out is a -1R outcome by definition; that is what makes R "
            "comparable across positions of different sizes."},
    {"key": "openrisk", "label": "Open risk", "tier": "model",
     "what": "Money that would be lost if every open stop hit today.",
     "how": "The forward-looking twin of realised P&L. Kept as a separate tile "
            "because banked money and money still on the table are different "
            "facts, and one blended figure hides which is which."},
    {"key": "unrealised", "label": "Unrealised", "tier": "model",
     "what": "Open positions marked at the latest price on the ticker rail.",
     "how": "The tile says how many of the open rows could be marked. An "
            "unmarked row is one with no live quote, not one worth zero."},
    {"key": "realized", "label": "Realized P&L", "tier": "result",
     "what": "Money actually banked by closed paper positions.",
     "how": "Winners book on a ladder — half at the first target, the rest at "
            "the second — so a row that reached the far target is banked at "
            "the blend, not as though the whole position ran to it."},
    {"key": "tradessized", "label": "Trades sized", "tier": "model",
     "what": "Signals the allocator gave a position size to.",
     "how": "Fewer than the ledger publishes: engines outside the mandate are "
            "logged but never sized, and the section names which ones."},
    {"key": "openpos", "label": "Open Setups", "tier": "fact",
     "what": "Published signals that have neither hit a target nor a stop.",
     "how": "An open setup is not a position. Nothing here has been bought — "
            "this ledger cannot place a trade, and says so."},
    {"key": "advancing", "label": "Markets Advancing", "tier": "fact",
     "what": "How many of the tracked indices closed up.",
     "how": "Counted across the index list on the ticker rail, not across "
            "stocks. It is a breadth reading of markets, not of the tape."},
    {"key": "fii", "label": "FII net", "tier": "fact",
     "what": "Net rupees bought or sold by foreign institutions.",
     "how": "NSE publishes it once, after the close, so it is never live. FII "
            "and DII are usually on opposite sides; the size of the gap says "
            "more than the direction of either."},
    {"key": "dii", "label": "DII net", "tier": "fact",
     "what": "Net rupees bought or sold by domestic institutions.",
     "how": "Same source and the same once-a-day cadence as the FII figure."},
    {"key": "subscribed", "label": "Subscribed", "tier": "fact",
     "what": "Times an open IPO book has been covered.",
     "how": "Straight from NSE's public issue endpoint. Grey-market premium is "
            "deliberately absent everywhere on this page: it is an unofficial "
            "quote with no audit trail."},

    # ── IPO Radar's counts, and the one verdict among them.
    {"key": "ipoopen", "label": "Open now", "tier": "fact",
     "what": "Mainboard issues whose book is open for applications today.",
     "how": "Mainboard is NSE's own series == EQ. SME issues are a different "
            "instrument with different lot sizes and are not counted here."},
    {"key": "ipoSoon", "label": "Opening soon", "tier": "fact",
     "what": "Issues with a published open date still ahead.",
     "how": "Dates move. A book that slips is re-read on the next build "
            "rather than carried forward from the announcement."},
    {"key": "ipoverdict", "label": "Apply / Apply-small", "tier": "model",
     "what": "How many open books the scorer rates worth applying to.",
     "how": "A reading of public DEMAND and nothing else. NSE's feeds carry "
            "no lot size, sector, financials or valuation, so none of those "
            "inform the score and none are guessed at. Grey-market premium is "
            "deliberately excluded: an unofficial quote with no audit trail."},
    {"key": "ipoavoid", "label": "Avoid", "tier": "model",
     "what": "Open books the same scorer rates against applying to.",
     "how": "Printed beside the Apply count on purpose. A scorer that only "
            "publishes its positives is a marketing sheet."},
    {"key": "ipoawait", "label": "Awaiting listing", "tier": "fact",
     "what": "Closed books that have not yet produced a traded price.",
     "how": "These carry an ISSUE date, not a listing date, and every "
            "performance cell is blank rather than zero. Mixing them with "
            "names that actually listed is what made this table read as "
            "broken."},
    {"key": "ipomeasured", "label": "Listed & measured · 12m", "tier": "result",
     "what": "Last year's listings whose post-listing return could be priced.",
     "how": "A listing with no reachable price is excluded from the return "
            "rather than counted flat — a delisted or unquoted symbol is "
            "missing data, not a 0% outcome."},

    # ── The savings calculators. MODEL: projections, not observations.
    {"key": "corpusret", "label": "Corpus at retirement", "tier": "model",
     "what": "What the plan is projected to be worth on the retirement date.",
     "how": "Compounded from the stated contribution and return assumption. "
            "It is arithmetic on an assumption, not a forecast of markets."},
    {"key": "corpusreq", "label": "Corpus required", "tier": "model",
     "what": "What the stated withdrawal actually needs to be funded.",
     "how": "Printed next to the projected corpus so the gap is visible. The "
            "gap, not either figure alone, is the number that decides "
            "anything."},
    {"key": "firstdraw", "label": "First withdrawal / month", "tier": "model",
     "what": "The opening monthly withdrawal the plan supports.",
     "how": "It rises with inflation across the plan; the first year is the "
            "smallest one, which is the honest number to quote."},
    {"key": "lastuntil", "label": "Money lasts till", "tier": "model",
     "what": "The year the plan runs out under its own assumptions.",
     "how": "Sensitive to the return assumption more than to anything else. "
            "Treat a year past the life expectancy as 'does not run out', "
            "not as precision."},
    {"key": "invested", "label": "Invested", "tier": "fact",
     "what": "Money actually put in, before any return.",
     "how": "The cost base. Paired with Value so the return is the "
            "difference rather than a separately computed figure that could "
            "disagree with it."},

    # ── Freshness. FACT about the build, not about the market.
    {"key": "asof", "label": "As of", "tier": "fact",
     "what": "When the underlying data was read, not when the page was built.",
     "how": "A weekly screen rebuilt on Sunday still shows Sunday's date on "
            "Thursday. The badge says which, because a stale RSI and a stale "
            "ROCE go wrong at very different speeds."},
]

# Keyed for the template, so a section can pull one definition without walking
# the list. Duplicate keys would silently shadow, so it is asserted.
METRICS_BY_KEY = {m["key"]: m for m in METRICS}
assert len(METRICS_BY_KEY) == len(METRICS), "duplicate metric key"

# ── THE SIX FRESHNESS TIERS ─────────────────────────────────────────────────
#
# Every dataset on the page carries one of these badges. The definitions used
# to sit in a 660-character paragraph at the foot of Data Health, which is the
# one place a reader is not looking when they hit a STALE badge 4,000 pixels
# higher up. They live here now, render in How to Read This next to everything
# else that needed defining, and Data Health links to them.
FRESHNESS = [
    ("LIVE",        "Built within a quarter of its refresh interval."),
    ("FRESH",       "Built within its refresh interval."),
    ("STALE",       "Older than its refresh interval, and still valid — a "
                    "weekly screen on a Thursday is stale by design, not broken."),
    ("DEGRADED",    "Valid data behind a known problem: a newer attempt failed, "
                    "coverage is thin, or the vintage cannot be read."),
    ("FAILED",      "The build broke and there is nothing valid to fall back on."),
    ("UNAVAILABLE", "Never published."),
]

# ── ONE DEFINITION OF "CLOSED" ──────────────────────────────────────────────
#
# This number was computed in THREE places and two of them were wrong.
#
#   generate.py:860   counted expiries as losses — correct, and shadowed
#   newspaper.py      a {% set %} in the template recomputed it WITHOUT
#                     expiries, and the template is what renders, so the
#                     correct figure never reached the page
#   generate.py:1124  the social card, also without expiries
#
# That is the "win rates different, fix which is genuine" complaint: the hero
# printed 24% over 55 closed while /api/stats printed 20.6% and the underlying
# feed supports 20.0% over 65. Fixing the generator alone did nothing, because
# the template's own {% set %} silently outranked it.
#
# An expired signal RESOLVED and did not reach its target. Calling it "not
# closed" removes it from the denominator and raises the win rate without a
# single trade going differently, which is the one arithmetic every published
# track record is tempted by. It counts as a loss.
# ── HOW TALL EACH SECTION IS, ROUGHLY ───────────────────────────────────────
#
# The fallback height content-visibility:auto uses for a section it has not
# rendered yet. Only the FIRST estimate matters: contain-intrinsic-size carries
# the `auto` keyword, so once the browser has laid a section out once it
# remembers the real height and stops using this number.
#
# One global guess was tried first and is why this table exists. At a flat
# 1200px the document reported 31,636px tall against a real 54,308px — a 42%
# error, which on a page this long means the scrollbar thumb visibly resizes
# under the reader's thumb as they scroll. Measured per section instead, the
# spread is 396px to 7,707px: nineteen to one. No single number can serve that.
#
# Measured on a 1280px desktop viewport. Phones run taller, so these
# under-estimate there — but under-estimating by a factor of two beats
# under-estimating one section by a factor of six, and `auto` corrects both on
# first render. A section that grows later is likewise only wrong until it has
# been scrolled past once.
SECTION_INTRINSIC = {
    "marketintel": 3700, "picks": 2900, "world": 2600, "findings": 2800,
    "volspikes": 1800, "longterm": 1400, "stocks": 3100, "iporadar": 7000,
    "funds": 7700, "sip": 1200, "swp": 400, "tracker": 800,
    "paperwallet": 2100, "alerts": 1750, "perf": 1900, "rules": 800,
    "datahealth": 1800, "buildlog": 500, "method": 5700, "who": 600,
}
DEFAULT_INTRINSIC = 1900          # the median, for any section not measured


LOSS_BADGES = ("loss", "expired")


def ledger_counts(alerts: list) -> dict:
    """Wins, losses, opens and the win rate over a list of alert rows.

    The single source. Called by the template, by the generator's hero
    numbers and by the social card, so the three cannot drift apart again.

    Rounding is half-up via +0.5 rather than Python's round(), which is
    half-to-even: 24.5% has to print the same on the page and on the card.
    """
    alerts = alerts or []
    wins = sum(1 for a in alerts if a.get("badge") == "win")
    losses = sum(1 for a in alerts if a.get("badge") in LOSS_BADGES)
    opens = sum(1 for a in alerts if a.get("badge") == "open")
    closed = wins + losses
    return {
        "wins": wins, "losses": losses, "opens": opens, "closed": closed,
        "winrate": int(wins / closed * 100 + 0.5) if closed else 0,
    }


def inr(n) -> str:
    """Indian digit grouping. 10000000 -> '1,00,00,000'.

    Python's own separator groups in threes all the way up, so the mandate
    printed as "10,000,000" — a number the reader has to stop and count the
    digits of to tell a crore from a million. Last three digits group in three,
    everything above them in twos, which is what every Indian statement, broker
    note and bank app does.
    """
    try:
        n = int(round(float(n)))
    except (TypeError, ValueError):
        return "—"
    sign, n = ("-" if n < 0 else ""), abs(n)
    t = str(n)
    if len(t) <= 3:
        return sign + t
    head, tail = t[:-3], t[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:]); head = head[:-2]
    if head:
        parts.insert(0, head)
    return sign + ",".join(parts) + "," + tail


def inr_short(n, *, unit_only: bool = False) -> str:
    """Large rupee amounts in the units Indians actually say them in.

    "₹1,00,00,000" is correctly grouped and still unreadable as a headline: the
    reader counts digit groups to work out whether it is a crore or ten lakh,
    which is exactly the work a headline number exists to save. Every Indian
    broker statement, news ticker and bank app writes the same figure "₹1 Cr".

    Bands, and why they stop where they do:
      >= 1 crore   ->  "1 Cr", "1.35 Cr"
      >= 1 lakh    ->  "28.3 L"
      below that   ->  full grouped digits, because "0.67 L" is worse than
                       "67,262" for a number a reader may want exactly.

    Trailing zeros are trimmed, so a round crore reads "1 Cr" and not "1.00 Cr".

    NOT for prices, stops or targets. A stop at "₹1,759.39" is an instruction
    with a paise in it and rounding it to "1.76 K" would make it wrong. This is
    for capital, cash, turnover and issue sizes — figures whose magnitude is
    the message.
    """
    try:
        v = float(n)
    except (TypeError, ValueError):
        return "\u2014"
    sign, v = ("-" if v < 0 else ""), abs(v)
    if v >= 1e7:
        num, unit = v / 1e7, "Cr"
    elif v >= 1e5:
        num, unit = v / 1e5, "L"
    else:
        return sign + inr(v)
    txt = f"{num:.2f}".rstrip("0").rstrip(".")
    return unit if unit_only else f"{sign}{txt} {unit}"


PROV_TIERS = {
    "fact":   ("Fact",   "an observed value — a close, a flow, a filing"),
    "model":  ("Model",  "computed by the engine from facts"),
    "result": ("Result", "what happened to a published signal"),
    "view":   ("View",   "a human opinion, labelled as one"),
}


SECTION_MAP = [
    # (id,          nav label,      page,   nav group)
    #
    # FOUR PILLARS. The order of this list IS document order — page_context
    # builds the nav from it and test_page_structure.py fails the build if the
    # template drifts out of step.
    #
    #   SIGNAL    what is happening now
    #   RESEARCH  what is worth understanding
    #   DESK      what you operate and own
    #   LIFE      what you are improving      (the whole /desk page)
    #
    # Every group must be a CONTIGUOUS run — a group that stops and restarts
    # prints its heading twice and stops being navigation.
    #
    # Nothing was deleted to build this. 32 sections before, 32 after; the
    # mapping is proved section by section in AUDIT/SECTION-MIGRATION.md, and
    # v1.0-pre-4pillar is the restore point.

    # ── SIGNAL ──────────────────────────────────────────────────────────────
    # What moved the tape, and what the engine wants to do about it.
    # Market Intel sits here rather than in Research: FII/DII, sector heat and
    # corporate actions are the day's market state, not a research artefact.
    ("marketintel", "Market Intel", "main", "Today"),
    ("picks",       "Trade Ideas",  "main", "Today"),

    # ── RESEARCH ────────────────────────────────────────────────────────────
    # Context first, then what is unusual in it, then the instruments.
    # Findings sits second because it answers the question the ranked tables
    # underneath it cannot: what here would I never have scrolled to?
    ("world",       "World",        "main", "Markets"),
    ("findings",    "Findings",     "main", "Markets"),
    # 2026-08-26. Price says where a name went; volume says whether anybody
    # came with it. The screen has carried a real volume ratio all along and
    # nothing on the page read it — a comment in generate.py wrongly claimed
    # the field was constant, and that comment stopped anyone looking.
    ("volspikes",   "Volume",       "main", "Markets"),
    # Renamed from "Long-Term". The section's own headline has read "Own the
    # business." for some time while the nav still said "Long-Term" — the same
    # nav/heading disagreement that put "07 Performance" over "17 / EDGE".
    # seclabel reads from here, so the eyebrow follows automatically.
    ("longterm",    "Own the Business", "main", "Research"),
    ("stocks",      "Stock Screen", "main", "Research"),
    # IPO Radar is a DECISION artefact: what is open now and whether to apply.
    # New Listings below it is a PERFORMANCE artefact: how names that already
    # listed have traded since. Different question, different source, different
    # failure mode — so they are two sections, and Radar comes first because a
    # book that closes on Monday cannot wait behind a history table.
    ("iporadar",    "IPO Radar",    "main", "Research"),
    # New Listings retired 2026-08-22. IPO Radar's "Listed in the last 12
    # months" is the same population with the same measured returns, plus the
    # unmeasured listings it used to omit — two sections answering one question
    # with two different counts was the duplication being complained about.
    # ipo_tracker.py still runs: the Radar consumes its rows.

    ("funds",       "Fund Screen",  "main", "Research"),
    ("sip",         "SIP Buckets",  "main", "Research"),
    ("swp",         "SWP",          "main", "Research"),

    # ── DESK ────────────────────────────────────────────────────────────────
    # What is held, what it was sized to, what happened, what that adds up to,
    # and — last — whether any of it can be trusted today.
    ("tracker",     "Portfolio",    "main", "Portfolio"),
    ("paperwallet", "Paper Wallet", "main", "Portfolio"),
    ("alerts",      "Signal Log",   "main", "Ledger"),
    ("perf",        "Performance",  "main", "Ledger"),
    # Engine Log and Data Health are the System Health pair the brief asks for.
    # Kept as two sections rather than merged into one: they answer different
    # questions (what the engine CHANGED vs whether today's data is CURRENT),
    # and collapsing them would bury the second inside the first.
    ("rules",       "Engine Log",   "main", "Ledger"),
    ("datahealth",  "Data Health",  "main", "Ledger"),
    # 2026-08-26. The Engine Log records what the LEDGER forced the rules to
    # change; this records what the SITE shipped. Same question, two subjects,
    # so they sit together. Generated from git history rather than hand-kept.
    ("buildlog",    "Build Log",    "main", "Ledger"),
    # 2026-08-26. One place that defines every number on the page and says
    # where it comes from. The definitions were previously spread across
    # seventeen section ledes, which meant a reader who wanted to check what
    # "expectancy" meant had to remember which section had explained it. The
    # ledes stay — an explanation is most useful next to the thing it explains
    # — but this is the canonical copy, and the legend links to it.
    ("method",      "How to Read This", "main", "About"),
    ("who",         "Who",          "main", "About"),

    # ── LIFE — the whole /desk page ─────────────────────────────────────────
    # The page is the pillar; these groups are its secondary navigation.

    # CAREER — find the role, then close the gap to it.
    ("careers",     "Finance Careers", "desk", "Career"),
    ("interview",   "CFO Track",    "desk", "Career"),

    # LEARNING — the day's wire, the longer reading, the book, the listening.
    ("brief",       "Daily Brief",  "desk", "Learning"),
    ("smartreads",  "Smart Reads",  "desk", "Learning"),
    ("book",        "Book",         "desk", "Learning"),
    ("podcasts",    "Podcasts",     "desk", "Learning"),

    # PRACTICE — the daily reps.
    ("language",    "Language",     "desk", "Practice"),
    ("father",      "Father",       "desk", "Practice"),

    # MIND — how to think, and what thinking has already been done.
    ("wisdom",      "Wisdom",       "desk", "Mind"),
    ("mind",        "The Mind",     "desk", "Mind"),
    ("way",         "The Way",      "desk", "Mind"),
    ("review",      "The Review",   "desk", "Mind"),
    ("desk",        "The Desk",     "desk", "Mind"),

    # DRILLS — the things scored against a board.
    ("chess",       "Chess",        "desk", "Drills"),
    ("gym",         "Mind Gym",     "desk", "Drills"),
]

PAGE_META = {
    "main": {
        "title": "The Daily Signal — live NSE trading ledger, scored",
        "desc": ("A public, auditable NSE trading ledger. Every signal logged when it "
                 "fires and scored when it closes — wins and losses both. Live markets, "
                 "long-term conviction picks and the last 24 hours of world news, "
                 "rebuilt at 6 AM MYT daily by Akshay Kothari, CA."),
        "path": "/",
        # The other page IS the fourth pillar, so the link says so. "The Desk"
        # was ambiguous once DESK became a pillar name on this page.
        "other_label": "Life",
        "other_path": "/desk",
        "other_hint": "career, learning, practice, mind, drills",
    },
    "desk": {
        "title": "Life — career, learning, practice, mind and the daily drills",
        "desc": ("The fourth pillar: the Gulf finance search and the CFO track, the "
                 "day's reading and the book underneath it, Spanish, one thing to do "
                 "with a seven-month-old, the thinking, and chess from yesterday's "
                 "games. Rebuilt daily alongside the ledger."),
        "path": "/desk",
        "other_label": "Signal · Research · Desk",
        # Absolute, not "/". This page is served from TWO origins now — as
        # /desk on news.askakshay.com and as the index of life.askakshay.com —
        # and a relative "/" resolves to the Life site's own front page there,
        # so the link back to the ledger pointed at itself. The absolute URL is
        # correct from both origins.
        #
        # The main page's link to Life stays relative at /desk until
        # life.askakshay.com has DNS; flipping it before then would publish a
        # dead link on the busier of the two sites.
        "other_path": "https://news.askakshay.com",
        "other_hint": "markets, findings, the ledger and what it is worth",
    },
}


def empty_sections(fund_screen=None, podcasts=None, smart_reads=None,
                   stock_screen=None, market_intel=None, careers=None,
                   brief=None, health=None, ipos=None, findings=None,
                   iporadar=None, volspikes=None, buildlog=None,
                   evidence=None, book=None) -> set:
    """Sections that must not be advertised in the nav on this build.

    A helper rather than an inline check so the decision has one home. Only
    generate.py consumes it today — the Flask routes never pass `secs` at all,
    so they already render without sections — but the published page is built
    here, and this is where the nav and the document have to agree.
    """
    drop = set()
    # Radar earns its nav slot only when there is a live book or one coming.
    # Between windows there is genuinely nothing to decide, and a permanently
    # empty "IPO Radar" in the nav teaches the reader to skip it.
    if not ((iporadar or {}).get("open") or (iporadar or {}).get("upcoming")):
        drop.add("iporadar")
    if not (fund_screen or {}).get("categories"):
        drop.add("funds")
    if not (podcasts or {}).get("episodes"):
        drop.add("podcasts")
    if not smart_reads:
        drop.add("smartreads")
    # Same contract as #funds: the weekly screen has its own clock, so a build
    # that runs before the first screen has an empty cache and the section must
    # not be advertised. Named here rather than left to the template guard,
    # which is the mistake #funds already made once.
    if not (stock_screen or {}).get("rows"):
        drop.add("stocks")
    # All three sub-blocks come from independent NSE/Yahoo fetches inside
    # the same daily build — any one can legitimately be empty on its own
    # (e.g. NSE hasn't published today's FII/DII yet), so only hide the
    # whole section when there is truly nothing across all three.
    mi = market_intel or {}
    if not (mi.get("corporate_actions") or mi.get("market_heat") or mi.get("fii_dii")):
        drop.add("marketintel")
    # Careers renders from docs/jobs.json, written by its own workflow on its
    # own clock. Same contract as #funds and #stocks: a build that runs before
    # the first scrape has nothing, and the nav must not advertise it. Counted
    # on the RENDERABLE rows, not the raw file — a file of 86 rows that are all
    # excluded is an empty section, and the nav would otherwise point at a
    # heading with nothing under it.
    if not (careers or {}).get("visible"):
        drop.add("careers")
    # A briefing with no events is not a briefing. Same contract as the rest:
    # named here rather than left to a template guard, so the nav can never
    # advertise a section the document does not contain.
    if not (brief or {}).get("events"):
        drop.add("brief")
    # #datahealth renders from the health snapshot and nothing else. The Flask
    # routes render this template without one, and a nav link to a section the
    # document does not contain is the exact failure this helper exists to
    # prevent — the same one #funds caused when its extra render condition was
    # left to a template guard.
    if not (health or {}).get("datasets"):
        drop.add("datahealth")
    if not (ipos or {}).get("rows"):
        drop.add("ipos")
    # A findings section with nothing found is worse than none: it teaches the
    # reader that the section is usually empty and to stop opening it.
    if not ((findings or {}).get("hidden") or (findings or {}).get("contradictions")):
        drop.add("findings")
    # Added 2026-08-27, after both broke the build. Volume and Build Log were
    # added with `{% if 'x' in secs and x %}` guards in the template and never
    # registered here — so on a build where either was empty the DOM dropped
    # the section while the nav still advertised it, and the pre-publish check
    # failed with "nav order does not match document order". Locally both were
    # always populated, which is why it only ever failed on CI.
    #
    # A template data-guard without a matching entry here is the same bug every
    # time. test_page_structure now fails the build when one is missing.
    if not volspikes:
        drop.add("volspikes")
    if not buildlog:
        drop.add("buildlog")
    # Found by the test above the moment it was written, and neither of these
    # is new — both have been latent since long before this session, waiting
    # for a build where their data happened to be empty. `perf` needs the
    # per-engine evidence table and `book` needs the current book; without
    # either the section vanishes from the DOM while the nav keeps its link.
    if not ((evidence or {}).get("engines") if isinstance(evidence, dict) else evidence):
        drop.add("perf")
    if not book:
        drop.add("book")
    return drop


def page_context(page: str, drop=()) -> dict:
    """Sections and nav for one page, in document order.

    `drop` removes sections that have no data to show. It exists because the
    nav was built unconditionally from SECTION_MAP while at least one section
    carried an EXTRA render condition of its own — #funds only appears when the
    weekly fund cache is populated. On a build with an empty cache the nav
    advertised eleven sections and the document contained ten, which is both a
    dead nav link for a reader and a hard failure of the pre-publish gate.

    Dropping here keeps nav, section numbering and the section guard reading
    from one list, in the same spirit as secnum/seclabel below. A section with
    an extra condition must be named in `drop` when that condition is false,
    never left to disappear underneath the nav.
    """
    rows = [(i, lbl, grp) for i, lbl, pg, grp in SECTION_MAP
            if pg == page and i not in set(drop)]
    meta = PAGE_META[page]
    # `head` marks the first item of each run of one group, so the nav can
    # print the group name once above the run instead of repeating it per link.
    nav = []
    for n, (i, lbl, grp) in enumerate(rows, 1):
        nav.append({"id": i, "label": lbl, "n": f"{n:02d}", "group": grp,
                    "head": grp if (n == 1 or rows[n - 2][2] != grp) else ""})
    # Grouped nav. `nav` stays a flat list — the command palette, the scroll spy
    # and the tests all read it — and this is the same items keyed by group so
    # the header can render six destinations instead of seventeen links.
    navgroups = []
    for item in nav:
        if not navgroups or navgroups[-1]["name"] != item["group"]:
            navgroups.append({"name": item["group"], "links": []})
        navgroups[-1]["links"].append(item)

    return {
        "page": page,
        "secs": {i for i, _l, _g in rows},
        "nav": nav,
        "navgroups": navgroups,
        # Section headings read their number from here rather than carrying a
        # literal, so the nav and the heading cannot disagree — which they did,
        # with Performance showing "17 / EDGE" under a nav item numbered 07.
        # Number AND label both come from SECTION_MAP. Carrying the label as a
        # literal in the template is how "07 Performance" in the nav ended up
        # over "17 / EDGE" on the page — two sources of truth for one name.
        "secnum": {i: f"{n:02d}" for n, (i, _l, _g) in enumerate(rows, 1)},
        "seclabel": {i: l.upper() for i, l, _g in rows},
        # Which pillar of the paper a section belongs to. Read only by the
        # generated pillar stylesheet below the nav — the hue is derived from
        # SECTION_MAP like the number and the label are, so a section moved
        # between pillars changes colour without anyone editing CSS.
        "secgroup": {i: g for i, _l, g in rows},
        # Supplied here rather than at each render call site. There are two of
        # those in the Flask path alone plus the static generator, and the
        # error-path render is the one that would have silently dropped it —
        # which is precisely the day the log needs to still be on the page.
        "engine_changes": ENGINE_CHANGES,
        # The metric dictionary and the four provenance tiers. Supplied from
        # page_context for the same reason engine_changes is: three render call
        # sites, and the error path is the one that would silently drop it.
        "metrics": METRICS,
        "prov_tiers": PROV_TIERS,
        "freshness": FRESHNESS,
        "inr": inr,
        "inr_short": inr_short,
        "ledger_counts": ledger_counts,
        "section_intrinsic": SECTION_INTRINSIC,
        "default_intrinsic": DEFAULT_INTRINSIC,
        # The same list again, trimmed to what the badge stamper needs and
        # serialised here rather than in the template: json.dumps escapes for a
        # <script> context, and hand-building this in Jinja is how a stray
        # apostrophe in a metric label breaks JSON.parse for the whole page.
        # `what` and `how` travel with the badge now. They were held back to keep
        # the payload small, which meant the only way to read how a number was
        # computed was to follow the badge to the glossary at the foot of a
        # 45,000px page — losing your place to answer a question about the
        # number you were looking at. That is a jump, not a disclosure. The
        # whole dictionary is ~6KB and it buys the method at the point of use.
        "metrics_json": json.dumps(
            [{"key": m["key"], "label": m["label"], "tier": m["tier"],
              "what": m.get("what", ""), "how": m.get("how", "")}
             for m in METRICS], separators=(",", ":")),
        # Supplied here for the same reason engine_changes is: three render
        # call sites, and the provenance strip must not be the one that
        # silently renders blank on the error path.
        "picks_engine": PICKS_ENGINE,
        "page_title": meta["title"],
        "page_desc": meta["desc"],
        "page_path": meta["path"],
        "other_label": meta["other_label"],
        "other_path": meta["other_path"],
        "other_hint": meta["other_hint"],
    }

# ─────────────────────────────────────────────────────────────
# TOP 5 STOCK PICKS
# ─────────────────────────────────────────────────────────────

WATCHLIST = [
    # ── India NSE — Large Cap (Nifty 50 core) ────────────────────────────────
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS",
    "KOTAKBANK.NS", "HINDUNILVR.NS", "SBIN.NS", "BAJFINANCE.NS", "BHARTIARTL.NS",
    "WIPRO.NS", "HCLTECH.NS", "AXISBANK.NS", "MARUTI.NS", "ULTRACEMCO.NS",
    "TITAN.NS", "ASIANPAINT.NS", "LT.NS", "NESTLEIND.NS", "TECHM.NS",
    # ── India NSE — Mid/Small Cap momentum ───────────────────────────────────
    "ADANIENT.NS", "TATAMOTORS.NS", "SUNPHARMA.NS", "IRCTC.NS", "TATAPOWER.NS",
    "ZOMATO.NS", "DIXON.NS", "POWERGRID.NS", "PIDILITIND.NS", "DMART.NS",
    "PERSISTENT.NS", "LTIM.NS", "COFORGE.NS", "MPHASIS.NS", "KPITTECH.NS",
    "TATAELXSI.NS", "POLYCAB.NS", "ASTRAL.NS", "CAMS.NS", "ANGELONE.NS",
    "LALPATHLAB.NS", "NUVAMA.NS", "360ONE.NS", "BIKAJI.NS", "PAYTM.NS",
    "NYKAA.NS", "POLICYBZR.NS", "MAPMYINDIA.NS", "CAMPUS.NS", "KAYNES.NS",
    # ── India NSE — Infra / Energy / PSU ─────────────────────────────────────
    "NTPC.NS", "NHPC.NS", "SJVN.NS", "COALINDIA.NS", "BPCL.NS",
    "IOC.NS", "GAIL.NS", "ONGC.NS", "TORNTPOWER.NS", "CESC.NS",
    "ADANIGREEN.NS", "ADANIPORTS.NS", "ADANITRANS.NS", "PGCIL.NS", "RECLTD.NS",
    # ── US — Mega Cap (S&P top 30) ────────────────────────────────────────────
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO",
    "BRK-B", "JPM", "LLY", "V", "UNH", "XOM", "MA", "JNJ",
    "HD", "PG", "MRK", "ABBV", "COST", "WMT", "KO", "PEP",
    "BAC", "ORCL", "CRM", "ACN", "AMD", "NFLX",
    # ── US — High Growth / Tech ───────────────────────────────────────────────
    "CRWD", "SNOW", "DDOG", "NET", "MDB", "PANW", "ZS", "FTNT",
    "AXON", "CELH", "DUOL", "APP", "APLD", "HOOD", "COIN",
    "SMCI", "ARM", "TSM", "ASML", "NVO", "NOVO-B.CO",
    # ── Global ADRs & International ───────────────────────────────────────────
    # Europe
    "NESN.SW", "NOVN.SW", "ROG.SW",       # Switzerland
    "SAP", "SIEGY", "BAYRY",              # Germany
    "LVMUY", "IDEXY", "BNPQY",           # France
    "SHEL", "AZN", "ULVR.L",             # UK
    # Asia-Pacific
    "9988.HK", "0700.HK", "1810.HK",     # China (Alibaba, Tencent, Xiaomi)
    "005930.KS", "000660.KS",            # Korea (Samsung, SK Hynix)
    "7203.T", "6758.T", "9432.T",        # Japan (Toyota, Sony, NTT)
    "BABA", "JD", "PDD", "BIDU",         # China ADRs (US-listed)
    "SE", "GRAB", "GOTO.JK",             # SEA
    # Commodities / Energy / Mining
    "XOM", "CVX", "COP", "SLB", "EOG",
    "GOLD", "NEM", "FCX", "RIO", "BHP",
    # Global Finance / Banks
    "GS", "MS", "C", "WFC", "HSBA.L",
    "8306.T", "1288.HK",                 # Mitsubishi UFJ, AgBank HK
]

_picks_cache: dict = {}
_picks_lock = threading.Lock()

# Yahoo suffix → the currency that exchange actually quotes in. Every non-NSE
# name used to render with a "$", so a London quote came out as "$1523.40" when
# HSBA.L is quoted in pence, and a Copenhagen quote as "$295.15" when NOVO-B.CO
# is in kroner. The suffix is the only reliable signal here that costs nothing:
# reading Ticker.info["currency"] is right too, but it is one extra network
# round-trip per symbol across a 200-name universe.
_CURRENCY_BY_SUFFIX = {
    ".NS": "₹", ".BO": "₹",     # India
    ".L":  "p",                            # London quotes in PENCE, not pounds
    ".T":  "¥",                       # Tokyo
    ".HK": "HK$",
    ".CO": "kr", ".ST": "kr", ".OL": "kr", # Copenhagen / Stockholm / Oslo
    ".DE": "€", ".PA": "€", ".AS": "€", ".MC": "€",
    ".MI": "€",
    ".SW": "CHF", ".AX": "A$", ".TO": "C$",
    ".JK": "Rp", ".KS": "₩", ".SI": "S$", ".TW": "NT$",
    ".SS": "¥", ".SZ": "¥",
}


def pick_currency(sym: str) -> str:
    """Currency symbol for a Yahoo ticker. Bare tickers are US listings."""
    for suf, cur in _CURRENCY_BY_SUFFIX.items():
        if sym.endswith(suf):
            return cur
    return "$"


def score_stock(sym: str) -> Optional[dict]:
    try:
        # A YEAR, not three months. The targets below are derived from
        # structure — the 52-week high, the true range — and none of that
        # exists in a 3-month window. It also lets the 50-day average
        # converge: over 63 bars an ewm(span=50) is barely warmed up.
        hist     = yf.Ticker(sym).history(period="1y")
        if hist.empty or len(hist) < 20: return None
        # Yahoo intermittently returns rows with a null Close for non-US
        # listings — a holiday on that exchange, or a partial response when 200
        # symbols are pulled in a loop from a GitHub Actions runner. The frame
        # is neither empty nor short, so both guards above pass and the NaN
        # flows into every derived metric.
        #
        # Dropping them here is not cosmetic. _band() clamps with min/max, and
        # `nan < 1.0` is False, so a NaN input used to clamp to 1.0 — a PERFECT
        # component score. Five NaN metrics scored ~95/100 and carried the
        # symbol into the top 5, displacing a real idea and rendering "$nan".
        # A missing measurement has to score zero, never full marks.
        hist     = hist[hist["Close"].notna()]
        if len(hist) < 20: return None
        close    = hist["Close"]
        ema20    = close.ewm(span=20).mean().iloc[-1]
        ema50    = close.ewm(span=50).mean().iloc[-1]
        price    = close.iloc[-1]
        mom_1m   = (price - close.iloc[-22]) / close.iloc[-22] * 100 if len(close) >= 22 else 0
        # Indexed from the END, not from close.iloc[0]. With a 3-month window
        # those were the same bar; with a year they are not, and leaving it
        # would have silently turned every "3-month trend" score into a
        # 12-month one — a scoring change disguised as a data change.
        # ~63 trading days is three months.
        _b3      = close.iloc[-63] if len(close) >= 63 else close.iloc[0]
        mom_3m   = (price - _b3) / _b3 * 100
        vol_ratio = hist["Volume"].iloc[-5:].mean() / (hist["Volume"].iloc[-20:].mean() or 1)
        # Graded, not binary. The old version was six pass/fail buckets adding
        # to exactly 100, so in a strong tape every decent name cleared all six
        # and the "Top 5" was five stocks tied on 100/100 in arbitrary order —
        # a score with no power to rank is not a score.
        #
        # Each component now returns a fraction of its weight, so two stocks
        # that both sit above their averages are still separated by how far
        # above, by how much momentum, and by how much volume confirmed it.
        def _band(v, lo, hi):
            """0 at lo, 1 at hi, linear between. Clamped."""
            # NaN is checked before the clamp, and deliberately scores 0.
            # min()/max() do not propagate NaN — `nan < 1.0` is False, so
            # min(1.0, nan) returns 1.0 and a missing metric used to earn full
            # marks. That is how two unpriceable symbols reached the top 5.
            if v is None or not math.isfinite(v):
                return 0.0
            if hi == lo:
                return 1.0 if v >= hi else 0.0
            return max(0.0, min(1.0, (v - lo) / (hi - lo)))

        ext20 = (price - ema20) / ema20 * 100 if ema20 else 0      # % above 20EMA
        ext50 = (price - ema50) / ema50 * 100 if ema50 else 0
        sep   = (ema20 - ema50) / ema50 * 100 if ema50 else 0      # trend separation
        # Components are built as a list rather than summed inline so the card
        # can show WHERE a score came from. "93/100" with no breakdown is the
        # one black-box number this page publishes, and a reader has no way to
        # tell a 93 carried by momentum from a 93 carried by volume — which are
        # different trades. Same arithmetic as before; it is now inspectable.
        # (label, value, weight, band low, band high)
        COMPONENTS = [
            ("Above 20-day avg",  ext20,     25, -2,  8),
            ("Above 50-day avg",  ext50,     20, -2, 15),
            ("Averages stacked",  sep,       15,  0,  6),
            ("1-month thrust",    mom_1m,    20,  0, 15),
            ("3-month trend",     mom_3m,    10,  0, 35),
            ("Volume confirming", vol_ratio, 10, 0.9, 1.8),
        ]
        # Sum at full precision, round only what is shown. Summing the rounded
        # components instead would move some scores by a point against every
        # score already in the DB, for no reason other than display.
        earned = [(lbl, w * _band(v, lo, hi), w) for lbl, v, w, lo, hi in COMPONENTS]
        score = round(sum(e for _l, e, _w in earned))
        factors = [{"k": lbl, "w": w, "e": round(e, 1)} for lbl, e, w in earned]
        # Last gate before this dict reaches a template. Everything above is
        # arithmetic on floats, and one NaN slipping through renders "$nan" in
        # a price, a target and a stop — a card that looks like a trade idea
        # and carries no tradeable number. Drop the symbol instead.
        if not all(math.isfinite(float(v)) for v in
                   (price, ema20, ema50, mom_1m, mom_3m, vol_ratio, score)):
            log.warning(f"score_stock {sym}: non-finite metric, dropped from ranking")
            return None

        currency = pick_currency(sym)

        # ── Levels, from structure ──────────────────────────────────────────
        # These used to be `price * 1.25` and `price * 0.92`: a flat +25% and
        # −8% on every idea regardless of where the stock actually was. Two
        # names could sit a rupee below and a rupee above their 52-week high
        # and be given the same target. That is not analysis, it is a constant
        # wearing the costume of one — and the old stop contradicted the very
        # comment beside it, which already named the 20-day average as the
        # level that ends the idea.
        prev   = close.shift(1)
        tr     = pd.concat([hist["High"] - hist["Low"],
                            (hist["High"] - prev).abs(),
                            (hist["Low"]  - prev).abs()], axis=1).max(axis=1)
        atr    = float(tr.rolling(14).mean().iloc[-1])
        hi52   = float(hist["High"].max())

        if not math.isfinite(atr) or atr <= 0:
            return None

        # STOP FIRST, because the target has to be measured against the risk
        # and not the other way round. The 20-day average is the invalidation
        # — the same line the largest single score component measures, and the
        # one the factor table already names. Floored at 1.5x ATR: an average
        # sitting inside one day's noise is not a stop, it is a coin toss.
        if ema20 < price - atr:
            stop, stop_basis = float(ema20), "the 20-day average"
        else:
            stop, stop_basis = price - 1.5 * atr, "1.5x the 14-day range (the 20-day average is inside the noise)"
        if stop <= 0 or stop >= price:
            return None
        risk = price - stop

        # TARGET. A real level where supply actually appeared beats an invented
        # percentage — so the 52-week high is the first candidate.
        #
        # But a level is only a target if it is worth the risk. A momentum name
        # sitting just under its high gives a target 3% away against a stop 7%
        # away: R:R 0.48, which is not a trade. The old flat +25% hid this
        # completely — it printed 3.1 for every idea on the page, including
        # that one. So the high has to clear one unit of risk to be used, and
        # when it does not, the objective is the measured move THROUGH it:
        # the high plus the range the name would have to break to get there.
        if hi52 >= price + risk:
            target, target_basis = hi52, "the 52-week high"
        elif hi52 > price:
            target = hi52 + 2 * atr
            target_basis = "2x the 14-day range beyond the 52-week high (the high is too close to be worth the stop)"
        else:
            target = price + 2 * atr
            target_basis = "2x the 14-day range (already at its highs, no overhead level left)"

        # HORIZON. Extrapolated from the name's OWN three-month pace, and
        # labelled as exactly that. A stock that has covered 30% in three
        # months is moving ~10%/month; asking it for another 12% is roughly a
        # month's more work. When the three-month drift is flat or negative
        # there is no pace to extrapolate and no honest estimate — so it says
        # so rather than printing a number it cannot support.
        move_pct     = (target - price) / price * 100
        monthly_pace = mom_3m / 3.0
        if monthly_pace >= 1.0:
            months = move_pct / monthly_pace
            if   months <= 1.5: timeframe = "about a month"
            elif months <= 3:   timeframe = "1-3 months"
            elif months <= 6:   timeframe = "3-6 months"
            elif months <= 12:  timeframe = "6-12 months"
            else:               timeframe = "over a year at this pace"
            horizon_basis = f"{move_pct:.0f}% to go at its recent {monthly_pace:.1f}%/month"
        else:
            timeframe = "no estimate"
            horizon_basis = "three-month drift is flat or negative — no pace to extrapolate"

        rr = round((target - price) / risk, 2)
        return {"symbol": sym, "name": sym.replace(".NS","").replace(".BO",""),
                "ext20": round(ext20, 2), "vol_ratio": round(float(vol_ratio), 2),
                "price": round(price, 2), "change_1d": round((price - close.iloc[-2]) / close.iloc[-2] * 100, 2),
                "mom_1m": round(mom_1m, 1), "mom_3m": round(mom_3m, 1), "score": score,
                "target": round(target, 2), "stop_loss": round(stop, 2),
                # Every level now says where it came from. A reader who
                # disagrees with the target can see the level it was taken
                # from and argue with THAT, instead of with a constant.
                "target_basis": target_basis, "stop_basis": stop_basis,
                "horizon_basis": horizon_basis, "rr": rr,
                "atr": round(atr, 2), "high_52w": round(hi52, 2),
                "timeframe": timeframe, "currency": currency,
                # What the score is made of, and the level that ends the idea.
                # The 20-day average is the invalidation because it is the same
                # line the largest single component scores — an idea whose
                # biggest reason to exist has broken is not a smaller idea, it
                # is a different one.
                "factors": factors, "ema20": round(float(ema20), 2),
                "tv": tv_symbol(sym), "thesis": ""}
    except Exception as e:
        log.warning(f"score_stock {sym}: {e}")
        return None

# Non-NSE alert symbols → TradingView. The alert table used to hard-prefix
# every symbol with "NSE:", which is right for the equities that make up most of
# the ledger and wrong for every commodity, FX pair and crypto in it —
# "NSE:BRNUSD" is not a symbol, so the chart link opened an error page. Same
# root cause as Brent being priced in ₹.
#
# Only verified TradingView symbols go in here. Anything non-NSE that is NOT in
# this map renders as plain text with no link: no chart beats a broken chart.
TV_ALIASES = {
    # metals
    "GOLD": "TVC:GOLD", "XAUUSD": "OANDA:XAUUSD",
    "SILVER": "TVC:SILVER", "XAGUSD": "OANDA:XAGUSD",
    "COPPER": "COMEX:HG1!",
    # energy
    "CRUDE": "TVC:USOIL", "WTI": "TVC:USOIL", "WTIUSD": "TVC:USOIL",
    "BRENT": "TVC:UKOIL", "BRNUSD": "TVC:UKOIL",
    "NATGAS": "NYMEX:NG1!", "NGAS": "NYMEX:NG1!",
    # crypto
    "BTCUSD": "BINANCE:BTCUSDT", "ETHUSD": "BINANCE:ETHUSDT",
    "BNBUSD": "BINANCE:BNBUSDT", "SOLUSD": "BINANCE:SOLUSDT",
    "XRPUSD": "BINANCE:XRPUSDT", "DOGEUSD": "BINANCE:DOGEUSDT",
    # fx
    "USDJPY": "FX:USDJPY", "EURUSD": "FX:EURUSD", "GBPUSD": "FX:GBPUSD",
    "AUDUSD": "FX:AUDUSD", "NZDUSD": "FX:NZDUSD", "USDCHF": "FX:USDCHF",
    "USDCAD": "FX:USDCAD", "EURJPY": "FX:EURJPY", "GBPJPY": "FX:GBPJPY",
    "USDINR": "FX_IDC:USDINR", "MYRINR": "FX_IDC:MYRINR",
    "USDMYR": "FX_IDC:USDMYR",
    # index
    "DXY": "TVC:DXY", "NIFTY": "NSE:NIFTY", "BANKNIFTY": "NSE:BANKNIFTY",
}


def tv_alert_symbol(symbol: str) -> str:
    """Alert-ledger symbol → TradingView symbol, or "" when there is no chart."""
    s = (symbol or "").upper()
    if s in TV_ALIASES:
        return TV_ALIASES[s]
    if _price_unit(s) == "\u20b9":       # an NSE equity
        return f"NSE:{s}"
    return ""


# Yahoo suffix → TradingView exchange prefix. TradingView will happily resolve a
# bare US ticker, but "7203.T" is not a symbol it knows — a chart link that
# opens an error page is worse than no link, so map the suffix explicitly and
# leave anything unrecognised bare rather than guessing an exchange.
_TV_EXCHANGE = {
    ".NS": "NSE", ".BO": "BSE", ".T": "TSE", ".L": "LSE", ".HK": "HKEX",
    ".SS": "SSE", ".SZ": "SZSE", ".DE": "XETR", ".PA": "EURONEXT",
    ".AX": "ASX", ".TO": "TSX", ".SW": "SIX", ".KS": "KRX", ".SI": "SGX",
    ".TW": "TWSE", ".MI": "MIL", ".AS": "EURONEXT", ".MC": "BME",
}


def tv_symbol(yahoo: str) -> str:
    """Yahoo ticker → TradingView chart symbol."""
    for suf, ex in _TV_EXCHANGE.items():
        if yahoo.endswith(suf):
            return f"{ex}:{yahoo[: -len(suf)]}"
    return yahoo


def _build_picks() -> list[dict]:
    """Score all 60 stocks, return top 5 by momentum score among those that
    clear the site's own R:R floor.

    score_stock() already computes rr and even names the failure mode in its
    own comment ("R:R 0.48, which is not a trade") — but nothing acted on it.
    Ranking by momentum score alone let a stock with a great score and a
    terrible R:R (target too close to a wide, structure-based stop) outrank a
    stock with a slightly lower score and an actual tradeable setup. Same
    floor every other engine enforces (config.MIN_RR), so this page cannot
    publish a "trade idea" the scanners themselves would have rejected.

    Runs weekly — same week's picks stay consistent for journal tracking.
    """
    scored = []
    for sym in WATCHLIST:
        s = score_stock(sym)
        if s: scored.append(s)
        time.sleep(0.05)
    tradeable = [s for s in scored if s.get("rr") is not None and s["rr"] >= MIN_RR]
    tradeable.sort(key=lambda x: x["score"], reverse=True)
    top5 = tradeable[:5]
    for s in top5:
        s["thesis"] = ai_stock_thesis(s["name"], s["mom_1m"], s["mom_3m"], s["score"],
                                      s.get("ext20", 0.0), s.get("vol_ratio", 1.0))
        time.sleep(0.1)
    return top5

# Bump when score_stock() or ai_stock_thesis() changes shape. The cache key
# carries it, so a scoring change invalidates itself instead of serving last
# week's numbers computed by last week's code.
#
# Learned the hard way: the scorer was rewritten from six pass/fail buckets to
# graded components, and the site kept showing five stocks tied on 100/100 —
# correct new code, stale cached output, and nothing to tell them apart.
#   v2 — graded components, stock-specific thesis (2026-08-05)
#   v3 — (2026-08-05)
#   v4 — NaN metrics score 0 instead of clamping to a perfect 1.0, unpriceable
#        symbols are dropped, and non-US listings carry their real currency.
#        Without this bump the cached ranking keeps serving the two "$nan"
#        cards that the NaN-clamp bug promoted into the top 5. (2026-08-06)
# v5: score_stock now returns `factors` (the per-component breakdown behind the
# composite) and `ema20` (the invalidation level). The key is part of the cache
# key on purpose — without the bump the page would render this week's v4 rows,
# which have neither field, and the new card would silently show nothing.
# v6: levels come from STRUCTURE, not from a constant. Target is the 52-week
#     high (or a 2x-ATR measured move when the name is already at its highs);
#     the stop is the 20-day average, floored at 1.5x ATR; the horizon is
#     extrapolated from the name's own three-month pace instead of being the
#     string "2-3 months" on every card. Adds target_basis, stop_basis,
#     horizon_basis, rr, atr and high_52w. History widened 3mo -> 1y to make
#     any of it computable, so scores shift slightly too: a 50-day average
#     over 250 bars is converged where one over 63 bars was not. Without the
#     bump the page would serve v5 rows that carry none of the basis fields
#     and render blank "why this level" lines. (2026-08-20)
PICKS_ENGINE = "v6"


# A weekly screen is rebuilt every 7 days, so anything inside two cycles is
# still a fair answer to "which funds rank best". Past that it stops being
# stale and starts being wrong, and the section hides itself instead.
MAX_FUND_CACHE_AGE_DAYS = 15


def _payload_age_days(data: dict):
    """Age of a fund payload in days, or None if it cannot be established.

    None is deliberately NOT treated as fresh by the caller. An unreadable
    timestamp is exactly the case where serving the data anyway would publish
    fund rankings of unknown vintage under today's date.
    """
    ts = (data or {}).get("generated_at")
    if not ts:
        return None
    try:
        built = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if built.tzinfo is None:
            built = built.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - built).total_seconds() / 86400
    except ValueError:
        return None


def get_fund_screen(build_if_missing: bool = False) -> dict:
    """This week's SIP screen, cached. Empty dict when unavailable.

    Never blocks a page render: ~700 NAV downloads take minutes, so a cache
    miss returns nothing and the section hides itself rather than hanging the
    build behind a third-party API.
    """
    week = _fund_week_key()
    try:
        with _db() as con:
            row = con.execute("SELECT payload FROM newspaper_funds WHERE week=?",
                              (week,)).fetchone()
            if row:
                return _stamp_fund_payload(json.loads(row[0]), fallback=False)

            # Exact-key miss is the NORMAL state every Monday, and it used to
            # take the whole Fund Screen off the site until the weekly workflow
            # next ran. The ISO week rolls over and this screen is rebuilt
            # weekly, so Monday morning always misses.
            # (It used to miss for a second reason too — the key embedded
            # PICKS_ENGINE — which _fund_week_key() has now removed.)
            # Fund NAVs do not expire on a Monday, so fall back to the most
            # recent build and let the DATA decide whether it is still usable.
            row = con.execute("SELECT payload FROM newspaper_funds "
                              "ORDER BY week DESC LIMIT 1").fetchone()
            if row:
                data = json.loads(row[0])
                age = _payload_age_days(data)
                if age is not None and age <= MAX_FUND_CACHE_AGE_DAYS:
                    log.info(f"fund screen: serving previous build, {age:.1f}d old")
                    # Serving it is right; serving it SILENTLY is not. The page
                    # had no way to say "this is last week's screen", so an
                    # unchanged table read as a screen that ran and found the
                    # same funds — not one that never ran at all.
                    return _stamp_fund_payload(data, fallback=True)
                log.warning("fund screen: hiding it — newest build is "
                            + ("of unknown age" if age is None else f"{age:.1f}d old"))
    except Exception as e:
        log.warning(f"fund cache read: {e}")

    if not build_if_missing:
        return {}

    try:
        import funds as _funds
        data = _funds.build()
        if not data.get("ok"):
            return {}
        with _db() as con:
            con.execute("INSERT OR REPLACE INTO newspaper_funds VALUES (?,?)",
                        (week, json.dumps(data)))
            con.commit()
        return _stamp_fund_payload(data, fallback=False)
    except Exception as e:
        log.warning(f"fund screen build failed: {e}")
        return {}


def _stamp_fund_payload(data: dict, fallback: bool) -> dict:
    """Attach provenance the template can render: when, and is it a fallback.

    Every weekly artefact on this page had the same defect — it showed the
    result and not the vintage — so "unchanged since last week" and "did not
    run this week" looked identical to a reader. These two keys are what let
    the section say which one it is.
    """
    if not data:
        return data
    age = _payload_age_days(data)
    data["built_on"] = str(data.get("generated_at") or "")[:10]
    data["age_days"] = None if age is None else round(age, 1)
    data["is_fallback"] = bool(fallback)
    return data


def _migrate_job_runs(con) -> None:
    """Kept as a thin alias. The implementation moved to job_runs.py so cron
    jobs can record their own outcome without importing flask — see that
    module's header for the failure that forced the split."""
    from job_runs import _migrate
    _migrate(con)


def record_job_status(job: str, status: str, detail: str = "",
                      records: int | None = None,
                      expected: int | None = None) -> None:
    """Record the outcome of a publish attempt — success or failure — keyed
    by job name, one row per job.

    Before this, a FAILED weekly build left no trace anywhere the site could
    read: it just didn't write a new payload, and the page kept showing the
    last successful one with no way to tell "unchanged this week" apart from
    "tried and failed this week". This table is deliberately separate from
    the payload tables (newspaper_screen / newspaper_funds /
    newspaper_market_intel) so a failed attempt is queryable even when it
    produced no payload at all.

    `records` and `expected` are how many rows the ATTEMPT produced against
    how many a complete build has. They are not decoration. The 2026-08-18
    audit found the site showing a 750-company table while simultaneously
    warning that the newest rebuild had priced only 50 — the two numbers
    lived in different places and nothing could state them side by side.
    Recorded here, the health layer can say "published 750 / latest attempt
    50 of 750 / FAILED" as one sentence instead of two contradictory ones.

    A free-text detail string could never do this: "only 50 companies priced"
    is unparseable, so no badge, test or API could act on it.
    """
    from job_runs import record
    record(job, status, detail, records, expected)


def get_job_status(job: str, served_generated_at: str | None = None) -> dict:
    """Latest recorded attempt for a job, or {} if none exists yet.

    When served_generated_at is given (the vintage of the payload currently
    on the page), also sets "attempted_after_serve": True when the latest
    attempt is BOTH failed and happened after that payload was built — i.e.
    the site is showing old data not because nothing changed, but because a
    newer attempt existed and failed. That distinction used to be invisible;
    a stale screen and a broken one looked identical to a reader.

    Timestamps come from two different clocks (job_runs is always UTC;
    payload generated_at is UTC for funds/market-intel but IST for the
    stock screen), so this parses both with fromisoformat() rather than
    comparing the raw strings — a naive string compare would misorder an
    IST-stamped payload against a UTC attempt by up to 5.5 hours.
    """
    try:
        from job_runs import latest
        row = latest(job)
        if not row:
            return {}
        out = {**row, "attempted_after_serve": False}
        # The attempt's own coverage, kept separate from the SERVED payload's
        # coverage on purpose. Collapsing them is exactly the contradiction
        # the audit found: one number describing two different builds.
        out["attempt_coverage"] = (f"{row['records']}/{row['expected']}"
                                   if row["records"] is not None and row["expected"] else None)
        if row["status"] == "failed" and served_generated_at:
            try:
                attempt_ts = datetime.fromisoformat(str(row["run_at"]).replace("Z", "+00:00"))
                served_ts = datetime.fromisoformat(str(served_generated_at).replace("Z", "+00:00"))
                out["attempted_after_serve"] = attempt_ts > served_ts
            except ValueError:
                pass
        return out
    except Exception as e:
        log.warning(f"get_job_status({job}): {e}")
        return {}


def _week_key() -> str:
    """Cache key: the SUNDAY-based week, plus the engine that produced the picks.

    Keyed on the IST date, not the runner's UTC date. Everything else on this
    page is stamped IST, and a delayed GitHub run near the UTC/IST boundary
    would otherwise file the new week's picks under the old one.

    The +1 day is what moves the roll from Monday to Sunday. ISO weeks run
    Monday to Sunday, so a bare isocalendar() kept Sunday in the OUTGOING week
    and the picks changed on Monday morning — which is why "why don't the picks
    update on Sunday" had the answer "because they never could". Shifting the
    date forward one day before taking the ISO week means Sunday already reads
    as the next week: the key changes at Sunday 00:00 IST and then holds steady
    Sunday through Saturday.
    """
    d = (datetime.now(IST) + timedelta(days=1)).date()
    return f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}-{PICKS_ENGINE}"


# A screen built on annual statements does not go stale in a week — the
# accounts it ranks on are quarterly at best. Prices inside it DO, which is why
# the provenance strip prints the price date separately: a three-week-old screen
# has usable fundamentals and technical columns the reader must be told to
# distrust. Hiding the section over a stale RSI would take the ROCE table down
# with it.
MAX_STOCK_CACHE_AGE_DAYS = 21


def get_stock_screen(build_if_missing: bool = False) -> dict:
    """This week's Nifty 500 research screen, cached. Empty dict if unavailable.

    Same contract as get_fund_screen, for the same reason: the build is ~11
    minutes of sequential Yahoo fetches across 500 symbols and must never sit
    inside the 6 AM render. The daily paper reads this cache or shows nothing.
    """
    week = _stock_week_key()
    try:
        with _db() as con:
            row = con.execute("SELECT payload FROM newspaper_screen WHERE week=?",
                              (week,)).fetchone()
            if row:
                return _stamp_fund_payload(json.loads(row[0]), fallback=False)

            # Monday always misses the exact key — the ISO week rolls over
            # before the Sunday-night build's week does. Falling back to the
            # newest build and letting its AGE decide is the behaviour #funds
            # had to learn; there is no reason to relearn it here.
            row = con.execute("SELECT payload FROM newspaper_screen "
                              "ORDER BY week DESC LIMIT 1").fetchone()
            if row:
                data = json.loads(row[0])
                age = _payload_age_days(data)
                if age is not None and age <= MAX_STOCK_CACHE_AGE_DAYS:
                    log.info(f"stock screen: serving previous build, {age:.1f}d old")
                    return _stamp_fund_payload(data, fallback=True)
                log.warning("stock screen: hiding it — newest build is "
                            + ("of unknown age" if age is None else f"{age:.1f}d old"))
    except Exception as e:
        log.warning(f"stock cache read: {e}")

    if not build_if_missing:
        return {}

    try:
        import stock_screen as _screen
        # AI injected, like podcasts. Without a key the screen still builds and
        # every row keeps its rule-based SWOT; only the prose layer is absent.
        # Previous build, so the new one can carry deltas. Finding numbers
        # that are IMPROVING matters more than finding ones already high.
        _prev = None
        try:
            with _db() as con:
                _row = con.execute("SELECT payload FROM newspaper_screen "
                                   "ORDER BY week DESC LIMIT 1").fetchone()
            if _row:
                _prev = json.loads(_row[0])
        except Exception as e:
            log.info(f"stock screen: no previous build to diff against ({e})")
        data = _screen.build(ai=groq_complete if GROQ_KEY else None, prev=_prev)
        if not data.get("ok"):
            return {}
        with _db() as con:
            con.execute("CREATE TABLE IF NOT EXISTS newspaper_screen "
                        "(week TEXT PRIMARY KEY, payload TEXT)")
            con.execute("INSERT OR REPLACE INTO newspaper_screen VALUES (?,?)",
                        (week, json.dumps(data)))
            con.commit()
        return _stamp_fund_payload(data, fallback=False)
    except Exception as e:
        log.warning(f"stock screen build failed: {e}")
        return {}


# A listings table is a weekly artefact like the screens. Its prices go stale
# faster than its membership does — a name listed in March is still a March
# listing next week — so the ceiling is generous and the provenance strip
# carries the build date.
MAX_IPO_CACHE_AGE_DAYS = 21


def get_ipos(build_if_missing: bool = False) -> dict:
    """Recent NSE listings and what they have done. Empty dict if unavailable.

    Same contract as get_stock_screen and for the same reason: establishing
    which of 750 names listed recently is two passes of network per symbol,
    which must never sit inside the 6 AM render. ipo_tracker.yml owns the
    clock; this reads the cache and the section hides itself when there is
    nothing to read.
    """
    week = _stock_week_key()
    try:
        with _db() as con:
            con.execute("CREATE TABLE IF NOT EXISTS newspaper_ipos "
                        "(week TEXT PRIMARY KEY, payload TEXT)")
            row = con.execute("SELECT payload FROM newspaper_ipos WHERE week=?",
                              (week,)).fetchone()
            if row:
                return _stamp_fund_payload(json.loads(row[0]), fallback=False)
            row = con.execute("SELECT payload FROM newspaper_ipos "
                              "ORDER BY week DESC LIMIT 1").fetchone()
            if row:
                data = json.loads(row[0])
                age = _payload_age_days(data)
                if age is not None and age <= MAX_IPO_CACHE_AGE_DAYS:
                    log.info(f"ipos: serving previous build, {age:.1f}d old")
                    return _stamp_fund_payload(data, fallback=True)
                log.warning("ipos: hiding it — newest build is "
                            + ("of unknown age" if age is None else f"{age:.1f}d old"))
    except Exception as e:
        log.warning(f"ipo cache read: {e}")

    if not build_if_missing:
        return {}
    try:
        import ipo_tracker as _ipo
        data = _ipo.build()
        if not data.get("ok"):
            return {}
        with _db() as con:
            con.execute("CREATE TABLE IF NOT EXISTS newspaper_ipos "
                        "(week TEXT PRIMARY KEY, payload TEXT)")
            con.execute("INSERT OR REPLACE INTO newspaper_ipos VALUES (?,?)",
                        (week, json.dumps(data)))
            con.commit()
        return _stamp_fund_payload(data, fallback=False)
    except Exception as e:
        log.warning(f"ipo build failed: {e}")
        return {}


def _stock_week_key() -> str:
    """ISO week on the IST date. No engine tag — see _fund_week_key."""
    d = datetime.now(IST).date()
    return f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"


def get_podcasts(build_if_missing: bool = False) -> dict:
    """Today's podcast list, cached DAILY. Empty dict when unavailable.

    Same shape as get_fund_screen and for the same reasons: a per-channel feed
    fetch plus an AI pass per episode is real work, and a failure must hide the
    section rather than fail the build.

    Keyed by DATE, not ISO week. It was weekly, which meant a new episode from
    any of these channels waited up to seven days to appear — and these are
    daily-to-every-other-day publishers, so the list was routinely showing
    episodes several days behind while newer ones sat unlisted. Sixteen feeds
    and a bounded AI pass is affordable in the daily build; the fund and stock
    screens are the two that genuinely are not, and both keep their own clocks.
    The cache column is still called `week` because renaming a primary key
    across a live Turso table buys nothing — the VALUE is a date now.

    The AI is injected rather than imported by podcasts.py so that module stays
    standalone-testable — `python podcasts.py` runs it against the live feeds
    with no key and takes the deterministic path.
    """
    week = datetime.now(IST).strftime("%Y-%m-%d")
    stale = None          # newest date-keyed row, used only if the build fails
    try:
        with _db() as con:
            row = con.execute("SELECT payload FROM newspaper_podcasts WHERE week=?",
                              (week,)).fetchone()
            if row:
                return _stamp_fund_payload(json.loads(row[0]), fallback=False)

            # Fallback to the newest build we have. A list a couple of days old
            # is still a real list of real episodes and an empty section is not
            # better — but the ceiling came down from 14 days to 4 when this
            # went daily. At 14 the section could serve a fortnight-old list
            # under today's date, which is precisely the "did it run?" ambiguity
            # the provenance strip exists to remove.
            # Date-shaped keys ONLY. This section's key format changed from ISO
            # week ("2026-W33") to date ("2026-08-12"), and "W" sorts above every
            # digit — so `ORDER BY week DESC` returned the stale WEEKLY row
            # forever, it was inside the age limit, and it was served as a
            # fallback on every build. The daily rebuild therefore never ran:
            # the section kept showing ten episodes from the old list while
            # reporting "previous week", and nothing errored.
            row = con.execute(
                "SELECT payload FROM newspaper_podcasts "
                "WHERE week LIKE '____-__-__' AND week NOT LIKE '%W%' "
                "ORDER BY week DESC LIMIT 1").fetchone()
            if row:
                stale = json.loads(row[0])
    except Exception as e:
        log.warning(f"podcast cache read: {e}")

    # BUILD BEFORE FALLING BACK. The fund screen returns its stale payload here
    # and never builds, which is right for a section whose builder is a separate
    # weekly workflow. This one builds inline and is supposed to refresh every
    # day — so returning the fallback first meant a ≤4-day-old row pre-empted
    # the rebuild entirely, and "daily" silently became "whenever the cache aged
    # past four days". The date key had already rolled over and the section was
    # serving yesterday's list under today's date.
    if build_if_missing:
        try:
            import podcasts as _pod
            data = _pod.build(ai=groq_complete if GROQ_KEY else None)
            if data.get("ok"):
                with _db() as con:
                    con.execute("INSERT OR REPLACE INTO newspaper_podcasts VALUES (?,?)",
                                (week, json.dumps(data)))
                    con.commit()
                return _stamp_fund_payload(data, fallback=False)
            log.warning("podcasts: build produced nothing")
        except Exception as e:
            log.warning(f"podcast build failed: {e}")

    # Only now: yesterday's list beats an empty section, and the strip says so.
    if stale:
        age = _payload_age_days(stale)
        if age is not None and age <= 4:
            log.info(f"podcasts: serving previous build, {age:.1f}d old")
            return _stamp_fund_payload(stale, fallback=True)
        log.warning("podcasts: newest build is "
                    + ("of unknown age" if age is None else f"{age:.1f}d old") + " — hidden")
    return {}


def _iso_week() -> str:
    """IST-dated ISO week. The cache key for anything rebuilt weekly."""
    d = datetime.now(IST).date()
    return f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"


def _fund_week_key() -> str:
    """Cache key for the fund screen: ISO week only, no engine suffix.

    The screen used to share _week_key() with the stock picks, which embeds
    PICKS_ENGINE. Bumping the picks engine — a change to how EQUITIES are
    scored, nothing whatsoever to do with mutual funds — therefore orphaned
    the fund cache and pushed the section onto its "previous build" fallback
    until the weekly job next ran. That happened on 2026-08-10 with the v4→v5
    bump, and it is the second time this key has silently expired data it does
    not own.

    Two caches, two keys. Same IST-dated ISO week as everything else.
    """
    return _iso_week()


# A day old is still today's picture for slow-moving context (which sector
# led yesterday is still informative), but corporate-action ex-dates and
# FII/DII flow are date-specific facts — showing them under the wrong date
# is actively misleading, not just stale. Shorter ceiling than funds (15d)
# or podcasts (4d) for that reason.
MAX_MARKET_INTEL_CACHE_AGE_DAYS = 3

# A briefing is a claim about TODAY. One day stale, clearly labelled, still
# beats an empty section; a week stale is a different product and is hidden.
MAX_BRIEF_AGE_DAYS = 2


def get_market_intel(build_if_missing: bool = False) -> dict:
    """Today's corporate actions / FII-DII / sector heat, cached daily by a
    separate workflow (market_intel.yml) — same contract as get_fund_screen:
    NSE's endpoints are third-party calls that can hang, so the daily paper
    only ever READS this cache, never builds it inline.

    Also assembles fii_dii_trend from the last 7 CACHED rows, not from a
    single API call — NSE's fiidiiTradeReact endpoint returns only the
    latest day's provisional figures (confirmed directly), so a real 7-day
    trend has to accumulate one cached day at a time, same as this section
    genuinely will over its first week live rather than faking a backfill.

    `build_if_missing` is safe HERE in a way it is not for the fund or stock
    screens: this payload is three bounded fetches (~30s measured on the
    runner), not 11-15 minutes. It exists because market_intel.yml and
    newspaper.yml race. Scheduled runs on this repo land 1.5-3h late, and the
    gap between the two crons is smaller than that drift, so on 2026-08-17 the
    paper built at 14:47 UTC and the cache was not written until 15:33 —
    "0 corporate actions, 0 sectors", section silently dropped from the nav.
    Widening the cron gap narrows that window; only building inline closes it.
    """
    today = datetime.now(IST).strftime("%Y-%m-%d")
    stale: dict | None = None
    try:
        with _db() as con:
            row = con.execute(
                "SELECT payload FROM newspaper_market_intel WHERE date=?",
                (today,)).fetchone()
            if row:
                data = json.loads(row[0])
                # An empty row still counts as today's answer when we are not
                # allowed to build — but never let it mask a good previous day.
                if _has_market_intel(data) or not build_if_missing:
                    data = _stamp_fund_payload(data, fallback=False)
                    data["fii_dii_trend"] = _fii_dii_trend(con, today)
                    return data

            row = con.execute(
                "SELECT payload FROM newspaper_market_intel "
                "ORDER BY date DESC LIMIT 1").fetchone()
            if row:
                data = json.loads(row[0])
                age = _payload_age_days(data)
                if age is not None and age <= MAX_MARKET_INTEL_CACHE_AGE_DAYS:
                    data = _stamp_fund_payload(data, fallback=True)
                    data["fii_dii_trend"] = _fii_dii_trend(con, today)
                    if not build_if_missing:
                        log.info(f"market intel: serving previous build, {age:.1f}d old")
                        return data
                    stale = data
                else:
                    log.warning("market intel: newest build is "
                                + ("of unknown age" if age is None else f"{age:.1f}d old")
                                + " — hidden")
    except Exception as e:
        log.warning(f"market intel cache read: {e}")

    if not build_if_missing:
        return {}
    try:
        import market_intel as _mi
        data = _mi.build()
        # build() reports ok=True even when every fetch came back empty — it
        # deliberately does not paper over a gap. Caching that would pin an
        # empty row to today's date and hide the previous day's real data for
        # the rest of the day, so an empty build is treated as no build.
        if not data.get("ok") or not _has_market_intel(data):
            log.warning("market intel: inline build returned nothing"
                        + (" — serving previous build" if stale else ""))
            return stale or {}
        with _db() as con:
            con.execute("INSERT OR REPLACE INTO newspaper_market_intel VALUES (?,?)",
                        (today, json.dumps(data)))
            con.commit()
            data = _stamp_fund_payload(data, fallback=False)
            data["fii_dii_trend"] = _fii_dii_trend(con, today)
        log.info("market intel: built inline (cache was missing for today)")
        return data
    except Exception as e:
        log.warning(f"market intel build failed: {e}")
        return stale or {}


def get_brief(build_if_missing: bool = False) -> dict:
    """Today's Daily Intelligence Brief, cached by IST date.

    Same contract as get_market_intel: the daily paper READS this cache, and
    brief.yml owns the clock. `build_if_missing` closes the same race — the
    two workflows drift independently and a paper that builds first would ship
    without the section entirely.

    Falls back to the most recent edition within MAX_BRIEF_AGE_DAYS and marks
    it stale. A day-old briefing clearly labelled beats an empty section; a
    week-old one does not, so it is hidden rather than dressed up as today.
    """
    today = datetime.now(IST).strftime("%Y-%m-%d")
    stale = None
    try:
        with _db() as con:
            row = con.execute("SELECT payload FROM newspaper_brief WHERE date=?",
                              (today,)).fetchone()
            if row:
                data = json.loads(row[0])
                if data.get("events"):
                    return _stamp_fund_payload(data, fallback=False)

            row = con.execute("SELECT payload FROM newspaper_brief "
                              "ORDER BY date DESC LIMIT 1").fetchone()
            if row:
                data = json.loads(row[0])
                age = _payload_age_days(data)
                if data.get("events") and age is not None and age <= MAX_BRIEF_AGE_DAYS:
                    data = _stamp_fund_payload(data, fallback=True)
                    if not build_if_missing:
                        log.info(f"brief: serving previous edition, {age:.1f}d old")
                        return data
                    stale = data
    except Exception as e:                                   # noqa: BLE001
        log.warning(f"brief cache read: {e}")

    if not build_if_missing:
        return {}
    try:
        import brief_engine
        data = brief_engine.build()
        if not data.get("events"):
            log.warning("brief: inline build produced no events"
                        + (" — serving previous edition" if stale else ""))
            return stale or {}
        with _db() as con:
            con.execute("INSERT OR REPLACE INTO newspaper_brief VALUES (?,?)",
                        (today, json.dumps(data)))
            con.commit()
        log.info("brief: built inline (cache was empty for today)")
        return _stamp_fund_payload(data, fallback=False)
    except Exception as e:                                   # noqa: BLE001
        log.warning(f"brief build failed: {e}")
        return stale or {}


def _has_market_intel(data: dict) -> bool:
    """Whether a payload carries anything worth rendering. Mirrors the guard
    in _sections_present() — a payload with all three pieces empty drops the
    section, so it must not be cached as today's answer either."""
    d = data or {}
    return bool(d.get("corporate_actions") or d.get("market_heat") or d.get("fii_dii"))


def _fii_dii_trend(con, before_date: str, days: int = 7) -> list[dict]:
    """Last N cached days' fii_dii figures, oldest first — built from
    accumulated daily cache rows, see get_market_intel()'s own docstring."""
    rows = con.execute(
        "SELECT date, payload FROM newspaper_market_intel "
        "WHERE date <= ? ORDER BY date DESC LIMIT ?",
        (before_date, days)).fetchall()
    out, seen = [], set()
    for d, payload in rows:
        try:
            fd = (json.loads(payload) or {}).get("fii_dii")
        except (json.JSONDecodeError, TypeError):
            fd = None
        if not fd:
            continue
        # `d` is the CACHE row's date — the day this build ran. `fd` carries
        # NSE's own TRADE date, and because it is spread second it wins. That is
        # correct: a flow belongs to the session it happened in, not to the day
        # a build noticed it.
        #
        # But it means two builds can carry the SAME trade date. NSE publishes
        # after the close, so a build that runs before the next day's figures
        # land re-reads yesterday's — and the series then plotted one session
        # twice, side by side, as if it were two. Rows are walked newest-cache
        # first, so the first sighting of a trade date is the most recently
        # built version of it and later duplicates are dropped.
        row = {"date": d, **fd}
        key = str(row.get("date"))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    out.reverse()
    return out


def _warm_picks_cache():
    week = _week_key()
    with _db() as con:
        row = con.execute("SELECT picks FROM newspaper_stocks_picked WHERE pick_date=?", (week,)).fetchone()
        if row:
            with _picks_lock:
                _picks_cache[week] = json.loads(row[0])
            log.info(f"picks: loaded from DB cache ({week})")
            return
    log.info(f"picks: warming cache for {week} — scanning {len(WATCHLIST)} stocks...")
    picks = _build_picks()
    with _db() as con:
        con.execute("INSERT OR REPLACE INTO newspaper_stocks_picked VALUES (?,?)", (week, json.dumps(picks)))
    with _picks_lock:
        _picks_cache[week] = picks
    log.info(f"picks: cached {len(picks)} top picks for {week}")

    # Mirror into the ledger. These are the paper's front-page ideas and until
    # now they existed ONLY in this cache — chosen weekly, shown to the reader
    # as the week's picks, and never recorded anywhere that could later say
    # whether they worked. Every scan engine was accountable; the most
    # prominent ideas on the page were not.
    #
    # Written here, at the moment a week's picks are FIRST built, so it fires
    # exactly once per week rather than on every page build. Best-effort: the
    # picks are already committed above, so a ledger failure must not cost the
    # reader the section.
    try:
        import tracker
        ids = tracker.log_top5_picks(picks, week)
        log.info(f"picks: mirrored {len([i for i in ids if i])} to the ledger")
    except Exception as e:                                   # noqa: BLE001
        log.warning(f"picks: ledger mirror failed: {e}")

# The reward/risk floor every engine enforces. Read from config when config is
# importable, with an explicit fallback that must stay equal to it.
#
# `from config import MIN_RR` inside a function looked harmless and was not:
# config.py calls _require("TELEGRAM_TOKEN") at module scope, so importing it
# for one float raises whenever the Telegram secrets are absent — which is
# exactly the case in the newspaper workflow. That exception was thrown inside
# _warm_picks_cache's worker THREAD, where it printed a traceback and killed
# only that thread. The build carried on, the picks cache stayed empty,
# get_top5_picks() returned nothing, and generate.py fell through to
# last_known_picks() — the previous week's list, unfiltered. MRK shipped as the
# week's top idea at 0.65 R:R underneath a paragraph promising a 2:1 minimum.
#
# A threshold is not a credential and must not be reachable only through one.
try:
    from config import MIN_RR as _MIN_RR
    MIN_RR = float(_MIN_RR)
except Exception:                                     # noqa: BLE001
    MIN_RR = 2.0
    log.warning("config.MIN_RR unreadable (config requires secrets) — "
                "using the built-in floor of %.1f", MIN_RR)


def _rr_floor(picks: list[dict]) -> list[dict]:
    """Drop ideas that do not clear config.MIN_RR, at READ time.

    _build_picks() already applies this floor, but the picks are a weekly
    SNAPSHOT stored in newspaper_stocks_picked and read back all week. Any row
    written before the floor existed sails straight past it — which is how MRK
    stayed on the front page as the week's top idea at an R:R of 0.65 against a
    2.0 minimum, alongside COFORGE at 1.3 and PAYTM at 1.51.

    A rule that only runs at write time is not a rule, it is a rule about
    Mondays. Enforcing on read means a cached snapshot cannot outlive the
    standard it was built under, and a threshold change takes effect on the
    next page build rather than the next ISO week.

    An idea with no rr at all is dropped too: this page publishes a REWARD for
    a stated RISK, and one without the ratio cannot be checked against the
    floor. Unmeasurable is not the same as acceptable.
    """
    out = []
    for p in picks or []:
        try:
            rr = float(p.get("rr"))
        except (TypeError, ValueError):
            continue
        if rr >= MIN_RR:
            out.append(p)
    return out


def get_top5_picks(build_if_missing: bool = False) -> list[dict]:
    """This week's five ideas.

    `build_if_missing` exists because the static generator has no long-running
    process behind it. Under Flask, _warm_picks_cache() runs on a background
    thread at startup; generate.py never called it, so on the first build of a
    new ISO week the DB had no row for that week and the section rendered its
    "check back Monday" empty state — every Monday, all day. See generate.py.
    """
    # ONE exit, so the floor cannot be bypassed by whichever path happens to be
    # taken. It was applied on the DB read and on the build_if_missing branch
    # but NOT on the in-memory early return — and a startup thread warms
    # _picks_cache before generate.py ever calls this, so the unfiltered branch
    # was the one that always ran. Three returns, two of them filtered, and the
    # third was the live one. Collect, then filter, then return.
    week = _week_key()
    picks = None

    with _picks_lock:
        if week in _picks_cache:
            picks = _picks_cache[week]

    if picks is None:
        with _db() as con:
            row = con.execute("SELECT picks FROM newspaper_stocks_picked WHERE pick_date=?",
                              (week,)).fetchone()
        if row:
            picks = json.loads(row[0])
            with _picks_lock:
                _picks_cache[week] = picks

    if picks is None and build_if_missing:
        _warm_picks_cache()
        with _picks_lock:
            picks = _picks_cache.get(week, [])

    return _rr_floor(picks or [])


def picks_outcomes(week: str) -> dict:
    """What the ledger now says about this week's five ideas, keyed by symbol.

    The section renders from the newspaper_stocks_picked cache, which is a
    SNAPSHOT of the ranking at the moment it was built and carries no exit
    state. The picks are separately mirrored into the ledger as top5_pick and
    graded there like every other signal — so a pick could stop out on Monday
    and still sit on the front page all week presented as a live idea. SMCI
    did exactly that: SL_HIT at -8.01% on 2026-08-17, still shown as one of
    five ideas.

    Cache and ledger are two records of the same five names, and only one of
    them was being read. This joins them back together.
    """
    try:
        with _db() as con:
            rows = con.execute(
                "SELECT symbol, status, pnl_pct, closed_at FROM all_signals "
                "WHERE signal_type = 'top5_pick' AND metadata LIKE ?",
                (f'%"week": "{week}"%',)).fetchall()
    except Exception as e:                                   # noqa: BLE001
        log.warning(f"picks_outcomes({week}): {e}")
        return {}
    out = {}
    for r in rows:
        status = str(r["status"] or "").upper()
        if status in ("", "OPEN"):
            continue                      # still running — nothing to report
        out[str(r["symbol"])] = {
            "status": status,
            "pnl_pct": r["pnl_pct"],
            "closed_at": (str(r["closed_at"])[:10] if r["closed_at"] else None),
            "is_loss": status in ("SL_HIT", "STOPPED", "STOP_HIT"),
        }
    return out


def last_known_picks() -> tuple[list[dict], str | None]:
    """Most recent week's picks, whatever week that was, plus its key.

    A fallback for the build: stale ideas clearly labelled as last week's beat
    an empty section, and beat blocking the whole newspaper on a Yahoo outage.
    """
    with _db() as con:
        row = con.execute(
            "SELECT pick_date, picks FROM newspaper_stocks_picked "
            "ORDER BY pick_date DESC LIMIT 1").fetchone()
    if not row:
        return [], None
    try:
        # The floor applies here as well. This is the path that actually
        # published MRK: a fallback that skips the standard is not a fallback,
        # it is a hole in it.
        return _rr_floor(json.loads(row[1])), row[0]
    except (ValueError, TypeError):
        return [], None

# ─────────────────────────────────────────────────────────────
# STOCK TRACKER
# ─────────────────────────────────────────────────────────────

def open_setup_context(alerts: list[dict]) -> dict:
    """Live price and sector for every OPEN setup, keyed by symbol.

    Serves two questions the page could not answer:

      · Which of these is actionable TODAY? A setup whose entry is 9% below
        the last price has already run away; one sitting 0.4% away is live.
      · How correlated is the book? Heat counted twenty setups at 1% as 20%
        of risk. If fifteen share a sector that is nearer one bet held fifteen
        ways, and the honest number is worse.

    Prices are fetched in ONE batched download. The first version called
    yf.Ticker().fast_info per symbol and then .info for the sector; from a
    GitHub runner Yahoo rate-limited every one of them and the build logged
    "0 symbols priced" while reporting success. Batch for prices, and cache
    sectors permanently — a company changes sector approximately never, so
    fetching it more than once per symbol is pure rate-limit exposure.
    """
    syms = sorted({str(a.get("symbol") or "").strip()
                   for a in alerts
                   if str(a.get("badge") or "") == "open" and a.get("symbol")})
    if not syms:
        return {}
    syms = syms[:60]                     # bounded: cannot stall the build

    # Imported here, not at module scope: newspaper.py is imported by
    # generate.py at build time and by the Flask app at boot, and a top-level
    # import of a sibling module that itself imports yfinance lengthens both.
    from symbols import to_yahoo

    with _db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS symbol_sector (
            symbol TEXT PRIMARY KEY, sector TEXT, fetched_at TEXT
        )""")
        known = {r[0]: r[1] for r in
                 con.execute("SELECT symbol, sector FROM symbol_sector").fetchall()}

    # ── prices: one request for the lot ──
    prices: dict[str, float] = {}
    ymap = {to_yahoo(s): s for s in syms}
    try:
        df = yf.download(list(ymap.keys()), period="5d", interval="1d",
                         progress=False, auto_adjust=True, group_by="ticker")
        for yt, orig in ymap.items():
            try:
                # group_by="ticker" gives MultiIndex columns for >1 symbol and
                # flat columns for exactly one. nlevels avoids importing pandas
                # into this module for a single isinstance check.
                col = df[yt]["Close"] if getattr(df.columns, "nlevels", 1) > 1 else df["Close"]
                col = col.dropna()
                if len(col):
                    prices[orig] = round(float(col.iloc[-1]), 4)
            except Exception:
                continue
    except Exception as e:
        log.warning(f"open_setup_context batch price fetch: {e}")

    # ── sectors: only for symbols never seen before ──
    missing = [s for s in syms if s not in known]
    for sym in missing[:25]:             # cap the slow path per build
        sec = ""
        try:
            sec = str((yf.Ticker(to_yahoo(sym)).info or {}).get("sector") or "")
        except Exception:
            sec = ""
        known[sym] = sec
        try:
            with _db() as con:
                con.execute("INSERT OR REPLACE INTO symbol_sector VALUES (?,?,?)",
                            (sym, sec, datetime.now(IST).isoformat()))
                con.commit()
        except Exception:
            pass

    out: dict[str, dict] = {}
    for sym in syms:
        px, sec = prices.get(sym), known.get(sym, "")
        if px is None and not sec:
            continue
        out[sym] = {"price": px, "sector": sec}
    return out


def get_tracker_stocks() -> list[dict]:
    # Named columns and an explicit mapping, not SELECT * with keyed access.
    # row_factory does not survive the Turso connection wrapper, so keyed
    # access raised "tuple indices must be integers" and took the whole 6 AM
    # build down with it — but only once a position existed, which is why an
    # empty book hid it.
    cols = ("id", "symbol", "name", "entry_price", "current_price",
            "target_price", "stop_loss", "thesis", "timeframe", "added_date")
    with _db() as con:
        rows = con.execute(
            f"SELECT {', '.join(cols)} FROM stock_tracker "
            "WHERE status='active' ORDER BY added_date DESC").fetchall()
    out = []
    for raw in rows:
        r = dict(zip(cols, raw))
        sym = r["symbol"]
        current = r["current_price"] or r["entry_price"] or 0
        try:
            current = round(yf.Ticker(sym).fast_info.last_price, 2)
            with _db() as con:
                con.execute("UPDATE stock_tracker SET current_price=?, updated_at=? WHERE id=?",
                            (current, datetime.now(IST).isoformat(), r["id"]))
        except Exception: pass
        entry   = r["entry_price"] or current
        pnl_pct = (current - entry) / entry * 100 if entry else 0
        currency = "₹" if ".NS" in sym or ".BO" in sym else "$"
        out.append({"id": r["id"], "symbol": sym, "name": r["name"] or sym,
                    "entry_price": entry, "current_price": current,
                    "target_price": r["target_price"] or 0, "stop_loss": r["stop_loss"] or 0,
                    "thesis": r["thesis"] or "", "timeframe": r["timeframe"] or "",
                    "pnl_pct": round(pnl_pct, 2), "added_date": r["added_date"] or "",
                    "currency": currency, "winning": pnl_pct >= 0})
    return out

def add_to_tracker(symbol, entry_price, target_price, stop_loss, thesis, timeframe="2-3 months", name=""):
    with _db() as con:
        con.execute("""INSERT INTO stock_tracker
            (symbol, name, added_date, entry_price, current_price, target_price,
             stop_loss, thesis, timeframe, status, updated_at) VALUES (?,?,?,?,?,?,?,?,?,'active',?)""",
            (symbol.upper(), name or symbol, date.today().isoformat(),
             entry_price, entry_price, target_price, stop_loss, thesis, timeframe,
             datetime.now(IST).isoformat()))

def exit_tracker(stock_id: int):
    with _db() as con:
        con.execute("UPDATE stock_tracker SET status='exited', updated_at=? WHERE id=?",
                    (datetime.now(IST).isoformat(), stock_id))

# ─────────────────────────────────────────────────────────────
# MONEY HACKS
# ─────────────────────────────────────────────────────────────

MONEY_HACKS = [
    ("The 50-30-20 Rule", "50% needs · 30% wants · 20% savings. Automate the 20% on salary day."),
    ("SIP on Salary Day", "Set SIP date = salary day + 1. Invest before you spend. Pay yourself first."),
    ("Expense Tracking", "Track every expense for 30 days. You will find ₹3–5K of invisible leaks."),
    ("Tax-Loss Harvesting", "Book losses at year-end to offset LTCG. Reinvest post 30 days. Saves 10–15% tax."),
    ("EPF Power", "EPF gives 8.25% guaranteed, tax-free. Max out VPF if your employer allows."),
    ("No Lifestyle Inflation", "Got a raise? Don't upgrade lifestyle. Invest the increment for 3 years."),
    ("Emergency Fund", "6 months of expenses in a liquid FD. Job loss + medical can overlap."),
    ("Credit Card Strategy", "Use card for all spends, pay in full before due. Earn 1–2% cashback. Never pay interest."),
    ("NPS for Tax Saving", "₹50K in NPS under 80CCD(1B) = ₹15K saved at 30% bracket. Plus retirement corpus."),
    ("Index Funds over Active", "85% of large-cap active funds underperform Nifty over 10 years. Index at 0.1% expense ratio."),
    ("Term Insurance First", "₹1Cr term insurance (₹10–15K/year at 25). Non-negotiable. Buy this before any investment."),
    ("ELSS Lock-in Trick", "ELSS 3-year lock-in: SIP each month → each instalment unlocks separately. Best 80C option."),
    ("Gold via SGB", "Sovereign Gold Bonds: 2.5% interest + gold price appreciation. No storage cost."),
    ("Auto-Sweep FD", "Link savings to sweep FD. Idle cash earns FD rates automatically."),
    ("Direct Funds Only", "Regular mutual funds cost 1–1.5% more per year. Over 20 years = 30% of corpus gone."),
]

def get_money_hack() -> dict:
    idx = date.today().toordinal() % len(MONEY_HACKS)
    title, body = MONEY_HACKS[idx]
    return {"title": title, "body": body}

PRODUCTIVITY_TIPS = [
    "Eat the frog: hardest task first, before checking any messages.",
    "2-minute rule: if it takes under 2 min, do it now. Don't queue it.",
    "Time-block your calendar. Unblocked time = wasted time.",
    "90-min deep work sprints. No phone. Door closed. Results compound.",
    "Write tomorrow's top 3 tasks tonight. Wake up with a plan.",
    "Done > perfect. Ship at 80%, iterate on real feedback.",
    "Weekly review: 15 min every Sunday. What worked, what's next week's #1.",
    "Batch similar tasks. Answer all messages in one sitting.",
    "Phone in another room during deep work. Physical distance reduces urge 60%.",
    "End every meeting with: who does what by when. No action = no meeting.",
    "Read 10 pages of non-fiction daily. 10 × 365 = 12 books/year.",
    "Respond to messages at set times. Real-time response is a myth.",
    "Track energy, not just time. Hard work when energy is highest.",
    "Define 'done' before starting. Vague tasks never finish.",
    "Build systems, not goals. Goals are outcomes; systems produce them.",
    "Under-promise, over-deliver. Every time. Build a reputation.",
    "Weekly financial review: 10 min. Net worth, cash flow, investments.",
    "Clear inbox to zero before 9 AM. Empty inbox = no mental overhead.",
    "Use Parkinson's Law: shorter deadlines. Work expands to fill time given.",
    "Shutdown ritual: write tomorrow's top 3, close all tabs. Stop working.",
]

def get_productivity_tip() -> str:
    idx = (date.today().toordinal() + 7) % len(PRODUCTIVITY_TIPS)
    return PRODUCTIVITY_TIPS[idx]

# ─────────────────────────────────────────────────────────────
# DUBAI CORNER — AED 30K+ TRACK
#
# This pane was a single hardcoded block in the template. Same headline, same
# five employers, same cover-letter tip, every day since it shipped — the one
# section on a page that rebuilds daily which never changed. It is also the
# section attached to the deadline that matters (Dubai FP&A, AED 30K+, by
# mid-2026), so a static pane there is worse than none.
#
# Fields: title, body, targets line, (action label, action).
# ─────────────────────────────────────────────────────────────

DUBAI_TRACK = [
    ("The stack that clears AED 30K",
     "CA or ACCA, plus SAP or Oracle, plus Power BI, plus IFRS 9 and 16. That is the whole gate. "
     "Miss one and you are competing on price.",
     "Targets: ADNOC · Emirates · Majid Al Futtaim · DP World · FAB · Emaar.",
     ("Keyword tip", 'Put "IFRS 16 implementation" and "rolling forecast" in the cover letter. '
      "Recruiters grep for exactly those two strings.")),

    ("What AED 30K actually is",
     "AED 30,000/month is ₹6,86,000 at 22.87. Tax on it: zero. The equivalent Indian gross, "
     "after 30% plus cess, is roughly ₹1.18 crore a year. That is the arbitrage — not the number itself.",
     "Reality check: housing eats AED 6–9K of it in Dubai Marina or JVC.",
     ("Run the number", "Build the AED-to-net-savings model before the first interview. "
      "Negotiating without it is negotiating on vibes.")),

    ("The package is not the salary",
     "Gulf offers split into basic, housing, transport and a school allowance. Gratuity accrues on "
     "BASIC only — 21 days' basic per year for the first five. An offer that is 40% basic and 60% "
     "allowances quietly halves your end-of-service.",
     "Ask for the split in writing before you counter.",
     ("Negotiation lever", "Push basic up, not total up. Same headline number, materially more gratuity.")),

    ("Who actually hires FP&A in the UAE",
     "Four pools, in order of volume: government-linked groups (ADNOC, DEWA, Etihad), retail and "
     "F&B conglomerates (MAF, Landmark, Alshaya), logistics and ports (DP World, Aramex), and Big 4 "
     "advisory. Retail hires the most and pays the least. Government-linked pays the most and moves slowest.",
     "Best ratio of pay to speed: logistics and healthcare groups.",
     ("This week", "Pick one pool. Applying across all four with one CV is why nothing lands.")),

    ("The visa question, answered",
     "Employment visa is sponsored by the employer — you do not need one to apply, and any recruiter "
     "asking you to pay for one is running a scam. The Golden Visa needs a AED 30K+ salary and a "
     "degree attestation, which is exactly why AED 30K is the threshold worth targeting.",
     "Attest the degree in India first. It takes 3–6 weeks and blocks everything downstream.",
     ("Do now", "MEA attestation on the CA certificate and the degree. Before an offer, not after.")),

    ("Your CV is being read by software",
     "Most Gulf groups run Taleo or SuccessFactors. Two columns, tables, headers and a photo all "
     "parse to garbage. Single column, standard headings, .docx not PDF, no logos.",
     "Job title in the CV header must match the job title in the posting. Literally.",
     ("30-minute fix", "Rewrite the top third of the CV as: title, then five bullets, "
      "each with a number in it.")),

    ("The IFRS 16 story they want",
     "Every UAE retail and logistics group is lease-heavy, so IFRS 16 is not a technical question — "
     "it is the whole job. Have one story ready: the portfolio size, the discount rate you used, "
     "how you handled the transition, and what broke.",
     "If you have not run one, say so and describe the mechanics anyway. Bluffing gets caught in round two.",
     ("Prep", "Write the story in 200 words. Say it out loud until it takes 90 seconds.")),

    ("Recruiters worth the email",
     "Michael Page, Robert Half, Hays and Charterhouse run most of the AED 25–45K finance mandates. "
     "Cooper Fitch and Nathan HR sit closer to the local groups.",
     "One consultant per firm. Four firms. Not forty applications.",
     ("Message", "Two lines: what you do, what you want, salary expectation. Recruiters bin the essays.")),

    ("Time your application to the budget cycle",
     "UAE groups build budgets September to November and hire FP&A ahead of it. January is the second "
     "window, funded by the new year's headcount. Ramadan and July–August are dead.",
     "That makes September the highest-yield month of the year to be applying.",
     ("Plan", "Have the CV, LinkedIn and referral list finished by mid-August. Not started.")),

    ("The 'why Dubai' answer",
     "Every panel asks it, and 'better opportunities' loses the room. The answer that works is "
     "specific: the sector, the company's stage, and what you do about a problem they actually have.",
     "Weak: growth and exposure. Strong: your lease portfolio doubled after the 2024 expansion.",
     ("Homework", "Read the last annual report before the call. Quote one number from it.")),

    ("Referral beats portal, by a lot",
     "Applications through a company portal convert at low single digits. An internal referral converts "
     "an order of magnitude better. The list of people who can refer you is smaller than you think and "
     "you already know most of them.",
     "ICAI Dubai Chapter has roughly 4,000 members. That is the network.",
     ("This week", "Message five CAs already in the UAE. Ask for a 15-minute call, not a job.")),

    ("Power BI is the tiebreaker",
     "Between two CAs with identical FP&A experience, the one who can build the dashboard gets the "
     "offer. Not because the dashboard matters — because it proves you will not need an analyst.",
     "DAX, one real model, published. A certificate without an artefact proves nothing.",
     ("Build", "One dashboard on your own trading ledger. It is real data and it is defensible.")),

    ("What the counter-offer is really for",
     "First offers in the Gulf come in 10–15% below the band because everybody counters. Not "
     "countering does not read as humble; it reads as someone who did not check.",
     "Counter once, with a number and a reason. Then stop.",
     ("Script", '"Based on the band for this role and my IFRS 16 experience, I was targeting AED X."')),

    ("The cost side nobody models",
     "Housing is paid annually up front in Dubai, or quarterly at a premium. Schooling runs AED 25–60K "
     "per child per year. Health insurance is mandatory and usually covered. Add a AED 40–60K "
     "first-year setup cost before the savings maths works.",
     "Sharjah rent is roughly half of Dubai's. The commute costs you 90 minutes a day.",
     ("Model it", "Year-one net savings, not monthly salary. They are very different numbers.")),

    ("Arabic is not required. Reading it helps.",
     "No finance role in the Gulf requires Arabic. But invoices, government portals and trade licences "
     "arrive in it, and being able to read a heading rather than forwarding it is a visible edge.",
     "Numbers, months, and twenty document words. That is the whole ask.",
     ("Ten minutes", "Learn the Arabic numerals and the words for invoice, tax, and licence.")),

    ("Corporate tax changed the job",
     "The UAE introduced 9% corporate tax from June 2023. Every group now needs someone who can model "
     "an effective tax rate, handle transfer pricing between free-zone and mainland entities, and file. "
     "That is new demand, and the supply of people who have actually done it is thin.",
     "Free-zone qualifying income at 0% is the question that separates the prepared from the rest.",
     ("Edge", "Read the FTA corporate tax guide. Two hours buys you a whole interview answer.")),

    ("Free zone vs mainland, in one line",
     "A free-zone entity is 100% foreign-owned, tax-advantaged and restricted to trading within its "
     "zone or abroad. A mainland entity can trade anywhere in the UAE and pays the 9%. Groups run both, "
     "and the intercompany between them is where the FP&A work lives.",
     "If you can explain this cleanly, you are ahead of most candidates.",
     ("Say it", "Practise the explanation in 30 seconds. It comes up constantly.")),

    ("Six weeks, not six months",
     "Government-linked groups take three to six months from application to offer. Private groups take "
     "four to eight weeks. Applying only to the first kind and then reading the silence as rejection is "
     "the most common way this search dies.",
     "Run both pipelines at once. The fast one funds the patience for the slow one.",
     ("Track it", "One sheet: company, date applied, stage, next action, date of next action.")),

    ("The LinkedIn setting that matters most",
     "Set location to Dubai, United Arab Emirates and turn on Open to Work for recruiters only. Gulf "
     "recruiters filter by location before they read anything, and a Mumbai location excludes you from "
     "the search entirely.",
     "This is a two-minute change that decides whether you are visible at all.",
     ("Now", "Location, headline with the target title, and the About section rewritten in numbers.")),

    ("Month-end close is the first question",
     "Not 'walk me through your CV' — 'walk me through your close'. They want the day count, what runs "
     "on which day, where it breaks, and what you did about it.",
     "A close you shortened from 10 days to 6 is a better answer than any certificate.",
     ("Prep", "Write your close calendar as a day-by-day list. Learn it.")),

    ("Saudi pays more. Consider it.",
     "Riyadh FP&A bands sit 15–25% above Dubai's for the same role, driven by Vision 2030 spending, and "
     "the tax position is the same. The trade is lifestyle and family logistics, not money.",
     "Also live: Qatar and Bahrain, both quieter and both hiring.",
     ("Widen", "Add Riyadh to the search. The Dubai-only filter is a self-imposed pay cut.")),

    ("The reference call decides it",
     "Gulf groups check references properly, and they call the person you name. A manager who says "
     "'he was good' loses you the band. A manager who says 'he cut our forecast error from 12% to 4%' "
     "wins you the counter.",
     "Brief your references. Give them the number you want said.",
     ("Do", "Pick two. Send each a three-line note on what the role needs.")),

    ("Attestation is the silent blocker",
     "Degree and CA certificate need MEA attestation in India, then UAE embassy attestation, then MOFA "
     "in the UAE. Three to six weeks if nothing goes wrong. Offers have been withdrawn over this.",
     "Start it before you have an offer. It expires slowly and costs little.",
     ("Cost", "Roughly ₹8–15K all-in per document, and the only thing it buys is not losing the job.")),

    ("Notice period is a negotiation, not a fact",
     "Gulf employers plan around a 30-day joiner. A 90-day Indian notice period kills live mandates. "
     "Buy-out clauses exist in most Indian contracts and employers routinely fund them.",
     "Say the real number early. Discovering it in week six loses the offer.",
     ("Ask", "Find out your buy-out amount now. It is a line in the contract.")),

    ("What a Financial Controller title actually needs",
     "The jump from FP&A Manager to Controller in the Gulf is statutory reporting plus audit ownership "
     "plus a team. If the CV has no signed statutory set on it, that is the gap — not the years.",
     "Controller bands start around AED 40K. It is the next rung, and it is close.",
     ("Gap", "Get a statutory reporting line on the CV in the next six months. Any entity, any size.")),

    ("Consolidation is the differentiator",
     "Multi-entity, multi-currency consolidation with intercompany elimination is what a Gulf group does "
     "every month and what most candidates have never touched. Naming the tool — HFM, OneStream, "
     "Tagetik, SAP BPC — moves you up a band on its own.",
     "IFRS 10, IFRS 3 and IAS 21 are the standards behind it. Know which does what.",
     ("Learn", "Take one 20-entity consolidation end to end, even as an exercise. Then say so.")),

    ("Do not send the same CV twice",
     "One master CV, then a version per pool: retail, logistics, energy, advisory. Same facts, different "
     "order, different top five bullets. It takes 20 minutes per version and roughly doubles response rate.",
     "The bullet at the top of the page is doing 80% of the work.",
     ("Rule", "If the top bullet does not name the sector's core problem, rewrite it.")),

    ("Interviews here are panels",
     "Expect three rounds: recruiter screen, hiring manager, then a panel with the CFO and a business "
     "head. The business head is the one to convince — they decide whether finance is useful or overhead.",
     "Answer the business head in their language, not in accounting standards.",
     ("Prep", "Have one story about a decision you changed with a number.")),

    ("The offer letter clauses to read twice",
     "Probation length (usually six months, terminable on 14 days), annual leave (30 calendar days is "
     "standard, 22 working days is not the same thing), flight allowance, and whether the visa is "
     "sponsored for family from day one or after probation.",
     "Family sponsorship after probation means six months of separation nobody mentioned.",
     ("Check", "Ask about family visa timing before you accept, in writing.")),

    ("Why applications stall at 60%",
     "Most Gulf portals reject on missing fields, not on merit. Passport number, visa status, notice "
     "period, current and expected salary. Leave one blank and the application never reaches a human.",
     "Expected salary blank is read as 'unclear', which is worse than a high number.",
     ("Fix", "Keep a text file with every field the portals ask for. Paste, do not retype.")),
]


def get_dubai_note() -> dict:
    """Today's Dubai Corner entry. Rotates daily on the same ordinal scheme as
    every other bank on this page, so the whole desk turns over together."""
    idx = (date.today().toordinal() + 3) % len(DUBAI_TRACK)
    title, body, targets, (act_label, action) = DUBAI_TRACK[idx]
    return {"title": title, "body": body, "targets": targets,
            "action_label": act_label, "action": action,
            "index": idx + 1, "total": len(DUBAI_TRACK)}

# ─────────────────────────────────────────────────────────────
# OBSIDIAN SYNC
# ─────────────────────────────────────────────────────────────

def sync_tracker_to_obsidian(stocks: list[dict]) -> bool:
    import base64
    token = os.environ.get("GITHUB_TOKEN", "")
    repo  = os.environ.get("OBSIDIAN_GITHUB_REPO", "caakshayk1-boop/obsidian-brain")
    if not token: return False
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    today   = date.today()
    path    = f"02-DAILY/{today.isoformat()}.md"
    api_url = f"https://api.github.com/repos/{repo}/contents/{path}"
    r = requests.get(api_url, headers=headers, timeout=10)
    if r.status_code == 200:
        data = r.json()
        content = base64.b64decode(data["content"]).decode()
        sha = data.get("sha")
    else:
        content = f"# {today.strftime('%B %d, %Y')}\n\n"
        sha = None
    section  = "\n\n## 📈 Stock Tracker\n\n"
    section += "| Symbol | Entry | Current | Target | P&L | Thesis |\n"
    section += "|--------|-------|---------|--------|-----|--------|\n"
    for s in stocks:
        pnl = f"{'▲' if s['winning'] else '▼'} {abs(s['pnl_pct']):.1f}%"
        section += f"| {s['symbol']} | {s['currency']}{s['entry_price']:.2f} | {s['currency']}{s['current_price']:.2f} | {s['currency']}{s['target_price']:.2f} | {pnl} | {str(s['thesis'])[:40]} |\n"
    anchor, end_anchor = "<!-- akk-stock-tracker -->", "<!-- /akk-stock-tracker -->"
    if anchor in content and end_anchor in content:
        s_idx = content.index(anchor); e_idx = content.index(end_anchor) + len(end_anchor)
        content = content[:s_idx] + anchor + section + end_anchor + content[e_idx:]
    else:
        content = content.rstrip() + "\n\n" + anchor + section + end_anchor + "\n"
    payload = {"message": f"newspaper: stock tracker {today.isoformat()}", "content": base64.b64encode(content.encode()).decode()}
    if sha: payload["sha"] = sha
    resp = requests.put(api_url, headers=headers, json=payload, timeout=15)
    ok = resp.status_code in (200, 201)
    log.info(f"Obsidian sync: {'OK' if ok else 'FAIL'}")
    return ok

# ─────────────────────────────────────────────────────────────
# HTML TEMPLATE
# ─────────────────────────────────────────────────────────────

TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<!-- Enforced from this point in the document, so it must precede every inline
     style and script. Strict because nothing loads from a third party any
     more: no 'unsafe-inline', no CDN, no font host. Each inline block carries
     the per-build nonce. -->
<meta http-equiv="Content-Security-Policy" content="{{ csp }}">
<!-- Applies the stored theme BEFORE first paint. Placed in <head>, inline and
     synchronous on purpose: deferring it by even one frame means the page
     paints in one theme and repaints in the other, which is the single most
     visible way a theme toggle can look broken.

     There is deliberately no work to do in the default case. Light is the
     default in CSS, on :root:not([data-theme]), so a first-time reader needs
     no JavaScript at all to get the right theme — and a reader with JS
     disabled or blocked gets it too. This script only re-applies an explicit
     choice the reader made on a previous visit. -->
<script nonce="{{ nonce }}">
(function(){try{
  var t=localStorage.getItem('aa-theme');
  if(t==='light'||t==='dark')document.documentElement.setAttribute('data-theme',t);
}catch(e){}})();
</script>
<meta name="theme-color" content="#F3F2EE" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#FBFAF7">
<meta name="color-scheme" content="light dark">

<!-- The title used to be "THE DAILY SIGNAL — {{ date_str }}", which handed
     search engines a brand-new ranking target every 24 hours and never
     accumulated authority for anything. The subject leads now; the date is a
     suffix that says the page is fresh without being the thing being ranked. -->
<title>{{ page_title }} · {{ date_str }}</title>
<meta name="description" content="{{ page_desc }}">
<link rel="canonical" href="https://news.askakshay.com{{ page_path }}">
<meta name="author" content="Akshay K Kothari">
<meta name="robots" content="index,follow,max-image-preview:large,max-snippet:-1">

<!-- Sharing produced a bare URL with no title, image or description, which
     removed every reason to paste the link anywhere. -->
<meta property="og:type" content="website">
<meta property="og:site_name" content="The Daily Signal">
<meta property="og:url" content="https://news.askakshay.com{{ page_path }}">
<meta property="og:title" content="{{ page_title }}">
<meta property="og:description" content="Every signal logged when it fires, scored when it closes. Wins and losses both, in public. Rebuilt 6 AM MYT.">
<meta property="og:image" content="https://news.askakshay.com/og.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="The Daily Signal — win rate, signals logged and open setups for {{ date_str }}">
<meta property="og:locale" content="en_IN">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{{ page_title }}">
<meta name="twitter:description" content="Every signal logged when it fires, scored when it closes. Wins and losses both, in public.">
<meta name="twitter:image" content="https://news.askakshay.com/og.png">

<!-- Inline SVG favicon: the lime dot from the masthead. No extra request, and
     /favicon.ico was 404ing on every single page load. -->
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%2308090A'/%3E%3Ccircle cx='16' cy='16' r='6' fill='%23B8EF43'/%3E%3C/svg%3E">
<link rel="apple-touch-icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%2308090A'/%3E%3Ccircle cx='16' cy='16' r='6' fill='%23B8EF43'/%3E%3C/svg%3E">
<link rel="manifest" href="/manifest.webmanifest">

<!-- This publishes a genuine public dataset; saying so is the honest schema. -->
<script type="application/ld+json" nonce="{{ nonce }}">{{ jsonld }}</script>

<!-- Self-hosted. Google Fonts was the ONLY third-party origin this page
     contacted; removing it means the site now truthfully loads nothing from
     anyone else, and it drops two DNS lookups plus a render-blocking
     stylesheet from the critical path. Latin subset, the five weights the
     stylesheet actually uses, 61 KB across eight files that cache
     independently of the daily HTML rebuild. -->
<!-- Preload only the two faces above the fold: the section headline serif and
     the mono that sets the ticker rail and every number in the hero. Both
     pointed at the deleted Cyrillic files until now, so both preloads 404'd
     — the browser opened two connections, got nothing, and then discovered
     the real faces later through CSS. Preloading a font you do not ship is
     strictly worse than not preloading at all. -->
<link rel="preload" href="/fonts/Newsreader-600-latin.woff2" as="font" type="font/woff2" crossorigin>
<link rel="preload" href="/fonts/JetBrainsMono-400-latin.woff2" as="font" type="font/woff2" crossorigin>
<style>
/* ══════════ TYPEFACES ══════════
   These eight files used to be CYRILLIC subsets. Every @font-face carried
   unicode-range U+0400-045F, so the browser was told to use Fira Sans and
   JetBrains Mono only for Cyrillic — on a site with no Cyrillic on it. Every
   Latin letter and every digit fell through to the system font, which means
   the site's typography was whatever the reader's OS happened to have, and it
   looked different on every machine. Confirmed against production by measuring
   `'Fira Sans', monospace` against plain `monospace` for "Numbers first 24078":
   identical, i.e. Fira Sans covered none of it. The three JetBrainsMono files
   were also byte-identical, so 500 and 700 were copies of 400.

   The Cyrillic files are gone rather than kept. Nothing on this site is
   Cyrillic, and eight files serving a script the page never renders is dead
   weight in the repo and one more thing to keep in sync.

   THE SUBSETS ARE SPLIT ON PURPOSE. unicode-range means the browser fetches a
   file only when the page actually contains a glyph from that range at that
   weight — so latin-ext (which exists here almost entirely to carry ₹, U+20B9,
   absent from the `latin` subset) costs nothing on a page that has no rupee
   figures at that weight.

   Newsreader is pinned at opsz 36. It is a variable font, and an unpinned
   request returns the entire 132KB variable file for EVERY weight asked for;
   the pinned static instance is 24KB. It is only ever used at display sizes,
   so one optical size is the right one. Regenerate with
   tools_fetch_fonts.py — the reasoning lives in that file's docstring. */
@font-face{font-family:'Onest';font-style:normal;font-weight:400;font-display:swap;src:url('/fonts/Onest-400-latin-ext.woff2') format('woff2');unicode-range:U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+0304, U+0308, U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C0, U+2113, U+2C60-2C7F, U+A720-A7FF}
@font-face{font-family:'Onest';font-style:normal;font-weight:400;font-display:swap;src:url('/fonts/Onest-400-latin.woff2') format('woff2');unicode-range:U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD}
@font-face{font-family:'Onest';font-style:normal;font-weight:500;font-display:swap;src:url('/fonts/Onest-500-latin-ext.woff2') format('woff2');unicode-range:U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+0304, U+0308, U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C0, U+2113, U+2C60-2C7F, U+A720-A7FF}
@font-face{font-family:'Onest';font-style:normal;font-weight:500;font-display:swap;src:url('/fonts/Onest-500-latin.woff2') format('woff2');unicode-range:U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD}
@font-face{font-family:'Onest';font-style:normal;font-weight:700;font-display:swap;src:url('/fonts/Onest-700-latin-ext.woff2') format('woff2');unicode-range:U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+0304, U+0308, U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C0, U+2113, U+2C60-2C7F, U+A720-A7FF}
@font-face{font-family:'Onest';font-style:normal;font-weight:700;font-display:swap;src:url('/fonts/Onest-700-latin.woff2') format('woff2');unicode-range:U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD}
@font-face{font-family:'Fira Sans';font-style:normal;font-weight:400;font-display:swap;src:url('/fonts/FiraSans-400-latin-ext.woff2') format('woff2');unicode-range:U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+0304, U+0308, U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C0, U+2113, U+2C60-2C7F, U+A720-A7FF}
@font-face{font-family:'Fira Sans';font-style:normal;font-weight:400;font-display:swap;src:url('/fonts/FiraSans-400-latin.woff2') format('woff2');unicode-range:U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD}
@font-face{font-family:'Fira Sans';font-style:normal;font-weight:500;font-display:swap;src:url('/fonts/FiraSans-500-latin-ext.woff2') format('woff2');unicode-range:U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+0304, U+0308, U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C0, U+2113, U+2C60-2C7F, U+A720-A7FF}
@font-face{font-family:'Fira Sans';font-style:normal;font-weight:500;font-display:swap;src:url('/fonts/FiraSans-500-latin.woff2') format('woff2');unicode-range:U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD}
@font-face{font-family:'Fira Sans';font-style:normal;font-weight:600;font-display:swap;src:url('/fonts/FiraSans-600-latin-ext.woff2') format('woff2');unicode-range:U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+0304, U+0308, U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C0, U+2113, U+2C60-2C7F, U+A720-A7FF}
@font-face{font-family:'Fira Sans';font-style:normal;font-weight:600;font-display:swap;src:url('/fonts/FiraSans-600-latin.woff2') format('woff2');unicode-range:U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD}
@font-face{font-family:'Fira Sans';font-style:normal;font-weight:700;font-display:swap;src:url('/fonts/FiraSans-700-latin-ext.woff2') format('woff2');unicode-range:U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+0304, U+0308, U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C0, U+2113, U+2C60-2C7F, U+A720-A7FF}
@font-face{font-family:'Fira Sans';font-style:normal;font-weight:700;font-display:swap;src:url('/fonts/FiraSans-700-latin.woff2') format('woff2');unicode-range:U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD}
@font-face{font-family:'Fira Sans';font-style:normal;font-weight:800;font-display:swap;src:url('/fonts/FiraSans-800-latin-ext.woff2') format('woff2');unicode-range:U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+0304, U+0308, U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C0, U+2113, U+2C60-2C7F, U+A720-A7FF}
@font-face{font-family:'Fira Sans';font-style:normal;font-weight:800;font-display:swap;src:url('/fonts/FiraSans-800-latin.woff2') format('woff2');unicode-range:U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD}
@font-face{font-family:'JetBrains Mono';font-style:normal;font-weight:400;font-display:swap;src:url('/fonts/JetBrainsMono-400-latin-ext.woff2') format('woff2');unicode-range:U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+0304, U+0308, U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C0, U+2113, U+2C60-2C7F, U+A720-A7FF}
@font-face{font-family:'JetBrains Mono';font-style:normal;font-weight:400;font-display:swap;src:url('/fonts/JetBrainsMono-400-latin.woff2') format('woff2');unicode-range:U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD}
@font-face{font-family:'JetBrains Mono';font-style:normal;font-weight:500;font-display:swap;src:url('/fonts/JetBrainsMono-500-latin-ext.woff2') format('woff2');unicode-range:U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+0304, U+0308, U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C0, U+2113, U+2C60-2C7F, U+A720-A7FF}
@font-face{font-family:'JetBrains Mono';font-style:normal;font-weight:500;font-display:swap;src:url('/fonts/JetBrainsMono-500-latin.woff2') format('woff2');unicode-range:U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD}
@font-face{font-family:'JetBrains Mono';font-style:normal;font-weight:700;font-display:swap;src:url('/fonts/JetBrainsMono-700-latin-ext.woff2') format('woff2');unicode-range:U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+0304, U+0308, U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C0, U+2113, U+2C60-2C7F, U+A720-A7FF}
@font-face{font-family:'JetBrains Mono';font-style:normal;font-weight:700;font-display:swap;src:url('/fonts/JetBrainsMono-700-latin.woff2') format('woff2');unicode-range:U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD}
@font-face{font-family:'Newsreader';font-style:italic;font-weight:400;font-display:swap;src:url('/fonts/Newsreader-400i-latin-ext.woff2') format('woff2');unicode-range:U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+0304, U+0308, U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C0, U+2113, U+2C60-2C7F, U+A720-A7FF}
@font-face{font-family:'Newsreader';font-style:italic;font-weight:400;font-display:swap;src:url('/fonts/Newsreader-400i-latin.woff2') format('woff2');unicode-range:U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD}
@font-face{font-family:'Newsreader';font-style:normal;font-weight:400;font-display:swap;src:url('/fonts/Newsreader-400-latin-ext.woff2') format('woff2');unicode-range:U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+0304, U+0308, U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C0, U+2113, U+2C60-2C7F, U+A720-A7FF}
@font-face{font-family:'Newsreader';font-style:normal;font-weight:400;font-display:swap;src:url('/fonts/Newsreader-400-latin.woff2') format('woff2');unicode-range:U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD}
@font-face{font-family:'Newsreader';font-style:normal;font-weight:600;font-display:swap;src:url('/fonts/Newsreader-600-latin-ext.woff2') format('woff2');unicode-range:U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+0304, U+0308, U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C0, U+2113, U+2C60-2C7F, U+A720-A7FF}
@font-face{font-family:'Newsreader';font-style:normal;font-weight:600;font-display:swap;src:url('/fonts/Newsreader-600-latin.woff2') format('woff2');unicode-range:U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD}

</style>
<style>
/* ═══════════════════ TOKENS ═══════════════════ */
:root{
  /* ── AskAkshay design tokens ────────────────────────────────────────────
     ONE source for surface, ink, accent, type and spacing. Every component
     on the site already reads these names, which is why the whole visual
     system can be rebuilt here rather than across 5,200 lines of CSS.

     Two themes, both DESIGNED. Light is not an inversion of dark: inverted
     dark themes read as washed-out grey documents, because a palette tuned
     for glowing text on a dark ground has the wrong contrast curve on paper.
     Each theme sets its own values for the same token names.

     Colour NEVER carries meaning alone — every status also has a word and a
     glyph. See the status system in the component layer. */

  /* Surfaces, layered. The old palette had two (--bg, --surface) which meant
     a card inside a panel inside a modal all rendered at the same depth and
     the eye had nothing to climb. Five steps, each a small tonal lift. */
  --bg:#0A0B0E;            /* page ground */
  --bg2:#0E1013;           /* section band */
  --surface:#131519;       /* card */
  --surface2:#181B20;      /* card, raised */
  --surface3:#1E2228;      /* elevated / hover */
  --overlay:#22262D;       /* modal, drawer, palette */

  /* Hairlines. Two weights only — a border scale wider than this turns into
     visual noise on a data-dense page. */
  --line:rgba(255,255,255,.07);
  --line2:rgba(255,255,255,.14);

  /* Ink. Measured against --bg with full alpha compositing:
     --text 16.1:1 · --muted 7.4:1 · --dim 4.9:1 — all clear of WCAG AA, and
     --dim clears it on the ~35 places it carries timestamps and table meta. */
  --text:#F2F3F5;
  --muted:#9BA3AE;
  --dim:#79818C;

  /* Accent. The lime stays — it IS the identity, and a rebrand that discards
     the one memorable thing about a product is a redesign, not a rebrand.
     Everything around it is what changed. */
  --lime:#C2F04A;
  /* What text goes ON the brand fill. Bright lime wants black; the light
     theme's navy wants white. A literal here is how a button becomes
     unreadable the day the brand colour changes — which is exactly what
     happened. */
  --on-brand:#000;
  --lime-soft:rgba(194,240,74,.10);
  --lime-line:rgba(194,240,74,.34);

  /* Semantic. Financial meaning only — never decoration, never a button. */
  /* Hero orbs. Tokenised because a translucent colour glow is an ADDITIVE
     device: on a dark ground it reads as light, on a warm paper ground the
     same fill is darker than the page and reads as a smudge. Light mode
     gets its own values rather than inheriting these. */
  --orb-a:rgba(184,239,67,.11);
  --orb-b:rgba(106,168,255,.07);

  /* Surfaces that were hardcoded hex before the light theme existed and
     stayed hardcoded after it. Each one is a near-black: on the paper
     ground they rendered a dark card under dark text, which is how the
     Trade Ideas grid came to be unreadable in light mode while every
     token-driven surface beside it was fine. */
  --pick-edge:#0E0F12;        /* far end of the pick-card gradient */
  /* Ink printed ON a --up fill. The fill itself flips from a bright mint
     (dark) to a deep forest (light), so a single hardcoded ink is legible
     against exactly one of them. */
  --on-up:#06251A;
  --rank-ink:rgba(255,255,255,.035);  /* the ghosted 01..05 numeral */
  --scroll-thumb:#232529;

  /* ── COLOUR CONTRACT ────────────────────────────────────────────────────
     Each colour means ONE thing. Overlapping meanings is how a page ends up
     with a freshness badge and a rising price wearing the same green — which
     is exactly what this file did before the contract was written down.

       --up      positive market movement · winning outcome · nothing else
       --down    negative movement · loss · failed state
       --gold    warning · degraded · unresolved · needs attention
       --blue    information · freshness · neutral status · active selection
       --violet  machine-generated text, so it can never pass as measured data
       --lime    BRAND ONLY — wordmark, section numbers, editorial accent.
                 Never a status: a brand colour that also means "good" cannot
                 be used for emphasis without implying a judgement.
       --muted
       --dim     metadata · secondary · historical

     Before giving a component a colour, find its meaning in that list. If the
     meaning is not there, the component needs a different one, not a new hue.
     ─────────────────────────────────────────────────────────────────────── */
  --up:#3DDC97;   --up-soft:rgba(61,220,151,.12);
  --down:#FF6B6B; --down-soft:rgba(255,107,107,.12);
  --gold:#E8C547; --gold-soft:rgba(232,197,71,.12);
  --blue:#6AA8FF; --violet:#A78BFA;

  /* Type. Thirty distinct hardcoded sizes existed before this scale; every
     one of them was a decision nobody made. Ratio ~1.16, tuned so adjacent
     steps are distinguishable without shouting. */
  --t-display:clamp(30px,4.6vw,46px);
  --t-h1:clamp(24px,3.2vw,34px);
  --t-h2:clamp(19px,2.2vw,24px);
  --t-h3:17px;
  --t-h4:15px;
  --t-body-lg:15.5px;
  --t-body:14.5px;
  --t-body-sm:13px;
  --t-caption:12px;
  --t-label:11px;
  --t-overline:10px;
  /* Numbers get their own ramp — a price and a paragraph should never share
     a size by accident. */
  --t-data-lg:clamp(22px,2.6vw,30px);
  --t-data:15px;
  --t-data-sm:12.5px;

  /* Tables. Two densities, and both of them are now a DECISION. Before this,
     five table dialects in this file rendered at three different sizes
     (11px / 12px / 13px) that nobody had chosen — they were whatever the
     rule that happened to win the cascade said. A six-column volume board
     and an eighteen-column screener genuinely do not want the same size, so
     the scale names the two cases instead of pretending one fits both.

     Size is the ONLY thing a table is allowed to vary. Family, weight, case,
     tracking, colour, rules, padding and numeric alignment are shared by
     every table on the page — those were the parts that were drifting, and
     drift in those is what reads as amateur. */
  --t-table:13px;         /* default — up to ~8 columns */
  --t-table-dense:12px;   /* the wide screener, the wallet, the alert log */
  --t-table-h:11px;       /* every column header, at both densities */
  --tbl-pad-y:12px;       /* body cell, vertical */
  --tbl-pad-y-h:11px;     /* header cell, vertical */
  --tbl-pad-x:14px;       /* both, horizontal */
  --tbl-track:.12em;      /* header tracking — was 1.4px here, .13em there */

  /* Spacing. One scale, used everywhere. */
  --s1:4px;  --s2:8px;  --s3:12px; --s4:16px; --s5:20px;
  --s6:24px; --s7:32px; --s8:40px; --s9:48px; --s10:64px; --s11:80px;

  /* Reading measure. Editorial text never spans a dashboard's full width. */
  --measure:68ch;

  /* FOUR ROLES, and nothing outside them.
     A page where every metric looks equally important is a page with no
     hierarchy, which is the single loudest complaint about this site.

       --disp   Onest, tight-tracked. Headlines and the numbers that ARE the
                point. Variable width axis, so a headline can be set tight
                without faking it with letter-spacing.
       --serif  Newsreader. The editorial voice — section titles and pull
                quotes. It is what makes this read as a publication rather
                than a dashboard, so it is kept rather than replaced.
       --sans   Onest. Running text. Warmer and rounder than Fira Sans at the
                same size, which matters on a white ground where a neutral
                grotesk goes cold.
       --mono   JetBrains Mono. Data, and only data. If it is in mono it is a
                measurement. */
  /* RETIRED AS A SEPARATE FACE, 2026-08-27. Measured on production: Bricolage
     was on 80 elements and Newsreader on 49, against Onest's 3,081 and
     JetBrains Mono's 4,592. Two whole families — eight woff2 files — for 129
     elements between them, on a page whose brief is "WSJ or a Bloomberg
     terminal, not too many fonts".
     --disp stays as a TOKEN because 16 call sites use it and they are all
     correct about their intent: big, tight-tracked, data-carrying headline
     numbers. It now resolves to Onest, which is a grotesque and does that job.
     Three families, each with one job: serif argues, sans speaks, mono
     measures. That is the WSJ split; four was one face pretending. */
  --disp:'Onest','Fira Sans',-apple-system,BlinkMacSystemFont,sans-serif;
  --mono:'JetBrains Mono',ui-monospace,monospace;
  --sans:'Onest','Fira Sans',-apple-system,BlinkMacSystemFont,sans-serif;
  /* Editorial display face. Newsreader carries the headlines and nothing
     else — never a number, never a label, never a table. The split is the
     point: a serif says "this is an argument", the mono says "this is a
     measurement", and a page that uses one face for both reads as either a
     blog or a terminal rather than a research desk.
     Georgia is the fallback because it ships everywhere and its x-height
     is close enough that a swap does not reflow the headline. */
  --serif:'Newsreader',Georgia,'Times New Roman',serif;
  --ease:cubic-bezier(.22,1,.36,1);
  --gut:clamp(16px,4vw,40px);

  /* Motion. Named so a component cannot invent its own timing. */
  --m-micro:130ms;
  --m-std:200ms;
  --m-panel:280ms;

  color-scheme:dark;
}

/* ── DEFAULT: LIGHT ───────────────────────────────────────────────────────
   The palette on :root above is the DARK one and it stays there, because it is
   what `[data-theme="dark"]` needs to fall back to. This block makes light the
   theme a reader gets when they have expressed no preference.

   :root:not([data-theme]) outranks bare :root on specificity, so:
     · no stored choice          -> this block wins            -> light
     · reader picked light       -> [data-theme="light"] wins  -> light
     · reader picked dark        -> :not() stops matching,
                                    bare :root applies         -> dark

   No values are duplicated by hand — they are the same measured tokens as the
   explicit light theme below, which is the only place they are tuned. System
   preference no longer decides this: the site is light, and dark is a choice
   the reader makes and keeps. */
:root:not([data-theme]){
  /* RAG PAPER, NOT A LIGHTBOX — and not cream either.
     The previous ground was a warm off-white with pure-white cards: two
     near-whites, so the page was one bright field with nothing separating a
     card from the sheet it sits on, and at that brightness the field is a lamp
     pointed at the reader. The ground now drops to a cool rag paper with a
     blue-grey bias and the cards come UP to a near-white that is still not
     white, so elevation reads as a step in value rather than as a border.
     The bias is cool on purpose: warm cream under dense red/green financial
     tables muddies both, and a slate-tinted sheet lets the semantic colours
     sit on it cleanly. */
  --bg:#F3F2EE;
  --bg2:#EAE9E4;
  --surface:#FAF9F6;
  --surface2:#EFEEE9;
  --surface3:#E2E1DB;
  --overlay:#FAF9F6;

  /* Rules, not edges. A broadsheet separates things with hairlines and space;
     it does not put a rounded box round every idea. These are darker than the
     borders they replace because a hairline has to be seen to be doing the job
     a card outline used to do. */
  --line:rgba(17,17,17,.16);
  --line2:rgba(17,17,17,.30);

  /* Ink on paper, blue-black rather than neutral so it belongs to the ground.
     Measured against --bg2, the darkest sheet these three regularly
     print on — a contrast figure quoted against pure white is a figure that is
     never true where the text actually sits.
     Computed: 13.6:1 / 7.4:1 / 4.9:1. --dim is metadata only, never body. */
  --text:#111111;
  --muted:#3A3A38;
  --dim:#5C5C58;

  /* THE MARK. Was an olive lime chosen to survive a dark ground; on rag paper
     it read as tired. Deep ink-teal instead — far enough from --up green that
     the wordmark is never mistaken for a P&L colour, which is the whole reason
     a brand hue and a semantic hue have to be different colours.
     #0B5C63 computes 6.3:1 on --bg2. */
  /* One accent, used for links and the wordmark. Everything else that used to
     carry a hue now carries weight, size or a rule instead. */
  --lime:#123E6E;
  --on-brand:#fff;
  --lime-soft:rgba(18,62,110,.07);
  --lime-line:rgba(18,62,110,.24);

  /* PILLAR HUES. Six sections of the paper, six colours — carried by the nav
     button, the section number and the rule beside a heading. This is the
     "colourful" part of the design and it is doing a job: the hue tells you
     which part of the paper you are standing in, so colour is navigation
     rather than decoration. None of them is used for a value; values only
     ever get --up/--down/--gold. */
  /* Pulled back hard. These were six saturated hues painting every eyebrow and
     nav chip, which on a page carrying real semantic colour — gains, losses,
     provenance — meant colour had four different jobs and therefore none.
     Colour now means ONE thing: where a number came from, and whether it went
     up or down. The pillars keep an identity, at the weight of a pencil mark. */
  --p-today:#6B4A2A;
  --p-markets:#2A4A6B;
  --p-research:#4A3A6B;
  --p-portfolio:#2A5450;
  --p-ledger:#6B2A3A;
  --p-about:#4A4A48;
  /* The Life page runs its own five pillars off the same idea. */
  --p-career:#6B4A2A;
  --p-learning:#2A4A6B;
  --p-practice:#2A5450;
  --p-drills:#4A3A6B;
  --p-mind:#6B2A3A;

  --orb-a:rgba(18,62,110,.04);
  --orb-b:rgba(17,17,17,.03);
  --pick-edge:#FAF9F6;
  --on-up:#FAF9F6;
  --rank-ink:rgba(17,17,17,.05);
  --scroll-thumb:#CFCEC8;
  --up:#0A6B45;   --up-soft:rgba(10,107,69,.12);
  --down:#B4231A; --down-soft:rgba(180,35,26,.11);
  --gold:#8A6A00; --gold-soft:rgba(138,106,0,.13);
  --blue:#1A4FB0; --violet:#6438B8;

  color-scheme:light;
}

/* ── LIGHT ────────────────────────────────────────────────────────────────
   Designed, not inverted. A warm paper ground rather than #fff: pure white
   against dense financial tables is fatiguing, and the warmth is what makes
   this read as an editorial research terminal rather than a spreadsheet.
   Borders do more work here and shadows do less, which is the opposite of
   the dark theme — on paper, elevation reads through edges. */
:root[data-theme="light"]{
  /* RAG PAPER, NOT A LIGHTBOX — and not cream either.
     The previous ground was a warm off-white with pure-white cards: two
     near-whites, so the page was one bright field with nothing separating a
     card from the sheet it sits on, and at that brightness the field is a lamp
     pointed at the reader. The ground now drops to a cool rag paper with a
     blue-grey bias and the cards come UP to a near-white that is still not
     white, so elevation reads as a step in value rather than as a border.
     The bias is cool on purpose: warm cream under dense red/green financial
     tables muddies both, and a slate-tinted sheet lets the semantic colours
     sit on it cleanly. */
  --bg:#F3F2EE;
  --bg2:#EAE9E4;
  --surface:#FAF9F6;
  --surface2:#EFEEE9;
  --surface3:#E2E1DB;
  --overlay:#FAF9F6;

  /* Rules, not edges. A broadsheet separates things with hairlines and space;
     it does not put a rounded box round every idea. These are darker than the
     borders they replace because a hairline has to be seen to be doing the job
     a card outline used to do. */
  --line:rgba(17,17,17,.16);
  --line2:rgba(17,17,17,.30);

  /* Ink on paper, blue-black rather than neutral so it belongs to the ground.
     Measured against --bg2, the darkest sheet these three regularly
     print on — a contrast figure quoted against pure white is a figure that is
     never true where the text actually sits.
     Computed: 13.6:1 / 7.4:1 / 4.9:1. --dim is metadata only, never body. */
  --text:#111111;
  --muted:#3A3A38;
  --dim:#5C5C58;

  /* THE MARK. Was an olive lime chosen to survive a dark ground; on rag paper
     it read as tired. Deep ink-teal instead — far enough from --up green that
     the wordmark is never mistaken for a P&L colour, which is the whole reason
     a brand hue and a semantic hue have to be different colours.
     #0B5C63 computes 6.3:1 on --bg2. */
  /* One accent, used for links and the wordmark. Everything else that used to
     carry a hue now carries weight, size or a rule instead. */
  --lime:#123E6E;
  --on-brand:#fff;
  --lime-soft:rgba(18,62,110,.07);
  --lime-line:rgba(18,62,110,.24);

  /* PILLAR HUES. Six sections of the paper, six colours — carried by the nav
     button, the section number and the rule beside a heading. This is the
     "colourful" part of the design and it is doing a job: the hue tells you
     which part of the paper you are standing in, so colour is navigation
     rather than decoration. None of them is used for a value; values only
     ever get --up/--down/--gold. */
  /* Pulled back hard. These were six saturated hues painting every eyebrow and
     nav chip, which on a page carrying real semantic colour — gains, losses,
     provenance — meant colour had four different jobs and therefore none.
     Colour now means ONE thing: where a number came from, and whether it went
     up or down. The pillars keep an identity, at the weight of a pencil mark. */
  --p-today:#6B4A2A;
  --p-markets:#2A4A6B;
  --p-research:#4A3A6B;
  --p-portfolio:#2A5450;
  --p-ledger:#6B2A3A;
  --p-about:#4A4A48;
  /* The Life page runs its own five pillars off the same idea. */
  --p-career:#6B4A2A;
  --p-learning:#2A4A6B;
  --p-practice:#2A5450;
  --p-drills:#4A3A6B;
  --p-mind:#6B2A3A;

  --orb-a:rgba(18,62,110,.04);
  --orb-b:rgba(17,17,17,.03);
  --pick-edge:#FAF9F6;
  --on-up:#FAF9F6;
  --rank-ink:rgba(17,17,17,.05);
  --scroll-thumb:#CFCEC8;
  --up:#0A6B45;   --up-soft:rgba(10,107,69,.12);
  --down:#B4231A; --down-soft:rgba(180,35,26,.11);
  --gold:#8A6A00; --gold-soft:rgba(138,106,0,.13);
  --blue:#1A4FB0; --violet:#6438B8;

  color-scheme:light;
}

/* No prefers-color-scheme block. Light is the default for every reader
   without an explicit choice, so a media query repeating the same palette
   would only be a third place for it to drift out of step. Dark remains a
   deliberate pick via the toggle, which stamps data-theme="dark". */

/* ── THE MANDATE'S ORDER BOOK ─────────────────────────────────────────────
   Every colour and size below is a token, so this block follows the theme
   without a second set of light-mode rules. Built as rows rather than cards:
   a book is read down a column and compared line to line, which a card grid
   actively prevents. */
.mandate{
  border:1px solid var(--lime-line);
  background:var(--lime-soft);
  border-radius:14px;
  padding:clamp(14px,2vw,20px);
  margin:0 0 28px;
}
.mandate-head{
  display:flex; flex-wrap:wrap; gap:10px 18px;
  align-items:baseline; justify-content:space-between;
  padding-bottom:12px; margin-bottom:12px;
  border-bottom:1px solid var(--line2);
  font:500 var(--t-body-sm)/1.5 var(--sans); color:var(--muted);
}
.mandate-head b{ color:var(--text); font-family:var(--mono); }
.mandate-state{ display:flex; flex-wrap:wrap; gap:6px 16px;
  font:500 var(--t-caption)/1.4 var(--mono); color:var(--dim); }
.mandate-state i{ font-style:normal; color:var(--text); font-weight:600; }
/* Set apart from the four figures beside it because it is not a figure — it
   says why the figure a reader is looking for is not in this row. */
.mandate-state .mandate-pnl{ color:var(--dim); }
.mandate-state .mandate-pnl a{ color:var(--lime); border-bottom:1px solid transparent; }
.mandate-state .mandate-pnl a:hover{ border-bottom-color:var(--lime); }

.mandate-rows{ display:flex; flex-direction:column; gap:2px; }
.mrow{
  background:var(--surface);
  border:1px solid var(--line);
  border-radius:10px;
  padding:11px 13px;
  transition:border-color var(--m-micro) var(--ease),
             transform var(--m-micro) var(--ease);
}
/* Motion is a nudge, not a performance. 2px and 130ms is enough to say the row
   is a unit; anything larger and a list of eight becomes a wave. */
.mrow:hover{ border-color:var(--lime-line); transform:translateX(2px); }
@media (prefers-reduced-motion:reduce){ .mrow{ transition:none } .mrow:hover{ transform:none } }

.mrow-top{ display:flex; flex-wrap:wrap; align-items:baseline; gap:6px 10px; }
.msym{ font:700 var(--t-body)/1.2 var(--sans); color:var(--text); letter-spacing:-.01em; }
.mhz{ font:600 var(--t-overline)/1 var(--sans); text-transform:uppercase;
  letter-spacing:.08em; color:var(--lime);
  background:var(--lime-soft); border:1px solid var(--lime-line);
  border-radius:4px; padding:3px 6px; }
.meng{ font:400 var(--t-caption)/1 var(--mono); color:var(--dim); }
.mrr{ margin-left:auto; font:600 var(--t-data-sm)/1 var(--mono); color:var(--muted); }
.mgain{ font:700 var(--t-data-sm)/1 var(--mono); color:var(--up);
  background:var(--up-soft); border-radius:4px; padding:3px 6px; }

.mrow-nums{ display:flex; flex-wrap:wrap; gap:4px 14px; margin-top:7px;
  font:400 var(--t-caption)/1.5 var(--mono); color:var(--muted); }
.mrow-nums b{ color:var(--text); font-weight:600; }
.mstop b{ color:var(--down); }

.mladder{ display:flex; flex-wrap:wrap; gap:5px; margin-top:9px; }
.mleg{
  font:400 var(--t-caption)/1 var(--mono); color:var(--muted);
  background:var(--surface2); border:1px solid var(--line);
  border-radius:5px; padding:4px 7px; white-space:nowrap;
}
.mleg i{ font-style:normal; font-weight:700; color:var(--text); }
.mleg em{ font-style:normal; color:var(--up); }
.mtrail{ margin-top:7px; font:400 var(--t-caption)/1.5 var(--sans); color:var(--dim); }

.mandate-empty{ font:400 var(--t-body-sm)/1.6 var(--sans); color:var(--muted); margin:4px 0; }
.mandate-foot{
  display:flex; flex-wrap:wrap; gap:4px 14px; margin-top:12px; padding-top:10px;
  border-top:1px solid var(--line);
  font:400 var(--t-caption)/1.5 var(--sans); color:var(--dim);
}
.mandate-foot span:not(:last-child)::after{ content:'\00b7'; margin-left:14px; opacity:.5 }

@media (max-width:640px){
  .mrr{ margin-left:0 }
  .mladder{ flex-direction:column; align-items:flex-start }
  .mleg{ width:100% }
}

/* ── MAGAZINE LAYER ───────────────────────────────────────────────────────
   The palette above made the page white. This makes it read like a magazine
   rather than a dashboard that happens to be light.

   Three moves, in order of how much work they do:

   1. SPACE. A dark UI holds together on contrast; a white page holds together
      on whitespace. Sections breathe roughly 40% harder, and the measure is
      capped so running text never crosses ~68 characters.

   2. RULES, NOT BOXES. On dark, a card reads as a card because it is lighter
      than its ground. On white there is nowhere lighter to go, so the same
      card needs a border on every side and the page turns into a grid of
      rectangles. Cards lose their frames and keep a single hairline above —
      elevation reads through the edge, the way it does in print.

   3. TYPE CARRIES THE HIERARCHY. Headings get tighter tracking and more size
      contrast against the body, so a reader can find the shape of the page
      without a single box being drawn.

   Scoped to the light themes only. The dark theme was designed around
   contrast and shadow and is left exactly as it was. */
/* THE SECTION CARD. Authoritative, because these theme-scoped selectors
   compute to (0,3,0) and out-rank anything set on `main section.sec` at
   (0,1,2) — the fourth time in this file that a rule written elsewhere lost
   silently to this block. The rule for this codebase: if a property is set
   HERE, change it HERE. Do not add specificity to beat it.

   Card padding, not broadsheet rhythm: 104px of air inside a bounded white
   card is not generous, it is a card that looks empty. */
:root:not([data-theme]) .sec,
:root[data-theme="light"] .sec{
  padding-block:clamp(18px,2.6vw,34px);
  padding-inline:clamp(14px,2.2vw,30px);
  background:var(--surface);
  border:1px solid var(--line);
  border-radius:8px;
  margin-bottom:clamp(12px,1.6vw,18px);
}
:root:not([data-theme]) .shead,
:root[data-theme="light"] .shead{
  margin-bottom:clamp(26px,3.4vw,46px);
}
:root:not([data-theme]) .stitle,
:root[data-theme="light"] .stitle{
  letter-spacing:-.032em;
  line-height:1.02;
}
:root:not([data-theme]) .sdesc,
:root[data-theme="light"] .sdesc{
  max-width:64ch;
  color:var(--muted);
}
/* NO CARDS. A broadsheet groups with a rule and a column of air, and this page
   was drawing twenty-three outlined, shadowed boxes to say "these belong
   together" — which a hairline says more quietly and without adding
   twenty-three competing rectangles to a page that already carries a great
   deal of structure.

   These are the authoritative light-theme rules and they out-specify anything
   set on a bare `.card`, which is why the treatment is rewritten HERE rather
   than layered on top: two card systems fighting on specificity is how the
   last one ended up invisible. */
:root:not([data-theme]) .card,
:root[data-theme="light"] .card{
  background:transparent;
  border:0;
  border-top:1px solid var(--line);
  border-radius:0;
  box-shadow:none;
  padding:18px 0 20px;
}
:root:not([data-theme]) .card:hover,
:root[data-theme="light"] .card:hover{
  border-color:var(--line);
  box-shadow:none;
}
/* A table IS its own container on paper. Rules top and bottom, nothing round
   the outside, and the row rules carry the rest. */
:root:not([data-theme]) .tblwrap,
:root[data-theme="light"] .tblwrap{
  background:transparent;
  border:0;
  border-top:1px solid var(--line2);
  border-bottom:1px solid var(--line2);
  border-radius:0;
}
/* SCROLLS, and did not before.
   .tblwrap has no base rule anywhere in this file — its only declaration was
   the theme rule above, which carried `overflow:hidden`. So a table wider than
   the screen was being CLIPPED: the columns past the edge were unreachable at
   any viewport, on every device, silently.

   Removing that hidden in the broadsheet rewrite turned a silent clip into a
   500px document overflow at 320px, which is how it was finally caught — and
   only because the check was run with content-visibility disabled as a
   control. With sections skipped the overflow does not reach the document and
   the page measures clean.

   auto, not hidden: a wide table should be reachable, not amputated.
   overscroll-behavior-x stops a sideways flick inside a table from triggering
   the browser's back gesture, which on iOS navigates away mid-read. */
.tblwrap{
  overflow-x:auto;
  overscroll-behavior-x:contain;
}
/* Tables lose their outer box for the same reason and keep their row rules. */
:root:not([data-theme]) table,
:root[data-theme="light"] table{
  border:0;
}
:root:not([data-theme]) th,
:root[data-theme="light"] th{
  border-bottom:1px solid var(--line2);
  color:var(--dim);
  font-weight:600;
}
:root:not([data-theme]) td,
:root[data-theme="light"] td{
  border-bottom:1px solid var(--line);
}
/* The eyebrow becomes a rule-and-label instead of a chip. */
/* This rule outranks the base .snum on specificity, so it has to carry the
   pillar hue too — it silently repainted every section eyebrow grey and made
   the whole colour scheme invisible below the nav. Fallback stays --dim for
   any .snum outside a mapped section. */
:root:not([data-theme]) .snum,
:root[data-theme="light"] .snum{
  color:var(--pillar,var(--dim));
  letter-spacing:.16em;
}
/* ── PROVENANCE LEGEND ────────────────────────────────────────────────────
   The single most useful thing this site can do that a screener cannot: say
   where every number came from. Four kinds sit on the same page and they are
   not the same kind of claim.

     FACT     an observed value. A close, a flow, a filing. Wrong only if the
              source is wrong.
     MODEL    computed by this engine from facts. A score, an expectancy, a
              regime reading. Wrong if the method is wrong.
     RESULT   what actually happened to a published signal. Wins and losses
              both, never edited after the fact.
     VIEW     a human opinion. Rare on purpose, and always labelled.

   Rendered as a small badge, and explained once in a legend strip so the
   badges do not need a caption each. Colour is never the only carrier — each
   badge is a word first.

   This also does regulatory work. A page that mixes an observed close with a
   model output and an opinion, all in the same weight, is implicitly claiming
   they are the same kind of statement. They are not. */
.prov-legend{
  display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;
  padding:12px 0;margin:0 0 20px;border-top:1px solid var(--line);
  border-bottom:1px solid var(--line);
}
.prov-legend .pl-lead{
  font:600 11px/1 var(--mono);letter-spacing:.16em;text-transform:uppercase;color:var(--dim);
}
.prov-legend .pl-item{display:flex;align-items:center;gap:7px;font:400 12px/1.4 var(--sans);color:var(--muted)}
.pill{
  font:600 11px/1 var(--mono);letter-spacing:.11em;text-transform:uppercase;
  padding:4px 7px;border-radius:3px;border:1px solid;white-space:nowrap;flex:none;
}
.pill-fact  {color:var(--p-markets); border-color:color-mix(in srgb, var(--p-markets) 32%, transparent); background:color-mix(in srgb, var(--p-markets) 7%, transparent)}
.pill-model {color:var(--p-research); border-color:color-mix(in srgb, var(--p-research) 34%, transparent); background:color-mix(in srgb, var(--p-research) 7%, transparent)}
.pill-result{color:var(--up);   border-color:color-mix(in srgb, var(--up) 34%, transparent);   background:var(--up-soft)}
.pill-view  {color:var(--gold); border-color:color-mix(in srgb, var(--gold) 34%, transparent); background:var(--gold-soft)}

/* ── FOUR LEVELS OF EMPHASIS ──────────────────────────────────────────────
   Primary is what matters now. Secondary is the evidence under it. Tertiary
   is context. System is metadata — freshness, method, provenance — and it is
   deliberately the quietest thing on the page. */
.lv-1{font:800 clamp(26px,3.4vw,40px)/1.05 var(--disp);letter-spacing:-.03em;color:var(--text)}
.lv-2{font:600 clamp(16px,1.8vw,19px)/1.35 var(--disp);letter-spacing:-.02em;color:var(--text)}
.lv-3{font:400 14px/1.65 var(--sans);color:var(--muted);max-width:64ch}
.lv-sys{font:400 11px/1.5 var(--mono);color:var(--dim);letter-spacing:.02em}

/* ── WHEN IT WORKS / WHAT IF ──────────────────────────────────────────────
   Both blocks are built from tokens only. The heat scale runs through --up and
   --down at varying alpha rather than a rainbow: the question is "did this day
   make or lose money", which is one axis, and a two-colour scale answers it
   without asking the reader to learn a legend. */
.whenwrap,.whatifwrap{margin-top:clamp(28px,3.4vw,44px)}
.whenhead{margin-bottom:16px}
.whentitle{font-size:clamp(19px,2.2vw,25px);letter-spacing:-.025em;margin-bottom:6px}
.whensub{color:var(--muted);font-size:14px;max-width:62ch;margin:0}
.whengrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(96px,1fr));gap:6px;margin-bottom:10px}
.whenmonths{grid-template-columns:repeat(auto-fit,minmax(78px,1fr))}
.whencell{border:1px solid var(--line);border-radius:6px;padding:10px 11px;min-height:74px;
  display:flex;flex-direction:column;justify-content:space-between}
.whencell .wk{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--dim)}
.whencell .wv{font-family:var(--mono);font-size:16px;font-weight:600;font-variant-numeric:tabular-nums}
.whencell .wn{font-size:11px;color:var(--dim);font-family:var(--mono)}
.whencell.empty-cell .wv{color:var(--dim)}
.whennote{color:var(--dim);font-size:13px;max-width:64ch;margin-top:4px}
.whatifrow{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:16px}
.whatifchip{font-family:var(--mono);font-size:12px;padding:6px 11px;border-radius:99px;
  border:1px solid var(--line2);background:none;color:var(--muted);cursor:pointer;
  transition:background .15s,color .15s,border-color .15s,opacity .15s}
.whatifchip:hover{color:var(--text)}
.whatifchip[aria-pressed="true"]{opacity:.42;text-decoration:line-through}
.whatifout{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(150px,100%),1fr));gap:12px}
.whatifout .wo{border-top:1px solid var(--line);padding-top:11px}
.whatifout .wo .k{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--dim);margin-bottom:5px}
.whatifout .wo .v{font-family:var(--mono);font-size:19px;font-weight:600;font-variant-numeric:tabular-nums}
.whatifout .wo .d{font-size:11px;color:var(--dim);font-family:var(--mono);margin-top:4px}

/* The decorative column grid nearly disappears.
   .vgrid paints 1px verticals at --line. On the dark ground that was a faint
   texture you had to look for; the same alpha as dark-on-light is markedly
   stronger, and a white page ruled into columns reads as a spreadsheet rather
   than a magazine. Kept rather than removed — it still aligns the eye — at
   roughly a quarter of the weight. */
:root:not([data-theme]) .vgrid,
:root[data-theme="light"] .vgrid{
  opacity:.28;
}

/* Numerals line up in columns on a page that is mostly numbers. */
:root:not([data-theme]) table,
:root:not([data-theme]) .num,
:root[data-theme="light"] table,
:root[data-theme="light"] .num{
  font-variant-numeric:tabular-nums;
}

/* Surfaces and ink cross-fade on a theme switch; nothing else does, or the
   whole page appears to move. */
body,.card,.sec,header,footer,.nav,.topbar{
  transition:background-color var(--m-std) var(--ease),
             border-color var(--m-std) var(--ease),
             color var(--m-std) var(--ease);
}
@media (prefers-reduced-motion: reduce){
  *,*::before,*::after{
    animation-duration:.01ms!important; animation-iteration-count:1!important;
    transition-duration:.01ms!important; scroll-behavior:auto!important;
  }
}

/* Financial numbers must align vertically down a column. Without this a
   table of prices wobbles digit by digit and stops being scannable. */
.num,.mono-dim,table.t td,table.t th,[class*="kpi"] .v,.dh-age{
  font-variant-numeric:tabular-nums;
  font-feature-settings:"tnum" 1;
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth;scroll-padding-top:var(--headh,200px);-webkit-text-size-adjust:100%}
/* overflow-x:clip, never hidden. `hidden` turns <body> into a scroll container,
   which silently scopes every position:sticky to it instead of the viewport —
   that is what stopped the header and nav from staying put on scroll. `clip`
   contains the same overflow without creating that container. */
body{background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px;
  line-height:1.6;font-weight:400;overflow-x:clip;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
.up{color:var(--up)} .dn{color:var(--down)}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums}
::selection{background:var(--lime);color:var(--on-brand)}
::-webkit-scrollbar{width:9px;height:9px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--scroll-thumb);border-radius:9px}
::-webkit-scrollbar-thumb:hover{background:#33363c}

/* ═══════════════════ TEXTURE + CHROME ═══════════════════ */
.grain{position:fixed;inset:0;z-index:999;pointer-events:none;opacity:.04;mix-blend-mode:overlay;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='180' height='180'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='3'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");}
.vgrid{position:fixed;inset:0;z-index:0;pointer-events:none;max-width:1400px;margin:0 auto;
  background-image:linear-gradient(90deg,var(--line) 1px,transparent 1px);
  background-size:calc(100% / 6) 100%;opacity:.55;}
@media(max-width:900px){.vgrid{background-size:50% 100%;}}
.progress{position:fixed;top:0;left:0;height:2px;width:0;background:var(--lime);z-index:1000;
  box-shadow:0 0 12px var(--lime);transition:width .1s linear;}

/* ═══════════════════ HEADER ═══════════════════ */
/* One sticky container for the whole header. Previously .topbar, .nav and
   .livebar were each position:sticky — .nav and .livebar both at top:60px, so
   once scrolled they landed on the same 60 pixels and the status bar covered
   the section menu. Sticking the wrapper instead means the three stack in
   normal flow and no magic offsets can drift apart. */
.headstack{position:sticky;top:0;z-index:300;background:var(--bg)}
.topbar{background:var(--bg);border-bottom:1px solid var(--line);}
.topbar-in{max-width:1400px;margin:0 auto;padding:0 var(--gut);height:60px;
  display:flex;align-items:center;justify-content:space-between;gap:18px;}
.brand{display:flex;align-items:baseline;gap:9px;font-weight:700;font-size:16px;letter-spacing:-.4px;white-space:nowrap;}
.brand b{color:var(--lime);font-weight:800}
.brand .dot{width:6px;height:6px;border-radius:50%;background:var(--lime);align-self:center;
  animation:pulse 2.4s var(--ease) infinite;}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(.7)}}
.stamp{display:flex;align-items:center;gap:14px;font-family:var(--mono);font-size:11px;
  color:var(--muted);letter-spacing:.4px;text-transform:uppercase;}
.stamp .live{color:var(--lime);display:inline-flex;align-items:center;gap:6px}
.stamp .live i{width:5px;height:5px;border-radius:50%;background:var(--lime);animation:pulse 1.8s infinite}
@media(max-width:720px){.stamp span.d{display:none}}

/* ═══════════════════ NAV ═══════════════════ */
.nav{background:var(--bg);
  backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);border-bottom:1px solid var(--line);}
.nav-in{max-width:1400px;margin:0 auto;padding:0 var(--gut);display:flex;gap:2px;
  overflow-x:auto;scrollbar-width:none;}
.nav-in::-webkit-scrollbar{display:none}
.nav a{position:relative;padding:11px 13px;font-size:11px;font-weight:500;letter-spacing:1.1px;
  text-transform:uppercase;color:var(--dim);white-space:nowrap;transition:color .25s var(--ease);}
.nav a i{font-style:normal;font-family:var(--mono);font-size:11px;color:#33363c;margin-right:5px;transition:color .25s}
.nav a::after{content:'';position:absolute;left:13px;right:13px;bottom:0;height:2px;background:var(--lime);
  transform:scaleX(0);transform-origin:left;transition:transform .35s var(--ease);}
/* Group label. Not a link and not focusable — it names the run of links after
   it so eleven equal-weight items read as five decisions instead of eleven.
   aria-hidden on the element, with the group folded into each link's
   aria-label, because a screen reader hitting a bare orphan word between
   links learns nothing from it. */
.nav-g{display:flex;align-items:center;padding:11px 11px 11px 16px;font-family:var(--mono);
  font-size:11px;font-weight:600;letter-spacing:1.6px;text-transform:uppercase;
  color:#4A4F57;white-space:nowrap;border-left:1px solid var(--line);}
.nav-g:first-child{border-left:none;padding-left:0}
.nav a:hover{color:var(--text)}
.nav a.on{color:var(--lime)}
.nav a.on i{color:var(--lime)}
.nav a.on::after{transform:scaleX(1)}

/* ═══════════════════ HERO ═══════════════════ */
.hero{position:relative;max-width:1400px;margin:0 auto;padding:clamp(48px,9vw,110px) var(--gut) clamp(30px,5vw,56px);
  overflow:hidden;z-index:2;}
.orb{position:absolute;border-radius:50%;filter:blur(90px);pointer-events:none;z-index:-1}
.orb.a{width:min(46vw,520px);aspect-ratio:1;background:var(--orb-a);top:-14%;right:-8%;animation:drift 22s ease-in-out infinite;}
.orb.b{width:min(34vw,380px);aspect-ratio:1;background:var(--orb-b);bottom:-20%;left:-6%;animation:drift 28s ease-in-out infinite reverse;}
@keyframes drift{0%,100%{transform:translate(0,0)}50%{transform:translate(-6%,7%)}}
.eyebrow{display:inline-flex;align-items:center;gap:10px;font-family:var(--mono);font-size:11px;
  letter-spacing:2.4px;text-transform:uppercase;color:var(--lime);border:1px solid var(--lime-line);
  background:var(--lime-soft);padding:6px 13px;border-radius:100px;margin-bottom:26px;}
/* Display type is the serif; everything measured stays sans or mono.
   Tracking is -1.4px rather than the -3px this was set at as a sans: a serif's
   serifs already close the gaps between letters, so the same negative tracking
   that tightens Fira Sans collides Newsreader. Weight is 600 because 600 is the
   heaviest face actually shipped — asking for 800 gets a synthesised bold,
   which is a smeared outline rather than a heavier cut. */
h1.hl{font-family:var(--serif);font-size:clamp(40px,8.2vw,94px);line-height:.98;
  font-weight:600;letter-spacing:-1.4px;max-width:15ch;margin-bottom:22px;}
h1.hl .w{display:inline-block;overflow:hidden;vertical-align:top}
h1.hl .w>span{display:inline-block;transform:translateY(105%);opacity:0;
  animation:rise .9s var(--ease) forwards;animation-delay:var(--d,0s);}
@keyframes rise{to{transform:translateY(0);opacity:1}}
/* Restored to a real italic. It was forced upright because the old stack had
   no italic cut to fall back on; Newsreader ships one, and the contrast
   between roman and italic is the whole reason to set a headline in a serif. */
h1.hl em{font-style:italic;font-weight:400;color:var(--lime)}
.hero-sub{font-size:clamp(15px,1.7vw,19px);color:var(--muted);max-width:52ch;line-height:1.6;
  opacity:0;animation:fadeUp .8s var(--ease) .7s forwards;}
@keyframes fadeUp{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:none}}

/* hero stat rail */
.statrail{display:flex;flex-wrap:wrap;gap:0;margin-top:clamp(32px,5vw,54px);
  border-top:1px solid var(--line);opacity:0;animation:fadeUp .8s var(--ease) .9s forwards;}
.stat{flex:1 1 150px;padding:20px 22px 20px 0;border-right:1px solid var(--line);}
.stat:last-child{border-right:none}
.stat .v{font-family:var(--mono);font-size:clamp(26px,3.4vw,40px);font-weight:700;letter-spacing:-1.5px;line-height:1;}
.stat .k{font-size:11px;letter-spacing:1.8px;text-transform:uppercase;color:var(--dim);margin-top:9px;font-weight:500;}
/* The sample a headline rate rests on, carried by the rate itself. */
.stat .kn{font-family:var(--mono);font-size:11px;color:var(--dim);margin-top:5px;letter-spacing:.3px}
@media(max-width:640px){.stat{flex:1 1 44%;padding:16px 14px 16px 0}}

/* ═══════════════════ TODAY IN 60 SECONDS ═══════════════════ */
.brief{margin-top:clamp(26px,4vw,40px);border:1px solid var(--line);border-radius:10px;
  background:var(--card,rgba(255,255,255,.015));overflow:hidden}
.brief-h{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;
  padding:13px 18px;border-bottom:1px solid var(--line)}
.brief-t{font-family:var(--mono);font-size:11px;font-weight:700;letter-spacing:2px;
  text-transform:uppercase;color:var(--lime)}
.brief-d{font-family:var(--mono);font-size:11px;color:var(--dim);letter-spacing:.4px}

.regime{padding:15px 18px;border-bottom:1px solid var(--line)}
.rg-l{display:flex;justify-content:space-between;align-items:baseline;gap:12px}
.rg-k{font-size:11px;letter-spacing:1.6px;text-transform:uppercase;color:var(--dim);font-weight:500}
.rg-v{font-family:var(--mono);font-size:14px;font-weight:700;letter-spacing:-.3px;color:var(--muted)}
.rg-v.up{color:var(--lime)} .rg-v.dn{color:var(--down)} .rg-v.flat{color:var(--gold)}
.rg-v i{font-style:normal;font-size:11px;color:var(--dim);font-weight:500}
/* The meter. The tick at 50 is the whole point — a bar with no neutral mark
   cannot show which side of neutral the reading is on. */
.rg-bar{position:relative;height:5px;border-radius:3px;background:rgba(255,255,255,.06);margin:10px 0 9px}
.rg-bar i{position:absolute;inset:0 auto 0 0;width:var(--rg);border-radius:3px;
  background:linear-gradient(90deg,var(--down),var(--gold) 50%,var(--lime));}
.rg-bar b{position:absolute;left:50%;top:-3px;bottom:-3px;width:1px;background:var(--dim);opacity:.7}
.rg-why{font-size:12px;line-height:1.65;color:var(--muted)}
.rg-why b{font-weight:600} .rg-why b.up{color:var(--up)} .rg-why b.dn{color:var(--down)}
.rg-why b.warn{display:block;margin-top:5px;color:var(--gold);font-weight:500}

.brief-l{list-style:none;margin:0;padding:4px 0}
.brief-l li{display:flex;gap:12px;padding:10px 18px;font-size:13px;line-height:1.6;color:var(--muted)}
.brief-l li + li{border-top:1px solid rgba(255,255,255,.04)}
.brief-l .bn{font-family:var(--mono);font-size:11px;color:#4A4F57;padding-top:3px;flex:0 0 auto}
.brief-l b{color:var(--text);font-weight:600}
.brief-l .up{color:var(--up)} .brief-l .dn{color:var(--down)}
.brief-l a{color:var(--lime);white-space:nowrap;border-bottom:1px solid rgba(184,239,67,.28)}
.brief-l a:hover{border-bottom-color:var(--lime)}
@media(max-width:640px){
  .brief-l li{padding:10px 14px;font-size:13px}
  .brief-h,.regime{padding-left:14px;padding-right:14px}
}

/* ══════════ WHAT MATTERS NOW ══════════
   The interpretation layer, and the only block above the fold allowed to make
   a claim. It lives INSIDE .brief's border on purpose: a bordered grid of
   bordered cards inside a bordered panel is three frames around one thought,
   which is the "excessive rounded cards" failure the rebuild brief names by
   name. The cards are separated by newspaper column rules instead — one
   hairline between readings, no box around any of them.

   Semantic colour appears once per card, on a 5px dot. Never on the heading,
   never as a fill: --down on a heading would make "Gold rose" read as a loss.
   Every card also carries its tag as a WORD, so the colour is never the only
   thing distinguishing a risk reading from an opportunity. */
.matters{padding:2px 0 0}
.matters-h{display:flex;justify-content:space-between;align-items:baseline;gap:var(--s3);
  flex-wrap:wrap;padding:var(--s3) var(--s5) var(--s2)}
.matters-t{font-family:var(--mono);font-size:var(--t-overline);font-weight:700;
  letter-spacing:2px;text-transform:uppercase;color:var(--text)}
.matters-n{font-family:var(--mono);font-size:var(--t-overline);color:var(--dim);
  letter-spacing:.4px}

/* auto-fit rather than a fixed column count: what_matters() returns one to
   five cards depending on what the day earned, and a fixed 5-up grid would
   leave dead columns on a quiet morning. */
/* Separators are box-shadows, not borders. auto-fit resolves to a different
   column count at every width AND with every card count, so any rule written
   as "border-left except nth-child(odd)" is correct at exactly one of those
   and wrong at the rest — it assumed two columns and got three at 900px.
   A left+top shadow on every cell draws the full lattice whatever the count;
   overflow:hidden on the grid clips the outermost two, and the grid's own
   border-top replaces the row of top shadows it just clipped. Shadows take no
   layout space, so this costs nothing in alignment. */
.matters-g{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(228px,100%),1fr));
  border-top:1px solid var(--line);overflow:hidden}

.mcard{position:relative;display:flex;flex-direction:column;
  padding:var(--s4) var(--s5) var(--s5);
  box-shadow:-1px 0 0 var(--line),0 -1px 0 var(--line);
  transition:background var(--m-micro) var(--ease)}
.mcard:hover{background:var(--surface)}

.mc-tag{display:flex;align-items:center;gap:6px;font-family:var(--mono);
  font-size:var(--t-overline);font-weight:700;letter-spacing:1.6px;
  text-transform:uppercase;color:var(--mc,var(--muted));margin-bottom:var(--s3)}
.mc-tag i{width:5px;height:5px;border-radius:50%;background:var(--mc,var(--muted));
  flex:0 0 auto}

.mc-head{font-size:var(--t-h4);font-weight:600;line-height:1.35;color:var(--text);
  letter-spacing:-.1px;margin:0 0 var(--s2);font-variant-numeric:tabular-nums}
.mc-why{font-size:var(--t-body-sm);line-height:1.6;color:var(--muted);margin:0 0 var(--s3)}

/* ── THE DECISION CARD'S TWO NEW PARTS ────────────────────────────────────
   A card used to be a heading, a paragraph and a link — an observation. The
   two pieces below are what make it a decision: what KIND of claim it is, and
   what would make it wrong.

   The basis chip is pushed to the far end of the tag row rather than sitting
   beside the tag, because it answers a different question. "Risk" says which
   reading this is; "FACT" says how much weight it carries. Adjacent, they read
   as one compound label. */
.mc-basis{margin-left:auto;font-size:9px;letter-spacing:1.3px;padding:2px 6px;
  border-radius:3px;border:1px solid currentColor;opacity:.85;font-weight:700}
/* Three grades, three hues, none of them the semantic up/down pair — a
   provenance chip must never be mistakable for a gain or a loss. FACT borrows
   the information blue, MODEL the machine violet already reserved for
   generated content, RESULT the gold that means "measured, and it cost
   something to measure". */
.mb-fact{color:var(--blue)}
.mb-model{color:var(--violet)}
.mb-result{color:var(--gold)}

/* The falsifier. Set quieter than the reading it qualifies but NOT hidden:
   the whole point is that a reader sees the failure condition at the same
   moment they see the claim. Left rule rather than a box — it is an aside to
   the paragraph above it, not a separate component. */
.mc-unless{font-size:var(--t-caption);line-height:1.55;color:var(--dim);
  margin:0 0 var(--s3);padding-left:9px;border-left:2px solid var(--line2)}
.mc-unless span{font-family:var(--mono);font-size:var(--t-overline);
  letter-spacing:1.2px;text-transform:uppercase;color:var(--mc,var(--muted));
  margin-right:6px}

/* margin-top:auto pins every CTA to the bottom of its column. Without it the
   links sit directly under their own paragraph, so a card whose reading wraps
   to three lines drops its link 20px below its neighbours' — four "→" at four
   different heights, which is the detail that makes a grid look assembled
   rather than designed. */
.mc-cta{margin-top:auto;align-self:flex-start;font-family:var(--mono);font-size:var(--t-overline);
  letter-spacing:1.2px;text-transform:uppercase;color:var(--dim);
  border-bottom:1px solid transparent;transition:color var(--m-micro) var(--ease),
  border-color var(--m-micro) var(--ease)}
.mcard:hover .mc-cta,.mc-cta:hover{color:var(--mc,var(--lime));
  border-bottom-color:currentColor}

.mc-risk{--mc:var(--down)}
.mc-momentum{--mc:var(--up)}
.mc-watch{--mc:var(--gold)}
.mc-opportunity{--mc:var(--lime)}
.mc-record{--mc:var(--blue)}

/* Below 900px auto-fit lands on two columns, so the left rule that separated
   columns now also has to separate ROWS — without this, cards 3 and 4 sit
   flush against 1 and 2 with no rule between them. */
@media(max-width:640px){
  .matters-h{padding-left:var(--s4);padding-right:var(--s4)}
  .mcard{padding:var(--s4)}
}


/* ═══════════════════ ACCESSIBILITY BASELINE ═══════════════════ */
/* 166 focusable elements shipped with no focus styling at all — a keyboard
   user tabbing the signal log had no idea where they were. WCAG 2.4.7 / 2.4.11. */
:focus-visible{outline:2px solid var(--lime);outline-offset:2px;border-radius:3px}
/* The sticky header must not cover whatever just received focus (2.4.11). */
a,button,input,select,textarea,[tabindex]{scroll-margin-top:190px}
/* Section anchors clear the sticky stack too, or clicking a nav item lands
   with the heading hidden behind the header it just scrolled past. */
main section.sec{scroll-margin-top:calc(var(--headh,200px) + 12px)}

/* WCAG 2.2 SC 2.5.8 — 24x24 minimum. Symbol links in the tables measured
   23x17; a bare checkbox measured 13x13. Sizing the hit area, not the text. */
.sym,.slink,td a,.fbtn,.tab{min-height:24px;min-width:24px;display:inline-flex;
  align-items:center;justify-content:flex-start}
/* Short tickers were still narrow targets even with a height floor. The pad is
   on the inline box so the hit area grows without moving the text. */
td a.sym,.crate-l a{padding-block:4px}
.wm-legend .dot,.trk .pl{pointer-events:none}
td a.sym{padding:2px 0}
input[type=checkbox],input[type=radio]{min-width:24px;min-height:24px;accent-color:var(--lime)}
.btn-gh,.gym-btn,.tickctl,.livebar button{min-height:24px}

/* Skip link — WCAG 2.4.1. Eighteen sections is a lot of tab stops to wade
   through before reaching content. */
.skip{position:absolute;left:-9999px;top:0;z-index:999;
  background:var(--lime);color:var(--on-brand);font-family:var(--mono);font-size:13px;
  font-weight:700;padding:12px 20px;border-radius:0 0 8px 0}
.skip:focus{left:0}

.nav-other{margin-left:auto;color:var(--lime)!important;border-left:1px solid var(--line);
  padding-left:18px!important}
.nav-other:hover{background:var(--lime-soft)}
@media(max-width:900px){.nav-other{margin-left:0}}

/* ═══════════════════ COMMAND PALETTE ═══════════════════ */
.cmdk{position:fixed;inset:0;z-index:500;display:flex;align-items:flex-start;
  justify-content:center;padding:12vh 20px 20px}
.cmdk[hidden]{display:none}
.cmdk-bd{position:absolute;inset:0;background:rgba(4,5,6,.72);
  backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px);animation:cmdkFade .16s ease}
@keyframes cmdkFade{from{opacity:0}to{opacity:1}}
.cmdk-box{position:relative;width:min(620px,100%);background:var(--surface);
  border:1px solid var(--line2);border-radius:16px;overflow:hidden;
  box-shadow:0 30px 80px rgba(0,0,0,.6);animation:cmdkIn .18s var(--ease)}
@keyframes cmdkIn{from{opacity:0;transform:translateY(-10px) scale(.985)}to{opacity:1;transform:none}}
.cmdk-box input{width:100%;background:none;border:none;border-bottom:1px solid var(--line);
  padding:18px 20px;color:var(--text);font-family:var(--mono);font-size:14px;outline:none}
.cmdk-box input::placeholder{color:var(--dim)}
.cmdk-list{list-style:none;margin:0;padding:6px;max-height:46vh;overflow-y:auto}
.cmdk-list li{display:flex;align-items:center;gap:11px;padding:10px 14px;border-radius:9px;
  cursor:pointer;min-height:24px}
.cmdk-list li[aria-selected="true"]{background:var(--lime-soft)}
.cmdk-list li .k{font-family:var(--mono);font-size:11px;letter-spacing:1.2px;text-transform:uppercase;
  color:var(--dim);border:1px solid var(--line);border-radius:4px;padding:2px 6px;flex:none}
.cmdk-list li .t{flex:1;font-size:14px;color:var(--text)}
.cmdk-list li .m{font-family:var(--mono);font-size:11px;color:var(--dim)}
.cmdk-list li[aria-selected="true"] .t{color:var(--lime)}
.cmdk-empty{padding:22px;text-align:center;color:var(--dim);font-family:var(--mono);font-size:12px}
.cmdk-ft{display:flex;gap:16px;padding:10px 18px;border-top:1px solid var(--line);
  font-family:var(--mono);font-size:11px;color:var(--dim)}
.cmdk-ft kbd{background:var(--bg2);border:1px solid var(--line);border-radius:4px;
  padding:1px 5px;margin-right:4px}
.cmdk-hint{display:inline-flex;align-items:center;gap:5px;font-family:var(--mono);font-size:11px;
  color:var(--dim);border:1px solid var(--line);border-radius:99px;padding:4px 10px;
  cursor:pointer;min-height:24px}
.cmdk-hint:hover{color:var(--lime);border-color:var(--lime-line)}
@media(max-width:640px){.cmdk{padding:8vh 12px 12px}.cmdk-ft{display:none}}

/* ═══════════════════ TICKER ═══════════════════ */
.tickwrap{position:relative;border-top:1px solid var(--line);border-bottom:1px solid var(--line);
  background:var(--bg2);overflow:hidden;z-index:2;}
.tickwrap::before,.tickwrap::after{content:'';position:absolute;top:0;bottom:0;width:90px;z-index:3;pointer-events:none}
.tickwrap::before{left:0;background:linear-gradient(90deg,var(--bg2),transparent)}
.tickwrap::after{right:0;background:linear-gradient(270deg,var(--bg2),transparent)}
/* The rail carries ~110 instruments in eleven labelled segments, so the loop
   is set by --tickdur (written by the client from the item count) rather than
   fixed — 46s across that many items is unreadable. */
.tick{display:flex;width:max-content;animation:marquee var(--tickdur,46s) linear infinite;}
.tickwrap:hover .tick,.tickwrap.hold .tick{animation-play-state:paused}
@keyframes marquee{from{transform:translateX(0)}to{transform:translateX(-50%)}}
/* Reduced motion: the rail stops moving and becomes a scrollable strip, so
   the prices stay reachable instead of disappearing with the animation. */
@media(prefers-reduced-motion:reduce){
  .tick{animation:none;width:auto}
  .tickwrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
  .tickwrap::before,.tickwrap::after{display:none}
}
/* Compact inside the sticky stack — topbar + nav + livebar + rail is already
   four rows before any content, and on a laptop that is a lot of chrome. */
.ti{display:flex;align-items:center;gap:9px;padding:8px 20px;border-right:1px solid var(--line);white-space:nowrap;}
.ti .n{font-size:11px;letter-spacing:1.4px;text-transform:uppercase;color:var(--dim);font-weight:500}
.ti .p{font-family:var(--mono);font-size:13px;font-weight:600}
.ti .c{font-family:var(--mono);font-size:12px;font-weight:700}
.ti .note{font-family:var(--mono);font-size:11px;color:var(--gold);letter-spacing:.5px}

/* Segment head — the thing that makes a 110-item rail readable instead of
   an undifferentiated stream of numbers. */
.tseg{display:flex;align-items:center;gap:8px;padding:8px 18px 8px 20px;white-space:nowrap;
  border-right:1px solid var(--line);background:
    linear-gradient(90deg,color-mix(in srgb,var(--sc,var(--lime)) 16%,transparent),transparent);}
.tseg .ic{font-size:13px;line-height:1}
.tseg .lb{font-family:var(--mono);font-size:11px;font-weight:700;letter-spacing:2px;
  text-transform:uppercase;color:var(--sc,var(--lime))}
.tseg::before{content:'';width:3px;height:15px;border-radius:2px;background:var(--sc,var(--lime))}
.ti.hi .p{color:var(--sc,inherit)}

/* The rail is the only market surface on the page now, so it gets a control:
   the whole strip is a live region a reader can freeze to actually read it. */
.tickctl{position:absolute;right:8px;top:50%;transform:translateY(-50%);z-index:4;
  font-family:var(--mono);font-size:11px;letter-spacing:1px;text-transform:uppercase;
  background:var(--bg2);color:var(--dim);border:1px solid var(--line);border-radius:99px;
  padding:4px 10px;cursor:pointer}
.tickctl:hover{color:var(--lime);border-color:var(--lime)}
/* The pause control stays at every width. It used to be hidden below 640px,
   which removed the only way to stop an infinite marquee on exactly the
   devices where that matters most — WCAG 2.2.2, Level A. */
@media(max-width:640px){.tickctl{padding:5px 9px;font-size:11px;right:6px}}
/* Phone: the rail STAYS. Hiding it was the wrong trade — on a phone the
   ticker is the single most-wanted row on the page, and "the numbers are a
   scroll away" is exactly the scroll a market rail exists to save.
   The header pays for it instead: the topbar shrinks, and the livebar (a long
   diagnostic string that ellipsises to nothing useful at this width anyway)
   drops out. Net sticky height is lower than before AND the prices are there. */
/* ── CHROME BUDGET ──────────────────────────────────────────────────────────
   Measured on production, 1280x720: topbar 61 + trust 32.4 + nav 46.4 +
   livebar 42.6 + ticker 38.8 = 221.2px of a 720px viewport. Thirty-one
   percent of the screen, permanently, before one number is read — and the
   whole point of this page is the numbers.

   The scroll-collapse that fixes it already existed. It was written inside
   @media(max-width:560px), so it ran only on phones and never on the desktop
   where the stack is tallest. That is the second time a change landed behind
   a phone-only breakpoint and read as "nothing happened".

   What survives a scroll-down is what a terminal keeps on screen: the PRICES
   and the way back to a section. The masthead, the trust strip and the
   livebar diagnostic are all orientation — read once on arrival, dead weight
   for the next twenty minutes. 221px -> 85px, and scrolling up restores
   everything. */
.headstack .topbar,.headstack .trust,.headstack .livebar{
  transition:margin-top .2s var(--ease),opacity .16s linear}
.headstack.compact .topbar{margin-top:-62px}
.headstack.compact .trust,
.headstack.compact .livebar{opacity:0;pointer-events:none;
  height:0;margin:0;padding:0;overflow:hidden;border:0}
/* The nav stays but loses its padding — a terminal keeps its section keys. */
.headstack.compact .nav{transition:height .2s var(--ease)}
.headstack.compact .nav .nav-in{padding-top:2px;padding-bottom:2px}
@media(prefers-reduced-motion:reduce){
  .headstack .topbar,.headstack .trust,.headstack .livebar,
  .headstack.compact .nav{transition:none}
}

@media(max-width:560px){
  .headstack .tickwrap{display:block}
  .topbar-in{height:48px}
  .brand{font-size:14px}
  .stamp .d{display:none}          /* the date is in the hero eyebrow already */
  .ti{padding:7px 14px;gap:7px}
  .ti .n{font-size:11px;letter-spacing:1px}
  .ti .p{font-size:12px}
  .ti .c{font-size:11px}
  .tseg{padding:7px 12px 7px 14px}
  .tseg .lb{font-size:11px;letter-spacing:1.4px}

  /* Scrolling DOWN collapses the chrome and keeps only the prices pinned;
     scrolling UP brings the whole header back. Standard mobile pattern, and it
     is what makes a four-row sticky stack affordable on a 390px screen:
     ~146px of chrome while reading becomes ~30px of ticker. */
  /* Phone keeps the harsher version: the nav goes too. At 390px a nav row is
     a horizontal scroller nobody scrolls mid-read, and the FAB already opens
     the section list. Desktop keeps its nav because there it is one click. */
  .headstack .nav{transition:margin-top .22s var(--ease),opacity .18s linear}
  .headstack.compact .topbar{margin-top:-48px}
  .headstack.compact .nav{opacity:0;pointer-events:none;
    margin-top:0;height:0;overflow:hidden;border:0}
}
@media(prefers-reduced-motion:reduce){
  .headstack .nav{transition:none}
}

/* ═══════════════════ WORLD MAP ═══════════════════ */
.wmap-wrap{margin:0 0 30px;background:var(--surface);border:1px solid var(--line);
  border-radius:16px;overflow:hidden}
.wmap-head{display:flex;justify-content:space-between;align-items:center;gap:12px;
  flex-wrap:wrap;padding:12px 16px;border-bottom:1px solid var(--line)}
.wm-t{font-family:var(--mono);font-size:11px;letter-spacing:2px;text-transform:uppercase;
  color:var(--dim)}
.wm-legend{display:flex;align-items:center;gap:14px;font-family:var(--mono);font-size:11px;
  letter-spacing:.6px;color:var(--dim)}
.wm-legend .dot{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:5px;
  vertical-align:middle}
.wm-legend .dot.red{background:var(--down)}
.wm-legend .dot.green{background:var(--up)}
.wm-legend .dot.blue{background:#3d6ea8}
.wmap{position:relative;background:
  radial-gradient(ellipse at 50% 40%,rgba(61,110,168,.10),transparent 70%)}
.wmap{position:relative}
.wmap canvas{display:block;width:100%;height:auto}
/* The SVG sits exactly on top of the canvas and only carries event bubbles. */
.wmap svg{position:absolute;inset:0;width:100%;height:100%}
/* Baseline landmass — the "blue map stays the same across the globe" layer. */
.wmap rect.land{fill:#31537d;opacity:.55}
.wmap circle.ev{cursor:pointer}
.wmap circle.ev.red{fill:var(--down)}
.wmap circle.ev.green{fill:var(--up)}
.wmap circle.ev.blue{fill:#6ea8ff}
.wmap circle.halo{fill:none;stroke-width:.6}
.wmap circle.halo.red{stroke:var(--down);animation:wmPulse 2.6s ease-out infinite}
.wmap circle.halo.green{stroke:var(--up);animation:wmPulse 3.4s ease-out infinite}
@keyframes wmPulse{0%{r:1.5;opacity:.9}100%{r:6;opacity:0}}
.wm-tip{position:absolute;z-index:5;max-width:290px;background:var(--bg2);
  border:1px solid var(--line2);border-radius:10px;padding:10px 12px;pointer-events:none;
  box-shadow:0 10px 30px rgba(0,0,0,.5)}
.wm-tip .c{font-family:var(--mono);font-size:11px;letter-spacing:1.4px;text-transform:uppercase;
  color:var(--lime);margin-bottom:5px}
.wm-tip .h{font-size:12px;line-height:1.45;color:var(--text)}
.wm-tip .m{font-family:var(--mono);font-size:11px;color:var(--dim);margin-top:6px}
.wm-foot{padding:10px 16px;border-top:1px solid var(--line);font-family:var(--mono);
  font-size:11px;letter-spacing:.5px;color:var(--dim)}
@media(prefers-reduced-motion:reduce){.wmap circle.halo{animation:none;opacity:.25}}
@media(max-width:640px){.wm-legend{font-size:11px;gap:9px}}

.ncard .tone{font-family:var(--mono);font-size:11px;letter-spacing:1px;text-transform:uppercase;
  padding:2px 7px;border-radius:99px;margin-left:8px;vertical-align:middle}
.ncard .tone.red{background:rgba(255,92,92,.16);color:var(--down)}
.ncard .tone.green{background:rgba(60,220,130,.14);color:var(--up)}

/* Every auto-fit/auto-fill grid below uses minmax(min(Npx,100%),1fr) rather
   than minmax(Npx,1fr). A bare pixel floor is a floor the track cannot go
   under, so on a 320px phone — where about 288px is actually available inside
   the gutters — a 340px card simply hung off the side and dragged the whole
   document with it. Measured: the page came out 356px wide at a 320 viewport,
   and the IPO cards were the widest of ten grids doing it.

   min(Npx,100%) makes the floor "whichever is smaller, the design width or the
   space there is", which is what was meant in the first place. It needs no
   media query and cannot be got wrong again by adding an eleventh grid. */
.ltgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(300px,100%),1fr));gap:16px}
.lt{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:20px;
  transition:border-color .35s,transform .35s var(--ease)}
.lt:hover{transform:translateY(-3px);border-color:var(--line2)}
.lt-h{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.lt .sec-l{font-family:var(--mono);font-size:11px;color:var(--dim);letter-spacing:1px;
  text-transform:uppercase;margin-top:3px}
.lt .px{font-family:var(--mono);font-size:24px;font-weight:700;letter-spacing:-.6px;margin:12px 0 8px}
.lt .th{font-size:13px;line-height:1.55;color:var(--muted);margin:10px 0}
.lt .facts{font-family:var(--mono);font-size:11px;line-height:1.6;color:var(--dim);
  padding:8px 10px;background:var(--bg2);border-radius:8px}
.lt .lvl{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:14px}
.lt .lvl .k{font-family:var(--mono);font-size:11px;color:var(--dim);letter-spacing:.6px}
.lt .lvl .v{font-family:var(--mono);font-size:12px;font-weight:700;margin-top:3px}
.lt .lvl .pc{display:block;font-size:11px;font-weight:500;opacity:.8}
.lt-f{margin-top:14px;padding-top:11px;border-top:1px solid var(--line);
  font-family:var(--mono);font-size:11px;color:var(--dim);letter-spacing:.4px}
@media(max-width:520px){.ltgrid{grid-template-columns:1fr}}

/* Single column since the stat tiles came out — a 1.6fr/1fr grid with one
   child leaves 38% of the row empty. */
.who{display:block}
.who-m{background:var(--surface);border:1px solid var(--line);border-left:3px solid var(--lime);
  border-radius:16px;padding:26px}
.who-name{font-size:24px;font-weight:800;letter-spacing:-1px}
.who-role{font-family:var(--mono);font-size:11px;letter-spacing:2px;text-transform:uppercase;
  color:var(--lime);margin:6px 0 16px}
.who-m p{font-size:14px;line-height:1.65;color:var(--muted);margin-bottom:12px}
.who-m .who-sub{font-size:13px;color:var(--dim)}
.who-links{display:flex;flex-wrap:wrap;gap:9px;margin-top:18px}
.who-links a{font-family:var(--mono);font-size:11px;letter-spacing:.6px;color:var(--lime);
  border:1px solid var(--lime-line);border-radius:99px;padding:6px 13px;transition:background .25s}
.who-links a:hover{background:var(--lime-soft)}

/* ═══════════════════ SUBSCRIBE ═══════════════════ */
.sub-cta{background:var(--surface);border:1px solid var(--line);border-left:3px solid var(--lime);
  border-radius:16px;padding:26px 28px;margin:34px 0;
  display:grid;grid-template-columns:1.15fr 1fr;gap:26px;align-items:center}
.sub-cta h3{font-size:19px;font-weight:750;letter-spacing:-.5px;margin:0 0 8px;text-wrap:balance}
.sub-cta p{font-size:14px;line-height:1.6;color:var(--muted);margin:0}
.sub-cta .fine{font-family:var(--mono);font-size:11px;color:var(--dim);margin-top:11px;line-height:1.5}
.sub-form{display:flex;gap:9px;flex-wrap:wrap}
.sub-form input[type=email]{flex:1 1 210px;min-width:0;background:var(--bg2);
  border:1px solid var(--line2);border-radius:10px;padding:13px 15px;color:var(--text);
  font-family:var(--mono);font-size:13px;min-height:46px}
.sub-form input[type=email]::placeholder{color:var(--dim)}
.sub-form input[type=email]:focus{border-color:var(--lime);outline:none}
.sub-form button{background:var(--lime);color:var(--on-brand);border:none;border-radius:10px;
  padding:13px 22px;font-family:var(--mono);font-size:13px;font-weight:700;letter-spacing:.5px;
  cursor:pointer;min-height:46px;transition:transform .16s var(--ease),opacity .16s}
.sub-form button:hover:not(:disabled){transform:translateY(-1px)}
.sub-form button:disabled{opacity:.55;cursor:default}
/* Honeypot: off-screen, not display:none — some bots skip hidden fields. */
.sub-form .hp{position:absolute;left:-9999px;width:1px;height:1px;overflow:hidden}
.sub-msg{font-family:var(--mono);font-size:12px;margin-top:10px;min-height:17px}
.sub-msg.ok{color:var(--up)} .sub-msg.err{color:var(--down)}
.tg-cta{display:inline-flex;align-items:center;gap:9px;background:var(--bg2);
  border:1px solid var(--lime-line);border-radius:99px;padding:10px 18px;
  font-family:var(--mono);font-size:12px;color:var(--lime);min-height:24px;
  transition:background .2s}
.tg-cta:hover{background:var(--lime-soft)}
@media(max-width:760px){.sub-cta{grid-template-columns:1fr;gap:18px;padding:22px}}

.foot-legal{display:grid;grid-template-columns:1.4fr 1.4fr 1fr;gap:28px;
  max-width:1400px;margin:34px auto 0;padding:26px var(--gut) 0;
  border-top:1px solid var(--line)}
.foot-legal h4,.foot-legal .fh4{font-family:var(--mono);font-size:11px;letter-spacing:1.6px;text-transform:uppercase;
  color:var(--lime);margin:0 0 9px}
.foot-legal p{font-size:12px;line-height:1.65;color:var(--dim);margin:0}
.foot-legal strong{color:var(--muted)}
.foot-legal a{color:var(--muted);text-decoration:underline;text-underline-offset:2px}
.foot-legal a:hover{color:var(--lime)}
@media(max-width:840px){.foot-legal{grid-template-columns:1fr;gap:20px}}

/* ═══════════════════ MUSIC ═══════════════════ */
.crates{display:grid;grid-template-columns:repeat(3,1fr);gap:20px}
.crate{background:var(--surface);border:1px solid var(--line);border-radius:16px;overflow:hidden}
.crate[data-crate="bhakti"]{border-left:3px solid var(--gold)}
.crate[data-crate="songs"]{border-left:3px solid var(--violet)}
.crate[data-crate="global"]{border-left:3px solid var(--blue)}
.crate-h{display:flex;align-items:center;gap:10px;padding:15px 18px;border-bottom:1px solid var(--line)}
.crate-h .ic{font-size:14px;line-height:1}
.crate-h .nm{font-family:var(--mono);font-size:11px;letter-spacing:2px;text-transform:uppercase;
  color:var(--text);font-weight:700;flex:1}
.crate-h .ct{font-family:var(--mono);font-size:11px;color:var(--dim);
  border:1px solid var(--line);border-radius:99px;padding:2px 9px}
.crate-l{list-style:none;margin:0;padding:0}
.trk{display:flex;align-items:center;gap:12px;padding:0 18px;border-bottom:1px solid var(--line)}
.trk:last-child{border-bottom:none}
.trk.more{display:none}
.crate.open .trk.more{display:flex;animation:trkIn .32s var(--ease) both}
@keyframes trkIn{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:none}}
.trk .no{font-family:var(--mono);font-size:11px;color:var(--dim);width:20px;flex:none}
.trk a{flex:1;display:flex;flex-direction:column;gap:2px;padding:11px 0;min-height:24px;
  text-decoration:none}
.trk .ti{font-size:14px;font-weight:500;color:var(--text);line-height:1.35}
.trk .ar{font-family:var(--mono);font-size:11px;color:var(--dim);letter-spacing:.3px}
.trk .pl{font-size:11px;color:var(--dim);flex:none;transition:color .2s,transform .2s var(--ease)}
.trk:hover{background:var(--bg2)}
.trk:hover .ti{color:var(--lime)}
.trk:hover .pl{color:var(--lime);transform:scale(1.35)}
/* ═══════════════════ TRADE SHEET ═══════════════════ */
#alertTable tr.clickable{cursor:pointer}
#alertTable tr.clickable:hover{background:var(--bg2)}
.sheet{position:fixed;inset:0;z-index:600;background:rgba(0,0,0,.72);
  display:flex;align-items:flex-start;justify-content:center;overflow-y:auto;padding:5vh 16px}
.sheet[hidden]{display:none}
.sheet-in{position:relative;background:var(--surface);border:1px solid var(--line2);
  border-radius:16px;padding:26px;max-width:720px;width:100%;margin-bottom:5vh}
/* The sticky header stack is z-index 300 and the command palette 500. At 120
   the sheet opened UNDERNEATH both: its own title and close button sat behind
   the nav, so it looked truncated rather than broken and there was no obvious
   way out of it. 600 clears the whole stack. */
.sheet-x{position:absolute;top:14px;right:14px;background:none;border:none;color:var(--dim);
  font-size:16px;cursor:pointer;width:34px;height:34px;border-radius:50%}
.sheet-x:hover{color:var(--text);background:var(--bg2)}
.sheet-h{display:flex;justify-content:space-between;align-items:baseline;gap:14px;
  flex-wrap:wrap;padding-right:38px;margin-bottom:18px}
.sheet-sym{font-size:24px;font-weight:700;letter-spacing:-.5px}
.sheet-kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(120px,100%),1fr));gap:1px;
  background:var(--line);border:1px solid var(--line);border-radius:10px;overflow:hidden;margin-bottom:26px}
.sheet-kpi>div{background:var(--surface);padding:14px}
.sheet-kpi b{display:block;font-family:var(--mono);font-size:19px}
.sheet-kpi span{display:block;font-size:11px;color:var(--dim);margin-top:4px;
  text-transform:uppercase;letter-spacing:.8px}
/* Levels on one axis. Height is generous because the labels stagger. */
.scale{position:relative;height:104px;margin:8px 0 4px;
  border-bottom:1px solid var(--line2)}
.scale .lv{position:absolute;bottom:0;transform:translateX(-50%);text-align:center;width:74px}
/* End labels anchor inward so they cannot hang off the axis. The tick stays
   where it belongs — only the text box moves. */
.scale .lv.at-start{transform:translateX(-8px);text-align:left}
.scale .lv.at-start i{margin-left:8px}
.scale .lv.at-end{transform:translateX(calc(-100% + 8px));text-align:right}
.scale .lv.at-end i{margin-right:8px}
.scale .lv i{display:block;width:2px;height:34px;margin:0 auto 6px;background:var(--dim)}
.scale .lv-l{display:block;font-family:var(--mono);font-size:11px;letter-spacing:.8px;
  text-transform:uppercase;color:var(--dim)}
.scale .lv-v{display:block;font-family:var(--mono);font-size:11px;color:var(--text)}
.scale .lv-r{display:block;font-family:var(--mono);font-size:11px;color:var(--dim)}
.scale .sl i{background:var(--down)} .scale .sl .lv-l{color:var(--down)}
.scale .t1 i,.scale .t2 i{background:var(--up)} .scale .t1 .lv-l,.scale .t2 .lv-l{color:var(--up)}
.scale .en i{background:var(--text);height:52px} .scale .en .lv-l{color:var(--text)}
.scale .ex i{background:var(--gold);height:68px} .scale .ex .lv-l{color:var(--gold)}
.scale-note{font-family:var(--mono);font-size:11px;color:var(--dim);margin:0 0 22px}
.sheet-tl{border-left:2px solid var(--line2);padding-left:16px;margin-bottom:20px}
.tl-row{display:grid;grid-template-columns:120px 150px 1fr;gap:10px;padding:7px 0;
  font-family:var(--mono);font-size:11px;align-items:baseline}
.tl-k{color:var(--text)} .tl-v{color:var(--lime)} .tl-w{color:var(--dim)}
.sheet-flags{background:var(--bg2);border-left:2px solid var(--gold);border-radius:0 8px 8px 0;
  padding:12px 16px;margin-bottom:20px}
.sheet-flags p{margin:0 0 8px;font-family:var(--mono);font-size:11px;line-height:1.6;color:var(--gold)}
.sheet-flags p:last-child{margin-bottom:0}
/* "Why this fired" — the engine's own gates, rendered from metadata. */
.sheet-why{border:1px solid var(--line2);border-radius:8px;padding:14px 16px;margin-bottom:20px}
.sheet-why h4,.sheet-why .fh4{margin:0 0 10px;font-family:var(--mono);font-size:11px;letter-spacing:1.2px;
  text-transform:uppercase;color:var(--dim)}
.wy-p{margin:0 0 10px;font-size:12px;line-height:1.65;color:var(--muted)}
.wy-p:last-child{margin-bottom:0}
.wy-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(150px,100%),1fr));gap:6px 18px}
.wy-row{display:flex;justify-content:space-between;gap:10px;border-bottom:1px solid var(--line);
  padding:5px 0;font-family:var(--mono);font-size:11px}
.wy-k{color:var(--dim)}
.wy-v{color:var(--fg);font-variant-numeric:tabular-nums}
@media(max-width:640px){
  .tl-row{grid-template-columns:1fr;gap:2px}
  .scale .lv{width:56px}
}

/* ═══════════════════ SIZER + HEAT ═══════════════════ */
.sizer{border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:20px;background:var(--bg2)}
.sizer-h{font-family:var(--mono);font-size:11px;letter-spacing:1.3px;text-transform:uppercase;
  color:var(--dim);margin-bottom:12px}
.sizer-in{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px}
.sizer-in label{display:flex;flex-direction:column;gap:5px;font-family:var(--mono);
  font-size:11px;letter-spacing:.8px;text-transform:uppercase;color:var(--dim);flex:1;min-width:120px}
.sizer-in input{background:var(--surface);border:1px solid var(--line2);border-radius:8px;
  color:var(--text);font-family:var(--mono);font-size:14px;padding:9px 11px;min-height:42px;width:100%}
.sizer-in input:focus{outline:none;border-color:var(--lime)}
.sz-row{display:flex;justify-content:space-between;gap:12px;padding:6px 0;
  font-family:var(--mono);font-size:12px;border-bottom:1px solid var(--line)}
.sz-row:last-of-type{border-bottom:none}
.sz-row span{color:var(--dim)} .sz-row b{color:var(--text)}
.sz-note{font-family:var(--mono);font-size:11px;color:var(--dim);margin:10px 0 0;line-height:1.6}
.sz-warn{font-family:var(--mono);font-size:11px;color:var(--gold);margin:10px 0 0;line-height:1.6}
.heat{margin:18px 0 22px;padding:16px 18px;border:1px solid var(--line);border-radius:12px;
  background:var(--surface)}
.heat-h{display:flex;justify-content:space-between;gap:12px;margin-bottom:6px}
.heat-h .eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:1.3px;
  text-transform:uppercase;color:var(--dim)}
.heat-n{font-family:var(--mono);font-size:32px;font-weight:600;color:var(--lime);line-height:1.1}
.heat-n.hot{color:var(--down)}
.heat-w{font-family:var(--mono);font-size:11px;line-height:1.7;color:var(--muted);
  margin:8px 0 0;max-width:82ch}
.heat-w b{color:var(--text)}
.heat-g{display:flex;gap:26px;flex-wrap:wrap;margin-top:10px}
.heat-g>div{display:flex;flex-direction:column;gap:3px}
.heat-g span{font-family:var(--mono);font-size:11px;letter-spacing:1px;
  text-transform:uppercase;color:var(--dim)}
.heat-g b{font-family:var(--mono);font-size:11px;color:var(--muted);font-weight:400}
/* Distance from entry, under the entry price. Green within 1% (live), grey to
   4%, red beyond (already gone). */
.dist{font-family:var(--mono);font-size:11px;color:var(--dim);margin-top:2px}
.dist.up{color:var(--up)} .dist.dn{color:var(--down)}

/* ═══════════════════ UNDERWATER ═══════════════════ */
.uw{margin:22px 0 6px}
.uw-h{display:flex;justify-content:space-between;gap:12px;margin-bottom:6px}
.uw-h .eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:1.3px;
  text-transform:uppercase;color:var(--dim)}
.uw svg{display:block;width:100%;height:auto}
.uw-f{display:flex;flex-wrap:wrap;gap:18px;margin-top:8px;
  font-family:var(--mono);font-size:11px;color:var(--dim)}
.uw-f b{color:var(--text)}

/* ═══════════════════ FUND SCREEN ═══════════════════ */
.fund-note{font-family:var(--mono);font-size:12px;line-height:1.7;color:var(--muted);
  background:var(--bg2);border-left:2px solid var(--blue);border-radius:0 8px 8px 0;
  padding:14px 18px;margin-bottom:22px;max-width:82ch}
.fund-note strong{color:var(--text)}
.fundcat{margin-bottom:26px}
.fundcat-h{display:flex;align-items:baseline;justify-content:space-between;gap:12px}
.fundcat-h h3{font-size:16px;margin:0}
.fundcat-b{color:var(--muted);font-size:13px;margin:4px 0 12px;max-width:74ch}
.fund-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(280px,100%),1fr));gap:14px}
.fund-card{padding:16px}
.fund-card-h{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:12px}
.fund-card-h strong{font-size:14px}
.fund-card-f{display:flex;flex-wrap:wrap;gap:8px 16px;margin-top:12px;font-size:12px}
.fund-isin{font-family:var(--mono);font-size:11px;letter-spacing:.04em;
  white-space:nowrap;padding-top:2px}

/* ═══════════ DAILY BRIEF ═══════════ */
.ev-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(320px,100%),1fr));gap:12px}
.ev{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:15px 17px;
  display:flex;flex-direction:column;gap:9px}
.ev-top{background:var(--surface2);border-top:2px solid var(--lime)}
.ev-h{display:flex;justify-content:space-between;align-items:center;gap:10px}
.ev-cat{font-family:var(--mono);font-size:11px;letter-spacing:.11em;text-transform:uppercase;
  color:var(--lime)}
.ev-dots{display:flex;gap:3px;flex:none}
.ev-dots i{width:5px;height:5px;border-radius:50%;background:var(--line2);display:inline-block}
.ev-dots i.on{background:var(--lime)}
.ev-t{font-size:16px;font-weight:600;line-height:1.32;letter-spacing:-.01em;margin:0;text-wrap:balance}
.ev-m{display:flex;flex-wrap:wrap;gap:5px;font-family:var(--mono);font-size:11px;color:var(--dim)}
.ev-raw{color:var(--gold);cursor:help}
.ev-b{margin:0;padding-left:15px;font-size:13px;line-height:1.55;color:var(--muted)}
.ev-b li{margin-bottom:4px}
.ev-why{background:var(--bg2);border-left:2px solid var(--lime);border-radius:0 6px 6px 0;
  padding:8px 12px}
.ev-why span{font-family:var(--mono);font-size:11px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--lime)}
.ev-why p{margin:3px 0 0;font-size:12px;line-height:1.5;color:var(--text)}
.ev-mi{display:flex;flex-wrap:wrap;gap:5px}
.ev-chip{font-family:var(--mono);font-size:11px;padding:2px 8px;border-radius:20px;
  border:1px solid var(--line2);color:var(--dim)}
.ev-positive{color:var(--up);border-color:rgba(61,220,151,.45)}
.ev-negative{color:var(--down);border-color:rgba(255,92,92,.45)}
.ev-mixed,.ev-neutral,.ev-unclear{color:var(--muted)}
.ev-w{font-family:var(--mono);font-size:11px;color:var(--muted)}
.ev-s{display:flex;flex-wrap:wrap;gap:9px;padding-top:8px;border-top:1px dashed var(--line);
  margin-top:auto}
.ev-s a{font-family:var(--mono);font-size:11px;color:var(--dim);text-decoration:none}
.ev-s a:hover{color:var(--blue)}
.pv-warn{color:var(--gold)}

/* ═══════════ RESOURCES ═══════════ */
.res-g{margin-bottom:18px}
.res-h{font-family:var(--mono);font-size:11px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--muted);margin:0 0 9px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.res-l{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(290px,100%),1fr));gap:9px}
.res-i{display:block;background:var(--bg2);border:1px solid var(--line);border-radius:8px;
  padding:10px 13px;text-decoration:none;transition:border-color .15s}
.res-i:hover{border-color:var(--line2)}
.res-t{display:block;font-size:13px;font-weight:600;color:var(--text)}
.res-i:hover .res-t{color:var(--lime)}
.res-n{display:block;font-size:11px;line-height:1.5;color:var(--dim);margin-top:3px}

/* Standing methodology notes — long, unchanging, and previously sitting
   between the reader and the thing they came for. Collapsed by default. */
.fund-note-d{margin:14px 0}
.fund-note-d>summary{cursor:pointer;list-style:none;display:inline-flex;
  align-items:center;gap:7px;font-family:var(--mono);font-size:11px;
  letter-spacing:.06em;text-transform:uppercase;color:var(--dim);
  border:1px solid var(--line);border-radius:7px;padding:7px 12px}
.fund-note-d>summary::-webkit-details-marker{display:none}
.fund-note-d>summary::before{content:"+";font-size:13px;line-height:1}
.fund-note-d[open]>summary::before{content:"\2212"}
.fund-note-d>summary:hover{color:var(--text);border-color:var(--line2)}
.fund-note-d>summary:focus-visible{outline:2px solid var(--blue);outline-offset:3px}
.fund-note-d .fund-note{margin-top:12px}

/* "Relates to" in the signal log. Constrained and wrapped: it is a sentence in
   a table of numbers, and left free it would set the width of every column. */
.rmk{font-size:11px;color:var(--dim);max-width:210px;min-width:150px;
  white-space:normal;line-height:1.4}

/* Portfolio composition — what the fund owns. */
.fpf{margin-top:11px;padding-top:10px;border-top:1px solid var(--line)}
.fpf-r{display:flex;gap:8px;align-items:baseline;margin-bottom:6px}
.fpf-k{font-family:var(--mono);font-size:11px;letter-spacing:.07em;
  text-transform:uppercase;color:var(--dim);flex:none;width:64px;padding-top:2px}
.fpf-v{display:flex;flex-wrap:wrap;gap:4px;min-width:0}
.fpf-c{font-size:11px;color:var(--muted);background:var(--bg2);
  border:1px solid var(--line);border-radius:5px;padding:2px 6px;white-space:nowrap}
.fpf-c b{font-family:var(--mono);color:var(--text);font-weight:600}
.fpf-m{font-family:var(--mono);font-size:11px;color:var(--dim);margin-top:7px}

/* Advanced detail. A native <details> on purpose: no z-index, no stacking
   context to escape (see the modal note above), works with JS disabled, and
   two funds can be open at once to compare. */
.fund-more{margin-top:12px;border-top:1px solid var(--line);padding-top:10px}
.fund-more>summary{cursor:pointer;font-family:var(--mono);font-size:11px;
  letter-spacing:.08em;text-transform:uppercase;color:var(--dim);
  list-style:none;display:flex;align-items:center;gap:6px}
.fund-more>summary::-webkit-details-marker{display:none}
.fund-more>summary::before{content:"+";font-size:13px;line-height:1}
.fund-more[open]>summary::before{content:"\2212"}
.fund-more>summary:hover{color:var(--text)}
.fund-more>summary:focus-visible{outline:2px solid var(--blue);outline-offset:3px}
.fund-more-b{margin-top:12px}
.fm-block{margin-bottom:14px}
.fm-h{font-family:var(--mono);font-size:11px;letter-spacing:.06em;
  text-transform:uppercase;color:var(--muted);margin-bottom:4px}
.fm-note{color:var(--dim);font-size:11px;line-height:1.55;margin:0 0 8px;max-width:52ch}
.fm-row{display:flex;justify-content:space-between;gap:12px;font-size:12px;
  padding:3px 0;border-bottom:1px dotted var(--line)}
.fm-row:last-child{border-bottom:0}
.fm-row span{color:var(--dim)}
.fm-row b{font-family:var(--mono);color:var(--text);font-weight:600}
.fm-cal{display:flex;flex-wrap:wrap;gap:6px}
.fm-cy{flex:1 1 58px;background:var(--bg2);border:1px solid var(--line);
  border-radius:6px;padding:6px 8px;text-align:center}
.fm-cy-y{display:block;font-family:var(--mono);font-size:11px;color:var(--dim)}
.fm-cy-v{display:block;font-family:var(--mono);font-size:12px;font-weight:600;margin-top:2px}
.fm-src{color:var(--dim);font-size:11px;line-height:1.55;margin:0;
  padding-top:8px;border-top:1px solid var(--line)}
.fm-src a{color:var(--blue)}

/* ═══════════════════ FINANCE CAREERS ═══════════════════ */
.jsnap{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(104px,100%),1fr));
  gap:1px;background:var(--line);border:1px solid var(--line);border-radius:8px;
  overflow:hidden;margin:14px 0}
.jsnap-i{background:var(--bg2);padding:12px 10px;text-align:center}
.jsnap-i b{display:block;font-family:var(--mono);font-size:19px;color:var(--text);line-height:1.1}
.jsnap-i span{display:block;font-size:11px;color:var(--dim);margin-top:3px;letter-spacing:.02em}
.jfail{font-family:var(--mono);font-size:11px;line-height:1.6;color:var(--gold);
  background:var(--bg2);border-left:2px solid var(--gold);border-radius:0 8px 8px 0;
  padding:10px 14px;margin:0 0 14px;max-width:80ch}
.jfilters{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;margin:14px 0}
.jf-grp{display:flex;flex-wrap:wrap;gap:6px}
.jf-count{font-family:var(--mono);font-size:11px;color:var(--dim);margin-left:auto}
.jsub{font-size:14px;margin:22px 0 2px;letter-spacing:-.01em}
.jsub-n{color:var(--dim);font-size:12px;line-height:1.6;margin:0 0 12px;max-width:76ch}
.jgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(330px,100%),1fr));gap:12px}
.jgrid-quiet{opacity:.72}
.jcard{background:var(--surface);border:1px solid var(--line);border-radius:10px;
  padding:14px 16px;display:flex;flex-direction:column;gap:10px}
.jcard-top{border-color:rgba(61,220,151,.34)}
.jcard[hidden]{display:none}
.jc-h{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.jc-id{display:flex;gap:10px;align-items:flex-start;min-width:0}
.jc-rank{font-family:var(--mono);font-size:11px;color:var(--dim);padding-top:3px}
.jc-t{font-size:14px;line-height:1.35;margin:0;letter-spacing:-.01em}
.jc-co{font-size:12px;color:var(--muted);margin-top:3px}
.jc-loc{color:var(--dim)}
.jc-tier{text-align:center;border-radius:7px;padding:5px 9px;min-width:46px;
  border:1px solid var(--line2);flex:none}
.jc-tier b{display:block;font-family:var(--mono);font-size:14px;line-height:1}
.jc-tier span{display:block;font-family:var(--mono);font-size:11px;color:var(--dim);margin-top:2px}
.jc-tier-s{background:rgba(61,220,151,.14);border-color:rgba(61,220,151,.45)}
.jc-tier-s b{color:var(--up)}
.jc-tier-a{background:rgba(106,168,255,.12);border-color:rgba(106,168,255,.4)}
.jc-tier-a b{color:var(--blue)}
.jc-tier-b b{color:var(--text)}
.jc-tier-c b,.jc-tier-d b{color:var(--dim)}
.jc-meta{display:flex;flex-wrap:wrap;gap:5px 12px;font-size:11px;color:var(--muted);
  align-items:center}
.jc-meta .jm b{font-family:var(--mono);color:var(--text)}
.jc-meta .dimmed{color:var(--dim)}
.jbadge{font-family:var(--mono);font-size:11px;letter-spacing:.07em;padding:2px 6px;
  border-radius:4px;border:1px solid var(--line2);color:var(--dim)}
.jb-new{color:var(--up);border-color:rgba(61,220,151,.45);background:rgba(61,220,151,.1)}
.jb-active{color:var(--blue);border-color:rgba(106,168,255,.4)}
.jb-aging{color:var(--gold);border-color:rgba(232,197,71,.4)}
.jb-stale{color:var(--dim)}
.jb-closed,.jb-removed,.jb-link_broken{color:var(--down);border-color:rgba(255,92,92,.4)}
.jc-why,.jc-warn{margin:0;padding-left:15px;font-size:12px;line-height:1.55}
.jc-why{color:var(--muted)}
.jc-why li{margin-bottom:3px}
.jc-warn{color:var(--dim)}
.jc-warn li::marker{color:var(--gold)}
.jc-f{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:auto;padding-top:4px}
.jc-apply{font-family:var(--mono);font-size:11px;letter-spacing:.06em;text-transform:uppercase;
  padding:7px 13px;border-radius:6px;background:var(--up);color:var(--on-up);font-weight:600;
  text-decoration:none;border:1px solid transparent}
.jc-apply:hover{filter:brightness(1.08)}
.jc-apply-unv{background:transparent;color:var(--gold);border-color:rgba(232,197,71,.5)}
.jc-apply-none{background:transparent;color:var(--dim);border-color:var(--line);cursor:default}
.jc-view{font-family:var(--mono);font-size:11px;letter-spacing:.05em;color:var(--muted);
  text-decoration:none;border-bottom:1px solid var(--line2);padding-bottom:1px}
.jc-view:hover{color:var(--text)}
.jc-src{font-family:var(--mono);font-size:11px;color:var(--dim);margin-left:auto}
@media (max-width:560px){.jgrid{grid-template-columns:1fr}.jc-src{margin-left:0}}

/* Small-sample warning on the performance section. Gold, not red: this is not
   an error, it is a true statement about how little data there is. */
.thin-warn{font-family:var(--mono);font-size:12px;line-height:1.65;
  color:var(--gold);background:var(--bg2);border-left:2px solid var(--gold);
  border-radius:0 8px 8px 0;padding:12px 16px;margin:14px 0 0;max-width:78ch}

/* ═══════════════════ SWP ═══════════════════ */
.swp-in{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(148px,100%),1fr));gap:12px;
  margin-bottom:22px}
.swp-in label{display:flex;flex-direction:column;gap:6px;font-family:var(--mono);
  font-size:11px;letter-spacing:1.2px;text-transform:uppercase;color:var(--dim)}
.swp-in input{background:var(--bg2);border:1px solid var(--line);border-radius:8px;
  color:var(--text);font-family:var(--mono);font-size:14px;padding:10px 12px;
  min-height:44px;width:100%;transition:border-color .18s}
.swp-in input:focus{outline:none;border-color:var(--lime)}
.swp-verdict{font-family:var(--mono);font-size:13px;line-height:1.6;padding:14px 16px;
  border-left:2px solid var(--lime);background:var(--bg2);border-radius:0 8px 8px 0;
  margin-bottom:22px;color:var(--text)}
.swp-verdict.short{border-left-color:var(--down)}
.swp-toggle{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.swp-toggle button{background:none;border:none;color:var(--dim);cursor:pointer;
  font-family:var(--mono);font-size:11px;letter-spacing:1px;text-transform:uppercase;
  padding:9px 13px;min-height:38px;transition:background .18s,color .18s}
.swp-toggle button.on{background:var(--lime);color:var(--on-brand)}
#swpChart{display:block;width:100%;height:auto;margin-top:6px}
.legend{display:flex;flex-wrap:wrap;gap:18px;margin-top:12px;font-family:var(--mono);
  font-size:11px;letter-spacing:.6px;color:var(--dim)}
.legend .sw{display:inline-block;width:11px;height:3px;border-radius:2px;
  margin-right:7px;vertical-align:middle}
.cardhead{display:flex;justify-content:space-between;align-items:center;gap:12px;
  margin-bottom:10px;flex-wrap:wrap}
.cardhead .eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:1.4px;
  text-transform:uppercase;color:var(--dim)}
#swp .card{margin-bottom:18px}
/* Summary strip — the collapsed state's entire content, and it stays put when
   expanded so the headline numbers never disappear while you are editing. */
.swp-sum{display:flex;justify-content:space-between;align-items:center;gap:16px;
  flex-wrap:wrap;padding:16px 18px;margin-bottom:18px;background:var(--surface);
  border:1px solid var(--line);border-radius:12px}
.swp-sum-k{font-family:var(--mono);font-size:13px;color:var(--muted);
  display:flex;gap:10px;flex-wrap:wrap;align-items:baseline}
.swp-sum-k b{color:var(--text);font-weight:600}
.swp-sum-k .sep{color:var(--line2)}
.swp-sum-k .short{color:var(--down)}
.swp-sum-k .ok{color:var(--lime)}
.swp-toggle-all{background:none;border:1px solid var(--line2);border-radius:8px;
  color:var(--dim);cursor:pointer;font-family:var(--mono);font-size:11px;
  letter-spacing:1.2px;text-transform:uppercase;padding:10px 14px;min-height:40px;
  white-space:nowrap;transition:color .18s,border-color .18s}
.swp-toggle-all:hover{color:var(--lime);border-color:var(--lime)}
.swp-body[hidden]{display:none}
#swpTbl tr.ret td{color:var(--lime);border-top:1px solid var(--lime)}
#swpTbl tr.dead td{color:var(--down);opacity:.75}
@media(max-width:600px){.swp-in{grid-template-columns:1fr 1fr}}

/* Like control. 34px square is below the 44px touch guidance on its own, so
   the row itself is the generous target and this sits inside it — the whole
   .trk highlights on hover and the button only has to be hittable, not huge.
   Kept at opacity 0 on the pointer devices that can reveal it on hover, and
   always visible on touch, where there is no hover to reveal anything. */
.trk .lk{flex:none;width:34px;height:34px;display:grid;place-items:center;
  background:none;border:none;border-radius:50%;cursor:pointer;
  font-size:14px;line-height:1;color:var(--dim);opacity:0;
  transition:color .2s,opacity .2s,transform .2s var(--ease)}
.trk:hover .lk,.trk .lk:focus-visible{opacity:1}
.trk .lk:hover{color:var(--down);background:var(--bg2);transform:scale(1.18)}
/* Liked is a state, not a hover affordance — it stays lit and stays visible. */
.trk .lk[aria-pressed="true"]{opacity:1;color:var(--down)}
.trk .lk.busy{opacity:1;color:var(--gold)}
@media(hover:none){.trk .lk{opacity:1}}
.crate-note{margin-top:12px;font-family:var(--mono);font-size:11px;
  letter-spacing:.4px;color:var(--dim);min-height:16px}
.crate-note.err{color:var(--gold)}
/* The play control is a button now, not a decorative span. Same look, but it
   is tabbable and hittable. .trk:hover styling already targets .pl. */
.trk .pl{background:none;border:none;padding:0;width:26px;height:34px;cursor:pointer}
.trk .pl.on{color:var(--lime);opacity:1;transform:scale(1.3)}

/* Docked player. Fixed bottom-right on desktop so it survives scrolling;
   full-width across the bottom on phones, where a floating card would cover
   the crate you are picking from. */
.player{position:fixed;right:20px;bottom:20px;width:360px;max-width:calc(100vw - 32px);
  background:var(--surface);border:1px solid var(--line2);border-radius:14px;
  overflow:hidden;z-index:60;box-shadow:0 18px 48px rgba(0,0,0,.6)}
.player[hidden]{display:none}
.player-h{display:flex;align-items:center;gap:10px;padding:9px 12px;
  border-bottom:1px solid var(--line);background:var(--bg2)}
.player-t{flex:1;font-size:12px;font-weight:500;color:var(--text);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.player-y{font-family:var(--mono);font-size:11px;letter-spacing:.6px;
  color:var(--dim);text-decoration:none;flex:none;white-space:nowrap}
.player-y:hover{color:var(--lime)}
.player-x{background:none;border:none;color:var(--dim);cursor:pointer;
  font-size:14px;line-height:1;flex:none;width:30px;height:30px;border-radius:50%}
.player-x:hover{color:var(--down);background:var(--surface2)}
/* 16:9, and sized so the player clears YouTube's documented 200x200 minimum
   for embedded players (356 wide gives ~200 high). Smaller than that would be
   a nicer audio bar and would breach the embed terms. */
.player-f{position:relative;width:100%;aspect-ratio:16/9;background:#000}
.player-f iframe{position:absolute;inset:0;width:100%;height:100%;border:0}
.player-a{font-size:13px;line-height:1;color:var(--dim);text-decoration:none;flex:none}
.player-a:hover{color:var(--text)}
@media(max-width:620px){
  .player{right:0;left:0;bottom:0;width:auto;max-width:none;border-radius:14px 14px 0 0}
}
.crate-more{width:100%;background:none;border:none;border-top:1px solid var(--line);
  color:var(--dim);font-family:var(--mono);font-size:11px;letter-spacing:1.4px;
  text-transform:uppercase;padding:13px;cursor:pointer;min-height:24px;transition:color .2s}
.crate-more:hover{color:var(--lime)}
@media(max-width:1040px){.crates{grid-template-columns:1fr 1fr}}
@media(max-width:760px){.crates{grid-template-columns:1fr}}

/* ═══════════ MAP MOTION ═══════════
   The map was static apart from two pulse rings. It is a *live* incident feed,
   so it should read as alive without becoming a toy: a terminator line for
   where night currently falls, a sweep that passes once every 12s, and dots
   that arrive rather than appear. Everything here is transform/opacity only,
   so it composites on the GPU and costs no layout. All of it stops under
   prefers-reduced-motion. */
.wmap{isolation:isolate}
/* Night side — a real datum, positioned from UTC by JS. */
.wm-night{position:absolute;top:0;bottom:0;pointer-events:none;z-index:1;
  background:linear-gradient(90deg,transparent,rgba(3,6,14,.52) 18%,rgba(3,6,14,.52) 82%,transparent);
  transition:left .8s var(--ease),width .8s var(--ease)}
/* Radar sweep, one pass every 12s. */
.wm-sweep{position:absolute;top:0;bottom:0;width:14%;pointer-events:none;z-index:2;
  background:linear-gradient(90deg,transparent,rgba(184,239,67,.055) 55%,rgba(184,239,67,.11));
  border-right:1px solid rgba(184,239,67,.18);
  animation:wmSweep 12s linear infinite}
@keyframes wmSweep{from{transform:translateX(-120%)}to{transform:translateX(820%)}}
/* Dots land instead of blinking on. */
.wmap circle.ev{transform-box:fill-box;transform-origin:center;
  animation:wmDrop .5s var(--ease) both;animation-delay:var(--d,0s)}
@keyframes wmDrop{from{transform:scale(0);opacity:0}60%{transform:scale(1.28)}to{transform:scale(1);opacity:1}}
.wmap circle.ev{transition:filter .2s}
.wmap circle.ev:hover{filter:brightness(1.35) drop-shadow(0 0 4px currentColor)}
/* A red country keeps a slow breath so escalation reads before you hover. */
.wmap circle.ev.red{animation:wmDrop .5s var(--ease) both,wmBreathe 2.8s ease-in-out 1s infinite}
@keyframes wmBreathe{0%,100%{opacity:1}50%{opacity:.62}}
@media(prefers-reduced-motion:reduce){
  .wm-sweep{display:none}
  .wmap circle.ev,.wmap circle.ev.red{animation:none;opacity:1}
}

/* ═══════════ HERO EQUITY CURVE ═══════════ */
/* .herocurve CSS removed with the element — see the note in the markup. */

/* Trade idea symbols open the chart, same as the long-term cards. The name
   was plain text on the one card type where you most want the chart. */
.pick .sym a{color:inherit;text-decoration:none;border-bottom:1px solid transparent;
  transition:color .2s,border-color .2s}
.pick .sym a:hover{color:var(--lime);border-bottom-color:var(--lime-line)}
.pick .sym a::after{content:' ↗';font-size:.62em;opacity:.5;vertical-align:super}

/* ═══════════════════ SECTIONS ═══════════════════ */
main{position:relative;z-index:2;max-width:1400px;margin:0 auto;padding:0 var(--gut)}
.sec{border-bottom:0}
.sec:last-child{border-bottom:none}
/* Was clamp(26px,4vw,44px). Measured on the rendered page: the gap after a
   .shead or .subhead was the most common large space on the document — 40px,
   fourteen times over, on top of the 12px between sections and 18px of section
   padding. Trimmed to a 20-28px band, which still separates a heading from its
   content without costing most of a phone screen per section. */
.shead{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;flex-wrap:wrap;margin-bottom:clamp(20px,2.4vw,28px)}
/* The eyebrow takes the pillar's hue, so "12 / PAPER WALLET" is the same
   colour as the PORTFOLIO button that got you here. --pillar is set per
   section by the generated block under the nav; the fallback keeps every
   other use of .snum working. */
.snum{font-family:var(--mono);font-size:11px;color:var(--pillar,var(--lime));letter-spacing:2px;
  margin-bottom:12px;display:flex;align-items:center;gap:8px}
/* A short rule in the pillar hue ahead of the number. On a page of 33 stacked
   sections this is the cheapest possible "you are somewhere new" marker, and
   it is the thing that makes the colour scheme visible while scrolling rather
   than only in the nav. */
.snum::before{content:'';width:22px;height:2px;background:var(--pillar,var(--lime));flex:none;border-radius:2px}
.stitle{font-family:var(--serif);font-size:clamp(26px,4.4vw,50px);font-weight:600;
  letter-spacing:-.8px;line-height:1.04}
.sdesc{font-size:13px;color:var(--muted);max-width:44ch;line-height:1.55}
/* Subsection heading. Nine subsections were each hand-styled with an inline
   `<h3 style="font-size:14px">`, which rendered them as bold body text: no
   eyebrow, no rule, no relationship to the .shead above them. A reader hit a
   table with what looked like a caption over it, which is how a section ends
   up reading as an unlabelled block of data. One class, so a subsection cannot
   be added later without inheriting the hierarchy. */
.subhead{margin:clamp(28px,3.6vw,42px) 0 12px;padding-top:16px;border-top:1px solid var(--line)}
.subhead:first-child{border-top:none;padding-top:0;margin-top:0}
.subeyebrow{font-family:var(--mono);font-size:11px;letter-spacing:1.8px;text-transform:uppercase;
  color:var(--lime);display:block;margin-bottom:8px}
.subhead h3{font-family:var(--serif);font-size:clamp(18px,2.1vw,25px);font-weight:600;
  letter-spacing:-.4px;line-height:1.2;margin:0;display:flex;align-items:baseline;
  gap:9px;flex-wrap:wrap;text-wrap:balance}
/* The "what is this / why it matters" line. Wider measure than .sdesc: it sits
   under a narrower heading and carries a full sentence, not a label. */
.subdesc{font-size:12px;color:var(--muted);max-width:66ch;line-height:1.6;margin:9px 0 0}
@media(max-width:640px){.subhead{margin-top:26px;padding-top:14px}}
/* ── Data health ─────────────────────────────────────────────────────────
   One badge, six statuses, every section. Before it, #funds said "0.5d old",
   #stocks printed a coverage count and the brief said nothing — a reader had
   no way to tell which section was oldest.

   Colour is deliberately NOT the only signal. The status WORD is always
   printed next to the dot, because a green dot over stale data is precisely
   the misleading "live" presentation the 2026-08-18 audit named. It also
   means the badge survives a colour-blind reader and a greyscale print. */
.dh{display:inline-flex;align-items:center;gap:6px;font-family:var(--mono);
  font-size:11px;letter-spacing:1.2px;text-transform:uppercase;
  padding:3px 8px;border-radius:3px;border:1px solid var(--line2);
  color:var(--muted);white-space:nowrap;vertical-align:middle}
.dh::before{content:"";width:6px;height:6px;border-radius:50%;
  background:currentColor;flex:none}
/* Freshness is INFORMATION, not a market outcome, so it is blue.
   LIVE was var(--up) — the identical green as "+5.64%" — so a data-freshness
   badge and a rising price were the same colour saying two unrelated things,
   and a reader scanning for green found both. FRESH was var(--lime), the brand
   accent, which made a status read as decoration. Both are now blue: neutral,
   informational, impossible to mistake for a gain. */
.dh-LIVE{color:var(--blue);border-color:rgba(106,168,255,.40);background:rgba(106,168,255,.07)}
.dh-FRESH{color:var(--blue);border-color:rgba(106,168,255,.24)}
.dh-STALE{color:var(--gold);border-color:rgba(232,197,71,.35)}
.dh-DEGRADED{color:var(--gold);border-color:rgba(232,197,71,.55);background:rgba(232,197,71,.07)}
.dh-FAILED{color:var(--down);border-color:rgba(255,92,92,.45);background:rgba(255,92,92,.07)}
.dh-UNAVAILABLE{color:var(--dim)}
.dh-age{color:var(--dim);letter-spacing:.4px;text-transform:none;font-size:11px}
/* The health table. Grid rather than <table> so it can reflow to stacked
   cards on a phone without a horizontal scroller — the audit's mobile note
   was specifically that dense tables must be redesigned, not shrunk. */
.dh-list{list-style:none;display:grid;gap:10px;margin-top:20px}
.dh-row{border:1px solid var(--line);border-left:2px solid var(--line2);
  border-radius:4px;padding:14px 16px;background:var(--surface)}
.dh-row.bad{border-left-color:var(--gold);background:var(--surface2)}
.dh-row.dead{border-left-color:var(--down);background:var(--surface2)}
.dh-top{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.dh-name{font-weight:500;font-size:14px}
.dh-src{color:var(--dim);font-size:12px;margin-left:auto}
.dh-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(140px,100%),1fr));
  gap:10px 18px;margin-top:12px;font-size:12px}
.dh-grid dt{color:var(--dim);font-family:var(--mono);font-size:11px;
  letter-spacing:1px;text-transform:uppercase}
.dh-grid dd{color:var(--muted);font-family:var(--mono);margin-top:2px}
.dh-note{margin-top:10px;font-size:12px;color:var(--gold);line-height:1.5}
/* ── Smart Read structure ────────────────────────────────────────────────
   FACT and INTERPRETATION are visually distinct, not just labelled, because
   a reader skimming will not read the label. What the article SAYS carries
   the page's normal text colour; what a model made of it is dimmer, indented
   behind a rule, and captioned. This is the only place on the site where a
   model writes interpretation, and it must never be mistakable for reporting. */
.sr-x{margin-top:12px;border-top:1px solid var(--line);padding-top:12px}
.sr-x dt{font-family:var(--mono);font-size:11px;letter-spacing:1.4px;
  text-transform:uppercase;color:var(--dim);margin-bottom:5px}
.sr-x dd{margin:0 0 12px;font-size:13px;line-height:1.55}
.sr-x dd:last-child{margin-bottom:0}
.sr-fact{color:var(--text)}
.sr-fact li{margin-bottom:5px}
.sr-interp{color:var(--muted);border-left:2px solid var(--line2);padding-left:10px}
.sr-x .sr-why dt{color:var(--gold)}
.sr-read{font-family:var(--mono);font-size:11px;color:var(--dim);letter-spacing:.5px}
/* The F-score, broken into its nine tests. Tick, cross and dash rather than
   colour alone — a reader who cannot distinguish red from green must still be
   able to read which criteria failed. */
.fsc{list-style:none;margin:10px 0 0;padding:0;display:grid;gap:5px}
.fsc li{font-size:12px;line-height:1.45;display:flex;gap:8px;align-items:baseline}
.fsc li b{font-family:var(--mono);font-size:12px;width:12px;flex:none}
.fsc li.mono-dim{color:var(--dim)}
/* "+2 more sources" on a clustered news card. Deliberately quiet — it is
   provenance, not a headline. */
.nalso{color:var(--dim);border-bottom:1px dotted var(--line2);cursor:help}
/* Theme control. Quiet by default — it is a preference, not a feature. */
.thm{background:var(--surface);border:1px solid var(--line);color:var(--muted);
  width:30px;height:30px;border-radius:6px;cursor:pointer;display:inline-flex;
  align-items:center;justify-content:center;font-size:13px;line-height:1;
  transition:background var(--m-micro) var(--ease),color var(--m-micro) var(--ease),
             border-color var(--m-micro) var(--ease)}
.thm:hover{background:var(--surface3);color:var(--text);border-color:var(--line2)}
.thm:focus-visible{outline:2px solid var(--lime);outline-offset:2px}
.thm-i{display:block;transition:transform var(--m-std) var(--ease)}
.thm:hover .thm-i{transform:rotate(18deg)}
/* Watchlist star. Quiet until set — a column of bright stars would compete
   with the numbers, which are the point of the table. */
.wcell{width:30px;padding-left:4px!important;padding-right:0!important}
.wstar{background:none;border:0;cursor:pointer;font-size:14px;line-height:1;
  color:var(--line2);padding:2px 4px;border-radius:4px;
  transition:color var(--m-micro) var(--ease),transform var(--m-micro) var(--ease)}
.wstar:hover{color:var(--muted);transform:scale(1.15)}
.wstar.on{color:var(--gold)}
.wstar:focus-visible{outline:2px solid var(--lime);outline-offset:1px}

.wbar{display:flex;align-items:center;gap:var(--s2);flex-wrap:wrap;
  margin:0 0 var(--s3);padding:var(--s3) var(--s4);border:1px solid var(--line);
  border-left:2px solid var(--gold);border-radius:6px;background:var(--surface)}
.wbar-n{font-family:var(--mono);font-size:var(--t-label);color:var(--muted);
  letter-spacing:1px;text-transform:uppercase;margin-right:auto}
.btn.ghost{background:none;border-color:transparent;color:var(--dim)}
.btn.ghost:hover{color:var(--text);border-color:var(--line)}

/* Comparison drawer. A drawer rather than a modal: the reader is comparing
   against the table behind it, and a modal that hides the source makes them
   close it to check. */
.cmp-back{position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:900;
  opacity:0;pointer-events:none;transition:opacity var(--m-std) var(--ease)}
.cmp-back.on{opacity:1;pointer-events:auto}
.cmp{position:fixed;top:0;right:0;bottom:0;width:min(880px,96vw);z-index:901;
  background:var(--overlay);border-left:1px solid var(--line2);
  transform:translateX(100%);transition:transform var(--m-panel) var(--ease);
  overflow-y:auto;padding:var(--s6) var(--s6) var(--s9)}
.cmp.on{transform:translateX(0)}
.cmp-h{display:flex;align-items:baseline;gap:var(--s3);margin-bottom:var(--s5)}
.cmp-h h3{font-size:var(--t-h2);font-weight:600}
.cmp-x{margin-left:auto;background:none;border:1px solid var(--line);
  color:var(--muted);border-radius:6px;width:28px;height:28px;cursor:pointer}
.cmp-x:hover{color:var(--text);border-color:var(--line2)}
.cmp table{width:100%;border-collapse:collapse;font-size:var(--t-body-sm)}
.cmp th,.cmp td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right}
.cmp th:first-child,.cmp td:first-child{text-align:left;color:var(--dim);
  font-family:var(--mono);font-size:var(--t-label);letter-spacing:.6px;
  text-transform:uppercase;white-space:nowrap}
.cmp thead th{color:var(--text);font-weight:600;font-size:var(--t-body);
  text-transform:none;letter-spacing:0;font-family:var(--sans)}
/* The win marker is a glyph AND a colour — a reader who cannot separate the
   two must still see who won a row. */
.cmp .win{color:var(--lime);font-weight:600}
.cmp .win::after{content:" \2713";font-size:11px}
.cmp-sum{margin-top:var(--s5);padding-top:var(--s4);border-top:1px solid var(--line2)}
.cmp-sum li{list-style:none;font-size:var(--t-body-sm);line-height:1.7;color:var(--muted)}
.cmp-sum b{color:var(--text)}
/* Findings. Cards rather than a table: each is a short argument, not a row. */
.fnd-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(280px,100%),1fr));gap:12px}
.fnd{padding:16px 18px}
.fnd-warn{border-left:2px solid var(--gold)}
.fnd-t{font-size:14px;margin:0 0 8px;font-weight:600;line-height:1.35}
.fnd-n{font-family:var(--mono);font-size:11px;color:var(--lime);margin-left:6px}
.fnd-r{font-family:var(--mono);font-size:11px;color:var(--dim);letter-spacing:.3px;
  margin:0 0 10px;line-height:1.5}
.fnd-d{font-size:13px;color:var(--muted);margin:0 0 8px;line-height:1.5}
.fnd-s{font-size:12px;line-height:1.7;margin:0}
.fnd-m,.fnd-w{font-size:12px;color:var(--muted);margin:8px 0 0;line-height:1.5}
.fnd-m b,.fnd-w b{color:var(--dim);font-family:var(--mono);font-size:11px;
  letter-spacing:1px;text-transform:uppercase;display:block;margin-bottom:3px}
/* The full-list disclosure. <summary> is 24px tall to clear WCAG 2.5.8 the
   same way the tap-target block below does, and carries a visible focus ring
   because it is genuinely keyboard-operable now. */
.fnd-all{margin-top:8px}
.fnd-all>summary{font-family:var(--mono);font-size:11px;color:var(--lime);
  letter-spacing:.4px;cursor:pointer;list-style:none;min-height:24px;
  display:flex;align-items:center}
.fnd-all>summary::-webkit-details-marker{display:none}
.fnd-all>summary::before{content:"▸";margin-right:6px;transition:transform .15s ease}
.fnd-all[open]>summary::before{transform:rotate(90deg)}
.fnd-all>summary:focus-visible{outline:2px solid var(--lime);outline-offset:2px}
.fnd-all .fnd-s{margin-top:8px;max-height:230px;overflow-y:auto}
@media (prefers-reduced-motion:reduce){.fnd-all>summary::before{transition:none}}
/* Multi-rule names. Left border in --lime, not --gold: --gold marks a
   contradiction (something is wrong), this marks agreement (several rules
   independently landed on the same company). */
.fnd-multi{border-left:2px solid var(--lime)}
.fnd-hits{margin:8px 0 0;padding-left:16px;font-size:12px;color:var(--muted);
  line-height:1.6}
.fnd-hits li{margin-bottom:2px}
/* ── Tap targets ─────────────────────────────────────────────────────────
   WCAG 2.2 AA 2.5.8 asks for 24x24 CSS px. Measured on a 375px viewport,
   64 controls were under it — mostly 16px-tall inline links in the footer
   and the provenance strips.

   Applied on POINTER-COARSE only. On a mouse a 16px link is perfectly
   clickable, and padding every inline link on desktop would loosen the
   dense, deliberate typography this page is built on. The problem is a
   finger, so the fix is scoped to fingers.

   inline-flex + min-height rather than padding: padding on an inline element
   does not grow its hit box vertically in a way that survives line-wrapping,
   which is exactly how these ended up at 16px in the first place. */
@media (pointer: coarse){
  a:not(.btn):not(.readmore),
  summary,
  .nav a,
  .prov a,
  footer a{
    min-height:24px;
    display:inline-flex;
    align-items:center;
  }
  /* Anchors that wrap mid-sentence must stay inline, or a link inside a
     paragraph becomes a block and breaks the line it sits in. */
  p a, li a, .sdesc a, .sr-s a{
    display:inline;
    min-height:0;
  }
}
/* The visually-hidden helper the new <label> uses. Present already for the
   honeypot fields; named here because a label must be reachable, not gone. */
.hp{position:absolute!important;width:1px;height:1px;overflow:hidden;
  clip:rect(0 0 0 0);white-space:nowrap}
@media(max-width:560px){.dh-src{margin-left:0;width:100%}}
/* Provenance strip. Every weekly artefact on this page showed its RESULT and
   not its VINTAGE, so "ran and found the same funds" and "did not run at all"
   rendered identically. One strip, stated the same way in every section that
   is rebuilt on a clock slower than the page. */
.prov{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;margin-top:12px;
  font-family:var(--mono);font-size:11px;letter-spacing:.6px;color:var(--dim)}
.prov b{color:var(--muted);font-weight:500}
.prov .pv-tag{border:1px solid var(--line2);border-radius:999px;padding:3px 9px}
.prov.stale{color:var(--gold)}
.prov.stale .pv-tag{border-color:var(--gold);color:var(--gold)}
/* ── read-more affordance, shared by news cards and smart reads ── */
.readmore{display:inline-block;margin-top:10px;font-family:var(--mono);font-size:11px;
  letter-spacing:1.1px;text-transform:uppercase;color:var(--lime);text-decoration:none;
  border-bottom:1px solid transparent}
.readmore:hover{border-bottom-color:var(--lime)}
.mini-s{margin:5px 0 0;font-size:12px;line-height:1.5;color:var(--muted)}
.ncard-f{display:flex;justify-content:space-between;align-items:center;gap:10px;
  margin-top:auto;padding-top:10px;flex-wrap:wrap}
.ncard-f .readmore{margin-top:0}
/* ── smart reads ── */
.sr-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(300px,100%),1fr));gap:14px}
.sr{border:1px solid var(--line);border-radius:14px;padding:16px 18px;background:var(--bg2);
  display:flex;flex-direction:column}
.sr-h{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-bottom:9px;
  font-family:var(--mono);font-size:11px;letter-spacing:1.1px;text-transform:uppercase}
.sr-src{color:var(--down);font-weight:600}
.sr-tag{color:var(--dim);border:1px solid var(--line2);border-radius:999px;padding:2px 8px}
/* One colour per Smart Reads category, so the mix is legible at a glance rather
   than something you have to read nine cards to establish. Colour is never the
   only signal — the tag carries the word too. */
.sr-money{color:var(--lime);border-color:rgba(195,245,60,.32)}
.sr-habits{color:var(--blue);border-color:rgba(106,168,255,.32)}
.sr-health{color:var(--up);border-color:rgba(61,220,151,.30)}
.sr-mind{color:var(--violet);border-color:rgba(167,139,250,.32)}
.sr-ideas{color:var(--gold);border-color:rgba(233,196,106,.32)}
.sr-date{color:var(--dim);margin-left:auto}
.sr-t{margin:0 0 8px;font-size:14px;line-height:1.4;font-weight:650}
.sr-t a{color:var(--fg);text-decoration:none;border-bottom:1px solid transparent}
.sr-t a:hover{color:var(--lime);border-bottom-color:var(--lime)}
.sr-s{margin:0;font-size:12px;line-height:1.6;color:var(--muted)}
.sr .readmore{margin-top:auto;padding-top:12px}
@media(max-width:640px){.sr-grid{grid-template-columns:1fr}}
/* ── podcasts ── */
.pod-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(320px,100%),1fr));gap:14px}
.pod{border:1px solid var(--line);border-radius:14px;padding:16px 18px;background:var(--bg2);
  display:flex;flex-direction:column;gap:8px}
.pod-h{display:flex;justify-content:space-between;align-items:center;gap:10px;
  font-family:var(--mono);font-size:11px;letter-spacing:1.1px;text-transform:uppercase}
.pod-cat{color:var(--lime);border:1px solid var(--line2);border-radius:999px;padding:3px 9px}
.pod-date{color:var(--dim)}
.pod-t{margin:0;font-size:14px;line-height:1.4;font-weight:600}
.pod-t a{color:var(--fg);text-decoration:none;border-bottom:1px solid transparent}
.pod-t a:hover{border-bottom-color:var(--lime);color:var(--lime)}
.pod-s{font-family:var(--mono);font-size:11px;color:var(--muted);letter-spacing:.4px}
.pod-s b{color:var(--fg);font-weight:500}
.pod-k{margin:2px 0 0;padding-left:16px;display:flex;flex-direction:column;gap:6px}
.pod-k li{font-size:12px;line-height:1.55;color:var(--muted)}
.pod-k li::marker{color:var(--lime)}
.pod-note{margin:16px 0 0;font-family:var(--mono);font-size:11px;color:var(--dim);line-height:1.6}
@media(max-width:640px){.pod-grid{grid-template-columns:1fr}}
.slink{font-family:var(--mono);font-size:11px;color:var(--dim);letter-spacing:1px;text-transform:uppercase;
  border-bottom:1px solid var(--line2);padding-bottom:3px;transition:color .25s,border-color .25s}
.slink:hover{color:var(--lime);border-color:var(--lime)}

/* reveal */
.rv{opacity:0;transform:translateY(28px);transition:opacity .75s var(--ease),transform .75s var(--ease);
  transition-delay:var(--d,0s);}
.rv.in{opacity:1;transform:none}

/* ═══════════════════ CARD PRIMITIVE ═══════════════════ */
.card{position:relative;background:var(--surface);border:1px solid var(--line);border-radius:16px;
  padding:22px;transition:border-color .35s var(--ease),transform .35s var(--ease),background .35s;}
.card:hover{border-color:var(--line2);transform:translateY(-3px);background:var(--surface2)}
.card::before{content:'';position:absolute;top:0;left:22px;right:22px;height:1px;
  background:linear-gradient(90deg,transparent,var(--lime),transparent);opacity:0;transition:opacity .4s}
.card:hover::before{opacity:.6}
.tag{display:inline-block;font-family:var(--mono);font-size:11px;letter-spacing:1.4px;text-transform:uppercase;
  padding:3px 8px;border-radius:5px;background:var(--lime-soft);color:var(--lime);border:1px solid var(--lime-line)}
</style>
<style>
/* The .mkt / .mkt-grid rules that lived here belonged to the "What moved"
   section, which duplicated the ticker and has been removed. */

/* ═══════════════════ 01 PICKS ═══════════════════ */
.pick-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(280px,100%),1fr));gap:14px}
.pick{position:relative;background:linear-gradient(160deg,var(--surface),var(--pick-edge));border:1px solid var(--line);
  border-radius:18px;padding:22px;overflow:hidden;transition:border-color .35s,transform .35s var(--ease)}
.pick:hover{border-color:var(--lime-line);transform:translateY(-4px)}

/* ══════════ THE LEAD IDEA ══════════
   Five equally-sized cards said the fifth idea was worth the same attention as
   the first, and the grid put the fifth alone on a second row with three empty
   cells beside it. Rank 01 now occupies a 2x2 block: same markup, same fields,
   same detail — nothing is summarised away — but at a size that matches what
   the score already claims about it.

   Guarded on .has-lead (set only at five ideas) and on a width wide enough for
   four columns. Below that the grid stays uniform, because a double-width card
   in a two-column grid is just a full-width card with a gap under it. */
.pick-lead{display:none}
@media(min-width:1080px){
  .pick-grid.has-lead{grid-template-columns:repeat(4,1fr)}
  .pick-grid.has-lead > .pick:first-child{grid-column:span 2;grid-row:span 2;padding:30px 32px}
  .pick-grid.has-lead > .pick:first-child .rank{font-size:104px;top:-20px;right:18px}
  .pick-grid.has-lead > .pick:first-child .sym{font-size:24px}
  .pick-grid.has-lead > .pick:first-child .px{font-size:clamp(40px,3.6vw,54px);letter-spacing:-2.4px}
  .pick-grid.has-lead > .pick:first-child .mom{font-size:12px;gap:18px;margin-top:10px}
  .pick-grid.has-lead > .pick:first-child .th{font-size:14px;margin:20px 0}
  .pick-grid.has-lead > .pick:first-child .inval{font-size:13px}
  /* The label is the only new content on the card, and it exists because size
     alone does not say WHY this one is bigger. */
  .pick-grid.has-lead > .pick:first-child .pick-lead{display:inline-block;
    font-family:var(--mono);font-size:var(--t-overline);font-weight:700;
    letter-spacing:2px;text-transform:uppercase;color:var(--lime);
    background:var(--lime-soft);border:1px solid var(--lime-line);
    border-radius:4px;padding:4px 9px;margin-bottom:var(--s3)}
}
.pick .rank{position:absolute;top:-14px;right:10px;font-family:var(--mono);font-size:58px;font-weight:700;
  color:var(--rank-ink);line-height:1;pointer-events:none}
.pick .sym{font-family:var(--mono);font-size:16px;font-weight:700;letter-spacing:-.4px}
.pick .px{font-family:var(--mono);font-size:32px;font-weight:700;letter-spacing:-1.6px;margin:6px 0 2px}
.pick .mom{display:flex;gap:12px;font-family:var(--mono);font-size:11px;color:var(--muted);margin-top:6px;flex-wrap:wrap}
.pick .mom b{font-weight:600}
.pick .th{font-size:12px;color:var(--muted);line-height:1.6;margin:14px 0;font-style:italic;
  border-left:2px solid var(--line2);padding-left:11px}
.lvl{display:flex;gap:10px;margin-top:14px;padding-top:14px;border-top:1px solid var(--line)}
.lvl>div{flex:1}
.lvl .k{font-size:11px;letter-spacing:1.4px;text-transform:uppercase;color:var(--dim);margin-bottom:3px}
.lvl .v{font-family:var(--mono);font-size:14px;font-weight:700}
.scorebar{height:3px;background:rgba(255,255,255,.06);border-radius:3px;overflow:hidden;margin-top:12px}
.scorebar i{display:block;height:100%;background:linear-gradient(90deg,var(--lime),#7ED321);width:0;
  transition:width 1.2s var(--ease) .2s;border-radius:3px}
.rv.in .scorebar i{width:var(--w)}

/* Score breakdown. <details> rather than a click handler so it works with the
   script blocked, which is the same reader the SSR fixes above are for. */
.why{margin-top:12px}
.why summary{cursor:pointer;list-style:none;font-family:var(--mono);font-size:11px;
  letter-spacing:1.2px;text-transform:uppercase;color:var(--dim);padding:5px 0;
  transition:color .2s var(--ease)}
.why summary::-webkit-details-marker{display:none}
.why summary::after{content:' ▾';font-size:11px}
.why[open] summary::after{content:' ▴'}
.why summary:hover{color:var(--lime)}
.why summary span{color:#4A4F57}
.why-b{padding:4px 0 2px;display:grid;gap:5px}
.why-r{display:grid;grid-template-columns:1fr 54px auto;align-items:center;gap:8px;font-size:11px}
.why-r .wk{color:var(--muted)}
.why-r .wb{height:3px;border-radius:2px;background:rgba(255,255,255,.07);overflow:hidden}
.why-r .wb i{display:block;height:100%;width:var(--w);background:var(--lime);opacity:.75;border-radius:2px}
.why-r .wn{font-family:var(--mono);font-size:11px;color:var(--text);text-align:right}
.why-r .wn em{font-style:normal;color:#4A4F57}

/* The level that ends the idea. Gold, not red — it has not happened. */
.inval{margin-top:12px;font-size:11px;line-height:1.6;color:var(--muted);
  border-left:2px solid rgba(224,178,74,.45);padding-left:11px}
.inval b{color:var(--gold);font-weight:600;letter-spacing:.3px}

/* ═══════════════════ STOCK SCREEN ═══════════════════
   Reuses .tw / .tw-tall / table.t / .ctlbar / .fbtn / .fund-note wholesale —
   this section introduces no table, control bar or note styling of its own.
   What is genuinely new is only the score cell (a number that has to carry its
   own confidence) and the detail sheet's ratio grid. */

/* A score with a bar behind it. The bar is not decoration: four scores across
   fifteen columns are unreadable as bare numbers, and the eye needs to rank a
   column at a glance without reading it. */
.sc{position:relative;font-family:var(--mono);font-variant-numeric:tabular-nums;
  font-size:12px;font-weight:700;min-width:52px;display:inline-block;
  padding:3px 7px;border-radius:5px;text-align:right}
.sc i{position:absolute;left:0;top:0;bottom:0;border-radius:5px;z-index:0;
  background:var(--lime-soft)}
.sc b{position:relative;z-index:1;font-weight:700}
.sc.s-hi b{color:var(--lime)}
.sc.s-md b{color:var(--text)}
.sc.s-lo b{color:var(--dim)}
/* Confidence is a real property of every score here — one built from two of
   five inputs must not look like one built from five. Dotted underline rather
   than a second number, which would double the width of four columns. */
.sc.thin{border-bottom:1px dotted var(--gold)}

/* Breadth strip. Its own block rather than a .prov because it carries data, not
   provenance — a reader should not have to distinguish "when was this built"
   from "what does it say" by squinting at two identically styled rows. */
.scr-breadth{background:var(--bg2);border:1px solid var(--line);border-left:2px solid var(--lime);
  border-radius:0 10px 10px 0;padding:13px 17px;margin-bottom:18px;
  display:flex;flex-wrap:wrap;gap:10px 26px;align-items:baseline}
.scr-breadth .sb-k{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.scr-breadth .sb-lab{font-family:var(--mono);font-size:11px;letter-spacing:1.6px;
  color:var(--dim);border:1px solid var(--line2);border-radius:999px;padding:3px 9px}
.scr-breadth .sb-reg{font-family:var(--mono);font-size:13px;letter-spacing:1.2px;color:var(--lime)}
.scr-breadth .sb-as{font-family:var(--mono);font-size:11px;color:var(--dim)}
.scr-breadth .sb-n{display:flex;flex-wrap:wrap;gap:6px 18px;font-family:var(--mono);
  font-size:11px;color:var(--muted)}
.scr-breadth .sb-n b{color:var(--text);font-weight:700}

/* AI narrative. Marked as generated, and visually subordinate to the computed
   SWOT above it — the prose is the commentary, the numbers are the evidence. */
.sd-ai{background:rgba(106,168,255,.05);border:1px solid rgba(106,168,255,.22);
  border-radius:10px;padding:13px 15px}
.sd-ai .tag{font-family:var(--mono);font-size:11px;letter-spacing:1.4px;
  color:var(--blue);display:block;margin-bottom:7px}
.sd-ai p{font-size:13px;line-height:1.65;color:var(--muted);margin:0}
.sd-ai .fine{font-family:var(--mono);font-size:11px;color:var(--dim);margin-top:8px;display:block}

/* Peer-median column in the detail sheet's ratio grid. */
.sd-peer{display:grid;grid-template-columns:1fr auto auto;gap:7px 14px;
  font-family:var(--mono);font-size:12px;align-items:baseline}
.sd-peer .h{font-size:11px;letter-spacing:1.3px;color:var(--dim);text-transform:uppercase}
.sd-peer .k{color:var(--muted)}
.sd-peer .v{color:var(--text);text-align:right;font-weight:700}
.sd-peer .m{color:var(--dim);text-align:right}
.sd-peer .v.better{color:var(--up)} .sd-peer .v.worse{color:var(--down)}

/* Risk level. LOW/MEDIUM/HIGH, never a 0-100 — an arbitrary "risk 62" is
   unreadable without also knowing which direction is better. Colour is never the
   only signal: the word is always there. */
.rk{font-family:var(--mono);font-size:11px;letter-spacing:1px;font-weight:700;
  border-radius:4px;padding:2px 7px;border:1px solid}
.rk-low{color:var(--up);border-color:rgba(61,220,151,.32);background:rgba(61,220,151,.07)}
.rk-medium{color:var(--gold);border-color:rgba(233,196,106,.34);background:rgba(233,196,106,.07)}
.rk-high{color:var(--down);border-color:rgba(255,92,92,.34);background:rgba(255,92,92,.08)}
.rk-n{font-family:var(--mono);font-size:11px;color:var(--dim);margin-left:5px}
/* Cash is not a risk level, so it gets the neutral pill rather than borrowing
   the "low risk" green — uninvested money is an absence of a position, not a
   safe one. */
.rk-idle{color:var(--dim);border-color:var(--line2);background:var(--surface2)}
/* Earnings momentum: the DIRECTION of the accounts. A level and a direction
   are different facts — a 25% compounder that is slowing and a 12% one that
   is speeding up have the same CAGR column. */
.em{font-family:var(--mono);font-size:11px;letter-spacing:1.2px;font-weight:700;
  border-radius:4px;padding:2px 7px;border:1px solid;margin-left:4px}
.em-accelerating{color:var(--up);border-color:rgba(61,220,151,.34);background:rgba(61,220,151,.07)}
.em-stable{color:var(--dim);border-color:var(--line2)}
.em-decelerating{color:var(--down);border-color:rgba(255,92,92,.34);background:rgba(255,92,92,.07)}
/* Movement since the previous build. A 91 that was a 91 is priced; a 78 that
   was a 61 is a change, and the change is the interesting part. */
.dl{display:block;font-family:var(--mono);font-size:11px;font-style:normal;
  letter-spacing:.4px;margin-top:2px}
.dl-up{color:var(--up)} .dl-dn{color:var(--down)}
.dl-new{color:var(--lime);border:1px solid rgba(195,245,60,.3);border-radius:3px;
  padding:0 4px;display:inline-block}
/* Capital allocation, out of 10 rather than 100 — it is a judgement over six
   coarse inputs, and two significant figures would imply a precision it does
   not have. */
.ca{font-family:var(--mono);font-size:11px;color:var(--lime);letter-spacing:.5px;
  border:1px solid rgba(195,245,60,.28);border-radius:4px;padding:1px 7px;margin-left:4px}

/* Why now / what can go wrong, side by side. The two columns exist so the case
   for and the case against are read together rather than one scrolled past. */
.sd-why{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.wn-col{background:var(--bg2);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.wn-col>span{font-family:var(--mono);font-size:11px;letter-spacing:1.4px;
  text-transform:uppercase;display:block;margin-bottom:9px}
.wn-for>span{color:var(--up)} .wn-against>span{color:var(--gold)}
.wn-col ul{list-style:none;margin:0;padding:0;display:grid;gap:9px}
.wn-col li{font-size:12px;line-height:1.5;color:var(--muted);padding-left:13px;position:relative}
.wn-for li::before{content:"+";position:absolute;left:0;color:var(--up);font-weight:700}
.wn-col li em{display:block;font-family:var(--mono);font-size:11px;color:var(--dim);
  font-style:normal;margin-top:3px}
/* Severity on the against side, so a solvency flag does not read like a wide
   spread. Prefix character AND colour, never colour alone. */
.wn-against li{padding-left:15px}
.wn-against li.f-high::before{content:"!!";position:absolute;left:0;color:var(--down);
  font-family:var(--mono);font-size:11px;font-weight:700}
.wn-against li.f-med::before{content:"!";position:absolute;left:0;color:var(--gold);
  font-family:var(--mono);font-weight:700}
.wn-against li.f-low::before{content:"·";position:absolute;left:0;color:var(--dim)}
@media(max-width:700px){.sd-why{grid-template-columns:1fr}}

.scr-tags{display:flex;flex-wrap:wrap;gap:4px}
.scr-tag{font-family:var(--mono);font-size:11px;letter-spacing:.8px;padding:2px 6px;
  border-radius:4px;border:1px solid var(--line2);color:var(--dim);white-space:nowrap}
.scr-tag.t-brk{border-color:rgba(195,245,60,.35);color:var(--lime)}
.scr-tag.t-vol{border-color:rgba(106,168,255,.35);color:var(--blue)}
.scr-tag.t-os{border-color:rgba(255,92,92,.32);color:var(--down)}
.scr-tag.t-rs{border-color:rgba(61,220,151,.32);color:var(--up)}

table.t th.sortable{cursor:pointer;user-select:none;white-space:nowrap}
table.t th.sortable:hover{color:var(--text)}
table.t th.sortable[aria-sort]{color:var(--lime)}
table.t th.sortable[aria-sort]::after{content:" ▾"}
table.t th.sortable[aria-sort=ascending]::after{content:" ▴"}

.scr-empty{font-family:var(--mono);font-size:12px;color:var(--dim);
  padding:34px 18px;text-align:center}
.scr-more{display:block;width:100%;margin:14px 0 0;padding:13px;
  background:var(--bg2);border:1px solid var(--line);border-radius:12px;
  font-family:var(--mono);font-size:11px;letter-spacing:1.4px;text-transform:uppercase;
  color:var(--muted);cursor:pointer;min-height:44px}
.scr-more:hover{border-color:var(--lime-line);color:var(--lime)}

/* ── detail sheet ── */
.sd-h{display:flex;flex-wrap:wrap;gap:6px 16px;align-items:baseline;margin-bottom:4px}
.sd-h h3{font-size:19px;margin:0;font-family:var(--mono)}
.sd-h .co{color:var(--muted);font-size:13px}
.sd-sub{font-family:var(--mono);font-size:11px;color:var(--dim);letter-spacing:.6px;
  margin-bottom:18px;display:flex;flex-wrap:wrap;gap:4px 12px}
.sd-scores{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(112px,100%),1fr));
  gap:9px;margin-bottom:20px}
.sd-sc{background:var(--bg2);border:1px solid var(--line);border-radius:10px;padding:11px 13px}
.sd-sc .k{font-family:var(--mono);font-size:11px;letter-spacing:1.3px;text-transform:uppercase;
  color:var(--dim);display:block;margin-bottom:5px}
.sd-sc .v{font-family:var(--mono);font-size:19px;font-weight:700;color:var(--text)}
/* display:block is load-bearing, not tidiness. This is a <span>, and `height`
   does not apply to an inline box — so the 3px was ignored, the inner <i> at
   height:100% had no bound to resolve against, and each of the five score
   tiles rendered a ~60px slab of lime that covered the heading underneath. */
.sd-sc .bar{display:block;height:3px;border-radius:2px;background:var(--line);
  margin-top:7px;overflow:hidden}
.sd-sc .bar i{display:block;height:100%;background:var(--lime)}
.sd-sc.wide{grid-column:1/-1}
.sd-sc .conf{font-family:var(--mono);font-size:11px;color:var(--gold);margin-top:5px;display:block}

.sd-blk{margin:0 0 20px}
.sd-blk h4,.sd-blk .fh4{font-family:var(--mono);font-size:11px;letter-spacing:1.7px;text-transform:uppercase;
  color:var(--lime);margin:0 0 9px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.sd-blk p{font-size:13px;line-height:1.65;color:var(--muted);margin:0}

.sd-swot{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.sd-q{background:var(--bg2);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.sd-q>span{font-family:var(--mono);font-size:11px;letter-spacing:1.4px;text-transform:uppercase;
  display:block;margin-bottom:8px}
.sd-q.q-s>span{color:var(--up)} .sd-q.q-w>span{color:var(--down)}
.sd-q.q-o>span{color:var(--blue)} .sd-q.q-t>span{color:var(--gold)}
.sd-q ul{list-style:none;margin:0;padding:0;display:grid;gap:9px}
.sd-q li{font-size:12px;line-height:1.55;color:var(--muted)}
/* The evidence line is the point of the whole SWOT: every claim above it is
   generated from this number, so it travels with the claim and never gets
   collapsed away on small screens. */
.sd-q li em{display:block;font-family:var(--mono);font-size:11px;color:var(--dim);
  font-style:normal;margin-top:3px}
.sd-q.empty{display:none}

.sd-upd{list-style:none;margin:0;padding:0;display:grid;gap:8px}
.sd-upd li{font-family:var(--mono);font-size:12px;line-height:1.55;color:var(--muted);
  padding-left:16px;position:relative}
.sd-upd li::before{content:"›";position:absolute;left:0;color:var(--dim)}
.sd-upd li.k-good::before{content:"▲";color:var(--up);font-size:11px;top:3px}
.sd-upd li.k-bad::before{content:"▼";color:var(--down);font-size:11px;top:3px}
.sd-upd li.k-warn::before{content:"!";color:var(--gold);font-weight:700}

.sd-news{list-style:none;margin:0;padding:0;display:grid;gap:10px}
.sd-news a{font-size:12px;line-height:1.5;color:var(--muted);display:block}
.sd-news a:hover{color:var(--lime)}
.sd-news .m{font-family:var(--mono);font-size:11px;color:var(--dim);margin-top:2px;display:block}

@media(max-width:700px){
  .sd-swot{grid-template-columns:1fr}
  .sd-h h3{font-size:19px}
}

/* ═══════════════════ 03 SIGNAL LOG ═══════════════════ */
.kpi-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(130px,100%),1fr));gap:1px;background:var(--line);
  border:1px solid var(--line);border-radius:16px;overflow:hidden;margin-bottom:22px}
.kpi{background:var(--surface);padding:20px 18px}
.kpi .v{font-family:var(--mono);font-size:clamp(22px,3vw,32px);font-weight:700;letter-spacing:-1.2px;line-height:1}
.kpi .k{font-size:11px;letter-spacing:1.6px;text-transform:uppercase;color:var(--dim);margin-top:8px;font-weight:500}
.filters{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:16px}
.fbtn{padding:7px 15px;font-family:var(--mono);font-size:11px;letter-spacing:1.2px;text-transform:uppercase;
  background:transparent;border:1px solid var(--line);color:var(--muted);cursor:pointer;border-radius:100px;
  transition:all .25s var(--ease)}
.fbtn:hover{border-color:var(--line2);color:var(--text)}
.fbtn.on{border-color:var(--lime);color:var(--on-brand);background:var(--lime);font-weight:700}
/* No -webkit-overflow-scrolling:touch: that's a pre-iOS-13 property for
   momentum scroll on an overflow container, which modern WebKit already
   does natively. Left on, it's a known cause of exactly the bug it caused
   here — position:sticky descendants (table.t th below) losing their
   stuck offset specifically on scroll-direction reversal, because the
   legacy property forces its own compositing path that doesn't always
   resync the sticky offset on fling/scroll-up. */
/* Every table is its own scroll region, and its header sticks to the top of
   THAT box — never to the viewport.

   The viewport-sticky version is what put a column-header row in the middle of
   Corporate Actions, SIP Buckets and the Paper Wallet, floating over a row and
   hiding it. It stuck at top:var(--headh), which had to equal the height of a
   .headstack that grows asynchronously as the ticker fills; any drift, at any
   viewport width, on any late paint, put every header on the site in the wrong
   place at once. That is too much machinery to keep correct for a column label.

   top:0 inside a bounded box needs nothing measured and cannot drift. max-height
   only engages once a table is actually taller than it, so short tables are
   untouched and keep growing with the page. */
.tw{overflow:auto;border:1px solid var(--line);border-radius:16px;max-height:min(78vh,760px);
  overscroll-behavior:contain}
/* The signal log is 87 rows and growing, and `position:sticky` on its <th> did
   nothing: .tw sets overflow-x, which makes overflow-y compute to auto, so .tw
   IS the scroll container — and an unbounded container has no top edge to
   stick to. You scrolled the PAGE, the whole table moved, and the header left
   with it, so by row 20 the columns were unlabelled.
   Bounding the height gives the header something to stick to and turns the
   table into its own scroll region. Applied only here; short tables elsewhere
   must keep growing with the page. */
.tw-tall{max-height:min(78vh,780px);overflow-y:auto}
/* Inside .tw-tall the wrapper is its own scroll container, so top:0 means the
   top of THAT box and the header behaves. It must override the viewport
   offset set on `table.t th` below. */
.tw-tall table.t th{top:0;z-index:5;box-shadow:inset 0 -1px 0 var(--line2)}
table.t{width:100%;border-collapse:collapse;font-size:var(--t-table-dense);min-width:900px}
/* top:0, scoped to the .tw box above — see the note there. --headh is no longer
   involved in table headers at all; it still backs scroll-padding-top and
   section scroll-margin for anchor jumps, where being a few px out is invisible
   rather than a header landing in the middle of a table. */
/* The header was set in the SANS face here and the mono face in .tblwrap —
   the same row of column labels, two typefaces, decided by which selector
   won. Mono is the right answer for both: these labels sit directly above
   columns of tabular figures, and a mono label matches the grid its column
   is set on. Tracking moves 1.4px -> --tbl-track for the same reason: .13em
   was used four rules away and the two were never the same number. */
table.t th{position:sticky;top:0;z-index:5;background:var(--surface);text-align:left;
  font:600 var(--t-table-h)/1.35 var(--mono);letter-spacing:var(--tbl-track);
  text-transform:uppercase;color:var(--dim);
  padding:var(--tbl-pad-y-h) var(--tbl-pad-x);
  /* --line2, not --line. The light theme already used --line2 here, so the
     header rule was HEAVIER than the row rules on paper and LIGHTER than
     them on screen: the same table had its hierarchy inverted between the
     two themes. Both now read header-heavy, rows-light. */
  border-bottom:1px solid var(--line2);z-index:2}
/* rgba(255,255,255,.04) was hardcoded here, and it is the single worst line
   in the old table CSS. Four percent white is below the visible threshold on
   this ground, so in DARK mode the row rules were effectively absent and the
   table read as a floating block of digits. In LIGHT mode the theme override
   (higher specificity) replaced it with a real value, so the same table had
   row rules on paper and none on screen. A hardcoded white alpha cannot
   survive a theme switch — that is what the token is for. */
table.t td{padding:var(--tbl-pad-y) var(--tbl-pad-x);border-bottom:1px solid var(--line);vertical-align:middle}
table.t tbody tr{transition:background .2s}
table.t tbody tr:hover{background:rgba(255,255,255,.025)}
table.t tbody tr:last-child td{border-bottom:none}
.badge{display:inline-flex;align-items:center;gap:5px;padding:4px 10px;font-family:var(--mono);font-size:11px;
  font-weight:700;letter-spacing:1px;text-transform:uppercase;border-radius:100px;white-space:nowrap}
.badge-win{background:rgba(61,220,151,.12);color:var(--up);border:1px solid rgba(61,220,151,.3)}
.badge-loss{background:rgba(255,92,92,.1);color:var(--down);border:1px solid rgba(255,92,92,.28)}
.badge-open{background:rgba(106,168,255,.1);color:var(--blue);border:1px solid rgba(106,168,255,.28)}
/* Unrealised P&L on an open wallet row. Deliberately quieter than a booked
   result — dimmer, italic percentage, a dotted underline that invites the
   tooltip naming the mark price. An open position's mark is not a result, and
   rendering the two identically is the same error as counting open trades in a
   win rate. */
.wal-live{border-bottom:1px dotted var(--line2);cursor:help;opacity:.86}
/* ═══════════════════ IPO RADAR ═══════════════════
   Cards, not a table: each issue carries a verdict, a reason, a counter-reason
   and a list of what was NOT measured, and none of that survives being flattened
   into columns. The verdict colour is a left border rather than a filled card —
   an APPLY should read as confident, not as an advertisement. */
.ipo-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(340px,100%),1fr));gap:14px}
.ipo-card{background:var(--surface);border:1px solid var(--line);border-left:3px solid var(--line2);
  border-radius:14px;padding:17px 19px;transition:border-color .25s,transform .25s}
.ipo-card:hover{transform:translateY(-2px)}
.ipo-apply{border-left-color:var(--up)}
.ipo-avoid{border-left-color:var(--down)}
.ipo-watch{border-left-color:var(--gold)}
.ipo-top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:14px}
.ipo-co{font-size:12px;color:var(--muted);line-height:1.4;margin-top:3px;max-width:34ch}
.ipo-verdict{font-family:var(--mono);font-size:11px;font-weight:700;letter-spacing:1.1px;
  padding:5px 10px;border-radius:100px;white-space:nowrap;border:1px solid currentColor}
.ipo-apply .ipo-verdict{color:var(--up);background:rgba(61,220,151,.1)}
.ipo-avoid .ipo-verdict{color:var(--down);background:rgba(255,92,92,.1)}
.ipo-watch .ipo-verdict{color:var(--gold);background:rgba(230,180,80,.1)}
.ipo-facts{display:grid;grid-template-columns:1fr 1fr;gap:9px 14px;margin-bottom:12px}
.ipo-facts>div{display:flex;flex-direction:column;gap:2px}
.ipo-facts .k{font-family:var(--mono);font-size:11px;letter-spacing:1.1px;text-transform:uppercase;color:var(--dim)}
.ipo-facts .v{font-size:13px;font-variant-numeric:tabular-nums}
.ipo-cats{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:12px}
.ipo-cat-hot{color:var(--up)!important;border-color:rgba(61,220,151,.3)!important}
.ipo-cat-cold{color:var(--down)!important;border-color:rgba(255,92,92,.28)!important}
.ipo-cat-src{color:var(--dim)!important;border-style:dashed!important}
.ipo-cats span{font-family:var(--mono);font-size:11px;color:var(--muted);
  background:var(--bg2);border:1px solid var(--line);border-radius:100px;padding:3px 9px}
.ipo-score{margin-bottom:12px}
.ipo-score-bar{height:4px;background:var(--bg2);border-radius:100px;overflow:hidden;margin-bottom:7px}
/* Width is inline from the template; the transition makes it draw in rather
   than snap, which is the only motion on this card. */
.ipo-score-bar i{display:block;height:100%;background:var(--lime);border-radius:100px;
  transition:width .9s cubic-bezier(.22,1,.36,1)}
.ipo-score-n{font-family:var(--mono);font-size:11px;color:var(--text);margin-right:8px}
.ipo-score-parts{font-family:var(--mono);font-size:11px;color:var(--dim)}
.ipo-why,.ipo-caveat,.ipo-missing{font-size:12px;line-height:1.55;margin:0 0 7px}
.ipo-why{color:var(--text)}
.ipo-caveat{color:var(--muted)}
/* The list of gaps is deliberately legible, not hidden in a tooltip: what a
   score does NOT include is part of reading the score. */
.ipo-missing{font-family:var(--mono);font-size:11px;color:var(--dim);line-height:1.6;
  border-top:1px solid var(--line);padding-top:9px;margin-top:11px}
/* The screens are a multi-select that ANDs, and nothing on screen said so —
   readers picked one, saw the list change, and assumed it was a radio group. */
.ctl-hint{display:block;font-family:var(--mono);font-size:11px;letter-spacing:.4px;
  text-transform:none;color:var(--dim);margin-top:2px;font-weight:400}
/* Business performance. Boxed apart from the issue mechanics above it because
   these are the company's accounts, not the offer's terms — and because none of
   them enter the score. */
.ipo-fin{background:var(--bg2);border:1px solid var(--line);border-radius:10px;
  padding:11px 13px;margin-bottom:12px}
.ipo-fin-h{font-family:var(--mono);font-size:11px;letter-spacing:1.2px;text-transform:uppercase;
  color:var(--lime);margin-bottom:9px}
.ipo-fin-g{display:grid;grid-template-columns:1fr 1fr;gap:8px 14px}
/* The sector, in the eyebrow beside the financial year. Chittorgarh's own
   classification of the issue, which the site had been reporting as
   unavailable — it is not a labelled field anywhere on the page, it is the
   heading of the "Recently Listed IPOs in ..." block. Set apart from the
   uppercase eyebrow around it because it is a proper noun, not a label. */
.ipo-sector{text-transform:none;letter-spacing:0;color:var(--muted);font-family:var(--sans)}
.ipo-fin-g>div{display:flex;flex-direction:column;gap:1px}
.ipo-fin-g .k{font-family:var(--mono);font-size:11px;letter-spacing:1px;
  text-transform:uppercase;color:var(--dim)}
.ipo-fin-g .v{font-size:12px;font-variant-numeric:tabular-nums}
.ipo-fin-g .v i{font-style:normal;font-family:var(--mono);font-size:11px}
/* The margin warning is gold, not red: a thin margin is a characteristic of the
   business model, not an error. */
.ipo-fin-w{font-size:11px;line-height:1.55;color:var(--gold);margin:10px 0 0;
  border-left:2px solid rgba(230,180,80,.35);padding-left:9px}
.ipo-fin-u{font-size:11px;line-height:1.5;color:var(--muted);margin:8px 0 0}
/* The two-sided argument, side by side so neither reads as the conclusion. */
.ipo-args-src{font-family:var(--mono);font-size:11px;line-height:1.6;color:var(--dim);
  margin:0 0 12px}
.ipo-args{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px}
.ipo-arg{border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.ipo-arg b{font-family:var(--mono);font-size:11px;letter-spacing:1px;text-transform:uppercase;
  display:block;margin-bottom:6px}
.ipo-arg-y{border-left:2px solid var(--up)} .ipo-arg-y b{color:var(--up)}
.ipo-arg-n{border-left:2px solid var(--down)} .ipo-arg-n b{color:var(--down)}
.ipo-arg ul{margin:0;padding-left:15px}
.ipo-arg li{font-size:11px;line-height:1.5;color:var(--muted);margin-bottom:4px}
@media(max-width:560px){.ipo-args{grid-template-columns:1fr}.ipo-fin-g{grid-template-columns:1fr}}
.ipo-drv{font-family:var(--mono);font-size:11px;letter-spacing:.5px;text-transform:uppercase;
  color:var(--dim);border:1px solid var(--line);border-radius:3px;padding:1px 4px;margin-left:5px;cursor:help}
/* Grey market. Boxed and visually cooler than every other fact on the card,
   because it is the one number here with no official source behind it. */
.ipo-gmp{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap;background:var(--bg2);
  border:1px dashed var(--line2);border-radius:9px;padding:8px 12px;margin-bottom:12px}
.ipo-gmp-k{font-family:var(--mono);font-size:11px;letter-spacing:1.1px;text-transform:uppercase;color:var(--dim)}
.ipo-gmp-v{font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums}
.ipo-gmp-w{font-family:var(--mono);font-size:11px;color:var(--dim);margin-left:auto}
@media(max-width:520px){.ipo-facts{grid-template-columns:1fr}.ipo-gmp-w{margin-left:0}}
/* Paper-wallet tier framework. Three cards explaining WHY a tier exists, above
   the rule list that states WHAT its caps are. Separated because they answer
   different questions and a reader needs the first to make sense of the second. */
.wal-tiers{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(260px,100%),1fr));gap:12px}
.wal-tier{background:var(--bg2);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.wal-tier-h{display:flex;justify-content:space-between;align-items:baseline;gap:10px;
  flex-wrap:wrap;margin-bottom:8px}
.wal-tier-n{font-family:var(--mono);font-size:11px;color:var(--lime);white-space:nowrap}
.wal-tier-w{font-size:12px;line-height:1.6;color:var(--muted);margin:0 0 10px}
.wal-tier-e{display:flex;flex-wrap:wrap;gap:5px}
.wal-tier-e span{font-family:var(--mono);font-size:11px;color:var(--dim);
  background:var(--surface);border:1px solid var(--line);border-radius:100px;padding:3px 8px}
/* Why a row got the size it got. Quiet by default, full arithmetic on hover —
   the number is the answer, the rule behind it is the explanation. */
/* Status line under the Data Health heading. The heading states the system's
   condition; this states the exact counts. Green when the degraded feeds are
   all non-market. */
/* Hero byline. Mono and small on purpose — it is provenance, not a headline,
   and it has to sit under the statement without competing with it. */
/* The interpretation layer on a world card. Set apart from the wire summary
   above it, because one is what a newsroom reported and the other is a reading
   of it — collapsing them visually would let the second borrow the first's
   authority. */
/* Content that belongs to the section above it but is not a section of its own.
   Keeps the page's section count honest — the structure tests assert that every
   <section> is declared in the nav, and a nav entry per subsection is exactly
   the menu pollution this page already has too much of. */
.sec-append{max-width:var(--wrap,1400px);margin:0 auto;padding:0 clamp(18px,4vw,54px) clamp(40px,6vw,80px)}
/* A listing that exists but cannot be measured. Dimmer, not hidden: it is a
   real IPO, and dropping it would understate the population every verdict on
   this page is eventually judged against. */
.tv-lnk{font-family:var(--mono);font-size:11px;letter-spacing:.5px;text-transform:uppercase;
  color:var(--dim);border:1px solid var(--line);border-radius:3px;padding:1px 4px;margin-left:6px;
  text-decoration:none;white-space:nowrap}
.tv-lnk:hover{color:var(--lime);border-color:var(--lime)}
.ipo-unmeasured td{opacity:.6}
.ipo-unmeasured .sym{cursor:default}
.ev-v{font-family:var(--mono);font-size:11px;font-weight:700;letter-spacing:1px;
  padding:3px 8px;border-radius:100px;border:1px solid currentColor;cursor:help;white-space:nowrap}
.ev-edge{color:var(--up);background:rgba(61,220,151,.1)}
.ev-bleeding{color:var(--down);background:rgba(255,92,92,.1)}
.ev-unproven{color:var(--gold);background:rgba(230,180,80,.1)}
.ev-flat{color:var(--dim);background:var(--bg2)}
/* Decision board. Four states at a glance, above everything. Deliberately
   plain — no borders competing with the cards below, just a rule between
   tiles — because it is the first thing read and the last thing that should
   be decorated. */
/* Mobile type floor. A phone audit found 91 text nodes under 11px. Most are
   legitimately small — badges, freshness stamps, source lines — and shrinking
   metadata is how a dense page stays readable at all. But a handful carry real
   meaning a reader has to act on: what a score does NOT include, where a number
   came from, and how the score decomposes. Those are arguments, not labels, and
   an argument set in 9.5px on a phone is an argument nobody reads.
   Desktop density is untouched. */
@media(max-width:640px){
  .ipo-missing,.ipo-args-src,.ipo-score-parts,.ipo-gmp-w,.wal-excluded{font-size:11px}
  .ipo-missing,.ipo-args-src{line-height:1.65}
}
/* India at a glance. Tabular numerals and a fixed decimal count so the column
   of figures aligns on the decimal point — a board of market levels that jitters
   as digits change width reads as unreliable whatever the numbers say. */
.ib-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(148px,100%),1fr));gap:1px;
  background:var(--line);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.ib{background:var(--bg);padding:13px 15px;display:flex;flex-direction:column;gap:3px}
.ib-k{font-family:var(--mono);font-size:11px;letter-spacing:1.3px;text-transform:uppercase;
  color:var(--dim);white-space:nowrap}
.ib-v{font-size:19px;font-weight:600;font-variant-numeric:tabular-nums;letter-spacing:-.3px;
  color:var(--text)}
.ib-c{font-family:var(--mono);font-size:11px;font-variant-numeric:tabular-nums}
.ib-hero{grid-column:span 2}
.ib-hero .ib-v{font-size:clamp(23px,3vw,31px)}
/* VIX is not a price and must not be read as one: no currency, and a band label
   instead of a change figure, because "11.20 -1.1%" invites the reader to treat
   a fall in expected volatility as a loss. */
.ib-vix .ib-v{color:var(--blue)}
@media(max-width:520px){.ib-hero{grid-column:span 2}}
/* ── THE FIRST 60 SECONDS ─────────────────────────────────────────────────
   An ordered index, set as a list and not as cards on purpose: a numbered
   column is read top-to-bottom in one pass, and a grid of four boxes is
   scanned in whatever order the eye lands. The whole point of this block is
   the ORDER, so the shape has to carry it.

   Rules top and bottom, nothing round the outside — the same treatment the
   tables use. It sits above the decision board and belongs to the masthead
   group rather than to the content below it. */
.sixty{margin:0 0 var(--s6);border-top:1px solid var(--line2);
  border-bottom:1px solid var(--line2);padding:var(--s4) 0}
.sixty-h{display:flex;align-items:baseline;gap:var(--s3);flex-wrap:wrap;
  margin-bottom:var(--s3)}
.sixty-t{font-family:var(--mono);font-size:var(--t-overline);font-weight:700;
  letter-spacing:1.6px;text-transform:uppercase;color:var(--lime)}
.sixty-n{font-size:var(--t-caption);color:var(--dim)}
.sixty-l{list-style:none;display:flex;flex-direction:column;gap:1px}
.sixty-r a{display:flex;align-items:center;gap:var(--s3);padding:9px 8px;
  border-radius:5px;transition:background var(--m-micro) var(--ease)}
.sixty-r a:hover{background:var(--surface2)}
/* The numeral is the reading order, so it is set in the ghosted rank ink the
   pick cards already use for exactly the same job. */
.sixty-i{font-family:var(--mono);font-size:var(--t-label);font-weight:700;
  letter-spacing:1px;color:var(--dim);flex:0 0 auto}
/* The headline takes the row and pushes the basis chip to the far edge, so the
   chips form a column a reader can scan on its own — five FACTs and one MODEL
   is itself information about the day. */
.sixty-x{flex:1 1 auto;font-size:var(--t-body-sm);line-height:1.45;
  color:var(--text);font-variant-numeric:tabular-nums}
.sixty-r a:hover .sixty-x{color:var(--lime)}
@media(max-width:560px){
  /* The chip drops below the headline rather than squeezing it — at 375px a
     three-column row leaves the headline about eleven characters. */
  .sixty-r a{flex-wrap:wrap;gap:var(--s2) var(--s3)}
  .sixty-x{flex:1 1 100%;order:2}
  .sixty-r .mc-basis{order:1;margin-left:0}
}

.dboard{display:grid;grid-template-columns:repeat(4,1fr);gap:0;margin:0 0 26px;
  border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.db{display:flex;flex-direction:column;gap:3px;padding:15px 18px;text-decoration:none;
  border-left:1px solid var(--line);transition:background .2s}
.db:first-child{border-left:none}
.db:hover{background:var(--bg2)}
.db-k{font-family:var(--mono);font-size:11px;letter-spacing:1.6px;text-transform:uppercase;
  color:var(--dim)}
.db-v{font-family:var(--serif);font-size:clamp(17px,2vw,23px);font-weight:600;
  letter-spacing:-.3px;color:var(--text);line-height:1.15}
.db-s{font-family:var(--mono);font-size:11px;color:var(--muted)}
.db-hot .db-v{color:var(--lime)}
.db-warn .db-s{color:var(--down)}
@media(max-width:760px){.dboard{grid-template-columns:1fr 1fr}
  .db:nth-child(3),.db:nth-child(4){border-top:1px solid var(--line)}
  .db:nth-child(odd){border-left:none}}
.hc-why{font-size:12px;line-height:1.6;color:var(--muted);margin:0 0 12px;max-width:74ch}
.hc-why b{color:var(--text);font-weight:600}
.hc-why-d{display:block;margin-top:5px;color:var(--dim);font-size:11px}
.ev-more{margin-top:14px;border-top:1px solid var(--line);padding-top:12px}
.ev-more>summary{cursor:pointer;list-style:none;font-size:13px;color:var(--muted);
  line-height:1.6;max-width:78ch}
.ev-more>summary::-webkit-details-marker{display:none}
.ev-more>summary::before{content:'+ ';font-family:var(--mono);color:var(--dim)}
.ev-more[open]>summary::before{content:'– '}
.ev-more>summary b{color:var(--text)}
.ev-more>summary:focus-visible{outline:2px solid var(--lime);outline-offset:2px}
.ev-supp{font-family:var(--mono);font-size:11px;letter-spacing:.6px;text-transform:uppercase;
  color:var(--down);border:1px solid rgba(255,92,92,.35);border-radius:3px;padding:1px 4px;
  margin-left:5px;cursor:help;white-space:nowrap}
.wal-why-x{color:var(--down)!important;border-color:rgba(255,92,92,.35)!important}
.ev-tag{font-family:var(--mono);font-size:11px;letter-spacing:.5px;text-transform:uppercase;
  color:var(--dim);border:1px solid var(--line);border-radius:3px;padding:1px 4px;margin-left:5px;cursor:help}
.ev-alert{font-size:13px;line-height:1.6;color:var(--muted);max-width:80ch;margin:0 0 16px;
  border-left:2px solid var(--down);padding-left:12px}
.ev-alert code{font-family:var(--mono);font-size:11px;color:var(--down);
  background:rgba(255,92,92,.08);padding:1px 5px;border-radius:3px}
/* Impact level on a news card. Information, so blue per the colour contract —
   an impact LEVEL is a classification, not an outcome, and colouring HIGH red
   would say "this is bad" when it only says "this reaches your portfolio". */
/* Rolling 24h. A rail of time down the left, regions grouped inside each
   window. Events carrying a transmission chain are marked — those are the ones
   with a stated route to a portfolio, and in a list ordered by TIME rather than
   by rank they would otherwise be indistinguishable from the rest. */
.wt{margin:0 0 26px}
.wt-b{border-top:1px solid var(--line);padding:14px 0 4px}
.wt-bh{display:flex;align-items:baseline;justify-content:space-between;gap:12px;
  flex-wrap:wrap;margin-bottom:10px}
.wt-bt{font-family:var(--mono);font-size:11px;letter-spacing:1.6px;text-transform:uppercase;
  color:var(--lime)}
.wt-bn{font-family:var(--mono);font-size:11px;color:var(--dim)}
.wt-r{display:grid;grid-template-columns:112px 1fr;gap:14px;margin-bottom:10px}
.wt-rn{font-family:var(--mono);font-size:11px;color:var(--muted);padding-top:2px}
.wt-l{list-style:none;margin:0;padding:0;border-left:1px solid var(--line)}
.wt-i{display:grid;grid-template-columns:44px 1fr;gap:10px;padding:5px 0 5px 12px;
  position:relative}
.wt-i::before{content:'';position:absolute;left:-3px;top:11px;width:5px;height:5px;
  border-radius:50%;background:var(--line2)}
/* Blue: an impact classification is information, per the colour contract. */
.wt-hi::before{background:var(--blue);box-shadow:0 0 0 3px rgba(106,168,255,.15)}
.wt-t{font-family:var(--mono);font-size:11px;color:var(--dim);padding-top:2px;text-align:right}
.wt-x{font-size:13px;line-height:1.5}
.wt-x a{color:var(--text);text-decoration:none;border-bottom:1px solid var(--line2)}
.wt-x a:hover{border-color:var(--lime)}
.wt-s{display:block;font-family:var(--mono);font-size:11px;color:var(--dim);margin-top:2px}
.wt-s b{color:var(--muted);font-weight:400}
@media(max-width:640px){.wt-r{grid-template-columns:1fr;gap:4px}
  .wt-rn{font-size:11px;letter-spacing:1px;text-transform:uppercase}}
.nimp{font-family:var(--mono);font-size:11px;font-weight:700;letter-spacing:1px;
  padding:2px 6px;border-radius:3px;border:1px solid currentColor;margin-left:6px;white-space:nowrap}
.nimp-high{color:var(--blue);background:rgba(106,168,255,.10)}
.nimp-medium{color:var(--muted);border-color:var(--line2)}
.nimp-low{color:var(--dim);border-color:var(--line)}
.nwhy{border-top:1px solid var(--line);margin-top:11px;padding-top:10px}
.nwhy-k{font-family:var(--mono);font-size:11px;letter-spacing:1.2px;text-transform:uppercase;
  color:var(--lime);display:block;margin-bottom:3px}
.nwhy p{font-size:12px;line-height:1.55;color:var(--muted);margin:0 0 8px}
.nwhy-chain{font-family:var(--mono);font-size:11px;color:var(--text)}
.nwhy-w{font-family:var(--mono);font-size:11px;color:var(--gold);display:block}
.hero-by{font-family:var(--mono);font-size:11px;line-height:1.9;color:var(--dim);
  margin-top:18px;display:flex;flex-wrap:wrap;align-items:baseline;gap:0 10px;max-width:68ch}
.hero-by b{color:var(--text);font-weight:600}
.hero-by i{font-style:italic;color:var(--muted)}
.hero-by-sep{color:var(--line2)}
@media(max-width:640px){.hero-by{gap:0 7px;font-size:11px}.hero-by-sep{display:none}
  .hero-by span{display:block;width:100%}}
.dh-status{font-family:var(--mono);font-size:11px;color:var(--muted);margin:9px 0 0;
  display:flex;align-items:center;gap:8px}
.dh-dot{width:7px;height:7px;border-radius:50%;flex:0 0 auto}
.dh-dot-ok{background:var(--up);box-shadow:0 0 0 3px rgba(61,220,151,.15)}
.dh-dot-warn{background:var(--gold);box-shadow:0 0 0 3px rgba(230,180,80,.15)}
.wal-why{font-family:var(--mono);font-size:11px;letter-spacing:.4px;color:var(--dim);
  border:1px solid var(--line);border-radius:3px;padding:1px 4px;margin-left:5px;cursor:help;
  white-space:nowrap}
.wal-why-0{color:var(--gold);border-color:rgba(230,180,80,.35)}
.wal-excluded{font-size:12px;line-height:1.6;color:var(--muted);margin:12px 0 0;max-width:78ch;
  border-left:2px solid var(--line2);padding-left:12px}
.wal-live-tag{font-family:var(--mono);font-size:11px;font-style:italic;color:var(--dim);margin-left:3px}
.badge-cancelled{background:rgba(255,255,255,.04);color:var(--dim);border:1px solid var(--line)}
/* Long vs short in the paper wallet. Shape AND colour, not colour alone —
   the arrow carries the meaning for a red/green colour-blind reader, and the
   word carries it for a screen reader. */
.lt-held{display:inline-block;font-family:var(--mono);font-size:11px;font-weight:700;
  letter-spacing:.8px;text-transform:uppercase;color:var(--gold);
  border:1px solid var(--gold);border-radius:3px;padding:2px 6px;margin-top:4px}
.sec-movers{padding:14px 16px}
.sec-movers>summary{cursor:pointer;list-style:none;display:flex;justify-content:space-between;
  align-items:center;gap:10px;min-height:24px}
.sec-movers>summary::-webkit-details-marker{display:none}
.sec-movers>summary:focus-visible{outline:2px solid var(--lime);outline-offset:2px}
.mv-list{list-style:none;margin:4px 0 0;padding:0;font-size:12px}
.mv-list li{display:flex;justify-content:space-between;gap:10px;padding:3px 0;
  font-family:var(--mono)}
/* A pick the ledger has already resolved. Marked, never removed — it WAS
   this week's pick, and deleting it would be the dishonest fix. */
.pick-done{display:inline-block;font-family:var(--mono);font-size:11px;font-weight:700;
  letter-spacing:.9px;text-transform:uppercase;padding:3px 8px;border-radius:4px;margin-bottom:8px}
.pd-loss{color:var(--down);background:var(--down-soft);border:1px solid var(--down)}
.pd-win{color:var(--up);background:var(--up-soft);border:1px solid var(--up)}
.wside{display:inline-flex;align-items:center;gap:4px;font-family:var(--mono);
  font-size:11px;font-weight:700;letter-spacing:.8px;padding:3px 7px;
  border-radius:4px;white-space:nowrap}
.ws-long{color:var(--up);background:var(--up-soft);border:1px solid var(--up)}
.ws-short{color:var(--down);background:var(--down-soft);border:1px solid var(--down)}
.badge-expired{background:rgba(232,197,71,.1);color:var(--gold);border:1px solid rgba(232,197,71,.25)}
/* Position battle status — where a tracked position sits in the profit-protection ladder. */
.badge-accumulation{background:rgba(255,255,255,.04);color:var(--dim);border:1px solid var(--line)}
.badge-protected{background:rgba(184,239,67,.12);color:var(--lime);border:1px solid rgba(184,239,67,.3)}
.badge-compounding{background:rgba(167,139,250,.12);color:var(--violet);border:1px solid rgba(167,139,250,.3)}
.badge-threatened{background:rgba(232,197,71,.12);color:var(--gold);border:1px solid rgba(232,197,71,.3)}
.sym{font-family:var(--mono);font-weight:700;color:var(--text);transition:color .2s}
.sym:hover{color:var(--lime)}
.mono-dim{font-family:var(--mono);color:var(--dim);font-size:11px}
.pnl-u{color:var(--up);font-weight:700;font-family:var(--mono)}
.pnl-d{color:var(--down);font-weight:700;font-family:var(--mono)}

/* ═══════════════════ 04 PORTFOLIO ═══════════════════ */
.formbox{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:20px;margin-top:16px}
.formbox h4,.formbox .fh4{font-family:var(--mono);font-size:11px;letter-spacing:2px;text-transform:uppercase;
  color:var(--lime);margin-bottom:14px;font-weight:600}
.frow{display:flex;gap:9px;flex-wrap:wrap;margin-bottom:9px}
.frow input{background:var(--bg);border:1px solid var(--line);color:var(--text);padding:10px 13px;
  font-size:13px;flex:1;min-width:130px;border-radius:9px;font-family:var(--sans);transition:border-color .25s}
.frow input::placeholder{color:var(--dim)}
.frow input:focus{outline:none;border-color:var(--lime)}
.frow select{background:var(--bg);border:1px solid var(--line);color:var(--text);padding:10px 13px;
  font-size:13px;flex:1;min-width:130px;border-radius:9px;font-family:var(--sans);transition:border-color .25s}
.frow select:focus{outline:none;border-color:var(--lime)}
.fnote{font-size:11px;color:var(--dim);margin:2px 0 12px}
.btn{background:var(--lime);color:var(--on-brand);border:none;padding:10px 20px;font-size:11px;font-weight:700;
  cursor:pointer;letter-spacing:1.2px;border-radius:100px;font-family:var(--sans);text-transform:uppercase;
  transition:transform .25s var(--ease),box-shadow .25s}
.btn:hover{transform:translateY(-2px);box-shadow:0 6px 22px rgba(184,239,67,.25)}
.btn-sm{padding:6px 13px;font-size:11px}
/* 10px text plus 6px padding lands at ~23.6px — under WCAG 2.2 AA's
   24px target, and invisibly so, because it ROUNDS to 24 in devtools.
   Only enforced for a finger; on a mouse the button is fine as drawn. */
@media (pointer: coarse){.btn-sm{min-height:24px}}
.btn-gh{background:transparent;color:var(--muted);border:1px solid var(--line);padding:6px 13px;
  font-family:var(--mono);font-size:11px;letter-spacing:1px;cursor:pointer;border-radius:100px;
  text-transform:uppercase;transition:all .25s}
.btn-gh:hover{border-color:var(--down);color:var(--down)}
.btn-gh.v:hover{border-color:var(--violet);color:var(--violet)}

/* ═══════════════════ 05 WORLD ═══════════════════ */
.lead{display:grid;grid-template-columns:1.55fr 1fr;gap:0;border:1px solid var(--line);border-radius:18px;
  overflow:hidden;margin-bottom:14px;background:var(--surface)}
@media(max-width:820px){.lead{grid-template-columns:1fr}}
.lead-m{padding:clamp(22px,3.4vw,38px)}
.lead-m h2{font-family:var(--serif);font-size:clamp(21px,3vw,34px);font-weight:600;
  line-height:1.18;letter-spacing:-.4px;margin:12px 0 14px}
.lead-m h2 a{transition:color .25s}
.lead-m h2 a:hover{color:var(--lime)}
.lead-m p{font-size:14px;color:var(--muted);line-height:1.7}
.lead-s{padding:clamp(20px,2.6vw,30px);border-left:1px solid var(--line);background:var(--bg2)}
@media(max-width:820px){.lead-s{border-left:none;border-top:1px solid var(--line)}}
.mini{padding:13px 0;border-bottom:1px solid var(--line)}
.mini:last-child{border-bottom:none;padding-bottom:0}
.mini .s{font-family:var(--mono);font-size:11px;letter-spacing:1.4px;text-transform:uppercase;color:var(--lime);display:block;margin-bottom:5px}
.mini a{font-size:13px;font-weight:600;line-height:1.42;transition:color .25s}
.mini a:hover{color:var(--lime)}
.news-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(280px,100%),1fr));gap:14px}
.ncard{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:20px;
  transition:border-color .35s,transform .35s var(--ease)}
.ncard:hover{border-color:var(--line2);transform:translateY(-3px)}
.ncard .s{font-family:var(--mono);font-size:11px;letter-spacing:1.4px;text-transform:uppercase;color:var(--lime)}
.ncard h3{font-size:14px;font-weight:600;line-height:1.4;margin:9px 0 9px;letter-spacing:-.2px}
.ncard h3 a{transition:color .25s} .ncard h3 a:hover{color:var(--lime)}
.ncard p{font-size:12px;color:var(--muted);line-height:1.6}
.ncard .ts{font-family:var(--mono);font-size:11px;color:var(--dim);margin-top:12px;padding-top:11px;border-top:1px solid var(--line)}

/* ═══════════════════ 06 THE DESK (tabs) ═══════════════════ */
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:22px}
.tab{padding:9px 17px;font-size:11px;font-weight:500;letter-spacing:.8px;background:transparent;
  border:1px solid var(--line);color:var(--muted);cursor:pointer;border-radius:100px;
  font-family:var(--sans);transition:all .28s var(--ease);white-space:nowrap}
.tab:hover{border-color:var(--line2);color:var(--text)}
.tab.on{background:var(--lime);border-color:var(--lime);color:var(--on-brand);font-weight:700}
/* The Way — Arabic phrase card */
.arabic-hero{text-align:center;padding:26px 18px;background:var(--bg);border:1px solid var(--line);
  border-radius:14px;margin:14px 0 4px}
.ar-script{font-size:clamp(34px,6vw,52px);line-height:1.5;color:var(--up);font-weight:600;
  letter-spacing:0;margin-bottom:12px;direction:rtl;unicode-bidi:isolate}
.ar-translit{font-family:var(--mono);font-size:14px;color:var(--text);letter-spacing:.4px;margin-bottom:6px}
.ar-meaning{font-size:14px;color:var(--muted);font-style:italic}
@media(max-width:640px){ .arabic-hero{padding:20px 12px} }
/* Streak tracker — client-side only */
.streak{margin-top:18px;padding:18px 20px;background:var(--surface);border:1px solid var(--line);border-radius:14px}
.stk-head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap;margin-bottom:14px}
.stk-lab{font-family:var(--mono);font-size:11px;letter-spacing:1.8px;text-transform:uppercase;color:var(--lime);margin-bottom:5px}
.stk-sub{font-size:12px;color:var(--dim)}
.stk-nums{display:flex;gap:18px}
.stk-n{text-align:right}
.stk-n b{display:block;font-size:24px;font-weight:700;letter-spacing:-1px;color:var(--text);line-height:1}
.stk-n i{font-style:normal;font-family:var(--mono);font-size:11px;letter-spacing:1.2px;text-transform:uppercase;color:var(--dim)}
.stk-checks{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:14px}
.stk-c{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);cursor:pointer;
  background:var(--bg);border:1px solid var(--line);border-radius:100px;padding:6px 12px;user-select:none;
  transition:all .18s var(--ease)}
.stk-c:hover{border-color:var(--line2);color:var(--text)}
.stk-c.done{background:rgba(61,220,151,.10);border-color:rgba(61,220,151,.42);color:var(--up)}
.stk-c input{accent-color:var(--up);margin:0;cursor:pointer}
.stk-strip{display:flex;gap:3px;margin-bottom:9px}
.stk-d{flex:1;height:22px;border-radius:3px;background:rgba(255,255,255,.05);border:1px solid transparent}
.stk-d.p1{background:rgba(61,220,151,.22)} .stk-d.p2{background:rgba(61,220,151,.45)}
.stk-d.p3{background:rgba(61,220,151,.72)} .stk-d.today{border-color:var(--lime)}
.stk-foot{font-size:11px;color:var(--dim)}
/* Weekly review */
.deep-q{padding:22px 24px;background:rgba(167,139,250,.06);border:1px solid rgba(167,139,250,.22);
  border-radius:14px;margin-bottom:16px}
.dq-lab{font-family:var(--mono);font-size:11px;letter-spacing:1.8px;text-transform:uppercase;
  color:var(--violet);margin-bottom:9px}
.deep-q h3{font-size:clamp(18px,2.4vw,25px);font-weight:700;letter-spacing:-.7px;line-height:1.3;
  color:var(--text);margin-bottom:8px}
.deep-q p{font-size:13px;color:var(--muted);line-height:1.65}
.rv-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}
.rv-card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.rv-card.wide{grid-column:1/-1}
.rv-card label{display:block;font-family:var(--mono);font-size:11px;letter-spacing:1.6px;
  text-transform:uppercase;color:var(--lime);margin-bottom:5px}
.rv-hint{font-size:11px;color:var(--dim);margin-bottom:9px;line-height:1.5}
.rv-card textarea{width:100%;background:var(--bg);border:1px solid var(--line);border-radius:9px;
  padding:10px 12px;color:var(--text);font-family:inherit;font-size:13px;line-height:1.6;resize:vertical}
.rv-card textarea:focus{outline:none;border-color:var(--lime)}
.rv-bar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:12px;flex-wrap:wrap}
.rv-status{font-family:var(--mono);font-size:11px;color:var(--dim)}
.rv-btn{font-family:var(--mono);font-size:11px;letter-spacing:1px;text-transform:uppercase;
  background:transparent;border:1px solid var(--line);color:var(--muted);border-radius:100px;
  padding:8px 16px;cursor:pointer;transition:all .18s var(--ease)}
.rv-btn:hover{border-color:var(--lime);color:var(--lime)}
@media(max-width:640px){ .rv-grid{grid-template-columns:1fr} .stk-nums{gap:14px} }
.pane{display:none;animation:panein .5s var(--ease)}
.pane.on{display:block}
@keyframes panein{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
.essay{background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:clamp(22px,3.4vw,38px);
  position:relative;overflow:hidden}
.essay::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--ac,var(--lime))}
.essay h3{font-size:clamp(19px,2.6vw,27px);font-weight:700;letter-spacing:-.8px;line-height:1.25;
  margin-bottom:16px;color:var(--ac,var(--lime))}
.essay p{font-size:14px;line-height:1.85;color:var(--muted)}
.essay .q{font-size:14px;font-style:italic;color:var(--ac,var(--lime));border-left:2px solid var(--ac,var(--lime));
  padding-left:15px;margin:20px 0;line-height:1.7}
.essay .act{background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:16px 18px;
  margin-top:20px;font-size:13px;line-height:1.7;color:var(--muted)}
.essay .act b{display:block;font-family:var(--mono);font-size:11px;letter-spacing:1.8px;text-transform:uppercase;
  color:var(--ac,var(--lime));margin-bottom:7px}
.essay .meta{font-family:var(--mono);font-size:11px;letter-spacing:1.6px;text-transform:uppercase;
  color:var(--dim);margin-bottom:10px}
/* Book depth: full crux, learnings, examples, how to adapt */
.bookdeep{margin-top:20px;padding-top:18px;border-top:1px solid var(--line)}
.bdhead{font-family:var(--mono);font-size:11px;letter-spacing:1.8px;text-transform:uppercase;
  color:var(--ac,var(--lime));margin-bottom:12px}
.bookdeep ol.crux{margin:0;padding-left:0;list-style:none;counter-reset:cx}
.bookdeep ol.crux li{counter-increment:cx;position:relative;padding-left:34px;margin-bottom:11px;
  font-size:13px;line-height:1.65;color:var(--muted)}
.bookdeep ol.crux li::before{content:counter(cx,decimal-leading-zero);position:absolute;left:0;top:1px;
  font-family:var(--mono);font-size:11px;color:var(--ac,var(--lime));opacity:.75}
.bookdeep ul.bdlist{margin:0;padding-left:0;list-style:none}
.bookdeep ul.bdlist li{position:relative;padding-left:20px;margin-bottom:10px;
  font-size:13px;line-height:1.65;color:var(--muted)}
.bookdeep ul.bdlist li::before{content:"—";position:absolute;left:0;color:var(--ac,var(--lime));opacity:.7}
.bookdeep ul.bdlist.eg li{color:#AEB5BE;font-size:13px}
.bookdeep.adapt{background:var(--bg);border:1px solid var(--line);border-radius:12px;
  padding:16px 18px;border-top:1px solid var(--line)}
@media(max-width:640px){
  .bookdeep ol.crux li{padding-left:28px;font-size:13px}
  .bookdeep ul.bdlist li{font-size:13px}
}
.two{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:760px){.two{grid-template-columns:1fr}}

/* ═══════════════════ 07 THE MIND ═══════════════════ */
.quote-hero{text-align:center;padding:clamp(34px,6vw,70px) clamp(20px,4vw,50px);
  background:radial-gradient(ellipse at 50% 0%,rgba(232,197,71,.07),transparent 65%),var(--surface);
  border:1px solid var(--line);border-radius:20px;position:relative;overflow:hidden}
.quote-hero .mark{position:absolute;top:-30px;left:26px;font-size:170px;color:rgba(255,255,255,.028);
  font-family:Georgia,serif;line-height:1;pointer-events:none}
.quote-hero blockquote{font-size:clamp(19px,3vw,34px);font-weight:500;line-height:1.35;letter-spacing:-1px;
  max-width:20ch;margin:0 auto 22px;position:relative}
.quote-hero cite{font-family:var(--mono);font-size:11px;letter-spacing:2.6px;text-transform:uppercase;
  color:var(--gold);font-style:normal;font-weight:600}
.quote-hero .idx{font-family:var(--mono);font-size:11px;color:var(--dim);margin-top:8px}

/* ═══════════════════ 08 CHESS ═══════════════════ */
.chess-kpi{display:flex;gap:0;flex-wrap:wrap;border:1px solid var(--line);border-radius:16px;overflow:hidden;margin-bottom:18px}
.ck{flex:1 1 90px;padding:18px 16px;border-right:1px solid var(--line);background:var(--surface)}
.ck:last-child{border-right:none}
.ck .v{font-family:var(--mono);font-size:clamp(20px,2.6vw,28px);font-weight:700;letter-spacing:-1px;line-height:1}
.ck .k{font-size:11px;letter-spacing:1.6px;text-transform:uppercase;color:var(--dim);margin-top:7px}
.verdict{padding:18px 20px;background:rgba(61,220,151,.05);border:1px solid rgba(61,220,151,.22);
  border-radius:14px;font-size:14px;line-height:1.75;color:var(--muted);margin-bottom:18px}
.verdict b{color:var(--up);font-weight:700}
.game{background:var(--surface);border:1px solid var(--line);border-left:3px solid var(--dim);border-radius:14px;
  padding:18px;margin-bottom:10px;position:relative;transition:border-color .3s,transform .3s var(--ease)}
.game:hover{transform:translateX(3px)}
.game.win{border-left-color:var(--up)} .game.loss{border-left-color:var(--down)} .game.draw{border-left-color:var(--dim)}
.game .hdr{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:9px}
.game .res{font-weight:700;font-size:14px}
.game .meta{font-size:12px;color:var(--muted);line-height:1.6}
.game .op{font-size:13px;color:var(--text);margin-bottom:6px;font-weight:500}
.game .mv{font-family:var(--mono);font-size:11px;color:var(--dim);overflow-x:auto;white-space:nowrap;margin-top:5px}
.game .an{margin-top:11px;padding:11px 13px;background:var(--bg);border-radius:10px;border-left:2px solid var(--lime);
  font-size:12px;color:var(--muted);line-height:1.7}
/* Best move / standout / key facts — replaced the raw opening+final move dumps */
.game .bestmv{margin-top:11px;padding:11px 13px;background:rgba(232,183,74,.06);
  border:1px solid rgba(232,183,74,.22);border-radius:10px}
.game .bmlab{font-family:var(--mono);font-size:11px;letter-spacing:1.4px;text-transform:uppercase;
  color:var(--gold);margin-bottom:7px}
.game .bmrow{display:flex;align-items:baseline;gap:11px;flex-wrap:wrap}
.game .bmsan{font-family:var(--mono);font-size:16px;font-weight:700;color:var(--gold);letter-spacing:-.3px}
.game .bmgain{font-size:12px;font-weight:600;color:var(--up)}
.game .bmeval{font-family:var(--mono);font-size:11px;color:var(--dim)}
.game .uniq{margin-top:10px;padding:11px 13px;background:rgba(106,168,255,.05);
  border-left:2px solid var(--blue);border-radius:10px;font-size:12px;color:var(--muted);line-height:1.65}
.game .uniq b{display:block;font-family:var(--mono);font-size:11px;letter-spacing:1.4px;
  text-transform:uppercase;color:var(--blue);margin-bottom:5px;font-weight:600}
.game .kfacts{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.game .kf{font-size:11px;color:var(--muted);background:rgba(255,255,255,.04);
  border:1px solid var(--line);border-radius:7px;padding:4px 9px}
.game .ratings{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-top:10px;
  padding-top:10px;border-top:1px solid var(--line)}
.game .rt{display:flex;flex-direction:column;gap:2px}
.game .rt i{font-style:normal;font-family:var(--mono);font-size:11px;letter-spacing:1.2px;
  text-transform:uppercase;color:var(--dim)}
.game .rt b{font-size:14px;font-weight:700;color:var(--gold);letter-spacing:-.4px}
.game .rtnote{font-size:11px;color:var(--dim);line-height:1.5;flex:1;min-width:150px}
.pill{font-family:var(--mono);font-size:11px;letter-spacing:1.2px;padding:3px 8px;border-radius:100px;
  background:rgba(255,255,255,.05);color:var(--muted);text-transform:uppercase}
.trend{display:flex;gap:6px;align-items:flex-end;height:88px;margin-top:12px}
.trend>div{flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;gap:5px}
.trend .bar{width:100%;border-radius:4px 4px 0 0;height:0;transition:height .9s var(--ease) var(--d,0s)}
.rv.in .trend .bar{height:var(--h)}
.trend .lb{font-family:var(--mono);font-size:11px;color:var(--dim)}

/* ═══════════════════ FOOTER + FAB ═══════════════════ */
footer{position:relative;z-index:2;border-top:1px solid var(--line);margin-top:20px;background:var(--bg2)}
.foot-in{max-width:1400px;margin:0 auto;padding:clamp(40px,6vw,70px) var(--gut);
  display:flex;justify-content:space-between;gap:28px;flex-wrap:wrap;align-items:flex-end}
.foot-in h4,.foot-in .fh4{font-size:clamp(24px,4vw,42px);font-weight:800;letter-spacing:-1.8px;line-height:1}
.foot-in h4 b{color:var(--lime)}
.foot-in .m{font-family:var(--mono);font-size:11px;color:var(--dim);line-height:2;text-align:right}
@media(max-width:640px){.foot-in .m{text-align:left}}
.fab{position:fixed;right:20px;bottom:20px;z-index:400;width:46px;height:46px;border-radius:50%;
  background:var(--lime);color:var(--on-brand);border:none;cursor:pointer;font-size:16px;display:grid;place-items:center;
  opacity:0;pointer-events:none;transform:translateY(14px);transition:all .35s var(--ease);
  box-shadow:0 8px 26px rgba(184,239,67,.3)}
.fab.on{opacity:1;pointer-events:auto;transform:none}
.empty{padding:34px 22px;text-align:center;color:var(--dim);font-size:13px;background:var(--surface);
  border:1px dashed var(--line2);border-radius:16px}

@media(prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.001s!important;transition-duration:.001s!important}
  .rv{opacity:1;transform:none}
  h1.hl .w>span{transform:none;opacity:1}
  .hero-sub,.statrail{opacity:1}
}

/* ═══════════════════ LIVE LAYER ═══════════════════
   Everything below drives the /api-backed sections. On a static host the
   API probe fails and these components stay hidden, leaving the daily
   snapshot exactly as it renders today. */
.livebar{display:none;align-items:center;gap:10px;
  padding:8px var(--gut);font-family:var(--mono);font-size:11px;letter-spacing:.4px;
  border-bottom:1px solid var(--line);background:var(--bg)}
.livebar.on{display:flex}
.livebar .pip{width:6px;height:6px;border-radius:50%;background:var(--up);flex:none;
  animation:pulse 2.4s var(--ease) infinite}
.livebar.stale .pip{background:var(--gold);animation:none}
.livebar.off .pip{background:var(--dim);animation:none}
.livebar .msg{color:var(--muted);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.livebar button{font:inherit;color:var(--lime);background:none;border:1px solid var(--lime-line);
  border-radius:999px;padding:3px 11px;cursor:pointer;flex:none}
.livebar button:hover{background:var(--lime-soft)}

/* Stale-edition notice. Gold, not lime: this is not a healthy state. */
.editionbar{display:none;align-items:center;gap:12px;
  padding:8px var(--gut);font-family:var(--mono);font-size:11px;letter-spacing:.4px;
  border-bottom:1px solid var(--gold);color:var(--gold);
  background:linear-gradient(rgba(255,193,71,.10),rgba(255,193,71,.10)),var(--bg)}
.editionbar.on{display:flex}
/* Was nowrap + ellipsis, which truncated the message to "New edition
   publish…" on anything narrower than a laptop — the banner said least
   exactly where it had least room. It wraps now; two lines beats a clipped
   sentence. */
.editionbar span{flex:1;min-width:0;line-height:1.55}
.editionbar b{font-weight:700;letter-spacing:.6px;text-transform:uppercase}
.editionbar button{font:inherit;color:#000;background:var(--gold);border:none;
  border-radius:999px;padding:4px 13px;cursor:pointer;flex:none;font-weight:700}

.ctlbar{display:flex;flex-wrap:wrap;gap:9px;align-items:center;margin:0 0 16px}
.ctlbar input,.ctlbar select{font-family:var(--mono);font-size:12px;color:var(--text);
  background:var(--surface);border:1px solid var(--line2);border-radius:10px;padding:9px 12px;min-width:0}
.ctlbar input:focus,.ctlbar select:focus{outline:none;border-color:var(--lime-line)}
.ctlbar input[type=search]{flex:1 1 200px}
.ctlbar .ghost{color:var(--dim);font-family:var(--mono);font-size:11px;margin-left:auto}

.perf-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(150px,100%),1fr));gap:1px;
  background:var(--line);border:1px solid var(--line);border-radius:16px;overflow:hidden;margin-bottom:22px}
.perf-cell{background:var(--surface);padding:18px 16px}
.perf-cell .v{font-family:var(--mono);font-size:24px;font-weight:700;letter-spacing:-1px;line-height:1.1}
.perf-cell .k{font-family:var(--mono);font-size:11px;color:var(--dim);text-transform:uppercase;
  letter-spacing:1px;margin-top:7px}
.perf-cell .sub{font-size:11px;color:var(--muted);margin-top:4px}


.brk{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(280px,100%),1fr));gap:16px}
.brk-card{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:18px;min-width:0}
.brk-card h4,.brk-card .fh4{font-family:var(--mono);font-size:11px;color:var(--dim);text-transform:uppercase;
  letter-spacing:1.2px;margin-bottom:12px}
.brk-row{display:grid;grid-template-columns:1fr auto auto;gap:10px;align-items:center;
  padding:8px 0;border-top:1px solid var(--line);font-size:12px}
.brk-row:first-of-type{border-top:none}
.brk-row .kk{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.brk-row .nn{font-family:var(--mono);font-size:11px;color:var(--dim)}
.brk-row .rr{font-family:var(--mono);font-weight:700;font-variant-numeric:tabular-nums}

/* Engine log. A ledger of rule changes, so it reads as a record: a fixed
   metadata rail on the left, prose on the right, hairline between entries. */
.elog{list-style:none;border-top:1px solid var(--line)}
.elog-i{display:grid;grid-template-columns:132px 1fr;gap:0 28px;
  padding:26px 0;border-bottom:1px solid var(--line)}
.elog-m{display:flex;flex-direction:column;align-items:flex-start;gap:8px}
.elog-d{font-family:var(--mono);font-size:11px;color:var(--muted);
  font-variant-numeric:tabular-nums;letter-spacing:.4px}
.elog-t{font-family:var(--mono);font-size:11px;letter-spacing:1.4px;color:var(--dim)}
.elog-v{font-family:var(--mono);font-size:11px;letter-spacing:1.2px;text-transform:uppercase;
  border:1px solid var(--line2);border-radius:4px;padding:2px 7px}
.elog-v.adopted{color:var(--lime);border-color:var(--lime-line);background:var(--lime-soft)}
/* Rejected is deliberately not red. It is not a failure state — it is a test
   that returned a negative, which is the point of publishing it. */
.elog-v.rejected{color:var(--muted)}
/* A change that was neither adopted nor rejected — a cadence or a fact about
   how something already runs, written down so it stops being folklore. The
   verdict list is validated by test_engine_regressions, which is how this
   arrived unstyled and was caught before it shipped. */
.elog-v.logged{color:var(--p-markets);
  border-color:color-mix(in srgb,var(--p-markets) 30%,transparent);
  background:color-mix(in srgb,var(--p-markets) 8%,transparent)}
.elog-b{min-width:0}
/* The rule stays visible; the evidence is one click away. Marker suppressed
   and rebuilt so the whole title row is the hit target, not a 10px triangle. */
.elog-sum{cursor:pointer;list-style:none;display:flex;align-items:baseline;
  justify-content:space-between;gap:16px}
.elog-sum::-webkit-details-marker{display:none}
.elog-sum:focus-visible{outline:2px solid var(--blue);outline-offset:4px;border-radius:4px}
.elog-more{font-family:var(--mono);font-size:11px;letter-spacing:.09em;
  text-transform:uppercase;color:var(--dim);white-space:nowrap;flex:none}
.elog-sum:hover .elog-more{color:var(--text)}
.elog-more::after{content:" +"}
.elog-b[open] .elog-more::after{content:" \2212"}
.elog-b[open] .elog-more{color:var(--muted)}
.elog-d2{margin-top:10px}
.elog-h{font-size:19px;font-weight:600;letter-spacing:-.3px;text-wrap:balance;margin-bottom:8px}
.elog-sum .elog-h{margin-bottom:0}
.elog-p{color:var(--muted);font-size:14px;max-width:62ch}
.elog-e{width:100%;border-collapse:collapse;margin:14px 0 0;font-size:12px}
.elog-e th{text-align:left;font-weight:500;color:var(--text);padding:6px 12px 6px 0}
.elog-e td{padding:6px 0 6px 12px;text-align:right;white-space:nowrap;
  font-variant-numeric:tabular-nums}
.elog-e tr+tr th,.elog-e tr+tr td{border-top:1px solid var(--line)}
.elog-n,.elog-s{color:var(--dim);font-size:11px}
.elog-c{margin-top:12px;padding-left:13px;border-left:2px solid var(--line2);
  color:var(--dim);font-size:12px;line-height:1.65;max-width:62ch}
@media(max-width:640px){
  .elog-i{grid-template-columns:1fr;gap:12px}
  .elog-m{flex-direction:row;align-items:center;gap:10px}
  /* The evidence table is the one thing here that can force a sideways page
     scroll on a narrow screen, so it gets its own scroll container. */
  .elog-e{display:block;overflow-x:auto}
}

.arch{display:flex;gap:7px;overflow-x:auto;padding:4px 0 12px;-webkit-overflow-scrolling:touch}
.arch::-webkit-scrollbar{height:5px}
.arch-day{flex:none;min-width:76px;background:var(--surface);border:1px solid var(--line);
  border-radius:12px;padding:10px;text-align:center;cursor:pointer;transition:.18s var(--ease)}
.arch-day:hover{border-color:var(--lime-line);transform:translateY(-2px)}
.arch-day.on{border-color:var(--lime);background:var(--lime-soft)}
.arch-day .d{font-family:var(--mono);font-size:11px;color:var(--muted)}
.arch-day .n{font-family:var(--mono);font-size:16px;font-weight:700;margin:3px 0}
.arch-day .r{font-family:var(--mono);font-size:11px}

.pos-alert{display:inline-block;font-family:var(--mono);font-size:11px;letter-spacing:.6px;
  text-transform:uppercase;padding:2px 7px;border-radius:999px;margin-left:6px;vertical-align:middle}
.pos-alert.near-stop{background:rgba(255,92,92,.14);color:var(--down)}
.pos-alert.stop-hit{background:var(--down);color:#000;font-weight:700}
.pos-alert.near-target{background:rgba(61,220,151,.14);color:var(--up)}
.pos-alert.target-hit{background:var(--up);color:#000;font-weight:700}
/* Next-action pill — what the ladder/decision layer recommends right now. */
.next-action{display:inline-block;font-family:var(--mono);font-size:11px;letter-spacing:.6px;
  text-transform:uppercase;padding:2px 7px;border-radius:999px;white-space:nowrap}
.next-action.act-sell{background:rgba(184,239,67,.14);color:var(--lime)}
.next-action.act-exit{background:rgba(255,92,92,.14);color:var(--down)}
.next-action.act-wait{background:rgba(255,255,255,.04);color:var(--dim)}

/* Tracker mobile cards — first table-to-card switch on this page (every
   other wide table just horizontal-scrolls via .tw). Pure media-query
   toggle, no JS/matchMedia, matching how every other responsive rule here
   works. Reuses .card/.badge/.pos-alert/.next-action wholesale — no new
   visual system for one section. */
.tracker-cards{display:none}
@media(max-width:640px){
  .tracker-cards{display:grid;gap:12px}
  #posLive .tw{display:none}
}
.tcard{padding:16px}
.tcard-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:10px}
.tcard-sym{font-family:var(--mono);font-weight:700;font-size:14px}
.tcard-sub{font-family:var(--mono);font-size:11px;color:var(--dim);letter-spacing:.4px;margin-top:2px}
.tcard-px{text-align:right}
.tcard-px .now{font-family:var(--mono);font-weight:700;font-size:14px}
.tcard-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px 14px;font-size:12px;margin-bottom:10px}
.tcard-grid .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.5px}
.tcard-grid .v{font-family:var(--mono);margin-top:1px}
.tcard-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}

.keybox{display:none;gap:9px;flex-wrap:wrap;align-items:center;margin:0 0 16px;padding:14px;
  background:var(--surface);border:1px dashed var(--line2);border-radius:14px;font-size:12px;color:var(--muted)}
.keybox.on{display:flex}
.keybox input{font-family:var(--mono);font-size:12px;color:var(--text);background:var(--bg2);
  border:1px solid var(--line2);border-radius:9px;padding:8px 11px;flex:1 1 180px}

/* ═══════════════════ LEARNING TRACKS ═══════════════════ */
.lrn-head{display:flex;align-items:center;gap:12px;margin:26px 0 14px}
.lrn-head:first-of-type{margin-top:0}
.lrn-kicker{font-family:var(--mono);font-size:11px;letter-spacing:1.6px;text-transform:uppercase;
  color:var(--lime);white-space:nowrap}
.lrn-head::after{content:'';flex:1;height:1px;background:var(--line)}

.qa-grid{display:grid;gap:11px}
.qa{background:var(--surface);border:1px solid var(--line);border-radius:14px;overflow:hidden;
  transition:border-color .3s var(--ease)}
.qa[open]{border-color:var(--lime-line)}
.qa summary{list-style:none;cursor:pointer;padding:17px 20px;display:flex;justify-content:space-between;
  align-items:flex-start;gap:16px}
.qa summary::-webkit-details-marker{display:none}
.qa summary::after{content:'+';font-family:var(--mono);font-size:16px;color:var(--dim);flex:none;line-height:1.3}
.qa[open] summary::after{content:'−';color:var(--lime)}
.qa:hover summary::after{color:var(--lime)}
.qa-q{font-size:14px;font-weight:600;line-height:1.5;letter-spacing:-.15px;flex:1}
.qa-who{font-family:var(--mono);font-size:11px;color:var(--dim);letter-spacing:.5px;
  text-align:right;flex:none;max-width:180px;line-height:1.5}
.qa-a{padding:0 20px 20px;font-size:13px;line-height:1.72;color:var(--muted);
  border-top:1px solid var(--line);padding-top:16px;margin:0 20px 20px;padding-left:0;padding-right:0}

.lrn-card{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:20px;
  transition:border-color .3s var(--ease)}
.lrn-card:hover{border-color:var(--line2)}
.lrn-card.jain{border-left:3px solid var(--gold)}
.lrn-card.budd{border-left:3px solid var(--violet)}
.lrn-tag{font-family:var(--mono);font-size:11px;letter-spacing:1.4px;text-transform:uppercase;
  color:var(--dim);margin-bottom:9px}
.lrn-word{font-size:24px;font-weight:700;letter-spacing:-.5px;color:var(--lime);line-height:1.25}
.lrn-word.sm{font-size:19px;color:var(--text)}
.lrn-word .tr{font-size:13px;font-weight:400;color:var(--muted);letter-spacing:0}
.lrn-mean{font-size:13px;color:var(--text);margin-top:6px}
.lrn-ex{margin-top:14px;padding-top:13px;border-top:1px solid var(--line);display:grid;gap:5px}
.lrn-ex .es{font-size:13px;font-style:italic;color:var(--text)}
.lrn-ex .en{font-size:12px;color:var(--dim)}
.lrn-do{font-size:13px;line-height:1.65;color:var(--text);margin-top:10px}
.lrn-why{font-size:12px;line-height:1.68;color:var(--muted);margin-top:13px;padding-top:12px;
  border-top:1px solid var(--line)}
.lrn-why b{color:var(--lime);font-family:var(--mono);font-size:11px;letter-spacing:1.2px;
  text-transform:uppercase;margin-right:7px}

.drill{background:var(--surface);border:1px solid var(--line);border-left:3px solid var(--lime);
  border-radius:16px;padding:22px}
.drill-t{font-size:19px;font-weight:700;letter-spacing:-.3px}
.drill-d{font-size:13px;line-height:1.7;color:var(--text);margin-top:9px}
.drill-w{font-size:12px;line-height:1.65;color:var(--muted);margin-top:13px;padding-top:12px;
  border-top:1px solid var(--line)}

@media(max-width:640px){
  .qa summary{padding:15px 16px;gap:10px}
  .qa-q{font-size:13px}
  .qa-who{display:none}
  .qa-a{margin:0 16px 16px;font-size:13px}
  .lrn-word{font-size:19px}
}

/* ═══════════════════ MIND GYM ═══════════════════ */
.gym-tabs{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:20px}
.gym-tab{display:flex;align-items:center;gap:7px;padding:9px 15px;font-size:11px;font-weight:600;
  letter-spacing:.5px;background:transparent;border:1px solid var(--line);color:var(--muted);
  cursor:pointer;border-radius:100px;font-family:var(--sans);transition:all .28s var(--ease);white-space:nowrap}
.gym-tab:hover{border-color:var(--line2);color:var(--text)}
.gym-tab.on{background:var(--lime);border-color:var(--lime);color:var(--on-brand)}
.gym-tab .tdot{width:5px;height:5px;border-radius:50%;background:var(--gold);flex:none}
.gym-tab.on .tdot{background:#000}

.gym-stage{background:var(--surface);border:1px solid var(--line);border-radius:18px;
  padding:clamp(20px,4vw,34px);min-height:290px;display:flex;flex-direction:column;justify-content:center}
.gym-q{font-size:clamp(21px,4.4vw,32px);font-weight:700;letter-spacing:-.6px;line-height:1.3;margin-bottom:6px}
.gym-q .mono{font-family:var(--mono)}
.gym-sub{font-size:12px;color:var(--muted);margin-bottom:20px}
.gym-prompt{font-family:var(--mono);font-size:clamp(28px,7vw,52px);font-weight:700;color:var(--lime);
  letter-spacing:2px;text-align:center;padding:22px 0}

.gym-opts{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(130px,100%),1fr));gap:10px}
.gym-opt{padding:16px 14px;font-family:var(--mono);font-size:16px;font-weight:600;background:var(--bg2);
  border:1px solid var(--line2);color:var(--text);border-radius:12px;cursor:pointer;
  transition:all .2s var(--ease);text-align:center}
.gym-opt:hover:not(:disabled){border-color:var(--lime);color:var(--lime)}
.gym-opt:disabled{cursor:default;opacity:.55}
.gym-opt.right{background:var(--lime);border-color:var(--lime);color:var(--on-brand);opacity:1}
.gym-opt.wrong{background:rgba(255,92,92,.15);border-color:var(--down);color:var(--down);opacity:1}

.gym-input{display:flex;gap:9px;flex-wrap:wrap}
.gym-input input{flex:1 1 160px;font-family:var(--mono);font-size:19px;font-weight:600;color:var(--text);
  background:var(--bg2);border:1px solid var(--line2);border-radius:12px;padding:14px 16px;min-width:0}
.gym-input input:focus{outline:none;border-color:var(--lime)}
.gym-btn{padding:14px 24px;font-size:13px;font-weight:700;letter-spacing:.6px;background:var(--lime);
  border:none;color:var(--on-brand);border-radius:12px;cursor:pointer;font-family:var(--sans);transition:opacity .2s}
.gym-btn:hover{opacity:.85}
.gym-btn.ghost{background:transparent;border:1px solid var(--line2);color:var(--muted)}
.gym-btn.ghost:hover{border-color:var(--lime);color:var(--lime);opacity:1}

.gym-fb{margin-top:18px;padding:14px 16px;border-radius:12px;font-size:13px;line-height:1.55;
  border-left:3px solid var(--line2);background:var(--bg2)}
.gym-fb.good{border-left-color:var(--up)} .gym-fb.bad{border-left-color:var(--down)}
.gym-fb b{font-family:var(--mono)}

.gym-meta{display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap;
  margin-bottom:16px;font-family:var(--mono);font-size:11px;color:var(--dim);letter-spacing:.6px}
.gym-meta .prog{color:var(--lime)}
.gym-score{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(110px,100%),1fr));gap:1px;background:var(--line);
  border:1px solid var(--line);border-radius:14px;overflow:hidden;margin-top:20px}
.gym-score div{background:var(--surface);padding:14px 12px;text-align:center}
.gym-score .v{font-family:var(--mono);font-size:19px;font-weight:700}
.gym-score .k{font-family:var(--mono);font-size:11px;color:var(--dim);text-transform:uppercase;
  letter-spacing:1px;margin-top:5px}
@media(max-width:640px){
  .gym-opts{grid-template-columns:1fr 1fr}
  .gym-stage{min-height:250px}
}

/* Mobile: tables become the pain point on a phone, so give them a real scroll
   affordance and stop the control bars from stacking into a wall. */
@media(max-width:640px){
  .livebar{font-size:11px;padding:7px 14px}
  .livebar .msg{white-space:normal}
  .perf-cell .v{font-size:19px}
  .ctlbar input[type=search]{flex:1 1 100%}
  .ctlbar .ghost{margin-left:0;width:100%}
  .tw{position:relative}
  .tw::after{content:"swipe →";position:absolute;right:8px;top:-16px;font-family:var(--mono);
    font-size:11px;color:var(--dim);letter-spacing:.8px}
  .arch-day{min-width:66px;padding:8px}
}

/* The divider between measured listings and issues that never traded. */
.ipo-split td{
  background:var(--bg2);
  font:400 12px/1.6 var(--sans);
  color:var(--muted);
  padding:12px 14px;
}
.ipo-unmeasured{opacity:.72}

/* ── BUILD LOG ────────────────────────────────────────────────────────────
   Two columns: when, and what. The "why" runs under the title in the quiet
   voice — the reason a thing changed is context, not headline. */
.rows-log{display:flex;flex-direction:column;border-top:1px solid var(--line)}
.logrow{display:grid;grid-template-columns:190px 1fr;gap:18px;
  padding:16px 0;border-bottom:1px solid var(--line)}
.logmeta{display:flex;align-items:flex-start;gap:8px;flex-wrap:wrap}
.logdate{font:500 11px/1.6 var(--mono);color:var(--dim)}
.logsha{font:400 11px/1.6 var(--mono);color:var(--dim);opacity:.7}
.logbody b{display:block;font:600 16px/1.4 var(--disp);letter-spacing:-.015em;
  color:var(--text);margin-bottom:5px}
.logbody span{display:block;font:400 13px/1.65 var(--sans);color:var(--muted);max-width:70ch}
@media (max-width:720px){.logrow{grid-template-columns:1fr;gap:8px}}

/* ── TRUST STRIP ──────────────────────────────────────────────────────────
   One line under the nav saying how fresh the page is. Tone is carried by a
   word as well as a colour — "worst stale" reads without seeing the dot. */
.trust{border-bottom:1px solid var(--line);background:var(--bg2)}
.trust-in{
  max-width:1400px;margin:0 auto;padding:8px var(--gut);
  display:flex;align-items:center;gap:10px;flex-wrap:wrap;
  font:400 11px/1.4 var(--sans);color:var(--muted);
}
.trust-txt b{color:var(--text);font-weight:600;font-family:var(--mono)}
.trust-dot{width:7px;height:7px;border-radius:50%;flex:none;background:var(--dim)}
.trust.ok   .trust-dot{background:var(--up)}
.trust.warn .trust-dot{background:var(--gold)}
.trust.bad  .trust-dot{background:var(--down)}
.trust-link{margin-left:auto;color:var(--lime);text-decoration:none;font-weight:500;white-space:nowrap}
.trust-link:hover{text-decoration:underline}

/* ── EXPLAIN THIS ─────────────────────────────────────────────────────────
   A metric with no stated method is a number you are asked to take on faith.
   Every headline figure gets a small "?" that opens what it is, how it is
   computed and what it is computed FROM.

   Built on <details>, so it works with no JavaScript at all and is keyboard
   operable for free. */
.xp{display:inline-block;vertical-align:middle;margin-left:6px}
.xp>summary{
  list-style:none;cursor:pointer;width:16px;height:16px;border-radius:50%;
  border:1px solid var(--line2);color:var(--dim);
  font:600 11px/14px var(--mono);text-align:center;
  transition:color .15s var(--ease),border-color .15s var(--ease);
}
.xp>summary::-webkit-details-marker{display:none}
.xp>summary:hover{color:var(--text);border-color:var(--text)}
.xp[open]>summary{background:var(--text);color:var(--bg);border-color:var(--text)}
.xp-body{
  position:absolute;z-index:60;margin-top:8px;max-width:340px;
  background:var(--bg);border:1px solid var(--line2);border-radius:10px;
  padding:14px 15px;box-shadow:0 18px 44px rgba(17,18,20,.13);
  font:400 12px/1.6 var(--sans);color:var(--muted);text-align:left;
  white-space:normal;
}
.xp-body b{display:block;color:var(--text);font-weight:600;margin-bottom:5px;font-family:var(--disp)}
.xp-body dt{font:600 11px/1 var(--mono);letter-spacing:.11em;text-transform:uppercase;
  color:var(--dim);margin-top:10px}
.xp-body dd{margin:3px 0 0}

/* ── COLLAPSED NAVIGATION ─────────────────────────────────────────────────
   Six buttons, each opening its sections. The anchors are unchanged and still
   in the DOM — this is a disclosure layer, not a different list, so the scroll
   spy, command palette and deep links all keep working. */
.navgrp{position:relative;display:flex}
.navgrp-btn{
  appearance:none;background:none;border:0;cursor:pointer;
  display:flex;align-items:center;gap:7px;
  padding:12px 14px;color:var(--muted);
  font:600 11px/1 var(--disp);font-variation-settings:'wdth' 92;
  letter-spacing:.06em;text-transform:uppercase;
  border-bottom:2px solid transparent;
  transition:color .15s var(--ease),border-color .15s var(--ease);
}
.navgrp-btn:hover{color:var(--text)}
.navgrp-btn i{
  font-style:normal;font-family:var(--mono);font-size:11px;
  color:var(--dim);background:var(--surface2);
  border-radius:99px;padding:2px 6px;line-height:1.4;
}
.navgrp-btn[aria-expanded="true"]{color:var(--text);border-bottom-color:var(--pillar,var(--lime))}
/* Each group carries its pillar's colour on the count chip. Six grey chips
   told you nothing; six coloured ones are the legend for the rules and
   eyebrows further down the page. */
.navgrp-btn i{color:var(--pillar,var(--dim));background:color-mix(in srgb,var(--pillar,var(--dim)) 10%,transparent)}
/* Marks the group containing whatever the scroll spy says you are reading, so
   a collapsed nav still tells you where you are. */
.navgrp.here .navgrp-btn{color:var(--text)}
.navgrp.here .navgrp-btn::before{
  content:'';width:5px;height:5px;border-radius:50%;background:var(--pillar,var(--lime));flex:none;
}
.navgrp-menu{
  /* fixed, NOT absolute: the parent .nav-in is overflow-x:auto, which clips on
     both axes, so an absolutely-positioned menu below the button was painted
     outside the scroll box and was invisible. Coordinates come from JS. */
  position:fixed;top:0;left:0;z-index:70;min-width:230px;
  background:var(--bg);border:1px solid var(--line2);border-radius:10px;
  padding:6px;display:flex;flex-direction:column;gap:1px;
  box-shadow:0 18px 44px rgba(17,18,20,.13);
}
.navgrp-menu[hidden]{display:none}
.navgrp-menu a{
  display:flex;align-items:center;gap:10px;padding:9px 11px;border-radius:6px;
  font:400 13px/1.3 var(--sans);color:var(--muted);text-decoration:none;white-space:nowrap;
  transition:background .12s var(--ease),color .12s var(--ease);
}
.navgrp-menu a:hover,.navgrp-menu a:focus-visible{background:var(--surface2);color:var(--text)}
.navgrp-menu a i{
  font-style:normal;font-family:var(--mono);font-size:11px;color:var(--dim);
  min-width:18px;
}
@media (max-width:760px){
  .navgrp-menu{min-width:0;max-height:60vh;overflow-y:auto}
}

/* ── WHERE BRICOLAGE ACTUALLY LANDS ───────────────────────────────────────
   The split is by JOB, not by size.

   Newsreader keeps the editorial work — section titles and subheadings. That
   serif is what makes the page read as a publication rather than a control
   panel, and replacing it would have cost the thing the redesign was for.

   --disp takes the DATA. Every headline number on this page was set in
   JetBrains Mono at 40px with -1.5px tracking: a monospace face stretched
   three times past the size it was drawn for, where the even advance widths
   that make a table readable turn a headline gappy. --disp is a display
   grotesque with a width axis, so a big number can be set tight and still
   keep tabular figures.

   Mono stays exactly where it belongs — inside tables, in row data, and
   anywhere digits must line up in a column. If it is small and it is data,
   it is mono. If it is large and it is data, it is --disp. */
.stat .v,
.kpi .v,
.hero-stat .v,
.whencell .wv,
.whatifout .wo .v{
  font-family:var(--disp);
  font-variation-settings:'wdth' 88;
  font-variant-numeric:tabular-nums;
  font-weight:800;
  letter-spacing:-.03em;
}
/* Eyebrows and group labels — small, wide-tracked, and previously mono, which
   made them read as data rather than as navigation. */
.nav-g,
.snum,
.prov-legend .pl-lead{
  font-family:var(--disp);
  font-variation-settings:'wdth' 92;
  font-weight:600;
}

/* ── VOLUME BOARD ─────────────────────────────────────────────────────────
   The bar is the point of the section. A column of "3.4x  2.9x  2.7x" is a
   list; a column of bars is a shape, and the shape is what tells you whether
   today's participation is concentrated in three names or spread across
   eighteen. Width is the ratio against a 10x ceiling — anything past 10x is a
   different kind of event and gets a full bar rather than a longer one.
   Colour is the WEEK's direction, never the ratio, so a wall of red reads as
   distribution before a single figure is read. */
.volboard td{vertical-align:middle}
.volbar{display:inline-block;width:min(180px,34vw);height:9px;border-radius:2px;
  background:var(--surface3);overflow:hidden;vertical-align:middle;margin-right:9px}
.volbar-fill{display:block;height:100%;border-radius:2px;background:var(--dim)}
.volbar-fill.up{background:var(--up)}
.volbar-fill.dn{background:var(--down)}
.volbar-x{font-family:var(--mono);font-size:12px;color:var(--text);font-variant-numeric:tabular-nums}
/* The reading is a word, not only a colour: the colour is unreadable to a
   reader who cannot separate the two, and "Churn" has no colour at all. */
.vread{font:600 11px/1 var(--mono);letter-spacing:.1em;text-transform:uppercase;
  padding:4px 7px;border-radius:3px;border:1px solid var(--line2);color:var(--dim);
  background:var(--surface2);white-space:nowrap}
.vread.up{color:var(--up);border-color:color-mix(in srgb,var(--up) 32%,transparent);background:var(--up-soft)}
.vread.dn{color:var(--down);border-color:color-mix(in srgb,var(--down) 32%,transparent);background:var(--down-soft)}

/* ── DEFINITIONS ──────────────────────────────────────────────────────────
   Two-column on a wide screen — term left, definition right — because a
   glossary is scanned by term, and a term buried above its own paragraph
   cannot be scanned. Collapses to stacked below 720px where two columns
   would give the definition about twenty characters of measure. */
.metdefs{display:flex;flex-direction:column;gap:0;margin:0}
.metdef{display:grid;grid-template-columns:minmax(min(140px,100%),1fr) 3fr;gap:clamp(16px,3vw,44px);
  padding:16px 0;border-top:1px solid var(--line)}
.metdef:first-child{border-top:0}
.metdef dt{font:600 clamp(15px,1.6vw,17px)/1.3 var(--disp);color:var(--text);letter-spacing:-.01em}
.metdef dd{margin:0}
.metdef .md-what{font:400 14px/1.6 var(--sans);color:var(--text);margin:0}
.metdef .md-how{font:400 13px/1.65 var(--sans);color:var(--muted);margin:6px 0 0;max-width:62ch}
@media (max-width:720px){
  .metdef{grid-template-columns:1fr;gap:6px;padding:14px 0}
}

/* ── METRIC BADGES ────────────────────────────────────────────────────────
   Stamped by app.js onto any KPI whose label matches the METRICS list, and
   linked to that metric's definition. Deliberately tiny and low-contrast: the
   badge is a footnote marker, and a legend that competes with the number it
   annotates has defeated itself. */
.kpi .k{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
/* The badge is a <button> now, not an <a> — it opens the method under the
   number instead of jumping to the glossary at the foot of the page. Both
   selectors are kept: `a.mprov` still matches the hand-written badges in the
   template, and dropping it would have unstyled them. */
a.mprov,button.mprov{font:600 11px/1 var(--mono);letter-spacing:.1em;text-transform:uppercase;
  padding:3px 5px;border-radius:3px;border:1px solid;text-decoration:none;flex:none;
  transition:opacity .15s var(--ease)}
a.mprov:hover,a.mprov:focus-visible,
button.mprov:hover,button.mprov:focus-visible{opacity:1}
a.mprov,button.mprov{opacity:.78}
button.mprov{cursor:pointer;font-family:var(--mono);background:none}
/* The caret earns the click. A chip that reads "RESULT" looks like a label;
   the same chip with a caret reads as something that opens. */
button.mprov::after{content:" +";opacity:.7}
button.mprov[aria-expanded="true"]::after{content:" \2212"}
button.mprov:focus-visible{outline:2px solid var(--lime);outline-offset:2px}

/* THE METHOD, IN PLACE.
   Two paragraphs and a way out: WHAT the number is, HOW it is arrived at, and
   a link to the full glossary entry for anyone who wants its neighbours too.
   The jump the badge used to perform is still available — it is just no longer
   the only way to answer the question.

   The panel takes the full width of its tile and pushes the tiles below it
   down rather than floating over them: a popover covering the numbers beside
   the one being explained makes comparison impossible at the exact moment the
   reader is trying to understand a comparison. */
.mpanel{
  flex:1 1 100%;margin-top:8px;padding:10px 12px;
  background:var(--surface2);border-left:2px solid var(--line2);border-radius:0 5px 5px 0;
  text-align:left;
}
.mpanel-what{font:500 var(--t-body-sm)/1.55 var(--sans);color:var(--text);margin:0}
.mpanel-how{font:400 var(--t-caption)/1.6 var(--sans);color:var(--muted);margin:6px 0 0}
.mpanel-more{display:inline-block;margin-top:8px;font:600 var(--t-overline)/1 var(--mono);
  letter-spacing:.1em;text-transform:uppercase;color:var(--dim)}
.mpanel-more:hover{color:var(--lime)}

/* "5 days old" is only bad news if the dataset refreshes daily. Three of the
   twelve here are weekly, and their age was being read as staleness by every
   reader who did not also read the Expected column. The verdict travels with
   the number now. */
.dh-within{display:inline-block;margin-left:6px;font:400 var(--t-label)/1 var(--mono);
  letter-spacing:.06em;color:var(--up)}
.dh-over{display:inline-block;margin-left:6px;font:400 var(--t-label)/1 var(--mono);
  letter-spacing:.06em;color:var(--gold)}
/* The tile becomes a column so the panel can sit under the label rather than
   beside it — .kpi is a flex row and an unset panel would try to share it. */
.kpi:has(.mpanel),.stat:has(.mpanel){flex-wrap:wrap}
.mprov-fact  {color:var(--p-markets); border-color:color-mix(in srgb,var(--p-markets) 30%,transparent); background:color-mix(in srgb,var(--p-markets) 7%,transparent)}
.mprov-model {color:var(--p-research);border-color:color-mix(in srgb,var(--p-research) 30%,transparent);background:color-mix(in srgb,var(--p-research) 7%,transparent)}
.mprov-result{color:var(--up);        border-color:color-mix(in srgb,var(--up) 30%,transparent);        background:var(--up-soft)}
.mprov-view  {color:var(--gold);      border-color:color-mix(in srgb,var(--gold) 30%,transparent);      background:var(--gold-soft)}


/* ── MOBILE: THE PAGE MUST NOT SCROLL SIDEWAYS ────────────────────────────
   Measured at 375px, the document was 419px wide, so every screen could be
   dragged 44px to the right and the fixed header slid with it. Two causes,
   both fixed at the source rather than papered over with overflow-x:hidden on
   <html> — that hides the symptom and the next one arrives unnoticed.

   1. A fund's sector chip is white-space:nowrap inside a flex row. "Financial
      Services 21.54%" is wider than a phone, and nowrap means it cannot give.
      It wraps below 760px; on a desktop the chip still reads as one unit.
   2. The masthead ran brand + date + clock + Search + theme on one 375px line.
      The date already hid at 720px; below 600px the word "Search" goes too —
      the glyph carries it, and a phone has no Cmd key to press anyway. */
@media(max-width:760px){
  .fpf-c{white-space:normal;overflow-wrap:anywhere;max-width:100%}
}
@media(max-width:600px){
  .topbar-in{gap:10px}
  .stamp{gap:8px;min-width:0}
  .cmdk-hint span:last-child{display:none}
  .stamp .live{font-size:11px;white-space:nowrap}
  /* Tap targets. A 24px button is under every platform's 44px minimum, and
     these two sit next to each other in a corner. */
  .thm,.cmdk-hint{min-height:38px;display:inline-flex;align-items:center}
}


/* ── PLAIN TABLES ─────────────────────────────────────────────────────────
   Every table rule in this file targeted `table.t` — the sortable stock
   screen. The breakout board, the volume board, the IPO listings and the
   engine tables are plain <table> inside .tblwrap and matched NONE of them, so
   they rendered with zero cell padding, no border collapse and a company name
   set in full-strength body ink butted straight against its symbol:
   "NETWEBNetweb Technologies India Ltd." That is the "tabulate properly"
   complaint, and it was a missing selector rather than a layout choice.

   Scoped to .tblwrap so table.t keeps its own rules untouched — it works, and
   changing a table that works is a separate decision. */
.tblwrap table{width:100%;border-collapse:collapse;font-size:var(--t-table)}
.tblwrap table th{
  /* STICKY, and was not. Measured: of 18 tables, these were the only two whose
     headers scrolled away — including the volume board and the breakout
     screen, both of which run past twenty rows. `--surface2` is already opaque
     here, which is what test_page_structure's "sticky table headers are
     opaque" check requires: a translucent sticky header smears the rows
     passing under it. */
  position:sticky;top:0;z-index:5;
  text-align:left;padding:var(--tbl-pad-y-h) var(--tbl-pad-x);white-space:nowrap;
  /* line-height was 1 here and unset (so 1.6 from body) on table.t. A header
     that is 11px/1 sits optically higher in its cell than one at 11px/1.6,
     which is why the two tables' header rows never lined up when they were
     stacked in the same section. */
  font:600 var(--t-table-h)/1.35 var(--mono);letter-spacing:var(--tbl-track);
  text-transform:uppercase;
  color:var(--dim);background:var(--surface2);
  border-bottom:1px solid var(--line2);
}
.tblwrap table th.r,.tblwrap table td.r{text-align:right}

/* The filter box that app.js inserts above any table of eight rows or more.
   Sits outside the scroll wrapper so it stays put while the table scrolls
   sideways under it. */
.tfilter{display:flex;align-items:center;gap:var(--s3);margin:0 0 var(--s2)}
.tfilter input{
  flex:0 1 260px;background:var(--surface);color:var(--text);
  border:1px solid var(--line);border-radius:6px;padding:7px 10px;
  font:400 var(--t-body-sm)/1.2 var(--sans);
}
.tfilter input:focus{outline:none;border-color:var(--lime)}
.tfilter input::placeholder{color:var(--dim)}
.tfilter-n{font:400 var(--t-caption)/1 var(--mono);color:var(--dim)}

/* Sortable headers, generic. The cursor and the caret are the whole
   affordance — without them a sortable column is indistinguishable from a
   fixed one and nobody clicks it. */
table th.sortable{cursor:pointer;user-select:none}
table th.sortable:hover{color:var(--text)}
table th.sortable:focus-visible{outline:2px solid var(--lime);outline-offset:-2px}
/* A dimmed caret on every sortable header, full strength on the active one:
   the reader can see WHICH columns sort before clicking, rather than
   discovering it. */
table th.sortable::after{content:" \2195";opacity:.28;font-size:9px}
table th.sortable[aria-sort]{color:var(--lime)}
table th.sortable[aria-sort=ascending]::after{content:" \25B4";opacity:1}
table th.sortable[aria-sort=descending]::after{content:" \25BE";opacity:1}
/* border-TOP here, border-BOTTOM on table.t. Same visual intent, opposite
   mechanics, so the two disagreed about which end of the table carries a
   rule and needed opposite :first-child / :last-child exceptions to look
   the same. Both are border-bottom now and the exception is the same one. */
.tblwrap table td{
  padding:var(--tbl-pad-y) var(--tbl-pad-x);border-bottom:1px solid var(--line);vertical-align:middle;
  color:var(--text);
}
.tblwrap table tbody tr:last-child td{border-bottom:0}
.tblwrap table tbody tr:hover{background:var(--surface2)}
/* Digits in a column have to line up or the column cannot be compared down its
   own length, which is the only reason to put them in a column. */
.tblwrap table td.num,.tblwrap table .num{
  font-family:var(--mono);font-variant-numeric:tabular-nums;letter-spacing:-.01em;
}
/* The company name under its symbol. It had no rule at all, so it rendered as
   inline body text in full ink — same size, same colour, no space. A subtitle
   has to be a different weight AND a different line, or it is not a subtitle. */
.tsub{
  display:block;margin-top:3px;
  font:400 11px/1.35 var(--sans);color:var(--muted);
  max-width:26ch;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
}


/* A symbol that opens a detail sheet has to look like it does. Inheriting the
   table's ink made these read as plain text, so the affordance existed and was
   invisible — the same click worked in the sector boards, where the symbol is
   styled, and appeared not to work here. */
.tblwrap table a.sym{color:var(--pillar,var(--text));text-decoration:none;
  border-bottom:1px dotted color-mix(in srgb,var(--pillar,var(--text)) 45%,transparent)}
.tblwrap table a.sym:hover,.tblwrap table a.sym:focus-visible{border-bottom-style:solid}


/* The book's reconciliation line. Eight orders under a header reading
   "deployed 71.7%" could not be tied together by eye, which is what made the
   book look like it was ignoring most of the crore. */
.mandate-total{display:flex;flex-wrap:wrap;gap:6px 20px;padding:12px 16px;
  border-top:1px solid var(--line2);background:var(--surface2);
  font:400 12px/1.5 var(--mono);color:var(--muted);font-variant-numeric:tabular-nums}
.mandate-total b{color:var(--text);font-weight:700}
.mandate-total span:first-child{color:var(--dim);text-transform:uppercase;
  letter-spacing:.1em;font-size:11px;align-self:center}


/* ── SMART MONEY FLOW ─────────────────────────────────────────────────────
   Paired bars growing from a shared baseline, FII beside DII. The two are
   almost always on opposite sides, and that opposition is the whole point of
   the chart — a run of numbers cannot show it, and a single line hides which
   side is doing the moving.

   Bars grow DOWN when the flow is negative (`.neg` flips align-self), so a
   selling day reads as a bar hanging below the line the way it would on any
   flow chart, without a second axis or a transform. */
.smf{margin-top:18px;border:1px solid var(--line);border-radius:10px;
  background:var(--surface);padding:16px 18px 14px}
.smf-head{display:flex;justify-content:space-between;align-items:baseline;
  gap:14px;flex-wrap:wrap;margin-bottom:16px}
.smf-t{font:600 13px/1.2 var(--disp);color:var(--text);letter-spacing:-.01em}
.smf-k{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.smf-key{display:inline-flex;align-items:center;gap:6px;
  font:400 11px/1 var(--mono);color:var(--muted)}
.smf-key i{width:9px;height:9px;border-radius:2px;display:inline-block}
.sw-fii{background:var(--p-markets)}
.sw-dii{background:var(--gold)}
/* The zero line is the chart's spine: the row is centred on it, bars above are
   buys and bars below are sells. */
.smf-chart{display:flex;align-items:center;gap:clamp(3px,1vw,10px);
  min-height:150px;overflow-x:auto;padding-bottom:4px}
.smf-day{flex:1 1 0;min-width:26px;display:flex;flex-direction:column;
  align-items:center;gap:7px}
.smf-pair{display:flex;align-items:flex-end;justify-content:center;gap:3px;
  height:120px;width:100%;border-bottom:1px solid var(--line2);position:relative}
.smf-bar{flex:1 1 0;max-width:14px;min-height:2px;border-radius:2px 2px 0 0;
  align-self:flex-end;transition:opacity .15s var(--ease)}
/* A net sell hangs below the baseline rather than being drawn upward in a
   different colour — direction has to be readable without the legend. */
.smf-bar.neg{align-self:flex-start;border-radius:0 0 2px 2px;
  transform:translateY(120px);opacity:.85}
.smf-day:hover .smf-bar{opacity:.72}
.smf-d{font:400 11px/1 var(--mono);color:var(--dim);white-space:nowrap}
@media(max-width:600px){
  .smf{padding:13px 12px 11px}
  .smf-chart{gap:4px}
  .smf-day{min-width:22px}
}


/* ── THE RECORD ───────────────────────────────────────────────────────────
   A broadsheet band, not a card. Rules above and below rather than a border
   and a shadow, because the thing it is imitating is a newspaper's boxed
   standfirst — the piece of the page that says "this is the argument" — and
   that has never been a rounded rectangle.

   Two columns on a wide screen: the claim on the left in the editorial serif,
   the evidence on the right in the data face. Deliberately NOT centred; a
   centred claim reads as a slogan, a left-set one reads as a masthead. */
.record{max-width:1400px;margin:clamp(40px,6vw,72px) auto 0;padding:0 var(--gut)}
.record-in{display:grid;grid-template-columns:minmax(0,1.05fr) minmax(0,1fr);
  gap:clamp(28px,5vw,72px);align-items:start;
  border-top:2px solid var(--text);border-bottom:1px solid var(--line2);
  padding:clamp(24px,3vw,38px) 0}
.record-eyebrow{display:block;font:600 11px/1 var(--mono);letter-spacing:.22em;
  text-transform:uppercase;color:var(--p-ledger);margin-bottom:14px}
.record-h{font-family:var(--serif);font-weight:600;
  font-size:clamp(28px,3.6vw,44px);line-height:1.06;letter-spacing:-.02em;
  color:var(--text);margin:0 0 16px;text-wrap:balance}
.record-h em{font-style:italic;color:var(--p-ledger)}
.record-p{font:400 14px/1.65 var(--sans);color:var(--muted);max-width:52ch;margin:0 0 18px}
.record-cta{display:inline-flex;align-items:center;gap:7px;
  font:600 12px/1 var(--mono);letter-spacing:.06em;text-transform:uppercase;
  color:var(--text);text-decoration:none;
  border-bottom:2px solid var(--p-ledger);padding-bottom:5px;
  transition:gap .15s var(--ease)}
.record-cta:hover,.record-cta:focus-visible{gap:12px}

/* Six figures on a hairline grid. Rules instead of card edges: on a page this
   dense another six bordered boxes would be noise, and the numbers are the
   point rather than the containers. */
.record-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));
  gap:1px;background:var(--line);border:1px solid var(--line)}
.rec{background:var(--surface);padding:16px 14px;display:flex;
  flex-direction:column;gap:5px;min-width:0}
.rec-v{font:700 clamp(22px,2.6vw,30px)/1 var(--disp);letter-spacing:-.03em;
  color:var(--text);font-variant-numeric:tabular-nums}
.rec-v.up{color:var(--up)}
.rec-v.dn{color:var(--down)}
.rec-k{font:400 11px/1.3 var(--mono);letter-spacing:.06em;
  text-transform:uppercase;color:var(--dim)}
.record-foot{padding:14px 0 0;max-width:none}
.record-foot b{color:var(--text);font-weight:600;margin-right:8px}
.record-foot a{color:var(--muted);text-decoration:none;
  border-bottom:1px dotted var(--line2)}
.record-foot a:hover{color:var(--text)}
@media(max-width:900px){
  .record-in{grid-template-columns:1fr;gap:24px}
}
@media(max-width:560px){
  .record-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
}


/* ── MOBILE BOTTOM NAV ────────────────────────────────────────────────────
   Phones only. Above 760px the header nav is reachable and a second one would
   be clutter; below it, the header nav is a horizontal scroller full of
   dropdowns, which on a page this long means nobody navigates and everybody
   scrolls to the bottom looking for something.

   safe-area-inset-bottom because iOS puts a home indicator exactly where a
   fixed bar goes, and a 44px minimum because that is the smallest thing a
   thumb hits reliably. */
.botnav{display:none}
@media(max-width:760px){
  .botnav{
    display:flex;position:fixed;left:0;right:0;bottom:0;z-index:80;
    background:color-mix(in srgb,var(--bg) 92%,transparent);
    backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
    border-top:1px solid var(--line2);
    padding-bottom:env(safe-area-inset-bottom,0px);
    /* Height lives on the BAR, not on the links. A min-height on the anchors
       was measuring 30px in the browser while reporting 44 in the source —
       the links are flex children and their own minimum was not deciding the
       row. Setting it here and letting them stretch is both more robust and
       one fewer place for the tap target to be wrong. */
    min-height:54px;align-items:stretch;
  }
  .botnav-a{
    flex:1 1 0;min-width:0;
    display:flex;flex-direction:column;align-items:center;justify-content:center;
    gap:3px;padding:9px 2px;text-decoration:none;
    color:var(--dim);border-top:2px solid transparent;
  }
  .botnav-a .botnav-t{
    font:600 11px/1.1 var(--mono);letter-spacing:.06em;text-transform:uppercase;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%;
  }
  /* The pillar hue again, so the bar agrees with the header and the section
     eyebrows about which part of the paper you are in. */
  .botnav-a.here{color:var(--pillar,var(--text));border-top-color:var(--pillar,var(--lime))}
  /* The bar covers the last of the page otherwise, and the back-to-top button
     lands underneath it. */
  body{padding-bottom:66px}
  .fab{bottom:76px}
}


/* ── KEYBOARD FOCUS ON INPUTS ─────────────────────────────────────────────
   WCAG 2.4.7. There is a global :focus-visible outline, but five input rules
   set `outline:none` on :focus and lean on a border-colour change instead.
   Those selectors are more specific, so they win — verified in the browser: a
   keyboard-focused email field matched :focus-visible and still computed
   outline-style:none. A tinted 1px border is not a focus indicator; on this
   ground it is nearly invisible, and it is the only cue a keyboard user gets.

   Restored as a ring rather than by unpicking each rule, so a new input cannot
   reintroduce the same bug by copying an existing one. */
input:focus-visible,
textarea:focus-visible,
select:focus-visible{
  outline:2px solid var(--lime) !important;
  outline-offset:2px;
}


/* ── 320px ────────────────────────────────────────────────────────────────
   The narrowest phone still in real use — iPhone SE 1st gen, Galaxy Fold
   closed, and every Android budget device. Measured at 320: the document came
   out 356px wide, so the whole page could be dragged sideways.

   The driver was the masthead again. At 375 the brand, the clock and the two
   buttons fit once the date and the word "Search" are hidden; at 320 they do
   not, and the theme button was pushed 12px past the edge. The brand gives up
   the space, because a wordmark can lose two points and stay a wordmark while
   a clock that wraps stops being readable. */
@media(max-width:400px){
  .brand{font-size:14px;gap:6px}
  .stamp{gap:6px}
  .stamp .live{font-size:11px}
  .cmdk-hint{padding-inline:8px}
}
/* The phone bar's longest label — "Portfolio" — was clipping into an ellipsis
   at this width. Six labels have to fit six equal columns, so the type gives
   way rather than the word. */
@media(max-width:380px){
  .botnav-a .botnav-t{font-size:11px;letter-spacing:.02em}
  .botnav-a{padding-inline:1px}
}


/* ── RENDER ONLY WHAT IS ON SCREEN ────────────────────────────────────────
   Measured on production: 14,007 DOM nodes across 22 sections, a document
   54,308px tall — seventy-five screens of content — and a first forced layout
   costing 96.7ms. Every one of those sections was being laid out and painted
   on load so that a reader could see the one at the top.

   content-visibility:auto tells the browser to skip layout, paint and hit
   testing for a section while it is off screen, and to do the work when it
   scrolls near. It is the right tool rather than deferring the MARKUP because
   the markup staying in the document is what keeps this page working with
   JavaScript off, keeps every #anchor and the command palette resolving, and
   keeps find-in-page able to reach a section the reader has not scrolled to.

   contain-intrinsic-size carries the `auto` keyword, so once a section has
   been rendered once the browser remembers its real height and reuses it when
   it scrolls away. Without that the scrollbar grows and shrinks as you scroll
   — the classic virtualised-list jitter — and on a 54,000px page that is very
   visible. The 1200px is only the FIRST guess, before anything is measured. */
.sec{
  content-visibility:auto;
  /* Per-section fallback heights are emitted in the generated block under the
     nav, from SECTION_INTRINSIC. This is the floor for anything not in it. */
  contain-intrinsic-size:auto 1900px;
}
/* The hero, the record band and the trust strip are always on screen at load
   and must never be deferred: skipping them would mean the first paint has to
   wait for the skip to be undone, which is slower than not skipping at all. */
.record,.trust,.hero{content-visibility:visible}

/* Printing renders the whole document at once with no viewport to be "near",
   so a skipped section can print blank. Turned off entirely for print. */
@media print{
  .sec{content-visibility:visible;contain-intrinsic-size:auto}
}
/* Someone who has asked for reduced motion is often also asking for fewer
   surprises; a scrollbar that resizes while they read is one. They get the
   remembered-size behaviour too — this is here to document that
   contain-intrinsic-size:auto is doing that job for everyone. */


/* ══════════════════════════════════════════════════════════════════════════
   BROADSHEET PASS
   A newspaper does not put a rounded box round every idea. It uses a rule, a
   change of type, and space. Twenty-three card surfaces on this page each drew
   an outline and a shadow to say "these things belong together" — which a
   hairline and 24px of air say better and quieter.

   What is deliberately NOT touched: the provenance pills, and up/down. Colour
   now has exactly one job on this page — where a number came from, and which
   way it went. Everything that used to compete with that is ink now.
   ══════════════════════════════════════════════════════════════════════════ */

/* Cards become rule-separated blocks. Kept as a class rather than unpicked at
   23 call sites so the markup, the JS that builds cards, and every future card
   inherit the change. */
/* .card and .tblwrap are defined in the light-theme block above, which
   out-specifies anything set here — see the note there. Only the hover
   transform, which that block does not set, remains. */
.card:hover{transform:none}

/* Section headings: the serif does the work. This is the single biggest
   change to how the page reads — the display face was carrying data and the
   serif was decorating headings, and a broadsheet is the other way round. */
.stitle{
  font-family:var(--serif);
  font-weight:600;
  font-size:clamp(30px,5.2vw,58px);
  line-height:1.01;
  letter-spacing:-.028em;
}
.shead{
  border-bottom:1px solid var(--line2);
  padding-bottom:clamp(14px,2vw,20px);
  margin-bottom:clamp(20px,3vw,32px);
  align-items:flex-start;
}
/* The eyebrow loses its coloured bar for a single hairline. The pillar hue is
   still there, at the weight it should always have been. */
.snum::before{width:16px;height:1px;border-radius:0;opacity:.9}
.snum{letter-spacing:.18em;font-size:11px}

/* Subheads get a rule above rather than a box around. */
.subhead{
  border-top:1px solid var(--line);
  padding-top:16px;
  /* Was clamp(26px,3.4vw,40px) — this is the rule that actually won, and the
     40px measured between a subhead and the block above it fourteen times on
     one page. The rule above it already draws the separation; the space was
     doing the same job twice. */
  margin-top:clamp(20px,2.2vw,26px);
}
.subeyebrow{color:var(--dim);letter-spacing:.18em}

/* Tables lose the panel and keep the grid. On paper a table IS the container. */
.tw{
  background:transparent;border:0;border-radius:0;
  border-top:1px solid var(--line2);border-bottom:1px solid var(--line2);
}
/* A STICKY HEADER MUST BE OPAQUE.
   The broadsheet pass set these transparent to drop the header panel, and a
   sticky element with no background does not hide what scrolls beneath it —
   the rows slid under the header and both drew in the same place. That is the
   overlapping "SYMBOL COMPANY LISTED…" over the first data row.

   It stays a header, so it keeps a surface. The card ground, not the old grey
   panel, so it reads as part of the table rather than a chrome bar. */
.tblwrap table th,.tw table th{
  background:var(--surface);
  box-shadow:0 1px 0 var(--line2);
}
.tblwrap table tbody tr:hover,.tw table tbody tr:hover{background:var(--surface2)}

/* KPI tiles: hairline grid, no fills. The number is the object; the box was
   never the object. */
.kpi-row{
  display:grid;grid-template-columns:repeat(auto-fit,minmax(min(150px,100%),1fr));
  gap:0;border-top:1px solid var(--line2);border-bottom:1px solid var(--line2);
}
.kpi{
  background:transparent;border:0;border-radius:0;
  border-left:1px solid var(--line);
  padding:16px 16px 15px;
}
.kpi:first-child{border-left:0;padding-left:0}
.kpi .v{font-family:var(--disp);letter-spacing:-.03em}
/* The total is the answer to the question the book is asking, so it is the
   one tile that reads as a conclusion rather than a component. Rule, not a
   fill — a coloured panel here would fight the up/down colour the number
   itself carries. */
.mlive{font-family:var(--mono);font-size:11px;letter-spacing:.04em;
  padding:2px 7px;border:1px solid var(--line);border-radius:3px;white-space:nowrap}
.mlive .mlv{font-weight:700}
.mlive .mdrift{margin-left:6px}
/* An order whose price has already run past the entry is not an order any
   more, and that is the single most useful thing this row can say. */
.mrow[data-live="gone"]{opacity:.62}
.mrow[data-live="gone"] .mlive{border-color:var(--dn,#c0392b)}
.markline{margin:-8px 0 18px;font-family:var(--mono);font-size:11px;color:var(--dim);
  letter-spacing:.03em}
.markstamp{display:inline-flex;align-items:center;gap:6px}
.markstamp::before{content:'';width:6px;height:6px;border-radius:50%;
  background:var(--dim);flex:none}
.markstamp.on::before{background:var(--up,#0a7c3f)}
.kpi-total{border-left:2px solid var(--line2);padding-left:14px}
.kpi-total .v{font-size:clamp(24px,3vw,32px)}

/* The Record band, restated in the same language. */
.record-in{border-top:3px solid var(--text);border-bottom:1px solid var(--line2)}
.record-grid{background:transparent;border:0;border-top:1px solid var(--line);gap:0}
.rec{background:transparent;border-left:1px solid var(--line);padding:15px 15px 14px}
.rec:nth-child(3n+1){border-left:0;padding-left:0}
.record-h{font-size:clamp(26px,3.4vw,40px)}
.record-h em{color:var(--text);font-style:italic}
.record-cta{border-bottom-width:1px;border-bottom-color:var(--text)}

/* Smart money flow: a chart on paper, not a widget in a box. */
.smf{background:transparent;border:0;border-top:1px solid var(--line2);
  border-bottom:1px solid var(--line);border-radius:0;padding:16px 0 12px}


/* ── "WHY, AND HOW IT IS MEASURED" ────────────────────────────────────────
   The disclosure that now holds every explanation longer than one sentence.

   Styled to be found, not hidden. The whole argument of this page is that its
   methods are inspectable, so the control that opens a method has to look like
   an offer — a rule, a marker, and a label that says what is behind it. A
   caret in 9px grey would have been "hiding the prose", which is the opposite
   of what was asked for.

   With JavaScript off no split happens and the full paragraph renders as it
   always did. Nothing here removes text from the document: find-in-page and
   screen readers still reach every word, open or closed. */
.why{margin-top:10px;border-top:1px solid var(--line)}
.why>summary{
  list-style:none;cursor:pointer;
  display:inline-flex;align-items:center;gap:8px;
  padding:9px 0 0;
  font:600 11px/1 var(--mono);letter-spacing:.11em;text-transform:uppercase;
  color:var(--muted);
  transition:color .15s var(--ease);
}
.why>summary::-webkit-details-marker{display:none}
/* A plus that becomes a minus. Unambiguous at 11px in a way a caret is not,
   and it needs no icon font. */
.why>summary::before{
  content:'+';font:400 14px/1 var(--mono);color:var(--lime);
  width:15px;height:15px;display:inline-flex;align-items:center;justify-content:center;
  border:1px solid var(--lime-line);flex:none;
}
.why[open]>summary::before{content:'\2212'}
.why>summary:hover,.why>summary:focus-visible{color:var(--text)}
.why[open]>summary{color:var(--text)}
/* On a phone the summary is the control that opens every explanation on the
   page, and it measured 26px tall — under every platform's 44px minimum. The
   padding goes on the summary rather than the marker so the whole line is the
   target, not just the plus sign. */
@media(max-width:760px){
  .why>summary{padding:13px 0;min-height:44px}
}
.why-body{
  margin:10px 0 12px;
  font:400 14px/1.68 var(--sans);color:var(--muted);
  max-width:66ch;
  border-left:2px solid var(--line2);padding-left:15px;
}
/* The lede that remains visible carries more weight than it did when it was
   the first third of a wall of text. */
.sdesc,.subdesc{font-size:14px;line-height:1.6;max-width:56ch}


/* ── THE EQUITY CURVE ─────────────────────────────────────────────────────
   The most honest object on the page: every closed signal in order, and where
   that leaves you. It currently goes down, and it is drawn at full size at the
   top of the page rather than tucked into Performance, because a ledger that
   publishes its losses quietly has not really published them.

   Zero is a solid rule, not a faint one — it is the only line on the chart
   that means anything, and where the curve sits relative to it is the entire
   message. */
.rcurve{margin:0 0 18px;padding:0;grid-column:1/-1}
.rcurve-cap{
  display:flex;justify-content:space-between;align-items:baseline;gap:14px;
  flex-wrap:wrap;margin-bottom:9px;
  font:400 11px/1.4 var(--mono);color:var(--dim);letter-spacing:.03em;
}
.rcurve-now{font-weight:600;font-variant-numeric:tabular-nums}
.rcurve-now.up{color:var(--up)} .rcurve-now.dn{color:var(--down)}
.rcurve-plot{width:100%;height:120px}
.rcurve-plot svg{display:block;width:100%;height:100%;overflow:visible}
.rc-zero{stroke:var(--text);stroke-width:1;opacity:.55}
.rc-line{fill:none;stroke:var(--text);stroke-width:1.75;
  stroke-linejoin:round;stroke-linecap:round}
.rc-fill{stroke:none}
.rc-fill.dn{fill:var(--down);opacity:.10}
.rc-fill.up{fill:var(--up);opacity:.10}
.rc-end{fill:var(--text)}
@media(max-width:640px){ .rcurve-plot{height:96px} }
@media (prefers-reduced-motion:no-preference){
  .rc-line{stroke-dasharray:var(--len,0);stroke-dashoffset:var(--len,0);
    animation:rcdraw 1.1s var(--ease) .25s forwards}
  @keyframes rcdraw{to{stroke-dashoffset:0}}
}


/* ══════════════════════════════════════════════════════════════════════════
   PHONE: THE APP SURFACE
   Desktop keeps the broadsheet — rules, newsprint, serif. A phone is not a
   broadsheet, and pretending otherwise is why a sixteen-column ledger row was
   a horizontal scroll nobody performed.

   Below 760px the page becomes what an Indian finance app looks like: a
   tinted ground, white cards with a small radius, dense type, status carried
   by a chip. Same data, same markup, same everything — a different surface for
   a different hand.
   ══════════════════════════════════════════════════════════════════════════ */
/* ══════════════════════════════════════════════════════════════════════════
   THE APP SURFACE — EVERY SCREEN, NOT JUST PHONES.
   This was written inside a max-width:760px query, which meant a desktop
   reader saw none of it and reported, correctly, that nothing had changed.
   The card-and-chip layout is the design now, at every width; the phone rules
   further down only adjust density, not the system.
   ══════════════════════════════════════════════════════════════════════════ */
:root:not([data-theme]),
:root[data-theme="light"]{
  --bg:#EAEFF5;            /* tinted, so a white card has somewhere to sit */
  --surface:#FFFFFF;
  --surface2:#F5F7FA;
  --surface3:#E3E9F1;
  --line:rgba(20,26,36,.11);
  --line2:rgba(20,26,36,.20);
}
body{background:var(--bg)}

/* A section is a card. This is the single change that makes the page read as
   a product rather than a scroll: a bounded white surface with its own
   heading, on a ground that is not white. */
/* The card itself is defined in the light-theme block above — see the note
   there about why it cannot be set from here. */
.shead{border-bottom:1px solid var(--line);padding-bottom:14px;margin-bottom:18px}
.tblwrap,.tw{border:0}
/* Cards inside a card need to stop being cards, or it is boxes all the way
   down. Inside a section they are rule-separated blocks. */
main section.sec .card{background:transparent;border:0;border-top:1px solid var(--line);border-radius:0}

@media(max-width:760px){
  main section.sec{border-radius:6px}
  .stitle{font-size:clamp(22px,6.4vw,28px);line-height:1.08}

  /* ── ROWS BECOME CARDS ──────────────────────────────────────────────
     data-label comes from the column heading at runtime. The header row is
     hidden because every cell now carries its own header. */
  table.t-cards thead{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
  table.t-cards,table.t-cards tbody{display:block;width:100%;min-width:0}
  table.t-cards tr{
    display:grid;grid-template-columns:repeat(2,minmax(0,1fr));
    gap:0 10px;
    border:1px solid var(--line);border-radius:6px;
    background:var(--surface);
    padding:10px 12px;margin-bottom:8px;
  }
  table.t-cards td{
    display:flex;align-items:baseline;justify-content:space-between;gap:8px;
    border:0;border-top:1px solid var(--line);padding:6px 0;
    font-size:12px;min-width:0;
  }
  table.t-cards td::before{
    content:attr(data-label);
    font:600 11px/1.3 var(--mono);letter-spacing:.09em;text-transform:uppercase;
    color:var(--dim);flex:none;
  }
  /* The first two cells are the card's headline — usually date and symbol —
     so they span the full width, lose their label, and get real weight. */
  table.t-cards td:nth-child(1),
  table.t-cards td:nth-child(2){
    grid-column:1/-1;border-top:0;padding-top:0;
    font-size:14px;font-weight:600;
  }
  table.t-cards td:nth-child(1)::before{font-size:11px}
  table.t-cards td:nth-child(2)::before{display:none}
  table.t-cards td:nth-child(2){font-family:var(--disp);letter-spacing:-.01em}
  /* Status is the thing you scan for, so it goes last and full width. */
  table.t-cards td:last-child{grid-column:1/-1;justify-content:flex-start;gap:10px}
  /* A "nothing matches" row is a message, not a record. */
  table.t-cards tr.t-cards-msg{display:block;text-align:center}
  table.t-cards tr.t-cards-msg td{display:block;border:0}
  table.t-cards tr.t-cards-msg td::before{display:none}

  /* Filter chips scroll sideways instead of wrapping into four rows. */
  .filters,.ctlbar{
    display:flex;flex-wrap:nowrap;overflow-x:auto;gap:7px;
    padding-bottom:6px;overscroll-behavior-x:contain;
  }
  .filters::-webkit-scrollbar,.ctlbar::-webkit-scrollbar{display:none}
  .fbtn{flex:none;min-height:36px}
}

/* ── SORTABLE LEDGER HEADERS ──────────────────────────────────────────────
   Desktop only in practice, since the phone hides the header row entirely —
   which is correct: sorting a list you read as cards belongs in a control, and
   the filter chips already are that control. */
.t th.sortable{cursor:pointer;user-select:none;white-space:nowrap}
.t th.sortable:hover{color:var(--text)}
.t th.sortable[aria-sort]{color:var(--lime)}
.t th.sortable[aria-sort=descending]::after{content:" \25BE"}
.t th.sortable[aria-sort=ascending]::after{content:" \25B4"}


/* The edition banner, at two lengths. Desktop has room for the full
   explanation; a phone does not, and eleven lines of caveat above the fold is
   a worse first impression than the staleness it is warning about. */
.ed-short{display:none}
@media(max-width:760px){
  .ed-long{display:none}
  .ed-short{display:inline}
  .editionbar{font-size:12px;padding:9px 12px;gap:9px;align-items:center}
  .editionbar #editionReload{flex:none;white-space:nowrap;padding:7px 11px;font-size:11px}
}


/* ── THE LEGEND, PROPERLY ─────────────────────────────────────────────────
   Four kinds of claim, four solid chips. They were tinted outlines that read
   as decoration; on a white card a filled chip reads as a STATUS, which is
   what these are — and status chips are the thing Chittorgarh's layout gets
   right and this page did not.

   Colour on this page now means exactly two things and they never overlap:
   a chip says where a number came from, and up/down says which way it went. */
.pill{
  font:700 11px/1 var(--mono);letter-spacing:.1em;text-transform:uppercase;
  padding:4px 7px;border-radius:3px;border:0;color:#fff;
  white-space:nowrap;flex:none;
}
.pill-fact  {background:#1F6FB2}   /* observed */
.pill-model {background:#6A4CC4}   /* computed */
.pill-result{background:#0E7A55}   /* what happened */
.pill-view  {background:#8A5A18}   /* opinion, labelled */

/* The legend strip itself becomes a real key: a bounded card at the top of the
   page that a reader can return to, not a line of grey text they scroll past. */
.prov-legend{
  background:var(--surface);
  border:1px solid var(--line);border-radius:8px;
  padding:14px 16px;
  display:flex;flex-wrap:wrap;gap:10px 22px;align-items:center;
}
.prov-legend .pl-lead{
  font:700 11px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase;
  color:var(--text);width:100%;margin-bottom:2px;
}
.prov-legend .pl-item{
  display:flex;align-items:center;gap:8px;
  font:400 12px/1.45 var(--sans);color:var(--muted);
}

/* Freshness badges get the same treatment — they are statuses too, and the
   whole point of a data-health badge is that it is legible at a glance. */
.dh{
  font:700 11px/1 var(--mono);letter-spacing:.1em;text-transform:uppercase;
  padding:4px 6px;border-radius:3px;border:0;color:#fff;margin-left:8px;
  vertical-align:middle;
}
.dh-LIVE{background:#0E7A55}
.dh-FRESH{background:#1F6FB2}
.dh-STALE{background:#8A7320}
.dh-DEGRADED{background:#8A5A18}
.dh-FAILED,.dh-UNAVAILABLE{background:#B4231A}

/* The metric badges app.js stamps on every KPI follow the same key, so a
   reader learns four colours once and they hold everywhere on the page. */
a.mprov{opacity:1;color:#fff;border:0;font-weight:700}
.mprov-fact{background:#1F6FB2}
.mprov-model{background:#6A4CC4}
.mprov-result{background:#0E7A55}
.mprov-view{background:#8A5A18}


/* A period under a column head. "RSI 68" is unreadable without knowing whether
   that is a day or a week, and the answer belongs in the column, not in a
   glossary the reader has to go and find. */
.th-sub{display:block;font-weight:400;font-size:11px;letter-spacing:.06em;
  color:var(--dim);text-transform:none;margin-top:2px}


/* ── WHAT SITS ON THE BRAND COLOUR ────────────────────────────────────────
   Every primary button was color:#000 on background:var(--lime). That was
   readable while --lime was a bright olive. It is now a dark navy, and black
   text on dark navy measured 1.94:1 — Subscribe, Add to book, + Track, the
   active filter chip and the back-to-top button were all effectively
   unreadable, on every page, since the palette changed.

   The lesson is not "check the buttons". It is that changing a token means
   checking everything painted ON that token, and nothing was measuring it.
   The audit that found this walks 9,264 text elements and composites the full
   stack of translucent backgrounds down to the page before computing a ratio —
   the first two passes reported false failures because they treated a 7% tint
   as opaque. */
.btn,.fbtn.on,.fab{color:var(--on-brand)}
/* Metadata that had faded to 1.26:1 — an age stamp nobody could read is not a
   quieter age stamp, it is a missing one. */
.dh-age{color:var(--muted)}


/* ══════════════════════════════════════════════════════════════════════════
   TYPE, MEASURED AND FIXED
   Audited on the rendered page: 34 distinct font sizes across 9,264 text
   elements, 2,064 of them under 11px, and JetBrains Mono — the MONOSPACE
   face — carrying 7,204 of them. The body face was doing 1,890 and the serif
   54. That is why the type read as "some small, some huge, some different":
   the page was set in a code font at thirty-four sizes.

   Every literal px size is now snapped to a ten-step scale and nothing sits
   below 11px. This block does the second half: mono goes back to being for
   NUMBERS, and prose gets the text face.

   Mono keeps: figures, tickers, timestamps, code, the eyebrow labels where
   its width is doing real alignment work. Mono loses: descriptions, notes,
   list bodies, anything that is a sentence.
   ══════════════════════════════════════════════════════════════════════════ */
.sdesc,.subdesc,.why-body,.md-what,.md-how,.record-p,.fnote,
.mandate-foot,.dh-note,.prov-legend .pl-item,.lv-3,
.empty,.mv-list,.fund-note,.note,.hero-sub{
  font-family:var(--sans);
}
/* Numbers stay aligned wherever they are compared down a column. */
.num,.rec-v,.kpi .v,.stat .v,.rcurve-now,.volbar-x,
table td.num,table .num,.mono-dim{
  font-family:var(--mono);
  font-variant-numeric:tabular-nums;
}
/* The Arial that leaked through: two elements had no family at all. */
body,button,input,select,textarea{font-family:var(--sans)}

/* One reading measure. Prose that runs the full width of a 1,271px card is
   unreadable regardless of which face it is set in. */
.sdesc,.subdesc,.why-body,.md-how,.record-p{max-width:62ch}


/* ── THE DAY, IN LABELLED BLOCKS ──────────────────────────────────────────
   Semafor's Semaform, applied to a market summary: each block says what KIND
   of claim it is before it says anything else. The labels are the same four
   this page already uses everywhere, so the reader learns one key. */
.dayblocks{
  display:grid;grid-template-columns:repeat(auto-fit,minmax(min(240px,100%),1fr));
  gap:1px;background:var(--line);border:1px solid var(--line);border-radius:8px;
  overflow:hidden;margin-bottom:clamp(20px,2.6vw,30px);
}
.dayblock{background:var(--surface);padding:16px 18px 18px;display:flex;
  flex-direction:column;gap:10px;min-width:0}
.dayblock.db-wide{grid-column:1/-1}
/* The flow block's three figures, stacked. app.js rewrites this container's
   innerHTML with the same .kpi markup the old three-up row used, so the
   children are fixed and only the container can change — which is why this is
   a column rule here rather than a different markup shape there. Stacked
   because the block is one column of a four-column grid: three KPIs side by
   side inside ~280px gives each about ninety pixels, and "₹-1,200 Cr" does not
   fit in ninety pixels. */
.dbflow{display:flex;flex-direction:column;gap:7px}
.dbflow .kpi{display:flex;align-items:baseline;justify-content:space-between;
  gap:10px;padding:0;background:none;border:0}
.dbflow .kpi .v{font:700 var(--t-data)/1.1 var(--mono);font-variant-numeric:tabular-nums}
.dbflow .kpi .k{font:400 var(--t-label)/1 var(--mono);letter-spacing:.08em;
  text-transform:uppercase;color:var(--dim)}
.db-lab{
  align-self:flex-start;
  font:700 11px/1 var(--mono);letter-spacing:.1em;text-transform:uppercase;
  padding:5px 8px;border-radius:3px;color:#fff;
}
.db-fact{background:#1F6FB2}
.db-model{background:#6A4CC4}
.db-view{background:#8A5A18}
.db-big{
  font:700 clamp(24px,3vw,32px)/1.05 var(--disp);letter-spacing:-.03em;
  color:var(--text);font-variant-numeric:tabular-nums;
}
.db-big .up{color:var(--up)} .db-big .dn{color:var(--down)}
.db-vs{color:var(--dim);font-weight:400;margin:0 4px}
.db-note{font:400 13px/1.6 var(--sans);color:var(--muted);margin:0;max-width:52ch}
.db-note b{color:var(--text);font-weight:600}
.db-arg{max-width:none}
/* The split, as one picture rather than two numbers. */
.db-bar{display:flex;height:8px;border-radius:2px;overflow:hidden;background:var(--surface3)}
.db-up{background:var(--up)} .db-dn{background:var(--down)}

/* ── SECTOR CUBES ─────────────────────────────────────────────────────────
   A treemap-style block per sector, coloured in five steps by size of move.
   Eleven bordered cards each holding one percentage is a list wearing a
   grid's clothes — you read it name by name. This is read in one look: which
   half of the market is green, and how hard.

   Steps, not a gradient: five buckets a reader can name beat two hundred
   shades nobody can tell apart. */
.heatcubes{
  display:grid;grid-template-columns:repeat(auto-fit,minmax(min(132px,100%),1fr));
  gap:4px;
}
.hcube{
  border-radius:4px;padding:13px 12px 12px;min-height:74px;
  display:flex;flex-direction:column;justify-content:space-between;gap:8px;
  border:1px solid transparent;
}
.hc-n{font:600 12px/1.25 var(--sans);letter-spacing:-.01em}
.hc-p{font:700 16px/1 var(--mono);font-variant-numeric:tabular-nums;letter-spacing:-.02em}
/* Flat is deliberately grey, not pale green. A sector that did nothing should
   not read as a small win. */
.h-flat{background:var(--surface2);color:var(--muted);border-color:var(--line)}
.h-up.s-1{background:color-mix(in srgb,var(--up) 14%,var(--surface));color:var(--text)}
.h-up.s-2{background:color-mix(in srgb,var(--up) 42%,var(--surface));color:var(--text)}
.h-up.s-3{background:var(--up);color:#fff}
.h-dn.s-1{background:color-mix(in srgb,var(--down) 14%,var(--surface));color:var(--text)}
.h-dn.s-2{background:color-mix(in srgb,var(--down) 42%,var(--surface));color:var(--text)}
.h-dn.s-3{background:var(--down);color:#fff}
@media(max-width:600px){
  .heatcubes{grid-template-columns:repeat(auto-fit,minmax(min(104px,100%),1fr));gap:3px}
  .hcube{min-height:62px;padding:10px 9px}
  .hc-p{font-size:14px}
}


/* ── THE SUBSCRIBE BLOCK ──────────────────────────────────────────────────
   It was a white card with a navy bar down one edge and a navy button — the
   bar read as an alert stripe, the button was still black-on-navy until the
   sweep above, and the whole thing looked like a warning rather than an
   invitation. Six other brand-filled elements had the same unreadable text,
   including this button; the first fix caught three of nine because the test
   only checked one of them.

   Inverted instead: the block IS the brand colour, the type is white on it,
   and the button is white with brand-coloured text. One block, high contrast
   both ways, and it stops competing with the section cards around it. */
.sub-cta{
  background:var(--lime);
  border:0;border-left:0;border-radius:8px;
  color:var(--on-brand);
  padding:clamp(20px,2.6vw,30px);
}
/* Everything in here is --on-brand, never a literal. My first version set
   white and the contrast test failed it immediately: white is 10.8:1 on the
   light theme's navy and 1.3:1 on the dark theme's bright lime. The token is
   the only thing that is correct in both. */
.sub-cta h3{color:var(--on-brand)}
.sub-cta p,.sub-cta .mono-dim,.sub-cta small{color:var(--on-brand);opacity:.84}
.sub-cta .sub-form input[type=email]{
  background:var(--on-brand);
  border:1px solid var(--on-brand);
  color:var(--lime);
}
.sub-cta .sub-form input[type=email]::placeholder{color:var(--lime);opacity:.6}
.sub-cta .sub-form input[type=email]:focus-visible{
  outline:2px solid var(--on-brand);outline-offset:2px;
}
/* Reversed out: the plate is whatever the text on the brand would be, and the
   text is the brand. 10.8:1 in light, and the same figure inverted in dark. */
.sub-cta .sub-form button{
  background:var(--on-brand);color:var(--lime);font-weight:700;
}
.sub-cta .sub-form button:hover:not(:disabled){opacity:.9}
.sub-cta a{color:var(--on-brand);text-decoration-color:currentColor}


/* ── FUND CATEGORIES: SUMMARY, THEN DETAIL ───────────────────────────────
   Six categories, three cards each, three CAGR bars per card and a paragraph
   of category facts is the whole mutual-fund section open at once — several
   screens of it for a reader who mostly wants to know which fund leads.
   One line per category now; everything else is one click down. */
details.fundcat{border-top:1px solid var(--line);padding:0}
details.fundcat:first-of-type{border-top:0}
.fundcat-sum{
  list-style:none;cursor:pointer;
  display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;
  padding:14px 0;min-height:44px;
}
.fundcat-sum::-webkit-details-marker{display:none}
.fundcat-sum::before{
  content:'+';font:400 14px/1 var(--mono);color:var(--lime);flex:none;
  width:16px;height:16px;display:inline-flex;align-items:center;justify-content:center;
  border:1px solid var(--lime-line);align-self:center;
}
details.fundcat[open] .fundcat-sum::before{content:'\2212'}
.fc-name{font:600 14px/1.3 var(--disp);color:var(--text);letter-spacing:-.01em}
.fc-lead{font:400 13px/1.4 var(--sans);color:var(--muted);flex:1 1 200px;min-width:0;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fc-r3{font:700 14px/1 var(--mono);color:var(--up);font-variant-numeric:tabular-nums}
.fc-r3 i{font-style:normal;font-size:11px;color:var(--dim);margin-left:3px}
.fc-n{font:400 11px/1 var(--mono);color:var(--dim);flex:none}
@media(max-width:600px){
  .fc-lead{flex-basis:100%;order:3}
}


/* ── THE DECISION LOG ─────────────────────────────────────────────────────
   Not a holdings table. Holdings say what is there now; this says what the
   book DID, in the order it did it, with the money attached — which is the
   only way a mechanical allocator can be audited rather than trusted.

   Grid, not table: six fixed columns that stay aligned down the page and
   collapse to two rows on a phone without needing the card treatment. */
.dlog{margin:18px 0 4px;border-top:1px solid var(--line2)}
.dlog-h{display:flex;justify-content:space-between;align-items:baseline;
  gap:12px;flex-wrap:wrap;padding:12px 0 10px}
.dlog-t{font:600 13px/1.2 var(--disp);color:var(--text);letter-spacing:-.01em}
.dlog-r{
  display:grid;
  grid-template-columns:84px minmax(72px,1fr) 62px minmax(80px,1fr) minmax(90px,1fr) minmax(84px,auto);
  gap:10px;align-items:baseline;
  padding:9px 0;border-top:1px solid var(--line);
  font:400 12px/1.4 var(--mono);
}
.dl-d{color:var(--dim)}
.dl-s{font-weight:700;color:var(--text);text-decoration:none;
  border-bottom:1px dotted var(--line2)}
.dl-s:hover{border-bottom-style:solid}
/* The verb is the point of the row, so it is the only coloured thing in it
   until the outcome. */
.dl-v{font-size:11px;letter-spacing:.08em;text-transform:uppercase;font-weight:700}
.dl-sized{color:var(--p-markets)}
.dl-closed{color:var(--dim)}
.dl-a,.dl-o{font-variant-numeric:tabular-nums}
.dl-t{color:var(--dim);font-size:11px}
.dl-o{text-align:right;font-weight:700}
.dl-o.up{color:var(--up)} .dl-o.dn{color:var(--down)}
.dl-open{color:var(--dim);font-weight:400}
@media(max-width:700px){
  .dlog-r{grid-template-columns:1fr 1fr 1fr;gap:4px 10px}
  .dl-s{grid-column:1/3;font-size:13px}
  .dl-o{grid-column:3;grid-row:1}
  .dl-d,.dl-v,.dl-t{font-size:11px}
}


/* ── MULTI-COLUMN, WHERE IT ACTUALLY HELPS ───────────────────────────────
   The WSJ ask was multi-column text. A broadsheet runs continuous columns
   because its page is fixed and tall; this page is a responsive card grid,
   and setting a card's contents in columns means a reader scrolls DOWN to the
   bottom of column one and then back UP to the top of column two. On a
   scrolling page that is worse than one column, not better.

   So columns go where the shape is right: long-form prose that is read in one
   sitting and sits inside a bounded block — the disclosure bodies and the
   definition list. Both are finite, both are read straight through, and both
   currently run to a single very long measure on a wide screen.

   Only above 1100px, where two columns are each still 45+ characters. Below
   that a second column is narrower than a phone. */
@media(min-width:1100px){
  .why[open] .why-body{
    columns:2;column-gap:38px;column-rule:1px solid var(--line);
    max-width:none;
  }
  /* A heading or a figure orphaned at the foot of column one is the classic
     multi-column failure; this keeps each definition whole. */
  .metdefs{columns:2;column-gap:44px;column-rule:1px solid var(--line)}
  .metdef{break-inside:avoid;display:block;padding:14px 0}
  .metdef dt{margin-bottom:6px}
  .metdef .md-what,.metdef .md-how{max-width:none}
}
/* Print is the one place a real broadsheet column makes sense: the page stops
   scrolling and becomes a sheet. */
@media print{
  .sdesc,.subdesc,.why-body{columns:2;column-gap:32px}
  .why{display:block}
  .why>summary{display:none}
  .why-body{border:0;padding:0}
}


/* ── ONE DASHBOARD, NOT TWO BOXES ────────────────────────────────────────
   The day blocks and the index tiles were two boxes stacked directly on top
   of each other answering the same question — what did the market do today —
   each with its own heading, freshness badge and explanation. They are one
   surface now: claim blocks, then the board, then what the scans found. */
.dash-tiles,.dash-find{
  border-top:1px solid var(--line);
  padding-top:14px;margin-top:16px;
}
.dash-tiles-h{
  display:flex;justify-content:space-between;align-items:baseline;
  gap:12px;flex-wrap:wrap;margin-bottom:12px;
}
.dash-tiles-t{
  font:600 11px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase;
  color:var(--dim);
}
.dash-more{
  font:600 11px/1 var(--mono);letter-spacing:.06em;text-transform:uppercase;
  color:var(--lime);text-decoration:none;
}
.dash-more:hover{text-decoration:underline}

/* Key findings: the count first, because the count is the finding. */
.dfind-row{
  display:grid;grid-template-columns:repeat(auto-fit,minmax(min(210px,100%),1fr));
  gap:1px;background:var(--line);border:1px solid var(--line);border-radius:6px;
  overflow:hidden;
}
.dfind{background:var(--surface);padding:13px 14px;display:flex;
  flex-direction:column;gap:4px;min-width:0}
.dfind-n{font:700 24px/1 var(--disp);letter-spacing:-.03em;color:var(--text);
  font-variant-numeric:tabular-nums}
.dfind-t{font:600 13px/1.35 var(--sans);color:var(--text)}
.dfind-r{font:400 11px/1.45 var(--mono);color:var(--dim)}


/* ── IPO CARDS: SUMMARY, THEN EVERYTHING ─────────────────────────────────
   Each card carries band, dates, lot size, subscription by category, both
   sides of the argument, the score breakdown and the grey-market caveat.
   Eight open at once is the whole section. The summary is the line a reader
   scans — symbol, company, subscription, verdict — and every one of those
   details is one click away, not removed. */
details.ipo-card{padding:0}
.ipo-sum{
  list-style:none;cursor:pointer;
  display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;
  padding:13px 14px;min-height:44px;
}
.ipo-sum::-webkit-details-marker{display:none}
.ipo-sum::before{
  content:'+';font:400 14px/1 var(--mono);color:var(--dim);flex:none;
  width:16px;height:16px;display:inline-flex;align-items:center;justify-content:center;
  border:1px solid var(--line2);align-self:center;
}
details.ipo-card[open] .ipo-sum::before{content:'\2212'}
.ipo-sum .sym{font:700 14px/1.2 var(--mono);color:var(--text)}
.ipo-sum-co{font:400 12px/1.4 var(--sans);color:var(--muted);
  flex:1 1 140px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ipo-sum-x{font:700 13px/1 var(--mono);color:var(--text);font-variant-numeric:tabular-nums}
details.ipo-card > *:not(summary){padding-inline:14px}
details.ipo-card > *:last-child{padding-bottom:14px}


/* The screen's vintage, on the section rather than in a provenance strip
   seventeen lines down. Every technical column here is as old as this date,
   and a reader who checks a number against a live chart deserves to meet that
   fact before the number, not after. */
.screen-vintage{
  display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;
  margin:12px 0 0;padding:10px 12px;
  background:var(--surface2);border-left:3px solid var(--p-research);border-radius:0 4px 4px 0;
  font:400 12px/1.55 var(--sans);color:var(--muted);max-width:70ch;
}
.sv-tag{
  font:700 11px/1 var(--mono);letter-spacing:.1em;
  color:var(--p-research);flex:none;
}

/* ══════════════════ TABLE SYSTEM — NUMERIC ALIGNMENT ══════════════════════
   The defect, stated exactly: `.num` is declared FOUR times in this file
   (two theme rules, a tabular-figures rule, a font-family rule) and not one
   of them ever set text-align. It carried the mono face and tabular figures
   and stopped there. So every numeric column on the page was mono, was
   tabular — and was ragged left, which is the one thing tabular figures
   exist to prevent. Lining the digits up inside a cell is pointless if the
   cells themselves do not line up.

   It looked right in exactly two places, and only by accident. The plain
   .tblwrap renderer emits a `.r` class alongside `.num`, and `.r` DID carry
   the alignment. The sortable `table.t` renderer emits `.num` on its own.
   Two renderers, one convention each, and the convention that mattered was
   the one nobody had written down.

   Measured on the live page before this rule: 1,473 of 1,610 numeric cells
   ragged left. The volume board looked professional and the stock screener
   beside it did not, from one missing declaration.

   Safe as a blanket rule because `.num` has always been applied
   semantically. Of 2,052 cells carrying it, 304 are not bare numerals — and
   every one of those is still a right-edge value: "nil", "closed",
   "not measured", an F-score of "6/9", an ISO date, or a stacked money cell
   like "Rs 3,00,000 / 3.0%". No prose column carries `.num`: Symbol,
   Company, Reading, Risk, Setup and Action have none and are untouched.

   Scoped to table cells on purpose. `.num` is also worn by KPI values, hero
   stats and the record curve, which live in flex and grid containers that
   already position them — a blanket text-align would move all of those. */
table td.num,table th.num{text-align:right}

/* Mirroring `.num` onto the <th> has one side effect worth naming, because
   it was measured rather than predicted: `.tblwrap table .num` also carries
   letter-spacing:-.01em. That is correct for a column of digits — tightening
   figures helps them read as one number — and wrong for the uppercase label
   above them, which needs its tracking open to stay legible at 11px. Nine
   headers went from +1.32px to -0.11px before this rule caught it.

   The .tblwrap-scoped selector is listed explicitly: `.tblwrap table .num`
   is (0,2,1) and would outrank a bare `table th.num` at (0,1,2). */
.tblwrap table th.num,
table.t th.num,
table th.num{letter-spacing:var(--tbl-track)}

/* A column header has to point the same way as the column beneath it. Left
   labels over right figures detach at exactly the width where a column gets
   wide enough for the gap to matter, and this page has an eighteen-column
   table. The headers carry no class of their own in either renderer, so the
   class is mirrored onto them from the first body row at render time —
   window.alignTableHeaders() in app.js. This rule is what it switches on;
   the function sets no styles itself. */

/* Row hover. table.t had one, the plain tables had a different one, and the
   two were different colours. A hover that changes between two tables in
   the same section reads as two components rather than one. */
.tblwrap table tbody tr:hover,
table.t tbody tr:hover{background:var(--surface2)}

</style>
</head>

<body>
<a class="skip" href="#{{ nav[0].id }}">Skip to content</a>
<div class="cmp-back" id="cmpBack"></div>
<aside class="cmp" id="cmpPanel" role="dialog" aria-modal="true" aria-label="Comparison"></aside>
<div class="grain"></div>
<div class="vgrid"></div>
<div class="progress" id="prog"></div>

{# These used to be five {% set %} lines doing the arithmetic here, and they
   excluded `expired` from the denominator — so the hero printed 24% over 55
   while the same build's feed supported 20% over 65, and fixing the generator
   changed nothing because this block outranked it. One function now, shared
   with the generator and the social card. #}
{% set _counts = ledger_counts(alerts) %}
{% set wins    = _counts.wins %}
{% set losses  = _counts.losses %}
{% set opens   = _counts.opens %}
{% set closed  = _counts.closed %}
{% set winrate = _counts.winrate %}
{% set advancers = markets | selectattr("up") | list | length %}

<!-- ══════════ COMMAND PALETTE ══════════
     ⌘K / Ctrl-K. Replaces most of what the nav was doing: jump to a section,
     look up a symbol anywhere in the ledger, or cross to the other page — all
     without reading a 19-item scroller. Symbols come from /api/signals, which
     the page has already loaded. -->
<div class="cmdk" id="cmdk" hidden>
  <div class="cmdk-bd" data-close></div>
  <div class="cmdk-box" role="dialog" aria-modal="true" aria-label="Search">
    <input id="cmdkIn" type="text" autocomplete="off" spellcheck="false"
           aria-label="Jump to a section or search a symbol"
           placeholder="Jump to a section, or type a symbol…" aria-controls="cmdkList">
    <ul class="cmdk-list" id="cmdkList" role="listbox"></ul>
    <div class="cmdk-ft">
      <span><kbd>&uarr;</kbd><kbd>&darr;</kbd> move</span>
      <span><kbd>&crarr;</kbd> open</span>
      <span><kbd>esc</kbd> close</span>
    </div>
  </div>
</div>

<!-- ══════════ HEADER ══════════ -->
<div class="headstack">
<header class="topbar">
  <div class="topbar-in">
    <a href="#top" class="brand"><span class="dot"></span>THE DAILY <b>SIGNAL</b></a>
    <div class="stamp">
      <button type="button" class="cmdk-hint" id="cmdkOpen" aria-label="Search">
        <span>⌘K</span><span>Search</span>
      </button>
      <span class="d">{{ date_str }}</span>
      <span class="live" id="istClock"><i></i>{{ updated_at }} MYT</span>
      {# Three states, not two: a reader who has expressed no preference should
         follow their OS, and a two-way switch cannot express that. #}
      <button type="button" class="thm" id="themeBtn"
              aria-label="Theme: system. Click to change." title="Theme">
        <span class="thm-i" aria-hidden="true">◐</span>
      </button>
    </div>
  </div>
</header>

<!-- Nav order MUST match document order. It did not: Performance, Mind Gym and
     Signal Log were listed near the top while their sections sit at the very
     bottom, so the scroll spy underlined "Chess" while you were reading the
     signal log and "The Mind" while you were reading The Desk. The numbers had
     drifted too — two 05s in the nav, and section headings that disagreed with
     it. One sequence now, top to bottom, nav and headings the same. -->
{# ── TRUST STRIP ───────────────────────────────────────────────────────────
   Data freshness belongs above the numbers it qualifies, not seventeen
   sections below them. A reader who has already read a stale figure cannot
   un-read it.

   Server-rendered from the same health snapshot the Data Health section uses,
   so the strip and the section can never disagree — a client fetch would have
   been a second source for one number. The full section stays; this points
   at it. #}
{% if health and health.datasets %}
<div class="trust {{ 'ok' if health.current == health.total else ('bad' if health.worst in ('UNAVAILABLE','FAILED') else 'warn') }}">
  <div class="trust-in">
    <span class="trust-dot" aria-hidden="true"></span>
    <span class="trust-txt">
      <b>{{ health.current }} of {{ health.total }}</b> datasets current
      {%- if health.current != health.total %} &middot; worst <b>{{ (health.worst or 'unknown')|lower }}</b>{% endif %}
    </span>
    <a class="trust-link" href="#datahealth">How fresh is this? &rarr;</a>
  </div>
</div>
{% endif %}
<nav class="nav">
  <div class="nav-in" id="navin">
    {# Six destinations, not seventeen links. Seventeen anchors in a row is an
       index, not navigation — a first-time reader cannot tell which of them is
       the point. Each group is a button that opens its sections.

       The anchors still exist in the DOM inside each menu, so the scroll spy,
       the command palette and every deep link keep working unchanged. This is
       a disclosure layer over the same list, not a different list. #}
    {% for g in navgroups %}
    <div class="navgrp" data-group="{{ g.name }}">
      <button type="button" class="navgrp-btn" aria-expanded="false"
              aria-controls="navmenu-{{ loop.index }}">
        {{ g.name }}<i>{{ g.links|length }}</i>
      </button>
      <div class="navgrp-menu" id="navmenu-{{ loop.index }}" hidden>
        {% for n in g.links %}<a href="#{{ n.id }}" aria-label="{{ n.group }} — {{ n.label }}"><i>{{ n.n }}</i>{{ n.label }}</a>
        {% endfor %}
      </div>
    </div>
    {% endfor %}
    <a class="nav-other" href="{{ other_path }}" title="{{ other_hint }}">{{ other_label }} &rarr;</a>
  </div>
</nav>

{# ── PILLAR HUES ────────────────────────────────────────────────────────────
   One rule per section and one per nav group, generated from SECTION_MAP so
   the colour cannot drift from the grouping the nav already prints. Each sets
   a single --pillar variable; every rule that paints with it is in the main
   stylesheet, so this block assigns colour and never styles anything.

   The var() fallback matters: the Life page's pillars are a different five,
   and a missing token there should quietly become the house accent rather
   than an unset property that paints nothing. #}
<style>
{% for i, grp in secgroup.items() %}#{{ i }}{--pillar:var(--p-{{ grp|lower }},var(--lime));contain-intrinsic-size:auto {{ section_intrinsic.get(i, default_intrinsic) }}px}
{% endfor %}{% for g in navgroups %}.navgrp[data-group="{{ g.name }}"],.botnav-a[data-group="{{ g.name }}"]{--pillar:var(--p-{{ g.name|lower }},var(--lime))}
{% endfor %}
</style>

{# The metric dictionary, for the badge stamper in app.js. A data island rather
   than a JS literal so nothing here is executable, and generated from the same
   METRICS list that writes the definitions in How to Read This — a badge and
   its definition therefore cannot disagree. #}
<script type="application/json" id="metricProv">{{ metrics_json }}</script>

{# ── MOBILE BOTTOM NAV ──────────────────────────────────────────────────────
   The header nav is a horizontal scroller with six dropdown menus. That works
   on a desktop and is two taps and a horizontal drag on a phone, on a page
   this long — which in practice means nobody navigates at all and everybody
   scrolls.

   Five destinations, thumb-height, always there. It jumps to the FIRST section
   of each pillar rather than opening a menu: on a phone the useful question is
   "take me to markets", not "show me the four things filed under markets".
   The full menu is still in the header for anyone who wants the specific one.

   Built from navgroups, so it cannot drift from the header, and hidden above
   760px where the header nav is already usable. #}
<nav class="botnav" aria-label="Sections">
  {% for g in navgroups %}{% if g.links %}
  <a class="botnav-a" data-group="{{ g.name }}" href="#{{ g.links[0].id }}">
    <span class="botnav-t">{{ g.name }}</span>
  </a>
  {% endif %}{% endfor %}
</nav>

<!-- Live-layer status. Hidden until the API probe resolves one way or the other. -->
<div class="livebar" id="livebar">
  <span class="pip"></span>
  <span class="msg" id="livemsg">Checking live ledger…</span>
  <button type="button" id="liverefresh">Refresh</button>
</div>

<!-- A tab left open overnight kept serving the previous edition: the clock
     ticked, the ledger bar refreshed, but the masthead still read yesterday's
     date and the hero still held yesterday's numbers. Two tabs open a minute
     apart disagreed about what day it was. The page now knows its own build id
     and says so when a newer one is published. -->
<div class="editionbar" id="editionbar" data-build="{{ build_id }}">
  {# Say precisely WHAT is stale. "its markets, ideas and ledger are stale" was
     wrong on two of the three: the ticker, the heat map and the signal ledger
     all refresh from /api on their own timers and are current in this tab right
     now. A banner that overstates staleness while live numbers tick beside it
     teaches the reader to distrust both the banner and the numbers — the
     opposite of what it is for. #}
  {# Two lengths, one truth. On a phone this banner ran to eleven lines and ate
     a third of the first screen — the reader's first impression of the site was
     a paragraph about staleness, before a single number. The short form says
     the thing that matters; the long form is one tap away and still says every
     word it always did. #}
  <span class="ed-txt">
    <b>New edition published<span id="editionWhen"></span>.</b>
    <span class="ed-long">This tab was built for {{ date_str }}. Live data &mdash; the
      ticker, heat map and signal ledger &mdash; is still current; the written brief,
      trade ideas, screens and IPO cards are yesterday&rsquo;s.</span>
    <span class="ed-short">Live data here is still current. The brief, ideas and
      screens are yesterday&rsquo;s.</span>
  </span>
  <button type="button" id="editionReload">Load the new edition</button>
</div>
<!-- ══════════ TICKER ══════════
     Lives INSIDE .headstack (position:sticky) so it is on screen the whole
     way down the page. Below the hero it sat past a full viewport of
     headline, so on most screens only a sliver of it was ever visible —
     a market rail you have to scroll to find is not a market rail.
     The only market surface on the page. A "What moved" grid used to sit
     directly below carrying the identical nine instruments — same names, same
     numbers, twice, 200px apart. The grid is gone; the rail absorbed its job
     and now runs the whole board in segments, ordered by when each market
     actually opens in IST. Filled live from /api/ticker; the markup below is
     the 6 AM snapshot that renders before that resolves and on a static
     host. -->
<div class="tickwrap" id="tickWrap">
  <div class="tick" id="tickRail">
    {% for dup in [1,2] %}
    <div class="tseg" style="--sc:var(--lime)"><span class="ic">📈</span><span class="lb">Markets</span></div>
    {% for m in markets %}
    {# `m.change` does not exist — content_cache builds `change_pct`. Jinja
       renders a missing key as empty, so the 6 AM snapshot shipped "▲ " with
       no number against every instrument, and that is what a crawler, an LLM
       scraper and any reader with JS blocked saw: a market rail of bare
       arrows. It only looked right because /api/ticker overwrites the whole
       rail a moment later for everyone else. #}
    <div class="ti">
      <span class="n">{{ m.name }}</span>
      <span class="p">{{ m.price }}</span>
      <span class="c {{ 'up' if m.up else 'dn' }}">{{ '▲' if m.up else '▼' }} {{ '+' if m.change_pct > 0 else '' }}{{ m.change_pct }}%</span>
    </div>
    {% endfor %}{% endfor %}
  </div>
  <button type="button" class="tickctl" id="tickHold" aria-pressed="false">Pause</button>
</div>

</div><!-- /.headstack -->

<!-- ══════════ HERO ══════════ -->
<section class="hero" id="top">
  <div class="orb a"></div><div class="orb b"></div>

  <div class="eyebrow">◆ Compiled 6:00 AM MYT · {{ date_str }}</div>

  {% if page == 'desk' %}
  <h1 class="hl">
    <span class="w"><span style="--d:.05s">The</span></span>
    <span class="w"><span style="--d:.13s">reps</span></span><br>
    <span class="w"><span style="--d:.24s"><em>behind</em></span></span>
    <span class="w"><span style="--d:.32s"><em>the record.</em></span></span>
  </h1>

  <p class="hero-sub">The ledger is what happened. This is the practice underneath it —
    two languages, one thing to do with a seven-month-old, yesterday&rsquo;s chess, the
    reading, and six minutes of arithmetic under time pressure. Rebuilt every morning,
    same as the other page.</p>
  {% else %}
  <h1 class="hl">
    <span class="w"><span style="--d:.05s">Numbers</span></span>
    <span class="w"><span style="--d:.13s">first.</span></span><br>
    <span class="w"><span style="--d:.24s"><em>Noise</em></span></span>
    <span class="w"><span style="--d:.32s"><em>last.</em></span></span>
  </h1>

  {# The sub-head used to end "...and what to do about it." It was the best line
     on the page and it had to go. "What to do" is advice, and this is not an
     advisory service — it is a research record that publishes its own losses.
     The replacement makes the same promise without the claim: what changed,
     why it matters, and what the evidence actually supports. Everything
     downstream of this sentence is labelled FACT, MODEL, RESULT or VIEW so a
     reader can see which one they are looking at. #}
  <p class="hero-sub">Markets, research and a public signal ledger &mdash; one page, rebuilt
    every morning before the open. What changed, why it matters, and what the evidence
    says. Wins and losses both, because a record you can only see the good half of
    is not a record.</p>

  {# The legend, once, at the top. Every number below carries one of these four
     labels. It is the one thing this page does that a screener cannot. #}
  <div class="prov-legend rv" role="note" aria-label="How to read the labels on this page">
    <span class="pl-lead">How to read this</span>
    <span class="pl-item"><span class="pill pill-fact">Fact</span> an observed value &mdash; a close, a flow, a filing</span>
    <span class="pl-item"><span class="pill pill-model">Model</span> computed by the engine from facts</span>
    <span class="pl-item"><span class="pill pill-result">Result</span> what happened to a published signal</span>
    <span class="pl-item"><span class="pill pill-view">View</span> a human opinion, labelled as one</span>
  </div>

  {# Who built this, and the claim the whole page rests on, in the hero.
     Both were true and both sat 17 sections down in "Who" — a reader deciding
     in the first ten seconds whether to trust a track record had no idea who
     was publishing it or that the losses were included. Credentials up front
     are not vanity here; they are the reason the ledger means anything. #}
  <p class="hero-by">
    <span>Built by <b>Akshay Kothari</b> · CA · FP&amp;A · AI builder</span>
    <span class="hero-by-sep" aria-hidden="true">·</span>
    <span>Public ledger — wins <i>and</i> losses</span>
    <span class="hero-by-sep" aria-hidden="true">·</span>
    <span>Not investment advice</span>
  </p>
  {% endif %}

  {% if page != 'desk' %}
  <!-- Every tile ships its REAL value as its text, not "0".
       data-count still drives the count-up for a reader with JS, and setKpi()
       overwrites from /api/stats once the live ledger answers. What changed is
       the no-JS floor: a crawler, an LLM scraper and a reader with script
       blocked all used to be served "0% Signal Win Rate · 0 Signals Logged"
       above a page full of signals. A legitimate-looking zero is worse than a
       blank — it reads as a measured result.

       The win rate is also no longer unconditionally lime. Below SAMPLE_FLOOR
       closed trades it renders muted and carries its own sample count, because
       "66.7%" set in the accent colour over three trades is the single most
       misleading thing this page could say — and it said it, in the hero,
       while the ledger underneath it read 21% over 116. -->
  <div class="statrail">
    <div class="stat">
      <div class="v" id="heroRate"
           style="color:{{ 'var(--lime)' if closed >= 30 else 'var(--muted)' }}"
           data-count="{{ winrate }}" data-suffix="%">{{ winrate }}%</div>
      <div class="k">Signal Win Rate
        <span class="pill pill-result">Result</span>
        <details class="xp"><summary aria-label="How win rate is computed">?</summary>
          <div class="xp-body">
            <b>Signal win rate</b>
            Share of CLOSED signals that reached a target before their stop.
            <dl>
              <dt>Computed from</dt>
              <dd>Every signal with a recorded exit. Open signals are excluded — a
                  trade that has not resolved has no result, and counting it as
                  neutral is how a 50% system starts looking like an 80% one.</dd>
              <dt>Wrong if</dt>
              <dd>The exit price is wrong. R is recomputed from exit_price against
                  entry and stop, never read from the ledger's r_multiple column —
                  a 2026-08-08 re-grade corrupted that column on 168 of 573 rows.</dd>
              <dt>Sample</dt>
              <dd>{{ closed }} closed, from the most recent 200 alerts across
                  every engine version. Below 30 this is a running tally, not a
                  measurement.</dd>
              <dt>Why Performance differs</dt>
              <dd>The Performance section scores the v2 engine only, so it
                  reports a smaller sample. Neither figure is the &ldquo;real&rdquo;
                  one — they answer different questions, and both name their
                  population rather than leaving you to guess.</dd>
              <dt>Expiries count</dt>
              <dd>A signal that resolved without reaching a target is in the
                  denominator. Dropping expiries raises the win rate without
                  a single trade going differently.</dd>
            </dl>
          </div>
        </details>
      </div>
      {# The window, not just the count. This rail scores the last N alerts and
         the Performance section scores a different span, so "65 closed" and
         "34 closed" read as a contradiction unless each says what it is over.
         They agree to within half a point once you can see that. #}
      <div class="kn" id="heroRateNote">{{ closed }} closed of the last {{ alerts|length }} signals{{ ' · too few to measure' if closed < 30 else '' }}</div>
    </div>
    <div class="stat">
      <div class="v" id="heroOpen" style="color:var(--blue)" data-count="{{ opens }}">{{ opens }}</div>
      <div class="k" id="heroOpenK">Open Setups
        <span class="pill pill-fact">Fact</span>
        <details class="xp"><summary aria-label="What an open setup is">?</summary>
          <div class="xp-body">
            <b>Open setups</b>
            Signals the engine has published that have not yet resolved.
            <dl>
              <dt>What it is not</dt>
              <dd>A position. Nothing here holds capital — a signal becomes a
                  position only when the order is placed by hand and confirmed.</dd>
              <dt>Computed from</dt>
              <dd>A direct count of rows with status OPEN. No model, no estimate.</dd>
            </dl>
          </div>
        </details>
      </div>
    </div>
    <div class="stat">
      <div class="v" id="heroTotal" data-count="{{ alerts|length }}">{{ alerts|length }}</div>
      <div class="k">Signals Logged
        <span class="pill pill-fact">Fact</span>
        <details class="xp"><summary aria-label="What signals logged counts">?</summary>
          <div class="xp-body">
            <b>Signals logged</b>
            Every signal this engine has ever published, winners and losers.
            <dl>
              <dt>Computed from</dt>
              <dd>A row count. Each was written when it fired, not added afterwards.</dd>
              <dt>Why it matters</dt>
              <dd>It is the denominator. A track record quoted without one is a
                  selection of trades, not a record of them.</dd>
            </dl>
          </div>
        </details>
      </div>
    </div>
    <div class="stat">
      <div class="v" style="color:{{ 'var(--up)' if advancers >= (markets|length / 2) else 'var(--down)' }}"
           data-count="{{ advancers }}" data-total="{{ markets|length }}">{{ advancers }}/{{ markets|length }}</div>
      <div class="k">Markets Advancing</div>
    </div>
  </div>

  <!-- ══════════ THE RECORD ══════════
       The single most differentiated thing on this page, and until now it was
       four numbers in the hero rail competing with the market board.
       Every screener publishes what it likes today; almost none publish what
       happened to what they liked last month. That asymmetry IS the product,
       so it gets its own band, above every screen and every idea.

       NOT a new nav section. The nav is six groups and stays six — this is a
       homepage element that ends in a link to the Signal Log, which is where
       the detail already lives. Adding an eighteenth destination to prove the
       ledger matters would have been the opposite of the point.

       Server-rendered from the same wins/losses the hero uses, so it is right
       with JavaScript off. app.js enriches expectancy, cumulative R and
       drawdown from /api/stats — the same endpoint Performance renders from,
       so the two can never quote different figures. -->
  <section class="record rv" id="record" aria-labelledby="recordTitle">
    <div class="record-in">
      <div class="record-lede">
        <span class="record-eyebrow">The record</span>
        <h2 class="record-h" id="recordTitle">Every call, scored.<br><em>Including the bad ones.</em></h2>
        <p class="record-p">
          Publishing what looks good today is free. This is the part that costs
          something: what actually happened to it. Nothing is removed after the fact,
          and a signal that failed stays on the page at the size it failed by.
        </p>
        <a class="record-cta" href="#alerts">Open the signal log &rarr;</a>
      </div>
      {# The curve. Drawn from the same /api/stats payload the six figures below
         come from, so it cannot show a different story to the numbers beside
         it. Empty until that resolves — a fabricated placeholder curve on a
         page about not fabricating things would be indefensible. #}
      <figure class="rcurve" id="recordCurve" hidden>
        <figcaption class="rcurve-cap">
          <span>Cumulative R, every closed signal in order</span>
          <span class="rcurve-now" id="recordCurveNow"></span>
        </figcaption>
        <div class="rcurve-plot" id="recordCurvePlot"></div>
      </figure>
      <div class="record-grid">
        <div class="rec"><span class="rec-v">{{ closed }}</span><span class="rec-k">Closed &amp; scored</span></div>
        <div class="rec"><span class="rec-v">{{ winrate }}%</span><span class="rec-k">Win rate</span></div>
        <div class="rec"><span class="rec-v" id="recExp">&mdash;</span><span class="rec-k">Expectancy / trade</span></div>
        <div class="rec"><span class="rec-v" id="recTotal">&mdash;</span><span class="rec-k">Cumulative R</span></div>
        <div class="rec"><span class="rec-v" id="recDD">&mdash;</span><span class="rec-k">Max drawdown</span></div>
        <div class="rec"><span class="rec-v">{{ opens }}</span><span class="rec-k">Still open</span></div>
      </div>
    </div>
    <p class="record-foot lv-sys">
      <b>Numbers first. Evidence always. Losses stay visible.</b>
      Not investment advice, and not a recommendation to buy or sell anything.
      <a href="#method">How every figure here is computed &rarr;</a>
    </p>
  </section>


  <!-- ══════════ TODAY IN 60 SECONDS ══════════
       The page had eleven equally-loud sections and no answer to "why open
       this every morning". This is the answer, and it is the only thing above
       the fold that is allowed to be: four lines that each hand off to the
       section that proves them. Everything in it is already on the page — this
       block adds no new data source, only an order of reading. -->
  <div class="brief rv" id="brief">
    <div class="brief-h">
      <span class="brief-t">Today in 60 seconds</span>
      <span class="brief-d">{{ date_str }}</span>
    </div>

    {% if regime %}
    <div class="regime">
      <div class="rg-l">
        <span class="rg-k">Market regime</span>
        <span class="rg-v {{ regime.tone }}">{{ regime.label }} · {{ regime.score }}<i>/100</i></span>
      </div>
      <div class="rg-bar" style="--rg:{{ regime.score }}%"><i></i><b></b></div>
      <div class="rg-why">
        Risk appetite across {{ regime.n }} instrument{{ '' if regime.n == 1 else 's' }},
        weighted by move size.{% if regime.drivers %} Moved most by
        {% for d in regime.drivers %}<b class="{{ 'up' if d.pct >= 0 else 'dn' }}">{{ d.name }}
          {{ '+' if d.pct > 0 else '' }}{{ d.pct }}%</b>{{ ' and ' if not loop.last }}{% endfor %}.
        {% else %} Nothing moved enough to push it either way.{% endif %}
        50 is neutral; risk assets push up, havens push down.
        {% if regime.thin %}<b class="warn">Under half the board priced this morning —
        read it as a sketch, not a measurement.</b>{% endif %}
      </div>
    </div>
    {% endif %}

    {# ══════════ WHAT MATTERS NOW ══════════
       The interpretation layer. Built by what_matters() in Python — every card
       is a rule over data already on this page, so the reading is reproducible
       and traceable, and nothing here is model-generated. A card appears only
       when its trigger fires: four on a day that earned four, one on a day
       that earned one. Filling a fixed grid would mean inventing a reading.

       The numbered 60-second list below is kept as the {% raw %}{% else %}{% endraw %} branch, not
       deleted: the two legacy Flask routes render this same template without
       `matters`, and that path must still get its summary. #}
    {# ══════════ THE FIRST 60 SECONDS ══════════
       An index with substance, and the answer to a real gap: the ordered
       summary this page used to open with now only renders on the two legacy
       Flask routes, because it sits in the {% raw %}{% else %}{% endraw %} branch below and the static
       build always passes `matters`. Every reader of news.askakshay.com has
       been arriving to eighteen destinations and no reading order.

       NOT a third interpretation layer. A previous pass rejected a "So what?"
       decision strip for duplicating What Matters Now, and that reasoning
       stands — so this carries no content of its own. It is the SAME cards,
       headline only, numbered, one line each. Front page above the fold, the
       reasoning below it. Anything that needs a second line belongs in the
       card, not here.

       Which is also why there is no read-time estimate: the constraint is the
       length of this list, and the list is bounded at five by what_matters()
       itself. A minute is a promise the markup can keep rather than a number
       printed next to it. #}
    {% if matters %}
    <nav class="sixty" aria-label="The first 60 seconds">
      <div class="sixty-h">
        <span class="sixty-t">The first 60 seconds</span>
        <span class="sixty-n">{{ matters|length }} reading{{ '' if matters|length == 1 else 's' }}, one line each &middot; the rest of the page is the evidence</span>
      </div>
      <ol class="sixty-l">
        {% for c in matters %}
        <li class="sixty-r">
          <a href="{{ c.href }}">
            <span class="sixty-i">{{ '%02d'|format(loop.index) }}</span>
            <span class="sixty-x">{{ c.head }}</span>
            {% if c.basis %}<span class="mc-basis mb-{{ c.basis|lower }}">{{ c.basis }}</span>{% endif %}
          </a>
        </li>
        {% endfor %}
      </ol>
    </nav>
    {% endif %}

    {# ══════════ DECISION BOARD ══════════
       Four states, one line each, above everything else on the page.

       The homepage had eighteen destinations and no answer to "what is the
       state of things" — a reader had to assemble it from a regime score in one
       place, an IPO section eleven screens down and an engine verdict below
       that. Each of these four is already computed elsewhere on this page; the
       board is the synthesis, not new data, and every tile links to the section
       that produced it.

       Nothing is invented to fill a tile. A tile with no data says so. #}
    <div class="dboard">
      <a class="db" href="#marketintel">
        <span class="db-k">Market</span>
        <span class="db-v">{{ regime.label if regime else '—' }}</span>
        <span class="db-s">{% if regime %}{{ regime.score }}/100 regime{% else %}no reading{% endif %}</span>
      </a>
      <a class="db" href="#world">
        <span class="db-k">World</span>
        <span class="db-v">{{ (news|length) if news else 0 }} events</span>
        <span class="db-s">last 24h, clustered</span>
      </a>
      <a class="db {{ 'db-hot' if iporadar and iporadar.counts.open else '' }}" href="#iporadar">
        <span class="db-k">IPO</span>
        <span class="db-v">{% if iporadar %}{{ iporadar.counts.open }} open{% else %}—{% endif %}</span>
        <span class="db-s">{% if iporadar %}{{ iporadar.counts.apply }} apply · {{ iporadar.counts.upcoming }} coming{% else %}no radar{% endif %}</span>
      </a>
      <a class="db {{ 'db-warn' if evidence and evidence.bleeding else '' }}" href="#perf">
        <span class="db-k">Engine</span>
        <span class="db-v">{% if evidence %}{{ evidence.engines|length }} scored{% else %}—{% endif %}</span>
        <span class="db-s">{% if evidence and evidence.bleeding %}{{ evidence.bleeding|length }} not funded{% elif evidence %}none bleeding{% else %}no evidence{% endif %}</span>
      </a>
    </div>

    {% if matters %}
    <div class="matters">
      <div class="matters-h">
        <span class="matters-t">What matters now</span>
        <span class="matters-n">{{ matters|length }} reading{{ '' if matters|length == 1 else 's' }} &middot; every one links to its evidence</span>
      </div>
      <div class="matters-g">
        {% for c in matters %}
        <article class="mcard mc-{{ c.kind }}">
          <div class="mc-tag"><i></i>{{ c.tag }}
            {# The provenance chip. A price that moved and a model's score are
               not the same kind of claim, and until this chip existed the card
               presented them in identical type. #}
            {% if c.basis %}<span class="mc-basis mb-{{ c.basis|lower }}">{{ c.basis }}</span>{% endif %}
          </div>
          <h3 class="mc-head">{{ c.head }}</h3>
          <p class="mc-why">{{ c.why }}</p>
          {# The falsifier. Deliberately rendered as part of the card rather
             than hidden behind a toggle: a reading whose failure condition is
             one click away is a reading presented as more certain than it is. #}
          {% if c.unless %}<p class="mc-unless"><span>Unless</span>{{ c.unless }}</p>{% endif %}
          <a class="mc-cta" href="{{ c.href }}">{{ c.cta }} &rarr;</a>
        </article>
        {% endfor %}
      </div>
    </div>
    {% else %}
    <ol class="brief-l">
      {% set movers = markets | rejectattr('price', 'in', ['—', '', None]) | sort(attribute='change_pct', reverse=true) | list %}
      {% if movers %}
      <li>
        <span class="bn">01</span>
        <div>
          <b>What moved.</b>
          {% for m in movers[:2] %}<span class="up">{{ m.name }} +{{ m.change_pct }}%</span>{{ ', ' if not loop.last }}{% endfor %}{% if movers|length > 2 %};
          <span class="dn">{{ movers[-1].name }} {{ movers[-1].change_pct }}%</span>{% endif %}.
          <a href="#world">Full board &rarr;</a>
        </div>
      </li>
      {% endif %}

      <li>
        <span class="bn">02</span>
        <div>
          <b>The record.</b>
          {% if closed >= 30 %}{{ closed }} closed signals, {{ winrate }}% of them winners.
          {% elif closed %}Only {{ closed }} closed signal{{ '' if closed == 1 else 's' }} — too few to
            call an edge either way. Read it as a running tally.
          {% else %}Nothing has closed yet. There is no rate to report.{% endif %}
          <a href="#perf">Full record &rarr;</a>
        </div>
      </li>

      {% if engine_changes %}
      {% set ec = engine_changes[0] %}
      <li>
        <span class="bn">03</span>
        <div>
          <b>What changed in the engine.</b>
          <span class="mono-dim">{{ ec.date }} · {{ ec.tag }}</span> — {{ ec.title }}.
          <a href="#rules">Why &rarr;</a>
        </div>
      </li>
      {% endif %}

      {% if top5 %}
      <li>
        <span class="bn">04</span>
        <div>
          <b>Top idea.</b>
          {{ top5[0].name }} at {{ top5[0].score }}/100{% if top5|length > 1 %}, then
          {{ top5[1].name }} at {{ top5[1].score }}{% endif %}.
          <a href="#picks">All five &rarr;</a>
        </div>
      </li>
      {% endif %}
    </ol>
    {% endif %}
  </div>


  {% endif %}

  {# The hero's own equity curve was here and is removed.

     A second curve was added to The Record band below without noticing this
     one existed, so the page drew the same series twice, a few hundred pixels
     apart. The Record's version is the one kept: it carries the zero line, the
     shaded area against flat, the endpoint and the caption, and it sits in the
     band whose entire purpose is the record. This one was the "one glance"
     version of a thing that is now directly underneath it. #}

</section>

<main>

{# ── The one freshness badge ────────────────────────────────────────────────
   Every section that renders data calls this and nothing else. A section that
   phrases its own freshness is a section that can disagree with the health
   page about the same build, which is the state the audit found the site in.

   Guarded on `health` because the Flask routes render this same template
   without it — an undefined lookup there renders nothing rather than raising,
   so the badge is additive and can never take a page down. #}
{% macro dh(name) %}{% if health and health.by_name.get(name) %}{% set d = health.by_name[name] %}
<span class="dh dh-{{ d.status }}" title="{{ d.headline }}">{{ d.status }}<span class="dh-age">{{ d.freshness_age }}</span></span>{% endif %}{% endmacro %}

<!-- ══════════ MARKET INTEL ══════════
     Corporate actions, FII/DII net flow, sector heat — three independent
     NSE/Yahoo fetches (market_intel.py), cached daily by its own workflow
     (market_intel.yml) for the same reason the fund screen is separate
     from the daily build: a hung third-party call must never take the
     paper down with it. Every field goes through .get() from the start —
     the Fund Screen crash this session (StrictUndefined + a schema an
     older cached payload didn't have) is the lesson applied here up
     front rather than retrofitted after the same crash recurs. -->
{% if 'marketintel' in secs %}<section class="sec" id="marketintel">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['marketintel'] }} / {{ seclabel['marketintel'] }}</span> {{ dh('Market Intelligence') }}
      <h2 class="stitle">What moved the tape today</h2>
    </div>
    <p class="sdesc">Sector heat, FII/DII net flow, and every corporate action NSE published —
      straight from NSE's and Yahoo's own feeds, no ranking or "top N by importance" applied.</p>
  </div>

  <div class="prov{{ ' stale' if market_intel.get('is_fallback') else '' }} rv">
    <span class="pv-tag">DAILY</span>
    <span>Rebuilt <b>~04:30 IST</b>, before the 6 AM edition</span>
    {% if market_intel.get('built_on') %}<span>Built <b>{{ market_intel.built_on }}</b>
      {%- if market_intel.get('age_days') is not none %} · {{ market_intel.age_days }}d old{% endif %}</span>{% endif %}
    {% if market_intel.get('is_fallback') %}
    <span>&#9888; Today's build has not run &mdash; showing the previous day's.</span>
    {% endif %}
    {% if market_intel.get('job_status', {}).get('attempted_after_serve') %}
    <span style="color:var(--gold)">&#9888; The most recent attempt
      ({{ market_intel.job_status.run_at[:16] }} UTC) failed &mdash;
      {{ market_intel.job_status.detail[:120] }}</span>
    {% endif %}
  </div>

  {% set heat = market_intel.get('market_heat') or [] %}
  {% if heat %}
  {# The 6 AM snapshot. app.js replaces the grid below with a live one from
     /api/markets?heat=1 (15-minute edge cache) and flips the label — but the
     server-rendered version stays because it must read correctly with JS off
     and for a crawler. The id is what the live path targets; the label is
     what stops a stale map claiming to be current. #}
  <div class="subhead">
    <span class="subeyebrow">Market state</span>
    <h3>Sector heat
      <span id="heatAsOf" class="dh dh-STALE">6 AM SNAPSHOT</span></h3>
    <p class="subdesc">Which parts of the market moved today, by NSE sector index. Read it for rotation — money leaving one sector usually shows up in another before it shows up in the headlines.</p>
  </div>
  <div class="fund-grid rv" id="heatGrid">
    {% for s in heat|selectattr('chg_pct', 'defined')|sort(attribute='chg_pct', reverse=True) %}
    {% set chg = s.get('chg_pct', 0) %}
    <div class="card fund-card" style="padding:14px 16px">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <strong>{{ s.get('label', '—') }}</strong>
        <span class="rk {{ 'rk-low' if chg >= 0.3 else 'rk-high' if chg <= -0.3 else 'rk-medium' }}">
          {{ '+' if chg > 0 else '' }}{{ chg }}%
        </span>
      </div>
    </div>
    {% endfor %}
  </div>
  {% endif %}

  {# Movers INSIDE each sector. Built from the stock screen's own rows, so it
     costs no extra fetch and inherits the screen's build date — which is why
     it is stamped with that date and not with the 6 AM heat snapshot's.

     These are the SCREEN's sector labels, not the NSE index tiles above, and
     the two are deliberately not wired together: Banking and PSU Bank are
     separate indices that would both collapse into one "Financial Services"
     bucket, so drilling into either tile would show the same names. A
     drill-down that lies about which sector you asked for is worse than a
     separate, honest control. <details> so it needs no JavaScript. #}
  {# ══════════ INDIA AT A GLANCE ══════════
     Every instrument here already scrolls past in the rail. A rail is the wrong
     shape for "what is the Nifty at": it moves, it wraps, and finding one number
     means waiting for it to come round. This is the same data standing still.

     Filled by paintIndiaBoard() from /api/ticker — the response the rail is
     already fetching, so no extra request. The server-rendered numbers below are
     the 6 AM build and are replaced the moment the live path answers. #}
  {# ── THE DAY ────────────────────────────────────────────────────────────
     Semafor's one genuinely portable idea is the Semaform: an article broken
     into labelled blocks by KIND of claim — the news, the view, room for
     disagreement — so a reader knows what they are reading before they read
     it. This page already sorts every figure into fact / model / result /
     view; it just used those as small chips rather than as structure.

     So the day summary is built that way. What the tape did (fact), how broad
     it was (fact), where the money went (fact), what that reads as (model),
     and — the block most financial pages will not print — the case against
     that reading. #}
  {% set _b = stock_screen.breadth or {} %}
  {% if _b.get('counted') %}
  <div class="dayblocks rv">
    <div class="dayblock">
      <span class="db-lab db-fact">The tape</span>
      <div class="db-body">
        <div class="db-big" id="dayNifty">&mdash;</div>
        <p class="db-note">Nifty 50, live. The index is one number; the two blocks
          beside it are what is underneath it.</p>
      </div>
    </div>

    <div class="dayblock">
      <span class="db-lab db-fact">The breadth</span>
      <div class="db-body">
        <div class="db-big">
          <b class="up">{{ _b.advancing }}</b> up
          <span class="db-vs">/</span>
          <b class="dn">{{ _b.declining }}</b> down
        </div>
        {# The bar is the point: 321 and 412 are two numbers, the split is one
           picture. Widths are the real proportions, not a fixed ratio. #}
        <div class="db-bar" role="img"
             aria-label="{{ _b.advancing }} advancing against {{ _b.declining }} declining of {{ _b.counted }}">
          <span class="db-up" style="width:{{ (_b.advancing / _b.counted * 100)|round(1) }}%"></span>
          <span class="db-dn" style="width:{{ (_b.declining / _b.counted * 100)|round(1) }}%"></span>
        </div>
        <p class="db-note">Of {{ _b.counted }} screened.
          <b>{{ _b.above200 }}%</b> hold their 200-day average,
          <b>{{ _b.above50 }}%</b> their 50-day.
          <b>{{ _b.at_52w_high }}</b> sit at a 52-week high.</p>
      </div>
    </div>

    <div class="dayblock">
      <span class="db-lab db-model">What it reads as</span>
      <div class="db-body">
        <div class="db-big" id="dayRegime">&mdash;</div>
        <p class="db-note">
          {% if _b.advancing and _b.declining and _b.declining > _b.advancing %}
          More names fell than rose, so an index that held up was carried by a few
          of them rather than by the market.
          {% elif _b.advancing and _b.declining %}
          More names rose than fell, which is the version of a green index worth
          having &mdash; the move is in the market, not in five stocks.
          {% endif %}
          The median name is {{ _b.median_1m }}% over a month against the index&rsquo;s
          {{ _b.nifty_1m }}%.
        </p>
      </div>
    </div>

    {# WHERE THE MONEY WENT — the fourth fact the comment at the top of this
       block names ("what the tape did, how broad it was, where the money went")
       and the one that was never built. The grid is auto-fit across four
       columns, so with three blocks the fourth cell rendered as a flat grey
       rectangle roughly a third of the width of the section: the most
       prominent empty space on the page, directly under the headline numbers.

       Not new data. This is the FII/DII block that used to sit ~170 lines
       below under its own heading, moved here. It belongs beside breadth —
       "more names fell than rose" and "institutions were net sellers" are the
       same question asked of prices and of flows, and they were four screens
       apart. Moving it fills the hole and removes a whole subhead, a
       description and a KPI row from further down.

       #fiiGrid and #fiiAsOf keep their ids: app.js overwrites the innerHTML of
       the first from /api/markets?heat=1 and restamps the second with the
       flow's own trade date. Renaming either here would have left the block
       silently frozen at the 6 AM snapshot. #}
    {% set _fd = market_intel.get('fii_dii') if market_intel else None %}
    {% if _fd and _fd.get('fii_cr') is not none and _fd.get('dii_cr') is not none %}
    <div class="dayblock">
      <span class="db-lab db-fact">Where the money went</span>
      <div class="db-body">
        <div class="dbflow" id="fiiGrid">
          <div class="kpi"><div class="v {{ 'up' if _fd.get('fii_cr', 0) >= 0 else 'dn' }}">&#8377;{{ '{:,.0f}'.format(_fd.get('fii_cr', 0)) }} Cr</div><div class="k">FII net</div></div>
          <div class="kpi"><div class="v {{ 'up' if _fd.get('dii_cr', 0) >= 0 else 'dn' }}">&#8377;{{ '{:,.0f}'.format(_fd.get('dii_cr', 0)) }} Cr</div><div class="k">DII net</div></div>
          <div class="kpi"><div class="v {{ 'up' if _fd.get('net_cr', 0) >= 0 else 'dn' }}">&#8377;{{ '{:,.0f}'.format(_fd.get('net_cr', 0)) }} Cr</div><div class="k">Combined</div></div>
        </div>
        <p class="db-note">Net rupees bought and sold by foreign and domestic
          institutions. They are usually on opposite sides &mdash; the size of the
          gap says more than the direction of either one.
          <span id="fiiAsOf" class="dh dh-STALE">6 AM SNAPSHOT</span></p>
      </div>
    </div>
    {% endif %}

    <div class="dayblock db-wide">
      <span class="db-lab db-view">Room for disagreement</span>
      <div class="db-body">
        <p class="db-note db-arg">
          Breadth is a count, not a weight: {{ _b.counted }} names each count once, so a
          day where the largest twenty carry the index and four hundred small names drift
          reads here as weak and in your portfolio as fine. The 200-day figure is
          slow by construction and will still look healthy some way into a real
          decline. And none of this says anything about tomorrow &mdash; it is a
          description of a day that has already happened.
        </p>
      </div>
    </div>
  </div>
  {% endif %}

  {# The index tiles live INSIDE the dashboard now. They were a second box
     directly under the first, repeating the same question — what did the
     market do today — with a second heading, a second freshness badge and a
     second explanation. One box, one heading, one badge. #}
  <div class="dash-tiles">
    <div class="dash-tiles-h">
      <span class="dash-tiles-t">The board</span>
      <span id="indiaAsOf" class="dh dh-STALE">6 AM SNAPSHOT</span>
    </div>
  <div class="ib-grid rv" id="indiaBoard">
    <div class="empty">Loading the board&hellip;</div>
  </div>

  {# KEY FINDINGS FOR THE DAY — the deterministic scans' own headline, inside
     the dashboard rather than nine sections down. Three at most: this is the
     summary, and a summary that lists everything is the thing it was meant to
     replace. The full set, with every name and the rule behind it, is in
     Findings. #}
  {# `hidden`, not `findings.findings` — the context key is findings.hidden and
     the first version invented a key that does not exist. Verified against
     insights.hidden_findings(): six entries, each with count / title / rule. #}
  {% set _fs = (findings.get('hidden') or []) | selectattr('count') | list %}
  {% if _fs %}
  <div class="dash-find">
    <div class="dash-tiles-h">
      <span class="dash-tiles-t">What the scans found today</span>
      <a class="dash-more" href="#findings">All {{ _fs|length }} &rarr;</a>
    </div>
    <div class="dfind-row">
      {% for f in (_fs | sort(attribute='count', reverse=true))[:3] %}
      <div class="dfind">
        <span class="dfind-n num">{{ f.count }}</span>
        <span class="dfind-t">{{ f.title }}</span>
        <span class="dfind-r">{{ f.rule }}</span>
      </div>
      {% endfor %}
    </div>
  </div>
  {% endif %}
  </div>

  {% set movers = market_intel.get('sector_movers') or [] %}
  {% if movers %}
  {# TWO BOARDS, NOT ONE.
     The live board used to REPLACE this grid — same element id, painted over
     as soon as /api/ticker answered. That traded a weekly read across all 750
     screened names for a same-day read across whatever the ticker rail happens
     to carry, which is ten sectors and a handful of names each. Two different
     questions were sharing one box and the more thorough one always lost.
     They are separate elements now. Today first, because today is the one you
     can act on; the week underneath, because it is the one that tells you
     whether today means anything. #}
  <div class="subhead">
    <span class="subeyebrow">Market state</span>
    <h3>Moving today, by sector<span id="moversAsOf" class="dh dh-STALE">WAITING ON LIVE</span></h3>
    <p class="subdesc">Today&rsquo;s move, grouped by sector. Drawn from the live ticker, so it
      covers the names on the rail rather than the full screen &mdash; narrow but current.
      The week&rsquo;s version, across all {{ stock_screen.count or '750' }} screened names,
      sits below it.</p>
  </div>
  <div class="fnd-grid rv" id="sectorMoversLive">
    <div class="empty">Waiting on the live ticker&hellip;</div>
  </div>

  <div class="subhead" style="margin-top:clamp(26px,3vw,40px)">
    <span class="subeyebrow">Market state</span>
    <h3>Moving this week, by sector<span class="dh dh-STALE">1-WEEK SCREEN</span></h3>
    <p class="subdesc">Best and worst five in each of {{ movers|length }} sectors over
      <strong>one week</strong>, from the screen's own rows{% if stock_screen.built_on %}
      (built {{ stock_screen.built_on }}){% endif %}. The median says whether the sector
      moved or two names carried it.</p>
  </div>
  {# The paragraph that used to sit here repeated the subhead above it almost
     word for word — same source, same date caveat, same median explanation, in
     two consecutive paragraphs. It was the only genuine adjacent duplication on
     the page and it is gone; the count and the build date live in the subhead. #}
  <div class="fnd-grid rv" id="sectorMoversWeek">
    {% for m in movers %}
    <details class="card fnd sec-movers">
      <summary>
        <strong>{{ m.sector }}</strong>
        <span class="fnd-n">{{ '+' if m.median > 0 else '' }}{{ m.median }}% median &middot; {{ m.count }} names</span>
      </summary>
      <p class="fnd-r" style="margin-top:10px">Best five</p>
      <ul class="mv-list">
        {# Opens THAT company's detail sheet — scores, fundamentals, technicals,
           the year tables — rather than scrolling to the top of a 750-row
           screen and leaving the reader to find the name themselves. href
           survives as the no-JS fallback. #}
        {% for g in m.gainers %}
        <li><a href="#stocks" class="sym" data-stock="{{ g.sym }}">{{ g.sym }}</a>
            <span class="up">{{ '+' if g.move > 0 else '' }}{{ g.move }}%</span></li>
        {% endfor %}
      </ul>
      <p class="fnd-r" style="margin-top:10px">Worst five</p>
      <ul class="mv-list">
        {% for l in m.losers %}
        <li><a href="#stocks" class="sym" data-stock="{{ l.sym }}">{{ l.sym }}</a>
            <span class="dn">{{ '+' if l.move > 0 else '' }}{{ l.move }}%</span></li>
        {% endfor %}
      </ul>
    </details>
    {% endfor %}
  </div>
  {% endif %}

  {# The FII / DII subhead and KPI row that stood here are GONE, not disabled.
     They now render as the "Where the money went" block inside .dayblocks at
     the top of this section, which is where the flow belongs: beside breadth,
     answering the same question of money that breadth answers of prices.

     Removing it rather than hiding it is the point of the change — a heading,
     a description and a three-up KPI row is roughly 180px of vertical space,
     and it was duplicating a block the reader had already passed. #}
  {# Smart money flow was here and is removed.

     It plotted the last few sessions of FII vs DII as paired bars. Two reasons
     it went: with six sessions it was a chart of almost nothing, and it was
     wrong — _fii_dii_trend could return the SAME trade date twice, because NSE
     publishes after the close and a build that runs before the next figures
     land re-reads yesterday's. The chart drew one session as two bars side by
     side. That bug is fixed at the source regardless, since the figures above
     read the same series.

     The two numbers that matter — today's FII and DII net — are in the tiles
     above, dated, and they were never the thing that was broken. #}
  {% endif %}

  {% set ca = market_intel.get('corporate_actions') or [] %}
  {% if ca %}
  <div class="subhead">
    <span class="subeyebrow">Calendar</span>
    <h3>Corporate actions</h3>
    <p class="subdesc">Dividends, splits, bonuses and buybacks with their ex-dates. Buy on or after the ex-date and you do not receive the action — this is the date that decides it.</p>
  </div>
  {# Ex-date and record date are the SAME DAY for almost every row, and the two
     columns printed identical values down the page — which reads as a copied
     field rather than as what it is. India settles T+1, so the two coincide by
     design; NSE publishes them as separate fields and this reads both, so the
     duplication is in the market's calendar, not in the pipeline.

     One column when they agree, both when they do not, and the difference is
     stated rather than left for the reader to infer from two columns that
     almost never disagree. Nothing is dropped: a row where they genuinely
     differ still shows both dates. #}
  <p class="sdesc" style="margin-bottom:10px;max-width:70ch">
    Ex-date and record date fall on the same day under T+1 settlement, so one date is
    shown where they agree. Any row where they genuinely differ shows both.
  </p>
  <div class="tw rv">
    <table class="t">
      <thead><tr>
        <th scope="col">Symbol</th><th scope="col">Action</th>
        <th scope="col">Ex / record date</th>
      </tr></thead>
      <tbody>
        {% for r in ca %}
        {% set ex = r.get('ex_date') or '' %}
        {% set rd = r.get('record_date') or '' %}
        <tr>
          <td><strong>{{ r.get('symbol', '—') }}</strong></td>
          <td style="font-size:12px;color:var(--muted)">{{ r.get('subject', '—') }}</td>
          <td class="mono-dim">
            {%- if ex and rd and ex != rd -%}
              ex {{ ex }} &middot; record {{ rd }}
            {%- elif ex -%}
              {{ ex }}
            {%- elif rd -%}
              {{ rd }}
            {%- else -%}
              &mdash;
            {%- endif -%}
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% endif %}
</section>
{% endif %}

<!-- Capture. There is exactly ONE subscribe box on this page, and it sits at
     the bottom (id="subEnd") after the ledger. A second copy used to sit here,
     above the fold, asking for an email before the reader had seen a single
     scored trade — two asks on one page, and the top one had nothing to point
     back to. The claim that earns the address is the losing month printed in
     public further down, so the ask belongs after it, not before.
     The submit handler binds to every .sub-form, so it needs no change. -->

<!-- ══════════ 01 TRADE IDEAS ══════════ -->
{% if 'picks' in secs %}<section class="sec" id="picks">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['picks'] }} / {{ seclabel['picks'] }}</span> {{ dh('Trade ideas') }}
      <h2 class="stitle">{% if top5|length >= 5 %}Top 5 trade ideas{% elif top5 %}Top {{ top5|length }} trade ideas{% else %}No trade ideas clear the bar this week{% endif %}</h2>
    </div>
    <p class="sdesc">Global 200 universe — India, US, global. Scored, ranked, refreshed weekly.
      Every level is taken from structure &mdash; the target from the 52-week high or a measured
      move past it, the stop from the 20-day average &mdash; so the reward/risk differs per idea
      instead of being the same number on every card. Hover any level for the rule behind it.
      Ranked only among names clearing a minimum 2:1 reward/risk — the same floor every
      scanner engine enforces; this page used to rank on momentum score alone and could
      surface an idea with a great score and a sub-1.0 R:R.
      {% if top5|length < 5 %}<br><span style="color:var(--gold)">Only {{ top5|length }}
      of the watchlist cleared that floor this week{% if not top5 %} — none did{% endif %}.
      Fewer honest ideas beats five, one of which is not a trade.</span>{% endif %}
      {% if top5_week %}<br><span style="color:var(--gold)">This week's scan did not complete —
      showing {{ top5_week }}'s ranking. Prices have moved since.</span>{% endif %}</p>
  </div>

  <div class="prov{{ ' stale' if top5_week else '' }} rv">
    <span class="pv-tag">WEEKLY</span>
    <span>Ranked once a week, <b>Sunday morning IST</b> &mdash; the same five all week is the design, not a stalled scan</span>
    <span>Engine <b>{{ picks_engine }}</b></span>
    <span>These are ideas, not ledger signals &mdash; they carry no entry fill and
      never touch win rate or expectancy</span>
  </div>
  {# ── THE MANDATE'S ORDER BOOK ────────────────────────────────────────────
     The five above are a RANKING. This is a BOOK: what the Rs 1 crore rulebook
     would place today, at what size, with the exits already decided. Same
     section because a reader who scrolls to "trade ideas" is asking this
     question, and the page only ever answered the ranking one.

     Rendered only when sizing succeeded. A failed run drops the block; it must
     never print an empty book, because "0 to place" is a claim about the
     market and "sizing did not run" is a claim about this page. #}
  {% if mandate %}
  {# The heading above this block reads "Top 5 trade ideas" and names the
     RANKING. This is a different artefact with a different count — eight
     orders under a heading promising five — and with nothing separating them
     the section looked like it contradicted itself. Its own heading now. #}
  <div class="subhead rv" style="margin-top:clamp(26px,3vw,40px)">
    <h3>Orders to place &mdash; the ₹{{ inr_short(mandate.capital) }} book
      <span class="dh dh-LIVE">{{ mandate.admitted|length }} ORDERS</span></h3>
    <p class="subdesc">Not the five above &mdash; that is a weekly ranking. This is what
      the rulebook would BUY right now, at what size, with the exits already decided.
      Every row shows what it costs, and they add up to the deployed figure.</p>
    {# ₹1,00,00,000 appears in exactly two places on this page and they answer
       different questions — which is why a reader asks "if these are the top 5
       trades, why is there a separate wallet?". Neither is redundant, but
       nothing said so, and two identical headline numbers under two different
       headings read as the same thing printed twice.
       ORDERS TO PLACE (here)  — nothing bought. Entry, size, exits.
       POSITIONS HELD (wallet) — what the same capital already owns, marked to
                                 live prices, with realised and unrealised P&L.
       Same crore, two moments in its life. Stated, and linked. #}
    <p class="subdesc" style="margin-top:6px">
      <b>Nothing here is bought yet.</b> What this same ₹{{ inr_short(mandate.capital) }}
      already <i>holds</i>, marked to live prices, is
      <a href="#paperwallet">the paper wallet &rarr;</a></p>
  </div>
  <div class="mandate rv">
    <div class="mandate-head">
      <div>
        <span class="pv-tag">MANDATE</span>
        <b>&#8377;{{ inr_short(mandate.capital) }}</b> &middot; Indian listed equity, no intraday
      </div>
      <div class="mandate-state">
        <span><i>{{ mandate.admitted|length }}</i> to place</span>
        <span>heat <i>{{ mandate.state.heat_pct }}%</i></span>
        <span>deployed <i>{{ mandate.state.deployed_pct }}%</i></span>
        <span>cash <i>&#8377;{{ inr_short(mandate.state.cash) }}</i></span>
        {# WHERE THE P&L IS. This row lists what the book is doing — orders,
           heat, deployment, cash — and a reader scanning it looks for a P&L
           beside them and finds none. The paragraph above does say "nothing
           here is bought yet", but it is above the fold of this block and the
           question gets asked at THIS row.

           The answer belongs where the question is asked, so it is a fifth
           item in the same row rather than a better sentence higher up. #}
        <span class="mandate-pnl">no P&amp;L here &mdash; nothing is bought
          <a href="#paperwallet">see the wallet &rarr;</a></span>
      </div>
    </div>

    {% if mandate.admitted %}
    <div class="mandate-rows">
      {% for t in mandate.admitted %}
      {# data-sym / data-entry are what the live mark hangs off. The book is
         rendered at BUILD time, so without them every price on it is the
         price at 04:00 UTC and the reader has no way to tell whether the
         market has already run past the entry. See markMandate() in app.js. #}
      <div class="mrow" data-sym="{{ t.symbol }}" data-entry="{{ t.entry }}"
           data-stop="{{ t.stop }}" data-qty="{{ t.qty }}">
        <div class="mrow-top">
          <span class="msym">{{ t.symbol }}</span>
          <span class="mlive" hidden></span>
          <span class="mhz">{{ t.horizon_label }}</span>
          <span class="meng">{{ t.engine }}</span>
          <span class="mrr">{{ t.reward_risk }}:1</span>
          <span class="mgain">+{{ t.final_gain_pct }}%</span>
        </div>
        <div class="mrow-nums">
          <span>buy <b>{{ t.qty }}</b> @ <b>{{ '{:,.2f}'.format(t.entry) }}</b></span>
          <span class="mstop">stop <b>{{ '{:,.2f}'.format(t.stop) }}</b> ({{ t.stop_pct }}%)</span>
          <span>&#8377;{{ inr_short(t.notional) }} &middot; {{ t.notional_pct }}%</span>
          <span>risk &#8377;{{ inr_short(t.risk_amount) }}</span>
          <span>hold {{ t.hold_days }}</span>
        </div>
        {# The ladder is the point. 20% at T1, half the remainder at T2, the
           rest at T3 — printed per leg with the share count already worked
           out, because "scale out" without a number is not an instruction. #}
        <div class="mladder">
          {% for leg in t.legs %}
          <span class="mleg"><i>{{ leg.label }}</i> sell {{ leg.qty }} @
            {{ '{:,.2f}'.format(leg.price) }} <em>+{{ leg.gain_pct }}%</em></span>
          {% endfor %}
        </div>
        <div class="mtrail">{{ t.trail_note }}</div>
      </div>
      {% endfor %}
    </div>
    {% else %}
    <p class="mandate-empty">Nothing clears the mandate today. Cash is the position.</p>
    {% endif %}

    {% if mandate.admitted %}
    {# The reconciliation. Eight orders and a header reading "deployed 71.7%"
       could not be tied together by eye, which is what made the book look like
       it was ignoring most of the crore. #}
    {% set _placed = mandate.admitted | sum(attribute='notional') %}
    {% set _risked = mandate.admitted | sum(attribute='risk_amount') %}
    <div class="mandate-total">
      <span>{{ mandate.admitted|length }} orders</span>
      <span>&#8377;{{ inr_short(_placed) }} of &#8377;{{ inr_short(mandate.capital) }}
        &middot; <b>{{ '%.1f'|format(_placed / mandate.capital * 100) }}%</b> deployed</span>
      <span>&#8377;{{ inr_short(mandate.capital - _placed) }} stays in cash</span>
      <span>&#8377;{{ inr_short(_risked) }} at risk if every stop hits</span>
    </div>
    {% endif %}

    <div class="mandate-foot">
      {% if mandate.deferred %}<span>{{ mandate.deferred|length }} valid, waiting on a cap</span>{% endif %}
      {% if mandate.duplicates %}<span>{{ mandate.duplicates|length }} dropped as duplicate names</span>{% endif %}
      {% if mandate.rejected %}<span>{{ mandate.rejected|length }} rejected</span>{% endif %}
      <span>Bands: swing 25&ndash;60% &middot; medium 35&ndash;75% &middot; long 40&ndash;90%</span>
      <span>No order is placed by this page &mdash; there is no broker link.</span>
    </div>
  </div>
  {% endif %}

  {% if top5 %}
  {# has-lead promotes idea 01 to a double-width, double-height card. It is
     applied only at five ideas: with fewer, a 2x2 lead in a 4-column grid
     leaves empty cells beside it, and a hole in the grid reads as a
     failed render rather than a design. #}
  <div class="pick-grid{{ ' has-lead' if top5|length >= 5 }}">
    {% for s in top5 %}
    <div class="pick rv" style="--d:{{ loop.index0 * 0.07 }}s">
      <div class="rank" aria-hidden="true">{{ "%02d"|format(loop.index) }}</div>
      {% if loop.first and top5|length >= 5 %}<div class="pick-lead">Top idea</div>{% endif %}
      {# The ledger's verdict on this idea, if it already has one. The ranking
         is a snapshot taken once a week and carries no exit state, so a pick
         that stopped out on Monday used to sit here all week still presented
         as live — SMCI did exactly that, SL_HIT at -8.01%, on the front page
         for the rest of the week. The idea is NOT removed: it was this week's
         pick and hiding it would be the dishonest fix. It is marked. #}
      {% if s.outcome %}
      <div class="pick-done {{ 'pd-loss' if s.outcome.is_loss else 'pd-win' }}">
        {{ 'Stopped out' if s.outcome.is_loss else 'Closed' }}
        {%- if s.outcome.pnl_pct is not none %} · {{ '%+.1f'|format(s.outcome.pnl_pct) }}%{% endif %}
        {%- if s.outcome.closed_at %} · {{ s.outcome.closed_at }}{% endif %}
      </div>
      {% endif %}
      <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:10px">
        <div class="sym"><a href="https://www.tradingview.com/chart/?symbol={{ s.tv or s.name }}"
             target="_blank" rel="noopener"
             title="Open {{ s.name }} on TradingView">{{ s.name }}</a></div>
        <span class="tag">{{ s.score }}/100</span>
      </div>
      <div class="px">{{ s.currency }}{{ s.price }}</div>
      <div class="mom">
        <span class="{{ 'up' if s.change_1d >= 0 else 'dn' }}">1D <b>{{ '+' if s.change_1d >= 0 else '' }}{{ s.change_1d | round(2) }}%</b></span>
        <span class="{{ 'up' if s.mom_1m >= 0 else 'dn' }}">1M <b>{{ '+' if s.mom_1m >= 0 else '' }}{{ s.mom_1m | round(2) }}%</b></span>
        <span class="{{ 'up' if s.mom_3m >= 0 else 'dn' }}">3M <b>{{ '+' if s.mom_3m >= 0 else '' }}{{ s.mom_3m | round(2) }}%</b></span>
      </div>
      <div class="scorebar" style="--w:{{ s.score }}%"><i></i></div>
      {% if s.thesis %}<div class="th">{{ s.thesis }}</div>{% endif %}

      {# Where the score came from. A composite with no breakdown is a number
         the reader has to take on trust, and the whole argument of this site
         is that nothing here should be taken on trust. Collapsed by default —
         it is the answer to a question, not the headline. #}
      {# The lead card opens its breakdown by default. Two reasons: a 2x2 card
         whose content only fills the top half is padding pretending to be
         hierarchy, and the score bars are the most persuasive thing on the
         card — collapsing the evidence under the one idea being promoted is
         the wrong default. Ideas 02-05 stay collapsed. #}
      {% if s.factors %}
      <details class="why"{{ ' open' if loop.first and top5|length >= 5 }}>
        <summary>Why {{ s.score }}<span>/100</span></summary>
        <div class="why-b">
          {% for f in s.factors %}
          <div class="why-r">
            <span class="wk">{{ f.k }}</span>
            <span class="wb" style="--w:{{ (f.e / f.w * 100) | round(0) | int }}%"><i></i></span>
            <span class="wn">{{ f.e }}<em>/{{ f.w }}</em></span>
          </div>
          {% endfor %}
        </div>
      </details>
      {% endif %}

      {# The level that ends the idea, stated before it is reached. An idea
         with a target and a stop but no invalidation only tells you where you
         are wrong on price, never where you are wrong on the reason. #}
      {% if s.ema20 %}
      <div class="inval">
        <b>Wrong if</b> {{ s.name }} closes below {{ s.currency }}{{ s.ema20 }} — its 20-day
        average, and the single biggest component of that score.
      </div>
      {% endif %}

      <div class="lvl">
        {# Every level states where it came from. A target of "+25%" cannot be
           argued with; "the 52-week high" can — the reader can go and look at
           the level and disagree with it, which is the whole point. #}
        <div><div class="k">🎯 Target</div><div class="v up" title="{{ s.target_basis or '' }}">{{ s.currency }}{{ s.target }}</div></div>
        <div><div class="k">🛡 Stop</div><div class="v dn" title="{{ s.stop_basis or '' }}">{{ s.currency }}{{ s.stop_loss }}</div></div>
        {% if s.rr %}<div><div class="k">⚖ Reward/risk</div><div class="v">{{ s.rr }}</div></div>{% endif %}
        <div><div class="k">⏱ Horizon</div><div class="v" style="font-size:11px;color:var(--muted)" title="{{ s.horizon_basis or '' }}">{{ s.timeframe }}</div></div>
      </div>
      <form action="/tracker/add" method="post" style="margin-top:14px">
        <input type="hidden" name="symbol" value="{{ s.symbol }}">
        <input type="hidden" name="name" value="{{ s.name }}">
        <input type="hidden" name="entry_price" value="{{ s.price }}">
        <input type="hidden" name="target_price" value="{{ s.target }}">
        <input type="hidden" name="stop_loss" value="{{ s.stop_loss }}">
        <input type="hidden" name="thesis" value="{{ s.thesis }}">
        <input type="hidden" name="timeframe" value="{{ s.timeframe }}">
        <button type="submit" class="btn btn-sm">+ Track</button>
      </form>
    </div>
    {% endfor %}
  </div>
  {% else %}
  <div class="empty rv">No ranking available. The weekly scan runs with the 6 AM MYT build;
    if this persists past Monday morning, the scan is failing — check the Daily Newspaper workflow.</div>
  {% endif %}
</section>{% endif %}

<!-- ══════════ 04 WORLD ══════════ -->
{% if 'world' in secs %}<section class="sec" id="world">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['world'] }} / {{ seclabel['world'] }}</span> {{ dh('World news') }}
      <h2 class="stitle">The world, last 24h</h2>
    </div>
    <p class="sdesc" id="worldDesc">Wires only. Deduplicated, ranked, and cut to what
      actually changes a decision.</p>
  </div>

  {# ══════════ 24 ROLLING HOURS ══════════
     The card grid answers "what happened". It cannot answer "when, and where is
     it concentrated" — the two questions a 24-hour window exists to answer.
     Same response, arranged by time and place instead of by rank.

     Empty until /api/world answers: there is no server-rendered fallback
     because a timeline built from a 6 AM snapshot would claim a recency it does
     not have, and a stale timeline is worse than no timeline. #}
  <div class="wt" id="worldTimeline" hidden></div>

  <!-- Live incident map.
       Land is a 156x66 dot grid rasterised once from Natural Earth (public
       domain) and run-length encoded into the string below — no external asset,
       no runtime fetch, nothing for a CSP to block. Blue is the baseline
       everywhere; a country lights red or green when the last 24 hours of wires
       say something is happening there. Filled from /api/world. -->
  {# The world map is gone.

     It cost a 624x264 canvas, an SVG overlay, two night-shade layers, a sweep
     animation and a tooltip layer to convey one thing: roughly where today's
     stories happened. The region is already the first word of every row in the
     list below, in text, grouped and sorted. A decorative globe that repeats
     the rows underneath it teaches a reader to skip the section that holds the
     actual information.

     The "last 6 hours" strip went with it, for the same reason: it was the
     same events as the detailed list below, filtered to a shorter window — the
     top of the section was a preview of its own middle. One list now. #}

  {% if news %}
    {% set lead = news[0] %}
    <div class="lead rv">
      <div class="lead-m">
        <span class="tag">{{ lead.source }} · LEAD</span>
        <h2>{% if lead.link %}<a href="{{ lead.link }}" target="_blank">{{ lead.title }}</a>{% else %}{{ lead.title }}{% endif %}</h2>
        <p>{{ lead.summary }}</p>
        {% if lead.link %}<a class="readmore" href="{{ lead.link }}" target="_blank" rel="noopener">Read the full story &rarr;</a>{% endif %}
      </div>
      <div class="lead-s">
        {# These five carried a headline and nothing else — no summary, and the
           only way to learn what a story said was to leave the page. Every
           item on this page now states what happened before it asks you to
           click. #}
        {% for item in news[1:6] %}
        <div class="mini">
          <span class="s">{{ item.source }}</span>
          {% if item.link %}<a href="{{ item.link }}" target="_blank">{{ item.title }}</a>{% else %}<a>{{ item.title }}</a>{% endif %}
          {% if item.summary %}<p class="mini-s">{{ item.summary[:130] }}{% if item.summary|length > 130 %}&hellip;{% endif %}</p>{% endif %}
        </div>
        {% endfor %}
      </div>
    </div>

    <div class="news-grid">
      {% for item in news[6:15] %}
      <div class="ncard rv" style="--d:{{ loop.index0 * 0.05 }}s">
        <span class="s">{{ item.source }}</span>
        <h3>{% if item.link %}<a href="{{ item.link }}" target="_blank">{{ item.title }}</a>{% else %}{{ item.title }}{% endif %}</h3>
        <p>{{ item.summary[:180] }}{% if item.summary|length > 180 %}&hellip;{% endif %}</p>
        <div class="ncard-f">
          <span class="ts">{{ item.published }}</span>
          {% if item.link %}<a class="readmore" href="{{ item.link }}" target="_blank" rel="noopener">Read more &rarr;</a>{% endif %}
        </div>
      </div>
      {% endfor %}
    </div>
  {% else %}
    <div class="empty rv">Loading feeds…</div>
  {% endif %}
</section>{% endif %}

<!-- ══════════ FINDINGS ══════════
     What the datasets already contain and nobody was reading.

     Every finding here is a RULE over published numbers — no model writes any
     of it, and each one states its own criteria so a reader can reproduce it
     from screen.json by hand. That is the whole design: an LLM would add
     nothing here except the possibility of being wrong.

     Findings are research leads, not recommendations. "Strong on the accounts,
     weak on the tape" is an observation about a shared property; what it means
     for any one company is exactly the work the reader still has to do. -->
{% if 'findings' in secs and findings %}
<section class="sec" id="findings">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['findings'] }} / {{ seclabel['findings'] }}</span>
      <h2 class="stitle">What the data says that nobody looked for</h2>
    </div>
    <p class="sdesc">Deterministic scans across {{ findings.universe }} companies and the
      market's own internals. Every finding states the rule that produced it, so it can be
      checked rather than believed. Research leads &mdash; not recommendations, and not
      ranked: the tables below already do the ranking, this answers a different question.</p>
  </div>

  {% if findings.contradictions %}
  <div class="subhead">
    <span class="subeyebrow">Findings</span>
    <h3>Where the market disagrees with itself</h3>
    <p class="subdesc">Names where two screens reach opposite conclusions — strong fundamentals with a broken chart, or the reverse. Disagreement is not a signal; it marks where the easy read is wrong.</p>
  </div>
  <div class="fnd-grid rv">
    {% for c in findings.contradictions %}
    <div class="card fnd fnd-warn">
      <h4 class="fnd-t">{{ c.title }}</h4>
      <p class="fnd-d">{{ c.detail }}</p>
      <p class="fnd-m"><b>What it means</b> {{ c.means }}</p>
      <p class="fnd-w"><b>What to watch</b> {{ c.watch }}</p>
    </div>
    {% endfor %}
  </div>
  {% endif %}

  {% if findings.hidden %}
  <div class="subhead">
    <span class="subeyebrow">Findings</span>
    <h3>Unusual combinations</h3>
    <p class="subdesc">Pairs of attributes that rarely occur together in the same company. Rare is not the same as good — treat each as a question to investigate, not a conclusion.</p>
  </div>
  <div class="fnd-grid rv">
    {% for f in findings.hidden %}
    <div class="card fnd">
      <h4 class="fnd-t">{{ f.title }} <span class="fnd-n">{{ f.count }}</span></h4>
      {# The rule, always. A finding that cannot state its own criteria is not
         checkable, and an unfalsifiable finding is decoration. #}
      <p class="fnd-r">{{ f.rule }}</p>
      <p class="fnd-s">
        {# data-stock opens THAT company's sheet. Without it these were bare
           #stocks links that dumped the reader at the top of a 750-row screen
           to find the name themselves — a link answering a different question
           from the one that was clicked. The href stays as the no-JS path. #}
        {% for n in f.names %}<a href="#stocks" class="sym" data-stock="{{ n.sym }}">{{ n.sym }}</a>{% if not loop.last %} · {% endif %}{% endfor %}
      </p>
      {# "+N more" used to be a bare <span>. The other N names were never
         serialised, so it was a dead label offering an expansion that did not
         exist. insights.py now emits all_syms (the complete list, symbols
         only), and this is a real <details> — no JS, works with scripting off,
         keyboard-operable, and announces its own state to a screen reader. #}
      {% if f.count > f.names|length and f.all_syms %}
      <details class="fnd-all">
        <summary>+{{ f.count - f.names|length }} more &mdash; show all {{ f.count }}</summary>
        <p class="fnd-s">
          {% for sym in f.all_syms %}<a href="#stocks" class="sym" data-stock="{{ sym }}">{{ sym }}</a>{% if not loop.last %} · {% endif %}{% endfor %}
        </p>
      </details>
      {% endif %}
      {% if f.note %}<p class="fnd-m">{{ f.note }}</p>{% endif %}
    </div>
    {% endfor %}
  </div>
  {% endif %}

  {# Names that clear MORE THAN ONE rule above. Every finding on its own is a
     single property; this is the only place on the page that asks which
     companies several unrelated rules independently selected. Ranked by how
     many rules a name clears — not scored, and not a recommendation. #}
  {% if findings.multi %}
  <div class="subhead">
    <span class="subeyebrow">Findings</span>
    <h3>Names that clear more than one screen</h3>
    <p class="subdesc">Companies surfacing in two or more findings above. Independent screens agreeing is weak corroboration, not confirmation — they share the same underlying price and fundamental data.</p>
  </div>
  <p class="sdesc" style="margin-bottom:10px;max-width:70ch">
    {{ findings.multi|length }} companies appear in two or more of the findings above.
    One rule selecting a name is a property. Several unrelated rules selecting the same
    name is the finding &mdash; and it is the one thing six separate lists cannot show you.
  </p>
  <div class="fnd-grid rv">
    {% for m in findings.multi[:12] %}
    <div class="card fnd fnd-multi">
      <h4 class="fnd-t">
        <a href="#stocks" class="sym" data-stock="{{ m.sym }}">{{ m.sym }}</a>
        <span class="fnd-n">{{ m.n }} rules</span>
      </h4>
      <p class="fnd-r">{{ m.name or '' }}{% if m.sector %} &middot; {{ m.sector }}{% endif %}{% if m.comp is not none %} &middot; composite {{ m.comp|round|int }}{% endif %}</p>
      <ul class="fnd-hits">
        {% for t in m.findings %}<li>{{ t }}</li>{% endfor %}
      </ul>
    </div>
    {% endfor %}
  </div>
  {% if findings.multi|length > 12 %}
  <p class="sdesc" style="margin-top:8px">Showing the 12 clearing the most rules, of {{ findings.multi|length }}.</p>
  {% endif %}
  {% endif %}

  {% if findings.changed %}
  {% set w = findings.changed %}
  <div class="subhead">
    <span class="subeyebrow">Findings</span>
    <h3>What changed since the last build</h3>
    <p class="subdesc">The diff against the previous weekly screen: who entered, who left, who moved. This is the section that tells you whether anything is actually new this week.</p>
  </div>
  <p class="sdesc" style="max-width:70ch">
    {{ w.moved }} of {{ w.universe }} companies moved since {{ w.compared_with }}{% if w.new_names %},
    and {{ w.new_names }} names are new to the universe{% endif %}.
    Composite scores now sit at
    {% for k, v in w.bands.items() %}<b>{{ v }}</b> in {{ k }}{% if not loop.last %} · {% endif %}{% endfor %}.
    Market breadth: {{ w.breadth.advancing|int }} advancing against
    {{ w.breadth.declining|int }} declining, {{ w.breadth.above200 }}% above the 200-day.
  </p>
  {% endif %}

  <p class="sdesc" style="margin-top:20px;max-width:70ch">
    Research findings, not investment advice. Nothing here is scored, ranked or
    recommended &mdash; each is a group of companies that happen to share a
    measurable property, surfaced because 750 rows are more than anyone scrolls.
  </p>
</section>
{% endif %}

<!-- ══════════ VOLUME ══════════
     Price is half a fact. A name up 8% on its usual volume and a name up 8% on
     four times its usual volume are different events, and the screen has
     carried the ratio all along — a wrong comment in generate.py said the
     field was constant, so nothing read it for months.

     The board never shows the ratio alone. Volume has no direction; pairing it
     with the week's move is what turns a number into a reading, and the third
     reading ("churn") exists because most high-volume days genuinely go
     nowhere and calling those accumulation would be inventing a fact. -->
{% if 'volspikes' in secs and volspikes %}<section class="sec" id="volspikes">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['volspikes'] }} / {{ seclabel['volspikes'] }}</span>
      <h2 class="stitle">Who actually showed up</h2>
    </div>
    <p class="sdesc">Names trading at twice their own average volume or more. Volume is the
      only thing on this page that tells you whether a move had participation behind it.</p>
  </div>

  <p class="lv-3 rv" style="margin-bottom:18px">
    <span class="pill pill-fact">Fact</span>
    The multiple is the day&rsquo;s volume against the name&rsquo;s own average &mdash; not against
    another stock, so a small cap and a large cap are comparable here. Floor of
    <b>2&times;</b> on at least <b>&#8377;5 crore</b> of turnover: a twelve-times day on a name
    that trades forty lakh is a ratio artefact, not a crowd.
    <b>{{ volspikes|length }} shown</b> of the {{ stock_screen.count or '&mdash;' }} screened.
    Bars are drawn against <b>{{ volspike_ceiling }}&times;</b> &mdash; today&rsquo;s highest, so
    the column compares these names to each other rather than to a fixed line.
  </p>

  {# The bar is the infographic: width encodes the ratio, colour encodes the
     DIRECTION of the week — so a wall of red bars reads as distribution across
     the tape at a glance, before any number is read.

     The ceiling is the board's OWN top ratio, floored at 10x. A fixed 10x
     ceiling was tried first and saturated on the first real build: that day's
     board ran to 19x, so all eighteen bars painted full width and the column
     carried no information at all. Flooring at 10x is what stops a quiet day
     — where the best spike is 2.2x — from drawing that as a full bar. #}
  <div class="tblwrap rv">
    <table class="volboard" aria-label="Volume spikes: names trading at twice their own average volume or more">
      <thead>
        <tr>
          <th>Symbol</th><th>Volume vs its own average</th><th class="r">1W</th>
          <th class="r">Price</th><th class="r">Turnover</th><th>Reading</th>
        </tr>
      </thead>
      <tbody>
        {% for v in volspikes %}
        {% set _vs = v.get('vol_spike') %}{% set _w = v.get('r1w') %}
        {% set _px = v.get('price') %}{% set _to = v.get('turnover_cr') %}
        <tr>
          {# Every symbol on the page should open that company's sheet. The
             handler in app.js is delegated on the document and keyed only on
             data-stock, so this costs one attribute; the href is the no-JS
             fallback. It was already true in the sector boards and nowhere
             else, which made the same symbol clickable in one table and inert
             in the next. #}
          <td><a href="#stocks" class="sym" data-stock="{{ v.get('sym') }}"><b>{{ v.get('sym', '&mdash;') }}</b></a><span class="tsub">{{ (v.get('name') or '')[:30] }}</span></td>
          <td>
            <span class="volbar">
              <span class="volbar-fill {{ v.get('vclass') }}"
                    style="width:{{ [((_vs or 0) / volspike_ceiling * 100), 100]|min|round(1) }}%"></span>
            </span>
            <span class="volbar-x num">{{ '%.1f'|format(_vs) }}&times;</span>
          </td>
          <td class="r num {{ 'up' if (_w or 0) > 0 else 'down' if (_w or 0) < 0 else '' }}">{% if _w is not none %}{{ '%+.1f'|format(_w) }}%{% else %}&mdash;{% endif %}</td>
          <td class="r num">{% if _px is not none %}&#8377;{{ '{:,.0f}'.format(_px) }}{% else %}&mdash;{% endif %}</td>
          <td class="r num">{% if _to is not none %}&#8377;{{ '{:,.0f}'.format(_to) }}cr{% else %}&mdash;{% endif %}</td>
          <td><span class="vread {{ v.get('vclass') }}">{{ v.get('vread') }}</span></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

  <p class="lv-sys rv" style="margin-top:12px">
    A volume spike is an observation, not a setup. Nothing here has an entry, a stop
    or a size, none of it has been published to the ledger, and none of it touches the
    win rate. Volume from the weekly screen &mdash; same vintage as the stock screen
    {%- if stock_screen.built_on %}, built {{ stock_screen.built_on }}{% endif %}.
    The <b>Volume, no price</b> rows are the same population <a href="#findings">Findings</a>
    reports as &ldquo;unusual volume, no price response&rdquo; &mdash; one rule, two views,
    so the two can never disagree about which names those are.
    <a href="#method">What these columns mean &rarr;</a>
  </p>
</section>
{% endif %}

<!-- ══════════ LONG-TERM CONVICTION ══════════
     Written by ai_longterm.py, which screens the business before the chart.
     Deliberately NOT in the trade log above and excluded from expectancy: a
     2-3 year idea cannot resolve on a 20-day horizon, and letting it into the
     R statistics would corrupt the only honest number here. -->
{% if 'longterm' in secs %}<section class="sec" id="longterm">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['longterm'] }} / {{ seclabel['longterm'] }}</span>
      <h2 class="stitle">Own the business</h2>
    </div>
    <p class="sdesc">Five NSE names screened on return on capital, growth, leverage and what
      you pay — the chart only votes on whether the trend is intact. Two to three years.
      Selection is arithmetic; the paragraph under each is AI. Excluded from the trading
      win rate on purpose.</p>
    <p class="sdesc" style="margin-top:8px">This screen is roughly 70% annual accounts, and
      annual accounts do not move in a week &mdash; so a name already published in the last
      four weeks steps aside to let a new one through. Where too few fresh names clear the
      score floor, the list is filled from prior weeks and those cards are marked
      <b>held from a prior week</b> rather than presented as new.</p>
  </div>

  <div class="prov rv">
    <span class="pv-tag">WEEKLY</span>
    <span>Runs with the <b>Saturday 09:30 IST</b> scan</span>
    <span id="ltProv">Logged to the ledger as <b>ai_longterm</b> &mdash; in the Signal Log,
      excluded from expectancy</span>
  </div>

  <div id="ltBody">
    <div class="empty rv">The screen runs with the Saturday scan. Five names clear the
      business filter — return on capital, growth, leverage, valuation — before the chart
      gets a vote, and it publishes fewer than five rather than pad the list.</div>
  </div>
</section>{% endif %}

<!-- ══════════ STOCK SCREEN ══════════
     Five hundred companies ranked on published annual statements, sitting
     directly above the Signal Log because that is the reading order: this is
     where a name comes FROM, the log is what happened to the ones acted on.

     Two things about how this renders, both deliberate:

       1. The first 25 rows are server-rendered into the HTML and the remaining
          ~475 arrive from screen.json on demand. Rendering all 500 server-side
          adds ~300KB to a page that is already 220KB, and rendering none
          leaves a section that is blank without JS and invisible to a crawler.
          The table therefore works with JavaScript off — it just does not
          sort, filter or open.
       2. Every score column is decomposable. The four scores are never added
          into one without also showing the four, because "82" tells a reader
          nothing they can disagree with and "quality 91, valuation 38" tells
          them exactly where to argue. -->
{% if 'stocks' in secs %}
<section class="sec" id="stocks">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['stocks'] }} / {{ seclabel['stocks'] }}</span> {{ dh('Stock screen') }}
      <h2 class="stitle">Which {{ stock_screen.count or '—' }}, and why</h2>
      {# THE DECISION ON STALENESS, stated on the section rather than buried.
         Every technical column here — price, RSI, turnover, the moving-average
         stack — comes from the weekly build and is as old as the price date.
         Four trading days by Thursday, which is enough for RSI to move ten
         points and for a reader checking against a live chart to conclude the
         number is broken.

         It stays weekly. The alternative was overlaying live prices from the
         ticker, and the ticker carries about fifty symbols against this
         screen's 750 — six per cent of rows live and the rest not, in one
         table, is worse than a table that is consistently one vintage and
         says so. What changes is that the vintage is now impossible to miss:
         here, and on every priced column head. #}
      {% if stock_screen.price_date %}
      <p class="screen-vintage">
        <span class="sv-tag">PRICED TO {{ stock_screen.price_date }}</span>
        <span>Rebuilt nightly at 02:30 IST. Price, RSI and turnover are from that
          date &mdash; an intraday chart will differ. Fundamentals come from annual
          filings and do not move in a week; they are re-read each night anyway
          rather than kept on a second clock that could disagree with the prices
          beside them.</span>
      </p>
      {% endif %}
    </div>
    <div style="text-align:right">
      <p class="sdesc">The NSE Total Market &mdash; every listed name of any size
        &mdash; ranked on published annual statements: return on capital,
        three-year compounding, leverage, and what the chart is doing. Pick the
        question you are actually asking and the ranking changes, because a good
        company and a good thing to buy today are not the same thing.</p>
      <!-- Absolute, like /today.json and /edition.json. A relative href breaks
           on /day/:date, which Vercel rewrites to this same index.html — the
           link would resolve to /day/screen.json and 404. -->
      <a class="slink" href="/screen.json" target="_blank"
         style="display:inline-block;margin-top:10px">&darr; screen.json</a>
    </div>

  {# ── BREAKOUT BOARD ─────────────────────────────────────────────────────
     A view over screen.json, not a new dataset. Four conditions, and each one
     removes a specific way a breakout list goes wrong:

       above the 20-day high      it is a breakout
       above the 20/50/200 MAs    a breakout inside a downtrend is a bounce
       turnover over Rs 10cr      you can leave. A Rs 10 lakh position in a
                                  name that trades Rs 2cr a day IS the volume
       RSI under 75               not already vertical. Buying the third day
                                  of a straight-line move is how a breakout
                                  becomes somebody else's exit

     No delivery-percentage filter. NSE publishes delivery in the bhavcopy and
     nothing in this build reads that file, so there is no figure to filter on.
     Turnover is the liquidity proxy that actually exists here, and saying so
     is better than implying a delivery screen that is not running. #}
  {% if breakouts %}
  <div class="subhead rv">
    <h3>Breaking out, and liquid enough to leave.</h3>
  </div>
  <p class="lv-3 rv" style="margin-bottom:16px">
    <span class="pill pill-model">Model</span>
    Names closing above their 20-day high while above all three moving averages,
    trading more than &#8377;10 crore a day, and not yet extended.
    <b>{{ breakouts|length }} shown</b> of the {{ stock_screen.count or '—' }} screened.
    Liquidity is measured on turnover &mdash; NSE&rsquo;s delivery percentage is not in
    any feed this build reads, so it is not filtered on and not implied.
  </p>
  <div class="tblwrap rv">
    <table>
      <thead>
        <tr>
          {# Period AND vintage. "RSI(14) daily" was right about the method and silent about the date, so a reader comparing 68.5 here against 58.4 on a live chart concludes the number is wrong. It is not wrong, it is FOUR DAYS OLD — this screen rebuilds weekly and its prices stop at the date below. A stale number without its date is indistinguishable from a broken one. #}
          <th>Symbol</th><th class="r">Turnover{% if stock_screen.price_date %}<span class="th-sub">to {{ stock_screen.price_date[5:] }}</span>{% endif %}</th><th class="r">RSI(14)<span class="th-sub">daily{% if stock_screen.price_date %} · to {{ stock_screen.price_date[5:] }}{% endif %}</span></th>
          <th class="r">1M</th><th class="r">6M</th><th class="r">ROCE</th><th class="r">From 52w high</th>
        </tr>
      </thead>
      <tbody>
        {# Every optional field goes through .get(). A MISSING key in Jinja is
           Undefined, and `Undefined is not none` is TRUE — so a plain
           `is not none` guard passes and the format filter then fails on it.
           That is what broke this table on the first render. #}
        {% for b in breakouts %}
        {% set _rsi = b.get('rsi') %}{% set _r1m = b.get('r1m') %}
        {% set _r6m = b.get('r6m') %}{% set _roce = b.get('roce') %}
        {% set _fh = b.get('from_high') %}{% set _to = b.get('turnover_cr') %}
        <tr>
          <td><a href="#stocks" class="sym" data-stock="{{ b.get('sym') }}"><b>{{ b.get('sym', '—') }}</b></a><span class="tsub">{{ (b.get('name') or '')[:34] }}</span></td>
          <td class="r num">{% if _to is not none %}&#8377;{{ '{:,.0f}'.format(_to) }}cr{% else %}&mdash;{% endif %}</td>
          <td class="r num">{% if _rsi is not none %}{{ '%.0f'|format(_rsi) }}{% else %}&mdash;{% endif %}</td>
          <td class="r num {{ 'up' if (_r1m or 0) > 0 else 'down' }}">{% if _r1m is not none %}{{ '%+.1f'|format(_r1m) }}%{% else %}&mdash;{% endif %}</td>
          <td class="r num {{ 'up' if (_r6m or 0) > 0 else 'down' }}">{% if _r6m is not none %}{{ '%+.1f'|format(_r6m) }}%{% else %}&mdash;{% endif %}</td>
          <td class="r num">{% if _roce is not none %}{{ '%.1f'|format(_roce) }}%{% else %}<span class="lv-sys">not measured</span>{% endif %}</td>
          <td class="r num">{% if _fh is not none %}{{ '%.1f'|format(_fh) }}%{% else %}&mdash;{% endif %}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  <p class="lv-sys rv" style="margin-top:10px">
    A breakout is a setup, not a signal. None of these has been published to the
    ledger, none carries an entry, a stop or a size, and none of them appears in
    the win rate above.
  </p>
  {% endif %}
  </div>

  <!-- Vintage first, like every other weekly artefact here. The price date is
       printed SEPARATELY from the build date because they age differently: a
       three-week-old screen still has usable ROCE and a useless RSI, and a
       reader who is not told which columns went stale will trust both. -->
  <div class="prov{{ ' stale' if stock_screen.is_fallback else '' }} rv">
    <span class="pv-tag">WEEKLY</span>
    <span>Rebuilt <b>Sundays 02:30 IST</b></span>
    {% if stock_screen.built_on %}<span>Built <b>{{ stock_screen.built_on }}</b>
      {%- if stock_screen.age_days is not none %} · {{ stock_screen.age_days }}d old{% endif %}</span>{% endif %}
    {% if stock_screen.price_date %}<span>Prices to <b>{{ stock_screen.price_date }}</b></span>{% endif %}
    {% if stock_screen.coverage %}
    <span>Statements for <b>{{ stock_screen.coverage.statements }}</b> of
      {{ stock_screen.coverage.priced }} · ROCE for <b>{{ stock_screen.coverage.roce }}</b></span>
    {% endif %}
    {% if stock_screen.is_fallback %}
    <span>&#9888; This week&rsquo;s screen has not run &mdash; showing the previous
      build. The technical columns below are from that date, not today.</span>
    {% endif %}
    {# universe/universe_size: already computed by stock_screen.build() (named
       from what was ACTUALLY read, not what was intended — its own comment),
       just never rendered until now. A prior week where NSE's Total Market
       feed failed and the run fell back to plain Nifty 500 looked identical
       to a normal week — this is the second, DIFFERENT kind of fallback from
       is_fallback above (stale BUILD vs. smaller UNIVERSE this run), and a
       reader needs to be told which one, if either, applies. .get() throughout:
       older cached payloads may predate these keys. #}
    {% if stock_screen.get('universe_size') and stock_screen.get('universe_size', 9999) <= 600 %}
    <span style="color:var(--gold)">&#9888; Fell back to <b>{{ stock_screen.get('universe', 'Nifty 500') }}</b>
      this run &mdash; NSE's Total Market feed was unavailable, so this week
      screens a smaller universe than usual.</span>
    {% endif %}
    {% if stock_screen.get('job_status', {}).get('attempted_after_serve') %}
    <span style="color:var(--gold)">&#9888; The most recent rebuild attempt
      ({{ stock_screen.job_status.run_at[:16] }} UTC) failed &mdash;
      {{ stock_screen.job_status.detail[:120] }}</span>
    {% endif %}
  </div>

  <!-- Breadth, measured across this same screened universe (stock_screen.count
       companies — the headline above pulls the same field, so the two numbers
       can never disagree) rather than inferred from an index. It is the only
       market-wide reading on the page that counts businesses instead of
       instruments, and it is deliberately not an input to any score — see
       stock_screen.breadth(). -->
  {% if stock_screen.breadth and stock_screen.breadth.above50 is not none %}
  {% set b = stock_screen.breadth %}
  <div class="scr-breadth rv">
    <div class="sb-k">
      <span class="sb-lab">BREADTH</span>
      {% if b.label %}<b class="sb-reg">{{ b.label }}</b>{% endif %}
      <span class="sb-as">across {{ stock_screen.count }} companies{% if b.as_of %},
        {{ b.as_of }}{% endif %}</span>
    </div>
    <div class="sb-n">
      {% if b.above20 is not none %}<span>Above 20DMA <b>{{ b.above20 }}%</b></span>{% endif %}
      <span>Above 50DMA <b>{{ b.above50 }}%</b></span>
      {% if b.above200 is not none %}<span>Above 200DMA <b>{{ b.above200 }}%</b></span>{% endif %}
      {% if b.counted %}<span>{{ b.advancing }} up / {{ b.declining }} down on the week</span>{% endif %}
      {% if b.median_1m is not none %}<span>Median 1M <b
        class="{{ 'up' if b.median_1m > 0 else 'dn' }}">{{ b.median_1m }}%</b></span>{% endif %}
      {% if b.at_52w_high %}<span><b>{{ b.at_52w_high }}</b> at a 52-week high</span>{% endif %}
    </div>
  </div>
  {% endif %}

  {# Methodology, collapsed. Four dense paragraphs explaining Rank-for, the
     four component scores, how ROCE is computed and what the screen is not —
     ~1,300px of prose sitting between the reader and the actual 750-name
     table. It is the answer to a question, not the headline, and it reads
     identically one click in. Same <details> pattern as the fund screen and
     the engine log. #}
  <details class="fund-note-d rv">
    <summary>How this screen works &mdash; and what it is not</summary>
  <div class="fund-note">
    <strong>A good company is not the same as a good buy today.</strong> That is
    what <b>Rank for</b> is: the four component scores below never change, but each
    mode weights them for a different question. <b>Investor</b> leans on business
    quality and price and almost ignores the chart. <b>Positional</b> wants
    compounding that is also working now. <b>Swing</b> is the chart with the
    fundamentals only as a floor. The same name can be a 53 to an investor and an
    83 to a swing trader &mdash; that gap is the useful part, and averaging it into
    one number is what this section is trying to stop doing.
    <br><br>
    <strong>Four scores, deliberately not one.</strong> A company can be excellent
    and expensive; a chart can be strong while the business degrades. Collapsing
    that into a single number destroys the only thing you need in order to
    disagree with it. So <b>Quality</b> is return on capital, margins and
    leverage; <b>Growth</b> is three-year compounding of revenue and EBITDA;
    <b>Value</b> is how the stock is priced against its own industry peers, not
    against the market; <b>Tech</b> is the chart alone, with no fundamental input.
    The composite is a declared weighted blend of those four
    {%- if stock_screen.weights %} ({{ (stock_screen.weights.quality * 100)|round|int }}/{{ (stock_screen.weights.growth * 100)|round|int }}/{{ (stock_screen.weights.technical * 100)|round|int }}/{{ (stock_screen.weights.valuation * 100)|round|int }}){%- endif %},
    renormalised over whichever of them exist for that company.
    <br><br>
    <strong>On ROCE.</strong> It is computed here from the published income
    statement and balance sheet &mdash; operating profit over capital employed
    &mdash; not read off a field, because the data source does not publish one.
    For lenders it is left blank rather than approximated: a bank&rsquo;s capital
    employed is its deposit base, so the ratio means nothing there and a number
    would be worse than a dash. Where the accounts do not support a figure, the
    cell is empty and the score it feeds is computed from fewer inputs, marked
    with a dotted underline.
    <br><br>
    <strong>What this is not.</strong> A ranking of public data, not advice, and
    not a prediction &mdash; there is no probability, target or forecast anywhere
    in it, because nothing here has been validated as predictive. I am not a
    SEBI-registered adviser.
  </div>
  </details>

  <div class="ctlbar rv" id="scrPresets" role="group" aria-label="Preset screens">
    <span class="ghost" style="margin-left:0">SCREENS<span class="ctl-hint">combine any &mdash; they narrow together</span></span>
    <button type="button" class="fbtn on" data-preset="all">All</button>
    <button type="button" class="fbtn" data-preset="compounders">Quality compounders</button>
    <button type="button" class="fbtn" data-preset="cheapquality">Cheap &amp; good</button>
    <button type="button" class="fbtn" data-preset="growth">High growth</button>
    <button type="button" class="fbtn" data-preset="breakout">Breakouts</button>
    <button type="button" class="fbtn" data-preset="rs">RS leaders</button>
    <button type="button" class="fbtn" data-preset="oversold">Oversold</button>
    <button type="button" class="fbtn" data-preset="debtfree">Debt-free</button>
    <button type="button" class="fbtn" data-preset="piotroski_high" title="Piotroski F-score 7+ on at least 6 of 9 computable criteria">High F-Score</button>
    <button type="button" class="fbtn" data-preset="rsi_oversold">RSI &lt;30</button>
    <button type="button" class="fbtn" data-preset="rsi_neutral">RSI 30-50</button>
    <button type="button" class="fbtn" data-preset="rsi_bullish">RSI 50-70</button>
    <button type="button" class="fbtn" data-preset="rsi_overbought">RSI &gt;70</button>
    <button type="button" class="fbtn" data-preset="cagr_10">3Y CAGR &gt;10%</button>
    <button type="button" class="fbtn" data-preset="cagr_15">3Y CAGR &gt;15%</button>
    <button type="button" class="fbtn" data-preset="cagr_20">3Y CAGR &gt;20%</button>
    <button type="button" class="fbtn" data-preset="cagr_30">3Y CAGR &gt;30%</button>
  </div>

  <!-- Ranking mode. The single most important control here: a good company and
       a good thing to buy today are different questions, and one composite
       cannot answer both. Switching mode re-weights the SAME components and
       re-sorts, so the same stock can be an 82 to an investor and a 96 to a
       swing trader — and that gap is the useful part. -->
  <div class="ctlbar rv" id="scrModes" role="group" aria-label="Ranking mode">
    <span class="ghost" style="margin-left:0">RANK FOR</span>
    <button type="button" class="fbtn on" data-mode="comp">Balanced</button>
    <button type="button" class="fbtn" data-mode="m_inv">Investor</button>
    <button type="button" class="fbtn" data-mode="m_pos">Positional</button>
    <button type="button" class="fbtn" data-mode="m_swing">Swing</button>
    <span class="ghost" id="scrModeNote">business quality, growth, price and chart</span>
  </div>

  <div class="ctlbar rv" id="scrCtl">
    <input type="search" id="scrSearch" placeholder="Symbol, company or ISIN"
           aria-label="Search the screen by symbol, company name or ISIN" autocomplete="off">
    <select id="scrSector" aria-label="Filter by industry"><option value="">All industries</option></select>
    <select id="scrCap" aria-label="Filter by market capitalisation">
      <option value="">Any size</option>
      <option value="l">Large (&gt; ₹50,000cr)</option>
      <option value="m">Mid (₹15,000&ndash;50,000cr)</option>
      <option value="s">Small (&lt; ₹15,000cr)</option>
    </select>
    <select id="scrSort" aria-label="Sort by">
      <option value="comp">Rank (current mode)</option>
      <option value="m_inv">Investor score</option>
      <option value="m_pos">Positional score</option>
      <option value="m_swing">Swing score</option>
      <option value="q">Quality</option>
      <option value="g">Growth</option>
      <option value="v">Value</option>
      <option value="tech">Technical</option>
      <option value="roce">ROCE</option>
      <option value="piotroski">Piotroski F-score</option>
      <option value="rev_cagr">Revenue CAGR</option>
      <option value="r1y">1-year return</option>
      <option value="mcap_cr">Market cap</option>
    </select>
    <button type="button" class="fbtn" id="scrReset">Reset</button>
    <span class="ghost" id="scrCount">{{ stock_screen.count or 0 }} companies</span>
  </div>

  <div class="tw tw-tall rv">
    {# Watchlist and comparison. Hidden until something is watched — an empty
       toolbar teaches the reader there is nothing here. #}
    <div class="wbar rv" id="wBar" style="display:none">
      <span class="wbar-n" id="wCount"></span>
      <button type="button" class="btn btn-sm" id="wOnly">Show only watched</button>
      <button type="button" class="btn btn-sm" id="wCompare">Compare</button>
      <button type="button" class="btn btn-sm ghost" id="wClear">Clear</button>
    </div>

    <table class="t" id="scrTable" style="min-width:1180px">
      <thead><tr>
        <th scope="col" class="wcell"><span class="hp">Watch</span></th>
        <th scope="col" class="sortable" data-k="sym">Company</th>
        <th scope="col" class="num sortable" data-k="price">Price</th>
        <th scope="col" class="num sortable" data-k="r1y">1Y</th>
        <th scope="col" class="num sortable" data-k="comp" aria-sort="descending">Comp</th>
        <th scope="col" class="num sortable" data-k="q">Qual</th>
        <th scope="col" class="num sortable" data-k="g">Grow</th>
        <th scope="col" class="num sortable" data-k="v">Value</th>
        <th scope="col" class="num sortable" data-k="tech">Tech</th>
        <th scope="col" class="num sortable" data-k="roce">ROCE</th>
        <th scope="col" class="num sortable" data-k="roe">ROE</th>
        <th scope="col" class="num sortable" data-k="piotroski" title="Piotroski F-score — 9-point YoY financial-quality checklist">F-Score</th>
        <th scope="col" class="num sortable" data-k="rev_cagr">Rev CAGR</th>
        <th scope="col" class="num sortable" data-k="de">D/E</th>
        <th scope="col" class="num sortable" data-k="pe">PE</th>
        <th scope="col" class="num sortable" data-k="rsi">RSI</th>
        <th scope="col" class="sortable" data-k="risk_lvl">Risk</th>
        <th scope="col">Setup</th>
      </tr></thead>
      <!-- Seeded with the top 25 so the section is never blank and never
           depends on JS to exist. app.js replaces this wholesale once
           screen.json lands.

           Every cell reads s.get(KEY), never s.KEY. The payload has its null
           keys STRIPPED for transport, and a missing key in Jinja is Undefined
           rather than None — `Undefined is not none` is TRUE, so `s.roce ~ '%'`
           on a bank would render a bare "%" where a dash belongs. .get() gives
           a real None and the macro can then do its job. -->
      {% macro n(v, suf='') %}{{ (v ~ suf) if v is not none else '—' }}{% endmacro %}
      <tbody id="scrBody">
        {% for s in stock_screen.rows[:25] %}
        {% set r1y = s.get('r1y') %}
        <tr data-sym="{{ s.sym }}">
          {# Placeholder. The live renderer replaces this tbody wholesale and
             draws a real star; without a matching cell here every column after
             it sits under the wrong heading until screen.json resolves. #}
          <td class="wcell"></td>
          <td><strong class="sym">{{ s.sym }}</strong><br>
              <span class="mono-dim">{{ s.get('name', '')[:34] }}</span></td>
          <td class="num">{{ n(s.get('price')) }}</td>
          <td class="num {{ 'up' if (r1y or 0) > 0 else 'dn' if (r1y or 0) < 0 else '' }}">{{ n(r1y, '%') }}</td>
          <td class="num"><span class="sc s-hi"><b>{{ n(s.get('comp')) }}</b></span></td>
          <td class="num">{{ n(s.get('q')) }}</td>
          <td class="num">{{ n(s.get('g')) }}</td>
          <td class="num">{{ n(s.get('v')) }}</td>
          <td class="num">{{ n(s.get('tech')) }}</td>
          <td class="num">{{ n(s.get('roce'), '%') }}</td>
          <td class="num">{{ n(s.get('roe'), '%') }}</td>
          <td class="num">{{ (s.get('piotroski') ~ '/' ~ s.get('piotroski_of')) if s.get('piotroski') is not none else '—' }}</td>
          <td class="num">{{ n(s.get('rev_cagr'), '%') }}</td>
          <td class="num">{{ n(s.get('de')) }}</td>
          <td class="num">{{ n(s.get('pe')) }}</td>
          <td class="num">{{ n(s.get('rsi')) }}</td>
          <td><span class="mono-dim">{{ (s.get('risk') or {}).get('level') or '—' }}</span></td>
          <td><span class="mono-dim">{{ (s.get('setup') or {}).get('tags', [])[:2]|join(' · ') or '—' }}</span></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  <button type="button" class="scr-more" id="scrMore" hidden>Show more</button>

  <p class="note rv" style="margin-top:14px;color:var(--dim);font-size:12px">
    Ratios are computed from the annual statements the data source publishes, which
    are consolidated where available and occasionally absent altogether &mdash;
    {{ stock_screen.coverage.priced - stock_screen.coverage.statements if stock_screen.coverage else 0 }}
    of the {{ stock_screen.coverage.priced if stock_screen.coverage else 0 }} companies
    priced here have no statements at all. Those carry no composite and cannot be
    ranked: they are in the table, searchable, with their chart and their caveat,
    but a company that reports nothing must not outrank one that does. Per-share
    growth is withheld wherever the share count moved structurally inside the
    history, because an EPS CAGR across a merger or bonus is not a growth rate.
    Returns are total returns off a split- and dividend-adjusted close.
    <br><br>
    Screen rebuilt nightly. A high score means &ldquo;ranked well on published
    numbers&rdquo; and nothing more.
  </p>
</section>
{% endif %}

<!-- ══════════ NEW LISTINGS ══════════
     Every NSE name that listed inside the window, and what it has done since.

     Sits directly under the Stock Screen because it answers the question that
     screen structurally cannot: a company listed four months ago has no annual
     statements to rank on, so it scores None on almost everything and sits
     unranked at the bottom — correct for that screen, useless as an answer to
     "how have this year's listings actually done".

     Two things this deliberately does NOT show:

       1. Issue price, and therefore listing gain. NSE's issue-price data is
          not reliably reachable, and a listing gain computed off a guessed
          issue price is a fabricated number on a page whose entire argument is
          that it does not fabricate. Every figure here is measured from the
          FIRST TRADED CLOSE and the column says so.
       2. Any view on what to do about it. Same rule as the screen. -->
<!-- ══════════ IPO RADAR ══════════
     What is open now and whether to apply. Distinct from New Listings below,
     which measures how already-listed names have traded. Source is NSE's own
     public issue endpoints; mainboard only (series EQ), because SME issues are
     a different asset class with different lot sizes and liquidity.

     The score denominator is deliberately NOT normalised to 100. An issue whose
     book has not opened can only be scored on two of four dimensions, so it
     reads "/45 measured" rather than being scaled up to look complete. Filling
     the gaps to reach a tidy /100 is the exact failure this section exists to
     avoid. -->
{% if 'iporadar' in secs and iporadar %}<section class="sec" id="iporadar">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['iporadar'] }} / {{ seclabel['iporadar'] }}</span>
      <h2 class="stitle">Open books, and whether to apply</h2>
    </div>
    <p class="sdesc">Mainboard NSE issues only &mdash; SME is a different asset class and is
      filtered out by series, not by size. Verdicts are driven by <b>subscription demand</b>,
      the one dimension public data actually measures. Everything not measured is named on
      each card rather than estimated.</p>
  </div>

  <div class="kpi-row rv" style="margin-bottom:18px">
    <div class="kpi"><div class="v">{{ iporadar.counts.open }}</div><div class="k">Open now</div></div>
    <div class="kpi"><div class="v">{{ iporadar.counts.upcoming }}</div><div class="k">Opening soon</div></div>
    <div class="kpi"><div class="v {{ 'up' if iporadar.counts.apply else '' }}">{{ iporadar.counts.apply }}</div><div class="k">Apply / Apply-small</div></div>
    <div class="kpi"><div class="v {{ 'dn' if iporadar.counts.avoid else '' }}">{{ iporadar.counts.avoid }}</div><div class="k">Avoid</div></div>
    <div class="kpi"><div class="v">{{ iporadar.counts.awaiting }}</div><div class="k">Awaiting listing</div></div>
    <div class="kpi"><div class="v">{{ iporadar.counts.listed_measured or iporadar.counts.listed_12m }}</div><div class="k">Listed &amp; measured · 12m</div></div>
  </div>

  {% for grp, label, eyebrow, blurb in [
       ('open', 'Open now', 'Act this week',
        'Books taking bids today. The subscription multiple is live from NSE and moves through the day &mdash; most of it arrives on the final session, so an early figure is not a final one.'),
       ('upcoming', 'Opening soon', 'On the calendar',
        'Dates and bands are confirmed, but no money has been committed yet. Every one of these is WATCH by construction: there is no demand evidence to judge, and a verdict without evidence is a guess with a colour on it.')] %}
  {% set rows = iporadar.get(grp) or [] %}
  {% if rows %}
  <div class="subhead">
    <span class="subeyebrow">{{ eyebrow }}</span>
    <h3>{{ label }}</h3>
    <p class="subdesc">{{ blurb }}</p>
  </div>
  <div class="ipo-grid rv">
    {% for r in rows %}
    {% set v = r.verdict %}
    {# SUMMARY, THEN THE WHOLE CARD — same treatment as the fund screen, and
       for the same reason. Each IPO card carries the band, dates, lot size,
       subscription by category, both sides of the argument, the score
       breakdown and the grey-market caveat. Eight of those open at once is
       the section. Collapsed to the line a reader actually scans — symbol,
       company, verdict, subscription — with every one of those details one
       click down. Nothing removed. #}
    <details class="ipo-card ipo-{{ 'apply' if v.startswith('APPLY') else 'avoid' if v == 'AVOID' else 'watch' }}">
      <summary class="ipo-sum">
        <strong class="sym">{{ r.symbol }}</strong>
        <span class="ipo-sum-co">{{ r.company }}</span>
        {% if r.subscription_x is not none %}<span class="ipo-sum-x num">{{ '%.1fx'|format(r.subscription_x) }}</span>{% endif %}
        <span class="ipo-verdict">{{ v }}</span>
      </summary>
      <div class="ipo-top">
        <div>
          <strong class="sym">{{ r.symbol }}</strong>
          <div class="ipo-co">{{ r.company }}</div>
        </div>
        <span class="ipo-verdict">{{ v }}</span>
      </div>

      <div class="ipo-facts">
        <div><span class="k">Price band</span><span class="v">{{ r.price_band or '—' }}</span></div>
        <div><span class="k">Issue size</span><span class="v">{{ ('₹%s Cr'|format('{:,.0f}'.format(r.issue_size_cr))) if r.issue_size_cr else '—' }}</span></div>
        <div><span class="k">Window</span><span class="v">{{ r.open_date or '—' }} → {{ r.close_date or '—' }}</span></div>
        <div><span class="k">Subscribed</span><span class="v {{ 'up' if r.subscription_x and r.subscription_x >= 3 else 'dn' if r.subscription_x is not none and r.subscription_x < 1 else '' }}">{{ ('%.2fx'|format(r.subscription_x)) if r.subscription_x is not none else ('closed — not published' if r.phase == 'closed' or (r.days_left is not none and r.days_left < 0) else 'not open yet') }}</span></div>
        <div><span class="k">Lot size</span><span class="v">{{ (r.lot_size|int ~ ' shares') if r.lot_size else 'not published' }}</span></div>
        <div><span class="k">Min. investment</span><span class="v">{{ ('₹' ~ '{:,.0f}'.format(r.min_investment)) if r.min_investment else 'not published' }}{% if r.min_investment_derived %}<span class="ipo-drv" title="Lot size × the cap of the price band — the most a retail applicant can be asked for.">calc</span>{% endif %}</span></div>
        {% if r.fresh_issue_cr or r.ofs_cr %}
        <div><span class="k">Fresh / OFS</span><span class="v">{{ ('₹%s Cr'|format('{:,.0f}'.format(r.fresh_issue_cr))) if r.fresh_issue_cr else '—' }} / {{ ('₹%s Cr'|format('{:,.0f}'.format(r.ofs_cr))) if r.ofs_cr else '—' }}</span></div>
        {% endif %}
        {% if r.anchor_cr %}
        <div><span class="k">Anchor book</span><span class="v">₹{{ '{:,.0f}'.format(r.anchor_cr) }} Cr</span></div>
        {% endif %}
        {% if r.listing_date %}
        <div><span class="k">Listing</span><span class="v">{{ r.listing_date }}</span></div>
        {% endif %}
      </div>

      {# GMP is shown and never scored. It is an unofficial grey-market quote
         with no exchange, no audit trail and no regulator behind it — carried
         because a reader will look for it, boxed and labelled because acting on
         it as though it were a published figure is the mistake this whole
         section is built to prevent. #}
      {# The company's own filed numbers. Deliberately outside the score: the
         score measures public DEMAND, which is a fact about what money did,
         while these are accounting figures — turning them into points would
         assert a valuation view this site has no basis for. Published so a
         reader can form theirs. #}
      {% if r.revenue_cr or r.pat_cr or r.roe_pct or r.pe_post_issue or r.roce_pct %}
      <div class="ipo-fin">
        <div class="ipo-fin-h">Business performance{% if r.fy_label %} · {{ r.fy_label }}{% endif %}{% if r.sector %} · <span class="ipo-sector">{{ r.sector }}</span>{% endif %}</div>
        <div class="ipo-fin-g">
          {% if r.revenue_cr %}<div><span class="k">Revenue</span><span class="v">₹{{ '{:,.0f}'.format(r.revenue_cr) }} Cr{% if r.revenue_growth_pct %} <i class="{{ 'up' if r.revenue_growth_pct > 0 else 'dn' }}">{{ '%+.0f'|format(r.revenue_growth_pct) }}%</i>{% endif %}</span></div>{% endif %}
          {% if r.pat_cr %}<div><span class="k">PAT</span><span class="v">₹{{ '{:,.0f}'.format(r.pat_cr) }} Cr{% if r.pat_growth_pct %} <i class="{{ 'up' if r.pat_growth_pct > 0 else 'dn' }}">{{ '%+.0f'|format(r.pat_growth_pct) }}%</i>{% endif %}</span></div>{% endif %}
          {% if r.pat_margin_pct %}<div><span class="k">PAT margin</span><span class="v {{ 'dn' if r.pat_margin_pct < 2 else '' }}">{{ '%.2f'|format(r.pat_margin_pct) }}%</span></div>{% endif %}
          {% if r.roe_pct %}<div><span class="k">ROE</span><span class="v">{{ '%.0f'|format(r.roe_pct) }}%</span></div>{% endif %}
          {% if r.roce_pct %}<div><span class="k">ROCE</span><span class="v">{{ '%.0f'|format(r.roce_pct) }}%</span></div>{% endif %}
          {% if r.ebitda_margin_pct %}<div><span class="k">EBITDA margin</span><span class="v">{{ '%.1f'|format(r.ebitda_margin_pct) }}%</span></div>{% endif %}
          {% if r.debt_to_equity is not none and r.debt_to_equity != 0 %}<div><span class="k">Debt / equity</span><span class="v">{{ '%.2f'|format(r.debt_to_equity) }}</span></div>{% endif %}
          {% if r.pe_post_issue %}<div><span class="k">P/E post-issue</span><span class="v">{{ '%.1f'|format(r.pe_post_issue) }}x{% if r.peer_pe %} <i class="mono-dim">vs {{ '%.0f'|format(r.peer_pe) }}x median of {{ r.peer_pe_n }} peer{{ '' if r.peer_pe_n == 1 else 's' }}</i>{% endif %}</span></div>{% endif %}
        </div>
        {% if r.pat_margin_pct and r.pat_margin_pct < 2 %}
        <p class="ipo-fin-w">A PAT margin under 2% means a high-volume, low-margin model:
          a small move in input costs or realisations swings profit far more than it swings
          revenue. Read the P/E against that, not against the growth rate.</p>
        {% endif %}
        {% if r.use_of_proceeds %}<p class="ipo-fin-u"><b>Proceeds:</b> {{ r.use_of_proceeds }}</p>{% endif %}
      </div>
      {% endif %}

      {# Derived from the filed numbers above, never scraped. Chittorgarh carries
         analyst prose and lifting it would be somebody else's judgement wearing
         this page's voice, with no way to check it. Every line here is a
         statement about a number already on the card, with its threshold named,
         so a reader can disagree with the threshold rather than with an
         assertion. Silent where a figure is missing. #}
      {% if r.reads_for or r.reads_against %}
      <div class="ipo-args">
        {% if r.reads_for %}<div class="ipo-arg ipo-arg-y"><b>What the numbers support</b>
          <ul>{% for s in r.reads_for %}<li>{{ s }}</li>{% endfor %}</ul></div>{% endif %}
        {% if r.reads_against %}<div class="ipo-arg ipo-arg-n"><b>What they argue against</b>
          <ul>{% for s in r.reads_against %}<li>{{ s }}</li>{% endfor %}</ul></div>{% endif %}
      </div>
      <p class="ipo-args-src">Read from this company&rsquo;s own filed figures, not from any
        analyst&rsquo;s view. Margin and debt/equity are computed from the two rows above them;
        everything else is stated in the prospectus summary.</p>
      {% endif %}

      {% if r.gmp_text %}
      <div class="ipo-gmp">
        <span class="ipo-gmp-k">Grey market</span>
        <span class="ipo-gmp-v">{{ r.gmp_text }}</span>
        <span class="ipo-gmp-w">unofficial · not scored · no audit trail</span>
      </div>
      {% endif %}

      {# WHO is bidding, not just how much. NSE publishes one number — the
         total — and a book carried by retail while institutions sit out reads
         very differently from the reverse. Source is named because it is not
         the exchange's figure. #}
      {% if r.subscription_by_category %}
      <div class="ipo-cats">
        {% for c, n in r.subscription_by_category.items() %}
        <span class="{{ 'ipo-cat-hot' if n >= 10 else 'ipo-cat-cold' if n < 1 else '' }}">{{ c }} <b>{{ '%.2fx'|format(n) }}</b></span>
        {% endfor %}
        {% if r.category_source %}<span class="ipo-cat-src">via {{ r.category_source }}</span>{% endif %}
      </div>
      {% endif %}

      <div class="ipo-score">
        <div class="ipo-score-bar"><i style="width:{{ r.score.pct or 0 }}%"></i></div>
        <span class="ipo-score-n">{{ r.score.points }}/{{ r.score.of }} measured</span>
        <span class="ipo-score-parts">{% for k, val in r.score.parts.items() %}{{ k }} {{ val }}{% if not loop.last %} · {% endif %}{% endfor %}</span>
      </div>

      <p class="ipo-why"><b>Why:</b> {{ r.verdict_why }}</p>
      <p class="ipo-caveat"><b>Why it might be wrong:</b> {{ r.verdict_caveat }}</p>
      <p class="ipo-missing"><b>No public source:</b> {{ r.score.not_measured|join(' · ') }}
        {% if r.score.shown_not_scored %}<br><b>Shown but not scored:</b> {{ r.score.shown_not_scored|join(' · ') }}{% endif %}</p>
    </details>
    {% endfor %}
  </div>
  {% endif %}
  {% endfor %}

  {# Subscription CLOSED, no listing date yet. These are live decisions —
     allotment, refunds and a listing date are all still ahead — so they sit
     beside open and upcoming rather than in history. A book that closed more
     than three weeks ago with no listing date is NOT pending; it is stalled or
     withdrawn, and is separated below rather than presented as forthcoming. #}
  {% if iporadar.awaiting_listing %}
  <div class="subhead">
    <span class="subeyebrow">Bid closed</span>
    <h3>Awaiting listing</h3>
    <p class="subdesc">Subscription has closed and no listing date is published yet. Nothing
      here can still be applied for &mdash; what is outstanding is allotment, refunds and the
      listing itself. Normal mainboard timetables run about three days.</p>
  </div>
  <div class="tw rv">
    <table class="t"><thead><tr>
      <th scope="col">Symbol</th><th scope="col">Company</th>
      <th scope="col">Price band</th><th scope="col">Bid closed</th>
      <th scope="col">Lists on</th><th scope="col">Grey market</th><th scope="col">Days since</th>
    </tr></thead><tbody>
      {% for r in iporadar.awaiting_listing %}
      <tr><td><strong class="sym">{{ r.symbol }}</strong></td>
        <td>{{ r.company }}</td><td class="num">{{ r.price_band or '—' }}</td>
        <td class="num">{{ r.close_date }}</td>
        <td class="num">{{ r.listing_date or 'not announced' }}</td>
        <td class="num">{% if r.gmp_text %}<span class="wal-live" title="Unofficial grey-market quote. No exchange, no audit trail — context, not a price.">{{ r.gmp_text }}</span>{% else %}—{% endif %}</td>
        <td class="num">{{ r.days_since_close }}d</td></tr>
      {% endfor %}
    </tbody></table>
  </div>
  {% endif %}

  {% if iporadar.stalled %}
  <p class="subdesc" style="margin-top:12px">
    <b>{{ iporadar.stalled|length }}</b> issue{{ 's' if iporadar.stalled|length != 1 }} closed more than
    three weeks ago with no listing date on record &mdash;
    {% for r in iporadar.stalled %}{{ r.symbol }}{{ ', ' if not loop.last }}{% endfor %}.
    Listed here as unresolved rather than as forthcoming, because presenting a stalled book as
    pending would be a claim this section cannot support.
  </p>
  {% endif %}

  {% if iporadar.recent_listed %}
  <div class="subhead">
    <span class="subeyebrow">The record</span>
    <h3>Listed in the last {{ iporadar.recent_window_months }} months</h3>
    <p class="subdesc">All <b>{{ iporadar.counts.listed_12m }}</b> mainboard issues that closed
      and listed inside the window, <b>{{ iporadar.counts.listed_measured }}</b> of them with
      measured performance. Return is from the <b>first traded close</b>, not the issue price
      &mdash; nothing trades before it lists, so the first close is the first close by
      construction, while NSE's issue-price data is unreliable and a listing gain built on a
      guessed one would be fabricated. Names inside the 750-stock screen open their full detail
      sheet; every row links to its chart.
      Return is measured from the <b>first traded close</b>, not the issue price &mdash; NSE's
      issue-price data is not reliable and a listing gain computed off a guessed one would be
      fabricated. Click any measured name for its full screen detail.</p>
  </div>
  <div class="tw rv">
    <table class="t"><thead><tr>
      <th scope="col">Symbol</th><th scope="col">Company</th>
      <th scope="col">Listed</th><th scope="col">First close / band</th><th scope="col">Last</th>
      <th scope="col">Since listing</th><th scope="col">From high</th><th scope="col">Sessions</th>
    </tr></thead><tbody>
      {# Measured rows first, then the ones nothing could price, under a label.
         Mixed together they read as broken: SAATVIK shows a September date and
         a price band with every performance cell dashed, and a reader
         reasonably asks how that is a "listing". It is one — NSE published the
         issue — but no traded line exists for it on any feed this build reads,
         so there is nothing to measure and the row is evidence of that rather
         than of a bug. Saying so is the whole difference. #}
      {% set _measured = iporadar.recent_listed | selectattr('measured') | list %}
      {% set _unmeasured = iporadar.recent_listed | rejectattr('measured') | list %}
      {% for r in (_measured + _unmeasured) %}
      {% if loop.index0 == _measured | length and _unmeasured %}
      <tr class="ipo-split">
        <td colspan="8">
          <span class="pill pill-fact">Issued</span>
          {{ _unmeasured | length }} of these never produced a traded price on
          any feed this build reads — partly-paid instruments, symbols that
          changed, or issues that did not list. The date is when the issue
          closed, not a listing date, and the band is the issue band. Nothing
          below is measured, and none of it is scored.
        </td>
      </tr>
      {% endif %}
      <tr class="{{ '' if r.measured else 'ipo-unmeasured' }}">
        <td><strong class="sym"{% if r.measured %} data-stock="{{ r.symbol }}" style="cursor:pointer"{% endif %}>{{ r.symbol }}</strong>
          {# TradingView for every row, measured or not. A listing outside the
             screen universe has no sheet to open, but it still has a chart, and
             a dead name in a table is worse than one that goes somewhere. #}
          <a class="tv-lnk" target="_blank" rel="noopener"
             href="https://www.tradingview.com/chart/?symbol=NSE%3A{{ r.symbol }}"
             title="Open {{ r.symbol }} on TradingView">chart</a></td>
        <td>{{ r.company }}</td>
        <td class="num">{{ r.listing_date }}{% if not r.measured %} <span class="lv-sys" title="Issue date — this symbol never produced a traded price">issue</span>{% endif %}</td>
        <td class="num">{{ '{:,.2f}'.format(r.first_close) if r.first_close else (r.price_band or '—') }}</td>
        <td class="num">{{ '{:,.2f}'.format(r.last_close) if r.last_close else '—' }}</td>
        {# `measured` means ipo_tracker reached the symbol, NOT that every field
           came back — a name with too few sessions has a listing return and no
           distance-from-high. Jinja's format filter raises on None rather than
           printing a blank, and one such row took down the entire page render
           in CI while five complete rows had passed locally. Guard each. #}
        <td class="num {{ 'up' if (r.since_listing_pct or 0) > 0 else 'dn' }}">{% if r.since_listing_pct is not none %}{{ '%+.1f'|format(r.since_listing_pct) }}%{% else %}&mdash;{% endif %}</td>
        <td class="num {{ 'dn' if (r.from_high_pct or 0) < 0 else '' }}">{% if r.from_high_pct is not none %}{{ '%+.1f'|format(r.from_high_pct) }}%{% else %}&mdash;{% endif %}</td>
        <td class="num mono-dim">{{ r.sessions if r.sessions is not none else '—' }}</td>
      </tr>
      {% endfor %}
    </tbody></table>
  </div>
  {% endif %}

  <p class="fine" style="margin-top:20px;max-width:80ch">
    Not investment advice, and not a view on any business. A verdict here is a reading of
    <i>public demand</i> and nothing more &mdash; NSE's feeds carry no lot size, no sector, no
    financials and no valuation, so none of those inform the score and none are guessed at.
    Grey-market premium is deliberately absent: it is an unofficial quote with no audit trail.
    Sources: NSE public issue endpoints for band, size, dates and subscription, read
    {{ iporadar.generated_at[:16]|replace('T', ' ') }}. Lot size, minimum application, the
    fresh/OFS split, anchor book and grey-market quote come from Chittorgarh, which NSE does
    not publish &mdash; those fields are marked on each card and none of them touch the score.
  </p>
</section>
{% endif %}

<!-- New Listings retired 2026-08-22. IPO Radar's "Listed in the last 12
     months" carries the same population and the same measured returns,
     plus the unmeasured listings this section omitted. Two sections
     answering one question with two different counts (32 here, 88 there)
     was the duplication being reported. ipo_tracker.py still runs and the
     Radar consumes its rows — the data did not go anywhere, the second
     rendering of it did. -->

<!-- ══════════ FUND SCREEN ══════════
     A ranking of public data, not a recommendation. Direct + Growth plans
     only, ranked on CAGR computed here from AMFI's own NAV series.

     Deliberately shows the drawdown next to the return: a 3-year CAGR on its
     own tells you the reward and hides the ride, and the small-cap column is
     where that gap is widest. -->
{# Guard is `secs` alone. The extra `fund_screen.get('categories')` test that
   used to live here is what let the section vanish while the nav still linked
   to it — see page_context(drop=...), which now decides this once. #}
{% if 'funds' in secs %}
<section class="sec" id="funds">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['funds'] }} / {{ seclabel['funds'] }}</span> {{ dh('Fund screen') }}
      <h2 class="stitle">Where the SIP goes</h2>
    </div>
    <p class="sdesc">Top three by three-year return in each category, from
      {{ fund_screen.source }}. Direct plans only &mdash; same portfolio, same
      manager, without the distributor commission. Returns are computed from the
      published NAV series, not copied off a factsheet.</p>
  </div>

  <!-- Vintage, not just result. This table looking identical to last week's is
       the EXPECTED outcome of a weekly screen over multi-year returns — but it
       is also exactly what a screen that never ran looks like, and until now
       the page could not tell you which. -->
  <div class="prov{{ ' stale' if fund_screen.is_fallback else '' }} rv">
    <span class="pv-tag">WEEKLY</span>
    <span>Rebuilt <b>Sundays 01:30 IST</b>, after the week&rsquo;s last NAV publish</span>
    {% if fund_screen.built_on %}<span>Built <b>{{ fund_screen.built_on }}</b>
      {%- if fund_screen.age_days is not none %} · {{ fund_screen.age_days }}d old{% endif %}</span>{% endif %}
    {% if fund_screen.is_fallback %}
    <span>&#9888; This week&rsquo;s screen has not run &mdash; showing the previous build.
      NAVs below are from that date, not today.</span>
    {% endif %}
    {% if fund_screen.get('job_status', {}).get('attempted_after_serve') %}
    <span style="color:var(--gold)">&#9888; The most recent rebuild attempt
      ({{ fund_screen.job_status.run_at[:16] }} UTC) failed &mdash;
      {{ fund_screen.job_status.detail[:120] }}</span>
    {% endif %}
  </div>

  {# Collapsed for the same reason as the stock screen's methodology block:
     it is a standing caveat, not today's news, and it sat above every fund
     card on every visit. The per-category `facts` note below stays open —
     that one changes weekly and is about these specific funds. #}
  <details class="fund-note-d rv">
    <summary>On expense ratio &mdash; why this screen is Direct-only</summary>
  <div class="fund-note">
    <strong>On expense ratio.</strong> Per-scheme TER is not published in the free
    AMFI feed, so this screen does not claim to know it. The cost lever that
    <em>is</em> visible is Direct versus Regular, and it is the big one: a Regular
    plan carries the distributor commission inside its TER, typically 0.5&ndash;1.2%
    a year more for the same portfolio. Everything below is Direct.
  </div>
  </details>

  {% for cat in fund_screen.categories %}
  {% if cat.funds %}
  {# SUMMARY FIRST, DETAIL ON REQUEST.
     Six categories, three fund cards each, three CAGR bars per card and a
     paragraph of category facts is the entire mutual-fund section open at
     once — several screens of it, on a page that already runs long, for a
     reader who mostly wants to know which fund leads.

     One line per category now: the leader, its 3-year return, how many were
     screened. Everything that was here is still here, one click down. The
     first category is open so the pattern is visible without guessing that
     the rows expand. #}
  <details class="fundcat rv"{% if loop.first %} open{% endif %}>
    <summary class="fundcat-sum">
      <span class="fc-name">{{ cat.label }}</span>
      {% if cat.funds and cat.funds[0] %}
      <span class="fc-lead">{{ cat.funds[0].name[:44] }}</span>
      {% set _r3 = cat.funds[0].get('r3') %}
      {% if _r3 is not none %}<span class="fc-r3 num">{{ '%.1f'|format(_r3) }}%<i>3Y</i></span>{% endif %}
      {% endif %}
      <span class="fc-n">{{ cat.screened }} screened</span>
    </summary>
    <p class="fundcat-b">{{ cat.blurb }}</p>

    {% set facts = cat.get('facts') %}
    {% if facts %}
    <div class="fund-note rv" style="margin:12px 0">
      <strong>{{ facts.best_5y.name }}</strong> leads the full screened set on 5-year return
      ({{ facts.best_5y.r5 }}%) &mdash; not always the same fund as the 3-year leader above.
      The category spans {{ facts.dispersion_5y }} points between best and worst 5-year performer
      ({{ facts.worst_5y.name }}, {{ facts.worst_5y.r5 }}%).
      {% if facts.get('steadiest_top_quartile') %}
      Steadiest top-quartile fund: <strong>{{ facts.steadiest_top_quartile.name }}</strong>
      &mdash; {{ facts.steadiest_top_quartile.volatility }}% volatility at a
      {{ facts.steadiest_top_quartile.r3 }}% 3-year return, the smoothest ride among funds that
      still beat most of the category.
      {% endif %}
    </div>
    {% endif %}

    <!-- Card grid, not a table — each fund is its own unit at this point,
         with three CAGR bars (1Y/3Y/5Y, same .sd-sc/.bar component the trade
         sheet already uses) rather than one same-shaped row among six.

         Every NEW field (r1/volatility/bar_*/percentile_r3/facts) is read
         via .get() — dict.get(), not Jinja attribute lookup — because this
         environment uses StrictUndefined and the fund cache refreshes
         weekly (fund_screen.yml): a payload cached by an OLDER build of
         funds.py genuinely lacks these keys until its next rebuild, and
         `f.r1 is not none` still raises on a truly MISSING key — only a
         key that exists with value None passes that check silently. This
         crashed the entire page build once already (2026-08-16, prod
         cache still had the pre-upgrade 6-field schema) before this fix. -->
    <div class="fund-grid rv">
      {% for f in cat.funds %}
      {% set r1 = f.get('r1') %}
      {% set r5 = f.get('r5') %}
      {% set dd3 = f.get('dd3') %}
      {% set vol = f.get('volatility') %}
      {% set pct = f.get('percentile_r3') %}
      <div class="card fund-card">
        <div class="fund-card-h">
          <div>
            <strong>{{ f.name }}</strong><br>
            <span class="mono-dim" style="font-size:11px">{{ f.house }}</span>
          </div>
          {% if f.get('isin') %}<span class="fund-isin mono-dim" title="ISIN — paste this into any broker or platform to find the exact scheme">{{ f.isin }}</span>{% endif %}
        </div>
        <div class="sd-scores">
          <div class="sd-sc">
            <span class="k">1Y</span>
            <span class="v">{{ r1 if r1 is not none else '—' }}{{ '%' if r1 is not none else '' }}</span>
            <span class="bar"><i style="width:{{ f.get('bar_r1', 0) }}%"></i></span>
          </div>
          <div class="sd-sc">
            <span class="k">3Y</span>
            <span class="v">{{ f.r3 }}%</span>
            <span class="bar"><i style="width:{{ f.get('bar_r3', 0) }}%"></i></span>
          </div>
          <div class="sd-sc">
            <span class="k">5Y</span>
            <span class="v">{{ r5 if r5 is not none else '—' }}{{ '%' if r5 is not none else '' }}</span>
            <span class="bar"><i style="width:{{ f.get('bar_r5', 0) }}%"></i></span>
          </div>
        </div>
        <div class="fund-card-f">
          {% if vol is not none %}<span class="mono-dim">Volatility <b style="color:var(--text)">{{ vol }}%</b></span>{% endif %}
          <span class="mono-dim">Worst fall (3y) <b class="dn">{{ dd3 if dd3 is not none else '—' }}{{ '%' if dd3 is not none else '' }}</b></span>
          {% if pct is not none %}<span class="mono-dim">Top {{ 100 - pct }}% of category</span>{% endif %}
          {# Fund age sits with the other headline facts, not buried in the
             detail panel: a 3Y CAGR from a fund with 3.2 years of history is a
             different claim from the same number over 12 years. #}
          {% if f.get('history_years') %}<span class="mono-dim">Age <b style="color:var(--text)">{{ f.history_years }}y</b></span>{% endif %}
          <span class="mono-dim">NAV {{ f.nav }}</span>
        </div>

        {# ── What it actually owns ──
           Always rendered when the data exists, not hidden behind the detail
           toggle: the NAV series ranks these funds but says nothing about what
           is inside them, and two funds with the same 3Y CAGR can be a
           banks-and-IT portfolio and a smallcap-industrials one. That is the
           difference that decides whether adding one diversifies anything.
           Absent entirely when the portfolio could not be resolved — never a
           placeholder, never a guess. #}
        {% set pf = f.get('portfolio') %}
        {% if pf and (pf.get('top_sectors') or pf.get('top_stocks')) %}
        <div class="fpf">
          {% if pf.get('top_sectors') %}
          <div class="fpf-r">
            <span class="fpf-k">Sectors</span>
            <span class="fpf-v">
              {% for s in pf.top_sectors %}<span class="fpf-c">{{ s.name }} <b>{{ s.pct }}%</b></span>{% endfor %}
            </span>
          </div>
          {% endif %}
          {% if pf.get('top_stocks') %}
          <div class="fpf-r">
            <span class="fpf-k">Top holdings</span>
            <span class="fpf-v">
              {% for s in pf.top_stocks %}<span class="fpf-c">{{ s.name|replace(' Ltd.','')|replace(' Limited','') }} <b>{{ s.pct }}%</b></span>{% endfor %}
            </span>
          </div>
          {% endif %}
          <div class="fpf-m">
            {% if pf.get('holdings_count') %}{{ pf.holdings_count }} holdings{% endif %}
            {%- if pf.get('equity_pct') %} · {{ pf.equity_pct }}% in equity{% endif %}
            {%- if pf.get('as_on') %} · as on {{ pf.as_on }}{% endif %}
          </div>
        </div>
        {% endif %}

        {# Advanced detail as a native <details>, deliberately not a modal.
           Overlays in this template have to live outside <main> to escape its
           stacking context, and an inline panel also lets two funds be opened
           side by side to compare — which is the whole point of a screen.
           Everything below comes from the same NAV series as the headline
           numbers; nothing is fetched and nothing is estimated. #}
        {% set cal = f.get('calendar') or [] %}
        {% set roll = f.get('rolling3y') %}
        {% if cal or roll or f.get('inception') %}
        <details class="fund-more">
          <summary>Advanced detail</summary>
          <div class="fund-more-b">
            {% if roll %}
            <div class="fm-block">
              <div class="fm-h">Rolling 3-year return &mdash; {{ roll.windows }} start dates</div>
              <p class="fm-note">The headline 3Y figure is one window ending today. This is every
                3-year hold this fund has ever offered, so you can see the range rather than
                the one number the calendar happens to produce.</p>
              <div class="fm-row"><span>Best</span><b class="up">{{ roll.best }}%</b></div>
              <div class="fm-row"><span>Median</span><b>{{ roll.median }}%</b></div>
              <div class="fm-row"><span>Worst</span><b class="{{ 'dn' if roll.worst < 0 else '' }}">{{ roll.worst }}%</b></div>
              <div class="fm-row"><span>Beat 7% a year</span><b>{{ roll.above_7pct }}% of windows</b></div>
            </div>
            {% endif %}

            {% if cal %}
            <div class="fm-block">
              <div class="fm-h">Calendar year returns</div>
              <p class="fm-note">Completed years only &mdash; a part-year is never annualised here.</p>
              <div class="fm-cal">
                {% for c in cal %}
                <div class="fm-cy">
                  <span class="fm-cy-y">{{ c.year }}</span>
                  <span class="fm-cy-v {{ 'up' if c.ret >= 0 else 'dn' }}">{{ '%+.1f'|format(c.ret) }}%</span>
                </div>
                {% endfor %}
              </div>
            </div>
            {% endif %}

            <div class="fm-block">
              <div class="fm-h">Scheme</div>
              {% if f.get('inception') %}<div class="fm-row"><span>NAV history from</span><b>{{ f.inception }}</b></div>{% endif %}
              {% if f.get('history_years') %}<div class="fm-row"><span>Track record</span><b>{{ f.history_years }} years</b></div>{% endif %}
              {% if f.get('since_inception') is not none %}<div class="fm-row"><span>Since inception</span><b>{{ f.since_inception }}% a year</b></div>{% endif %}
              {% if f.get('scheme_type') %}<div class="fm-row"><span>Type</span><b>{{ f.scheme_type }}</b></div>{% endif %}
              {% if f.get('isin') %}<div class="fm-row"><span>ISIN</span><b class="mono">{{ f.isin }}</b></div>{% endif %}
              <div class="fm-row"><span>NAV on {{ f.nav_date }}</span><b>{{ f.nav }}</b></div>
            </div>

            <p class="fm-src">
              Computed from the full published NAV series, not copied off a factsheet.
              <a href="{{ f.url }}" target="_blank" rel="noopener">Open the raw NAV series (JSON)</a>
              &mdash; this is the data source, not a fund page.
            </p>
          </div>
        </details>
        {% endif %}
      </div>
      {% endfor %}
    </div>
  </details>
  {% endif %}
  {% endfor %}

  <p class="note rv" style="margin-top:14px;color:var(--dim);font-size:12px">
    Past return is the only thing a NAV series can tell you, and it is the weakest
    predictor of the next three years there is. A fund at the top of a three-year
    table is often there because its style was in favour, not because it will stay
    there. This is a screen, not advice &mdash; I am not a SEBI-registered adviser.
    <br><br>
    NAV as of {{ fund_screen.categories[0].funds[0].nav_date if fund_screen.categories[0].funds else '—' }}.
    Screen rebuilt weekly.
  </p>
</section>
{% endif %}

<!-- ══════════ 04 SIP BUCKETS ══════════
     ₹10,000/month, stepped up 10% each SIP year, one bucket per month, four
     names per bucket. Every bucket keeps its own cost basis and its own XIRR —
     a blended portfolio number would hide which months' picks actually worked,
     which is the only feedback the ranking engine gets.
     Filled from /api/sip; the whole section hides itself on a static build. -->
{% if 'sip' in secs %}<section class="sec" id="sip" style="display:none">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['sip'] }} / {{ seclabel['sip'] }}</span>
      <h2 class="stitle">One bucket a month</h2>
    </div>
    <div style="text-align:right">
      <span class="slink" id="sipPlan">—</span>
      <p class="sdesc" style="margin-top:8px">Whole shares only — every name in a bucket
        is priced so at least one share fits its slice. Buckets are never blended; the
        month that worked and the month that did not stay visible as separate lines.</p>
    </div>
  </div>

  <div class="kpi-row rv">
    <div class="kpi"><div class="v" id="sipMonthly">—</div><div class="k">This month</div></div>
    <div class="kpi"><div class="v" id="sipBuckets">0</div><div class="k">Buckets</div></div>
    <div class="kpi"><div class="v" id="sipInvested">—</div><div class="k">Invested</div></div>
    <div class="kpi"><div class="v" id="sipValue">—</div><div class="k">Value</div></div>
    <div class="kpi"><div class="v" id="sipPnl">—</div><div class="k">Unrealised</div></div>
  </div>

  <div id="sipBody"></div>

  <div class="shead rv" style="margin-top:34px;border-top:1px solid var(--line);padding-top:22px">
    <div>
      <span class="snum">THE ARITHMETIC</span>
      <h2 class="stitle" style="font-size:24px">Where the step-up takes it</h2>
    </div>
  </div>
  <div class="tw rv"><table class="t" id="sipProj"><thead><tr>
    <th scope="col">Year</th><th scope="col">Monthly</th><th scope="col">Invested</th><th scope="col">@12%</th><th scope="col">@14%</th><th scope="col">@16%</th>
  </tr></thead><tbody></tbody></table></div>
  <p class="note rv" style="margin-top:10px;color:var(--dim);font-size:12px">
    Projections are compound arithmetic on the contribution schedule, not a forecast.
    They assume the return shown is achieved every year with no gaps in contribution.
    Actual equity returns arrive in a very different order, and sequence matters.
  </p>
</section>{% endif %}

<!-- ══════════ SWP ══════════
     The other half of the SIP section: buckets say what goes in, this says
     what comes out and for how long. Entirely client-side arithmetic — no API,
     no ledger — so it behaves the same on the static host and needs no
     display:none/reveal dance.

     Two things here are not the usual calculator arithmetic and are the whole
     reason it is worth having:
       1. Withdrawals are grossed up for capital gains tax using proportional
          cost-basis depletion, so the number entered is what actually reaches
          the bank. A calculator that ignores tax overstates how long the
          corpus lasts, which is the one thing it exists to tell you.
       2. The corpus is shown in nominal AND today's rupees. ₹2 Cr at 60 is not
          ₹2 Cr of groceries at 60, and the nominal line is the flattering one. -->
{% if 'swp' in secs %}<section class="sec" id="swp">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['swp'] }} / {{ seclabel['swp'] }}</span>
      <h2 class="stitle">And what it pays out</h2>
    </div>
    <p class="sdesc">Accumulate to retirement, then draw down. Withdrawals are grossed up
      for capital gains tax, so the monthly figure is what lands in the bank, not what
      leaves the fund. The dashed line is the same corpus in today&rsquo;s rupees.</p>
  </div>

  <!-- Collapsed by default. The eleven inputs and a 35-row table are for the
       sittings where you actually re-plan; the rest of the time the four
       numbers in the summary are the whole point, and the section should not
       push Performance and the Signal Log a screen and a half down the page.
       The summary strip below stays visible in BOTH states, so collapsing
       hides the controls, never the answer. -->
  <div class="swp-sum rv" id="swpSum">
    <div class="swp-sum-k">
      <span><b id="swpSumCorpus">—</b> at <span id="swpSumAge">—</span></span>
      <span class="sep">·</span>
      <span><b id="swpSumDraw">—</b>/month</span>
      <span class="sep">·</span>
      <span id="swpSumLast">—</span>
    </div>
    <button type="button" class="swp-toggle-all" id="swpExpand" aria-expanded="false"
            aria-controls="swpBody">Adjust the plan &darr;</button>
  </div>

  <div id="swpBody" class="swp-body" hidden>
  <div class="swp-in rv">
    <label>Age now<input type="number" id="swpCurAge" value="34" min="18" max="75" step="1"></label>
    <label>Retire at<input type="number" id="swpRetAge" value="55" min="35" max="80" step="1"></label>
    <label>Plan till<input type="number" id="swpEndAge" value="90" min="60" max="105" step="1"></label>
    <label>Corpus today<input type="number" id="swpCorpus" value="500000" min="0" step="10000"></label>
    <label>SIP / month<input type="number" id="swpSip" value="30000" min="0" step="1000"></label>
    <label>Step-up % / yr<input type="number" id="swpStep" value="10" min="0" max="25" step="1"></label>
    <label>Return pre %<input type="number" id="swpRetPre" value="12" min="0" max="30" step="0.5"></label>
    <label>Return post %<input type="number" id="swpRetPost" value="8" min="0" max="30" step="0.5"></label>
    <label>Inflation %<input type="number" id="swpInfl" value="6" min="0" max="15" step="0.5"></label>
    <label>Withdraw / mth<input type="number" id="swpDraw" value="100000" min="0" step="5000"></label>
    <label>LTCG tax %<input type="number" id="swpTax" value="12.5" min="0" max="40" step="0.5"></label>
  </div>

  <div class="kpi-row rv">
    <div class="kpi"><div class="v" id="swpKCorpus">—</div><div class="k">Corpus at retirement</div></div>
    <div class="kpi"><div class="v" id="swpKNeed">—</div><div class="k">Corpus required</div></div>
    <div class="kpi"><div class="v" id="swpKDraw">—</div><div class="k">First withdrawal / month</div></div>
    <div class="kpi"><div class="v" id="swpKLast">—</div><div class="k">Money lasts till</div></div>
  </div>

  <div class="swp-verdict rv" id="swpVerdict"></div>

  <div class="card rv">
    <div class="cardhead">
      <span class="eyebrow">Corpus path</span>
      <span class="eyebrow" id="swpPeak">&nbsp;</span>
    </div>
    <svg id="swpChart" viewBox="0 0 760 214" width="100%" role="img" aria-labelledby="swpChartT">
      <title id="swpChartT">Corpus rises to retirement age, then declines</title>
    </svg>
    <div class="legend">
      <span><i class="sw" style="background:var(--blue)"></i>Corpus (nominal)</span>
      <span><i class="sw" style="background:var(--dim)"></i>Same corpus in today&rsquo;s rupees</span>
    </div>
  </div>

  <div class="card rv">
    <div class="cardhead">
      <span class="eyebrow">Year by year</span>
      <div class="swp-toggle">
        <button type="button" data-mode="nominal" class="on">Nominal &#8377;</button>
        <button type="button" data-mode="real">Today&rsquo;s &#8377;</button>
      </div>
    </div>
    <div class="tw">
      <table class="t" id="swpTbl" style="min-width:560px">
        <thead><tr><th scope="col">Age</th><th scope="col">Year</th><th scope="col">In / Out (yr)</th><th scope="col">Closing corpus</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>
    <p class="note" style="margin-top:12px;color:var(--dim);font-size:12px">
      Withdrawals are grossed up so the figure shown is what reaches your bank after capital
      gains tax, using proportional cost-basis depletion. Returns compound monthly off the
      effective annual rate.
      <br><br>
      The model assumes a smooth return every single year &mdash; real markets do not, and a bad
      first five years of retirement destroys a corpus that the average return says is safe.
      Treat the required-corpus number as a floor, not a target.
    </p>
  </div>
  </div><!-- /#swpBody -->
</section>{% endif %}

<!-- ══════════ 03 PORTFOLIO ══════════ -->
{% if 'tracker' in secs %}<section class="sec" id="tracker">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['tracker'] }} / {{ seclabel['tracker'] }}</span>
      <h2 class="stitle">The book</h2>
      <p class="sdesc">Real capital, actually held &mdash; not the paper wallet and not a signal
        list. Risk before profit: each position shows what it can lose to its stop before it shows
        what it has made, because the first number is the one that is certain.</p>
    </div>
    <div style="display:flex;gap:9px;align-items:center;flex-wrap:wrap">
      <form action="/tracker/obsidian" method="post" style="display:inline">
        <button type="submit" class="btn-gh v">Sync Obsidian</button>
      </form>
      <a class="slink" href="/tracker/history" target="_blank">Exit history →</a>
      <button type="button" class="btn-gh" id="posHistBtn" style="display:none">Closed positions</button>
      <button type="button" class="btn-gh" id="keyLogout" style="display:none">Log out</button>
    </div>
  </div>

  <!-- Live book. Filled from /api/tracker; the server-rendered block below is
       the fallback for the static build. -->
  <div id="posLive" style="display:none"></div>

  <div class="keybox" id="keybox">
    <span>Editing the book needs your key. Signed in for 48 hours, on this device only — nothing is stored in the browser.</span>
    <label for="keyInput" class="hp">Edit key</label>
    <input type="password" id="keyInput" placeholder="Edit key" autocomplete="off">
    <button type="button" class="btn btn-sm" id="keySave">Unlock</button>
  </div>

  <div id="posStatic">
  {% if tracker %}
  <div class="tw rv">
    <table class="t" style="min-width:820px">
      <thead><tr>
        <th scope="col">Symbol</th><th scope="col">Entry</th><th scope="col">Current</th><th scope="col">Target</th><th scope="col">Stop</th>
        <th scope="col">P&amp;L</th><th scope="col">Horizon</th><th scope="col">Thesis</th><th scope="col">Added</th><th scope="col"></th>
      </tr></thead>
      <tbody>
        {% for s in tracker %}
        <tr>
          <td><strong class="sym">{{ s.symbol }}</strong></td>
          <td class="num">{{ s.currency }}{{ s.entry_price }}</td>
          <td class="num {{ 'up' if s.winning else 'dn' }}">{{ s.currency }}{{ s.current_price }}</td>
          <td class="num up">{{ s.currency }}{{ s.target_price }}</td>
          <td class="num dn">{{ s.currency }}{{ s.stop_loss }}</td>
          <td class="{{ 'pnl-u' if s.winning else 'pnl-d' }}">{{ '+' if s.winning else '' }}{{ s.pnl_pct }}%</td>
          <td class="mono-dim">{{ s.timeframe }}</td>
          <td style="font-size:12px;color:var(--muted);max-width:220px">{{ s.thesis[:60] }}</td>
          <td class="mono-dim">{{ s.added_date }}</td>
          <td><form action="/tracker/exit/{{ s.id }}" method="post"><button type="submit" class="btn-gh">Exit</button></form></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% else %}
  <div class="empty rv">No open positions. Hit <strong style="color:var(--lime)">+ Track</strong> on any trade idea, or add one below.</div>
  {% endif %}
  </div>

  <div class="formbox rv">
    <h3 class="fh4">+ Add position manually</h3>
    <form action="/tracker/add" method="post" id="posAddForm">
      <div class="frow">
        <input type="text" name="symbol" aria-label="Symbol" placeholder="Symbol e.g. RELIANCE.NS" required>
        <input type="text" name="name" aria-label="Company name" placeholder="Name">
        <input type="number" step="0.01" name="entry_price" aria-label="Entry price" placeholder="Entry price" required>
        <input type="number" step="1" min="1" name="quantity" aria-label="Quantity" placeholder="Quantity" required>
      </div>
      <div class="frow">
        <select name="side" aria-label="Side">
          <option value="LONG" selected>LONG</option>
          <option value="SHORT">SHORT</option>
        </select>
        <select name="trade_type" aria-label="Trade type">
          <option value="SWING" selected>SWING</option>
          <option value="LONG_TERM">LONG_TERM</option>
          <option value="INTRADAY">INTRADAY</option>
          <option value="INVESTMENT">INVESTMENT</option>
        </select>
        <input type="number" step="0.01" name="target_price" aria-label="Target price" placeholder="Target price" required>
        <input type="number" step="0.01" name="stop_loss" aria-label="Stop loss" placeholder="Stop loss">
      </div>
      <div class="frow">
        <input type="text" name="timeframe" aria-label="Timeframe" placeholder="Timeframe" value="2-3 months">
        <input type="text" name="thesis" aria-label="Thesis" placeholder="Why this stock?" style="flex:3">
      </div>
      <p class="fnote">SWING/LONG_TERM auto-run the 20%-at-+30% / 50%-of-remainder-at-+50% profit ladder. INTRADAY and INVESTMENT don't.</p>
      <button type="submit" class="btn">Add to book</button>
    </form>
  </div>
</section>{% endif %}

<!-- ══════════ PAPER WALLET ══════════
     The wallet's own capital, sized mechanically against every signal by tier —
     forward-only from launch (2026-08-17), no fabricated history. Entirely
     JS-rendered from GET /api/signals?wallet=1 (see paperWallet.js in
     static/app.js): the tier percentages, category caps and grade rules
     live in ONE place — _paper_wallet.js on the backend — and this section
     reads them back from the live response rather than hardcoding a second
     copy here that could drift out of sync with the code actually enforcing
     it. No server-rendered fallback: a static 6am snapshot of a forward-only
     wallet would show the same "just started" state live JS already shows
     honestly, so a fallback would add a second code path for no real gain. -->
{% if 'paperwallet' in secs %}<section class="sec" id="paperwallet">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['paperwallet'] }} / {{ seclabel['paperwallet'] }}</span>
      {# The figure is a placeholder that renderPaperWallet() overwrites from the
         live response. It was hardcoded ₹50,00,000 here while _paper_wallet.js
         ran at a crore, so the headline and every number under it disagreed.
         test_page_structure.py asserts this fallback equals CAPITAL. #}
      <h2 class="stitle">Positions held &mdash; <span id="pwCapital">₹1 Cr</span></h2>
    </div>
    {# The other half of the pair. See the note in the order book: the same
       crore appears in both places and they are different moments in its life.
       Named on both ends and linked both ways, so neither reads as a stray
       second wallet. #}
    <p class="sdesc"><b>This is what the book already owns</b>, marked to live prices.
      What it would BUY today &mdash; nothing bought yet, entry and size and exits &mdash;
      is <a href="#picks">the order book &rarr;</a></p>
    <p class="sdesc" style="margin-top:8px">A mechanical capital allocator, not a recommendation — every signal this
      ledger produces from here on gets sized by its horizon and grade, nothing more. Started
      2026-08-17; no history before that date is replayed in.</p>
    <p class="sdesc" style="margin-top:8px">Every position shows its <b>side</b>, stop and both
      targets. <b>Long only as of 2026-08-27</b> — the rulebook refuses to size a short
      (SHORT_NOT_TAKEN) though every one an engine files is still recorded in the signal
      log. Shorts opened before that date are still shown and still graded, and a
      short's stop sits <i>above</i> its entry.
      Winners are booked on a ladder &mdash; <b>half off at T1, the rest at T2</b> &mdash; so a
      row that reached the far target is banked at the blend of the two, not as though the whole
      position ran to T2. Rows booked that way are marked &frac12;, and the ledger's
      full-position figure is on the tooltip.</p>
  </div>

  <div id="paperWalletLive"><div class="empty">Loading the wallet…</div></div>
</section>
{% endif %}

<!-- ══════════ 10 SIGNAL LOG ══════════ -->
{% if 'alerts' in secs %}<section class="sec" id="alerts">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['alerts'] }} / {{ seclabel['alerts'] }}</span> {{ dh('Signal ledger') }}
      <h2 class="stitle">Every signal, scored</h2>
    </div>
    <div style="text-align:right">
      <p class="sdesc">Nothing hidden. Every Telegram alert ever sent, with entry, stop, target and outcome.</p>
      <a class="slink" href="alerts.json" target="_blank" style="display:inline-block;margin-top:10px">↓ alerts.json</a>
    </div>
  </div>

  <!-- Two engines write here on a weekly clock and neither is a swing trade.
       Saying so beside the table is the point: they are IN the log — that is
       the whole idea, they used to be nowhere — and they are OUT of every
       rate the log computes. A reader who sees a 6-month hold in a table
       headed "every signal, scored" is owed both halves of that. -->
  <div class="prov rv">
    <span class="pv-tag">IN THE LOG, OUT OF THE RATES</span>
    <span><b>multibagger</b> &mdash; weekly, Saturday 09:30 IST · 6&ndash;12 month hold off weekly bars</span>
    <span><b>ai_longterm</b> &mdash; weekly, Saturday · 2&ndash;3 year hold on a 200DMA structure stop</span>
    <span><b>magic</b> and <b>magicmagic</b> &mdash; weekly Investtech-style screens,
      logged as <b>WATCH</b> with no stop, no target and no R:R</span>
    <span>All four are excluded from win rate, expectancy and the equity curve. A
      multi-month hold cannot resolve on a swing horizon, and counting it would
      move the only honest number here. The two watch screens carry no levels at
      all, so there is nothing about them that could ever resolve &mdash; they are
      a shortlist that happens to live in the same table, and their SL, target
      and P&amp;L columns are empty because those numbers do not exist, not
      because they are missing.</span>
  </div>

  <!-- Archive strip: one tile per trading day, newest first. Live only. -->
  <div id="archWrap" style="display:none">
    <div class="ctlbar rv" style="margin-bottom:8px">
      <span class="ghost" style="margin-left:0" id="archSpan">ARCHIVE</span>
      <button type="button" class="fbtn on" id="archAll">Show all days</button>
      <span id="archMonths"></span>
    </div>
    <div class="arch rv" id="archStrip"></div>
    <div class="ghost" style="font-family:var(--mono);font-size:11px;color:var(--dim);margin:-4px 0 4px">
      ← scroll sideways for the full history →
    </div>
  </div>

  <div class="ctlbar rv" id="alertCtl" style="display:none">
    <input type="search" id="alertSearch" aria-label="Search the signal log by symbol"
           placeholder="Search symbol — e.g. BAJFINANCE" autocomplete="off">
    <input type="date" id="alertFrom" aria-label="From date">
    <input type="date" id="alertTo" aria-label="To date">
    <select id="alertTfSel" aria-label="Filter the signal log by timeframe"><option value="">All timeframes</option></select>
    <!-- Engine filter. The log is sorted newest-first and now carries four
         engines, so a weekly one whose last scan was ten days ago sits 40 rows
         down and reads as missing — which is exactly how 56 multibagger rows
         looked like they had never been written. Filter by engine and they are
         one click away. -->
    <select id="alertEngSel" aria-label="Filter the signal log by engine"><option value="">All engines</option></select>
    <input type="number" step="0.1" id="alertPnlMin" aria-label="Minimum P&amp;L percent" placeholder="Min P&amp;L %" style="width:90px">
    <input type="number" step="0.1" id="alertPnlMax" aria-label="Maximum P&amp;L percent" placeholder="Max P&amp;L %" style="width:90px">
    <span class="ghost" id="alertCount"></span>
  </div>

  {% if alerts %}
  <div class="kpi-row rv">
    <div class="kpi"><div class="v up" id="kpiWin" data-count="{{ wins }}">{{ wins }}</div><div class="k">Targets Hit</div></div>
    <div class="kpi"><div class="v dn" id="kpiLoss" data-count="{{ losses }}">{{ losses }}</div><div class="k">Stops Hit</div></div>
    <div class="kpi"><div class="v" id="kpiOpen" style="color:var(--blue)" data-count="{{ opens }}">{{ opens }}</div><div class="k">Open</div></div>
    <div class="kpi"><div class="v" id="kpiRate"
         style="color:{{ 'var(--lime)' if closed >= 30 else 'var(--muted)' }}"
         data-count="{{ winrate }}" data-suffix="%">{{ winrate }}%</div><div class="k">Win Rate</div></div>
    <div class="kpi"><div class="v" id="kpiTotal" data-count="{{ alerts|length }}">{{ alerts|length }}</div><div class="k">Total Signals</div></div>
  </div>

  <div class="filters rv">
    <button class="fbtn on" data-f="all">All</button>
    <button class="fbtn" data-f="open">Open</button>
    <button class="fbtn" data-f="win">Target Hit</button>
    <button class="fbtn" data-f="loss">Stop Hit</button>
    <button class="fbtn" data-f="expired">Expired</button>
    <button class="fbtn" data-f="cancelled" title="Withdrawn or never-valid signals — hidden from All by default, never deleted">Cancelled</button>
  </div>

  <!-- Engine generation. ensureAlertTable() returns early when this
       server-rendered table already exists, so these buttons have to live here
       too or the live layer has nothing to bind to. Hidden until the API probe
       confirms a ledger — there is nothing to switch between on a static host. -->
  <!-- The version switch is gone. There is one engine on this page now: the
       gated one. The pre-gate history still exists in the database and is
       still reachable at /api/signals?version=v1 for anyone auditing, but it
       is no longer part of what this site claims as its record — the
       2026-08-08 re-grade showed most of that population was a grading
       artifact, and a toggle that lets a reader pick the flattering number is
       not a ledger. -->
  <div class="filters rv" id="alertVer" style="margin-top:6px;display:none"></div>

  <div class="tw tw-tall rv">
    <table class="t" id="alertTable">
      <thead><tr>
        <th scope="col">Date</th><th scope="col">Symbol</th><th scope="col">Signal</th><th scope="col" title="What this row relates to — which product or engine produced it, and on what horizon">Relates to</th><th scope="col">TF</th><th scope="col">Grade</th><th scope="col">Entry</th><th scope="col">SL</th>
        <th scope="col">T1</th><th scope="col">T2</th><th scope="col">RR</th><th scope="col">B/E WR</th><th scope="col">Last</th><th scope="col">Exit</th><th scope="col">P&amp;L</th><th scope="col">Closed</th><th scope="col">Status</th>
      </tr></thead>
      <tbody>
      {# Only the first rows are server-rendered. The live layer replaces this
         table from /api/signals within a few hundred ms, so the other 180 rows
         were ~120 KB shipped to every visitor on every request and discarded
         before they could be read. This many is enough to fill the fold on a
         tall screen while the ledger loads. #}
      {% for a in alerts[:20] %}
        <tr data-badge="{{ a.badge }}">
          <td class="mono-dim">{{ a.alert_date }}</td>
          <td>{% if a.tv %}<a class="sym" href="https://www.tradingview.com/chart/?symbol={{ a.tv }}"
            target="_blank" rel="noopener">{{ a.symbol }}</a>{% else %}{{ a.symbol }}{% endif %}</td>
          <td class="{{ 'up' if a.action == 'BUY' else 'dn' }}" style="font-weight:600">{{ a.action }}{% if a.signal_type %}<span class="mono-dim" style="font-size:11px"> · {{ a.signal_type }}</span>{% endif %}</td>
          {# signal_type is an engine name; this is what the row MEANS. Same
             column exists in the live renderer in app.js and in the <thead> —
             all three must move together or every cell after this one shifts
             under the wrong heading the moment /api/signals resolves. #}
          <td class="rmk">{{ a.remarks or '—' }}</td>
          <td class="mono-dim">{{ a.timeframe or '—' }}</td>
          <td class="mono-dim">{{ a.grade or '—' }}</td>
          <td class="num">{% if a.entry %}{{ a.currency or "₹" }}{{ "%.2f"|format(a.entry) }}{% else %}—{% endif %}</td>
          <td class="num dn">{% if a.sl %}{{ a.currency or "₹" }}{{ "%.2f"|format(a.sl) }}{% else %}—{% endif %}</td>
          <td class="num up">{% if a.target1 %}{{ a.currency or "₹" }}{{ "%.2f"|format(a.target1) }}{% else %}—{% endif %}</td>
          <td class="num up">{% if a.target2 %}{{ a.currency or "₹" }}{{ "%.2f"|format(a.target2) }}{% else %}—{% endif %}</td>
          <td class="num" style="color:var(--gold)">{{ a.rr or '—' }}{% if a.rr %}x{% endif %}</td>
          {# Break-even win rate, 1/(1+R) — the same fact as R:R, made visible. #}
          <td class="num mono-dim">{% if a.rr %}{{ "%.0f"|format(100.0 / (1.0 + a.rr)) }}%{% else %}—{% endif %}</td>
          {# The 6 AM shell has no live quote — /api/ticker fills this in the
             browser. A placeholder keeps the SSR header and the live rows on
             the same 16 columns; without it the live table shifted one column
             right of a header that never moved. #}
          <td class="num">—</td>
          <td class="num">{% if a.exit_price %}{{ a.currency or "₹" }}{{ "%.2f"|format(a.exit_price) }}{% else %}—{% endif %}</td>
          <td class="{{ 'pnl-u' if (a.pnl_pct or 0) > 0 else ('pnl-d' if (a.pnl_pct or 0) < 0 else 'num') }}">{{ a.pnl_str }}</td>
          <td class="mono-dim">{{ a.close_date }}</td>
          <td><span class="badge badge-{{ a.badge }}">{% if a.badge == 'win' %}✅ Win{% elif a.badge == 'loss' %}❌ Stop{% elif a.badge == 'open' %}🔵 Open{% else %}{{ a.status or '—' }}{% endif %}</span></td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
  {% else %}
  <div class="empty rv">No signals logged yet — alerts appear here after Telegram sends them.</div>
  {% endif %}

  <!-- Total open risk, painted by paintHeat() once the rows are in. -->
  <div class="heat rv" id="heat"></div>
</section>{% endif %}

<!-- Capture. The page previously had none: 16,156 words and no way to keep a
     reader. Placed after proof, not before it. -->
<div class="sub-cta rv" data-src="ledger">
  <div>
    <h3>You just read a losing month in public.</h3>
    <p>Most people showing you signals do not show you the stops. If that is the kind of record you want in your inbox, this is where you say so.</p>
    {# This said "One email a day at 6 AM MYT". Nothing sends it. There is no
       mail path anywhere in this repository — no SMTP, no ESP, no send step in
       any workflow — so every address collected here has been joining a list
       that has never mailed anyone.

       On a page whose entire argument is that it does not overstate, that was
       the worst sentence on it. The copy now says what is actually true: the
       list exists, the daily email does not yet. It goes back to a promise the
       day there is a sender behind it. #}
    <p class="fine">The daily email is not sending yet &mdash; this puts you on the list
      for when it does, and nothing else. No advice, no forwarding, no third party:
      the address sits in my own database until there is something to send.</p>
  </div>
  <div>
    <form class="sub-form" id="subEnd" novalidate>
      <label class="hp" aria-hidden="true">Company
        <input type="text" name="company" tabindex="-1" autocomplete="off"></label>
      <label for="subEnd-e" class="hp">Email address</label>
      <input id="subEnd-e" type="email" name="email" required autocomplete="email"
             inputmode="email" placeholder="you@company.com">
      <button type="submit">Subscribe</button>
    </form>
    <div class="sub-msg" role="status" aria-live="polite"></div>
  </div>
</div>

<!-- ══════════ 11 PERFORMANCE ══════════
     Entirely live. Hidden on a static host, where there is no ledger to
     compute an edge from. -->
{% if 'perf' in secs %}<section class="sec" id="perf" style="display:none">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['perf'] }} / {{ seclabel['perf'] }}</span>
      <h2 class="stitle">Does this actually work?</h2>
    </div>
    {# It said "over the full ledger". It is not: /api/stats scores the window
       the basis line directly below this prints — 34 closed of 68 signals as
       this was written — while the ledger holds many times that. Two claims a
       centimetre apart, and the wrong one was the larger type. The window is
       whatever the basis line says, so this stops naming one. #}
    <p class="sdesc">Win rate, expectancy and drawdown over the window named below — closed signals only.
      Open signals are excluded, because counting them is how a 50% system starts looking like an 80% one.</p>
  </div>

  <div class="ctlbar rv">
    <select id="perfTf" aria-label="Filter performance by timeframe"><option value="">All timeframes</option></select>
    <select id="perfRange" aria-label="Filter performance by date range">
      <option value="">All time</option>
      <option value="30">Last 30 days</option>
      <option value="90">Last 90 days</option>
      <option value="365">Last 12 months</option>
    </select>
    <span class="ghost" id="perfBasis"></span>
  </div>
  {# Where a failed /api/stats says so. Its own node rather than reusing a
     content container, so a failure notice can never overwrite figures that
     did load — and an empty div renders as nothing when all is well. #}
  <div id="perfNotice"></div>

  {# ── WHEN IT WORKS ─────────────────────────────────────────────────────
     Day-of-week and month buckets over the closed ledger. Borrowed in spirit
     from the calendar heatmaps trade journals use, but built on the one thing
     this ledger has that a journal does not: every trade was published when it
     fired, so the buckets cannot be assembled after the fact to flatter a day.

     Rendered by renderWhen() in app.js from /api/stats' equity_curve, which
     already carries a date and an R for every closed trade. No new endpoint,
     no new payload. #}
  <div class="whenwrap rv" id="whenWrap" style="display:none">
    <div class="whenhead">
      <h3 class="whentitle">When it works</h3>
      <p class="whensub">Every closed trade bucketed by the day it resolved. Colour is total R,
        not win rate — a day can win often and still lose money.</p>
    </div>
    <div class="whengrid" id="whenDow"></div>
    <div class="whengrid whenmonths" id="whenMonth"></div>
    <p class="whennote" id="whenNote"></p>
  </div>

  {# ── WHAT IF ───────────────────────────────────────────────────────────
     The simulator trade journals sell as "your equity curve without your
     mistakes". Theirs removes emotion-tagged trades, which is a judgement the
     trader makes after the loss. This one removes an ENGINE — a pre-declared,
     mechanical grouping — and the arithmetic is exact rather than inferred:
     total R and trade count are both known per engine, so excluding one is a
     subtraction, not a re-simulation.

     It is deliberately framed as attribution, not as a better result. Removing
     your worst engine in hindsight is not a strategy; knowing which engine is
     paying for the others is. #}
  <div class="whatifwrap rv" id="whatIfWrap" style="display:none">
    <div class="whenhead">
      <h3 class="whentitle">What if</h3>
      <p class="whensub">Switch an engine off to see what the ledger would read without it.
        This is attribution, not a backtest — the trades still happened, and picking the
        loser to remove after the fact is not a strategy.</p>
    </div>
    <div class="whatifrow" id="whatIfToggles"></div>
    <div class="whatifout" id="whatIfOut"></div>
  </div>

  <!-- Shown by renderStats() while the closed count is below MIN_N_FOR_EDGE.
       The record restarted on the gated engine, so it will be visible for a
       while — that is the honest state of a ledger that has just begun. -->
  <p class="thin-warn rv" id="perfThin" style="display:none"></p>

  <div class="perf-grid rv" id="perfGrid"></div>
  <!-- Drawdown, beside the number that sells the system. Painted by
       paintUnderwater() from the same equity_curve the hero chart uses. -->
  <div class="uw rv" id="perfUw"></div>
  <div class="brk rv" id="perfBrk"></div>
</section>{% endif %}

<!-- ══════════ ENGINE LOG ══════════
     Server-rendered and always visible — no API, no display:none. It has to
     survive the static snapshot, because the whole point is that a reader can
     see what changed in the rules even on a day the ledger is unreachable. -->
{# ── DOES EACH ENGINE EARN ITS PLACE ────────────────────────────────────────
   The wallet sizes by horizon and grade; neither asks whether the engine has
   any measured edge. That question kept being asked of this page and the page
   had no answer on it. Worst first on purpose: leading with the best engine is
   a brochure, and what a reader needs is what is losing money right now. #}
{% if 'perf' in secs and evidence and evidence.engines %}
<div class="sec-append">
  <div class="subhead">
    <span class="subeyebrow">The evidence</span>
    <h3>Which engines have earned their place</h3>
    <p class="subdesc">Every engine, scored on its own closed trades &mdash; worst first.
      {% if evidence.aliased %}<code>{{ evidence.aliased|join('</code>, <code>') }}</code>
      {{ 'is' if evidence.aliased|length == 1 else 'are' }} reported inside
      <code>magic</code>: one screen with two spellings that wrote byte-identical levels on
      ten occasions, so the same idea was being counted twice here and twice again in
      expectancy. The ledger rows stay separate &mdash; rewriting history to tidy a report is
      not on &mdash; and a write-time guard stops new pairs.{% endif %}
      An engine with fewer than {{ evidence.min_n }} closed trades gets no verdict at all:
      below that the standard error is wider than any effect worth acting on, and
      &ldquo;not significant&rdquo; would wrongly imply the sample could have settled it.</p>
  </div>

  {% if evidence.bleeding %}
  <p class="ev-alert"><b>Measured losses:</b>
    {% for e in evidence.bleeding %}<code>{{ e }}</code>{{ ' · ' if not loop.last }}{% endfor %}.
    These are not runs of bad luck &mdash; they clear the significance bar in the wrong
    direction. They stay published because hiding a losing engine is the one thing this
    ledger exists not to do, but they no longer receive capital: the paper wallet sizes them
    at zero and marks the row <b>not funded</b>.
    <br><span class="mono-dim">Suppressing them moves the published headline from
    <b>+0.349R</b> to <b>+0.398R</b>, t 1.80 to 1.96 &mdash; almost nothing. That is the point.
    This is about not funding a loss, not about improving the number; if it were about the
    number it would not be worth doing.</span></p>
  {% endif %}

  {# Decided first, undecided folded away.
     Thirteen rows, ten of them UNPROVEN, is ten rows of "we do not know" —
     technically complete and useless to read. The engines with enough closed
     trades to say something are the table; the rest are one line and a
     disclosure. Nothing is removed, and the count is stated so a reader can see
     exactly how much is being held back. #}
  {% set decided = evidence.engines|rejectattr('verdict', 'equalto', 'UNPROVEN')|list %}
  {% set undecided = evidence.engines|selectattr('verdict', 'equalto', 'UNPROVEN')|list %}
  <div class="tw rv">
    <table class="t"><thead><tr>
      <th scope="col">Engine</th><th scope="col">Closed</th><th scope="col">Open</th>
      <th scope="col">Win rate</th><th scope="col">Expectancy</th><th scope="col">t</th>
      <th scope="col">Total R</th><th scope="col">Verdict</th>
    </tr></thead><tbody>
      {% for e in decided %}
      <tr>
        <td><strong class="sym">{{ e.engine }}</strong>{% if not e.is_trade %}
          <span class="ev-tag" title="A research artefact, not a trade signal — no capital is sized to it.">research</span>{% endif %}</td>
        <td class="num">{{ e.n }}</td>
        <td class="num mono-dim">{{ e.open_now }}</td>
        <td class="num">{{ e.win_rate }}%</td>
        <td class="num {{ 'up' if e.expectancy > 0 else 'dn' if e.expectancy < 0 else '' }}">{{ '%+.3f'|format(e.expectancy) }}R</td>
        <td class="num mono-dim">{{ '%+.2f'|format(e.t) if e.t is not none else '—' }}</td>
        <td class="num {{ 'up' if e.total_r > 0 else 'dn' if e.total_r < 0 else '' }}">{{ '%+.1f'|format(e.total_r) }}R</td>
        <td><span class="ev-v ev-{{ e.verdict|lower }}" title="{{ e.why }}">{{ e.verdict }}</span>{% if e.suppressed %}
          <span class="ev-supp" title="Still fires, still logged, still scored — but receives no capital in the paper wallet. Evidence decides funding.">not funded</span>{% endif %}</td>
      </tr>
      {% endfor %}
    </tbody></table>
  </div>

  {% if undecided %}
  <details class="ev-more">
    <summary><b>{{ undecided|length }} more engines</b> have fewer than {{ evidence.min_n }}
      closed trades &mdash; not enough to judge in either direction. Shown for completeness.</summary>
    <div class="tw" style="margin-top:12px">
      <table class="t"><thead><tr>
        <th scope="col">Engine</th><th scope="col">Closed</th><th scope="col">Open</th>
        <th scope="col">Win rate</th><th scope="col">Expectancy</th><th scope="col">Total R</th>
      </tr></thead><tbody>
        {% for e in undecided %}
        <tr>
          <td><strong class="sym">{{ e.engine }}</strong>{% if not e.is_trade %}
            <span class="ev-tag" title="A research artefact, not a trade signal — no capital is sized to it.">research</span>{% endif %}</td>
          <td class="num">{{ e.n }}</td>
          <td class="num mono-dim">{{ e.open_now }}</td>
          <td class="num">{{ e.win_rate }}%</td>
          <td class="num mono-dim">{{ '%+.3f'|format(e.expectancy) }}R</td>
          <td class="num mono-dim">{{ '%+.1f'|format(e.total_r) }}R</td>
        </tr>
        {% endfor %}
      </tbody></table>
    </div>
  </details>
  {% endif %}

  <p class="subdesc" style="margin-top:12px">Hover any verdict for the reasoning behind it.
    <b>t</b> is the expectancy divided by its own standard error &mdash; roughly, how many
    times larger the result is than the noise around it. Below <b>&plusmn;2</b> a result is not
    distinguishable from chance, however good or bad the headline number looks.</p>
</div>
{% endif %}

{% if 'rules' in secs %}<section class="sec" id="rules">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['rules'] }} / {{ seclabel['rules'] }}</span>
      <h2 class="stitle">What the ledger changed</h2>
    </div>
    <p class="sdesc">Every rule the engine follows because the record forced it — with the number
      that forced it. Including the ideas that were tested and thrown away.</p>
  </div>

  <ol class="elog rv">
    {% for c in engine_changes %}
    <li class="elog-i">
      <div class="elog-m">
        <time class="elog-d">{{ c.date }}</time>
        <span class="elog-t">{{ c.tag }}</span>
        <span class="elog-v {{ c.verdict }}">{{ c.verdict }}</span>
      </div>
      {# Collapsed by default. The log runs to every rule the engine has ever
         adopted or rejected, each with a body paragraph, an evidence table and
         a caveat — fully expanded it buried the section and made the list
         impossible to scan. The RULE stays visible; the reasoning is one click
         away. Native <details> for the same reasons as the fund screen: no
         stacking context, and it still reads fully with JS off. #}
      <details class="elog-b">
        <summary class="elog-sum">
          <h3 class="elog-h">{{ c.title }}</h3>
          <span class="elog-more">Evidence</span>
        </summary>
        <div class="elog-d2">
          <p class="elog-p">{{ c.body }}</p>
          {% if c.evidence %}
          <table class="elog-e">
            <tbody>
            {% for label, val, n, sig in c.evidence %}
              <tr>
                <th scope="row">{{ label }}</th>
                <td class="num">{{ val }}</td>
                <td class="num elog-n">{{ n }}</td>
                <td class="num elog-s">{{ sig }}</td>
              </tr>
            {% endfor %}
            </tbody>
          </table>
          {% endif %}
          {% if c.note %}<p class="elog-c">{{ c.note }}</p>{% endif %}
        </div>
      </details>
    </li>
    {% endfor %}
  </ol>
</section>{% endif %}

<!-- ══════════ DATA HEALTH ══════════
     Every dataset on the site, with the four facts that decide whether to
     trust the section above it: when it last succeeded, when it was last
     attempted, how much of its universe it covers, and what its status is.

     Sits directly under the Engine Log because they answer the same question
     from two ends — the log says what the engine changed and why, this says
     whether today's numbers are actually current. Both are the reason to
     believe anything else on the page.

     Sorted worst-first, never alphabetically. The only reason to open this
     section is to find what is broken, and a list sorted by name buries it.

     Renders from the SAME snapshot every badge above reads, so a section
     badge and this table cannot disagree about the same build. That was the
     whole finding of the 2026-08-18 audit: the site could show a 750-company
     table above a warning that the newest build had priced 50. -->
{% if 'datahealth' in secs and health %}
<section class="sec" id="datahealth">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['datahealth'] }} / {{ seclabel['datahealth'] }}</span>
      {# Lead with the system's state, then the exception — not with the
         exception alone. "1 of 12 datasets are not current" as a headline reads
         as "something is broken" even when the degraded feed is Careers, which
         has nothing to do with a market number on this page. Honest and alarming
         are different things, and the alarming version costs trust it should not.

         The count is still here, still exact, one line down and still linked to
         the full table. Nothing is hidden; the emphasis is corrected. #}
      <h2 class="stitle">
        {% set core_bad = health.degraded_core if health.degraded_core is defined else health.degraded %}
        {% if core_bad %}{{ core_bad }} market dataset{{ 's' if core_bad != 1 }} behind.
        {% elif health.degraded %}Operational.
        {% else %}All {{ health.total }} datasets current.{% endif %}
      </h2>
      {% if health.degraded %}
      <p class="dh-status">
        <span class="dh-dot {{ 'dh-dot-warn' if core_bad else 'dh-dot-ok' }}"></span>
        {{ health.total - health.degraded }} of {{ health.total }} current ·
        {{ health.degraded }} degraded{% if not core_bad %}, none of them market data{% endif %}.
      </p>
      {% endif %}
    </div>
    <p class="sdesc">Nothing on this page is allowed to look more current than the data
      behind it. Every section carries the same badge this table explains, from the same
      build. Machine-readable at
      <a href="/data-health.json" class="lnk">/data-health.json</a>.</p>
  </div>

  <ul class="dh-list rv">
    {% for d in health.datasets %}
    <li class="dh-row{% if d.severity >= 4 %} dead{% elif not d.is_current %} bad{% endif %}">
      <div class="dh-top">
        <span class="dh dh-{{ d.status }}">{{ d.status }}</span>
        <span class="dh-name">{{ d.dataset }}</span>
        <span class="dh-src">{{ d.source }}</span>
      </div>
      <dl class="dh-grid">
        <div><dt>Last successful</dt>
          <dd>{{ d.last_successful_update[:16].replace('T',' ') if d.last_successful_update else '—' }}</dd></div>
        <div><dt>Last attempt</dt>
          <dd>{{ d.last_attempted_update[:16].replace('T',' ') if d.last_attempted_update else '—' }}
            {% if d.attempt_status %}· {{ d.attempt_status }}{% endif %}</dd></div>
        {# AGE, JUDGED AGAINST ITS OWN CADENCE.
           "5 days old" beside a quiet "weekly" reads as broken, and three of
           the twelve datasets are weekly. They were being reported as stale by
           a reader's eye while the row's own status said FRESH — the loudest
           number on the row disagreeing with the verdict two columns along.

           freshness_age_hours and expected_refresh_hours are both already in
           the payload, so the ratio needs no new data. Under 1.0 the dataset
           is inside its cycle and the row says so in the same breath as the
           age. #}
        <div><dt>Age</dt><dd>{{ d.freshness_age }}
          {% if d.freshness_age_hours is not none and d.expected_refresh_hours %}
            {% if d.freshness_age_hours <= d.expected_refresh_hours %}
              <span class="dh-within">within its {{ d.expected_refresh }} cycle</span>
            {% else %}
              <span class="dh-over">past its {{ d.expected_refresh }} cycle</span>
            {% endif %}
          {% endif %}</dd></div>
        <div><dt>Expected</dt><dd>{{ d.expected_refresh }}</dd></div>
        {# Two coverage figures, never merged. The published dataset's size and
           the latest ATTEMPT's size are different numbers about different
           builds, and printing one of them as though it were both is exactly
           how "750 companies" ended up above "only 50 priced". #}
        {% if d.coverage %}<div><dt>Published coverage</dt>
          <dd>{{ d.coverage }} · {{ d.coverage_pct }}%</dd></div>
        {% elif d.record_count is not none %}<div><dt>Records</dt>
          <dd>{{ d.record_count }}</dd></div>{% endif %}
        {% if d.attempt_coverage %}<div><dt>Latest attempt coverage</dt>
          <dd>{{ d.attempt_coverage }}</dd></div>{% endif %}
        {% if d.fallback_used %}<div><dt>Serving</dt><dd>previous build</dd></div>{% endif %}
      </dl>
      {% if d.notes %}<p class="dh-note">{{ d.notes | join(' · ') }}</p>{% endif %}
    </li>
    {% endfor %}
  </ul>

  {# The six badge definitions used to be spelled out here, in a paragraph at
     the foot of the last section — which is the one place nobody is looking
     when they hit a STALE badge four thousand pixels higher up. They are in
     How to Read This with every other definition now. #}
  <p class="sdesc" style="margin-top:22px;max-width:70ch">
    A failed rebuild never overwrites the last dataset that passed validation.
    <a href="#method">What each badge means &rarr;</a>
  </p>
</section>
{% endif %}

<!-- ══════════ WHO ══════════
     Wording lifted from askakshay.com so the two sites say the same thing.
     Sits between the world and the trade ideas: a reader who arrived from a
     Telegram link should know whose ledger they are reading before they read
     the numbers. -->
{% if 'buildlog' in secs and buildlog %}
{# ── BUILD LOG ───────────────────────────────────────────────────────────
   Generated from git history, not hand-kept. A curated changelog is a second
   place to remember to write and the first to be abandoned; this cannot
   quietly omit the week nothing shipped, and it cannot describe work that was
   never committed. If it looks thin, the month was thin.

   Only feat/fix/perf/refactor commits appear. The daily bot's chore: and
   data: commits are hundreds of signal and newspaper writes, none of which is
   a change to the product. #}
<section class="sec" id="buildlog">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['buildlog'] }} / {{ seclabel['buildlog'] }}</span>
      <h2 class="stitle">What shipped, and when</h2>
    </div>
    <p class="sdesc">Read straight from this repository&rsquo;s history. The Engine Log
      records what the <em>ledger</em> forced the rules to change; this records what the
      <em>site</em> shipped. Nothing is curated into it, so a quiet month looks like a
      quiet month.</p>
  </div>
  <div class="rows-log rv">
    {% for c in buildlog[:24] %}
    <div class="logrow">
      <div class="logmeta">
        <span class="logdate">{{ c.date }}</span>
        <span class="pill {{ 'pill-result' if c.kind == 'Shipped' else 'pill-model' }}">{{ c.kind }}</span>
        <code class="logsha">{{ c.sha }}</code>
      </div>
      <div class="logbody">
        <b>{{ c.title }}</b>
        {% if c.why %}<span>{{ c.why }}</span>{% endif %}
      </div>
    </div>
    {% endfor %}
  </div>
  <p class="lv-sys rv" style="margin-top:14px">
    {{ buildlog|length }} product changes in history &middot; chore and data commits excluded
  </p>
</section>
{% endif %}

<!-- ══════════ HOW TO READ THIS ══════════
     The consolidation. Every number on the page defined once, with the tier
     that says where it came from, generated from the METRICS list at the top
     of this file rather than typed here — so a metric added to the page and
     not defined shows up as a gap in one obvious place instead of nowhere.

     What was NOT done: the per-section explanations were left where they are.
     Moving them all here would have produced a reference nobody reads and
     seventeen sections of bare tables, which is worse than the duplication it
     removes. This is the canonical copy; the ledes stay as the local one. -->
{% if 'method' in secs %}<section class="sec" id="method">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['method'] }} / {{ seclabel['method'] }}</span>
      <h2 class="stitle">Every number, defined once</h2>
    </div>
    <p class="sdesc">What each figure on this page measures, how it is computed, and which
      of the four kinds of claim it is. If a number here disagrees with a number above,
      this page is the one that is wrong &mdash; report it.</p>
  </div>

  <div class="prov-legend rv" role="note" aria-label="The four kinds of claim">
    <span class="pl-lead">The four labels</span>
    {% for k, (nm, desc) in prov_tiers.items() %}
    <span class="pl-item"><span class="pill pill-{{ k }}">{{ nm }}</span> {{ desc }}</span>
    {% endfor %}
  </div>

  {# Grouped by tier rather than alphabetically: the tier is the thing a reader
     is being taught, and a list sorted by name teaches nothing. #}
  {% for tier_key, (tier_name, tier_desc) in prov_tiers.items() %}
  {% set rows = metrics | selectattr('tier', 'equalto', tier_key) | list %}
  {% if rows %}
  <div class="subhead rv" style="margin-top:clamp(26px,3vw,40px)">
    <h3><span class="pill pill-{{ tier_key }}">{{ tier_name }}</span>
      {{ rows|length }} figure{{ '' if rows|length == 1 else 's' }}</h3>
    <p class="subdesc">{{ tier_desc|capitalize }}.</p>
  </div>
  <dl class="metdefs rv">
    {% for m in rows %}
    <div class="metdef" id="metric-{{ m.key }}">
      <dt>{{ m.label }}</dt>
      <dd>
        <p class="md-what">{{ m.what }}</p>
        <p class="md-how">{{ m.how }}</p>
      </dd>
    </div>
    {% endfor %}
  </dl>
  {% endif %}
  {% endfor %}

  <div class="subhead rv" style="margin-top:clamp(26px,3vw,40px)">
    <h3>How fresh is fresh</h3>
    <p class="subdesc">Every dataset carries one of six badges. They describe the BUILD,
      not the market: a weekly screen is stale by Thursday and still perfectly usable.</p>
  </div>
  <dl class="metdefs rv">
    {% for name, meaning in freshness %}
    <div class="metdef" id="fresh-{{ name|lower }}">
      <dt><span class="dh dh-{{ name }}">{{ name }}</span></dt>
      <dd><p class="md-what">{{ meaning }}</p></dd>
    </div>
    {% endfor %}
  </dl>
  <p class="lv-3 rv" style="margin-top:14px">
    A failed rebuild never overwrites the last dataset that passed validation, and a
    partial rebuild is never published as a complete one. When a newer attempt fails the
    section keeps serving the last good build and says so &mdash; it does not go blank,
    and it does not pretend the failure did not happen.
  </p>

  <p class="lv-sys rv" style="margin-top:clamp(26px,3vw,40px)">
    Nothing on this page is investment advice. The ledger publishes losses at the same
    size as wins because a record you can only see the good half of is not a record.
    Definitions are generated from the same list the page stamps its badges from, so a
    badge and its definition cannot disagree.
  </p>
</section>
{% endif %}

{% if 'who' in secs %}<section class="sec" id="who">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['who'] }} / {{ seclabel['who'] }}</span>
      <h2 class="stitle">Who is publishing this</h2>
    </div>
    <p class="sdesc">Most AI builders lack domain knowledge. Most finance operators
      can&rsquo;t build. Both, here.</p>
  </div>

  <div class="who rv">
    <div class="who-m">
      <div class="who-name">Akshay K Kothari</div>
      <div class="who-role">Chartered Accountant &middot; FP&amp;A &middot; AI builder</div>
      <p>CA with 10 years in corporate finance. $100M+ P&amp;L managed. I build AI tools
        finance teams actually pay for &mdash; and write the essays, models and calculators
        behind them.</p>
      <p class="who-sub">This page is one of those tools. Every signal below is logged the
        moment it fires, scored when it closes, and left on the record either way.
        Wins and losses both. Nothing hidden.</p>
      <div class="who-links">
        <a href="https://askakshay.com" target="_blank" rel="noopener">askakshay.com &nearr;</a>
        <a href="https://www.linkedin.com/in/akkothari" target="_blank" rel="noopener">LinkedIn &nearr;</a>
      </div>
    </div>
  </div>
</section>{% endif %}

<!-- ══════════ 05 INTERVIEW PREP ══════════ -->
{# ══════════ FINANCE CAREERS ══════════
   Renders docs/jobs.json, written by jobs.yml on its own clock. Presentation
   only: every score, tier, freshness label and apply URL is printed verbatim
   from the file. Nothing is recomputed here and nothing is invented — an
   absent salary prints "Not disclosed", an unproven link prints "Unverified".
   generate.load_careers does the grouping; empty_sections drops the nav entry
   when there is nothing renderable. #}
{% if 'careers' in secs %}<section class="sec" id="careers">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['careers'] }} / {{ seclabel['careers'] }}</span> {{ dh('Careers feed') }}
      <h2 class="stitle">Where the next role is</h2>
    </div>
    <p class="sdesc">Senior finance openings in Dubai, Saudi, Malaysia and Oman, scored against
      the actual CV &mdash; multi-country retail P&amp;L, IFRS/MPERS consolidation, D365, Board
      reporting. Ranked by whether it is worth applying to, not by how recently it was posted.</p>
  </div>

  <div class="prov rv">
    <span class="pv-tag">DAILY</span>
    {% if careers.generated_at %}<span>Last verified <b>{{ careers.generated_at[:16]|replace('T',' ') }} UTC</b></span>{% endif %}
    {% if careers.next_refresh %}<span>Next refresh <b>{{ careers.next_refresh[:16]|replace('T',' ') }} UTC</b></span>{% endif %}
    <span><b>{{ careers.stats.get('sources_ok', 0) }}</b> of
      <b>{{ careers.stats.get('sources_attempted', 0) }}</b> sources responded</span>
  </div>

  {# Market snapshot — every figure counted from the rendered list, never
     copied from the file's own totals, which include excluded rows. #}
  {% set c = careers.counts %}
  <div class="jsnap rv">
    <div class="jsnap-i"><b>{{ c.total }}</b><span>roles worth seeing</span></div>
    <div class="jsnap-i"><b>{{ c.s_tier + c.a_tier }}</b><span>high fit (S/A)</span></div>
    <div class="jsnap-i"><b>{{ c.new }}</b><span>new this week</span></div>
    <div class="jsnap-i"><b>{{ c.direct }}</b><span>verified direct apply</span></div>
    {% for country in careers.countries %}
    <div class="jsnap-i"><b>{{ c.by_country[country] }}</b><span>{{ country }}</span></div>
    {% endfor %}
  </div>

  {# The failed-source roll call used to print here — ten scraper names and
     their error states, in the reader's face, every day. Removed 2026-08-18:
     it is operational detail, not a job. The information is NOT lost — source
     coverage is now a Data Health dataset ("Careers feed", 11/21 -> DEGRADED),
     which is where a reader goes to ask whether to trust a section rather than
     having the answer pushed at them mid-scan. #}

  {# ── filters. Operate on the server-rendered cards; with JS off every card
       simply stays visible, which is the correct degraded state. ── #}
  <div class="jfilters rv" id="jFilters">
    <div class="jf-grp" role="group" aria-label="Filter by location">
      <button type="button" class="fbtn on" data-jf="loc" data-v="">All</button>
      {% for country in careers.countries %}
      <button type="button" class="fbtn" data-jf="loc" data-v="{{ country }}">{{ country }}</button>
      {% endfor %}
    </div>
    <div class="jf-grp" role="group" aria-label="Filter by tier">
      <button type="button" class="fbtn on" data-jf="tier" data-v="">Any tier</button>
      <button type="button" class="fbtn" data-jf="tier" data-v="S">S only</button>
      <button type="button" class="fbtn" data-jf="tier" data-v="SA">S + A</button>
    </div>
    <div class="jf-grp" role="group" aria-label="Filter by freshness">
      <button type="button" class="fbtn on" data-jf="fresh" data-v="">Any age</button>
      <button type="button" class="fbtn" data-jf="fresh" data-v="NEW">New</button>
      <button type="button" class="fbtn" data-jf="fresh" data-v="OPEN">Hide stale</button>
    </div>
    <span class="jf-count" id="jCount"></span>
  </div>

  {% macro jobcard(j, rank) %}
  <article class="jcard{{ ' jcard-top' if j.tier == 'S' }}"
           data-country="{{ j.country or '' }}" data-tier="{{ j.tier }}"
           data-status="{{ j.status }}" data-score="{{ j.opportunity_score }}">
    <div class="jc-h">
      <div class="jc-id">
        {% if rank %}<span class="jc-rank">{{ '%02d'|format(rank) }}</span>{% endif %}
        <div>
          <h3 class="jc-t">{{ j.title }}</h3>
          <div class="jc-co">{{ j.company }}
            <span class="jc-loc">&middot; {{ j.location or j.country or 'Location not stated' }}{% if j.location and j.country %}, {{ j.country }}{% endif %}</span>
          </div>
        </div>
      </div>
      <div class="jc-tier jc-tier-{{ j.tier|lower }}" title="Opportunity score {{ j.opportunity_score }}/100">
        <b>{{ j.tier }}</b><span>{{ j.opportunity_score }}</span>
      </div>
    </div>

    <div class="jc-meta">
      <span class="jbadge jb-{{ j.status|lower }}">{{ j.status }}</span>
      {% if j.posted_date %}<span class="jm">Posted {{ j.posted_date }}</span>
      {% else %}<span class="jm dimmed">Posting date not published</span>{% endif %}
      <span class="jm">Fit <b>{{ j.candidate_fit_score }}</b></span>
      <span class="jm">Employer <b>{{ j.employer_score }}</b></span>
      {% if j.experience_min %}<span class="jm">{{ j.experience_min }}+ yrs</span>{% endif %}
      {% if j.salary_min and j.salary_currency %}
        <span class="jm">{{ j.salary_currency }} {{ j.salary_min }}{% if j.salary_max %}&ndash;{{ j.salary_max }}{% endif %}</span>
      {% else %}<span class="jm dimmed">Salary not disclosed</span>{% endif %}
    </div>

    {% if j.why_fit %}
    <ul class="jc-why">
      {% for w in j.why_fit[:3] %}<li>{{ w }}</li>{% endfor %}
    </ul>
    {% endif %}
    {% if j.watch_out %}
    <ul class="jc-warn">
      {% for w in j.watch_out[:2] %}<li>{{ w }}</li>{% endfor %}
    </ul>
    {% endif %}

    <div class="jc-f">
      {% if j.application_url and j.application_url_verified %}
        <a class="jc-apply" href="{{ j.application_url }}" target="_blank" rel="noopener">Apply direct</a>
      {% elif j.application_url %}
        <a class="jc-apply jc-apply-unv" href="{{ j.application_url }}" target="_blank" rel="noopener"
           title="This link was not confirmed to resolve on the last check">Apply &mdash; unverified</a>
      {% else %}
        <span class="jc-apply jc-apply-none">No application link found</span>
      {% endif %}
      {% if j.source_url and j.source_url != j.application_url %}
        <a class="jc-view" href="{{ j.source_url }}" target="_blank" rel="noopener">View posting</a>
      {% endif %}
      <span class="jc-src" title="Source confidence: {{ j.source_confidence }}">
        {% if j.sources and j.sources|length > 1 %}{{ j.sources|length }} sources{% else %}{{ j.source }}{% endif %}
      </span>
    </div>
  </article>
  {% endmacro %}

  <h3 class="jsub rv">Top opportunities</h3>
  <p class="jsub-n rv">Ranked on fit, employer quality and how reachable the application is &mdash;
    not on recency. Everything below carries a verified direct application link unless it says otherwise.</p>
  <div class="jgrid rv" id="jTop">
    {% for j in careers.top %}{{ jobcard(j, loop.index) }}{% endfor %}
  </div>

  {# Compared on id, not on the dict itself: `j not in careers.top` makes Jinja
     deep-compare every field of every row against ten others. #}
  {% set top_ids = careers.top | map(attribute='id') | list %}
  {% set rest = careers.target | rejectattr('id', 'in', top_ids) | list %}
  {% if rest %}
  <h3 class="jsub rv">Everything else in {{ careers.countries|join(', ') }}</h3>
  <div class="jgrid rv" id="jRest">
    {% for j in rest %}{{ jobcard(j, 0) }}{% endfor %}
  </div>
  {% endif %}

  {% set other_rest = careers.other | rejectattr('id', 'in', top_ids) | list %}
  {% if other_rest %}
  <h3 class="jsub rv">Outside the target markets</h3>
  <p class="jsub-n rv">Same employers, different countries. Kept separate rather than ranked
    against Dubai &mdash; these are not what the search is for, but they are real openings at
    groups worth knowing. Every one is rendered rather than truncated, so the count above
    and the list below cannot disagree.</p>
  <div class="jgrid rv jgrid-quiet">
    {% for j in other_rest %}{{ jobcard(j, 0) }}{% endfor %}
  </div>
  {% endif %}

  <p class="note rv" style="margin-top:16px;color:var(--dim);font-size:12px">
    Scores are a reading of the posted description against the CV, not a prediction of
    whether an application succeeds. Freshness comes from the employer's own posted date
    where they publish one; where they do not, the card says so rather than guessing.
    Nothing here is invented &mdash; an unknown salary is blank, an unproven link is
    labelled unverified.
  </p>
</section>{% endif %}

{% if 'interview' in secs %}<section class="sec" id="interview">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['interview'] }} / {{ seclabel['interview'] }}</span>
      <h2 class="stitle">CFO in three years</h2>
    </div>
    <p class="sdesc">Four questions a day — two technical, two not — plus two field notes.
      Weighted to retail, the Gulf, and the controller-to-CFO jump. The non-technical ones
      decide the offer more often than the technical ones do; the field notes are the things
      nobody asks you and everybody assumes you already know.</p>
  </div>

  <div class="lrn-head rv"><span class="lrn-kicker">◆ Technical</span></div>
  <div class="qa-grid rv">
    {% for q in interview_tech %}
    <details class="qa">
      <summary><span class="qa-q">{{ q.q }}</span><span class="qa-who">{{ q.who }}</span></summary>
      <div class="qa-a">{{ q.a }}</div>
    </details>
    {% endfor %}
  </div>

  <div class="lrn-head rv"><span class="lrn-kicker">◆ Everything else</span></div>
  <div class="qa-grid rv">
    {% for q in interview_soft %}
    <details class="qa">
      <summary><span class="qa-q">{{ q.q }}</span><span class="qa-who">{{ q.who }}</span></summary>
      <div class="qa-a">{{ q.a }}</div>
    </details>
    {% endfor %}
  </div>

  {# Not questions. The things nobody asks in an interview and everybody
     assumes you already know once you are in the chair — the working-capital
     arithmetic, the lease standard, what actually decides the offer. Same
     <details> shape as the two banks above so the section reads as one thing
     and still works with JS off. #}
  {% if cfo_field %}
  <div class="lrn-head rv"><span class="lrn-kicker">◆ From the field</span></div>
  <div class="qa-grid rv">
    {% for q in cfo_field %}
    <details class="qa">
      <summary><span class="qa-q">{{ q.q }}</span><span class="qa-who">{{ q.who }}</span></summary>
      <div class="qa-a">{{ q.a }}</div>
    </details>
    {% endfor %}
  </div>
  {% endif %}
</section>{% endif %}

<!-- ══════════ SMART READS ══════════
     The wire tells you what happened; these argue about what it means. Same
     named mastheads, but the analysis and money desks rather than the market
     report, and a card only ships when the publisher gave it a real summary —
     a headline with a border round it is a link, not a read.

     Filtered harder than the news feed (two distinct finance terms, not one).
     Opinion desks run film and language columns beside the money writing, and
     one incidental word is how a review of The Odyssey reached a finance
     page during the build of this section. -->
{# ══════════ DAILY INTELLIGENCE BRIEF ══════════
   The wire compressed into EVENTS. Sits directly above Smart Reads: what
   happened, then the longer reading. Every number and name in the generated
   prose has been checked against the source articles by brief_engine.qa_reject
   before it reaches here; anything that failed fell back to a summary built
   from the headlines themselves and is marked as such. #}
{% if 'brief' in secs %}<section class="sec" id="brief">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['brief'] }} / {{ seclabel['brief'] }}</span> {{ dh('Daily Brief') }}
      <h2 class="stitle">Everything important today</h2>
    </div>
    <p class="sdesc">{{ brief.stats.articles }} articles from
      {{ brief.stats.sources }} wires, clustered into {{ brief.stats.events }} events and
      ranked by what actually matters &mdash; not by how many outlets syndicated it.
      Sources kept on every one.</p>
  </div>

  <div class="prov{{ ' stale' if brief.get('is_fallback') else '' }} rv">
    <span class="pv-tag">DAILY</span>
    <span>~{{ brief.stats.read_minutes }} min read</span>
    {% if brief.get('built_on') %}<span>Built <b>{{ brief.built_on }}</b>
      {%- if brief.get('age_days') is not none and brief.age_days >= 1 %} &middot; {{ brief.age_days }}d old{% endif %}</span>{% endif %}
    {% if brief.get('is_fallback') %}
      <span class="pv-warn">Showing the last edition &mdash; today's has not been built yet.</span>
    {% endif %}
    <span>{{ brief.stats.ai_written }} written up
      {%- if brief.stats.ai_rejected %}, {{ brief.stats.ai_rejected }} rejected by the fact check{% endif %}</span>
  </div>

  {% macro ev_card(e, top) %}
  <article class="ev{{ ' ev-top' if top }}">
    <div class="ev-h">
      <span class="ev-cat">{{ e.category }}</span>
      <span class="ev-dots" title="Importance {{ e.importance }} of 5">
        {%- for i in range(1,6) %}<i class="{{ 'on' if i <= e.importance }}"></i>{% endfor -%}
      </span>
    </div>
    <h3 class="ev-t">{{ e.headline }}</h3>
    <div class="ev-m">
      <span>{{ e.source_count }} source{{ '' if e.source_count == 1 else 's' }}</span>
      <span>&middot;</span><span>{{ e.confidence }} confidence</span>
      {% if not e.ai_generated %}<span>&middot;</span><span class="ev-raw"
        title="No model wrote this. The headline is the highest-tier outlet's own and the bullets are the other outlets' headlines.">from headlines</span>{% endif %}
    </div>
    <ul class="ev-b">{% for b in e.bullets %}<li>{{ b }}</li>{% endfor %}</ul>
    {% if e.whyItMatters %}
    <div class="ev-why"><span>Why it matters</span><p>{{ e.whyItMatters }}</p></div>
    {% endif %}
    {% if e.marketImpact %}
    <div class="ev-mi">
      {% for m in e.marketImpact %}<span class="ev-chip ev-{{ m.direction|lower }}">{{ m.asset }} &middot; {{ m.direction }}</span>{% endfor %}
    </div>
    {% endif %}
    {% if e.watchNext %}<div class="ev-w">Watch next: {{ e.watchNext }}</div>{% endif %}
    <div class="ev-s">
      {% for s in e.sources %}<a href="{{ s.url }}" target="_blank" rel="noopener">{{ s.name }}</a>{% endfor %}
    </div>
  </article>
  {% endmacro %}

  <h3 class="jsub rv">Top stories</h3>
  <div class="ev-grid rv">
    {% for e in brief.top %}{{ ev_card(e, true) }}{% endfor %}
  </div>

  {# Everything past the top five, collapsed. The whole point is a ten-minute
     read; the rest is there for the day you want it. #}
  {% set rest = brief.events[brief.top|length:] %}
  {% if rest %}
  <details class="fund-note-d rv">
    <summary>The rest of the day &mdash; {{ rest|length }} more</summary>
    <div class="ev-grid" style="margin-top:14px">
      {% for e in rest %}{{ ev_card(e, false) }}{% endfor %}
    </div>
  </details>
  {% endif %}

  <p class="note rv" style="margin-top:14px;color:var(--dim);font-size:12px">
    Summaries are written from the linked reporting and nothing else &mdash; every figure and
    name is checked against the source articles before publishing, and an event that fails
    that check falls back to the outlets' own headlines rather than to invented copy.
    Read the originals for the full story.
  </p>
</section>{% endif %}

{% if 'smartreads' in secs %}<section class="sec" id="smartreads">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['smartreads'] }} / {{ seclabel['smartreads'] }}</span> {{ dh('Smart Reads') }}
      <h2 class="stitle">Worth the ten minutes</h2>
    </div>
    <!-- Copy rewritten when this stopped being a finance-only section. It used
         to name five money mastheads, which was accurate then and would have
         been quietly wrong the moment the other four categories landed. -->
    <p class="sdesc">Analysis, not headlines, and deliberately not all about money.</p>
  </div>

  <div class="sr-grid">
    {% for r in smart_reads %}
    <article class="sr rv" style="--d:{{ loop.index0 * 0.04 }}s">
      <div class="sr-h">
        <span class="sr-src">{{ r.source }}</span>
        <!-- The category, not a constant "SMART READS" label. The point of the
             mix is that a reader can see at a glance it is not nine money
             pieces, and a tag that says the same thing on every card cannot
             show that. -->
        <span class="sr-tag sr-{{ r.cat or 'money' }}">{{
          {'money':'MONEY','habits':'HABITS','health':'HEALTH',
           'mind':'MIND','ideas':'IDEAS'}.get(r.cat, 'READ') }}</span>
        {% if r.date %}<span class="sr-date">{{ r.date }}</span>{% endif %}
      </div>
      <h3 class="sr-t">
        {%- if r.link %}<a href="{{ r.link }}" target="_blank" rel="noopener">{{ r.title }}</a>
        {%- else %}{{ r.title }}{% endif -%}
      </h3>
      <p class="sr-s">{{ r.summary }}</p>

      {# Present only on reads that passed BOTH gates — the shared truthfulness
         gate in brief_engine (no number or name absent from the source) and
         the recommendation gate in smart_reads.py. A read that failed either
         renders exactly as it always did, so a rejection costs its prose and
         nothing else. #}
      {% if r.smart %}
      <dl class="sr-x">
        <dt>What happened &middot; from the article</dt>
        <dd class="sr-fact"><ul style="padding-left:16px;margin:0">
          {% for b in r.smart.what_happened %}<li>{{ b }}</li>{% endfor %}
        </ul></dd>
        {% if r.smart.why_it_matters %}
        <div class="sr-why">
          <dt>Why it matters &middot; interpretation</dt>
          <dd class="sr-interp">{{ r.smart.why_it_matters }}</dd>
        </div>
        {% endif %}
        {% if r.smart.what_to_watch %}
        <dt>What to watch &middot; interpretation</dt>
        <dd class="sr-interp">{{ r.smart.what_to_watch }}</dd>
        {% endif %}
      </dl>
      <p class="sr-read">{{ r.smart.read_seconds }} sec read &middot; no recommendation, by design</p>
      {% endif %}

      {% if r.link %}<a class="readmore" href="{{ r.link }}" target="_blank" rel="noopener">Read more &rarr;</a>{% endif %}
    </article>
    {% endfor %}
  </div>
</section>{% endif %}

<!-- ══════════ BOOK ══════════
     One chapter a day out of 48, and — for the 29 books that have one — the
     whole book underneath it: the crux in a dozen points, what actually
     changes in your head, worked examples, and how it lands in this life.

     This was a TAB inside The Desk, fifth of seven, which meant the deepest
     writing on the site was two clicks from being seen and never was. Nothing
     about the content changed on 2026-08-19; it was simply given its own
     section, because a book summary buried behind a tab strip is a book
     summary nobody reads. -->
{% if 'book' in secs and book %}
<section class="sec" id="book">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['book'] }} / {{ seclabel['book'] }}</span>
      <h2 class="stitle">{{ book.book }}</h2>
    </div>
    <p class="sdesc">{{ book.author }} &middot; chapter {{ book.index }} of {{ book.total }}.
      One chapter a day, and where the book has been read properly, the whole thing
      underneath it &mdash; the argument in a dozen points, what changes in your head
      after reading it, and how it lands on an FP&amp;A desk rather than in general.</p>
  </div>

  <div class="essay rv" style="--ac:var(--violet)">

        <div class="meta">{{ book.book }} · {{ book.author }} · {{ book.index }}/{{ book.total }}</div>
        <h3>{{ book.chapter }}</h3>
        <p>{{ book.lesson }}</p>
        <div class="q">{{ book.key_quote }}</div>
        <div class="act"><b>Today's action</b>{{ book.action }}</div>

        {% if book.crux %}
        <div class="bookdeep">
          <div class="bdhead">The whole book · {{ book.crux|length }} points</div>
          <ol class="crux">
            {% for c in book.crux %}<li>{{ c }}</li>{% endfor %}
          </ol>
        </div>
        {% endif %}

        {% if book.learnings %}
        <div class="bookdeep">
          <div class="bdhead">What actually changes in your head</div>
          <ul class="bdlist">
            {% for l in book.learnings %}<li>{{ l }}</li>{% endfor %}
          </ul>
        </div>
        {% endif %}

        {% if book.examples %}
        <div class="bookdeep">
          <div class="bdhead">Examples</div>
          <ul class="bdlist eg">
            {% for e in book.examples %}<li>{{ e }}</li>{% endfor %}
          </ul>
        </div>
        {% endif %}

        {% if book.adapt %}
        <div class="bookdeep adapt">
          <div class="bdhead">How to adapt it into your life</div>
          <ul class="bdlist">
            {% for a in book.adapt %}<li>{{ a }}</li>{% endfor %}
          </ul>
        </div>
        {% endif %}
        </div>
</section>
{% endif %}

<!-- ══════════ MUSIC ══════════
     Three crates. Two are yours (edit music.py to add a line; the 6 AM build
     picks it up), the third is a fixed all-time canon. Five show, the rest are
     one click away — a shelf you can see the whole of is a shelf you stop
     scanning. The five on top rotate daily, so the shelf reads differently
     every morning without the list changing. -->
{% if 'podcasts' in secs %}<section class="sec" id="podcasts">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['podcasts'] }} / {{ seclabel['podcasts'] }}</span> {{ dh('Podcasts') }}
      <h2 class="stitle">What&rsquo;s worth listening to</h2>
    </div>
    <p class="sdesc">Long-form Indian podcasts &mdash; business, money, society, health, psychology and culture. Thirty-four channels, newest first.</p>
  </div>

  <div class="prov{{ ' stale' if podcasts.is_fallback else '' }} rv">
    <span class="pv-tag">DAILY</span>
    <span>{{ podcasts.episodes|length }} episodes from {{ podcasts.shows }} shows</span>
    {% if podcasts.built_on %}<span>Built <b>{{ podcasts.built_on }}</b>{% endif %}
      {%- if podcasts.age_days is not none %} · {{ podcasts.age_days }}d old{% endif %}</span>
    {% if podcasts.is_fallback %}<span>&#9888; Today&rsquo;s refresh has not run &mdash;
      showing the most recent list.</span>{% endif %}
  </div>

  <div class="pod-grid">
    {% for e in podcasts.episodes %}
    <article class="pod rv" style="--d:{{ loop.index0 * 0.05 }}s">
      <div class="pod-h">
        <span class="pod-cat">{{ e.category }}</span>
        <span class="pod-date">{{ e.published }}</span>
      </div>
      <h3 class="pod-t">
        {%- if e.link %}<a href="{{ e.link }}" target="_blank" rel="noopener">{{ e.title }}</a>
        {%- else %}{{ e.title }}{% endif -%}
      </h3>
      <div class="pod-s">{{ e.show }}{% if e.guest %} &middot; <b>{{ e.guest }}</b>{% endif %}</div>
      {% if e.takeaways %}
      <ul class="pod-k">
        {% for t in e.takeaways %}<li>{{ t }}</li>{% endfor %}
      </ul>
      {% endif %}
    </article>
    {% endfor %}
  </div>
  <p class="pod-note">Takeaways are compressed from each episode&rsquo;s own published
    description &mdash; they are the show&rsquo;s claims about itself, not a review, and
    not a summary of anything said in the audio.</p>
</section>{% endif %}

<!-- ══════════ 06 LANGUAGE ══════════ -->
{% if 'language' in secs %}<section class="sec" id="language">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['language'] }} / {{ seclabel['language'] }}</span>
      <h2 class="stitle">Two tongues, sharper</h2>
    </div>
    <p class="sdesc">Spanish from zero, and English that survives a board room. Two words each,
      one delivery drill. Say them out loud — reading them does nothing.</p>
  </div>

  <div class="lrn-head rv"><span class="lrn-kicker">🇪🇸 Español</span></div>
  <div class="two rv">
    {% for w in spanish %}
    <div class="lrn-card">
      <div class="lrn-tag">{{ w.tag }}</div>
      <div class="lrn-word">{{ w.word }}</div>
      <div class="lrn-mean">{{ w.meaning }}</div>
      <div class="lrn-ex"><span class="es">{{ w.es }}</span><span class="en">{{ w.en }}</span></div>
    </div>
    {% endfor %}
  </div>

  <div class="lrn-head rv"><span class="lrn-kicker">◆ Vocabulary</span></div>
  <div class="two rv">
    {% for v in vocab %}
    <div class="lrn-card">
      <div class="lrn-tag">{{ v.say }}</div>
      <div class="lrn-word">{{ v.word }}</div>
      <div class="lrn-mean">{{ v.meaning }}</div>
      <div class="lrn-ex"><span class="es">{{ v.example }}</span><span class="en">{{ v.note }}</span></div>
    </div>
    {% endfor %}
  </div>

  {% if speaking %}
  <div class="lrn-head rv"><span class="lrn-kicker">◆ Speaking drill</span></div>
  <div class="drill rv">
    <div class="drill-t">{{ speaking.title }}</div>
    <div class="drill-d">{{ speaking.drill }}</div>
    <div class="drill-w">{{ speaking.why }}</div>
  </div>
  {% endif %}
</section>{% endif %}

<!-- ══════════ 07 FATHERHOOD ══════════ -->
{% if 'father' in secs %}<section class="sec" id="father">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['father'] }} / {{ seclabel['father'] }}</span>
      <h2 class="stitle">{{ daughter.heading }}</h2>
    </div>
    <p class="sdesc">Two things to actually do today, and the reason each one matters. Most of it
      is presence rather than technique — but the technique is not nothing.<br>
      <span class="mono-dim" style="font-size:11px">Born 25 December 2025 &middot;
        day {{ "{:,}".format(daughter.days) }}</span></p>
  </div>
  <div class="two rv">
    {% for f in father %}
    <div class="lrn-card tall">
      <div class="lrn-tag">Today</div>
      <div class="lrn-word sm">{{ f.title }}</div>
      <div class="lrn-do">{{ f.do }}</div>
      <div class="lrn-why"><b>Why</b> {{ f.why }}</div>
    </div>
    {% endfor %}
  </div>
</section>{% endif %}

<!-- ══════════ 08 WISDOM ══════════ -->
{% if 'wisdom' in secs %}<section class="sec" id="wisdom">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['wisdom'] }} / {{ seclabel['wisdom'] }}</span>
      <h2 class="stitle">Jainism and Buddhism</h2>
    </div>
    <p class="sdesc">Operating instructions, not theology. Each one carries the source idea and
      the thing to do with it today.</p>
  </div>
  <div class="two rv">
    {% for w in life_wisdom %}
    <div class="lrn-card tall {{ 'jain' if w.tradition == 'Jainism' else 'budd' }}">
      <div class="lrn-tag">{{ w.tradition }}</div>
      <div class="lrn-word sm">{{ w.term }} <span class="tr">· {{ w.translation }}</span></div>
      <div class="lrn-do">{{ w.teaching }}</div>
      <div class="lrn-why"><b>Today</b> {{ w.apply }}</div>
    </div>
    {% endfor %}
  </div>
</section>{% endif %}



<!-- ══════════ 06 THE MIND ══════════ -->
{% if 'mind' in secs %}<section class="sec" id="mind">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['mind'] }} / {{ seclabel['mind'] }}</span>
      <h2 class="stitle">Sharpen the operator</h2>
    </div>
    <p class="sdesc">One quote, one lesson from the world, one rule for being a better person and a better dad.</p>
  </div>

  <div class="quote-hero rv">
    <div class="mark" aria-hidden="true">&ldquo;</div>
    <blockquote>{{ quote.quote }}</blockquote>
    <cite>— {{ quote.name }}</cite>
    <div class="idx">Quote {{ quote.index }} of {{ quote.total }} · rotates daily</div>
  </div>

  <div class="two" style="margin-top:14px">
    <div class="essay rv" style="--ac:var(--up)">
      <div class="meta">Daily Wisdom · {{ wisdom.index }}/{{ wisdom.total }} · Better person · Better dad</div>
      <h3>{{ wisdom.title }}</h3>
      <p>{{ wisdom.body }}</p>
    </div>
    <div class="essay rv" style="--ac:var(--blue);--d:.08s">
      <div class="meta">{{ lesson.tradition }}</div>
      <h3 style="font-style:italic;font-weight:500;letter-spacing:-.5px">{{ lesson.lesson }}</h3>
      <p style="font-family:var(--mono);font-size:12px;color:var(--dim)">— {{ lesson.source }}</p>
    </div>
  </div>
</section>{% endif %}

<!-- ══════════ 07 THE WAY ══════════ -->
{% if 'way' in secs %}<section class="sec" id="way">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['way'] }} / {{ seclabel['way'] }}</span>
      <h2 class="stitle">Simple living. High thinking</h2>
    </div>
    <p class="sdesc">Own less. Behave well. Sit still. Think in models. One phrase of Arabic,
      one honest rep. Six tracks, rotating daily on different cycles — the combination never repeats.</p>
  </div>

  <div class="tabs rv" id="wayTabs">
    <button class="tab on" data-p="w1">🪶 Minimalism · {{ way.minimalism.index }}/{{ way.minimalism.total }}</button>
    <button class="tab" data-p="w2">🤝 Etiquette · {{ way.etiquette.index }}/{{ way.etiquette.total }}</button>
    <button class="tab" data-p="w3">🧘 Stillness · {{ way.stillness.index }}/{{ way.stillness.total }}</button>
    <button class="tab" data-p="w4">⚙️ Model · {{ way.model.index }}/{{ way.model.total }}</button>
    <button class="tab" data-p="w5">🇦🇪 Arabic · {{ way.arabic.index }}/{{ way.arabic.total }}</button>
    <button class="tab" data-p="w6">🎯 Drill · {{ way.drill.index }}/{{ way.drill.total }}</button>
    <button class="tab" data-p="w7">💪 Health · {{ way.health.index }}/{{ way.health.total }}</button>
  </div>

  <div class="rv">
    <div class="pane on" id="w1">
      <div class="essay" style="--ac:var(--lime)">
        <div class="meta">Minimalism · {{ way.minimalism.index }}/{{ way.minimalism.total }} · own less, decide less</div>
        <h3>{{ way.minimalism.title }}</h3>
        <p>{{ way.minimalism.body }}</p>
        <div class="act"><b>Do this today</b>{{ way.minimalism.action }}</div>
      </div>
    </div>

    <div class="pane" id="w2">
      <div class="essay" style="--ac:var(--gold)">
        <div class="meta">Etiquette · {{ way.etiquette.index }}/{{ way.etiquette.total }} · trust compounds</div>
        <h3>{{ way.etiquette.title }}</h3>
        <p>{{ way.etiquette.body }}</p>
        <div class="act"><b>Do this today</b>{{ way.etiquette.action }}</div>
      </div>
    </div>

    <div class="pane" id="w3">
      <div class="essay" style="--ac:var(--blue)">
        <div class="meta">Stillness · {{ way.stillness.index }}/{{ way.stillness.total }} · monk practice, modern life</div>
        <h3>{{ way.stillness.title }}</h3>
        <p>{{ way.stillness.body }}</p>
        <div class="act"><b>Do this today</b>{{ way.stillness.action }}</div>
      </div>
    </div>

    <div class="pane" id="w4">
      <div class="essay" style="--ac:var(--violet)">
        <div class="meta">Mental Model · {{ way.model.index }}/{{ way.model.total }} · the latticework</div>
        <h3>{{ way.model.title }}</h3>
        <p>{{ way.model.body }}</p>
        <div class="act"><b>Apply it today</b>{{ way.model.action }}</div>
      </div>
    </div>

    <div class="pane" id="w5">
      <div class="essay" style="--ac:var(--up)">
        <div class="meta">Arabic · {{ way.arabic.index }}/{{ way.arabic.total }} · for the Dubai move</div>
        <div class="arabic-hero">
          <div class="ar-script" dir="rtl" lang="ar">{{ way.arabic.script }}</div>
          <div class="ar-translit">{{ way.arabic.translit }}</div>
          <div class="ar-meaning">{{ way.arabic.meaning }}</div>
        </div>
        <div class="act"><b>When to use it</b>{{ way.arabic.use }}</div>
      </div>
    </div>

    <div class="pane" id="w6">
      <div class="essay" style="--ac:var(--down)">
        <div class="meta">Drill · {{ way.drill.index }}/{{ way.drill.total }} · ~10 minutes, deliberate</div>
        <h3>{{ way.drill.title }}</h3>
        <p>{{ way.drill.body }}</p>
        <div class="act"><b>The rep</b>{{ way.drill.action }}</div>
      </div>
    </div>

    <div class="pane" id="w7">
      <div class="essay" style="--ac:var(--pink,#FF7AA2)">
        <div class="meta">Health · {{ way.health.index }}/{{ way.health.total }} · the asset with no substitute</div>
        <h3>{{ way.health.title }}</h3>
        <p>{{ way.health.body }}</p>
        <div class="act"><b>Today's lever</b>{{ way.health.action }}</div>
      </div>
    </div>
  </div>

  <!-- streak tracker: localStorage only, no server -->
  <div class="streak rv" id="streakBox" hidden>
    <div class="stk-head">
      <div>
        <div class="stk-lab">Today's practice</div>
        <div class="stk-sub">Tick what you actually did. Stored in this browser only.</div>
      </div>
      <div class="stk-nums">
        <div class="stk-n"><b id="stkCur">0</b><i>current</i></div>
        <div class="stk-n"><b id="stkBest">0</b><i>best</i></div>
        <div class="stk-n"><b id="stkRate">0%</b><i>30d</i></div>
      </div>
    </div>
    <div class="stk-checks" id="stkChecks"></div>
    <div class="stk-strip" id="stkStrip" title="Last 30 days"></div>
    <div class="stk-foot">A day counts once you tick anything. Streak breaks on a fully empty day.</div>
  </div>
</section>{% endif %}

<!-- ══════════ 08 THE REVIEW ══════════ -->
{% if 'review' in secs %}<section class="sec" id="review">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['review'] }} / {{ seclabel['review'] }}</span>
      <h2 class="stitle">Look back, or none of it compounds</h2>
    </div>
    <p class="sdesc">Week {{ review.week }} of {{ review.year }}.
      {% if review.is_review_day %}Review day — do it now.{% else %}{{ review.days_left }} day{{ '' if review.days_left == 1 else 's' }} until the weekend review.{% endif %}
      Answers save in this browser, keyed to the week.</p>
  </div>

  <div class="rv">
    <div class="deep-q">
      <div class="dq-lab">This week's question · {{ review.index }}/{{ review.total }}</div>
      <h3>{{ review.prompt }}</h3>
      <p>{{ review.why }}</p>
    </div>

    <div class="rv-grid" id="reviewGrid" data-week="{{ review.key }}">
      <div class="rv-card">
        <label for="rvNumbers">The numbers</label>
        <div class="rv-hint">What moved, by how much, and did you cause it?</div>
        <textarea id="rvNumbers" rows="4" placeholder="e.g. 12 applications sent · SIP ₹10,000 · expectancy +0.14R over 22 signals"></textarea>
      </div>
      <div class="rv-card">
        <label for="rvWins">Wins</label>
        <div class="rv-hint">Only things that finished. Not things that progressed.</div>
        <textarea id="rvWins" rows="4" placeholder="What actually shipped or closed"></textarea>
      </div>
      <div class="rv-card">
        <label for="rvMisses">Misses</label>
        <div class="rv-hint">What slipped, and the cause — not the excuse.</div>
        <textarea id="rvMisses" rows="4" placeholder="What did not happen, and why"></textarea>
      </div>
      <div class="rv-card">
        <label for="rvAnswer">Answer to this week's question</label>
        <div class="rv-hint">{{ review.prompt }}</div>
        <textarea id="rvAnswer" rows="4" placeholder="Be specific. Vague answers are avoidance."></textarea>
      </div>
      <div class="rv-card wide">
        <label for="rvChange">One change for next week</label>
        <div class="rv-hint">Exactly one. A list of five is a list of none.</div>
        <textarea id="rvChange" rows="3" placeholder="One change. Specific enough to verify next Sunday."></textarea>
      </div>
    </div>

    <div class="rv-bar">
      <span class="rv-status" id="rvStatus">Not started</span>
      <button type="button" class="rv-btn" id="rvCopy">Copy week as Markdown</button>
    </div>
  </div>
</section>{% endif %}

<!-- ══════════ 05 THE DESK ══════════ -->
{% if 'desk' in secs %}<section class="sec" id="desk">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['desk'] }} / {{ seclabel['desk'] }}</span>
      <h2 class="stitle">Compound the skill</h2>
    </div>
    <p class="sdesc">FP&amp;A, the CFO ladder, a case study, a book, and one hack — rotating daily.
      Seven tabs, one discipline.</p>
  </div>

  <div class="tabs rv" id="deskTabs">
    <button class="tab on" data-p="d1">🎓 FP&amp;A · {{ fpna.index }}/{{ fpna.total }}</button>
    <button class="tab" data-p="d2">🇦🇪 Dubai · {{ dubai.index }}/{{ dubai.total }}</button>
    <button class="tab" data-p="d3">🏆 FC → CFO · {{ cfo.index }}/{{ cfo.total }}</button>
    <button class="tab" data-p="d4">📊 Case Study</button>
    <button class="tab" data-p="d6">💰 Money</button>
    <button class="tab" data-p="d7">⚡ Execution</button>
  </div>

  <div class="rv">
    <div class="pane on" id="d1">
      <div class="essay">
        <div class="meta">FP&amp;A Learn · Lesson {{ fpna.index }} of {{ fpna.total }}</div>
        <h3>{{ fpna.title }}</h3>
        <p>{{ fpna.body }}</p>
      </div>
    </div>

    <div class="pane" id="d2">
      <div class="essay" style="--ac:var(--violet)">
        <div class="meta">Dubai Corner · AED 30K+ Track · {{ dubai.index }}/{{ dubai.total }}</div>
        <h3>{{ dubai.title }}</h3>
        <p>{{ dubai.body }}</p>
        <div class="q">{{ dubai.targets }}</div>
        <div class="act"><b>{{ dubai.action_label }}</b>{{ dubai.action }}</div>
      </div>
    </div>

    <div class="pane" id="d3">
      <div class="essay" style="--ac:var(--gold)">
        <div class="meta">Financial Controller → CFO · Step {{ cfo.index }} of {{ cfo.total }}</div>
        <h3>{{ cfo.title }}</h3>
        <p>{{ cfo.body }}</p>
      </div>
    </div>

    <div class="pane" id="d4">
      <div class="essay" style="--ac:var(--blue)">
        <div class="meta">Business Case Study</div>
        <h3>{{ case.title }}</h3>
        <p>{{ case.story }}</p>
        <div class="act"><b>💡 The lesson</b>{{ case.lesson }}</div>
      </div>
    </div>

    <div class="pane" id="d6">
      <div class="essay" style="--ac:var(--lime)">
        <div class="meta">Money Hack</div>
        <h3>{{ money_hack.title }}</h3>
        <p>{{ money_hack.body }}</p>
      </div>
    </div>

    <div class="pane" id="d7">
      <div class="essay" style="--ac:var(--up)">
        <div class="meta">Today's Rule</div>
        <h3>Execution beats intention</h3>
        <p>{{ productivity_tip }}</p>
      </div>
    </div>
  </div>
</section>{% endif %}

<!-- ══════════ 09 CHESS ══════════ -->
{% if 'chess' in secs %}<section class="sec" id="chess">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['chess'] }} / {{ seclabel['chess'] }}</span>
      <h2 class="stitle">{% if lichess_summary.is_yesterday %}Yesterday&rsquo;s chess{% else %}Your last session{% endif %}</h2>
    </div>
    <div style="text-align:right">
      <p class="sdesc">AKK_010 on Lichess. Pattern over volume — review the turning point, not the result.
        {%- if lichess_summary.session_date and not lichess_summary.is_yesterday %}
        <br><span style="color:var(--gold)">No games yesterday &mdash; showing
        {{ lichess_summary.session_date }}, the most recent day you played.</span>{% endif %}</p>
      <a class="slink" href="https://lichess.org/@/AKK_010" target="_blank" style="display:inline-block;margin-top:10px">Profile →</a>
    </div>
  </div>

  {% if lichess_games %}
  <div class="chess-kpi rv">
    <div class="ck"><div class="v up">{{ lichess_summary.wins }}</div><div class="k">Wins</div></div>
    <div class="ck"><div class="v dn">{{ lichess_summary.losses }}</div><div class="k">Losses</div></div>
    <div class="ck"><div class="v" style="color:var(--dim)">{{ lichess_summary.draws }}</div><div class="k">Draws</div></div>
    <div class="ck"><div class="v" style="color:var(--lime)">{{ lichess_summary.pct }}%</div><div class="k">Win Rate</div></div>
    <div class="ck"><div class="v">{{ lichess_summary.total }}</div><div class="k">Games</div></div>
    {% if lichess_summary.mode == "full" %}
    <div class="ck"><div class="v" style="color:var(--blue)">{{ lichess_summary.upsets }}</div><div class="k">Upsets</div></div>
    <div class="ck"><div class="v dn">{{ lichess_summary.collapses }}</div><div class="k">Collapses</div></div>
    {% endif %}
  </div>

  {% if lichess_summary.session_summary %}
  <div class="verdict rv">🤖 <b>Coach's verdict</b><br>{{ lichess_summary.session_summary }}</div>
  {% else %}
  <div class="verdict rv" style="background:rgba(255,255,255,.03);border-color:var(--line)">
    {{ lichess_summary.icon }}
    {% if lichess_summary.pct >= 55 %} Good session — {{ lichess_summary.wins }}/{{ lichess_summary.total }}. Review the wins and lock in the patterns.
    {% elif lichess_summary.pct >= 45 %} Balanced. W{{ lichess_summary.wins }} L{{ lichess_summary.losses }}. Find the turning point in each loss.
    {% else %} Rough session. W{{ lichess_summary.wins }} L{{ lichess_summary.losses }}. Review losses before the next game. Pattern beats volume.{% endif %}
  </div>
  {% endif %}

  {% if lichess_summary.mode == "full" and (lichess_summary.weak_op or lichess_summary.best_op) %}
  <div class="two rv" style="margin-bottom:18px">
    {% if lichess_summary.weak_op %}
    <div class="card" style="border-color:rgba(255,92,92,.25)">
      <div class="meta" style="font-family:var(--mono);font-size:11px;letter-spacing:1.6px;text-transform:uppercase;color:var(--down);margin-bottom:6px">📚 Study this opening</div>
      <div style="font-size:14px;color:#FFA0A0">{{ lichess_summary.weak_op }}</div>
    </div>
    {% endif %}
    {% if lichess_summary.best_op %}
    <div class="card" style="border-color:rgba(61,220,151,.25)">
      <div class="meta" style="font-family:var(--mono);font-size:11px;letter-spacing:1.6px;text-transform:uppercase;color:var(--up);margin-bottom:6px">💪 Strongest opening</div>
      <div style="font-size:14px;color:#9BEFC9">{{ lichess_summary.best_op }}</div>
    </div>
    {% endif %}
  </div>
  {% endif %}

  {% if lichess_summary.mode == "full" %}
  <div class="rv">
    {% for g in lichess_games %}
    <div class="game {{ g.cls }}">
      <div class="hdr">
        <span style="font-size:16px">{{ g.icon }}</span>
        <span class="res {{ 'up' if g.cls == 'win' else ('dn' if g.cls == 'loss' else '') }}"
              {% if g.cls == 'draw' %}style="color:var(--dim)"{% endif %}>{{ g.result }}</span>
        <span class="pill">as {{ g.my_side }}</span>
        <span class="pill">{{ g.speed }}</span>
        {% if g.is_upset %}<span class="pill" style="background:rgba(106,168,255,.14);color:var(--blue)">Upset ⚡</span>{% endif %}
        {% if g.is_collapse %}<span class="pill" style="background:rgba(255,92,92,.13);color:var(--down)">Collapse ⚠</span>{% endif %}
        {% if g.is_long %}<span class="pill">{{ g.moves }}M Epic</span>{% endif %}
        <a href="{{ g.url }}" target="_blank" class="slink" style="margin-left:auto">▶ Review</a>
      </div>
      <div class="op">{% if g.eco %}<span style="color:var(--gold);font-family:var(--mono);font-size:11px;margin-right:7px">{{ g.eco }}</span>{% endif %}{{ g.opening }}</div>
      <div class="meta">
        vs <strong style="color:var(--text)">{{ g.opponent }}</strong> <span style="color:var(--dim)">({{ g.opp_rating }})</span>
        · me {{ g.my_rating }} · {{ g.moves }} moves · {{ g.termination }}
        {% if g.me_diff is not none %}· <span class="{{ 'up' if g.me_diff > 0 else 'dn' }}">{{ "+" if g.me_diff > 0 else "" }}{{ g.me_diff }} pts</span>{% endif %}
      </div>
      {% if g.best_move %}
      <div class="bestmv">
        <div class="bmlab">Best move of the game</div>
        <div class="bmrow">
          <span class="bmsan">{{ g.best_move.move_no }}. {{ g.best_move.san }}</span>
          <span class="bmgain">+{{ (g.best_move.gain_cp / 100) | round(1) }} pawns</span>
          <span class="bmeval">eval after {{ "%+.2f"|format(g.best_move.eval_after) }}</span>
        </div>
      </div>
      {% endif %}
      {% if g.standout %}<div class="uniq"><b>What made it different</b>{{ g.standout }}</div>{% endif %}
      {% if g.key_facts %}
      <div class="kfacts">
        {% for f in g.key_facts %}<span class="kf">{{ f }}</span>{% endfor %}
      </div>
      {% endif %}
      {% if g.game_strength or g.est_fide %}
      <div class="ratings">
        {% if g.game_strength %}<span class="rt"><i>Played at</i><b>~{{ g.game_strength }}</b></span>{% endif %}
        {% if g.est_fide %}<span class="rt"><i>Est. FIDE equiv.</i><b>~{{ g.est_fide }}</b></span>{% endif %}
        <span class="rtnote">estimates from Lichess {{ g.speed|lower }} rating &amp; centipawn loss — not official FIDE</span>
      </div>
      {% endif %}
      {% if not g.analysed %}<div class="mv" style="color:var(--dim)">Not analysed on Lichess — request computer analysis on the game to get best move, accuracy and key facts here.</div>{% endif %}
      {% if g.analysis %}<div class="an">💡 {{ g.analysis }}</div>{% endif %}
    </div>
    {% endfor %}
  </div>
  {% else %}
  <div class="two rv" style="margin-bottom:16px">
    {% for g in lichess_games %}
    <div class="card">
      <div style="font-family:var(--mono);font-size:11px;letter-spacing:1.6px;text-transform:uppercase;color:var(--dim);margin-bottom:8px">{{ g.speed }}</div>
      <div class="num" style="font-size:32px;font-weight:700;letter-spacing:-1.4px;color:{% if g.pct >= 55 %}var(--up){% elif g.pct >= 45 %}var(--gold){% else %}var(--down){% endif %}">{{ g.pct }}%</div>
      <div style="font-size:12px;color:var(--muted);margin-top:7px">
        <span class="up">W{{ g.wins }}</span> · <span class="dn">L{{ g.losses }}</span> ·
        <span style="color:var(--dim)">D{{ g.draws }}</span> · {{ g.total }} games
      </div>
      <a href="{{ g.profile_url }}" target="_blank" class="slink" style="display:inline-block;margin-top:11px">Lichess →</a>
    </div>
    {% endfor %}
  </div>
  <!-- This used to read "Add LICHESS_TOKEN to GitHub secrets" whenever the
       page fell back to aggregate counts — including every time the token was
       set, which it has been since 2026-07-29. Falling back and lacking a
       token are different questions, and conflating them sent you to create a
       credential you already had. Each cause now says its own name. -->
  {% if lichess_summary.token_missing %}
  <div class="empty rv" style="text-align:left">⚡ Add <code style="color:var(--lime);font-family:var(--mono)">LICHESS_TOKEN</code> to GitHub secrets to raise the export rate limit and include private games.
    <a href="https://lichess.org/account/oauth/token/create" target="_blank" style="color:var(--lime)">Create token →</a></div>
  {% else %}
  <div class="empty rv" style="text-align:left">No individual games came back from the export API for the last seven days,
    so this is the aggregate view. The token is set &mdash; this is not a credentials problem.</div>
  {% endif %}
  {% endif %}

  {% if lichess_summary.trend %}
  <div class="rv" style="margin-top:18px">
    <div style="font-family:var(--mono);font-size:11px;letter-spacing:1.8px;text-transform:uppercase;color:var(--dim);margin-bottom:6px">📈 7-day win rate</div>
    <div class="trend">
      {% for t in lichess_summary.trend | reverse %}
      <div>
        <div class="bar" style="--h:{{ [t.pct * 70 // 100, 4] | max }}px;--d:{{ loop.index0 * 0.07 }}s;background:{% if t.pct >= 55 %}var(--up){% elif t.pct >= 45 %}var(--gold){% else %}var(--down){% endif %}"></div>
        <div class="lb">{{ t.day }}</div>
        <div class="lb" style="color:var(--muted)">{{ t.pct }}%</div>
      </div>
      {% endfor %}
    </div>
  </div>
  {% endif %}

  {% else %}
  <div class="empty rv">No games played yesterday · <a href="https://lichess.org/@/AKK_010" target="_blank" style="color:var(--lime)">Play on Lichess →</a></div>
  {% endif %}

  <div class="two rv" style="margin-top:18px">
    <div class="essay" style="--ac:var(--gold)">
      <div class="meta">Chess Tutor · Lesson {{ chess.index }}/{{ chess.total }}</div>
      <h3>{{ chess.title }}</h3>
      <p>{{ chess.body }}</p>
      <p style="font-family:var(--mono);font-size:11px;color:var(--dim);margin-top:14px">
        Practise: <a href="https://lichess.org/study" target="_blank" style="color:var(--lime)">Lichess Study</a> ·
        <a href="https://chess.com/puzzles" target="_blank" style="color:var(--lime)">Chess.com Puzzles</a></p>
    </div>
    {% if lichess_puzzle %}
    <div class="essay" style="--ac:var(--gold);--d:.08s">
      <div class="meta">🧩 Today's puzzle</div>
      <h3>Rating {{ lichess_puzzle.rating }} · {{ lichess_puzzle.level }}</h3>
      <p style="font-family:var(--mono);font-size:12px;color:var(--dim)">{{ lichess_puzzle.themes }}</p>
      <div class="q">💡 {{ lichess_puzzle.tip }}</div>
      <a href="{{ lichess_puzzle.url }}" target="_blank" class="btn btn-sm" style="display:inline-block;margin-top:6px">→ Solve on Lichess</a>
    </div>
    {% endif %}
  </div>
</section>{% endif %}

<!-- ══════════ 10 MIND GYM ══════════
     Pure client-side: deterministic daily seed, scores in localStorage. No
     API, so it works identically on the static host. -->
{% if 'gym' in secs %}<section class="sec" id="gym">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['gym'] }} / {{ seclabel['gym'] }}</span>
      <h2 class="stitle">Six minutes. Sharper</h2>
    </div>
    <p class="sdesc">A new set every day, same set for the whole day. Numbers under time
      pressure, estimation, recall, and the two calculations a trading desk actually runs.
      Scores stay in this browser.</p>
  </div>

  <div class="gym-tabs rv" id="gymTabs"></div>
  <div class="gym-stage rv" id="gymStage"></div>
  <div class="gym-score rv" id="gymScore"></div>
</section>{% endif %}

</main>

<!-- The trade sheet sits OUTSIDE <main> deliberately. main is
     position:relative with z-index:2, which creates a stacking context — a
     modal nested inside it is confined to that context and can never paint
     above the sticky header at z-index 300, whatever z-index it is given. It
     opened behind the nav with its own close button hidden, which reads as a
     truncated panel rather than a layering bug. -->
<div class="sheet" id="sheet" hidden>
  <div class="sheet-in">
    <button type="button" class="sheet-x" id="sheetX" aria-label="Close">✕</button>
    <div id="sheetBody"></div>
  </div>
</div>



<footer>
  <div class="foot-in">
    <div>
      <h3 class="fh4">THE DAILY <b>SIGNAL</b></h3>
      <p style="color:var(--muted);font-size:13px;margin-top:12px;max-width:38ch">
        Built by Akshay Kothari. Rebuilt every morning at 6 AM MYT by a machine that does not sleep.</p>
    </div>
    <div class="m">
      news.askakshay.com<br>
      {{ date_str }} · built {{ updated_at }} MYT<br>
      <span style="color:var(--dim)">Sources: NSE · Yahoo Finance · AMFI · Chittorgarh ·
        investorgain · wire RSS</span><br>
      <a href="#datahealth" class="lnk">Data health</a> ·
      <a href="#who" class="lnk">Methodology</a> ·
      <a href="/data-health.json" class="lnk">JSON</a>
    </div>
  </div>

  <div class="foot-legal">
    <div>
      <h3 class="fh4">What this is, and is not</h3>
      <p>A public log of signals generated by my own engine and the trades I take against
        them. Every signal is recorded when it fires and scored when it closes, wins and
        losses alike. <strong>It is not investment advice and I am not a SEBI-registered
        adviser.</strong> Nothing here is a recommendation to buy or sell. Markets can and
        do take the whole position; size accordingly.</p>
    </div>
    <div>
      <h3 class="fh4">Your email</h3>
      <p>If you subscribe, your address is stored on my own database and used for one
        thing: sending the daily edition. It is never sold, never shared, and never passed
        to an ad network. This page loads no analytics and no tracking scripts of any kind,
        and the fonts are served from this domain. As loaded it contacts no third party at
        all. The one exception is deliberate and opt-in: pressing play on a track loads a
        YouTube player, which is a connection to Google. It uses the no-cookie player and
        is created only on that click &mdash; if you never press play, it never loads.
        Reply to any edition to be removed, or write to
        <a href="mailto:ca.akkothari@gmail.com">ca.akkothari@gmail.com</a>.</p>
    </div>
    <div>
      <h3 class="fh4">Reach me</h3>
      <p><a href="https://askakshay.com" target="_blank" rel="noopener">askakshay.com</a> ·
         <a href="https://www.linkedin.com/in/akkothari" target="_blank" rel="noopener">LinkedIn</a> ·
         <a href="mailto:ca.akkothari@gmail.com">Email</a><br>
         &copy; 2026 Akshay K Kothari, CA</p>
    </div>
  </div>
</footer>

<button class="fab" id="fab" aria-label="Back to top">↑</button>

<script type="application/json" id="tv-aliases" nonce="{{ nonce }}">{{ tv_aliases|tojson }}</script>
{# symbol -> industry for the heat-map drill-down. A JSON block, not string
   interpolation into app.js — app.js is a real file with a linter pointed at
   it and must never become a template again. #}
<script type="application/json" id="sector-map" nonce="{{ nonce }}">{{ (sector_map or {})|tojson }}</script>
<script nonce="{{ nonce }}" src="/app.js?v={{ build_id }}" defer></script>
</body>
</html>
"""

# ─────────────────────────────────────────────────────────────
# FLASK ROUTES
# ─────────────────────────────────────────────────────────────

@app.route("/app.js")
def app_js():
    """Serve the page script under Flask too.

    generate.py copies static/app.js into docs/ for the static host; this route
    is the equivalent for the local Flask server, so a dev run is not silently
    missing every interactive feature on the page.
    """
    from flask import send_from_directory
    resp = send_from_directory(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"),
        "app.js", mimetype="application/javascript")
    # Cache-busted by ?v=<build_id> in the template, so this can be immutable.
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route("/")
def _learning_ctx() -> dict:
    """The daily curriculum. Authored local data with no network or DB behind
    it, so it renders even on the error path where everything else is empty."""
    try:
        d = daily_learning.get_all()
        return {
            "interview_tech": d["interview_tech"], "interview_soft": d["interview_soft"],
            "cfo_field": d["cfo_field"],
            "spanish": d["spanish"], "vocab": d["vocab"], "speaking": d["speaking"],
            "father": d["father"], "life_wisdom": d["wisdom"],
        }
    except Exception as e:
        log.warning(f"learning tracks: {e}")
        return {"interview_tech": [], "interview_soft": [], "cfo_field": [], "spanish": [],
                "vocab": [], "speaking": {}, "father": [], "life_wisdom": []}


def index():
    try:
        now     = datetime.now(IST)
        markets        = fetch_markets()
        news           = fetch_global_news()
        fpna           = get_fpna_tip()
        cfo            = get_cfo_lesson()
        chess          = get_chess_lesson()
        wisdom         = get_wisdom_lesson()
        book           = get_book_lesson()
        way_ctx        = get_way()
        review_ctx     = get_review()
        top5           = get_top5_picks()
        tracker        = get_tracker_stocks()
        money          = get_money_hack()
        dubai          = get_dubai_note()
        daughter       = daughter_age()
        import music as _music
        music_lib      = _music.library()
        prod           = get_productivity_tip()
        quote          = get_entrepreneur_quote()
        lesson         = get_world_lesson()
        case           = get_case_study()
        lichess_games   = fetch_lichess_games()
        lichess_summary = get_lichess_summary(lichess_games)
        lichess_puzzle  = fetch_lichess_puzzle()
        alerts         = fetch_alert_log()
        fund_screen    = get_fund_screen()
        if fund_screen:
            fund_screen["job_status"] = get_job_status("fund_screen", fund_screen.get("generated_at"))

        return render_template_string(TEMPLATE,
            tv_aliases=TV_ALIASES,
            date_str=now.strftime("%A, %B %d %Y"),
            updated_at=now.strftime("%H:%M"),
            markets=markets, news=news, fpna=fpna, cfo=cfo,
            chess=chess, wisdom=wisdom, book=book, way=way_ctx, review=review_ctx,
            top5=top5, tracker=tracker, money_hack=money, dubai=dubai, daughter=daughter, music=music_lib,
            fund_screen=fund_screen,
            productivity_tip=prod,
            quote=quote, lesson=lesson, case=case,
            lichess_games=lichess_games, lichess_summary=lichess_summary, lichess_puzzle=lichess_puzzle,
            alerts=alerts,
            **_learning_ctx(),
        )
    except Exception as e:
        log.error(f"index error: {e}")
        import traceback; traceback.print_exc()
        now = datetime.now(IST)
        return render_template_string(TEMPLATE,
            tv_aliases=TV_ALIASES,
            date_str=now.strftime("%A, %B %d %Y"),
            updated_at=f"{now.strftime('%H:%M')} (partial)",
            markets=[], news=[], fpna={"title":"Loading","body":"","index":0,"total":1},
            cfo={"title":"Loading","body":"","index":0,"total":1},
            chess={"title":"Loading","body":"","index":0,"total":1},
            wisdom={"title":"Loading","body":"","index":0,"total":1},
            book={"book":"Loading","author":"","chapter":"Loading","lesson":"","key_quote":"","action":"","index":0,"total":1},
            way=_way_placeholder(), review=_review_placeholder(),
            top5=[], tracker=[], fund_screen={}, money_hack={"title":"Loading","body":""},
            dubai=get_dubai_note(), daughter=daughter_age(),
            music=__import__('music').library(),
            productivity_tip="Loading...",
            quote={"quote":"","name":"","index":0,"total":1},
            lesson={"tradition":"","lesson":"","source":""},
            case={"title":"","story":"","lesson":""},
            lichess_games=[], lichess_summary={}, lichess_puzzle={},
            alerts=[],
            **_learning_ctx(),
        ), 200

@app.route("/tracker/add", methods=["POST"])
def tracker_add():
    sym    = request.form.get("symbol", "").strip().upper()
    name   = request.form.get("name", sym)
    entry  = float(request.form.get("entry_price") or 0)
    target = float(request.form.get("target_price") or 0)
    stop   = float(request.form.get("stop_loss") or entry * 0.92)
    thesis = request.form.get("thesis", "")
    tf     = request.form.get("timeframe", "2-3 months")
    if sym and entry: add_to_tracker(sym, entry, target, stop, thesis, tf, name)
    return redirect("/#tracker")

@app.route("/tracker/exit/<int:stock_id>", methods=["POST"])
def tracker_exit(stock_id):
    exit_tracker(stock_id)
    return redirect("/#tracker")

@app.route("/tracker/obsidian", methods=["POST"])
def tracker_obsidian():
    sync_tracker_to_obsidian(get_tracker_stocks())
    return redirect("/#tracker")

@app.route("/tracker/history")
def tracker_history():
    with _db() as con:
        rows = con.execute("SELECT * FROM stock_tracker WHERE status='exited' ORDER BY updated_at DESC LIMIT 50").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/refresh")
def api_refresh():
    """Force-refresh all content: clear caches, rebuild picks."""
    try:
        from content_cache import invalidate
        invalidate()
        log.info("api/refresh: content cache cleared")
    except Exception as e:
        log.warning(f"api/refresh cache invalidate: {e}")
    today = date.today().isoformat()
    with _db() as con:
        con.execute("DELETE FROM newspaper_stocks_picked WHERE pick_date=?", (today,))
    with _picks_lock:
        _picks_cache.pop(today, None)
    threading.Thread(target=_warm_picks_cache, daemon=True).start()
    return redirect("/")

@app.route("/health")
def health():
    return jsonify({"status": "ok", "name": "The Daily Signal", "time": datetime.now(IST).isoformat()})

# ─────────────────────────────────────────────────────────────
# 6 AM MYT DAILY REFRESH SCHEDULER
# ─────────────────────────────────────────────────────────────

def _daily_6am_refresh():
    """Fires at 6 AM MYT (22:00 UTC) — clears all caches, rebuilds picks.

    Must stay in step with .github/workflows/newspaper.yml. When this said
    00:30 UTC and the workflow said 22:00 UTC, a long-running process would
    have cleared the caches 2h30m after the build that filled them.
    """
    log.info("6 AM MYT refresh: clearing all caches")
    try:
        from content_cache import invalidate
        invalidate()
    except Exception as e:
        log.warning(f"6AM cache invalidate: {e}")
    today = date.today().isoformat()
    with _db() as con:
        con.execute("DELETE FROM newspaper_stocks_picked WHERE pick_date=?", (today,))
    with _picks_lock:
        _picks_cache.pop(today, None)
    threading.Thread(target=_warm_picks_cache, daemon=True).start()
    log.info("6 AM MYT refresh: done — fresh content ready")

def _start_scheduler():
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(_daily_6am_refresh, CronTrigger(hour=22, minute=0, timezone="UTC"))  # 6 AM MYT
    sched.start()
    log.info("Scheduler: daily refresh at 06:00 MYT (22:00 UTC)")
    return sched

# ─────────────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────────────

def _startup():
    try:
        init_newspaper_db()
    except Exception as e:
        logging.warning(f"DB init skipped (read-only token?): {e}")
    try:
        from content_cache import invalidate
        invalidate()
        log.info("Startup: content cache invalidated")
    except Exception as e:
        log.warning(f"Startup cache: {e}")
    threading.Thread(target=_warm_picks_cache, daemon=True).start()
    _start_scheduler()
    log.info("THE DAILY SIGNAL — news.askakshay.com — started")

if __name__ == "__main__":
    _startup()
    app.run(host="0.0.0.0", port=PORT, debug=False)
else:
    _startup()
