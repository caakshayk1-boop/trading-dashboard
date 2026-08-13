#!/usr/bin/env python3
"""
THE DAILY SIGNAL — Akshay's Personal Intelligence Brief
Sections: Weather · World News · Markets · Quote · Wisdom/Dad · Chess · FP&A→CFO
          Business Case Study · Top 5 Picks · Stock Tracker · Money Hack · Productivity
Refreshes at 6 AM IST daily. Deploy: news.askakshay.com
"""
from __future__ import annotations

import os, json, math, sqlite3, logging, time, threading
from datetime import datetime, timezone, timedelta, date
from typing import Optional
import feedparser
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


def fetch_alert_log(limit: int = 200) -> list[dict]:
    """Read Telegram alert history from all_signals table (signals.db / Turso)."""
    try:
        with _db() as con:
            rows = con.execute("""
                SELECT date, symbol, action, timeframe, signal_type,
                       entry, sl, target1, target2, rr, score,
                       status, lifecycle_status, exit_price, pnl_pct,
                       closed_at, sent_at
                FROM all_signals
                ORDER BY date DESC, id DESC
                LIMIT ?
            """, (limit,)).fetchall()
        cols = ["date","symbol","action","timeframe","signal_type",
                "entry","sl","target1","target2","rr","score",
                "status","lifecycle_status","exit_price","pnl_pct",
                "closed_at","sent_at"]
        result = []
        for r in rows:
            r = dict(zip(cols, r))
            # badge colour logic
            s = (r.get("status") or "").upper()
            lc = (r.get("lifecycle_status") or "").upper()
            WIN_STATUSES  = {"TARGET_HIT", "T1_HIT", "T2_HIT", "TP1_HIT", "TP2_HIT", "PROFIT"}
            LOSS_STATUSES = {"SL_HIT", "STOPPED", "STOP_HIT", "LOSS"}
            if s in WIN_STATUSES or lc in WIN_STATUSES:
                badge = "win"
            elif s in LOSS_STATUSES or lc in LOSS_STATUSES:
                badge = "loss"
            elif s == "OPEN" or lc == "OPEN":
                badge = "open"
            else:
                badge = "cancelled"
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
        con.execute("""CREATE TABLE IF NOT EXISTS newspaper_funds (
            week TEXT PRIMARY KEY, payload TEXT
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

def groq_complete(prompt: str, max_tokens: int = 120) -> str:
    if not GROQ_KEY:
        return ""
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            # llama3-8b-8192 was decommissioned by Groq. Every call 400'd and
            # every caller silently used its fallback string — which is exactly
            # how five different stocks ended up with the same one-line thesis.
            json={"model": "llama-3.3-70b-versatile",
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": max_tokens, "temperature": 0.7},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"].strip()
        # Log the body, not just the code. A decommissioned model returns a 400
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
SECTION_MAP = [
    # (id,          nav label,      page,   nav group)
    ("world",       "World",        "main", "Markets"),
    ("who",         "Who",          "main", "About"),
    ("picks",       "Trade Ideas",  "main", "Ideas"),
    ("longterm",    "Long-Term",    "main", "Ideas"),
    ("tracker",     "Portfolio",    "main", "The Book"),
    ("sip",         "SIP Buckets",  "main", "Invest"),
    ("funds",       "Fund Screen",  "main", "Invest"),
    # Pure client-side arithmetic — no API, no ledger. It therefore works
    # identically on the static host, and unlike #sip it must NOT start hidden.
    ("swp",         "SWP",          "main", "Invest"),
    ("interview",   "Interview",    "desk", "Work"),
    ("language",    "Language",     "desk", "Practice"),
    ("father",      "Father",       "desk", "Practice"),
    ("wisdom",      "Wisdom",       "desk", "Practice"),
    ("desk",        "The Desk",     "desk", "Reading"),
    ("mind",        "The Mind",     "desk", "Reading"),
    ("way",         "The Way",      "desk", "Reading"),
    ("review",      "The Review",   "desk", "Reading"),
    ("chess",       "Chess",        "desk", "Drills"),
    # Sits directly above Music — both are "what is playing this week", and
    # the nav group keeps them adjacent no matter how the page is reordered.
    ("smartreads",  "Smart Reads",  "desk", "Reading"),
    ("podcasts",    "Podcasts",     "desk", "Drills"),
    ("music",       "Music",        "desk", "Drills"),
    ("gym",         "Mind Gym",     "desk", "Drills"),
    ("perf",        "Performance",  "main", "Track Record"),
    ("rules",       "Engine Log",   "main", "Track Record"),
    # Sits directly above the Signal Log because that is the reading order that
    # makes sense: the screen is where a name comes FROM, the log is what
    # happened to the ones that were acted on. Its own nav group, because a
    # research screen is not a track record — which does mean "Track Record"
    # prints twice in the nav, above rules and again above alerts. That is the
    # documented behaviour of a repeated group (see the SECTION_MAP note) and
    # the honest rendering of a fixed document order, not a bug to work around
    # by moving the section somewhere it does not belong.
    ("stocks",      "Stock Screen", "main", "Research"),
    ("alerts",      "Signal Log",   "main", "Track Record"),
]

PAGE_META = {
    "main": {
        "title": "The Daily Signal — live NSE trading ledger, scored",
        "desc": ("A public, auditable NSE trading ledger. Every signal logged when it "
                 "fires and scored when it closes — wins and losses both. Live markets, "
                 "long-term conviction picks and the last 24 hours of world news, "
                 "rebuilt at 6 AM IST daily by Akshay Kothari, CA."),
        "path": "/",
        "other_label": "The Desk",
        "other_path": "/desk",
        "other_hint": "languages, fatherhood, chess, music, drills",
    },
    "desk": {
        "title": "The Desk — languages, fatherhood, chess and the daily drills",
        "desc": ("The practice behind the ledger: Spanish and Arabic drills, one thing "
                 "to do with a seven-month-old, chess from yesterday's games, the "
                 "reading, and six minutes of mental arithmetic. Rebuilt daily."),
        "path": "/desk",
        "other_label": "The Signal",
        "other_path": "/",
        "other_hint": "markets, trade ideas, the live ledger",
    },
}


def empty_sections(fund_screen=None, podcasts=None, smart_reads=None,
                   stock_screen=None) -> set:
    """Sections that must not be advertised in the nav on this build.

    A helper rather than an inline check so the decision has one home. Only
    generate.py consumes it today — the Flask routes never pass `secs` at all,
    so they already render without sections — but the published page is built
    here, and this is where the nav and the document have to agree.
    """
    drop = set()
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
    return {
        "page": page,
        "secs": {i for i, _l, _g in rows},
        "nav": nav,
        # Section headings read their number from here rather than carrying a
        # literal, so the nav and the heading cannot disagree — which they did,
        # with Performance showing "17 / EDGE" under a nav item numbered 07.
        # Number AND label both come from SECTION_MAP. Carrying the label as a
        # literal in the template is how "07 Performance" in the nav ended up
        # over "17 / EDGE" on the page — two sources of truth for one name.
        "secnum": {i: f"{n:02d}" for n, (i, _l, _g) in enumerate(rows, 1)},
        "seclabel": {i: l.upper() for i, l, _g in rows},
        # Supplied here rather than at each render call site. There are two of
        # those in the Flask path alone plus the static generator, and the
        # error-path render is the one that would have silently dropped it —
        # which is precisely the day the log needs to still be on the page.
        "engine_changes": ENGINE_CHANGES,
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
        hist     = yf.Ticker(sym).history(period="3mo")
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
        mom_3m   = (price - close.iloc[0])   / close.iloc[0]  * 100
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
        target   = round(price * (1.25 if mom_3m > 15 else 1.20), 2)
        return {"symbol": sym, "name": sym.replace(".NS","").replace(".BO",""),
                "ext20": round(ext20, 2), "vol_ratio": round(float(vol_ratio), 2),
                "price": round(price, 2), "change_1d": round((price - close.iloc[-2]) / close.iloc[-2] * 100, 2),
                "mom_1m": round(mom_1m, 1), "mom_3m": round(mom_3m, 1), "score": score,
                "target": target, "stop_loss": round(price * 0.92, 2),
                "timeframe": "2–3 months", "currency": currency,
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
    """Score all 60 stocks, return top 5 by momentum score.
    Runs weekly — same week's picks stay consistent for journal tracking.
    """
    scored = []
    for sym in WATCHLIST:
        s = score_stock(sym)
        if s: scored.append(s)
        time.sleep(0.05)
    scored.sort(key=lambda x: x["score"], reverse=True)
    top5 = scored[:5]
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
PICKS_ENGINE = "v5"


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


def _week_key() -> str:
    """Cache key: ISO week plus the engine that produced the picks.

    Keyed on the IST date, not the runner's UTC date. Everything else on this
    page is stamped IST, and a delayed GitHub run near the UTC/IST boundary
    would otherwise file Monday's picks under last week.
    """
    d = datetime.now(IST).date()
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

def get_top5_picks(build_if_missing: bool = False) -> list[dict]:
    """This week's five ideas.

    `build_if_missing` exists because the static generator has no long-running
    process behind it. Under Flask, _warm_picks_cache() runs on a background
    thread at startup; generate.py never called it, so on the first build of a
    new ISO week the DB had no row for that week and the section rendered its
    "check back Monday" empty state — every Monday, all day. See generate.py.
    """
    week = _week_key()
    with _picks_lock:
        if week in _picks_cache: return _picks_cache[week]
    with _db() as con:
        row = con.execute("SELECT picks FROM newspaper_stocks_picked WHERE pick_date=?", (week,)).fetchone()
        if row:
            picks = json.loads(row[0])
            with _picks_lock: _picks_cache[week] = picks
            return picks

    if build_if_missing:
        _warm_picks_cache()
        with _picks_lock:
            return _picks_cache.get(week, [])

    return []


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
        return json.loads(row[1]), row[0]
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
<meta name="theme-color" content="#08090A">
<meta name="color-scheme" content="dark">

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
<meta property="og:description" content="Every signal logged when it fires, scored when it closes. Wins and losses both, in public. Rebuilt 6 AM IST.">
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
<link rel="preload" href="/fonts/FiraSans-700.woff2" as="font" type="font/woff2" crossorigin>
<link rel="preload" href="/fonts/JetBrainsMono-400.woff2" as="font" type="font/woff2" crossorigin>
<style>
@font-face{font-family:'Fira Sans';font-style:normal;font-weight:400;font-display:swap;src:url('/fonts/FiraSans-400.woff2') format('woff2');unicode-range:U+0301, U+0400-045F, U+0490-0491, U+04B0-04B1, U+2116}
@font-face{font-family:'Fira Sans';font-style:normal;font-weight:500;font-display:swap;src:url('/fonts/FiraSans-500.woff2') format('woff2');unicode-range:U+0301, U+0400-045F, U+0490-0491, U+04B0-04B1, U+2116}
@font-face{font-family:'Fira Sans';font-style:normal;font-weight:600;font-display:swap;src:url('/fonts/FiraSans-600.woff2') format('woff2');unicode-range:U+0301, U+0400-045F, U+0490-0491, U+04B0-04B1, U+2116}
@font-face{font-family:'Fira Sans';font-style:normal;font-weight:700;font-display:swap;src:url('/fonts/FiraSans-700.woff2') format('woff2');unicode-range:U+0301, U+0400-045F, U+0490-0491, U+04B0-04B1, U+2116}
@font-face{font-family:'Fira Sans';font-style:normal;font-weight:800;font-display:swap;src:url('/fonts/FiraSans-800.woff2') format('woff2');unicode-range:U+0301, U+0400-045F, U+0490-0491, U+04B0-04B1, U+2116}
@font-face{font-family:'JetBrains Mono';font-style:normal;font-weight:400;font-display:swap;src:url('/fonts/JetBrainsMono-400.woff2') format('woff2');unicode-range:U+0301, U+0400-045F, U+0490-0491, U+04B0-04B1, U+2116}
@font-face{font-family:'JetBrains Mono';font-style:normal;font-weight:500;font-display:swap;src:url('/fonts/JetBrainsMono-500.woff2') format('woff2');unicode-range:U+0301, U+0400-045F, U+0490-0491, U+04B0-04B1, U+2116}
@font-face{font-family:'JetBrains Mono';font-style:normal;font-weight:700;font-display:swap;src:url('/fonts/JetBrainsMono-700.woff2') format('woff2');unicode-range:U+0301, U+0400-045F, U+0490-0491, U+04B0-04B1, U+2116}
</style>
<style>
/* ═══════════════════ TOKENS ═══════════════════ */
:root{
  --bg:#08090A; --bg2:#0B0C0E; --surface:#121316; --surface2:#17181C;
  --line:rgba(255,255,255,.08); --line2:rgba(255,255,255,.15);
  --lime:#B8EF43; --lime-soft:rgba(184,239,67,.12); --lime-line:rgba(184,239,67,.3);
  /* Contrast measured against --bg #08090A with full alpha compositing.
     --dim was #5A6068 = 3.14:1, which failed WCAG AA (4.5:1) on the ~35
     places it carries timestamps, captions and table meta. --muted was
     6.20:1 — AA but not AAA. Both lifted; the brutalist look survives
     because this is an 8% lightening of grey on black. */
  --text:#F0F0F0; --muted:#9AA1AB; --dim:#7B8390;
  --up:#3DDC97; --down:#FF5C5C; --gold:#E8C547; --blue:#6AA8FF; --violet:#A78BFA;
  --mono:'JetBrains Mono',ui-monospace,monospace;
  --sans:'Fira Sans',-apple-system,BlinkMacSystemFont,sans-serif;
  --ease:cubic-bezier(.22,1,.36,1);
  --gut:clamp(16px,4vw,40px);
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth;scroll-padding-top:var(--headh,200px);-webkit-text-size-adjust:100%}
/* overflow-x:clip, never hidden. `hidden` turns <body> into a scroll container,
   which silently scopes every position:sticky to it instead of the viewport —
   that is what stopped the header and nav from staying put on scroll. `clip`
   contains the same overflow without creating that container. */
body{background:var(--bg);color:var(--text);font-family:var(--sans);font-size:15px;
  line-height:1.6;font-weight:400;overflow-x:clip;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
.up{color:var(--up)} .dn{color:var(--down)}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums}
::selection{background:var(--lime);color:#000}
::-webkit-scrollbar{width:9px;height:9px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:#232529;border-radius:9px}
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
.brand{display:flex;align-items:baseline;gap:9px;font-weight:700;font-size:17px;letter-spacing:-.4px;white-space:nowrap;}
.brand b{color:var(--lime);font-weight:800}
.brand .dot{width:6px;height:6px;border-radius:50%;background:var(--lime);align-self:center;
  animation:pulse 2.4s var(--ease) infinite;}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(.7)}}
.stamp{display:flex;align-items:center;gap:14px;font-family:var(--mono);font-size:10.5px;
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
.nav a i{font-style:normal;font-family:var(--mono);font-size:9px;color:#33363c;margin-right:5px;transition:color .25s}
.nav a::after{content:'';position:absolute;left:13px;right:13px;bottom:0;height:2px;background:var(--lime);
  transform:scaleX(0);transform-origin:left;transition:transform .35s var(--ease);}
/* Group label. Not a link and not focusable — it names the run of links after
   it so eleven equal-weight items read as five decisions instead of eleven.
   aria-hidden on the element, with the group folded into each link's
   aria-label, because a screen reader hitting a bare orphan word between
   links learns nothing from it. */
.nav-g{display:flex;align-items:center;padding:11px 11px 11px 16px;font-family:var(--mono);
  font-size:8.5px;font-weight:600;letter-spacing:1.6px;text-transform:uppercase;
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
.orb.a{width:min(46vw,520px);aspect-ratio:1;background:rgba(184,239,67,.11);top:-14%;right:-8%;animation:drift 22s ease-in-out infinite;}
.orb.b{width:min(34vw,380px);aspect-ratio:1;background:rgba(106,168,255,.07);bottom:-20%;left:-6%;animation:drift 28s ease-in-out infinite reverse;}
@keyframes drift{0%,100%{transform:translate(0,0)}50%{transform:translate(-6%,7%)}}
.eyebrow{display:inline-flex;align-items:center;gap:10px;font-family:var(--mono);font-size:10.5px;
  letter-spacing:2.4px;text-transform:uppercase;color:var(--lime);border:1px solid var(--lime-line);
  background:var(--lime-soft);padding:6px 13px;border-radius:100px;margin-bottom:26px;}
h1.hl{font-size:clamp(40px,8.2vw,94px);line-height:.94;font-weight:800;letter-spacing:-3px;
  max-width:15ch;margin-bottom:22px;}
h1.hl .w{display:inline-block;overflow:hidden;vertical-align:top}
h1.hl .w>span{display:inline-block;transform:translateY(105%);opacity:0;
  animation:rise .9s var(--ease) forwards;animation-delay:var(--d,0s);}
@keyframes rise{to{transform:translateY(0);opacity:1}}
h1.hl em{font-style:normal;color:var(--lime)}
.hero-sub{font-size:clamp(15px,1.7vw,19px);color:var(--muted);max-width:52ch;line-height:1.6;
  opacity:0;animation:fadeUp .8s var(--ease) .7s forwards;}
@keyframes fadeUp{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:none}}

/* hero stat rail */
.statrail{display:flex;flex-wrap:wrap;gap:0;margin-top:clamp(32px,5vw,54px);
  border-top:1px solid var(--line);opacity:0;animation:fadeUp .8s var(--ease) .9s forwards;}
.stat{flex:1 1 150px;padding:20px 22px 20px 0;border-right:1px solid var(--line);}
.stat:last-child{border-right:none}
.stat .v{font-family:var(--mono);font-size:clamp(26px,3.4vw,40px);font-weight:700;letter-spacing:-1.5px;line-height:1;}
.stat .k{font-size:10.5px;letter-spacing:1.8px;text-transform:uppercase;color:var(--dim);margin-top:9px;font-weight:500;}
/* The sample a headline rate rests on, carried by the rate itself. */
.stat .kn{font-family:var(--mono);font-size:10px;color:var(--dim);margin-top:5px;letter-spacing:.3px}
@media(max-width:640px){.stat{flex:1 1 44%;padding:16px 14px 16px 0}}

/* ═══════════════════ TODAY IN 60 SECONDS ═══════════════════ */
.brief{margin-top:clamp(26px,4vw,40px);border:1px solid var(--line);border-radius:10px;
  background:var(--card,rgba(255,255,255,.015));overflow:hidden}
.brief-h{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;
  padding:13px 18px;border-bottom:1px solid var(--line)}
.brief-t{font-family:var(--mono);font-size:10.5px;font-weight:700;letter-spacing:2px;
  text-transform:uppercase;color:var(--lime)}
.brief-d{font-family:var(--mono);font-size:10.5px;color:var(--dim);letter-spacing:.4px}

.regime{padding:15px 18px;border-bottom:1px solid var(--line)}
.rg-l{display:flex;justify-content:space-between;align-items:baseline;gap:12px}
.rg-k{font-size:10.5px;letter-spacing:1.6px;text-transform:uppercase;color:var(--dim);font-weight:500}
.rg-v{font-family:var(--mono);font-size:15px;font-weight:700;letter-spacing:-.3px;color:var(--muted)}
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
.brief-l li{display:flex;gap:12px;padding:10px 18px;font-size:13.5px;line-height:1.6;color:var(--muted)}
.brief-l li + li{border-top:1px solid rgba(255,255,255,.04)}
.brief-l .bn{font-family:var(--mono);font-size:10px;color:#4A4F57;padding-top:3px;flex:0 0 auto}
.brief-l b{color:var(--text);font-weight:600}
.brief-l .up{color:var(--up)} .brief-l .dn{color:var(--down)}
.brief-l a{color:var(--lime);white-space:nowrap;border-bottom:1px solid rgba(184,239,67,.28)}
.brief-l a:hover{border-bottom-color:var(--lime)}
@media(max-width:640px){
  .brief-l li{padding:10px 14px;font-size:13px}
  .brief-h,.regime{padding-left:14px;padding-right:14px}
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
  background:var(--lime);color:#000;font-family:var(--mono);font-size:13px;
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
  padding:18px 20px;color:var(--text);font-family:var(--mono);font-size:15px;outline:none}
.cmdk-box input::placeholder{color:var(--dim)}
.cmdk-list{list-style:none;margin:0;padding:6px;max-height:46vh;overflow-y:auto}
.cmdk-list li{display:flex;align-items:center;gap:11px;padding:10px 14px;border-radius:9px;
  cursor:pointer;min-height:24px}
.cmdk-list li[aria-selected="true"]{background:var(--lime-soft)}
.cmdk-list li .k{font-family:var(--mono);font-size:9px;letter-spacing:1.2px;text-transform:uppercase;
  color:var(--dim);border:1px solid var(--line);border-radius:4px;padding:2px 6px;flex:none}
.cmdk-list li .t{flex:1;font-size:14px;color:var(--text)}
.cmdk-list li .m{font-family:var(--mono);font-size:11px;color:var(--dim)}
.cmdk-list li[aria-selected="true"] .t{color:var(--lime)}
.cmdk-empty{padding:22px;text-align:center;color:var(--dim);font-family:var(--mono);font-size:12px}
.cmdk-ft{display:flex;gap:16px;padding:10px 18px;border-top:1px solid var(--line);
  font-family:var(--mono);font-size:10px;color:var(--dim)}
.cmdk-ft kbd{background:var(--bg2);border:1px solid var(--line);border-radius:4px;
  padding:1px 5px;margin-right:4px}
.cmdk-hint{display:inline-flex;align-items:center;gap:5px;font-family:var(--mono);font-size:10px;
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
.ti .n{font-size:10px;letter-spacing:1.4px;text-transform:uppercase;color:var(--dim);font-weight:500}
.ti .p{font-family:var(--mono);font-size:13px;font-weight:600}
.ti .c{font-family:var(--mono);font-size:12px;font-weight:700}
.ti .note{font-family:var(--mono);font-size:10px;color:var(--gold);letter-spacing:.5px}

/* Segment head — the thing that makes a 110-item rail readable instead of
   an undifferentiated stream of numbers. */
.tseg{display:flex;align-items:center;gap:8px;padding:8px 18px 8px 20px;white-space:nowrap;
  border-right:1px solid var(--line);background:
    linear-gradient(90deg,color-mix(in srgb,var(--sc,var(--lime)) 16%,transparent),transparent);}
.tseg .ic{font-size:13px;line-height:1}
.tseg .lb{font-family:var(--mono);font-size:10px;font-weight:700;letter-spacing:2px;
  text-transform:uppercase;color:var(--sc,var(--lime))}
.tseg::before{content:'';width:3px;height:15px;border-radius:2px;background:var(--sc,var(--lime))}
.ti.hi .p{color:var(--sc,inherit)}

/* The rail is the only market surface on the page now, so it gets a control:
   the whole strip is a live region a reader can freeze to actually read it. */
.tickctl{position:absolute;right:8px;top:50%;transform:translateY(-50%);z-index:4;
  font-family:var(--mono);font-size:9px;letter-spacing:1px;text-transform:uppercase;
  background:var(--bg2);color:var(--dim);border:1px solid var(--line);border-radius:99px;
  padding:4px 10px;cursor:pointer}
.tickctl:hover{color:var(--lime);border-color:var(--lime)}
/* The pause control stays at every width. It used to be hidden below 640px,
   which removed the only way to stop an infinite marquee on exactly the
   devices where that matters most — WCAG 2.2.2, Level A. */
@media(max-width:640px){.tickctl{padding:5px 9px;font-size:9px;right:6px}}
/* Phone: the rail STAYS. Hiding it was the wrong trade — on a phone the
   ticker is the single most-wanted row on the page, and "the numbers are a
   scroll away" is exactly the scroll a market rail exists to save.
   The header pays for it instead: the topbar shrinks, and the livebar (a long
   diagnostic string that ellipsises to nothing useful at this width anyway)
   drops out. Net sticky height is lower than before AND the prices are there. */
@media(max-width:560px){
  .headstack .tickwrap{display:block}
  .topbar-in{height:48px}
  .brand{font-size:15px}
  .stamp .d{display:none}          /* the date is in the hero eyebrow already */
  .ti{padding:7px 14px;gap:7px}
  .ti .n{font-size:9px;letter-spacing:1px}
  .ti .p{font-size:12px}
  .ti .c{font-size:11px}
  .tseg{padding:7px 12px 7px 14px}
  .tseg .lb{font-size:9px;letter-spacing:1.4px}

  /* Scrolling DOWN collapses the chrome and keeps only the prices pinned;
     scrolling UP brings the whole header back. Standard mobile pattern, and it
     is what makes a four-row sticky stack affordable on a 390px screen:
     ~146px of chrome while reading becomes ~30px of ticker. */
  .headstack .topbar,
  .headstack .nav,
  .headstack .livebar{transition:margin-top .22s var(--ease),opacity .18s linear}
  .headstack.compact .topbar{margin-top:-48px}
  .headstack.compact .nav,
  .headstack.compact .livebar{opacity:0;pointer-events:none;
    margin-top:0;height:0;overflow:hidden;border:0}
}
@media(prefers-reduced-motion:reduce){
  .headstack .topbar,.headstack .nav,.headstack .livebar{transition:none}
}

/* ═══════════════════ WORLD MAP ═══════════════════ */
.wmap-wrap{margin:0 0 30px;background:var(--surface);border:1px solid var(--line);
  border-radius:16px;overflow:hidden}
.wmap-head{display:flex;justify-content:space-between;align-items:center;gap:12px;
  flex-wrap:wrap;padding:12px 16px;border-bottom:1px solid var(--line)}
.wm-t{font-family:var(--mono);font-size:10px;letter-spacing:2px;text-transform:uppercase;
  color:var(--dim)}
.wm-legend{display:flex;align-items:center;gap:14px;font-family:var(--mono);font-size:10px;
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
.wm-tip .c{font-family:var(--mono);font-size:10px;letter-spacing:1.4px;text-transform:uppercase;
  color:var(--lime);margin-bottom:5px}
.wm-tip .h{font-size:12.5px;line-height:1.45;color:var(--text)}
.wm-tip .m{font-family:var(--mono);font-size:10px;color:var(--dim);margin-top:6px}
.wm-foot{padding:10px 16px;border-top:1px solid var(--line);font-family:var(--mono);
  font-size:10px;letter-spacing:.5px;color:var(--dim)}
@media(prefers-reduced-motion:reduce){.wmap circle.halo{animation:none;opacity:.25}}
@media(max-width:640px){.wm-legend{font-size:9px;gap:9px}}

.ncard .tone{font-family:var(--mono);font-size:9px;letter-spacing:1px;text-transform:uppercase;
  padding:2px 7px;border-radius:99px;margin-left:8px;vertical-align:middle}
.ncard .tone.red{background:rgba(255,92,92,.16);color:var(--down)}
.ncard .tone.green{background:rgba(60,220,130,.14);color:var(--up)}

.ltgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}
.lt{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:20px;
  transition:border-color .35s,transform .35s var(--ease)}
.lt:hover{transform:translateY(-3px);border-color:var(--line2)}
.lt-h{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.lt .sec-l{font-family:var(--mono);font-size:10px;color:var(--dim);letter-spacing:1px;
  text-transform:uppercase;margin-top:3px}
.lt .px{font-family:var(--mono);font-size:23px;font-weight:700;letter-spacing:-.6px;margin:12px 0 8px}
.lt .th{font-size:13px;line-height:1.55;color:var(--muted);margin:10px 0}
.lt .facts{font-family:var(--mono);font-size:10.5px;line-height:1.6;color:var(--dim);
  padding:8px 10px;background:var(--bg2);border-radius:8px}
.lt .lvl{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:14px}
.lt .lvl .k{font-family:var(--mono);font-size:9.5px;color:var(--dim);letter-spacing:.6px}
.lt .lvl .v{font-family:var(--mono);font-size:12.5px;font-weight:700;margin-top:3px}
.lt .lvl .pc{display:block;font-size:9.5px;font-weight:500;opacity:.8}
.lt-f{margin-top:14px;padding-top:11px;border-top:1px solid var(--line);
  font-family:var(--mono);font-size:9.5px;color:var(--dim);letter-spacing:.4px}
@media(max-width:520px){.ltgrid{grid-template-columns:1fr}}

/* Single column since the stat tiles came out — a 1.6fr/1fr grid with one
   child leaves 38% of the row empty. */
.who{display:block}
.who-m{background:var(--surface);border:1px solid var(--line);border-left:3px solid var(--lime);
  border-radius:16px;padding:26px}
.who-name{font-size:27px;font-weight:800;letter-spacing:-1px}
.who-role{font-family:var(--mono);font-size:11px;letter-spacing:2px;text-transform:uppercase;
  color:var(--lime);margin:6px 0 16px}
.who-m p{font-size:14.5px;line-height:1.65;color:var(--muted);margin-bottom:12px}
.who-m .who-sub{font-size:13px;color:var(--dim)}
.who-links{display:flex;flex-wrap:wrap;gap:9px;margin-top:18px}
.who-links a{font-family:var(--mono);font-size:11px;letter-spacing:.6px;color:var(--lime);
  border:1px solid var(--lime-line);border-radius:99px;padding:6px 13px;transition:background .25s}
.who-links a:hover{background:var(--lime-soft)}

/* ═══════════════════ SUBSCRIBE ═══════════════════ */
.sub-cta{background:var(--surface);border:1px solid var(--line);border-left:3px solid var(--lime);
  border-radius:16px;padding:26px 28px;margin:34px 0;
  display:grid;grid-template-columns:1.15fr 1fr;gap:26px;align-items:center}
.sub-cta h3{font-size:21px;font-weight:750;letter-spacing:-.5px;margin:0 0 8px;text-wrap:balance}
.sub-cta p{font-size:14px;line-height:1.6;color:var(--muted);margin:0}
.sub-cta .fine{font-family:var(--mono);font-size:10.5px;color:var(--dim);margin-top:11px;line-height:1.5}
.sub-form{display:flex;gap:9px;flex-wrap:wrap}
.sub-form input[type=email]{flex:1 1 210px;min-width:0;background:var(--bg2);
  border:1px solid var(--line2);border-radius:10px;padding:13px 15px;color:var(--text);
  font-family:var(--mono);font-size:13.5px;min-height:46px}
.sub-form input[type=email]::placeholder{color:var(--dim)}
.sub-form input[type=email]:focus{border-color:var(--lime);outline:none}
.sub-form button{background:var(--lime);color:#000;border:none;border-radius:10px;
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
  font-family:var(--mono);font-size:12.5px;color:var(--lime);min-height:24px;
  transition:background .2s}
.tg-cta:hover{background:var(--lime-soft)}
@media(max-width:760px){.sub-cta{grid-template-columns:1fr;gap:18px;padding:22px}}

.foot-legal{display:grid;grid-template-columns:1.4fr 1.4fr 1fr;gap:28px;
  max-width:1400px;margin:34px auto 0;padding:26px var(--gut) 0;
  border-top:1px solid var(--line)}
.foot-legal h4{font-family:var(--mono);font-size:10px;letter-spacing:1.6px;text-transform:uppercase;
  color:var(--lime);margin:0 0 9px}
.foot-legal p{font-size:12.5px;line-height:1.65;color:var(--dim);margin:0}
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
.crate-h .ic{font-size:15px;line-height:1}
.crate-h .nm{font-family:var(--mono);font-size:11px;letter-spacing:2px;text-transform:uppercase;
  color:var(--text);font-weight:700;flex:1}
.crate-h .ct{font-family:var(--mono);font-size:10px;color:var(--dim);
  border:1px solid var(--line);border-radius:99px;padding:2px 9px}
.crate-l{list-style:none;margin:0;padding:0}
.trk{display:flex;align-items:center;gap:12px;padding:0 18px;border-bottom:1px solid var(--line)}
.trk:last-child{border-bottom:none}
.trk.more{display:none}
.crate.open .trk.more{display:flex;animation:trkIn .32s var(--ease) both}
@keyframes trkIn{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:none}}
.trk .no{font-family:var(--mono);font-size:10.5px;color:var(--dim);width:20px;flex:none}
.trk a{flex:1;display:flex;flex-direction:column;gap:2px;padding:11px 0;min-height:24px;
  text-decoration:none}
.trk .ti{font-size:14px;font-weight:500;color:var(--text);line-height:1.35}
.trk .ar{font-family:var(--mono);font-size:10.5px;color:var(--dim);letter-spacing:.3px}
.trk .pl{font-size:10px;color:var(--dim);flex:none;transition:color .2s,transform .2s var(--ease)}
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
.sheet-sym{font-size:22px;font-weight:700;letter-spacing:-.5px}
.sheet-kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:1px;
  background:var(--line);border:1px solid var(--line);border-radius:10px;overflow:hidden;margin-bottom:26px}
.sheet-kpi>div{background:var(--surface);padding:14px}
.sheet-kpi b{display:block;font-family:var(--mono);font-size:18px}
.sheet-kpi span{display:block;font-size:10.5px;color:var(--dim);margin-top:4px;
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
.scale .lv-l{display:block;font-family:var(--mono);font-size:9.5px;letter-spacing:.8px;
  text-transform:uppercase;color:var(--dim)}
.scale .lv-v{display:block;font-family:var(--mono);font-size:11.5px;color:var(--text)}
.scale .lv-r{display:block;font-family:var(--mono);font-size:10px;color:var(--dim)}
.scale .sl i{background:var(--down)} .scale .sl .lv-l{color:var(--down)}
.scale .t1 i,.scale .t2 i{background:var(--up)} .scale .t1 .lv-l,.scale .t2 .lv-l{color:var(--up)}
.scale .en i{background:var(--text);height:52px} .scale .en .lv-l{color:var(--text)}
.scale .ex i{background:var(--gold);height:68px} .scale .ex .lv-l{color:var(--gold)}
.scale-note{font-family:var(--mono);font-size:10px;color:var(--dim);margin:0 0 22px}
.sheet-tl{border-left:2px solid var(--line2);padding-left:16px;margin-bottom:20px}
.tl-row{display:grid;grid-template-columns:120px 150px 1fr;gap:10px;padding:7px 0;
  font-family:var(--mono);font-size:11.5px;align-items:baseline}
.tl-k{color:var(--text)} .tl-v{color:var(--lime)} .tl-w{color:var(--dim)}
.sheet-flags{background:var(--bg2);border-left:2px solid var(--gold);border-radius:0 8px 8px 0;
  padding:12px 16px;margin-bottom:20px}
.sheet-flags p{margin:0 0 8px;font-family:var(--mono);font-size:11.5px;line-height:1.6;color:var(--gold)}
.sheet-flags p:last-child{margin-bottom:0}
/* "Why this fired" — the engine's own gates, rendered from metadata. */
.sheet-why{border:1px solid var(--line2);border-radius:8px;padding:14px 16px;margin-bottom:20px}
.sheet-why h4{margin:0 0 10px;font-family:var(--mono);font-size:10px;letter-spacing:1.2px;
  text-transform:uppercase;color:var(--dim)}
.wy-p{margin:0 0 10px;font-size:12.5px;line-height:1.65;color:var(--muted)}
.wy-p:last-child{margin-bottom:0}
.wy-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:6px 18px}
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
.sizer-h{font-family:var(--mono);font-size:10.5px;letter-spacing:1.3px;text-transform:uppercase;
  color:var(--dim);margin-bottom:12px}
.sizer-in{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px}
.sizer-in label{display:flex;flex-direction:column;gap:5px;font-family:var(--mono);
  font-size:10px;letter-spacing:.8px;text-transform:uppercase;color:var(--dim);flex:1;min-width:120px}
.sizer-in input{background:var(--surface);border:1px solid var(--line2);border-radius:8px;
  color:var(--text);font-family:var(--mono);font-size:14px;padding:9px 11px;min-height:42px;width:100%}
.sizer-in input:focus{outline:none;border-color:var(--lime)}
.sz-row{display:flex;justify-content:space-between;gap:12px;padding:6px 0;
  font-family:var(--mono);font-size:12.5px;border-bottom:1px solid var(--line)}
.sz-row:last-of-type{border-bottom:none}
.sz-row span{color:var(--dim)} .sz-row b{color:var(--text)}
.sz-note{font-family:var(--mono);font-size:10.5px;color:var(--dim);margin:10px 0 0;line-height:1.6}
.sz-warn{font-family:var(--mono);font-size:11px;color:var(--gold);margin:10px 0 0;line-height:1.6}
.heat{margin:18px 0 22px;padding:16px 18px;border:1px solid var(--line);border-radius:12px;
  background:var(--surface)}
.heat-h{display:flex;justify-content:space-between;gap:12px;margin-bottom:6px}
.heat-h .eyebrow{font-family:var(--mono);font-size:10.5px;letter-spacing:1.3px;
  text-transform:uppercase;color:var(--dim)}
.heat-n{font-family:var(--mono);font-size:32px;font-weight:600;color:var(--lime);line-height:1.1}
.heat-n.hot{color:var(--down)}
.heat-w{font-family:var(--mono);font-size:11.5px;line-height:1.7;color:var(--muted);
  margin:8px 0 0;max-width:82ch}
.heat-w b{color:var(--text)}
.heat-g{display:flex;gap:26px;flex-wrap:wrap;margin-top:10px}
.heat-g>div{display:flex;flex-direction:column;gap:3px}
.heat-g span{font-family:var(--mono);font-size:9.5px;letter-spacing:1px;
  text-transform:uppercase;color:var(--dim)}
.heat-g b{font-family:var(--mono);font-size:11.5px;color:var(--muted);font-weight:400}
/* Distance from entry, under the entry price. Green within 1% (live), grey to
   4%, red beyond (already gone). */
.dist{font-family:var(--mono);font-size:9.5px;color:var(--dim);margin-top:2px}
.dist.up{color:var(--up)} .dist.dn{color:var(--down)}

/* ═══════════════════ UNDERWATER ═══════════════════ */
.uw{margin:22px 0 6px}
.uw-h{display:flex;justify-content:space-between;gap:12px;margin-bottom:6px}
.uw-h .eyebrow{font-family:var(--mono);font-size:10.5px;letter-spacing:1.3px;
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
.fundcat-h h3{font-size:17px;margin:0}
.fundcat-b{color:var(--muted);font-size:13px;margin:4px 0 12px;max-width:74ch}

/* Small-sample warning on the performance section. Gold, not red: this is not
   an error, it is a true statement about how little data there is. */
.thin-warn{font-family:var(--mono);font-size:12px;line-height:1.65;
  color:var(--gold);background:var(--bg2);border-left:2px solid var(--gold);
  border-radius:0 8px 8px 0;padding:12px 16px;margin:14px 0 0;max-width:78ch}

/* ═══════════════════ SWP ═══════════════════ */
.swp-in{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:12px;
  margin-bottom:22px}
.swp-in label{display:flex;flex-direction:column;gap:6px;font-family:var(--mono);
  font-size:10.5px;letter-spacing:1.2px;text-transform:uppercase;color:var(--dim)}
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
  font-family:var(--mono);font-size:10.5px;letter-spacing:1px;text-transform:uppercase;
  padding:9px 13px;min-height:38px;transition:background .18s,color .18s}
.swp-toggle button.on{background:var(--lime);color:var(--bg)}
#swpChart{display:block;width:100%;height:auto;margin-top:6px}
.legend{display:flex;flex-wrap:wrap;gap:18px;margin-top:12px;font-family:var(--mono);
  font-size:10.5px;letter-spacing:.6px;color:var(--dim)}
.legend .sw{display:inline-block;width:11px;height:3px;border-radius:2px;
  margin-right:7px;vertical-align:middle}
.cardhead{display:flex;justify-content:space-between;align-items:center;gap:12px;
  margin-bottom:10px;flex-wrap:wrap}
.cardhead .eyebrow{font-family:var(--mono);font-size:10.5px;letter-spacing:1.4px;
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
  color:var(--dim);cursor:pointer;font-family:var(--mono);font-size:10.5px;
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
.player-t{flex:1;font-size:12.5px;font-weight:500;color:var(--text);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.player-y{font-family:var(--mono);font-size:10px;letter-spacing:.6px;
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
  color:var(--dim);font-family:var(--mono);font-size:10.5px;letter-spacing:1.4px;
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
.herocurve{margin-top:34px;max-width:640px;background:var(--surface);
  border:1px solid var(--line);border-radius:14px;overflow:hidden}
.herocurve[hidden]{display:none}
.hc-h{display:flex;justify-content:space-between;align-items:baseline;
  padding:12px 16px 8px}
.hc-t{font-family:var(--mono);font-size:10px;letter-spacing:1.6px;text-transform:uppercase;
  color:var(--dim)}
.hc-v{font-family:var(--mono);font-size:17px;font-weight:700;color:var(--lime)}
.herocurve svg{display:block;width:100%;height:96px}
#hcLine{stroke-dasharray:var(--len,0);stroke-dashoffset:var(--len,0);
  animation:hcDraw 1.1s var(--ease) .25s forwards}
@keyframes hcDraw{to{stroke-dashoffset:0}}
#hcDot{opacity:0;animation:hcPop .3s var(--ease) 1.3s forwards}
@keyframes hcPop{to{opacity:1}}
.hc-f{display:flex;justify-content:space-between;gap:10px;padding:8px 16px 12px;
  font-family:var(--mono);font-size:9.5px;color:var(--dim);letter-spacing:.4px}
@media(prefers-reduced-motion:reduce){
  #hcLine{animation:none;stroke-dashoffset:0}#hcDot{animation:none;opacity:1}
}
@media(max-width:640px){.herocurve{margin-top:26px}.herocurve svg{height:74px}}

/* Trade idea symbols open the chart, same as the long-term cards. The name
   was plain text on the one card type where you most want the chart. */
.pick .sym a{color:inherit;text-decoration:none;border-bottom:1px solid transparent;
  transition:color .2s,border-color .2s}
.pick .sym a:hover{color:var(--lime);border-bottom-color:var(--lime-line)}
.pick .sym a::after{content:' ↗';font-size:.62em;opacity:.5;vertical-align:super}

/* ═══════════════════ SECTIONS ═══════════════════ */
main{position:relative;z-index:2;max-width:1400px;margin:0 auto;padding:0 var(--gut)}
.sec{padding:clamp(56px,8vw,104px) 0;border-bottom:1px solid var(--line)}
.sec:last-child{border-bottom:none}
.shead{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;flex-wrap:wrap;margin-bottom:clamp(26px,4vw,44px)}
.snum{font-family:var(--mono);font-size:11px;color:var(--lime);letter-spacing:2px;margin-bottom:12px;display:block}
.stitle{font-size:clamp(26px,4.4vw,50px);font-weight:700;letter-spacing:-1.8px;line-height:1}
.sdesc{font-size:13px;color:var(--muted);max-width:44ch;line-height:1.55}
/* Provenance strip. Every weekly artefact on this page showed its RESULT and
   not its VINTAGE, so "ran and found the same funds" and "did not run at all"
   rendered identically. One strip, stated the same way in every section that
   is rebuilt on a clock slower than the page. */
.prov{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;margin-top:12px;
  font-family:var(--mono);font-size:10px;letter-spacing:.6px;color:var(--dim)}
.prov b{color:var(--muted);font-weight:500}
.prov .pv-tag{border:1px solid var(--line2);border-radius:999px;padding:3px 9px}
.prov.stale{color:var(--gold)}
.prov.stale .pv-tag{border-color:var(--gold);color:var(--gold)}
/* ── read-more affordance, shared by news cards and smart reads ── */
.readmore{display:inline-block;margin-top:10px;font-family:var(--mono);font-size:10px;
  letter-spacing:1.1px;text-transform:uppercase;color:var(--lime);text-decoration:none;
  border-bottom:1px solid transparent}
.readmore:hover{border-bottom-color:var(--lime)}
.mini-s{margin:5px 0 0;font-size:12px;line-height:1.5;color:var(--muted)}
.ncard-f{display:flex;justify-content:space-between;align-items:center;gap:10px;
  margin-top:auto;padding-top:10px;flex-wrap:wrap}
.ncard-f .readmore{margin-top:0}
/* ── smart reads ── */
.sr-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
.sr{border:1px solid var(--line);border-radius:14px;padding:16px 18px;background:var(--bg2);
  display:flex;flex-direction:column}
.sr-h{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-bottom:9px;
  font-family:var(--mono);font-size:9px;letter-spacing:1.1px;text-transform:uppercase}
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
.sr-t{margin:0 0 8px;font-size:14.5px;line-height:1.4;font-weight:650}
.sr-t a{color:var(--fg);text-decoration:none;border-bottom:1px solid transparent}
.sr-t a:hover{color:var(--lime);border-bottom-color:var(--lime)}
.sr-s{margin:0;font-size:12.5px;line-height:1.6;color:var(--muted)}
.sr .readmore{margin-top:auto;padding-top:12px}
@media(max-width:640px){.sr-grid{grid-template-columns:1fr}}
/* ── podcasts ── */
.pod-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}
.pod{border:1px solid var(--line);border-radius:14px;padding:16px 18px;background:var(--bg2);
  display:flex;flex-direction:column;gap:8px}
.pod-h{display:flex;justify-content:space-between;align-items:center;gap:10px;
  font-family:var(--mono);font-size:9.5px;letter-spacing:1.1px;text-transform:uppercase}
.pod-cat{color:var(--lime);border:1px solid var(--line2);border-radius:999px;padding:3px 9px}
.pod-date{color:var(--dim)}
.pod-t{margin:0;font-size:14.5px;line-height:1.4;font-weight:600}
.pod-t a{color:var(--fg);text-decoration:none;border-bottom:1px solid transparent}
.pod-t a:hover{border-bottom-color:var(--lime);color:var(--lime)}
.pod-s{font-family:var(--mono);font-size:10.5px;color:var(--muted);letter-spacing:.4px}
.pod-s b{color:var(--fg);font-weight:500}
.pod-k{margin:2px 0 0;padding-left:16px;display:flex;flex-direction:column;gap:6px}
.pod-k li{font-size:12.5px;line-height:1.55;color:var(--muted)}
.pod-k li::marker{color:var(--lime)}
.pod-note{margin:16px 0 0;font-family:var(--mono);font-size:10px;color:var(--dim);line-height:1.6}
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
.tag{display:inline-block;font-family:var(--mono);font-size:9.5px;letter-spacing:1.4px;text-transform:uppercase;
  padding:3px 8px;border-radius:5px;background:var(--lime-soft);color:var(--lime);border:1px solid var(--lime-line)}
</style>
<style>
/* The .mkt / .mkt-grid rules that lived here belonged to the "What moved"
   section, which duplicated the ticker and has been removed. */

/* ═══════════════════ 01 PICKS ═══════════════════ */
.pick-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px}
.pick{position:relative;background:linear-gradient(160deg,var(--surface),#0E0F12);border:1px solid var(--line);
  border-radius:18px;padding:22px;overflow:hidden;transition:border-color .35s,transform .35s var(--ease)}
.pick:hover{border-color:var(--lime-line);transform:translateY(-4px)}
.pick .rank{position:absolute;top:-14px;right:10px;font-family:var(--mono);font-size:64px;font-weight:700;
  color:rgba(255,255,255,.035);line-height:1;pointer-events:none}
.pick .sym{font-family:var(--mono);font-size:17px;font-weight:700;letter-spacing:-.4px}
.pick .px{font-family:var(--mono);font-size:30px;font-weight:700;letter-spacing:-1.6px;margin:6px 0 2px}
.pick .mom{display:flex;gap:12px;font-family:var(--mono);font-size:11px;color:var(--muted);margin-top:6px;flex-wrap:wrap}
.pick .mom b{font-weight:600}
.pick .th{font-size:12.5px;color:#B4BAC2;line-height:1.6;margin:14px 0;font-style:italic;
  border-left:2px solid var(--line2);padding-left:11px}
.lvl{display:flex;gap:10px;margin-top:14px;padding-top:14px;border-top:1px solid var(--line)}
.lvl>div{flex:1}
.lvl .k{font-size:9.5px;letter-spacing:1.4px;text-transform:uppercase;color:var(--dim);margin-bottom:3px}
.lvl .v{font-family:var(--mono);font-size:14px;font-weight:700}
.scorebar{height:3px;background:rgba(255,255,255,.06);border-radius:3px;overflow:hidden;margin-top:12px}
.scorebar i{display:block;height:100%;background:linear-gradient(90deg,var(--lime),#7ED321);width:0;
  transition:width 1.2s var(--ease) .2s;border-radius:3px}
.rv.in .scorebar i{width:var(--w)}

/* Score breakdown. <details> rather than a click handler so it works with the
   script blocked, which is the same reader the SSR fixes above are for. */
.why{margin-top:12px}
.why summary{cursor:pointer;list-style:none;font-family:var(--mono);font-size:10.5px;
  letter-spacing:1.2px;text-transform:uppercase;color:var(--dim);padding:5px 0;
  transition:color .2s var(--ease)}
.why summary::-webkit-details-marker{display:none}
.why summary::after{content:' ▾';font-size:9px}
.why[open] summary::after{content:' ▴'}
.why summary:hover{color:var(--lime)}
.why summary span{color:#4A4F57}
.why-b{padding:4px 0 2px;display:grid;gap:5px}
.why-r{display:grid;grid-template-columns:1fr 54px auto;align-items:center;gap:8px;font-size:11px}
.why-r .wk{color:var(--muted)}
.why-r .wb{height:3px;border-radius:2px;background:rgba(255,255,255,.07);overflow:hidden}
.why-r .wb i{display:block;height:100%;width:var(--w);background:var(--lime);opacity:.75;border-radius:2px}
.why-r .wn{font-family:var(--mono);font-size:10.5px;color:var(--text);text-align:right}
.why-r .wn em{font-style:normal;color:#4A4F57}

/* The level that ends the idea. Gold, not red — it has not happened. */
.inval{margin-top:12px;font-size:11.5px;line-height:1.6;color:var(--muted);
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
.scr-breadth .sb-lab{font-family:var(--mono);font-size:9.5px;letter-spacing:1.6px;
  color:var(--dim);border:1px solid var(--line2);border-radius:999px;padding:3px 9px}
.scr-breadth .sb-reg{font-family:var(--mono);font-size:13px;letter-spacing:1.2px;color:var(--lime)}
.scr-breadth .sb-as{font-family:var(--mono);font-size:10.5px;color:var(--dim)}
.scr-breadth .sb-n{display:flex;flex-wrap:wrap;gap:6px 18px;font-family:var(--mono);
  font-size:11.5px;color:var(--muted)}
.scr-breadth .sb-n b{color:var(--text);font-weight:700}

/* AI narrative. Marked as generated, and visually subordinate to the computed
   SWOT above it — the prose is the commentary, the numbers are the evidence. */
.sd-ai{background:rgba(106,168,255,.05);border:1px solid rgba(106,168,255,.22);
  border-radius:10px;padding:13px 15px}
.sd-ai .tag{font-family:var(--mono);font-size:9px;letter-spacing:1.4px;
  color:var(--blue);display:block;margin-bottom:7px}
.sd-ai p{font-size:13px;line-height:1.65;color:var(--muted);margin:0}
.sd-ai .fine{font-family:var(--mono);font-size:10px;color:var(--dim);margin-top:8px;display:block}

/* Peer-median column in the detail sheet's ratio grid. */
.sd-peer{display:grid;grid-template-columns:1fr auto auto;gap:7px 14px;
  font-family:var(--mono);font-size:12px;align-items:baseline}
.sd-peer .h{font-size:9px;letter-spacing:1.3px;color:var(--dim);text-transform:uppercase}
.sd-peer .k{color:var(--muted)}
.sd-peer .v{color:var(--text);text-align:right;font-weight:700}
.sd-peer .m{color:var(--dim);text-align:right}
.sd-peer .v.better{color:var(--up)} .sd-peer .v.worse{color:var(--down)}

/* Risk level. LOW/MEDIUM/HIGH, never a 0-100 — an arbitrary "risk 62" is
   unreadable without also knowing which direction is better. Colour is never the
   only signal: the word is always there. */
.rk{font-family:var(--mono);font-size:9.5px;letter-spacing:1px;font-weight:700;
  border-radius:4px;padding:2px 7px;border:1px solid}
.rk-low{color:var(--up);border-color:rgba(61,220,151,.32);background:rgba(61,220,151,.07)}
.rk-medium{color:var(--gold);border-color:rgba(233,196,106,.34);background:rgba(233,196,106,.07)}
.rk-high{color:var(--down);border-color:rgba(255,92,92,.34);background:rgba(255,92,92,.08)}
.rk-n{font-family:var(--mono);font-size:9.5px;color:var(--dim);margin-left:5px}
/* Earnings momentum: the DIRECTION of the accounts. A level and a direction
   are different facts — a 25% compounder that is slowing and a 12% one that
   is speeding up have the same CAGR column. */
.em{font-family:var(--mono);font-size:9px;letter-spacing:1.2px;font-weight:700;
  border-radius:4px;padding:2px 7px;border:1px solid;margin-left:4px}
.em-accelerating{color:var(--up);border-color:rgba(61,220,151,.34);background:rgba(61,220,151,.07)}
.em-stable{color:var(--dim);border-color:var(--line2)}
.em-decelerating{color:var(--down);border-color:rgba(255,92,92,.34);background:rgba(255,92,92,.07)}
/* Movement since the previous build. A 91 that was a 91 is priced; a 78 that
   was a 61 is a change, and the change is the interesting part. */
.dl{display:block;font-family:var(--mono);font-size:9px;font-style:normal;
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
.wn-col>span{font-family:var(--mono);font-size:9.5px;letter-spacing:1.4px;
  text-transform:uppercase;display:block;margin-bottom:9px}
.wn-for>span{color:var(--up)} .wn-against>span{color:var(--gold)}
.wn-col ul{list-style:none;margin:0;padding:0;display:grid;gap:9px}
.wn-col li{font-size:12.5px;line-height:1.5;color:var(--muted);padding-left:13px;position:relative}
.wn-for li::before{content:"+";position:absolute;left:0;color:var(--up);font-weight:700}
.wn-col li em{display:block;font-family:var(--mono);font-size:10.5px;color:var(--dim);
  font-style:normal;margin-top:3px}
/* Severity on the against side, so a solvency flag does not read like a wide
   spread. Prefix character AND colour, never colour alone. */
.wn-against li{padding-left:15px}
.wn-against li.f-high::before{content:"!!";position:absolute;left:0;color:var(--down);
  font-family:var(--mono);font-size:10px;font-weight:700}
.wn-against li.f-med::before{content:"!";position:absolute;left:0;color:var(--gold);
  font-family:var(--mono);font-weight:700}
.wn-against li.f-low::before{content:"·";position:absolute;left:0;color:var(--dim)}
@media(max-width:700px){.sd-why{grid-template-columns:1fr}}

.scr-tags{display:flex;flex-wrap:wrap;gap:4px}
.scr-tag{font-family:var(--mono);font-size:9px;letter-spacing:.8px;padding:2px 6px;
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

.scr-empty{font-family:var(--mono);font-size:12.5px;color:var(--dim);
  padding:34px 18px;text-align:center}
.scr-more{display:block;width:100%;margin:14px 0 0;padding:13px;
  background:var(--bg2);border:1px solid var(--line);border-radius:12px;
  font-family:var(--mono);font-size:11px;letter-spacing:1.4px;text-transform:uppercase;
  color:var(--muted);cursor:pointer;min-height:44px}
.scr-more:hover{border-color:var(--lime-line);color:var(--lime)}

/* ── detail sheet ── */
.sd-h{display:flex;flex-wrap:wrap;gap:6px 16px;align-items:baseline;margin-bottom:4px}
.sd-h h3{font-size:21px;margin:0;font-family:var(--mono)}
.sd-h .co{color:var(--muted);font-size:13px}
.sd-sub{font-family:var(--mono);font-size:11px;color:var(--dim);letter-spacing:.6px;
  margin-bottom:18px;display:flex;flex-wrap:wrap;gap:4px 12px}
.sd-scores{display:grid;grid-template-columns:repeat(auto-fit,minmax(112px,1fr));
  gap:9px;margin-bottom:20px}
.sd-sc{background:var(--bg2);border:1px solid var(--line);border-radius:10px;padding:11px 13px}
.sd-sc .k{font-family:var(--mono);font-size:9px;letter-spacing:1.3px;text-transform:uppercase;
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
.sd-sc .conf{font-family:var(--mono);font-size:9.5px;color:var(--gold);margin-top:5px;display:block}

.sd-blk{margin:0 0 20px}
.sd-blk h4{font-family:var(--mono);font-size:10px;letter-spacing:1.7px;text-transform:uppercase;
  color:var(--lime);margin:0 0 9px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.sd-blk p{font-size:13px;line-height:1.65;color:var(--muted);margin:0}

.sd-swot{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.sd-q{background:var(--bg2);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.sd-q>span{font-family:var(--mono);font-size:9.5px;letter-spacing:1.4px;text-transform:uppercase;
  display:block;margin-bottom:8px}
.sd-q.q-s>span{color:var(--up)} .sd-q.q-w>span{color:var(--down)}
.sd-q.q-o>span{color:var(--blue)} .sd-q.q-t>span{color:var(--gold)}
.sd-q ul{list-style:none;margin:0;padding:0;display:grid;gap:9px}
.sd-q li{font-size:12.5px;line-height:1.55;color:var(--muted)}
/* The evidence line is the point of the whole SWOT: every claim above it is
   generated from this number, so it travels with the claim and never gets
   collapsed away on small screens. */
.sd-q li em{display:block;font-family:var(--mono);font-size:10.5px;color:var(--dim);
  font-style:normal;margin-top:3px}
.sd-q.empty{display:none}

.sd-upd{list-style:none;margin:0;padding:0;display:grid;gap:8px}
.sd-upd li{font-family:var(--mono);font-size:12px;line-height:1.55;color:var(--muted);
  padding-left:16px;position:relative}
.sd-upd li::before{content:"›";position:absolute;left:0;color:var(--dim)}
.sd-upd li.k-good::before{content:"▲";color:var(--up);font-size:8px;top:3px}
.sd-upd li.k-bad::before{content:"▼";color:var(--down);font-size:8px;top:3px}
.sd-upd li.k-warn::before{content:"!";color:var(--gold);font-weight:700}

.sd-news{list-style:none;margin:0;padding:0;display:grid;gap:10px}
.sd-news a{font-size:12.5px;line-height:1.5;color:var(--muted);display:block}
.sd-news a:hover{color:var(--lime)}
.sd-news .m{font-family:var(--mono);font-size:10px;color:var(--dim);margin-top:2px;display:block}

@media(max-width:700px){
  .sd-swot{grid-template-columns:1fr}
  .sd-h h3{font-size:18px}
}

/* ═══════════════════ 03 SIGNAL LOG ═══════════════════ */
.kpi-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:1px;background:var(--line);
  border:1px solid var(--line);border-radius:16px;overflow:hidden;margin-bottom:22px}
.kpi{background:var(--surface);padding:20px 18px}
.kpi .v{font-family:var(--mono);font-size:clamp(22px,3vw,32px);font-weight:700;letter-spacing:-1.2px;line-height:1}
.kpi .k{font-size:10px;letter-spacing:1.6px;text-transform:uppercase;color:var(--dim);margin-top:8px;font-weight:500}
.filters{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:16px}
.fbtn{padding:7px 15px;font-family:var(--mono);font-size:10px;letter-spacing:1.2px;text-transform:uppercase;
  background:transparent;border:1px solid var(--line);color:var(--muted);cursor:pointer;border-radius:100px;
  transition:all .25s var(--ease)}
.fbtn:hover{border-color:var(--line2);color:var(--text)}
.fbtn.on{border-color:var(--lime);color:#000;background:var(--lime);font-weight:700}
.tw{overflow-x:auto;border:1px solid var(--line);border-radius:16px;-webkit-overflow-scrolling:touch}
/* The signal log is 87 rows and growing, and `position:sticky` on its <th> did
   nothing: .tw sets overflow-x, which makes overflow-y compute to auto, so .tw
   IS the scroll container — and an unbounded container has no top edge to
   stick to. You scrolled the PAGE, the whole table moved, and the header left
   with it, so by row 20 the columns were unlabelled.
   Bounding the height gives the header something to stick to and turns the
   table into its own scroll region. Applied only here; short tables elsewhere
   must keep growing with the page. */
.tw-tall{max-height:min(78vh,780px);overflow-y:auto}
.tw-tall table.t th{z-index:5;box-shadow:inset 0 -1px 0 var(--line2)}
table.t{width:100%;border-collapse:collapse;font-size:12.5px;min-width:900px}
table.t th{position:sticky;top:0;background:#0E0F12;text-align:left;font-size:9.5px;letter-spacing:1.4px;
  text-transform:uppercase;color:var(--dim);font-weight:600;padding:13px 14px;border-bottom:1px solid var(--line);z-index:2}
table.t td{padding:12px 14px;border-bottom:1px solid rgba(255,255,255,.04);vertical-align:middle}
table.t tbody tr{transition:background .2s}
table.t tbody tr:hover{background:rgba(255,255,255,.025)}
table.t tbody tr:last-child td{border-bottom:none}
.badge{display:inline-flex;align-items:center;gap:5px;padding:4px 10px;font-family:var(--mono);font-size:9.5px;
  font-weight:700;letter-spacing:1px;text-transform:uppercase;border-radius:100px;white-space:nowrap}
.badge-win{background:rgba(61,220,151,.12);color:var(--up);border:1px solid rgba(61,220,151,.3)}
.badge-loss{background:rgba(255,92,92,.1);color:var(--down);border:1px solid rgba(255,92,92,.28)}
.badge-open{background:rgba(106,168,255,.1);color:var(--blue);border:1px solid rgba(106,168,255,.28)}
.badge-cancelled{background:rgba(255,255,255,.04);color:var(--dim);border:1px solid var(--line)}
/* Position battle status — where a tracked position sits in the profit-protection ladder. */
.badge-accumulation{background:rgba(255,255,255,.04);color:var(--dim);border:1px solid var(--line)}
.badge-protected{background:rgba(184,239,67,.12);color:var(--lime);border:1px solid rgba(184,239,67,.3)}
.badge-compounding{background:rgba(167,139,250,.12);color:var(--violet);border:1px solid rgba(167,139,250,.3)}
.badge-threatened{background:rgba(232,197,71,.12);color:var(--gold);border:1px solid rgba(232,197,71,.3)}
.sym{font-family:var(--mono);font-weight:700;color:#E6EAF0;transition:color .2s}
.sym:hover{color:var(--lime)}
.mono-dim{font-family:var(--mono);color:var(--dim);font-size:11.5px}
.pnl-u{color:var(--up);font-weight:700;font-family:var(--mono)}
.pnl-d{color:var(--down);font-weight:700;font-family:var(--mono)}

/* ═══════════════════ 04 PORTFOLIO ═══════════════════ */
.formbox{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:20px;margin-top:16px}
.formbox h4{font-family:var(--mono);font-size:10px;letter-spacing:2px;text-transform:uppercase;
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
.btn{background:var(--lime);color:#000;border:none;padding:10px 20px;font-size:11.5px;font-weight:700;
  cursor:pointer;letter-spacing:1.2px;border-radius:100px;font-family:var(--sans);text-transform:uppercase;
  transition:transform .25s var(--ease),box-shadow .25s}
.btn:hover{transform:translateY(-2px);box-shadow:0 6px 22px rgba(184,239,67,.25)}
.btn-sm{padding:6px 13px;font-size:10px}
.btn-gh{background:transparent;color:var(--muted);border:1px solid var(--line);padding:6px 13px;
  font-family:var(--mono);font-size:10px;letter-spacing:1px;cursor:pointer;border-radius:100px;
  text-transform:uppercase;transition:all .25s}
.btn-gh:hover{border-color:var(--down);color:var(--down)}
.btn-gh.v:hover{border-color:var(--violet);color:var(--violet)}

/* ═══════════════════ 05 WORLD ═══════════════════ */
.lead{display:grid;grid-template-columns:1.55fr 1fr;gap:0;border:1px solid var(--line);border-radius:18px;
  overflow:hidden;margin-bottom:14px;background:var(--surface)}
@media(max-width:820px){.lead{grid-template-columns:1fr}}
.lead-m{padding:clamp(22px,3.4vw,38px)}
.lead-m h2{font-size:clamp(21px,3vw,34px);font-weight:700;line-height:1.14;letter-spacing:-1.1px;margin:12px 0 14px}
.lead-m h2 a{transition:color .25s}
.lead-m h2 a:hover{color:var(--lime)}
.lead-m p{font-size:14.5px;color:var(--muted);line-height:1.7}
.lead-s{padding:clamp(20px,2.6vw,30px);border-left:1px solid var(--line);background:var(--bg2)}
@media(max-width:820px){.lead-s{border-left:none;border-top:1px solid var(--line)}}
.mini{padding:13px 0;border-bottom:1px solid var(--line)}
.mini:last-child{border-bottom:none;padding-bottom:0}
.mini .s{font-family:var(--mono);font-size:9.5px;letter-spacing:1.4px;text-transform:uppercase;color:var(--lime);display:block;margin-bottom:5px}
.mini a{font-size:13.5px;font-weight:600;line-height:1.42;transition:color .25s}
.mini a:hover{color:var(--lime)}
.news-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px}
.ncard{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:20px;
  transition:border-color .35s,transform .35s var(--ease)}
.ncard:hover{border-color:var(--line2);transform:translateY(-3px)}
.ncard .s{font-family:var(--mono);font-size:9.5px;letter-spacing:1.4px;text-transform:uppercase;color:var(--lime)}
.ncard h3{font-size:15px;font-weight:600;line-height:1.4;margin:9px 0 9px;letter-spacing:-.2px}
.ncard h3 a{transition:color .25s} .ncard h3 a:hover{color:var(--lime)}
.ncard p{font-size:12.5px;color:var(--muted);line-height:1.6}
.ncard .ts{font-family:var(--mono);font-size:10px;color:var(--dim);margin-top:12px;padding-top:11px;border-top:1px solid var(--line)}

/* ═══════════════════ 06 THE DESK (tabs) ═══════════════════ */
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:22px}
.tab{padding:9px 17px;font-size:11.5px;font-weight:500;letter-spacing:.8px;background:transparent;
  border:1px solid var(--line);color:var(--muted);cursor:pointer;border-radius:100px;
  font-family:var(--sans);transition:all .28s var(--ease);white-space:nowrap}
.tab:hover{border-color:var(--line2);color:var(--text)}
.tab.on{background:var(--lime);border-color:var(--lime);color:#000;font-weight:700}
/* The Way — Arabic phrase card */
.arabic-hero{text-align:center;padding:26px 18px;background:var(--bg);border:1px solid var(--line);
  border-radius:14px;margin:14px 0 4px}
.ar-script{font-size:clamp(34px,6vw,52px);line-height:1.5;color:var(--up);font-weight:600;
  letter-spacing:0;margin-bottom:12px;direction:rtl;unicode-bidi:isolate}
.ar-translit{font-family:var(--mono);font-size:15px;color:var(--text);letter-spacing:.4px;margin-bottom:6px}
.ar-meaning{font-size:14px;color:var(--muted);font-style:italic}
@media(max-width:640px){ .arabic-hero{padding:20px 12px} }
/* Streak tracker — client-side only */
.streak{margin-top:18px;padding:18px 20px;background:var(--surface);border:1px solid var(--line);border-radius:14px}
.stk-head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap;margin-bottom:14px}
.stk-lab{font-family:var(--mono);font-size:10px;letter-spacing:1.8px;text-transform:uppercase;color:var(--lime);margin-bottom:5px}
.stk-sub{font-size:12px;color:var(--dim)}
.stk-nums{display:flex;gap:18px}
.stk-n{text-align:right}
.stk-n b{display:block;font-size:22px;font-weight:700;letter-spacing:-1px;color:var(--text);line-height:1}
.stk-n i{font-style:normal;font-family:var(--mono);font-size:9px;letter-spacing:1.2px;text-transform:uppercase;color:var(--dim)}
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
.dq-lab{font-family:var(--mono);font-size:10px;letter-spacing:1.8px;text-transform:uppercase;
  color:var(--violet);margin-bottom:9px}
.deep-q h3{font-size:clamp(18px,2.4vw,25px);font-weight:700;letter-spacing:-.7px;line-height:1.3;
  color:var(--text);margin-bottom:8px}
.deep-q p{font-size:13.5px;color:var(--muted);line-height:1.65}
.rv-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}
.rv-card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.rv-card.wide{grid-column:1/-1}
.rv-card label{display:block;font-family:var(--mono);font-size:10px;letter-spacing:1.6px;
  text-transform:uppercase;color:var(--lime);margin-bottom:5px}
.rv-hint{font-size:11.5px;color:var(--dim);margin-bottom:9px;line-height:1.5}
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
.essay p{font-size:14.5px;line-height:1.85;color:#C4CAD2}
.essay .q{font-size:15px;font-style:italic;color:var(--ac,var(--lime));border-left:2px solid var(--ac,var(--lime));
  padding-left:15px;margin:20px 0;line-height:1.7}
.essay .act{background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:16px 18px;
  margin-top:20px;font-size:13.5px;line-height:1.7;color:#C4CAD2}
.essay .act b{display:block;font-family:var(--mono);font-size:10px;letter-spacing:1.8px;text-transform:uppercase;
  color:var(--ac,var(--lime));margin-bottom:7px}
.essay .meta{font-family:var(--mono);font-size:10px;letter-spacing:1.6px;text-transform:uppercase;
  color:var(--dim);margin-bottom:10px}
/* Book depth: full crux, learnings, examples, how to adapt */
.bookdeep{margin-top:20px;padding-top:18px;border-top:1px solid var(--line)}
.bdhead{font-family:var(--mono);font-size:10px;letter-spacing:1.8px;text-transform:uppercase;
  color:var(--ac,var(--lime));margin-bottom:12px}
.bookdeep ol.crux{margin:0;padding-left:0;list-style:none;counter-reset:cx}
.bookdeep ol.crux li{counter-increment:cx;position:relative;padding-left:34px;margin-bottom:11px;
  font-size:13.5px;line-height:1.65;color:#C4CAD2}
.bookdeep ol.crux li::before{content:counter(cx,decimal-leading-zero);position:absolute;left:0;top:1px;
  font-family:var(--mono);font-size:10.5px;color:var(--ac,var(--lime));opacity:.75}
.bookdeep ul.bdlist{margin:0;padding-left:0;list-style:none}
.bookdeep ul.bdlist li{position:relative;padding-left:20px;margin-bottom:10px;
  font-size:13.5px;line-height:1.65;color:#C4CAD2}
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
.quote-hero .idx{font-family:var(--mono);font-size:10px;color:var(--dim);margin-top:8px}

/* ═══════════════════ 08 CHESS ═══════════════════ */
.chess-kpi{display:flex;gap:0;flex-wrap:wrap;border:1px solid var(--line);border-radius:16px;overflow:hidden;margin-bottom:18px}
.ck{flex:1 1 90px;padding:18px 16px;border-right:1px solid var(--line);background:var(--surface)}
.ck:last-child{border-right:none}
.ck .v{font-family:var(--mono);font-size:clamp(20px,2.6vw,28px);font-weight:700;letter-spacing:-1px;line-height:1}
.ck .k{font-size:9.5px;letter-spacing:1.6px;text-transform:uppercase;color:var(--dim);margin-top:7px}
.verdict{padding:18px 20px;background:rgba(61,220,151,.05);border:1px solid rgba(61,220,151,.22);
  border-radius:14px;font-size:14px;line-height:1.75;color:#C4CAD2;margin-bottom:18px}
.verdict b{color:var(--up);font-weight:700}
.game{background:var(--surface);border:1px solid var(--line);border-left:3px solid var(--dim);border-radius:14px;
  padding:18px;margin-bottom:10px;position:relative;transition:border-color .3s,transform .3s var(--ease)}
.game:hover{transform:translateX(3px)}
.game.win{border-left-color:var(--up)} .game.loss{border-left-color:var(--down)} .game.draw{border-left-color:var(--dim)}
.game .hdr{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:9px}
.game .res{font-weight:700;font-size:14px}
.game .meta{font-size:12px;color:var(--muted);line-height:1.6}
.game .op{font-size:13px;color:#DDE2E8;margin-bottom:6px;font-weight:500}
.game .mv{font-family:var(--mono);font-size:10.5px;color:var(--dim);overflow-x:auto;white-space:nowrap;margin-top:5px}
.game .an{margin-top:11px;padding:11px 13px;background:var(--bg);border-radius:10px;border-left:2px solid var(--lime);
  font-size:12.5px;color:#B4BAC2;line-height:1.7}
/* Best move / standout / key facts — replaced the raw opening+final move dumps */
.game .bestmv{margin-top:11px;padding:11px 13px;background:rgba(232,183,74,.06);
  border:1px solid rgba(232,183,74,.22);border-radius:10px}
.game .bmlab{font-family:var(--mono);font-size:9px;letter-spacing:1.4px;text-transform:uppercase;
  color:var(--gold);margin-bottom:7px}
.game .bmrow{display:flex;align-items:baseline;gap:11px;flex-wrap:wrap}
.game .bmsan{font-family:var(--mono);font-size:17px;font-weight:700;color:#F2E4C0;letter-spacing:-.3px}
.game .bmgain{font-size:12px;font-weight:600;color:var(--up)}
.game .bmeval{font-family:var(--mono);font-size:10.5px;color:var(--dim)}
.game .uniq{margin-top:10px;padding:11px 13px;background:rgba(106,168,255,.05);
  border-left:2px solid var(--blue);border-radius:10px;font-size:12.5px;color:#B4BAC2;line-height:1.65}
.game .uniq b{display:block;font-family:var(--mono);font-size:9px;letter-spacing:1.4px;
  text-transform:uppercase;color:var(--blue);margin-bottom:5px;font-weight:600}
.game .kfacts{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.game .kf{font-size:11px;color:var(--muted);background:rgba(255,255,255,.04);
  border:1px solid var(--line);border-radius:7px;padding:4px 9px}
.game .ratings{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-top:10px;
  padding-top:10px;border-top:1px solid var(--line)}
.game .rt{display:flex;flex-direction:column;gap:2px}
.game .rt i{font-style:normal;font-family:var(--mono);font-size:8.5px;letter-spacing:1.2px;
  text-transform:uppercase;color:var(--dim)}
.game .rt b{font-size:15px;font-weight:700;color:var(--gold);letter-spacing:-.4px}
.game .rtnote{font-size:10px;color:var(--dim);line-height:1.5;flex:1;min-width:150px}
.pill{font-family:var(--mono);font-size:9px;letter-spacing:1.2px;padding:3px 8px;border-radius:100px;
  background:rgba(255,255,255,.05);color:var(--muted);text-transform:uppercase}
.trend{display:flex;gap:6px;align-items:flex-end;height:88px;margin-top:12px}
.trend>div{flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;gap:5px}
.trend .bar{width:100%;border-radius:4px 4px 0 0;height:0;transition:height .9s var(--ease) var(--d,0s)}
.rv.in .trend .bar{height:var(--h)}
.trend .lb{font-family:var(--mono);font-size:9px;color:var(--dim)}

/* ═══════════════════ FOOTER + FAB ═══════════════════ */
footer{position:relative;z-index:2;border-top:1px solid var(--line);margin-top:20px;background:var(--bg2)}
.foot-in{max-width:1400px;margin:0 auto;padding:clamp(40px,6vw,70px) var(--gut);
  display:flex;justify-content:space-between;gap:28px;flex-wrap:wrap;align-items:flex-end}
.foot-in h4{font-size:clamp(24px,4vw,42px);font-weight:800;letter-spacing:-1.8px;line-height:1}
.foot-in h4 b{color:var(--lime)}
.foot-in .m{font-family:var(--mono);font-size:11px;color:var(--dim);line-height:2;text-align:right}
@media(max-width:640px){.foot-in .m{text-align:left}}
.fab{position:fixed;right:20px;bottom:20px;z-index:400;width:46px;height:46px;border-radius:50%;
  background:var(--lime);color:#000;border:none;cursor:pointer;font-size:17px;display:grid;place-items:center;
  opacity:0;pointer-events:none;transform:translateY(14px);transition:all .35s var(--ease);
  box-shadow:0 8px 26px rgba(184,239,67,.3)}
.fab.on{opacity:1;pointer-events:auto;transform:none}
.empty{padding:34px 22px;text-align:center;color:var(--dim);font-size:13.5px;background:var(--surface);
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

.perf-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;
  background:var(--line);border:1px solid var(--line);border-radius:16px;overflow:hidden;margin-bottom:22px}
.perf-cell{background:var(--surface);padding:18px 16px}
.perf-cell .v{font-family:var(--mono);font-size:25px;font-weight:700;letter-spacing:-1px;line-height:1.1}
.perf-cell .k{font-family:var(--mono);font-size:10px;color:var(--dim);text-transform:uppercase;
  letter-spacing:1px;margin-top:7px}
.perf-cell .sub{font-size:11px;color:var(--muted);margin-top:4px}


.brk{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}
.brk-card{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:18px;min-width:0}
.brk-card h4{font-family:var(--mono);font-size:11px;color:var(--dim);text-transform:uppercase;
  letter-spacing:1.2px;margin-bottom:12px}
.brk-row{display:grid;grid-template-columns:1fr auto auto;gap:10px;align-items:center;
  padding:8px 0;border-top:1px solid var(--line);font-size:12.5px}
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
.elog-t{font-family:var(--mono);font-size:9px;letter-spacing:1.4px;color:var(--dim)}
.elog-v{font-family:var(--mono);font-size:9px;letter-spacing:1.2px;text-transform:uppercase;
  border:1px solid var(--line2);border-radius:4px;padding:2px 7px}
.elog-v.adopted{color:var(--lime);border-color:var(--lime-line);background:var(--lime-soft)}
/* Rejected is deliberately not red. It is not a failure state — it is a test
   that returned a negative, which is the point of publishing it. */
.elog-v.rejected{color:var(--muted)}
.elog-b{min-width:0}
.elog-h{font-size:19px;font-weight:600;letter-spacing:-.3px;text-wrap:balance;margin-bottom:8px}
.elog-p{color:var(--muted);font-size:14px;max-width:62ch}
.elog-e{width:100%;border-collapse:collapse;margin:14px 0 0;font-size:12.5px}
.elog-e th{text-align:left;font-weight:500;color:var(--text);padding:6px 12px 6px 0}
.elog-e td{padding:6px 0 6px 12px;text-align:right;white-space:nowrap;
  font-variant-numeric:tabular-nums}
.elog-e tr+tr th,.elog-e tr+tr td{border-top:1px solid var(--line)}
.elog-n,.elog-s{color:var(--dim);font-size:11px}
.elog-c{margin-top:12px;padding-left:13px;border-left:2px solid var(--line2);
  color:var(--dim);font-size:12.5px;line-height:1.65;max-width:62ch}
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
.arch-day .d{font-family:var(--mono);font-size:10px;color:var(--muted)}
.arch-day .n{font-family:var(--mono);font-size:17px;font-weight:700;margin:3px 0}
.arch-day .r{font-family:var(--mono);font-size:10px}

.pos-alert{display:inline-block;font-family:var(--mono);font-size:9.5px;letter-spacing:.6px;
  text-transform:uppercase;padding:2px 7px;border-radius:999px;margin-left:6px;vertical-align:middle}
.pos-alert.near-stop{background:rgba(255,92,92,.14);color:var(--down)}
.pos-alert.stop-hit{background:var(--down);color:#000;font-weight:700}
.pos-alert.near-target{background:rgba(61,220,151,.14);color:var(--up)}
.pos-alert.target-hit{background:var(--up);color:#000;font-weight:700}
/* Next-action pill — what the ladder/decision layer recommends right now. */
.next-action{display:inline-block;font-family:var(--mono);font-size:9.5px;letter-spacing:.6px;
  text-transform:uppercase;padding:2px 7px;border-radius:999px;white-space:nowrap}
.next-action.act-sell{background:rgba(184,239,67,.14);color:var(--lime)}
.next-action.act-exit{background:rgba(255,92,92,.14);color:var(--down)}
.next-action.act-wait{background:rgba(255,255,255,.04);color:var(--dim)}

.keybox{display:none;gap:9px;flex-wrap:wrap;align-items:center;margin:0 0 16px;padding:14px;
  background:var(--surface);border:1px dashed var(--line2);border-radius:14px;font-size:12.5px;color:var(--muted)}
.keybox.on{display:flex}
.keybox input{font-family:var(--mono);font-size:12px;color:var(--text);background:var(--bg2);
  border:1px solid var(--line2);border-radius:9px;padding:8px 11px;flex:1 1 180px}

/* ═══════════════════ LEARNING TRACKS ═══════════════════ */
.lrn-head{display:flex;align-items:center;gap:12px;margin:26px 0 14px}
.lrn-head:first-of-type{margin-top:0}
.lrn-kicker{font-family:var(--mono);font-size:10.5px;letter-spacing:1.6px;text-transform:uppercase;
  color:var(--lime);white-space:nowrap}
.lrn-head::after{content:'';flex:1;height:1px;background:var(--line)}

.qa-grid{display:grid;gap:11px}
.qa{background:var(--surface);border:1px solid var(--line);border-radius:14px;overflow:hidden;
  transition:border-color .3s var(--ease)}
.qa[open]{border-color:var(--lime-line)}
.qa summary{list-style:none;cursor:pointer;padding:17px 20px;display:flex;justify-content:space-between;
  align-items:flex-start;gap:16px}
.qa summary::-webkit-details-marker{display:none}
.qa summary::after{content:'+';font-family:var(--mono);font-size:17px;color:var(--dim);flex:none;line-height:1.3}
.qa[open] summary::after{content:'−';color:var(--lime)}
.qa:hover summary::after{color:var(--lime)}
.qa-q{font-size:14.5px;font-weight:600;line-height:1.5;letter-spacing:-.15px;flex:1}
.qa-who{font-family:var(--mono);font-size:9.5px;color:var(--dim);letter-spacing:.5px;
  text-align:right;flex:none;max-width:180px;line-height:1.5}
.qa-a{padding:0 20px 20px;font-size:13.5px;line-height:1.72;color:var(--muted);
  border-top:1px solid var(--line);padding-top:16px;margin:0 20px 20px;padding-left:0;padding-right:0}

.lrn-card{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:20px;
  transition:border-color .3s var(--ease)}
.lrn-card:hover{border-color:var(--line2)}
.lrn-card.jain{border-left:3px solid var(--gold)}
.lrn-card.budd{border-left:3px solid var(--violet)}
.lrn-tag{font-family:var(--mono);font-size:9.5px;letter-spacing:1.4px;text-transform:uppercase;
  color:var(--dim);margin-bottom:9px}
.lrn-word{font-size:23px;font-weight:700;letter-spacing:-.5px;color:var(--lime);line-height:1.25}
.lrn-word.sm{font-size:18px;color:var(--text)}
.lrn-word .tr{font-size:13px;font-weight:400;color:var(--muted);letter-spacing:0}
.lrn-mean{font-size:13.5px;color:var(--text);margin-top:6px}
.lrn-ex{margin-top:14px;padding-top:13px;border-top:1px solid var(--line);display:grid;gap:5px}
.lrn-ex .es{font-size:13.5px;font-style:italic;color:var(--text)}
.lrn-ex .en{font-size:12px;color:var(--dim)}
.lrn-do{font-size:13.5px;line-height:1.65;color:var(--text);margin-top:10px}
.lrn-why{font-size:12.5px;line-height:1.68;color:var(--muted);margin-top:13px;padding-top:12px;
  border-top:1px solid var(--line)}
.lrn-why b{color:var(--lime);font-family:var(--mono);font-size:10px;letter-spacing:1.2px;
  text-transform:uppercase;margin-right:7px}

.drill{background:var(--surface);border:1px solid var(--line);border-left:3px solid var(--lime);
  border-radius:16px;padding:22px}
.drill-t{font-size:18px;font-weight:700;letter-spacing:-.3px}
.drill-d{font-size:13.5px;line-height:1.7;color:var(--text);margin-top:9px}
.drill-w{font-size:12.5px;line-height:1.65;color:var(--muted);margin-top:13px;padding-top:12px;
  border-top:1px solid var(--line)}

@media(max-width:640px){
  .qa summary{padding:15px 16px;gap:10px}
  .qa-q{font-size:13.5px}
  .qa-who{display:none}
  .qa-a{margin:0 16px 16px;font-size:13px}
  .lrn-word{font-size:20px}
}

/* ═══════════════════ MIND GYM ═══════════════════ */
.gym-tabs{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:20px}
.gym-tab{display:flex;align-items:center;gap:7px;padding:9px 15px;font-size:11.5px;font-weight:600;
  letter-spacing:.5px;background:transparent;border:1px solid var(--line);color:var(--muted);
  cursor:pointer;border-radius:100px;font-family:var(--sans);transition:all .28s var(--ease);white-space:nowrap}
.gym-tab:hover{border-color:var(--line2);color:var(--text)}
.gym-tab.on{background:var(--lime);border-color:var(--lime);color:#000}
.gym-tab .tdot{width:5px;height:5px;border-radius:50%;background:var(--gold);flex:none}
.gym-tab.on .tdot{background:#000}

.gym-stage{background:var(--surface);border:1px solid var(--line);border-radius:18px;
  padding:clamp(20px,4vw,34px);min-height:290px;display:flex;flex-direction:column;justify-content:center}
.gym-q{font-size:clamp(21px,4.4vw,32px);font-weight:700;letter-spacing:-.6px;line-height:1.3;margin-bottom:6px}
.gym-q .mono{font-family:var(--mono)}
.gym-sub{font-size:12.5px;color:var(--muted);margin-bottom:20px}
.gym-prompt{font-family:var(--mono);font-size:clamp(28px,7vw,52px);font-weight:700;color:var(--lime);
  letter-spacing:2px;text-align:center;padding:22px 0}

.gym-opts{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}
.gym-opt{padding:16px 14px;font-family:var(--mono);font-size:16px;font-weight:600;background:var(--bg2);
  border:1px solid var(--line2);color:var(--text);border-radius:12px;cursor:pointer;
  transition:all .2s var(--ease);text-align:center}
.gym-opt:hover:not(:disabled){border-color:var(--lime);color:var(--lime)}
.gym-opt:disabled{cursor:default;opacity:.55}
.gym-opt.right{background:var(--lime);border-color:var(--lime);color:#000;opacity:1}
.gym-opt.wrong{background:rgba(255,92,92,.15);border-color:var(--down);color:var(--down);opacity:1}

.gym-input{display:flex;gap:9px;flex-wrap:wrap}
.gym-input input{flex:1 1 160px;font-family:var(--mono);font-size:19px;font-weight:600;color:var(--text);
  background:var(--bg2);border:1px solid var(--line2);border-radius:12px;padding:14px 16px;min-width:0}
.gym-input input:focus{outline:none;border-color:var(--lime)}
.gym-btn{padding:14px 24px;font-size:13px;font-weight:700;letter-spacing:.6px;background:var(--lime);
  border:none;color:#000;border-radius:12px;cursor:pointer;font-family:var(--sans);transition:opacity .2s}
.gym-btn:hover{opacity:.85}
.gym-btn.ghost{background:transparent;border:1px solid var(--line2);color:var(--muted)}
.gym-btn.ghost:hover{border-color:var(--lime);color:var(--lime);opacity:1}

.gym-fb{margin-top:18px;padding:14px 16px;border-radius:12px;font-size:13.5px;line-height:1.55;
  border-left:3px solid var(--line2);background:var(--bg2)}
.gym-fb.good{border-left-color:var(--up)} .gym-fb.bad{border-left-color:var(--down)}
.gym-fb b{font-family:var(--mono)}

.gym-meta{display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap;
  margin-bottom:16px;font-family:var(--mono);font-size:11px;color:var(--dim);letter-spacing:.6px}
.gym-meta .prog{color:var(--lime)}
.gym-score{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:1px;background:var(--line);
  border:1px solid var(--line);border-radius:14px;overflow:hidden;margin-top:20px}
.gym-score div{background:var(--surface);padding:14px 12px;text-align:center}
.gym-score .v{font-family:var(--mono);font-size:21px;font-weight:700}
.gym-score .k{font-family:var(--mono);font-size:9.5px;color:var(--dim);text-transform:uppercase;
  letter-spacing:1px;margin-top:5px}
@media(max-width:640px){
  .gym-opts{grid-template-columns:1fr 1fr}
  .gym-stage{min-height:250px}
}

/* Mobile: tables become the pain point on a phone, so give them a real scroll
   affordance and stop the control bars from stacking into a wall. */
@media(max-width:640px){
  .livebar{font-size:10px;padding:7px 14px}
  .livebar .msg{white-space:normal}
  .perf-cell .v{font-size:21px}
  .ctlbar input[type=search]{flex:1 1 100%}
  .ctlbar .ghost{margin-left:0;width:100%}
  .tw{position:relative}
  .tw::after{content:"swipe →";position:absolute;right:8px;top:-16px;font-family:var(--mono);
    font-size:9px;color:var(--dim);letter-spacing:.8px}
  .arch-day{min-width:66px;padding:8px}
}
</style>
</head>

<body>
<a class="skip" href="#{{ nav[0].id }}">Skip to content</a>
<div class="grain"></div>
<div class="vgrid"></div>
<div class="progress" id="prog"></div>

{% set wins   = alerts | selectattr("badge","eq","win")  | list | length %}
{% set losses = alerts | selectattr("badge","eq","loss") | list | length %}
{% set opens  = alerts | selectattr("badge","eq","open") | list | length %}
{% set closed = wins + losses %}
{% set winrate = ((wins / closed * 100) | round(0) | int) if closed > 0 else 0 %}
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
      <span class="live" id="istClock"><i></i>{{ updated_at }} IST</span>
    </div>
  </div>
</header>

<!-- Nav order MUST match document order. It did not: Performance, Mind Gym and
     Signal Log were listed near the top while their sections sit at the very
     bottom, so the scroll spy underlined "Chess" while you were reading the
     signal log and "The Mind" while you were reading The Desk. The numbers had
     drifted too — two 05s in the nav, and section headings that disagreed with
     it. One sequence now, top to bottom, nav and headings the same. -->
<nav class="nav">
  <div class="nav-in" id="navin">
    {% for n in nav %}{% if n.head %}<span class="nav-g" aria-hidden="true">{{ n.head }}</span>{% endif %}<a href="#{{ n.id }}" aria-label="{{ n.group }} — {{ n.label }}"><i>{{ n.n }}</i>{{ n.label }}</a>
    {% endfor %}
    <a class="nav-other" href="{{ other_path }}" title="{{ other_hint }}">{{ other_label }} &rarr;</a>
  </div>
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
  <span><b>New edition published<span id="editionWhen"></span>.</b>
    This tab is still showing {{ date_str }} — its markets, ideas and ledger are stale.</span>
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

  <div class="eyebrow">◆ Compiled 6:00 AM IST · {{ date_str }}</div>

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

  <p class="hero-sub">Markets, live signals, the world, and the work — one page, rebuilt every
    morning before the open. No feeds. No scroll trap. Just what moved and what to do about it.</p>
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
      <div class="k">Signal Win Rate</div>
      <div class="kn" id="heroRateNote">{{ closed }} closed{{ ' · too few to measure' if closed < 30 else '' }}</div>
    </div>
    <div class="stat">
      <div class="v" id="heroOpen" style="color:var(--blue)" data-count="{{ opens }}">{{ opens }}</div>
      <div class="k" id="heroOpenK">Open Setups</div>
    </div>
    <div class="stat">
      <div class="v" id="heroTotal" data-count="{{ alerts|length }}">{{ alerts|length }}</div>
      <div class="k">Signals Logged</div>
    </div>
    <div class="stat">
      <div class="v" style="color:{{ 'var(--up)' if advancers >= (markets|length / 2) else 'var(--down)' }}"
           data-count="{{ advancers }}" data-total="{{ markets|length }}">{{ advancers }}/{{ markets|length }}</div>
      <div class="k">Markets Advancing</div>
    </div>
  </div>

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
  </div>


  {% endif %}

  {% if page != 'desk' %}
  <!-- Track record, in the hero. The full performance section stays where it
       is; this is the one-glance version, because the argument this page makes
       is "here is the record" and the record was 17 sections down. Drawn from
       /api/stats; hidden entirely on a static host with no ledger. -->
  <div class="herocurve" id="heroCurve" hidden>
    <div class="hc-h">
      <span class="hc-t">Cumulative R · every closed signal</span>
      <span class="hc-v" id="hcTotal">—</span>
    </div>
    <svg viewBox="0 0 600 96" preserveAspectRatio="none" role="img"
         aria-labelledby="hcDesc">
      <title id="hcDesc">Cumulative R-multiple across every closed signal</title>
      <defs>
        <linearGradient id="hcFill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="var(--lime)" stop-opacity=".22"/>
          <stop offset="100%" stop-color="var(--lime)" stop-opacity="0"/>
        </linearGradient>
      </defs>
      <path id="hcArea" fill="url(#hcFill)"></path>
      <path id="hcLine" fill="none" stroke="var(--lime)" stroke-width="1.6"
            vector-effect="non-scaling-stroke" stroke-linejoin="round"></path>
      <circle id="hcDot" r="3" fill="var(--lime)"></circle>
    </svg>
    <div class="hc-f">
      <span id="hcFrom">—</span>
      <span id="hcNote">drawdowns included — that is the point</span>
      <span id="hcTo">—</span>
    </div>
  </div>
  {% endif %}
</section>

<main>

<!-- ══════════ 04 WORLD ══════════ -->
{% if 'world' in secs %}<section class="sec" id="world">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['world'] }} / {{ seclabel['world'] }}</span>
      <h2 class="stitle">The world, last 24h.</h2>
    </div>
    <p class="sdesc" id="worldDesc">Wires only. Deduplicated, ranked, and cut to what
      actually changes a decision.</p>
  </div>

  <!-- Live incident map.
       Land is a 156x66 dot grid rasterised once from Natural Earth (public
       domain) and run-length encoded into the string below — no external asset,
       no runtime fetch, nothing for a CSP to block. Blue is the baseline
       everywhere; a country lights red or green when the last 24 hours of wires
       say something is happening there. Filled from /api/world. -->
  <div class="wmap-wrap rv">
    <div class="wmap-head">
      <span class="wm-t">Live incident map · rolling 24h</span>
      <span class="wm-legend">
        <i class="dot red"></i>escalation
        <i class="dot green"></i>good news
        <i class="dot blue"></i>quiet
      </span>
    </div>
    <div class="wmap" id="worldMap" data-mask="2a.8.8.a.7d.2.1.8.1.16.1b.2.12.2.41.1.3.1.1.1.2.7.2.16.e.3.23.3.39.2.3.1.3.2.1.2.1.1.1.1.5.15.24.3.d.6.35.3.1a.10.21.1.c.a.36.a.1.2.3.7.8.e.20.2.6.1.1.18.4.5.14.a.4.2.4.7.1.1.1.1.4.1.1.5.5.e.12.5.10.28.5.2.2.2.5.24.3.4.5.9.14.c.1.1.1.39.1.2.2.23.6.3.1.1.4.6.7.4.b.4.2.4.2.3d.4.1.2.20.8.3.6.4.15.5.1.45.6.1f.7.4.9.2.15.6.1.3b.1.1.1.4.c.3.6.13.7.4.1.2.1d.2.1.3.2.34.7.2.10.1.a.14.5.6.18.2.6.2.2.33.8.3.1c.15.1.a.15.4.7.1.1.32.8.3.1f.14.1.a.14.1.1.3.1.3c.6.1.1f.1b.2.1.1.1.19.3c.29.1a.2.3.15.3e.2a.1c.19.e.3.2c.2a.18.1.1.18.1.2.4.1.2.1.5.5.2a.2.2.27.17.1b.5.4.2.1.4.2.2.2.26.2e.16.1c.4.9.1.1.29.2.1.30.14.1d.3.7.1.2.1.2.29.2.1.4.1.2b.13.1e.8.9.1.1.24.3.1.1.4.2c.10.1f.a.3.1.5.25.4.1.32.e.1f.39.37.6.6.1.1e.1a.1.1f.36.1.1.5.8.1.1b.15.1.6.3.1b.3a.4.23.16.2.8.4.16.1.1.39.4.7.1.1b.17.1.9.4.9.1.9.27.1.15.4.3.1.6.2.17.17.2.7.7.5.4.5.1.1.40.6.1f.18.1.6.8.4.5.5.6.1.3f.4.1d.18.2.3.a.3.7.5.5.1.41.2.1d.19.1.1.c.3.9.3.5.2.41.1.3.3.18.19.1.2.b.2.a.2.6.1.42.1.2.7.14.1c.b.1.14.1.43.9.14.1a.16.1.6.1.47.c.1a.11.15.1.1.1.4.2.47.c.1a.10.17.2.3.3.46.d.1a.f.18.2.2.4.46.10.17.e.1a.1.3.3.1.1.4.1.1.1.3d.13.15.c.1c.1.d.4.3a.14.14.c.1d.1.d.4.3.1.36.13.15.b.22.2.1.1.6.1.1.1.3a.12.16.c.69.10.17.c.3.1.22.3.2.1.3d.10.16.d.2.2.20.5.2.2.3e.e.16.b.3.2.20.7.1.2.3f.d.17.9.4.2.1f.c.3e.c.18.9.4.2.1d.f.3c.b.1a.9.4.2.1c.11.3b.a.1c.7.23.11.3b.a.1c.7.24.11.3a.9.1e.5.25.10.3b.8.1f.4.26.4.4.8.3b.6.4b.1.8.6.3b.7.56.4.3b.5.66.2.2f.4.5b.1.b.1.30.4.65.2.31.3.65.2.31.4.98.3.9a.2.9b.2.1a3">
      <!-- Landmass on canvas, events on SVG. The land used to be 3,091
           individual <rect> nodes — 55% of the entire page DOM and a 223ms
           paint task — for something that never changes and is never
           interactive. Canvas draws it in one node. The 30-odd event bubbles
           stay as SVG because they need hit-testing, tooltips and a11y. -->
      <canvas id="wmCanvas" width="624" height="264" aria-hidden="true"></canvas>
      <svg viewBox="0 0 156 66" preserveAspectRatio="xMidYMid meet" role="img"
           aria-label="World map of the last 24 hours of news">
        <g id="wmDots"></g>
      </svg>
      <div class="wm-night" id="wmNight" aria-hidden="true"></div>
      <div class="wm-night" id="wmNight2" aria-hidden="true" style="display:none"></div>
      <div class="wm-sweep" aria-hidden="true"></div>
      <div class="wm-tip" id="wmTip" hidden></div>
    </div>
    <div class="wm-foot" id="wmFoot">Reading the wires…</div>
  </div>

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


<!-- Capture. There is exactly ONE subscribe box on this page, and it sits at
     the bottom (id="subEnd") after the ledger. A second copy used to sit here,
     above the fold, asking for an email before the reader had seen a single
     scored trade — two asks on one page, and the top one had nothing to point
     back to. The claim that earns the address is the losing month printed in
     public further down, so the ask belongs after it, not before.
     The submit handler binds to every .sub-form, so it needs no change. -->

<!-- ══════════ WHO ══════════
     Wording lifted from askakshay.com so the two sites say the same thing.
     Sits between the world and the trade ideas: a reader who arrived from a
     Telegram link should know whose ledger they are reading before they read
     the numbers. -->
{% if 'who' in secs %}<section class="sec" id="who">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['who'] }} / {{ seclabel['who'] }}</span>
      <h2 class="stitle">Who is publishing this.</h2>
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
        <a href="https://terminal.askakshay.com" target="_blank" rel="noopener">Dhruvedge terminal &nearr;</a>
        <a href="https://www.linkedin.com/in/akkothari" target="_blank" rel="noopener">LinkedIn &nearr;</a>
        <a href="https://www.instagram.com/askakshayfinance" target="_blank" rel="noopener">@askakshayfinance &nearr;</a>
      </div>
    </div>
  </div>
</section>{% endif %}

<!-- ══════════ 01 TRADE IDEAS ══════════ -->
{% if 'picks' in secs %}<section class="sec" id="picks">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['picks'] }} / {{ seclabel['picks'] }}</span>
      <h2 class="stitle">Top 5 trade ideas.</h2>
    </div>
    <p class="sdesc">Global 200 universe — India, US, global. Scored, ranked, refreshed weekly.
      Target 20–30%. Every idea carries a stop.
      {% if top5_week %}<br><span style="color:var(--gold)">This week's scan did not complete —
      showing {{ top5_week }}'s ranking. Prices have moved since.</span>{% endif %}</p>
  </div>

  <div class="prov{{ ' stale' if top5_week else '' }} rv">
    <span class="pv-tag">WEEKLY</span>
    <span>Ranked once per ISO week &mdash; <b>the same five all week is the design</b>, not a stalled scan</span>
    <span>Engine <b>{{ picks_engine }}</b></span>
    <span>These are ideas, not ledger signals &mdash; they carry no entry fill and
      never touch win rate or expectancy</span>
  </div>
  {% if top5 %}
  <div class="pick-grid">
    {% for s in top5 %}
    <div class="pick rv" style="--d:{{ loop.index0 * 0.07 }}s">
      <div class="rank" aria-hidden="true">{{ "%02d"|format(loop.index) }}</div>
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
      {% if s.factors %}
      <details class="why">
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
        <div><div class="k">🎯 Target</div><div class="v up">{{ s.currency }}{{ s.target }}</div></div>
        <div><div class="k">🛡 Stop</div><div class="v dn">{{ s.currency }}{{ s.stop_loss }}</div></div>
        <div><div class="k">⏱ Horizon</div><div class="v" style="font-size:11.5px;color:var(--muted)">{{ s.timeframe }}</div></div>
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
  <div class="empty rv">No ranking available. The weekly scan runs with the 6 AM IST build;
    if this persists past Monday morning, the scan is failing — check the Daily Newspaper workflow.</div>
  {% endif %}
</section>{% endif %}

<!-- ══════════ LONG-TERM CONVICTION ══════════
     Written by ai_longterm.py, which screens the business before the chart.
     Deliberately NOT in the trade log above and excluded from expectancy: a
     2-3 year idea cannot resolve on a 20-day horizon, and letting it into the
     R statistics would corrupt the only honest number here. -->
{% if 'longterm' in secs %}<section class="sec" id="longterm">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['longterm'] }} / {{ seclabel['longterm'] }}</span>
      <h2 class="stitle">Own the business.</h2>
    </div>
    <p class="sdesc">Five NSE names screened on return on capital, growth, leverage and what
      you pay — the chart only votes on whether the trend is intact. Two to three years.
      Selection is arithmetic; the paragraph under each is AI. Excluded from the trading
      win rate on purpose.</p>
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

<!-- ══════════ 03 PORTFOLIO ══════════ -->
{% if 'tracker' in secs %}<section class="sec" id="tracker">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['tracker'] }} / {{ seclabel['tracker'] }}</span>
      <h2 class="stitle">The book.</h2>
    </div>
    <div style="display:flex;gap:9px;align-items:center;flex-wrap:wrap">
      <form action="/tracker/obsidian" method="post" style="display:inline">
        <button type="submit" class="btn-gh v">Sync Obsidian</button>
      </form>
      <a class="slink" href="/tracker/history" target="_blank">Exit history →</a>
      <button type="button" class="btn-gh" id="posHistBtn" style="display:none">Closed positions</button>
    </div>
  </div>

  <!-- Live book. Filled from /api/tracker; the server-rendered block below is
       the fallback for the static build. -->
  <div id="posLive" style="display:none"></div>

  <div class="keybox" id="keybox">
    <span>Editing the book needs your key. Stored in this browser only.</span>
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
    <h4>+ Add position manually</h4>
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
      <h2 class="stitle">One bucket a month.</h2>
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
      <h2 class="stitle" style="font-size:24px">Where the step-up takes it.</h2>
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
      <span class="snum">{{ secnum['funds'] }} / {{ seclabel['funds'] }}</span>
      <h2 class="stitle">Where the SIP goes.</h2>
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
  </div>

  <div class="fund-note rv">
    <strong>On expense ratio.</strong> Per-scheme TER is not published in the free
    AMFI feed, so this screen does not claim to know it. The cost lever that
    <em>is</em> visible is Direct versus Regular, and it is the big one: a Regular
    plan carries the distributor commission inside its TER, typically 0.5&ndash;1.2%
    a year more for the same portfolio. Everything below is Direct.
  </div>

  {% for cat in fund_screen.categories %}
  {% if cat.funds %}
  <div class="fundcat rv">
    <div class="fundcat-h">
      <h3>{{ cat.label }}</h3>
      <span class="ghost">{{ cat.screened }} screened</span>
    </div>
    <p class="fundcat-b">{{ cat.blurb }}</p>
    <div class="tw">
      <table class="t" style="min-width:640px">
        <thead><tr>
          <th scope="col">#</th><th scope="col">Fund</th><th scope="col" class="num">3Y</th><th scope="col" class="num">5Y</th>
          <th scope="col" class="num">Worst fall (3y)</th><th scope="col" class="num">NAV</th><th scope="col"></th>
        </tr></thead>
        <tbody>
          {% for f in cat.funds %}
          <tr>
            <td class="mono-dim">{{ loop.index }}</td>
            <td><strong>{{ f.name }}</strong><br>
                <span class="mono-dim" style="font-size:11px">{{ f.house }}</span></td>
            <td class="num up">{{ f.r3 }}%</td>
            <td class="num">{{ f.r5 if f.r5 is not none else '—' }}{{ '%' if f.r5 is not none else '' }}</td>
            <td class="num dn">{{ f.dd3 if f.dd3 is not none else '—' }}{{ '%' if f.dd3 is not none else '' }}</td>
            <td class="num mono-dim">{{ f.nav }}</td>
            <td><a href="{{ f.url }}" target="_blank" rel="noopener"
                   class="btn-gh" title="The NAV series this ranking was computed from">Data</a></td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
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
      <h2 class="stitle">And what it pays out.</h2>
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

<!-- ══════════ 05 INTERVIEW PREP ══════════ -->
{% if 'interview' in secs %}<section class="sec" id="interview">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['interview'] }} / {{ seclabel['interview'] }}</span>
      <h2 class="stitle">CFO in three years.</h2>
    </div>
    <p class="sdesc">Four questions a day — two technical, two not. Weighted to retail, the Gulf,
      and the controller-to-CFO jump. The non-technical ones decide the offer more often than the
      technical ones do.</p>
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
</section>{% endif %}

<!-- ══════════ 06 LANGUAGE ══════════ -->
{% if 'language' in secs %}<section class="sec" id="language">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['language'] }} / {{ seclabel['language'] }}</span>
      <h2 class="stitle">Two tongues, sharper.</h2>
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
      <h2 class="stitle">Jainism and Buddhism.</h2>
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



<!-- ══════════ 05 THE DESK ══════════ -->
{% if 'desk' in secs %}<section class="sec" id="desk">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['desk'] }} / {{ seclabel['desk'] }}</span>
      <h2 class="stitle">Compound the skill.</h2>
    </div>
    <p class="sdesc">FP&amp;A, the CFO ladder, a case study, a book, and one hack — rotating daily.
      Seven tabs, one discipline.</p>
  </div>

  <div class="tabs rv" id="deskTabs">
    <button class="tab on" data-p="d1">🎓 FP&amp;A · {{ fpna.index }}/{{ fpna.total }}</button>
    <button class="tab" data-p="d2">🇦🇪 Dubai · {{ dubai.index }}/{{ dubai.total }}</button>
    <button class="tab" data-p="d3">🏆 FC → CFO · {{ cfo.index }}/{{ cfo.total }}</button>
    <button class="tab" data-p="d4">📊 Case Study</button>
    <button class="tab" data-p="d5">📚 Book · {{ book.index }}/{{ book.total }}</button>
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

    <div class="pane" id="d5">
      <div class="essay" style="--ac:var(--violet)">
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

<!-- ══════════ 06 THE MIND ══════════ -->
{% if 'mind' in secs %}<section class="sec" id="mind">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['mind'] }} / {{ seclabel['mind'] }}</span>
      <h2 class="stitle">Sharpen the operator.</h2>
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
      <h2 class="stitle">Simple living. High thinking.</h2>
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
      <h2 class="stitle">Look back, or none of it compounds.</h2>
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

<!-- ══════════ 09 CHESS ══════════ -->
{% if 'chess' in secs %}<section class="sec" id="chess">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['chess'] }} / {{ seclabel['chess'] }}</span>
      <h2 class="stitle">{% if lichess_summary.is_yesterday %}Yesterday&rsquo;s chess.{% else %}Your last session.{% endif %}</h2>
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
      <div class="meta" style="font-family:var(--mono);font-size:10px;letter-spacing:1.6px;text-transform:uppercase;color:var(--down);margin-bottom:6px">📚 Study this opening</div>
      <div style="font-size:14px;color:#FFA0A0">{{ lichess_summary.weak_op }}</div>
    </div>
    {% endif %}
    {% if lichess_summary.best_op %}
    <div class="card" style="border-color:rgba(61,220,151,.25)">
      <div class="meta" style="font-family:var(--mono);font-size:10px;letter-spacing:1.6px;text-transform:uppercase;color:var(--up);margin-bottom:6px">💪 Strongest opening</div>
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
        <span style="font-size:17px">{{ g.icon }}</span>
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
      <div style="font-family:var(--mono);font-size:10px;letter-spacing:1.6px;text-transform:uppercase;color:var(--dim);margin-bottom:8px">{{ g.speed }}</div>
      <div class="num" style="font-size:30px;font-weight:700;letter-spacing:-1.4px;color:{% if g.pct >= 55 %}var(--up){% elif g.pct >= 45 %}var(--gold){% else %}var(--down){% endif %}">{{ g.pct }}%</div>
      <div style="font-size:12.5px;color:var(--muted);margin-top:7px">
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
    <div style="font-family:var(--mono);font-size:10px;letter-spacing:1.8px;text-transform:uppercase;color:var(--dim);margin-bottom:6px">📈 7-day win rate</div>
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
<!-- ══════════ MUSIC ══════════
     Three crates. Two are yours (edit music.py to add a line; the 6 AM build
     picks it up), the third is a fixed all-time canon. Five show, the rest are
     one click away — a shelf you can see the whole of is a shelf you stop
     scanning. The five on top rotate daily, so the shelf reads differently
     every morning without the list changing. -->
<!-- ══════════ SMART READS ══════════
     The wire tells you what happened; these argue about what it means. Same
     named mastheads, but the analysis and money desks rather than the market
     report, and a card only ships when the publisher gave it a real summary —
     a headline with a border round it is a link, not a read.

     Filtered harder than the news feed (two distinct finance terms, not one).
     Opinion desks run film and language columns beside the money writing, and
     one incidental word is how a review of The Odyssey reached a finance
     page during the build of this section. -->
{% if 'smartreads' in secs %}<section class="sec" id="smartreads">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['smartreads'] }} / {{ seclabel['smartreads'] }}</span>
      <h2 class="stitle">Worth the ten minutes.</h2>
    </div>
    <!-- Copy rewritten when this stopped being a finance-only section. It used
         to name five money mastheads, which was accurate then and would have
         been quietly wrong the moment the other four categories landed. -->
    <p class="sdesc">Analysis, not headlines, and deliberately not all about money
      &mdash; markets and personal finance alongside habits and focus, health and
      longevity, psychology and relationships, and the longer essays on thinking
      and living well. Every card carries the publisher&rsquo;s own summary, so you
      know what a piece argues before you open it.</p>
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
      {% if r.link %}<a class="readmore" href="{{ r.link }}" target="_blank" rel="noopener">Read more &rarr;</a>{% endif %}
    </article>
    {% endfor %}
  </div>
</section>{% endif %}

<!-- ══════════ PODCASTS ══════════
     Twenty long-form episodes from Indian shows, newest first, one line of what
     each is about. Everything here is the publisher's: title, date, link and
     a takeaway compressed from their OWN episode description. Nothing is
     inferred from a title and nothing is written about a guest from general
     knowledge — see the header of podcasts.py for why that line is drawn hard.
     Round-robin by show, so a channel posting three times a day cannot own
     the list. Rebuilt DAILY — it was weekly, which left new episodes from
     every-other-day publishers unlisted for up to a week.

     Shorts are excluded by asking YouTube whether each id is one, not by
     looking for "#shorts" in the title. Most Shorts do not say so: "This SWP
     Mistake Can Destroy Your Retirement Plan!" reads exactly like an episode
     and is forty seconds long. See podcasts._is_short. -->
{% if 'podcasts' in secs %}<section class="sec" id="podcasts">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['podcasts'] }} / {{ seclabel['podcasts'] }}</span>
      <h2 class="stitle">What&rsquo;s worth listening to.</h2>
    </div>
    <p class="sdesc">Long-form Indian podcasts across everything &mdash; business,
      investing and money, society, politics and geopolitics, health, psychology,
      philosophy, education, comedy and culture. Thirty-four channels, up to twenty
      episodes, newest first, with what each one says it covers. Titles, dates and
      takeaways come from the shows themselves. Shorts are excluded per video, not
      per channel.</p>
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

{% if 'music' in secs %}<section class="sec" id="music">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['music'] }} / {{ seclabel['music'] }}</span>
      <h2 class="stitle">What&rsquo;s playing.</h2>
    </div>
    <p class="sdesc">{{ music.total }} tracks across three crates &mdash; mine,
      bhakti, and the all-time canon. The top five rotate every morning. Tap a
      title and it plays here &mdash; full length, free, no account needed.
      {% if music.playable < music.total %}<br><span style="color:var(--dim)">{{ music.total - music.playable }} of
      them isn&rsquo;t pinned yet and still opens a search.</span>{% endif %}</p>
  </div>

  <div class="crates rv">
    {% for crate in [{'key':'songs','icon':'🎧','name':'Anything','items':music.songs},
                     {'key':'bhakti','icon':'🪔','name':'Bhakti','items':music.bhakti},
                     {'key':'global','icon':'🌍','name':'All time','items':music['global']}] %}
    <div class="crate" data-crate="{{ crate.key }}">
      <div class="crate-h">
        <span class="ic">{{ crate.icon }}</span>
        <span class="nm">{{ crate.name }}</span>
        <span class="ct">{{ crate['items']|length }}</span>
      </div>
      <ol class="crate-l">
        {% for t in crate['items'] %}
        <li class="trk{% if loop.index > music.top_n %} more{% endif %}"
            data-title="{{ t.title }}" data-artist="{{ t.artist }}" data-url="{{ t.url }}"
            data-vid="{{ t.vid }}" data-embed="{{ t.embed }}" data-apple="{{ t.apple_url }}">
          <span class="no">{{ "%02d"|format(loop.index) }}</span>
          <!-- Still a real anchor to YouTube. It is the no-JS path and the
               middle-click/"open in new tab" path, and the click handler
               calls preventDefault only when it can actually play in-page. -->
          <a href="{{ t.url }}" target="_blank" rel="noopener">
            <span class="ti">{{ t.title }}</span>
            <span class="ar">{{ t.artist }}</span>
          </a>
          <button type="button" class="pl" title="Play here"
                  aria-label="Play {{ t.title }} on this page">▶</button>
          <!-- A real <button>, not a span with a click handler: this is a
               control, so it needs to be tabbable and to announce its state.
               aria-pressed carries "liked" to a screen reader; the heart glyph
               alone carries it to everyone else. -->
          <button type="button" class="lk" aria-pressed="false"
                  title="Save to my songs" aria-label="Save {{ t.title }} to my songs">♥</button>
        </li>
        {% endfor %}
      </ol>
      {% if crate['items']|length > music.top_n %}
      <button type="button" class="crate-more" aria-expanded="false">
        Show all {{ crate['items']|length }} &darr;
      </button>
      {% endif %}
    </div>
    {% endfor %}

    <!-- The fourth crate. Empty in the 6 AM build and filled by /api/music on
         load, because likes arrive through the day and the page is static.
         Hidden until it has something in it — an empty shelf next to three
         full ones reads like a bug. -->
    <div class="crate" data-crate="liked" id="likedCrate" style="display:none">
      <div class="crate-h">
        <span class="ic">♥</span>
        <span class="nm">Liked</span>
        <span class="ct" id="likedCt">0</span>
      </div>
      <ol class="crate-l" id="likedList"></ol>
      <button type="button" class="crate-more" aria-expanded="false" id="likedMore"
              style="display:none">Show all &darr;</button>
    </div>
  </div>
  <div class="crate-note" id="likeNote" role="status" aria-live="polite"></div>

  <!-- Docked player. Audio only — this is the Apple Music widget, not a video
       embed, so pressing play gives sound and nothing to watch.

       Deliberately NOT one iframe per row: a row-level embed stops the moment
       you scroll to another crate, and forty idle iframes is forty
       third-party connections. One player, created on first play and reused,
       keeps the music running while you read the rest of the page. It is
       built in JS rather than sitting here with a src, so the page contacts
       Apple only once you actually press play.

       Full length, free, no account. YouTube's embed terms require the player
       stay visible, so the dock is kept small and pinned out of the way rather
       than collapsed to an audio bar — the honest version of "audio only" when
       no free full-length audio-only source exists. -->
  <div class="player" id="player" hidden>
    <div class="player-h">
      <span class="player-t" id="playerT">—</span>
      <a class="player-a" id="playerA" href="#" target="_blank" rel="noopener"
         title="Open this track in Apple Music" style="display:none">&#63743;</a>
      <a class="player-y" id="playerY" href="#" target="_blank" rel="noopener"
         title="Open this track on YouTube">Open ↗</a>
      <button type="button" class="player-x" id="playerX" aria-label="Close player">✕</button>
    </div>
    <div class="player-f" id="playerF"></div>
  </div>
</section>{% endif %}

{% if 'gym' in secs %}<section class="sec" id="gym">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['gym'] }} / {{ seclabel['gym'] }}</span>
      <h2 class="stitle">Six minutes. Sharper.</h2>
    </div>
    <p class="sdesc">A new set every day, same set for the whole day. Numbers under time
      pressure, estimation, recall, and the two calculations a trading desk actually runs.
      Scores stay in this browser.</p>
  </div>

  <div class="gym-tabs rv" id="gymTabs"></div>
  <div class="gym-stage rv" id="gymStage"></div>
  <div class="gym-score rv" id="gymScore"></div>
</section>{% endif %}

<!-- ══════════ 11 PERFORMANCE ══════════
     Entirely live. Hidden on a static host, where there is no ledger to
     compute an edge from. -->
{% if 'perf' in secs %}<section class="sec" id="perf" style="display:none">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['perf'] }} / {{ seclabel['perf'] }}</span>
      <h2 class="stitle">Does this actually work?</h2>
    </div>
    <p class="sdesc">Win rate, expectancy and drawdown over the full ledger — closed signals only.
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
{% if 'rules' in secs %}<section class="sec" id="rules">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['rules'] }} / {{ seclabel['rules'] }}</span>
      <h2 class="stitle">What the ledger changed.</h2>
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
      <div class="elog-b">
        <h3 class="elog-h">{{ c.title }}</h3>
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
    </li>
    {% endfor %}
  </ol>
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
      <span class="snum">{{ secnum['stocks'] }} / {{ seclabel['stocks'] }}</span>
      <h2 class="stitle">Which five hundred, and why.</h2>
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
  </div>

  <!-- Breadth, measured across these same five hundred companies rather than
       inferred from an index. It is the only market-wide reading on the page
       that counts businesses instead of instruments, and it is deliberately not
       an input to any score — see stock_screen.breadth(). -->
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

  <div class="fund-note rv">
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

  <div class="ctlbar rv" id="scrPresets" role="group" aria-label="Preset screens">
    <span class="ghost" style="margin-left:0">SCREENS</span>
    <button type="button" class="fbtn on" data-preset="all">All</button>
    <button type="button" class="fbtn" data-preset="compounders">Quality compounders</button>
    <button type="button" class="fbtn" data-preset="cheapquality">Cheap &amp; good</button>
    <button type="button" class="fbtn" data-preset="growth">High growth</button>
    <button type="button" class="fbtn" data-preset="breakout">Breakouts</button>
    <button type="button" class="fbtn" data-preset="rs">RS leaders</button>
    <button type="button" class="fbtn" data-preset="oversold">Oversold</button>
    <button type="button" class="fbtn" data-preset="debtfree">Debt-free</button>
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
      <option value="rev_cagr">Revenue CAGR</option>
      <option value="r1y">1-year return</option>
      <option value="mcap_cr">Market cap</option>
    </select>
    <button type="button" class="fbtn" id="scrReset">Reset</button>
    <span class="ghost" id="scrCount">{{ stock_screen.count or 0 }} companies</span>
  </div>

  <div class="tw tw-tall rv">
    <table class="t" id="scrTable" style="min-width:1180px">
      <thead><tr>
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
    Screen rebuilt weekly. A high score means &ldquo;ranked well on published
    numbers&rdquo; and nothing more.
  </p>
</section>
{% endif %}

<!-- ══════════ 10 SIGNAL LOG ══════════ -->
{% if 'alerts' in secs %}<section class="sec" id="alerts">
  <div class="shead rv">
    <div>
      <span class="snum">{{ secnum['alerts'] }} / {{ seclabel['alerts'] }}</span>
      <h2 class="stitle">Every signal, scored.</h2>
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
    <div class="ghost" style="font-family:var(--mono);font-size:10px;color:var(--dim);margin:-4px 0 4px">
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
    <button class="fbtn" data-f="cancelled">Cancelled</button>
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
        <th scope="col">Date</th><th scope="col">Symbol</th><th scope="col">Signal</th><th scope="col">TF</th><th scope="col">Grade</th><th scope="col">Entry</th><th scope="col">SL</th>
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
          <td class="{{ 'up' if a.action == 'BUY' else 'dn' }}" style="font-weight:600">{{ a.action }}{% if a.signal_type %}<span class="mono-dim" style="font-size:10px"> · {{ a.signal_type }}</span>{% endif %}</td>
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
    <p class="fine">One email a day at 6 AM IST. Not investment advice — a public log of what I actually did.</p>
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
      <h4>THE DAILY <b>SIGNAL</b></h4>
      <p style="color:var(--muted);font-size:13.5px;margin-top:12px;max-width:38ch">
        Built by Akshay Kothari. Rebuilt every morning at 6 AM IST by a machine that does not sleep.</p>
    </div>
    <div class="m">
      <a href="https://instagram.com/askakshayfinance" target="_blank" style="color:var(--lime)">@askakshayfinance</a><br>
      news.askakshay.com<br>
      {{ date_str }} · {{ updated_at }} IST
    </div>
  </div>

  <div class="foot-legal">
    <div>
      <h4>What this is, and is not</h4>
      <p>A public log of signals generated by my own engine and the trades I take against
        them. Every signal is recorded when it fires and scored when it closes, wins and
        losses alike. <strong>It is not investment advice and I am not a SEBI-registered
        adviser.</strong> Nothing here is a recommendation to buy or sell. Markets can and
        do take the whole position; size accordingly.</p>
    </div>
    <div>
      <h4>Your email</h4>
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
      <h4>Reach me</h4>
      <p><a href="https://askakshay.com" target="_blank" rel="noopener">askakshay.com</a> ·
         <a href="https://www.linkedin.com/in/akkothari" target="_blank" rel="noopener">LinkedIn</a> ·
         <a href="mailto:ca.akkothari@gmail.com">Email</a><br>
         &copy; 2026 Akshay K Kothari, CA</p>
    </div>
  </div>
</footer>

<button class="fab" id="fab" aria-label="Back to top">↑</button>

<script type="application/json" id="tv-aliases" nonce="{{ nonce }}">{{ tv_aliases|tojson }}</script>
<script nonce="{{ nonce }}" src="/app.js?v={{ build_id }}" defer></script>
</body>
</html>"""

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
            "spanish": d["spanish"], "vocab": d["vocab"], "speaking": d["speaking"],
            "father": d["father"], "life_wisdom": d["wisdom"],
        }
    except Exception as e:
        log.warning(f"learning tracks: {e}")
        return {"interview_tech": [], "interview_soft": [], "spanish": [],
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

        return render_template_string(TEMPLATE,
            tv_aliases=TV_ALIASES,
            date_str=now.strftime("%A, %B %d %Y"),
            updated_at=now.strftime("%H:%M"),
            markets=markets, news=news, fpna=fpna, cfo=cfo,
            chess=chess, wisdom=wisdom, book=book, way=way_ctx, review=review_ctx,
            top5=top5, tracker=tracker, money_hack=money, dubai=dubai, daughter=daughter, music=music_lib,
            fund_screen=get_fund_screen(),
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
# 6 AM IST DAILY REFRESH SCHEDULER
# ─────────────────────────────────────────────────────────────

def _daily_6am_refresh():
    """Fires at 6 AM IST (00:30 UTC) — clears all caches, rebuilds picks."""
    log.info("6 AM IST refresh: clearing all caches")
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
    log.info("6 AM IST refresh: done — fresh content ready")

def _start_scheduler():
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(_daily_6am_refresh, CronTrigger(hour=0, minute=30, timezone="UTC"))  # 6 AM IST
    sched.start()
    log.info("Scheduler: daily refresh at 06:00 IST (00:30 UTC)")
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
