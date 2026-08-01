#!/usr/bin/env python3
"""
THE DAILY SIGNAL — Akshay's Personal Intelligence Brief
Sections: Weather · World News · Markets · Quote · Wisdom/Dad · Chess · FP&A→CFO
          Business Case Study · Top 5 Picks · Stock Tracker · Money Hack · Productivity
Refreshes at 6 AM IST daily. Deploy: news.askakshay.com
"""
from __future__ import annotations

import os, json, sqlite3, logging, time, threading
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
            json={"model": "llama3-8b-8192", "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": max_tokens, "temperature": 0.7},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.warning(f"Groq: {e}")
    return ""

def ai_stock_thesis(symbol: str, mom_1m: float, mom_3m: float, score: int) -> str:
    prompt = (f"Stock: {symbol}. 1M: {mom_1m:.1f}%. 3M: {mom_3m:.1f}%. Score: {score}/100. "
              "One sentence (max 20 words), numbers-first, why 20-30% return in 1-3 months. No fluff.")
    result = groq_complete(prompt, max_tokens=60)
    return result or f"Strong {mom_3m:.0f}% 3-month momentum with bullish trend structure."

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

def fetch_dubai_jobs() -> list[dict]:
    return get_cached_jobs()

def fetch_markets() -> list[dict]:
    return get_cached_markets()

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
            headers={"Accept": "application/json"}, timeout=15,
        )
        if r.status_code != 200:
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
    """Fetch individual games via export API. Requires LICHESS_TOKEN."""
    token = os.environ.get("LICHESS_TOKEN", "")
    if not token:
        return []
    try:
        r = requests.get(
            f"https://lichess.org/api/games/user/{LICHESS_USER.lower()}",
            # evals/accuracy/division/clocks power the best-move pick, the key
            # facts and the strength estimate. All four are only populated for
            # games Lichess has actually analysed, so every consumer below must
            # degrade gracefully when they are absent.
            params={"since": since_ms, "until": until_ms,
                    "opening": "true", "moves": "true", "max": 50,
                    "evals": "true", "accuracy": "true",
                    "division": "true", "clocks": "true"},
            headers={"Accept": "application/x-ndjson",
                     "Authorization": f"Bearer {token}"},
            timeout=25, stream=True,
        )
        if r.status_code != 200:
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

    token = os.environ.get("LICHESS_TOKEN", "")
    if token:
        # Full mode — individual games
        raw = _lichess_export_games(
            int(day_start.timestamp() * 1000),
            int(day_end.timestamp() * 1000),
        )
        if raw:
            games = [_parse_game(g) for g in raw]
            # Attach trend + mode marker on first game
            games[0]["_mode"]  = "full"
            games[0]["_trend"] = trend
            games[0]["_yest_counts"] = yest_counts
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
    }

def fetch_lichess_puzzle() -> dict:
    """Daily Lichess puzzle with theme tip."""
    import re
    try:
        r = requests.get("https://lichess.org/api/puzzle/daily",
                         headers={"Accept":"application/json"}, timeout=10)
        if r.status_code != 200:
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

def score_stock(sym: str) -> Optional[dict]:
    try:
        hist     = yf.Ticker(sym).history(period="3mo")
        if hist.empty or len(hist) < 20: return None
        close    = hist["Close"]
        ema20    = close.ewm(span=20).mean().iloc[-1]
        ema50    = close.ewm(span=50).mean().iloc[-1]
        price    = close.iloc[-1]
        mom_1m   = (price - close.iloc[-22]) / close.iloc[-22] * 100 if len(close) >= 22 else 0
        mom_3m   = (price - close.iloc[0])   / close.iloc[0]  * 100
        vol_ratio = hist["Volume"].iloc[-5:].mean() / (hist["Volume"].iloc[-20:].mean() or 1)
        score    = sum([
            25 if price > ema20  else 0,
            20 if price > ema50  else 0,
            15 if ema20 > ema50  else 0,
            20 if mom_1m > 5     else 0,
            10 if mom_3m > 10    else 0,
            10 if vol_ratio > 1.2 else 0,
        ])
        currency = "₹" if ".NS" in sym or ".BO" in sym else "$"
        target   = round(price * (1.25 if mom_3m > 15 else 1.20), 2)
        return {"symbol": sym, "name": sym.replace(".NS","").replace(".BO",""),
                "price": round(price, 2), "change_1d": round((price - close.iloc[-2]) / close.iloc[-2] * 100, 2),
                "mom_1m": round(mom_1m, 1), "mom_3m": round(mom_3m, 1), "score": score,
                "target": target, "stop_loss": round(price * 0.92, 2),
                "timeframe": "2–3 months", "currency": currency, "thesis": ""}
    except Exception as e:
        log.warning(f"score_stock {sym}: {e}")
        return None

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
        s["thesis"] = ai_stock_thesis(s["name"], s["mom_1m"], s["mom_3m"], s["score"])
        time.sleep(0.1)
    return top5

def _week_key() -> str:
    """ISO week key e.g. '2026-W23' — picks refresh every Monday."""
    d = date.today()
    return f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"

def _warm_picks_cache():
    week = _week_key()
    with _db() as con:
        row = con.execute("SELECT picks FROM newspaper_stocks_picked WHERE pick_date=?", (week,)).fetchone()
        if row:
            with _picks_lock:
                _picks_cache[week] = json.loads(row["picks"])
            log.info(f"picks: loaded from DB cache ({week})")
            return
    log.info(f"picks: warming cache for {week} — scanning {len(WATCHLIST)} stocks...")
    picks = _build_picks()
    with _db() as con:
        con.execute("INSERT OR REPLACE INTO newspaper_stocks_picked VALUES (?,?)", (week, json.dumps(picks)))
    with _picks_lock:
        _picks_cache[week] = picks
    log.info(f"picks: cached {len(picks)} top picks for {week}")

def get_top5_picks() -> list[dict]:
    week = _week_key()
    with _picks_lock:
        if week in _picks_cache: return _picks_cache[week]
    with _db() as con:
        row = con.execute("SELECT picks FROM newspaper_stocks_picked WHERE pick_date=?", (week,)).fetchone()
        if row:
            picks = json.loads(row["picks"])
            with _picks_lock: _picks_cache[week] = picks
            return picks
    return []

# ─────────────────────────────────────────────────────────────
# STOCK TRACKER
# ─────────────────────────────────────────────────────────────

def get_tracker_stocks() -> list[dict]:
    with _db() as con:
        rows = con.execute("SELECT * FROM stock_tracker WHERE status='active' ORDER BY added_date DESC").fetchall()
    out = []
    for r in rows:
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
<meta name="theme-color" content="#08090A">
<title>THE DAILY SIGNAL — {{ date_str }}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fira+Sans:ital,wght@0,300;0,400;0,500;0,600;0,700;0,800;1,400&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
/* ═══════════════════ TOKENS ═══════════════════ */
:root{
  --bg:#08090A; --bg2:#0B0C0E; --surface:#121316; --surface2:#17181C;
  --line:rgba(255,255,255,.08); --line2:rgba(255,255,255,.15);
  --lime:#B8EF43; --lime-soft:rgba(184,239,67,.12); --lime-line:rgba(184,239,67,.3);
  --text:#F0F0F0; --muted:#8A9099; --dim:#5A6068;
  --up:#3DDC97; --down:#FF5C5C; --gold:#E8C547; --blue:#6AA8FF; --violet:#A78BFA;
  --mono:'JetBrains Mono',ui-monospace,monospace;
  --sans:'Fira Sans',-apple-system,BlinkMacSystemFont,sans-serif;
  --ease:cubic-bezier(.22,1,.36,1);
  --gut:clamp(16px,4vw,40px);
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth;scroll-padding-top:120px;-webkit-text-size-adjust:100%}
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
.headstack{position:sticky;top:0;z-index:300}
.topbar{background:rgba(8,9,10,.82);
  backdrop-filter:blur(18px) saturate(150%);-webkit-backdrop-filter:blur(18px) saturate(150%);
  border-bottom:1px solid var(--line);}
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
.nav{background:rgba(8,9,10,.9);
  backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);border-bottom:1px solid var(--line);}
.nav-in{max-width:1400px;margin:0 auto;padding:0 var(--gut);display:flex;gap:2px;
  overflow-x:auto;scrollbar-width:none;}
.nav-in::-webkit-scrollbar{display:none}
.nav a{position:relative;padding:11px 13px;font-size:11px;font-weight:500;letter-spacing:1.1px;
  text-transform:uppercase;color:var(--dim);white-space:nowrap;transition:color .25s var(--ease);}
.nav a i{font-style:normal;font-family:var(--mono);font-size:9px;color:#33363c;margin-right:5px;transition:color .25s}
.nav a::after{content:'';position:absolute;left:13px;right:13px;bottom:0;height:2px;background:var(--lime);
  transform:scaleX(0);transform-origin:left;transition:transform .35s var(--ease);}
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
@media(max-width:640px){.stat{flex:1 1 44%;padding:16px 14px 16px 0}}


/* ═══════════════════ TICKER ═══════════════════ */
.tickwrap{position:relative;border-top:1px solid var(--line);border-bottom:1px solid var(--line);
  background:var(--bg2);overflow:hidden;z-index:2;}
.tickwrap::before,.tickwrap::after{content:'';position:absolute;top:0;bottom:0;width:90px;z-index:3;pointer-events:none}
.tickwrap::before{left:0;background:linear-gradient(90deg,var(--bg2),transparent)}
.tickwrap::after{right:0;background:linear-gradient(270deg,var(--bg2),transparent)}
.tick{display:flex;width:max-content;animation:marquee 46s linear infinite;}
.tickwrap:hover .tick{animation-play-state:paused}
@keyframes marquee{from{transform:translateX(0)}to{transform:translateX(-50%)}}
.ti{display:flex;align-items:center;gap:9px;padding:11px 22px;border-right:1px solid var(--line);white-space:nowrap;}
.ti .n{font-size:10px;letter-spacing:1.4px;text-transform:uppercase;color:var(--dim);font-weight:500}
.ti .p{font-family:var(--mono);font-size:13px;font-weight:600}
.ti .c{font-family:var(--mono);font-size:12px;font-weight:700}

/* ═══════════════════ SECTIONS ═══════════════════ */
main{position:relative;z-index:2;max-width:1400px;margin:0 auto;padding:0 var(--gut)}
.sec{padding:clamp(56px,8vw,104px) 0;border-bottom:1px solid var(--line)}
.sec:last-child{border-bottom:none}
.shead{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;flex-wrap:wrap;margin-bottom:clamp(26px,4vw,44px)}
.snum{font-family:var(--mono);font-size:11px;color:var(--lime);letter-spacing:2px;margin-bottom:12px;display:block}
.stitle{font-size:clamp(26px,4.4vw,50px);font-weight:700;letter-spacing:-1.8px;line-height:1}
.sdesc{font-size:13px;color:var(--muted);max-width:44ch;line-height:1.55}
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
/* ═══════════════════ 01 MARKETS ═══════════════════ */
.mkt-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px}
.mkt{position:relative;background:var(--surface);border:1px solid var(--line);border-radius:14px;
  padding:18px;overflow:hidden;transition:border-color .35s,transform .35s var(--ease)}
.mkt:hover{transform:translateY(-3px);border-color:var(--line2)}
.mkt::after{content:'';position:absolute;left:0;top:0;bottom:0;width:2px;transition:width .3s var(--ease)}
.mkt.u::after{background:var(--up)} .mkt.d::after{background:var(--down)}
.mkt:hover::after{width:4px}
.mkt .n{font-size:10.5px;letter-spacing:1.6px;text-transform:uppercase;color:var(--dim);font-weight:500}
.mkt .p{font-family:var(--mono);font-size:24px;font-weight:700;letter-spacing:-1px;margin:8px 0 4px}
.mkt .c{font-family:var(--mono);font-size:13px;font-weight:700;display:flex;align-items:center;gap:5px}
.spark{position:absolute;right:0;bottom:0;opacity:.13;pointer-events:none}
@media(max-width:520px){.mkt-grid{grid-template-columns:1fr 1fr;gap:9px}.mkt{padding:14px}.mkt .p{font-size:19px}}

/* ═══════════════════ 02 PICKS ═══════════════════ */
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
  border-bottom:1px solid var(--line);background:rgba(8,9,10,.9);
  backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px)}
.livebar.on{display:flex}
.livebar .pip{width:6px;height:6px;border-radius:50%;background:var(--up);flex:none;
  animation:pulse 2.4s var(--ease) infinite}
.livebar.stale .pip{background:var(--gold);animation:none}
.livebar.off .pip{background:var(--dim);animation:none}
.livebar .msg{color:var(--muted);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.livebar button{font:inherit;color:var(--lime);background:none;border:1px solid var(--lime-line);
  border-radius:999px;padding:3px 11px;cursor:pointer;flex:none}
.livebar button:hover{background:var(--lime-soft)}

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
<div class="grain"></div>
<div class="vgrid"></div>
<div class="progress" id="prog"></div>

{% set wins   = alerts | selectattr("badge","eq","win")  | list | length %}
{% set losses = alerts | selectattr("badge","eq","loss") | list | length %}
{% set opens  = alerts | selectattr("badge","eq","open") | list | length %}
{% set closed = wins + losses %}
{% set winrate = ((wins / closed * 100) | round(0) | int) if closed > 0 else 0 %}
{% set advancers = markets | selectattr("up") | list | length %}

<!-- ══════════ HEADER ══════════ -->
<div class="headstack">
<header class="topbar">
  <div class="topbar-in">
    <a href="#top" class="brand"><span class="dot"></span>THE DAILY <b>SIGNAL</b></a>
    <div class="stamp">
      <span class="d">{{ date_str }}</span>
      <span class="live" id="istClock"><i></i>{{ updated_at }} IST</span>
    </div>
  </div>
</header>

<nav class="nav">
  <div class="nav-in" id="navin">
    <a href="#markets"><i>01</i>Markets</a>
    <a href="#picks"><i>02</i>Trade Ideas</a>
    <a href="#tracker"><i>03</i>Portfolio</a>
    <a href="#sip"><i>04</i>SIP Buckets</a>
    <a href="#perf"><i>05</i>Performance</a>
    <a href="#interview"><i>05</i>Interview</a>
    <a href="#language"><i>06</i>Language</a>
    <a href="#father"><i>07</i>Father</a>
    <a href="#wisdom"><i>08</i>Wisdom</a>
    <a href="#gym"><i>09</i>Mind Gym</a>
    <a href="#alerts"><i>10</i>Signal Log</a>
    <a href="#world"><i>11</i>World</a>
    <a href="#desk"><i>12</i>The Desk</a>
    <a href="#mind"><i>13</i>The Mind</a>
    <a href="#way"><i>14</i>The Way</a>
    <a href="#review"><i>15</i>The Review</a>
    <a href="#chess"><i>16</i>Chess</a>
  </div>
</nav>

<!-- Live-layer status. Hidden until the API probe resolves one way or the other. -->
<div class="livebar" id="livebar">
  <span class="pip"></span>
  <span class="msg" id="livemsg">Checking live ledger…</span>
  <button type="button" id="liverefresh">Refresh</button>
</div>
</div><!-- /.headstack -->

<!-- ══════════ HERO ══════════ -->
<section class="hero" id="top">
  <div class="orb a"></div><div class="orb b"></div>

  <div class="eyebrow">◆ Compiled 6:00 AM IST · {{ date_str }}</div>

  <h1 class="hl">
    <span class="w"><span style="--d:.05s">Numbers</span></span>
    <span class="w"><span style="--d:.13s">first.</span></span><br>
    <span class="w"><span style="--d:.24s"><em>Noise</em></span></span>
    <span class="w"><span style="--d:.32s"><em>last.</em></span></span>
  </h1>

  <p class="hero-sub">Markets, live signals, the world, and the work — one page, rebuilt every
    morning before the open. No feeds. No scroll trap. Just what moved and what to do about it.</p>

  <div class="statrail">
    <div class="stat">
      <div class="v" id="heroRate" style="color:var(--lime)" data-count="{{ winrate }}" data-suffix="%">0%</div>
      <div class="k">Signal Win Rate</div>
    </div>
    <div class="stat">
      <div class="v" id="heroOpen" style="color:var(--blue)" data-count="{{ opens }}">0</div>
      <div class="k">Open Positions</div>
    </div>
    <div class="stat">
      <div class="v" id="heroTotal" data-count="{{ alerts|length }}">0</div>
      <div class="k">Signals Logged</div>
    </div>
    <div class="stat">
      <div class="v" style="color:{{ 'var(--up)' if advancers >= (markets|length / 2) else 'var(--down)' }}"
           data-count="{{ advancers }}" data-total="{{ markets|length }}">0</div>
      <div class="k">Markets Advancing</div>
    </div>
  </div>

</section>

<!-- ══════════ TICKER ══════════ -->
<div class="tickwrap">
  <div class="tick" id="tickRail">
    {% for dup in [1,2] %}{% for m in markets %}
    <div class="ti">
      <span class="n">{{ m.name }}</span>
      <span class="p">{{ m.price }}</span>
      <span class="c {{ 'up' if m.up else 'dn' }}">{{ '▲' if m.up else '▼' }} {{ m.change }}</span>
    </div>
    {% endfor %}{% endfor %}
  </div>
</div>

<main>

<!-- ══════════ 01 MARKETS ══════════ -->
<section class="sec" id="markets">
  <div class="shead rv">
    <div>
      <span class="snum">01 / MARKETS</span>
      <h2 class="stitle">What moved.</h2>
    </div>
    <p class="sdesc" id="mktDesc">{{ advancers }} of {{ markets|length }} tracked instruments are green.
      Indices, commodities, crypto and FX.</p>
  </div>
  <div class="mkt-grid" id="mktGrid">
    {% for m in markets %}
    <div class="mkt {{ 'u' if m.up else 'd' }} rv" style="--d:{{ loop.index0 * 0.04 }}s">
      <div class="n">{{ m.name }}</div>
      <div class="p">{{ m.price }}</div>
      <div class="c {{ 'up' if m.up else 'dn' }}">{{ '▲' if m.up else '▼' }} {{ m.change }}</div>
    </div>
    {% endfor %}
    {% if not markets %}<div class="empty" style="grid-column:1/-1">Market data loading…</div>{% endif %}
  </div>
</section>

<!-- ══════════ 02 TRADE IDEAS ══════════ -->
<section class="sec" id="picks">
  <div class="shead rv">
    <div>
      <span class="snum">02 / CONVICTION</span>
      <h2 class="stitle">Top 5 trade ideas.</h2>
    </div>
    <p class="sdesc">Global 200 universe — India, US, global. Scored, ranked, refreshed weekly.
      Target 20–30%. Every idea carries a stop.</p>
  </div>
  {% if top5 %}
  <div class="pick-grid">
    {% for s in top5 %}
    <div class="pick rv" style="--d:{{ loop.index0 * 0.07 }}s">
      <div class="rank">{{ "%02d"|format(loop.index) }}</div>
      <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:10px">
        <div class="sym">{{ s.name }}</div>
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
  <div class="empty rv">📅 Picks refresh every Monday via GitHub Actions — check back after 6 AM IST Monday.</div>
  {% endif %}
</section>

<!-- ══════════ 03 PORTFOLIO ══════════ -->
<section class="sec" id="tracker">
  <div class="shead rv">
    <div>
      <span class="snum">03 / POSITIONS</span>
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
        <th>Symbol</th><th>Entry</th><th>Current</th><th>Target</th><th>Stop</th>
        <th>P&amp;L</th><th>Horizon</th><th>Thesis</th><th>Added</th><th></th>
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
        <input type="text" name="symbol" placeholder="Symbol e.g. RELIANCE.NS" required>
        <input type="text" name="name" placeholder="Name">
        <input type="number" step="0.01" name="entry_price" placeholder="Entry price" required>
        <input type="number" step="0.01" name="target_price" placeholder="Target price" required>
      </div>
      <div class="frow">
        <input type="number" step="0.01" name="stop_loss" placeholder="Stop loss">
        <input type="text" name="timeframe" placeholder="Timeframe" value="2-3 months">
        <input type="text" name="thesis" placeholder="Why this stock?" style="flex:3">
      </div>
      <button type="submit" class="btn">Add to book</button>
    </form>
  </div>
</section>

<!-- ══════════ 04 SIP BUCKETS ══════════
     ₹10,000/month, stepped up 10% each SIP year, one bucket per month, four
     names per bucket. Every bucket keeps its own cost basis and its own XIRR —
     a blended portfolio number would hide which months' picks actually worked,
     which is the only feedback the ranking engine gets.
     Filled from /api/sip; the whole section hides itself on a static build. -->
<section class="sec" id="sip" style="display:none">
  <div class="shead rv">
    <div>
      <span class="snum">04 / COMPOUNDING</span>
      <h2 class="stitle">One bucket a month.</h2>
    </div>
    <span class="slink" id="sipPlan">—</span>
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
    <th>Year</th><th>Monthly</th><th>Invested</th><th>@10%</th><th>@12%</th><th>@14%</th>
  </tr></thead><tbody></tbody></table></div>
  <p class="note rv" style="margin-top:10px;color:var(--dim);font-size:12px">
    Projections are compound arithmetic on the contribution schedule, not a forecast.
    They assume the return shown is achieved every year with no gaps in contribution.
    Actual equity returns arrive in a very different order, and sequence matters.
  </p>
</section>

<!-- ══════════ 05 INTERVIEW PREP ══════════ -->
<section class="sec" id="interview">
  <div class="shead rv">
    <div>
      <span class="snum">05 / THE SEAT</span>
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
</section>

<!-- ══════════ 06 LANGUAGE ══════════ -->
<section class="sec" id="language">
  <div class="shead rv">
    <div>
      <span class="snum">06 / LANGUAGE</span>
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
</section>

<!-- ══════════ 07 FATHERHOOD ══════════ -->
<section class="sec" id="father">
  <div class="shead rv">
    <div>
      <span class="snum">07 / THE FATHER</span>
      <h2 class="stitle">Seven months old.</h2>
    </div>
    <p class="sdesc">Two things to actually do today, and the reason each one matters. Most of it
      is presence rather than technique — but the technique is not nothing.</p>
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
</section>

<!-- ══════════ 08 WISDOM ══════════ -->
<section class="sec" id="wisdom">
  <div class="shead rv">
    <div>
      <span class="snum">08 / HOW TO LIVE</span>
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
</section>

<!-- ══════════ 04 WORLD ══════════ -->
<section class="sec" id="world">
  <div class="shead rv">
    <div>
      <span class="snum">04 / CONTEXT</span>
      <h2 class="stitle">The world, last 24h.</h2>
    </div>
    <p class="sdesc">Wires only. Deduplicated, ranked, and cut to what actually changes a decision.</p>
  </div>

  {% if news %}
    {% set lead = news[0] %}
    <div class="lead rv">
      <div class="lead-m">
        <span class="tag">{{ lead.source }} · LEAD</span>
        <h2>{% if lead.link %}<a href="{{ lead.link }}" target="_blank">{{ lead.title }}</a>{% else %}{{ lead.title }}{% endif %}</h2>
        <p>{{ lead.summary }}</p>
      </div>
      <div class="lead-s">
        {% for item in news[1:6] %}
        <div class="mini">
          <span class="s">{{ item.source }}</span>
          {% if item.link %}<a href="{{ item.link }}" target="_blank">{{ item.title }}</a>{% else %}<a>{{ item.title }}</a>{% endif %}
        </div>
        {% endfor %}
      </div>
    </div>

    <div class="news-grid">
      {% for item in news[6:15] %}
      <div class="ncard rv" style="--d:{{ loop.index0 * 0.05 }}s">
        <span class="s">{{ item.source }}</span>
        <h3>{% if item.link %}<a href="{{ item.link }}" target="_blank">{{ item.title }}</a>{% else %}{{ item.title }}{% endif %}</h3>
        <p>{{ item.summary[:150] }}</p>
        <div class="ts">{{ item.published }}</div>
      </div>
      {% endfor %}
    </div>
  {% else %}
    <div class="empty rv">Loading feeds…</div>
  {% endif %}
</section>

<!-- ══════════ 05 THE DESK ══════════ -->
<section class="sec" id="desk">
  <div class="shead rv">
    <div>
      <span class="snum">05 / THE DESK</span>
      <h2 class="stitle">Compound the skill.</h2>
    </div>
    <p class="sdesc">FP&amp;A, the CFO ladder, a case study, a book, and one hack — rotating daily.
      Seven tabs, one discipline.</p>
  </div>

  <div class="tabs rv" id="deskTabs">
    <button class="tab on" data-p="d1">🎓 FP&amp;A · {{ fpna.index }}/{{ fpna.total }}</button>
    <button class="tab" data-p="d2">🇦🇪 Dubai</button>
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
        <div class="meta">Dubai Corner · AED 30K+ Track</div>
        <h3>The stack that clears AED 30K</h3>
        <p>CA or ACCA, plus SAP or Oracle, plus Power BI, plus IFRS 9 and 16. That is the whole gate.
          Miss one and you are competing on price.</p>
        <div class="q">Targets: ADNOC · Emirates · Majid Al Futtaim · DP World · FAB · Emaar.</div>
        <div class="act"><b>Keyword tip</b>Put "IFRS 16 implementation" and "rolling forecast" in the cover letter.
          Recruiters grep for exactly those two strings.</div>
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
</section>

<!-- ══════════ 06 THE MIND ══════════ -->
<section class="sec" id="mind">
  <div class="shead rv">
    <div>
      <span class="snum">06 / THE MIND</span>
      <h2 class="stitle">Sharpen the operator.</h2>
    </div>
    <p class="sdesc">One quote, one lesson from the world, one rule for being a better person and a better dad.</p>
  </div>

  <div class="quote-hero rv">
    <div class="mark">&ldquo;</div>
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
</section>

<!-- ══════════ 07 THE WAY ══════════ -->
<section class="sec" id="way">
  <div class="shead rv">
    <div>
      <span class="snum">07 / THE WAY</span>
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
</section>

<!-- ══════════ 08 THE REVIEW ══════════ -->
<section class="sec" id="review">
  <div class="shead rv">
    <div>
      <span class="snum">08 / THE REVIEW</span>
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
</section>

<!-- ══════════ 09 CHESS ══════════ -->
<section class="sec" id="chess">
  <div class="shead rv">
    <div>
      <span class="snum">08 / THE BOARD</span>
      <h2 class="stitle">Yesterday's chess.</h2>
    </div>
    <div style="text-align:right">
      <p class="sdesc">AKK_010 on Lichess. Pattern over volume — review the turning point, not the result.</p>
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
  <div class="empty rv" style="text-align:left">⚡ Add <code style="color:var(--lime);font-family:var(--mono)">LICHESS_TOKEN</code> to GitHub secrets for per-game analysis, openings, key moves and AI coaching.
    <a href="https://lichess.org/account/oauth/token/create" target="_blank" style="color:var(--lime)">Create token →</a></div>
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
</section>

<!-- ══════════ 10 MIND GYM ══════════
     Pure client-side: deterministic daily seed, scores in localStorage. No
     API, so it works identically on the static host. -->
<section class="sec" id="gym">
  <div class="shead rv">
    <div>
      <span class="snum">10 / MIND GYM</span>
      <h2 class="stitle">Six minutes. Sharper.</h2>
    </div>
    <p class="sdesc">A new set every day, same set for the whole day. Numbers under time
      pressure, estimation, recall, and the two calculations a trading desk actually runs.
      Scores stay in this browser.</p>
  </div>

  <div class="gym-tabs rv" id="gymTabs"></div>
  <div class="gym-stage rv" id="gymStage"></div>
  <div class="gym-score rv" id="gymScore"></div>
</section>

<!-- ══════════ 11 PERFORMANCE ══════════
     Entirely live. Hidden on a static host, where there is no ledger to
     compute an edge from. -->
<section class="sec" id="perf" style="display:none">
  <div class="shead rv">
    <div>
      <span class="snum">10 / EDGE</span>
      <h2 class="stitle">Does this actually work?</h2>
    </div>
    <p class="sdesc">Win rate, expectancy and drawdown over the full ledger — closed signals only.
      Open signals are excluded, because counting them is how a 50% system starts looking like an 80% one.</p>
  </div>

  <div class="ctlbar rv">
    <select id="perfTf"><option value="">All timeframes</option></select>
    <select id="perfRange">
      <option value="">All time</option>
      <option value="30">Last 30 days</option>
      <option value="90">Last 90 days</option>
      <option value="365">Last 12 months</option>
    </select>
    <span class="ghost" id="perfBasis"></span>
  </div>

  <div class="perf-grid rv" id="perfGrid"></div>
  <div class="brk rv" id="perfBrk"></div>
</section>

<!-- ══════════ 10 SIGNAL LOG ══════════ -->
<section class="sec" id="alerts">
  <div class="shead rv">
    <div>
      <span class="snum">09 / TRACK RECORD</span>
      <h2 class="stitle">Every signal, scored.</h2>
    </div>
    <div style="text-align:right">
      <p class="sdesc">Nothing hidden. Every Telegram alert ever sent, with entry, stop, target and outcome.</p>
      <a class="slink" href="alerts.json" target="_blank" style="display:inline-block;margin-top:10px">↓ alerts.json</a>
    </div>
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
    <input type="search" id="alertSearch" placeholder="Search symbol — e.g. BAJFINANCE" autocomplete="off">
    <input type="date" id="alertFrom" aria-label="From date">
    <input type="date" id="alertTo" aria-label="To date">
    <select id="alertTfSel"><option value="">All timeframes</option></select>
    <span class="ghost" id="alertCount"></span>
  </div>

  {% if alerts %}
  <div class="kpi-row rv">
    <div class="kpi"><div class="v up" id="kpiWin" data-count="{{ wins }}">0</div><div class="k">Targets Hit</div></div>
    <div class="kpi"><div class="v dn" id="kpiLoss" data-count="{{ losses }}">0</div><div class="k">Stops Hit</div></div>
    <div class="kpi"><div class="v" id="kpiOpen" style="color:var(--blue)" data-count="{{ opens }}">0</div><div class="k">Open</div></div>
    <div class="kpi"><div class="v" id="kpiRate" style="color:var(--lime)" data-count="{{ winrate }}" data-suffix="%">0%</div><div class="k">Win Rate</div></div>
    <div class="kpi"><div class="v" id="kpiTotal" data-count="{{ alerts|length }}">0</div><div class="k">Total Signals</div></div>
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
  <div class="filters rv" id="alertVer" style="margin-top:6px;display:none">
    <button class="fbtn on" data-v="v2">Gated (v2)</button>
    <button class="fbtn" data-v="v1">Legacy (v1)</button>
    <button class="fbtn" data-v="all">Both</button>
  </div>

  <div class="tw rv">
    <table class="t" id="alertTable">
      <thead><tr>
        <th>Date</th><th>Symbol</th><th>Signal</th><th>TF</th><th>Grade</th><th>Entry</th><th>SL</th>
        <th>T1</th><th>T2</th><th>RR</th><th>B/E WR</th><th>Exit</th><th>P&amp;L</th><th>Closed</th><th>Status</th>
      </tr></thead>
      <tbody>
      {% for a in alerts %}
        <tr data-badge="{{ a.badge }}">
          <td class="mono-dim">{{ a.alert_date }}</td>
          <td><a class="sym" href="https://www.tradingview.com/chart/?symbol=NSE:{{ a.symbol }}" target="_blank">{{ a.symbol }}</a></td>
          <td class="{{ 'up' if a.action == 'BUY' else 'dn' }}" style="font-weight:600">{{ a.action }}{% if a.signal_type %}<span class="mono-dim" style="font-size:10px"> · {{ a.signal_type }}</span>{% endif %}</td>
          <td class="mono-dim">{{ a.timeframe or '—' }}</td>
          <td class="mono-dim">{{ a.grade or '—' }}</td>
          <td class="num">{% if a.entry %}₹{{ "%.2f"|format(a.entry) }}{% else %}—{% endif %}</td>
          <td class="num dn">{% if a.sl %}₹{{ "%.2f"|format(a.sl) }}{% else %}—{% endif %}</td>
          <td class="num up">{% if a.target1 %}₹{{ "%.2f"|format(a.target1) }}{% else %}—{% endif %}</td>
          <td class="num up">{% if a.target2 %}₹{{ "%.2f"|format(a.target2) }}{% else %}—{% endif %}</td>
          <td class="num" style="color:var(--gold)">{{ a.rr or '—' }}{% if a.rr %}x{% endif %}</td>
          {# Break-even win rate, 1/(1+R) — the same fact as R:R, made visible. #}
          <td class="num mono-dim">{% if a.rr %}{{ "%.0f"|format(100.0 / (1.0 + a.rr)) }}%{% else %}—{% endif %}</td>
          <td class="num">{% if a.exit_price %}₹{{ "%.2f"|format(a.exit_price) }}{% else %}—{% endif %}</td>
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
</section>

</main>

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
      {{ date_str }} · {{ updated_at }} IST<br>
      <span style="color:#2E3238">Built with Claude Code</span>
    </div>
  </div>
</footer>

<button class="fab" id="fab" aria-label="Back to top">↑</button>

<script>
(function(){
  var RM = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ── scroll progress + fab ── */
  var prog = document.getElementById('prog'), fab = document.getElementById('fab'), ticking = false;
  function onScroll(){
    var h = document.documentElement.scrollHeight - window.innerHeight;
    prog.style.width = (h > 0 ? (window.scrollY / h) * 100 : 0) + '%';
    fab.classList.toggle('on', window.scrollY > 700);
    ticking = false;
  }
  window.addEventListener('scroll', function(){
    if (!ticking){ ticking = true; requestAnimationFrame(onScroll); }
  }, {passive:true});
  onScroll();
  fab.addEventListener('click', function(){ window.scrollTo({top:0, behavior: RM ? 'auto' : 'smooth'}); });

  /* ── reveal on scroll ── */
  var revs = document.querySelectorAll('.rv');
  if (RM || !('IntersectionObserver' in window)){
    revs.forEach(function(e){ e.classList.add('in'); });
  } else {
    // threshold must stay 0 (any pixel intersecting), not a ratio. A ratio of
    // 0.05 is unsatisfiable for anything taller than 20x the viewport, and the
    // signal log grows past that as soon as the live feed loads a few hundred
    // rows — the table would then never reveal and the section would render blank.
    var ro = new IntersectionObserver(function(entries){
      entries.forEach(function(en){
        if (en.isIntersecting){ en.target.classList.add('in'); ro.unobserve(en.target); }
      });
    }, {rootMargin:'0px 0px -8% 0px', threshold:0});
    revs.forEach(function(e){ ro.observe(e); });
  }

  /* ── count-up ── */
  function countUp(el){
    var target = parseFloat(el.dataset.count) || 0,
        suffix = el.dataset.suffix || '',
        total  = el.dataset.total ? '/' + el.dataset.total : '',
        dur = 1100, t0 = null;
    if (RM){ el.textContent = target + suffix + total; return; }
    function step(ts){
      // The live layer may replace this number mid-animation with the real
      // value from the ledger. It marks the node when it does; keep animating
      // past that and the page settles back on the stale snapshot figure.
      if (el.dataset.live) return;
      if (!t0) t0 = ts;
      var p = Math.min((ts - t0) / dur, 1),
          e = 1 - Math.pow(1 - p, 3);
      el.textContent = Math.round(target * e) + suffix + total;
      if (p < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }
  var nums = document.querySelectorAll('[data-count]');
  if (!('IntersectionObserver' in window)){
    nums.forEach(countUp);
  } else {
    var co = new IntersectionObserver(function(entries){
      entries.forEach(function(en){
        if (en.isIntersecting){ countUp(en.target); co.unobserve(en.target); }
      });
    }, {threshold:0.4});
    nums.forEach(function(e){ co.observe(e); });
  }

  /* ── nav active section ── */
  var links = [].slice.call(document.querySelectorAll('.nav a')),
      secs  = links.map(function(a){ return document.querySelector(a.getAttribute('href')); });
  function setActive(){
    var best = 0, y = window.scrollY + 200;
    secs.forEach(function(s, i){ if (s && s.offsetTop <= y) best = i; });
    links.forEach(function(a, i){ a.classList.toggle('on', i === best); });
    var el = links[best];
    if (el && el.offsetLeft !== undefined){
      var bar = document.getElementById('navin');
      if (el.offsetLeft < bar.scrollLeft || el.offsetLeft > bar.scrollLeft + bar.clientWidth - 100){
        bar.scrollTo({left: el.offsetLeft - 20, behavior:'smooth'});
      }
    }
  }
  window.addEventListener('scroll', setActive, {passive:true});
  setActive();

  /* ── alert filters ── */
  document.querySelectorAll('.fbtn').forEach(function(b){
    b.addEventListener('click', function(){
      document.querySelectorAll('.fbtn').forEach(function(x){ x.classList.remove('on'); });
      b.classList.add('on');
      var f = b.dataset.f;
      document.querySelectorAll('#alertTable tbody tr').forEach(function(r){
        r.style.display = (f === 'all' || r.dataset.badge === f) ? '' : 'none';
      });
    });
  });

  /* ── tab groups (desk, way) ──
     Scoped to the owning <section>. The previous version cleared '.pane'
     document-wide, so a second tab group anywhere on the page would blank the
     first group's open pane on every click. */
  document.querySelectorAll('.tabs').forEach(function(group){
    var sec = group.closest('section') || document;
    group.querySelectorAll('.tab').forEach(function(t){
      t.addEventListener('click', function(){
        group.querySelectorAll('.tab').forEach(function(x){ x.classList.remove('on'); });
        sec.querySelectorAll('.pane').forEach(function(p){ p.classList.remove('on'); });
        t.classList.add('on');
        var pane = sec.querySelector('#' + t.dataset.p) || document.getElementById(t.dataset.p);
        if (pane) pane.classList.add('on');
      });
    });
  });

  /* ── practice streak + weekly review (localStorage, no server) ──
     The published site is static GitHub Pages, so there is nowhere to persist
     server-side. State is per-browser and per-device by design; the review
     exports to Markdown so anything worth keeping leaves the browser. */
  (function(){
    var TRACKS = [
      ["minimalism","🪶 Minimalism"], ["etiquette","🤝 Etiquette"],
      ["stillness","🧘 Stillness"],   ["model","⚙️ Model"],
      ["arabic","🇦🇪 Arabic"],        ["drill","🎯 Drill"],
      ["health","💪 Health"]
    ];
    var KEY = "tds.practice.v1";

    function todayKey(d){
      d = d || new Date();
      return d.getFullYear() + "-" + String(d.getMonth()+1).padStart(2,"0")
           + "-" + String(d.getDate()).padStart(2,"0");
    }
    function load(){
      try { return JSON.parse(localStorage.getItem(KEY) || "{}") || {}; }
      catch(e){ return {}; }
    }
    function save(d){
      try { localStorage.setItem(KEY, JSON.stringify(d)); } catch(e){}
    }
    function dayCount(store, key){
      var v = store[key];
      return (v && v.length) ? v.length : 0;
    }
    /* Streak counts back from today. Today not yet ticked does not break it —
       the day is still in progress; yesterday empty does. */
    function streak(store){
      var n = 0, d = new Date();
      if (dayCount(store, todayKey(d)) === 0) d.setDate(d.getDate() - 1);
      for(;;){
        if (dayCount(store, todayKey(d)) === 0) break;
        n++; d.setDate(d.getDate() - 1);
        if (n > 3650) break;
      }
      return n;
    }
    function best(store){
      var keys = Object.keys(store).filter(function(k){ return dayCount(store,k) > 0; }).sort();
      var run = 0, top = 0, prev = null;
      keys.forEach(function(k){
        var d = new Date(k + "T00:00:00");
        if (prev && Math.round((d - prev) / 86400000) === 1) run++; else run = 1;
        if (run > top) top = run;
        prev = d;
      });
      return top;
    }

    var box = document.getElementById("streakBox");
    if (box){
      box.hidden = false;
      var store = load();
      var checks = document.getElementById("stkChecks");

      TRACKS.forEach(function(t){
        var id = t[0], label = t[1];
        var lab = document.createElement("label");
        lab.className = "stk-c";
        var cb = document.createElement("input");
        cb.type = "checkbox"; cb.dataset.track = id;
        var span = document.createElement("span"); span.textContent = label;
        lab.appendChild(cb); lab.appendChild(span);
        cb.addEventListener("change", function(){
          var st = load(), k = todayKey(), cur = st[k] || [];
          if (cb.checked){ if (cur.indexOf(id) < 0) cur.push(id); }
          else { cur = cur.filter(function(x){ return x !== id; }); }
          if (cur.length) st[k] = cur; else delete st[k];
          save(st); render();
        });
        checks.appendChild(lab);
      });

      function render(){
        var st = load(), today = st[todayKey()] || [];
        checks.querySelectorAll("input").forEach(function(cb){
          cb.checked = today.indexOf(cb.dataset.track) >= 0;
          cb.parentNode.classList.toggle("done", cb.checked);
        });
        document.getElementById("stkCur").textContent  = streak(st);
        document.getElementById("stkBest").textContent = Math.max(best(st), streak(st));

        var strip = document.getElementById("stkStrip");
        strip.innerHTML = "";
        var hit = 0;
        for (var i = 29; i >= 0; i--){
          var d = new Date(); d.setDate(d.getDate() - i);
          var k = todayKey(d), c = dayCount(st, k);
          if (c > 0) hit++;
          var cell = document.createElement("div");
          cell.className = "stk-d" + (c >= 5 ? " p3" : c >= 3 ? " p2" : c > 0 ? " p1" : "")
                         + (i === 0 ? " today" : "");
          cell.title = k + " — " + c + " of " + TRACKS.length;
          strip.appendChild(cell);
        }
        document.getElementById("stkRate").textContent = Math.round(hit / 30 * 100) + "%";
      }
      render();
    }

    /* ── weekly review ── */
    var grid = document.getElementById("reviewGrid");
    if (grid){
      var week = grid.dataset.week;
      var RKEY = "tds.review." + week;
      var fields = ["rvNumbers","rvWins","rvMisses","rvAnswer","rvChange"];
      var saved = {};
      try { saved = JSON.parse(localStorage.getItem(RKEY) || "{}") || {}; } catch(e){}

      function status(){
        var filled = fields.filter(function(id){
          var el = document.getElementById(id);
          return el && el.value.trim().length > 0;
        }).length;
        var el = document.getElementById("rvStatus");
        el.textContent = filled === 0 ? "Not started"
          : filled === fields.length ? ("Complete · saved for " + week)
          : (filled + " of " + fields.length + " · saved for " + week);
      }

      fields.forEach(function(id){
        var el = document.getElementById(id);
        if (!el) return;
        if (saved[id]) el.value = saved[id];
        el.addEventListener("input", function(){
          var cur = {};
          fields.forEach(function(f){
            var e = document.getElementById(f);
            if (e && e.value.trim()) cur[f] = e.value;
          });
          try { localStorage.setItem(RKEY, JSON.stringify(cur)); } catch(e){}
          status();
        });
      });
      status();

      var copyBtn = document.getElementById("rvCopy");
      if (copyBtn){
        copyBtn.addEventListener("click", function(){
          function val(id){ var e = document.getElementById(id); return e ? e.value.trim() : ""; }
          var q = grid.parentNode.querySelector(".deep-q h3");
          var md = "## Weekly Review — " + week + "\n\n"
            + "### The numbers\n" + (val("rvNumbers") || "—") + "\n\n"
            + "### Wins\n" + (val("rvWins") || "—") + "\n\n"
            + "### Misses\n" + (val("rvMisses") || "—") + "\n\n"
            + "### " + (q ? q.textContent : "This week's question") + "\n"
            + (val("rvAnswer") || "—") + "\n\n"
            + "### One change for next week\n" + (val("rvChange") || "—") + "\n";
          function done(ok){
            copyBtn.textContent = ok ? "Copied" : "Copy failed";
            setTimeout(function(){ copyBtn.textContent = "Copy week as Markdown"; }, 1800);
          }
          if (navigator.clipboard && navigator.clipboard.writeText){
            navigator.clipboard.writeText(md).then(function(){ done(true); },
                                                   function(){ done(false); });
          } else {
            var ta = document.createElement("textarea");
            ta.value = md; document.body.appendChild(ta); ta.select();
            var ok = false;
            try { ok = document.execCommand("copy"); } catch(e){}
            document.body.removeChild(ta); done(ok);
          }
        });
      }
    }
  })();

  /* ═══════════════════════════════════════════════════════════════════
     LIVE LAYER

     The page shell is rebuilt once a day. The trading data is not — it
     comes from /api, which reads the same Turso ledger the scanner writes
     to, so a signal that fired ten minutes ago shows up on the next load.

     If /api is unreachable (plain static host, or the deploy lost its env
     vars) everything below no-ops and the server-rendered snapshot stays
     exactly as it is. Nothing here is allowed to break the daily paper.
     ═══════════════════════════════════════════════════════════════════ */
  (function(){
    var API      = '/api';
    var KEY_LS   = 'ds_edit_key';
    var live     = false;
    var allRows  = [];      // last signal set pulled from /api/signals
    var archDate = null;    // when set, we are looking at one archived day

    function el(id){ return document.getElementById(id); }

    // The scroll-reveal IntersectionObserver registers once, at load, over the
    // .rv elements that exist and are laid out at that moment. Anything the
    // live layer un-hides or injects afterwards was never observed, so it
    // would sit at opacity:0 for good. Every render path calls this.
    function reveal(root){
      if (!root) return;
      var nodes = [];
      if (root.classList && root.classList.contains('rv')) nodes.push(root);
      root.querySelectorAll('.rv').forEach(function(n){ nodes.push(n); });
      nodes.forEach(function(n){
        // Snap, never animate. These nodes appear in response to an API call,
        // not to a scroll, so there is no reveal to choreograph — and a .75s
        // opacity transition on a signal table tens of thousands of pixels tall
        // means compositing a layer that size, which is where it gets stuck.
        n.style.transition = 'none';
        n.classList.add('in');
      });
    }
    function fmt(n, d){
      if (n === null || n === undefined || !isFinite(n)) return '—';
      return Number(n).toFixed(d === undefined ? 2 : d);
    }
    function esc(s){
      return String(s === null || s === undefined ? '' : s)
        .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
        .replace(/"/g,'&quot;');
    }
    function money(v){ return v === null || v === undefined ? '—' : '₹' + fmt(v, 2); }
    function editKey(){ try { return localStorage.getItem(KEY_LS) || ''; } catch(e){ return ''; } }

    function api(path, opts){
      opts = opts || {};
      var h = opts.headers || {};
      if (opts.method === 'POST'){
        h['Content-Type'] = 'application/json';
        h['x-edit-key'] = editKey();
      }
      opts.headers = h;
      return fetch(API + path, opts).then(function(r){
        return r.json().catch(function(){ return { ok:false, error:'bad response' }; })
                .then(function(j){ if (!r.ok && j.ok === undefined) j.ok = false; return j; });
      });
    }

    function bar(state, msg){
      var b = el('livebar'); if (!b) return;
      b.classList.add('on');
      b.classList.remove('stale','off');
      if (state !== 'live') b.classList.add(state);
      el('livemsg').textContent = msg;
    }

    /* ── boot: is there a ledger behind this page? ── */
    api('/health').then(function(h){
      if (!h || !h.ok) throw new Error(h && h.error ? h.error : 'unreachable');
      live = true;
      var stamp = h.latest_signal_date || '—';
      // "0 open positions" used to sit directly above a table of 67 rows
      // badged OPEN. Both numbers were right and meant different things: a
      // tracked position is one you hold, an OPEN signal is a setup that has
      // not resolved. Naming them differently is the whole fix.
      var tracked = (h.tracked_positions !== undefined ? h.tracked_positions
                                                       : h.open_positions);
      var vtxt = '';
      if (h.by_version){
        var v2 = h.by_version.v2 || 0, v1 = h.by_version.v1 || 0;
        vtxt = ' · ' + v2 + ' gated (v2) / ' + v1 + ' legacy (v1)';
      }
      bar('live', 'LIVE LEDGER · ' + h.signals + ' signals' + vtxt +
                  ' · latest ' + stamp +
                  ' · ' + tracked + ' tracked · ' + (h.open_setups || 0) + ' open setups' +
                  (h.writes_enabled ? '' : ' · read-only (EDIT_KEY not set)'));
      start();
    }).catch(function(e){
      // Static host, or the API is down. Say so plainly instead of letting the
      // page pretend a 6 AM snapshot is live data.
      bar('off', 'STATIC SNAPSHOT · rebuilt daily at 6:00 AM IST · live ledger unavailable (' + e.message + ')');
      staticFallback();
      var rb = el('liverefresh'); if (rb) rb.style.display = 'none';
    });

    /* ── static fallback: keep the old behaviour, minus the alert() popup ── */
    function staticFallback(){
      document.querySelectorAll('form[action^="/tracker"], form[action^="/api"]').forEach(function(f){
        f.addEventListener('submit', function(ev){
          ev.preventDefault();
          var note = f.querySelector('.formnote');
          if (!note){
            note = document.createElement('div');
            note.className = 'formnote';
            note.style.cssText = 'margin-top:10px;font-size:12px;color:var(--gold)';
            f.appendChild(note);
          }
          note.textContent = 'This build has no ledger behind it — open news.askakshay.com to edit the book.';
        });
      });
      document.querySelectorAll('a[href^="/tracker"], a[href^="/api"]').forEach(function(a){
        a.addEventListener('click', function(ev){ ev.preventDefault(); });
        a.style.opacity = '.3'; a.style.cursor = 'not-allowed';
      });
    }

    function start(){
      el('perf').style.display = '';
      el('archWrap').style.display = '';
      el('alertCtl').style.display = 'flex';
      // Only meaningful with an API behind the page — a static host has one
      // baked-in snapshot and nothing to switch between.
      var av = el('alertVer'); if (av) av.style.display = 'flex';
      el('posStatic').style.display = 'none';
      el('posLive').style.display = '';
      el('posHistBtn').style.display = '';
      el('keybox').classList.toggle('on', !editKey());

      // Flask-only controls. There is no /tracker/* on the serverless host, so
      // rather than leave buttons that 404, hide them — "Closed positions"
      // above already replaces the exit-history link.
      document.querySelectorAll('form[action^="/tracker/obsidian"], a[href^="/tracker/history"]')
        .forEach(function(n){ n.style.display = 'none'; });

      ensureAlertTable();
      ['perf','archWrap','alertCtl','alerts','tracker'].forEach(function(id){ reveal(el(id)); });
      wireKey();
      wireTracker();
      wireAlertControls();
      wirePerfControls();

      loadSignals();
      loadArchive();
      loadStats();
      loadPositions();
      loadSip();

      el('liverefresh').addEventListener('click', function(){
        loadSignals(); loadStats(); loadPositions(); loadSip();
      });

      // Deep link: /day/2026-07-31 opens straight into that day's archive.
      var m = location.pathname.match(/^\/day\/(\d{4}-\d{2}-\d{2})/);
      if (m) selectDay(m[1]);
    }

    /* ═══════ edit key ═══════ */
    function wireKey(){
      el('keySave').addEventListener('click', function(){
        var v = el('keyInput').value.trim();
        if (!v) return;
        try { localStorage.setItem(KEY_LS, v); } catch(e){}
        el('keybox').classList.remove('on');
        loadPositions();
      });
      el('keyInput').addEventListener('keydown', function(ev){
        if (ev.key === 'Enter') el('keySave').click();
      });
    }

    /* ═══════ positions ═══════ */
    var showingHistory = false;

    function loadPositions(){
      var box = el('posLive');
      box.innerHTML = '<div class="empty">Loading the book…</div>';
      api('/tracker' + (showingHistory ? '?history=1' : '')).then(function(j){
        if (!j.ok) { box.innerHTML = '<div class="empty">Could not load positions: ' + esc(j.error) + '</div>'; return; }
        renderPositions(j);
      });
    }

    function renderPositions(j){
      var box = el('posLive');
      var rows = j.positions || [];
      if (!showingHistory) setKpi('heroOpen', rows.length);
      if (!rows.length){
        box.innerHTML = '<div class="empty">' +
          (showingHistory ? 'No closed positions yet.'
                          : 'No open positions. Hit <strong style="color:var(--lime)">+ Track</strong> on any trade idea, or add one below.') +
          '</div>';
        return;
      }
      var warn = rows.filter(function(r){ return r.alert; });
      var html = '';
      if (warn.length && !showingHistory){
        html += '<div class="ctlbar"><span class="ghost" style="margin-left:0;color:var(--gold)">⚠ ' +
                warn.length + ' position' + (warn.length > 1 ? 's need' : ' needs') + ' attention — ' +
                esc(warn.map(function(r){ return r.symbol + ' (' + r.alert.replace('-',' ') + ')'; }).join(', ')) +
                '</span></div>';
      }
      html += '<div class="tw"><table class="t" style="min-width:880px"><thead><tr>' +
              '<th>Symbol</th><th>Entry</th><th>Current</th><th>Target</th><th>Stop</th>' +
              '<th>P&amp;L</th><th>R</th><th>Horizon</th><th>Thesis</th><th>Added</th><th></th>' +
              '</tr></thead><tbody>';
      rows.forEach(function(r){
        var cur = r.currency || '₹';
        html += '<tr>' +
          '<td><strong class="sym">' + esc(r.symbol) + '</strong>' +
            (r.alert ? '<span class="pos-alert ' + r.alert + '">' + r.alert.replace('-',' ') + '</span>' : '') + '</td>' +
          '<td class="num">' + cur + fmt(r.entry_price) + '</td>' +
          '<td class="num ' + (r.winning ? 'up' : 'dn') + '">' + cur + fmt(r.current_price) + '</td>' +
          '<td class="num up">' + (r.target_price ? cur + fmt(r.target_price) : '—') + '</td>' +
          '<td class="num dn">' + (r.stop_loss ? cur + fmt(r.stop_loss) : '—') + '</td>' +
          '<td class="' + (r.winning ? 'pnl-u' : 'pnl-d') + '">' +
            (r.pnl_pct === null ? '—' : (r.pnl_pct > 0 ? '+' : '') + fmt(r.pnl_pct, 2) + '%') + '</td>' +
          '<td class="num">' + (r.r_multiple === null ? '—' : fmt(r.r_multiple, 2) + 'R') + '</td>' +
          '<td class="mono-dim">' + esc(r.timeframe) + '</td>' +
          '<td style="font-size:12px;color:var(--muted);max-width:220px">' + esc((r.thesis || '').slice(0, 70)) + '</td>' +
          '<td class="mono-dim">' + esc(r.added_date) + '</td>' +
          '<td>' + (showingHistory ? '<span class="mono-dim">' + esc(r.status) + '</span>'
                                   : '<button type="button" class="btn-gh" data-exit="' + r.id + '">Exit</button>') + '</td>' +
          '</tr>';
      });
      html += '</tbody></table></div>';
      box.innerHTML = html;
      reveal(box);

      box.querySelectorAll('[data-exit]').forEach(function(b){
        b.addEventListener('click', function(){
          if (!confirm('Close this position?')) return;
          b.disabled = true; b.textContent = '…';
          api('/tracker', { method:'POST', body: JSON.stringify({ action:'exit', id: Number(b.dataset.exit) }) })
            .then(function(r){
              if (!r.ok){ b.disabled = false; b.textContent = 'Exit'; keyError(r.error); return; }
              loadPositions();
            });
        });
      });
    }

    function keyError(msg){
      el('keybox').classList.add('on');
      el('keybox').querySelector('span').textContent = msg || 'Write refused.';
    }

    function wireTracker(){
      el('posHistBtn').addEventListener('click', function(){
        showingHistory = !showingHistory;
        this.textContent = showingHistory ? 'Open positions' : 'Closed positions';
        loadPositions();
      });

      // Both the manual form and every "+ Track" button on the trade ideas
      // become real writes instead of dead POSTs to a server that isn't there.
      document.querySelectorAll('form[action^="/tracker/add"]').forEach(function(f){
        f.addEventListener('submit', function(ev){
          ev.preventDefault();
          var d = {};
          new FormData(f).forEach(function(v, k){ d[k] = v; });
          var btn = f.querySelector('button[type=submit]');
          var label = btn ? btn.textContent : '';
          if (btn){ btn.disabled = true; btn.textContent = 'Saving…'; }
          api('/tracker', { method:'POST', body: JSON.stringify(d) }).then(function(r){
            if (btn){ btn.disabled = false; btn.textContent = r.ok ? '✓ Tracked' : label; }
            if (!r.ok){ keyError(r.error); return; }
            f.reset();
            loadPositions();
            if (btn) setTimeout(function(){ btn.textContent = label; }, 2200);
          });
        });
      });
    }

    /* ═══════ signal feed + search ═══════ */

    // The KPI row, filter buttons and table only exist when the daily build
    // found signals. If the build ran while the ledger was unreachable, the
    // section holds an empty-state div instead — rebuild the scaffolding so
    // live data still has somewhere to land.
    function ensureAlertTable(){
      if (document.querySelector('#alertTable tbody')) return;
      var sec = el('alerts');
      var empty = sec.querySelector('.empty');
      var host = document.createElement('div');
      host.innerHTML =
        '<div class="kpi-row rv">' +
          '<div class="kpi"><div class="v up" id="kpiWin">0</div><div class="k">Targets Hit</div></div>' +
          '<div class="kpi"><div class="v dn" id="kpiLoss">0</div><div class="k">Stops Hit</div></div>' +
          '<div class="kpi"><div class="v" id="kpiOpen" style="color:var(--blue)">0</div><div class="k">Open</div></div>' +
          '<div class="kpi"><div class="v" id="kpiRate" style="color:var(--lime)">—</div><div class="k">Win Rate</div></div>' +
          '<div class="kpi"><div class="v" id="kpiTotal">0</div><div class="k">Total Signals</div></div>' +
        '</div>' +
        '<div class="filters rv">' +
          '<button class="fbtn on" data-f="all">All</button>' +
          '<button class="fbtn" data-f="open">Open</button>' +
          '<button class="fbtn" data-f="win">Target Hit</button>' +
          '<button class="fbtn" data-f="loss">Stop Hit</button>' +
          '<button class="fbtn" data-f="cancelled">Cancelled</button>' +
        '</div>' +
        // Engine generation. v1 is every signal before the quality gate; it is
        // kept, not deleted, because it is the 476-trade control group the new
        // gate has to beat. Default view is v2 only.
        '<div class="filters rv" style="margin-top:6px">' +
          '<button class="fbtn on" data-v="v2">Gated (v2)</button>' +
          '<button class="fbtn" data-v="v1">Legacy (v1)</button>' +
          '<button class="fbtn" data-v="all">Both</button>' +
        '</div>' +
        '<div class="tw rv"><table class="t" id="alertTable"><thead><tr>' +
          '<th>Date</th><th>Symbol</th><th>Signal</th><th>TF</th><th>Grade</th><th>Entry</th><th>SL</th>' +
          '<th>T1</th><th>T2</th><th>RR</th><th>B/E WR</th><th>Exit</th><th>P&amp;L</th><th>Closed</th><th>Status</th>' +
        '</tr></thead><tbody></tbody></table></div>';
      if (empty) empty.replaceWith(host); else sec.appendChild(host);
      reveal(host);
    }

    function activeVersion(){
      var on = document.querySelector('.fbtn.on[data-v]');
      return on ? on.dataset.v : 'v2';
    }

    function loadSignals(){
      var qs = archDate ? '?date=' + archDate + '&limit=2000' : '?limit=800';
      qs += '&version=' + activeVersion();
      api('/signals' + qs).then(function(j){
        if (!j.ok) return;
        allRows = j.signals || [];
        fillTfSelect(el('alertTfSel'), allRows);
        renderAlerts();
      });
    }

    function fillTfSelect(sel, rows){
      if (!sel || sel.dataset.filled) return;
      var seen = {};
      rows.forEach(function(r){ if (r.timeframe) seen[r.timeframe] = 1; });
      Object.keys(seen).sort().forEach(function(tf){
        var o = document.createElement('option');
        o.value = tf; o.textContent = tf; sel.appendChild(o);
      });
      sel.dataset.filled = '1';
    }

    function activeBadge(){
      var on = document.querySelector('.fbtn.on[data-f]');
      return on ? on.dataset.f : 'all';
    }

    function renderAlerts(){
      var tbody = document.querySelector('#alertTable tbody');
      if (!tbody) return;
      var q     = (el('alertSearch').value || '').trim().toUpperCase();
      var from  = el('alertFrom').value;
      var to    = el('alertTo').value;
      var tf    = el('alertTfSel').value;
      var badge = activeBadge();

      var rows = allRows.filter(function(r){
        if (badge !== 'all' && r.badge !== badge) return false;
        if (q && r.symbol.toUpperCase().indexOf(q) === -1) return false;
        if (from && r.date < from) return false;
        if (to   && r.date > to)   return false;
        if (tf   && r.timeframe !== tf) return false;
        return true;
      });

      // 800 rows of innerHTML is fine; building them one node at a time is not.
      var html = rows.map(function(a){
        var badgeTxt = a.badge === 'win' ? '✅ Win' : a.badge === 'loss' ? '❌ Stop'
                     : a.badge === 'open' ? '🔵 Open' : (a.status || '—');
        return '<tr data-badge="' + a.badge + '">' +
          '<td class="mono-dim">' + esc(a.date) + '</td>' +
          '<td><a class="sym" href="https://www.tradingview.com/chart/?symbol=NSE:' + encodeURIComponent(a.symbol) +
              '" target="_blank" rel="noopener">' + esc(a.symbol) + '</a></td>' +
          '<td class="' + (a.action === 'BUY' ? 'up' : 'dn') + '" style="font-weight:600">' + esc(a.action) +
              (a.signal_type ? '<span class="mono-dim" style="font-size:10px"> · ' + esc(a.signal_type) + '</span>' : '') + '</td>' +
          '<td class="mono-dim">' + esc(a.timeframe || '—') + '</td>' +
          '<td class="mono-dim">' + gradeCell(a) + '</td>' +
          '<td class="num">' + money(a.entry) + '</td>' +
          '<td class="num dn">' + money(a.sl) + '</td>' +
          '<td class="num up">' + money(a.target1) + '</td>' +
          '<td class="num up">' + money(a.target2) + '</td>' +
          '<td class="num" style="color:var(--gold)">' + (a.rr === null ? '—' : fmt(a.rr, 1) + 'x') + '</td>' +
          // The win rate this setup needs just to break even, 1/(1+R). Shown
          // next to R:R because the two are the same fact and only one of them
          // is obvious. v1 rows predate the stored column, so derive it from
          // R:R — those are precisely the rows worth seeing it on.
          '<td class="num mono-dim">' + beWr(a) + '</td>' +
          '<td class="num">' + money(a.exit_price) + '</td>' +
          '<td class="' + (a.pnl_pct > 0 ? 'pnl-u' : a.pnl_pct < 0 ? 'pnl-d' : 'num') + '">' + esc(a.pnl_str) + '</td>' +
          '<td class="mono-dim">' + esc(a.closed_at) + '</td>' +
          '<td><span class="badge badge-' + a.badge + '">' + badgeTxt + '</span></td>' +
          '</tr>';
      }).join('');

      tbody.innerHTML = html || '<tr><td colspan="15" style="padding:26px;text-align:center;color:var(--dim)">' +
                                (activeVersion() === 'v2'
                                  ? 'No gated signals yet. The v2 engine publishes only setups that clear ' +
                                    'their engine’s measured break-even R:R — quiet is the intended ' +
                                    'state. Switch to Legacy (v1) for the pre-gate history.'
                                  : 'Nothing matches those filters.') + '</td></tr>';

      // Spell out the full span. The table always held every signal, but with
      // the newest first it read as though the history stopped a week back.
      var span = '';
      if (allRows.length){
        var ds = allRows.map(function(r){ return r.date; }).filter(Boolean).sort();
        var uniq = ds.filter(function(v, i){ return ds.indexOf(v) === i; });
        span = ' · ' + ds[0] + ' → ' + ds[ds.length - 1] + ' · ' + uniq.length + ' trading days';
      }
      el('alertCount').textContent = rows.length + ' of ' + allRows.length + ' shown' + span +
        (archDate ? ' · archive ' + archDate : '');

      // KPI row reflects what is actually on screen.
      var w = rows.filter(function(r){ return r.badge === 'win';  }).length;
      var l = rows.filter(function(r){ return r.badge === 'loss'; }).length;
      var o = rows.filter(function(r){ return r.badge === 'open'; }).length;
      setKpi('kpiWin', w); setKpi('kpiLoss', l); setKpi('kpiOpen', o);
      setKpi('kpiRate', (w + l) ? Math.round(w / (w + l) * 100) + '%' : '—');
      setKpi('kpiTotal', rows.length);
    }

    function setKpi(id, v){
      var n = el(id); if (!n) return;
      // data-count drives a 1.1s count-up animation that writes textContent on
      // every frame. Removing the attribute is not enough — an already-running
      // loop captured its target and would animate straight over this value.
      // The flag makes that loop stand down.
      n.dataset.live = '1';
      n.removeAttribute('data-count');
      n.textContent = v;
    }

    function wireAlertControls(){
      ['alertSearch','alertFrom','alertTo','alertTfSel'].forEach(function(id){
        var n = el(id); if (!n) return;
        n.addEventListener('input', renderAlerts);
        n.addEventListener('change', renderAlerts);
      });
      // Take over the badge buttons. The original handler shows/hides rows
      // directly, which would fight with the search filter, so it is unbound
      // by cloning the node.
      document.querySelectorAll('.fbtn[data-f]').forEach(function(b){
        var c = b.cloneNode(true);
        b.parentNode.replaceChild(c, b);
        c.addEventListener('click', function(){
          document.querySelectorAll('.fbtn[data-f]').forEach(function(x){ x.classList.remove('on'); });
          c.classList.add('on');
          renderAlerts();
        });
      });
      // Engine-version buttons re-query the API rather than filtering in the
      // browser: the 800-row page would otherwise be drawn entirely from
      // whichever version happened to fill it.
      document.querySelectorAll('.fbtn[data-v]').forEach(function(b){
        b.addEventListener('click', function(){
          document.querySelectorAll('.fbtn[data-v]').forEach(function(x){ x.classList.remove('on'); });
          b.classList.add('on');
          loadSignals();
        });
      });
    }

    /* ═══════ SIP buckets ═══════ */
    function inr(v, d){
      if (v === null || v === undefined || !isFinite(v)) return '—';
      return '₹' + Number(v).toLocaleString('en-IN', {
        minimumFractionDigits: d === undefined ? 0 : d,
        maximumFractionDigits: d === undefined ? 0 : d });
    }

    function loadSip(){
      api('/sip').then(function(j){
        if (!j || !j.ok) return;
        var sec = el('sip'); if (!sec) return;
        sec.style.display = '';

        var p = j.plan || {};
        el('sipPlan').textContent = '₹' + Number(p.base_monthly || 0).toLocaleString('en-IN') +
          '/mo base · +' + (p.step_up_pct || 0) + '%/yr · SIP year ' + (p.sip_year || 1);
        el('sipMonthly').textContent = inr(p.monthly_amount);

        var t = j.totals || {};
        el('sipBuckets').textContent = t.buckets || 0;
        el('sipInvested').textContent = inr(t.invested);
        el('sipValue').textContent = inr(t.value);
        var pn = el('sipPnl');
        pn.textContent = (t.pnl === null || t.pnl === undefined) ? '—'
          : inr(t.pnl) + (t.pnl_pct === null ? '' : ' (' + fmt(t.pnl_pct, 1) + '%)');
        pn.className = 'v ' + (t.pnl > 0 ? 'up' : t.pnl < 0 ? 'dn' : '');

        // Projection table is pure arithmetic and always renders, even before
        // the first bucket exists — the plan is worth seeing on day one.
        var pb = document.querySelector('#sipProj tbody');
        if (pb) pb.innerHTML = (j.projections || []).map(function(r){
          return '<tr><td class="mono-dim">' + r.years + '</td>' +
            '<td class="num">' + inr(r.monthly) + '</td>' +
            '<td class="num mono-dim">' + inr(r.invested) + '</td>' +
            '<td class="num">' + inr(r.r10) + '</td>' +
            '<td class="num up">' + inr(r.r12) + '</td>' +
            '<td class="num up">' + inr(r.r14) + '</td></tr>';
        }).join('');

        var body = el('sipBody');
        if (!j.ready || !(j.buckets || []).length){
          body.innerHTML = '<div class="empty rv">' + esc(j.message ||
            'No buckets yet. The first one is proposed on the next monthly run.') +
            '</div>';
          reveal(sec); return;
        }

        body.innerHTML = (j.buckets || []).map(function(b){
          var hs = (b.holdings || []).map(function(h){
            var live = h.status === 'held' && h.qty && h.buy_price;
            var val  = live ? h.qty * (h.last_price || h.buy_price) : null;
            var cost = live ? h.qty * h.buy_price : null;
            var pct  = live && cost ? (val / cost - 1) * 100 : null;
            return '<tr>' +
              '<td class="mono-dim">' + (h.rank || '') + '</td>' +
              '<td><a class="sym" href="https://www.tradingview.com/chart/?symbol=NSE:' +
                  encodeURIComponent(h.symbol) + '" target="_blank" rel="noopener">' +
                  esc(h.symbol) + '</a></td>' +
              '<td class="num">' + fmt(h.score, 1) + '</td>' +
              '<td class="num">' + inr(h.allocated) + '</td>' +
              '<td class="num">' + (h.buy_price ? inr(h.buy_price, 2) : '—') + '</td>' +
              '<td class="num">' + (h.last_price ? inr(h.last_price, 2) : '—') + '</td>' +
              '<td class="' + (pct > 0 ? 'pnl-u' : pct < 0 ? 'pnl-d' : 'num') + '">' +
                  (pct === null ? '—' : (pct > 0 ? '+' : '') + fmt(pct, 1) + '%') + '</td>' +
              '<td><span class="badge badge-' + (h.status === 'held' ? 'open' : 'cancelled') +
                  '">' + esc(h.status) + '</span></td>' +
              '<td class="mono-dim" style="font-size:10px">' + esc(h.rationale || '') + '</td>' +
              '</tr>';
          }).join('');

          var xir = b.xirr_pct === null || b.xirr_pct === undefined
            ? '' : ' · XIRR ' + fmt(b.xirr_pct, 1) + '%';
          var pl  = b.pnl_pct === null || b.pnl_pct === undefined
            ? '' : ' · ' + (b.pnl > 0 ? '+' : '') + fmt(b.pnl_pct, 1) + '%';
          return '<div class="rv" style="margin-bottom:26px">' +
            '<div style="display:flex;justify-content:space-between;align-items:baseline;' +
                 'flex-wrap:wrap;gap:8px;margin-bottom:8px">' +
              '<strong style="font-family:var(--mono);font-size:13px;letter-spacing:1px">' +
                esc(b.bucket) + '</strong>' +
              '<span class="mono-dim" style="font-size:11px">' +
                inr(b.monthly_amount) + ' · year ' + b.sip_year + ' · ' +
                b.held + '/' + b.names + ' held' + pl + xir + '</span>' +
            '</div>' +
            '<div class="tw"><table class="t" style="min-width:860px"><thead><tr>' +
              '<th>#</th><th>Symbol</th><th>Score</th><th>Allocated</th><th>Buy</th>' +
              '<th>Last</th><th>P&amp;L</th><th>Status</th><th>Why</th>' +
            '</tr></thead><tbody>' + hs + '</tbody></table></div></div>';
        }).join('');
        reveal(sec);
      }).catch(function(){ /* no /api/sip on a static host — section stays hidden */ });
    }

    // Break-even win rate: stored on v2 rows, derived from R:R on older ones.
    // Red once it exceeds the ~37% this system actually wins, which is the
    // whole reason the gate exists.
    function beWr(a){
      var v = (a.breakeven_wr === null || a.breakeven_wr === undefined)
            ? (a.rr > 0 ? 100 / (1 + a.rr) : null)
            : a.breakeven_wr;
      if (v === null) return '—';
      return '<span style="color:' + (v > 37 ? 'var(--down)' : 'var(--dim)') + '">' +
             fmt(v, 0) + '%</span>';
    }

    // A/B/UNVERIFIED from signals/quality.py. UNVERIFIED means the setup
    // cleared every price gate but Yahoo had no fundamentals for it — worth
    // showing as distinct from a clean pass rather than silently equal to one.
    function gradeCell(a){
      if (!a.grade) return '<span style="color:var(--dim)">—</span>';
      var col = a.grade === 'A' ? 'var(--lime)'
              : a.grade === 'B' ? 'var(--gold)' : 'var(--dim)';
      var txt = a.grade === 'UNVERIFIED' ? 'UNVER' : a.grade;
      return '<span style="color:' + col + ';font-weight:700">' + esc(txt) + '</span>';
    }

    /* ═══════ archive ═══════ */
    function loadArchive(){
      api('/archive?limit=1000').then(function(j){
        if (!j.ok) return;
        var strip = el('archStrip');
        strip.innerHTML = (j.days || []).map(function(d){
          var rTxt = d.total_r === null ? '' :
            '<div class="r ' + (d.total_r >= 0 ? 'up' : 'dn') + '">' +
            (d.total_r > 0 ? '+' : '') + fmt(d.total_r, 1) + 'R</div>';
          return '<div class="arch-day" data-date="' + d.date + '" title="' + d.date + ' · ' +
                 d.wins + 'W / ' + d.losses + 'L / ' + d.open + ' open">' +
                 '<div class="d">' + d.date.slice(5) + '</div>' +
                 '<div class="n">' + d.signals + '</div>' + rTxt + '</div>';
        }).join('');
        // Only ~16 chips fit on a desktop screen and ~5 on a phone, which made
        // a 57-day archive look like one week. Say the span out loud and offer
        // month jumps so the depth is discoverable without horizontal scrolling.
        var days = j.days || [];
        if (days.length){
          var span = el('archSpan');
          if (span) span.textContent = 'ARCHIVE — ' + days.length + ' trading days, ' +
            days[days.length - 1].date + ' → ' + days[0].date + ' · tap a day';
          var months = [];
          days.forEach(function(d){
            var m = d.date.slice(0, 7);
            if (months.indexOf(m) === -1) months.push(m);
          });
          var mEl = el('archMonths');
          if (mEl) {
            mEl.innerHTML = months.map(function(m){
              return '<button type="button" class="fbtn" data-m="' + m + '">' + m + '</button>';
            }).join('');
            mEl.querySelectorAll('button').forEach(function(b){
              b.addEventListener('click', function(){
                var first = strip.querySelector('.arch-day[data-date^="' + b.dataset.m + '"]');
                if (first) strip.scrollLeft = first.offsetLeft - 12;
              });
            });
          }
        }
        reveal(el('archWrap'));
        strip.querySelectorAll('.arch-day').forEach(function(n){
          n.addEventListener('click', function(){ selectDay(n.dataset.date); });
        });
      });
      el('archAll').addEventListener('click', function(){ selectDay(null); });
    }

    function selectDay(date){
      archDate = date;
      document.querySelectorAll('.arch-day').forEach(function(n){
        n.classList.toggle('on', !!date && n.dataset.date === date);
      });
      if (date && history.replaceState) history.replaceState({}, '', '/day/' + date);
      else if (history.replaceState) history.replaceState({}, '', '/');
      loadSignals();
      document.getElementById('alerts').scrollIntoView({ behavior:'smooth', block:'start' });
    }

    /* ═══════ performance ═══════ */
    function wirePerfControls(){
      ['perfTf','perfRange'].forEach(function(id){
        el(id).addEventListener('change', loadStats);
      });
    }

    function loadStats(){
      var qs = [];
      var tf = el('perfTf').value;
      var days = el('perfRange').value;
      if (tf) qs.push('tf=' + encodeURIComponent(tf));
      if (days){
        var d = new Date(Date.now() - Number(days) * 86400000);
        qs.push('from=' + d.toISOString().slice(0, 10));
      }
      api('/stats' + (qs.length ? '?' + qs.join('&') : '')).then(function(j){
        if (!j.ok) return;
        fillTfSelect(el('perfTf'), (j.by_timeframe || []).map(function(b){ return { timeframe: b.key }; }));
        renderStats(j);
      });
    }

    function renderStats(j){
      var h = j.headline, t = j.totals;
      el('perfBasis').textContent = t.closed + ' closed of ' + t.all + ' signals · ' +
        (t.first_date || '—') + ' → ' + (t.last_date || '—');

      // The hero rail is baked at 6 AM. Left alone it would greet you with a
      // win rate from yesterday's snapshot, which is the exact staleness this
      // whole layer exists to kill.
      setKpi('heroRate',  h.win_rate === null ? '—' : h.win_rate + '%');
      setKpi('heroTotal', t.all);

      function cell(v, k, sub, colour){
        return '<div class="perf-cell"><div class="v"' + (colour ? ' style="color:' + colour + '"' : '') + '>' +
               v + '</div><div class="k">' + k + '</div>' +
               (sub ? '<div class="sub">' + sub + '</div>' : '') + '</div>';
      }
      var expColour = h.expectancy_r === null ? '' : (h.expectancy_r >= 0 ? 'var(--up)' : 'var(--down)');
      el('perfGrid').innerHTML =
        cell(h.win_rate === null ? '—' : h.win_rate + '%', 'Win rate', h.wins + 'W / ' + h.losses + 'L', 'var(--lime)') +
        cell(h.expectancy_r === null ? '—' : (h.expectancy_r > 0 ? '+' : '') + fmt(h.expectancy_r, 3) + 'R',
             'Expectancy / trade', 'the number that decides everything', expColour) +
        cell(h.profit_factor === null ? '—' : fmt(h.profit_factor, 2), 'Profit factor', 'gross win ÷ gross loss') +
        cell(h.avg_win_r === null ? '—' : '+' + fmt(h.avg_win_r, 2) + 'R', 'Avg win', '', 'var(--up)') +
        cell(h.avg_loss_r === null ? '—' : fmt(h.avg_loss_r, 2) + 'R', 'Avg loss', '', 'var(--down)') +
        cell(h.max_drawdown_r === null ? '—' : fmt(h.max_drawdown_r, 2) + 'R', 'Max drawdown', 'peak to trough', 'var(--gold)') +
        cell(t.open, 'Open now', 'excluded from every rate above', 'var(--blue)') +
        cell(h.trades, 'Closed trades', 'the sample this rests on');


      function table(title, rows, labelWord){
        if (!rows || !rows.length) return '';
        return '<div class="brk-card"><h4>' + title + '</h4>' +
          rows.slice(0, 8).map(function(b){
            var col = b.total_r === null ? '' : (b.total_r >= 0 ? 'var(--up)' : 'var(--down)');
            return '<div class="brk-row"><span class="kk">' + esc(b.key) + '</span>' +
              '<span class="nn">' + b.trades + ' ' + labelWord + ' · ' + fmt(b.win_rate, 0) + '%</span>' +
              '<span class="rr" style="color:' + col + '">' +
              (b.total_r === null ? '—' : (b.total_r > 0 ? '+' : '') + fmt(b.total_r, 1) + 'R') + '</span></div>';
          }).join('') + '</div>';
      }
      el('perfBrk').innerHTML =
        table('By timeframe', j.by_timeframe, 'tr') +
        table('By signal type', j.by_signal_type, 'tr') +
        table('By month', j.by_month, 'tr') +
        table('Top symbols · 5+ closed trades', j.by_symbol, 'tr');
      reveal(el('perf'));
    }

    // Hand-rolled SVG path — no chart library, nothing to load, works offline.
  })();

  /* ══════════════ MIND GYM ══════════════
     Five drills, one featured per day. Everything is generated from a seed
     derived from the date, so the set is identical on every device all day and
     changes at midnight IST — no content to author, no endpoint to call, and
     it works unchanged on the static host. Scores live in localStorage. */
  (function(){
    var stage = document.getElementById('gymStage');
    if (!stage) return;
    var tabsEl = document.getElementById('gymTabs'),
        scoreEl = document.getElementById('gymScore'),
        LS = 'ds_gym_v1';

    /* ── deterministic randomness ──
       mulberry32 off a date-derived seed. Same day → same questions. */
    function seedFor(dayKey, salt){
      var h = 2166136261;
      var str = dayKey + '|' + salt;
      for (var i = 0; i < str.length; i++){
        h ^= str.charCodeAt(i); h = Math.imul(h, 16777619);
      }
      return h >>> 0;
    }
    function rng(seed){
      return function(){
        seed |= 0; seed = seed + 0x6D2B79F5 | 0;
        var t = Math.imul(seed ^ seed >>> 15, 1 | seed);
        t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
        return ((t ^ t >>> 14) >>> 0) / 4294967296;
      };
    }
    var R = {
      int: function(r, a, b){ return a + Math.floor(r() * (b - a + 1)); },
      pick: function(r, arr){ return arr[Math.floor(r() * arr.length)]; },
      shuffle: function(r, arr){
        var a = arr.slice();
        for (var i = a.length - 1; i > 0; i--){
          var j = Math.floor(r() * (i + 1)); var t = a[i]; a[i] = a[j]; a[j] = t;
        }
        return a;
      }
    };

    function istDayKey(){
      var n = new Date();
      var ist = new Date(n.getTime() + (n.getTimezoneOffset() + 330) * 60000);
      return ist.getFullYear() + '-' + ('0' + (ist.getMonth() + 1)).slice(-2) + '-' + ('0' + ist.getDate()).slice(-2);
    }
    var DAY = istDayKey();

    function store(){ try { return JSON.parse(localStorage.getItem(LS) || '{}') || {}; } catch(e){ return {}; } }
    function save(d){ try { localStorage.setItem(LS, JSON.stringify(d)); } catch(e){} }

    function esc(s){
      return String(s == null ? '' : s)
        .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }
    var fmt = function(v, d){
      return Number(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
    };

    /* ═══ the drills ═══
       Each returns { rounds: [...] }, and each round renders itself and calls
       done(correct) exactly once. */

    // 1 · MENTAL MATH — the arithmetic an FP&A review actually needs at speed.
    function mathRounds(r){
      var out = [];
      for (var i = 0; i < 8; i++){
        var kind = R.pick(r, ['pct', 'margin', 'growth', 'markup', 'share']);
        var q, ans, note;
        if (kind === 'pct'){
          var base = R.int(r, 20, 95) * 100, p = R.pick(r, [5, 8, 12, 15, 18, 25, 40]);
          q = p + '% of ' + fmt(base, 0); ans = base * p / 100;
          note = fmt(base,0) + ' × ' + p + '/100';
        } else if (kind === 'margin'){
          var rev = R.int(r, 40, 90) * 100, cost = Math.round(rev * (R.int(r, 45, 85) / 100));
          q = 'Revenue ' + fmt(rev,0) + ', cost ' + fmt(cost,0) + ' — gross margin %';
          ans = Math.round((rev - cost) / rev * 1000) / 10;
          note = '(' + fmt(rev,0) + ' − ' + fmt(cost,0) + ') ÷ ' + fmt(rev,0);
        } else if (kind === 'growth'){
          var a = R.int(r, 20, 60) * 10, g = R.pick(r, [10, 15, 20, 25, 30, 50]);
          q = fmt(a,0) + ' grows ' + g + '% — new value';
          ans = Math.round(a * (1 + g / 100) * 100) / 100;
          note = fmt(a,0) + ' × ' + (1 + g/100);
        } else if (kind === 'markup'){
          var c2 = R.int(r, 100, 900), m = R.pick(r, [20, 25, 30, 40, 50]);
          q = 'Cost ' + fmt(c2,0) + ', markup ' + m + '% — selling price';
          ans = Math.round(c2 * (1 + m / 100) * 100) / 100;
          note = fmt(c2,0) + ' × ' + (1 + m/100);
        } else {
          var whole = R.int(r, 30, 90) * 100, part = Math.round(whole * (R.int(r, 10, 60) / 100));
          q = fmt(part,0) + ' out of ' + fmt(whole,0) + ' — what %';
          ans = Math.round(part / whole * 1000) / 10;
          note = fmt(part,0) + ' ÷ ' + fmt(whole,0) + ' × 100';
        }
        out.push({ type: 'input', q: q, ans: ans, tol: 0.02, note: note, unit: kind === 'margin' || kind === 'share' ? '%' : '' });
      }
      return out;
    }

    // 2 · R:R AND SIZING — the two numbers that decide whether a trade is takeable.
    function deskRounds(r){
      var syms = ['RELIANCE','TCS','INFY','HDFCBANK','GOLD','CRUDE','NIFTY','SILVER'];
      var out = [];
      for (var i = 0; i < 7; i++){
        var sym = R.pick(r, syms), entry = R.int(r, 100, 3000);
        var riskPct = R.int(r, 2, 6), rewardMult = R.pick(r, [1.5, 2, 2.5, 3]);
        var sl = Math.round(entry * (1 - riskPct / 100) * 100) / 100;
        var risk = entry - sl;
        if (i % 2 === 0){
          var tgt = Math.round((entry + risk * rewardMult) * 100) / 100;
          out.push({ type: 'input',
            q: sym + ' — entry ' + fmt(entry,0) + ', stop ' + fmt(sl,2) + ', target ' + fmt(tgt,2) + '. R:R?',
            ans: rewardMult, tol: 0.06,
            note: 'Reward ' + fmt(tgt - entry,2) + ' ÷ risk ' + fmt(risk,2), unit: 'R' });
        } else {
          var cap = R.pick(r, [100000, 200000, 500000]), riskBudget = R.pick(r, [0.5, 1, 2]);
          var qty = Math.floor(cap * riskBudget / 100 / risk);
          out.push({ type: 'input',
            q: 'Capital ' + fmt(cap,0) + ', risking ' + riskBudget + '% per trade. ' + sym +
               ' entry ' + fmt(entry,0) + ', stop ' + fmt(sl,2) + '. Position size?',
            ans: qty, tol: 0.04,
            note: fmt(cap * riskBudget / 100, 0) + ' risk ÷ ' + fmt(risk,2) + ' per share', unit: 'sh' });
        }
      }
      return out;
    }

    // 3 · ESTIMATION — order of magnitude. Scored on being in the right band,
    // because that is the skill; the exact figure is not the point.
    var FERMI = [
      ['Seconds in a 30-day month', 2592000],
      ['Heartbeats in an average year', 36792000],
      ['Litres of water in an Olympic pool', 2500000],
      ['Words in a 300-page novel', 90000],
      ['Grains of rice in a 1 kg bag', 50000],
      ['People who fly commercially worldwide each day', 12000000],
      ['Weight of a fully loaded 747 in kg', 400000],
      ['Kilometres from Mumbai to Dubai', 1930],
      ['Petrol stations in India', 90000],
      ['Cups of coffee drunk worldwide per day', 2250000000],
      ['Steps in 10 kilometres of walking', 13000],
      ['Bricks in a typical 2-storey house', 30000]
    ];
    function fermiRounds(r){
      return R.shuffle(r, FERMI).slice(0, 6).map(function(f){
        return { type: 'input', q: f[0], ans: f[1], logTol: 0.35,
                 note: 'Order of magnitude is the win — within ~2× counts.', unit: '' };
      });
    }

    // 4 · NUMBER RECALL — working memory, lengthening each round.
    function recallRounds(r){
      var out = [];
      for (var len = 4; len <= 9; len++){
        var digits = '';
        for (var i = 0; i < len; i++) digits += R.int(r, 0, 9);
        out.push({ type: 'recall', digits: digits, q: 'Memorise ' + len + ' digits' });
      }
      return out;
    }

    // 5 · MARKET LOGIC — multiple choice on things that cost money to get wrong.
    var LOGIC = [
      ['A stock falls 50%. What gain gets you back to breakeven?', ['100%','50%','75%','200%'], 0,
       'Down 50% halves the base. Doubling the half is +100%. Losses need bigger wins than they look.'],
      ['Two funds: A returns +30%, −20%. B returns +5%, +5%. Which ends higher?', ['B','A','Same','Need more info'], 0,
       'A: 1.30 × 0.80 = 1.04. B: 1.05 × 1.05 = 1.1025. Volatility drags compounding.'],
      ['Win rate 30%, average win 3R, average loss 1R. Expectancy?', ['+0.2R','−0.1R','+0.9R','0R'], 0,
       '0.30 × 3 − 0.70 × 1 = +0.2R. A 30% win rate can print money.'],
      ['₹1L at 12% for 30 years vs ₹3L at 8% for 30 years. Which is larger?', ['₹1L at 12%','₹3L at 8%','Equal','Depends on tax'], 0,
       '₹1L × 1.12³⁰ ≈ ₹30L. ₹3L × 1.08³⁰ ≈ ₹30.2L — near dead heat. Rate rivals principal over long horizons.'],
      ['Which costs more over 20 years on a ₹50L corpus?', ['1.5% expense ratio','₹50,000 one-time fee','A 10% single-year drawdown','A 2-year late start'], 0,
       'A 1.5% drag compounds to roughly a quarter of the corpus. Recurring costs beat one-off shocks.'],
      ['Position risks 1% of capital. How many consecutive losses to halve the account?', ['~69','~50','~100','~35'], 0,
       'ln(0.5)/ln(0.99) ≈ 69. Small fixed risk is remarkably hard to kill.'],
      ['EBITDA rises, operating cash flow falls. Most likely cause?', ['Working capital blew out','Sales fell','Depreciation rose','Interest rose'], 0,
       'EBITDA ignores working capital. Receivables and inventory absorb the cash.'],
      ['A 1.5 R:R setup needs what win rate to break even?', ['40%','50%','33%','60%'], 0,
       '1/(1+1.5) = 40%. Below that the edge is negative no matter how good it feels.'],
      ['₹10,000/month SIP at 12% for 10 years ends near', ['₹23L','₹12L','₹18L','₹35L'], 0,
       'Roughly ₹23.2L on ₹12L invested. The second decade is where it gets interesting.'],
      ['Rupee falls from 83 to 95 per dollar. A US-earning exporter sees', ['Higher rupee revenue','Lower rupee revenue','No change','Lower margins'], 0,
       'Each dollar converts to more rupees. Importers take the opposite hit.']
    ];
    function logicRounds(r){
      return R.shuffle(r, LOGIC).slice(0, 6).map(function(l){
        var opts = l[1].map(function(t, i){ return { t: t, ok: i === l[2] }; });
        return { type: 'choice', q: l[0], opts: R.shuffle(r, opts), note: l[3] };
      });
    }

    var GAMES = [
      { id:'math',    name:'Mental Math',   icon:'🔢', build: mathRounds,   blurb:'Percentages, margins, growth. No calculator.' },
      { id:'desk',    name:'Desk Math',     icon:'🎯', build: deskRounds,   blurb:'R:R and position sizing — the two that decide the trade.' },
      { id:'logic',   name:'Market Logic',  icon:'🧠', build: logicRounds,  blurb:'Things that cost money to get wrong.' },
      { id:'fermi',   name:'Estimation',    icon:'📐', build: fermiRounds,  blurb:'Order of magnitude beats precision.' },
      { id:'recall',  name:'Number Recall', icon:'🧩', build: recallRounds, blurb:'Working memory, four digits up to nine.' }
    ];

    // Featured game rotates by day, so the default changes every morning.
    var dayIndex = Math.abs(seedFor(DAY, 'featured')) % GAMES.length;
    var current = GAMES[dayIndex], rounds = [], idx = 0, correct = 0, startedAt = 0;

    function tabs(){
      tabsEl.innerHTML = GAMES.map(function(g, i){
        return '<button class="gym-tab' + (g.id === current.id ? ' on' : '') + '" data-g="' + g.id + '">' +
               (i === dayIndex ? '<span class="tdot" title="Today\'s pick"></span>' : '') +
               g.icon + ' ' + esc(g.name) + '</button>';
      }).join('');
      Array.prototype.forEach.call(tabsEl.querySelectorAll('.gym-tab'), function(b){
        b.addEventListener('click', function(){
          var g = GAMES.filter(function(x){ return x.id === b.dataset.g; })[0];
          if (g) { current = g; start(); }
        });
      });
    }

    function start(){
      rounds = current.build(rng(seedFor(DAY, current.id)));
      idx = 0; correct = 0; startedAt = Date.now();
      tabs(); render();
    }

    function meta(){
      return '<div class="gym-meta"><span>' + esc(current.icon + ' ' + current.name.toUpperCase()) +
             ' · ' + esc(current.blurb) + '</span>' +
             '<span class="prog">' + Math.min(idx + 1, rounds.length) + ' / ' + rounds.length + '</span></div>';
    }

    function finish(){
      var secs = Math.round((Date.now() - startedAt) / 1000);
      var pct = Math.round(correct / rounds.length * 100);
      var d = store(); d[DAY] = d[DAY] || {};
      var prev = d[DAY][current.id];
      // Keep the day's best, not the latest — replaying should not punish you.
      if (!prev || correct > prev.correct) d[DAY][current.id] = { correct: correct, total: rounds.length, secs: secs };
      save(d);

      var verdict = pct === 100 ? 'Clean sweep.' : pct >= 70 ? 'Solid.' : pct >= 40 ? 'Middling.' : 'Rough one.';
      stage.innerHTML = meta() +
        '<div class="gym-q">' + correct + ' / ' + rounds.length + ' <span style="color:var(--muted);font-size:.55em">' + verdict + '</span></div>' +
        '<div class="gym-sub">' + secs + ' seconds · ' + pct + '% · ' +
          (prev && correct <= prev.correct ? 'today\'s best stands at ' + prev.correct : 'new best for today') + '</div>' +
        '<div class="gym-input"><button class="gym-btn" id="gymAgain">Run it again</button>' +
        '<button class="gym-btn ghost" id="gymNext">Next drill →</button></div>';
      document.getElementById('gymAgain').addEventListener('click', start);
      document.getElementById('gymNext').addEventListener('click', function(){
        current = GAMES[(GAMES.indexOf(current) + 1) % GAMES.length]; start();
      });
      paintScore();
    }

    function next(ok){
      if (ok) correct++;
      idx++;
      setTimeout(function(){ idx >= rounds.length ? finish() : render(); }, ok ? 550 : 1900);
    }

    function feedback(ok, msg){
      var fb = document.createElement('div');
      fb.className = 'gym-fb ' + (ok ? 'good' : 'bad');
      fb.innerHTML = msg;
      stage.appendChild(fb);
    }

    function render(){
      var q = rounds[idx];
      if (!q) return finish();

      if (q.type === 'choice'){
        stage.innerHTML = meta() + '<div class="gym-q">' + esc(q.q) + '</div>' +
          '<div class="gym-opts">' + q.opts.map(function(o, i){
            return '<button class="gym-opt" data-i="' + i + '">' + esc(o.t) + '</button>';
          }).join('') + '</div>';
        Array.prototype.forEach.call(stage.querySelectorAll('.gym-opt'), function(b){
          b.addEventListener('click', function(){
            var o = q.opts[+b.dataset.i], ok = o.ok;
            Array.prototype.forEach.call(stage.querySelectorAll('.gym-opt'), function(x, j){
              x.disabled = true;
              if (q.opts[j].ok) x.classList.add('right');
            });
            if (!ok) b.classList.add('wrong');
            feedback(ok, (ok ? '<b>Right.</b> ' : '<b>No.</b> ') + esc(q.note));
            next(ok);
          });
        });
        return;
      }

      if (q.type === 'recall'){
        stage.innerHTML = meta() + '<div class="gym-q">' + esc(q.q) + '</div>' +
          '<div class="gym-sub">It disappears in ' + (1.2 + q.digits.length * 0.35).toFixed(1) + ' seconds.</div>' +
          '<div class="gym-prompt" id="gymDigits">' + esc(q.digits) + '</div>';
        setTimeout(function(){
          stage.innerHTML = meta() + '<div class="gym-q">Type it back</div>' +
            '<div class="gym-input"><input id="gymIn" inputmode="numeric" autocomplete="off" placeholder="' +
            q.digits.length + ' digits">' +
            '<button class="gym-btn" id="gymGo">Check</button></div>';
          wireInput(function(val){
            var ok = val.replace(/\D/g, '') === q.digits;
            feedback(ok, ok ? '<b>Correct.</b> ' + esc(q.digits)
                            : '<b>It was ' + esc(q.digits) + '.</b> You typed ' + esc(val || '—') + '.');
            next(ok);
          });
        }, (1.2 + q.digits.length * 0.35) * 1000);
        return;
      }

      // numeric input
      stage.innerHTML = meta() + '<div class="gym-q">' + esc(q.q) + '</div>' +
        '<div class="gym-input"><input id="gymIn" inputmode="decimal" autocomplete="off" placeholder="Your answer' +
        (q.unit ? ' (' + esc(q.unit) + ')' : '') + '">' +
        '<button class="gym-btn" id="gymGo">Check</button></div>';
      wireInput(function(val){
        var got = parseFloat(String(val).replace(/[, ]/g, ''));
        var ok;
        if (!isFinite(got)) ok = false;
        else if (q.logTol){
          // Estimation is judged on log distance — within about 2× is a pass.
          ok = got > 0 && Math.abs(Math.log10(got) - Math.log10(q.ans)) <= q.logTol;
        } else {
          ok = Math.abs(got - q.ans) <= Math.max(Math.abs(q.ans) * q.tol, 0.01);
        }
        var shown = q.ans >= 1000 ? fmt(q.ans, 0) : fmt(q.ans, Math.abs(q.ans % 1) > 0 ? 2 : 0);
        feedback(ok, (ok ? '<b>Right.</b> ' : '<b>Answer: ' + shown + (q.unit ? ' ' + esc(q.unit) : '') + '.</b> ') +
                     (q.note ? esc(q.note) : ''));
        next(ok);
      });
    }

    function wireInput(check){
      var inp = document.getElementById('gymIn'), go = document.getElementById('gymGo');
      if (!inp || !go) return;
      inp.focus();
      var fired = false;
      var submit = function(){
        if (fired) return; fired = true;
        go.disabled = true; inp.disabled = true;
        check(inp.value.trim());
      };
      go.addEventListener('click', submit);
      inp.addEventListener('keydown', function(e){ if (e.key === 'Enter') submit(); });
    }

    function paintScore(){
      var d = store(), today = d[DAY] || {};
      var done = Object.keys(today).length;
      var got = 0, tot = 0;
      for (var k in today){ got += today[k].correct; tot += today[k].total; }

      // Streak: consecutive days ending today with at least one drill played.
      var streak = 0;
      for (var i = 0; i < 400; i++){
        var t = new Date(Date.now() - i * 86400000);
        var ist = new Date(t.getTime() + (t.getTimezoneOffset() + 330) * 60000);
        var key = ist.getFullYear() + '-' + ('0'+(ist.getMonth()+1)).slice(-2) + '-' + ('0'+ist.getDate()).slice(-2);
        if (d[key] && Object.keys(d[key]).length) streak++;
        else if (i > 0) break;
      }

      scoreEl.innerHTML =
        '<div><div class="v" style="color:var(--lime)">' + done + '/' + GAMES.length + '</div><div class="k">Drills today</div></div>' +
        '<div><div class="v">' + (tot ? Math.round(got / tot * 100) : 0) + '%</div><div class="k">Accuracy today</div></div>' +
        '<div><div class="v" style="color:var(--gold)">' + streak + '</div><div class="k">Day streak</div></div>' +
        '<div><div class="v" style="color:var(--blue)">' + Object.keys(d).length + '</div><div class="k">Days trained</div></div>';
    }

    start(); paintScore();
  })();

  /* ══════════════ LIVE CLOCK · MARKETS · NEWS ══════════════
     The daily shell freezes at 06:00 IST. These three keep the page honest
     without a rebuild: the clock ticks, prices refresh every 5 minutes, the
     wires every 3 hours. All of it degrades to the baked-in values if /api
     is not there (GitHub Pages), so nothing here can leave a blank section. */
  (function(){
    var API = '/api';

    /* ── IST clock: real time, not build time ── */
    var clock = document.getElementById('istClock');
    if (clock){
      var tick = function(){
        var n = new Date();
        // IST is UTC+5:30 with no DST, so a fixed offset is exact.
        var ist = new Date(n.getTime() + (n.getTimezoneOffset() + 330) * 60000);
        var hh = ('0' + ist.getHours()).slice(-2),
            mm = ('0' + ist.getMinutes()).slice(-2),
            ss = ('0' + ist.getSeconds()).slice(-2);
        clock.innerHTML = '<i></i>' + hh + ':' + mm + ':' + ss + ' IST';
      };
      tick(); setInterval(tick, 1000);
    }

    function get(path){
      return fetch(API + path, { cache: 'no-store' })
        .then(function(r){ return r.ok ? r.json() : null; })
        .catch(function(){ return null; });
    }
    function esc(s){
      return String(s == null ? '' : s)
        .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }

    /* ── markets: every 5 minutes ── */
    function paintMarkets(j){
      if (!j || !j.ok || !j.markets || !j.markets.length) return;
      var live = j.markets.filter(function(m){ return m.price_raw !== null; });
      if (!live.length) return;

      var cell = function(m){
        var arrow = m.up ? '▲' : '▼', cls = m.up ? 'up' : 'dn';
        return { arrow: arrow, cls: cls,
                 chg: (m.change_pct > 0 ? '+' : '') + m.change_pct.toFixed(2) + '%' };
      };

      var rail = document.getElementById('tickRail');
      if (rail){
        var html = '';
        // duplicated once — the marquee translates -50%, so the strip has to
        // contain exactly two copies to loop seamlessly
        for (var d = 0; d < 2; d++){
          j.markets.forEach(function(m){
            var c = cell(m);
            html += '<div class="ti"><span class="n">' + esc(m.name) + '</span>' +
                    '<span class="p">' + esc(m.price) + '</span>' +
                    '<span class="c ' + c.cls + '">' + c.arrow + ' ' + c.chg + '</span></div>';
          });
        }
        rail.innerHTML = html;
      }

      var grid = document.getElementById('mktGrid');
      if (grid){
        grid.innerHTML = j.markets.map(function(m, i){
          var c = cell(m);
          return '<div class="mkt ' + (m.up ? 'u' : 'd') + ' rv in" style="--d:' + (i * 0.04) + 's">' +
                   '<div class="n">' + esc(m.name) + '</div>' +
                   '<div class="p">' + esc(m.price) + '</div>' +
                   '<div class="c ' + c.cls + '">' + c.arrow + ' ' + c.chg + '</div>' +
                 '</div>';
        }).join('');
      }

      var desc = document.getElementById('mktDesc');
      if (desc){
        var t = new Date(j.fetched_at);
        var ist = new Date(t.getTime() + (t.getTimezoneOffset() + 330) * 60000);
        desc.innerHTML = j.advancing + ' of ' + j.total + ' tracked instruments are green. ' +
          'Live quotes — last read ' + ('0' + ist.getHours()).slice(-2) + ':' +
          ('0' + ist.getMinutes()).slice(-2) + ' IST, refreshing every 5 minutes.';
      }

      var adv = document.querySelector('.statrail .stat .v[data-total]');
      if (adv){ adv.textContent = j.advancing + '/' + j.total; }
    }

    function loadMarkets(){ get('/markets').then(paintMarkets); }
    loadMarkets();
    setInterval(loadMarkets, 5 * 60 * 1000);

    /* ── news: every 3 hours ── */
    function paintNews(j){
      if (!j || !j.ok || !j.news || j.news.length < 3) return;
      var sec = document.getElementById('world');
      if (!sec) return;
      var lead = j.news[0], rest = j.news.slice(1, 13);

      var ago = function(iso){
        if (!iso) return '';
        var mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
        if (mins < 1)  return 'just now';
        if (mins < 60) return mins + 'm ago';
        var h = Math.round(mins / 60);
        return h < 24 ? h + 'h ago' : Math.round(h / 24) + 'd ago';
      };
      // Reuses .ncard from the stylesheet rather than inventing classes, so
      // live cards are pixel-identical to the ones the daily build renders.
      var card = function(n, i){
        var link = n.link
          ? '<a href="' + esc(n.link) + '" target="_blank" rel="noopener">' + esc(n.title) + '</a>'
          : esc(n.title);
        return '<div class="ncard rv in" style="--d:' + (i * 0.04) + 's">' +
                 '<span class="s">' + esc(n.source) + '</span>' +
                 '<h3>' + link + '</h3>' +
                 (n.summary ? '<p>' + esc(n.summary.slice(0, 150)) + '</p>' : '') +
                 '<div class="ts">' + esc(ago(n.published)) + '</div>' +
               '</div>';
      };

      var host = document.getElementById('newsLive');
      if (!host){
        host = document.createElement('div');
        host.id = 'newsLive';
        // Replace whatever the daily build rendered — the lead block and the
        // grid both — with one live grid.
        var olds = sec.querySelectorAll('.news-grid, .two, .empty');
        if (olds.length){ olds[0].replaceWith(host); for (var k = 1; k < olds.length; k++) olds[k].remove(); }
        else sec.appendChild(host);
      }
      host.className = 'news-grid';
      host.innerHTML = [lead].concat(rest).map(card).join('');

      var d = sec.querySelector('.sdesc');
      if (d) d.textContent = 'Wires only, deduplicated. ' + j.count + ' stories from ' +
                             j.sources_ok + ' feeds, refreshed every 3 hours.';
    }

    function loadNews(){ get('/news?limit=14').then(paintNews); }
    loadNews();
    setInterval(loadNews, 3 * 60 * 60 * 1000);
  })();

  /* No whole-page reload. The clock ticks, markets and news refresh on their
     own timers, and the ledger sections pull on demand — reloading every five
     minutes would only throw away scroll position and any game in progress. */
})();
</script>
</body>
</html>"""

# ─────────────────────────────────────────────────────────────
# FLASK ROUTES
# ─────────────────────────────────────────────────────────────

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
        prod           = get_productivity_tip()
        quote          = get_entrepreneur_quote()
        lesson         = get_world_lesson()
        case           = get_case_study()
        lichess_games   = fetch_lichess_games()
        lichess_summary = get_lichess_summary(lichess_games)
        lichess_puzzle  = fetch_lichess_puzzle()
        alerts         = fetch_alert_log()

        return render_template_string(TEMPLATE,
            date_str=now.strftime("%A, %B %d %Y"),
            updated_at=now.strftime("%H:%M"),
            markets=markets, news=news, fpna=fpna, cfo=cfo,
            chess=chess, wisdom=wisdom, book=book, way=way_ctx, review=review_ctx,
            top5=top5, tracker=tracker, money_hack=money,
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
            date_str=now.strftime("%A, %B %d %Y"),
            updated_at=f"{now.strftime('%H:%M')} (partial)",
            markets=[], news=[], fpna={"title":"Loading","body":"","index":0,"total":1},
            cfo={"title":"Loading","body":"","index":0,"total":1},
            chess={"title":"Loading","body":"","index":0,"total":1},
            wisdom={"title":"Loading","body":"","index":0,"total":1},
            book={"book":"Loading","author":"","chapter":"Loading","lesson":"","key_quote":"","action":"","index":0,"total":1},
            way=_way_placeholder(), review=_review_placeholder(),
            top5=[], tracker=[], money_hack={"title":"Loading","body":""},
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
