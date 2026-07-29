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
        result = []
        for r in rows:
            r = dict(r)
            # badge colour logic
            s = (r.get("status") or "").upper()
            lc = (r.get("lifecycle_status") or "").upper()
            if s == "TARGET_HIT" or lc == "TARGET_HIT":
                badge = "win"
            elif s in ("SL_HIT", "STOPPED") or lc in ("SL_HIT", "STOPPED"):
                badge = "loss"
            elif s == "OPEN":
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

def fetch_lichess_games() -> list[dict]:
    """Fetch last 30 days of Lichess games for AKK_010."""
    from datetime import datetime, timezone, timedelta
    try:
        now       = datetime.now(timezone.utc)
        since     = now - timedelta(days=30)
        token     = os.environ.get("LICHESS_TOKEN", "")
        headers   = {"Accept": "application/x-ndjson"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        params = {
            "since":   int(since.timestamp() * 1000),
            "until":   int(now.timestamp() * 1000),
            "max":     50,
            "moves":   "true",
            "opening": "true",
            "evals":   "false",
            "clocks":  "false",
        }
        r = requests.get(
            f"https://lichess.org/api/games/user/{LICHESS_USER}",
            params=params, headers=headers, timeout=20
        )
        if r.status_code != 200:
            return []
        games = []
        for line in r.text.strip().split("\n"):
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                g = json.loads(line)
            except Exception:
                continue

            # Who am I?
            white = g.get("players", {}).get("white", {})
            black = g.get("players", {}).get("black", {})
            i_am_white = white.get("user", {}).get("id", "").lower() == LICHESS_USER.lower()
            my_side    = "White" if i_am_white else "Black"
            opponent   = black.get("user", {}).get("name", "?") if i_am_white else white.get("user", {}).get("name", "?")
            opp_rating = black.get("rating", "?") if i_am_white else white.get("rating", "?")
            my_rating  = white.get("rating", "?") if i_am_white else black.get("rating", "?")

            # Result
            winner = g.get("winner", "")
            if not winner:
                result = "Draw"
                result_icon = "½"
            elif (winner == "white" and i_am_white) or (winner == "black" and not i_am_white):
                result = "Win"
                result_icon = "✅"
            else:
                result = "Loss"
                result_icon = "❌"

            # Opening
            opening = g.get("opening", {})
            opening_name = opening.get("name", "Unknown Opening") if opening else "Unknown Opening"
            opening_eco  = opening.get("eco", "")  if opening else ""

            # Moves count
            moves_str = g.get("moves", "")
            num_moves = len(moves_str.split()) // 2 if moves_str else 0

            # Time control
            clock = g.get("clock", {})
            tc    = g.get("speed", g.get("perf", "?"))

            # Key moves — last 3 moves of the game (often the decisive moment)
            all_moves = moves_str.split() if moves_str else []
            key_moves = ""
            if len(all_moves) >= 6:
                last_pairs = []
                for i in range(max(0, len(all_moves)-6), len(all_moves), 2):
                    move_num = (i // 2) + 1
                    w = all_moves[i] if i < len(all_moves) else ""
                    b = all_moves[i+1] if i+1 < len(all_moves) else ""
                    last_pairs.append(f"{move_num}.{w} {b}")
                key_moves = " ".join(last_pairs).strip()

            # Termination
            status = g.get("status", "")
            termination_map = {
                "mate": "Checkmate",
                "resign": "Resignation",
                "timeout": "Time Out",
                "draw": "Draw",
                "stalemate": "Stalemate",
                "outoftime": "Time Out",
                "cheat": "Cheat detected",
                "noStart": "No start",
                "unknownFinish": "Aborted",
                "variantEnd": "Variant end",
            }
            termination = termination_map.get(status, status.title())

            game_url = f"https://lichess.org/{g.get('id', '')}"

            # Groq analysis if key available
            analysis = ""
            if GROQ_KEY and opening_name and result:
                prompt = (
                    f"Chess game analysis. I played {my_side} ({my_rating}) vs {opponent} ({opp_rating}). "
                    f"Opening: {opening_eco} {opening_name}. Result: {result} by {termination} in {num_moves} moves. "
                    f"Last moves: {key_moves}. "
                    f"Give 1 specific learning point in 20 words max. Numbers-first, brutally honest."
                )
                analysis = groq_complete(prompt, max_tokens=50)

            games.append({
                "id":           g.get("id", ""),
                "url":          game_url,
                "result":       result,
                "result_icon":  result_icon,
                "my_side":      my_side,
                "my_rating":    my_rating,
                "opponent":     opponent,
                "opp_rating":   opp_rating,
                "opening":      opening_name,
                "eco":          opening_eco,
                "moves":        num_moves,
                "termination":  termination,
                "tc":           tc.title() if tc else "?",
                "key_moves":    key_moves,
                "analysis":     analysis,
            })
        return games
    except Exception as e:
        log.warning(f"Lichess fetch error: {e}")
        return []

def get_lichess_summary(games: list[dict]) -> dict:
    """Win/loss/draw summary + best and worst game."""
    if not games:
        return {}
    wins   = sum(1 for g in games if g["result"] == "Win")
    losses = sum(1 for g in games if g["result"] == "Loss")
    draws  = sum(1 for g in games if g["result"] == "Draw")
    total  = len(games)
    openings = {}
    for g in games:
        op = g["opening"]
        openings[op] = openings.get(op, {"w":0,"l":0,"d":0})
        if g["result"] == "Win":    openings[op]["w"] += 1
        elif g["result"] == "Loss": openings[op]["l"] += 1
        else:                       openings[op]["d"] += 1
    # Most played opening
    top_opening = max(openings, key=lambda k: openings[k]["w"]+openings[k]["l"]+openings[k]["d"]) if openings else ""
    return {
        "total": total, "wins": wins, "losses": losses, "draws": draws,
        "score": f"{wins}/{total}",
        "top_opening": top_opening,
        "openings": openings,
    }

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

def get_book_lesson() -> dict:
    idx = (date.today().toordinal() + 23) % len(BOOK_LESSONS)
    book, author, chapter, lesson, key_quote, action = BOOK_LESSONS[idx]
    return {
        "book": book, "author": author, "chapter": chapter,
        "lesson": lesson, "key_quote": key_quote, "action": action,
        "index": idx + 1, "total": len(BOOK_LESSONS),
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
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>THE DAILY SIGNAL — {{ date_str }}</title>
<style>
:root{--bg:#08090b;--surface:#0f1014;--border:#1c1e26;--accent:#f97316;--gold:#e8c547;
  --red:#ef4444;--green:#22c55e;--blue:#3b82f6;--purple:#9b7fe8;--text:#e4e4e4;--muted:#666}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Georgia',serif;font-size:14px;line-height:1.65}
a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}
.up{color:var(--green)} .dn{color:var(--red)}

.sticky-header{position:sticky;top:0;z-index:200;background:#060709}
.masthead{border-bottom:1px solid var(--border);padding:8px 20px;text-align:center;background:#060709;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.paper-name{font-size:22px;font-weight:900;letter-spacing:4px;color:var(--accent);line-height:1;font-family:sans-serif;white-space:nowrap}
.paper-sub{display:none}
.paper-meta{display:flex;gap:16px;font-size:10px;color:var(--muted);justify-content:flex-end;flex:1;
  border-top:1px solid var(--border);padding-top:8px;font-family:monospace}

.nav{display:flex;overflow-x:auto;background:#0a0b0e;border-bottom:1px solid var(--border)}
.nav a{padding:9px 13px;color:var(--muted);font-size:10px;letter-spacing:1px;text-transform:uppercase;
  white-space:nowrap;border-right:1px solid var(--border)}
.nav a:hover{color:var(--accent);background:var(--surface);text-decoration:none}

.ticker{display:flex;overflow-x:auto;background:#060709;border-bottom:1px solid var(--border);padding:7px 0}
.t-item{display:flex;gap:6px;align-items:center;padding:0 14px;border-right:1px solid #111;
  white-space:nowrap;font-size:11px;font-family:monospace}
.t-name{color:var(--muted);font-size:9px}

.main{max-width:1200px;margin:0 auto;padding:0 14px}
.section{margin:24px 0;padding-top:16px;border-top:2px solid var(--border)}
.label{font-size:9px;letter-spacing:3px;text-transform:uppercase;color:var(--accent);
  font-family:sans-serif;margin-bottom:12px;display:flex;align-items:center;gap:10px;font-weight:700}
.label::after{content:'';flex:1;height:1px;background:var(--border)}

/* WEATHER */
.weather-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
@media(max-width:700px){.weather-grid{grid-template-columns:1fr}}
.wx-card{background:var(--surface);border:1px solid var(--border);padding:16px;position:relative}
.wx-city{font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:2px;color:var(--text);font-family:sans-serif}
.wx-country{font-size:9px;color:var(--muted);letter-spacing:1px}
.wx-emoji{font-size:36px;margin:8px 0 4px}
.wx-temp{font-size:28px;font-weight:700;font-family:monospace;color:var(--accent)}
.wx-cond{font-size:11px;color:var(--muted);margin-bottom:8px}
.wx-meta{display:flex;gap:12px;font-size:10px;font-family:monospace;color:#555;flex-wrap:wrap}
.wx-rain{display:inline-block;padding:3px 8px;font-size:10px;font-weight:700;font-family:sans-serif;
  margin-top:8px;border-radius:2px}
.wx-rain.hi{background:#ef444422;color:var(--red);border:1px solid #ef444433}
.wx-rain.lo{background:#22c55e22;color:var(--green);border:1px solid #22c55e33}
.wx-range{font-size:10px;color:var(--muted);margin-top:4px;font-family:monospace}

/* NEWS */
.news-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px}
@media(max-width:900px){.news-grid{grid-template-columns:1fr 1fr}}
@media(max-width:580px){.news-grid{grid-template-columns:1fr}}
.ncard{border:1px solid var(--border);padding:14px;background:var(--surface)}
.ncard .src{font-size:9px;letter-spacing:2px;text-transform:uppercase;color:var(--accent);margin-bottom:5px;font-family:sans-serif}
.ncard h3{font-size:13px;font-weight:700;line-height:1.4;margin-bottom:7px}
.ncard p{font-size:11px;color:var(--muted);line-height:1.5}
.ncard .ts{font-size:9px;color:#333;margin-top:7px;font-family:monospace}
.lead{grid-column:1/-1;display:grid;grid-template-columns:3fr 2fr;gap:20px;border:1px solid #2a2214;background:#0b0a07}
@media(max-width:640px){.lead{grid-template-columns:1fr}}
.lead-main{padding:16px}
.lead-side{padding:16px;border-left:1px solid var(--border)}
.lead h2{font-size:20px;line-height:1.3;margin-bottom:10px}

/* LICHESS */
.chess-game-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px;margin-top:12px}
.game-card{background:var(--surface);border:1px solid var(--border);padding:14px;position:relative}
.game-card.win{border-left:3px solid var(--green)}
.game-card.loss{border-left:3px solid var(--red)}
.game-card.draw{border-left:3px solid var(--muted)}
.game-result{font-size:18px;font-weight:900;font-family:monospace}
.game-result.win{color:var(--green)} .game-result.loss{color:var(--red)} .game-result.draw{color:var(--muted)}
.game-meta{font-size:10px;color:var(--muted);font-family:monospace;margin:4px 0}
.game-opening{font-size:11px;color:var(--gold);margin:5px 0;font-weight:700}
.game-moves{font-size:10px;color:#555;font-family:monospace;margin-top:4px;line-height:1.5}
.game-analysis{font-size:11px;color:#aaa;font-style:italic;margin-top:8px;padding-top:8px;border-top:1px solid var(--border)}
.lichess-summary{display:flex;gap:20px;padding:14px;background:var(--surface);border:1px solid var(--border);margin-bottom:14px;flex-wrap:wrap;align-items:center}
.ls-stat{text-align:center}
.ls-num{font-size:28px;font-weight:900;font-family:monospace}
.ls-label{font-size:9px;letter-spacing:2px;text-transform:uppercase;color:var(--muted);font-family:sans-serif}

/* WISDOM / CHESS / CFO */
.wisdom-card{background:var(--surface);border:1px solid var(--border);border-left:4px solid var(--green);padding:20px}
.wisdom-title{font-size:13px;font-weight:700;color:var(--green);margin-bottom:10px;letter-spacing:1px;font-family:sans-serif;text-transform:uppercase}
.wisdom-body{font-size:13px;line-height:1.8;color:#ccc}
.chess-card{background:var(--surface);border:1px solid var(--border);border-left:4px solid var(--gold);padding:20px}
.chess-title{font-size:13px;font-weight:700;color:var(--gold);margin-bottom:10px;letter-spacing:1px;font-family:sans-serif;text-transform:uppercase}
.chess-body{font-size:13px;line-height:1.8;color:#ccc}
.cfo-card{background:var(--surface);border:1px solid var(--border);border-left:4px solid var(--accent);padding:20px}
.cfo-title{font-size:13px;font-weight:700;color:var(--accent);margin-bottom:10px;letter-spacing:1px;font-family:sans-serif;text-transform:uppercase}
.cfo-body{font-size:13px;line-height:1.8;color:#ccc}
.alert-filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
.af-btn{padding:4px 12px;font-size:9px;letter-spacing:1px;text-transform:uppercase;background:transparent;border:1px solid var(--border);color:var(--muted);cursor:pointer;font-family:monospace}
.af-btn.active{border-color:var(--accent);color:var(--accent)}
.alert-tbl{width:100%;border-collapse:collapse;font-size:11px}
.alert-tbl th{text-align:left;color:var(--muted);font-size:9px;letter-spacing:1px;text-transform:uppercase;padding:6px 8px;border-bottom:1px solid var(--border)}
.alert-tbl td{padding:7px 8px;border-bottom:1px solid #1a1c1f;vertical-align:middle}
.alert-tbl tr:hover td{background:var(--surface)}
.badge{display:inline-block;padding:2px 7px;font-size:9px;font-weight:700;letter-spacing:1px;text-transform:uppercase;border-radius:2px}
.badge-win{background:#064e3b;color:#34d399}
.badge-loss{background:#450a0a;color:#f87171}
.badge-open{background:#1e3a5f;color:#60a5fa}
.badge-cancelled{background:#1c1c1c;color:#555}
.alert-sym{font-weight:700;font-family:monospace;color:#e2e8f0}
.alert-buy{color:var(--green)}
.alert-sell{color:var(--red)}
.alert-stats{display:flex;gap:20px;margin-bottom:14px;padding:12px;background:var(--surface);border:1px solid var(--border)}
.ast{text-align:center}
.ast-num{font-size:18px;font-weight:700;font-family:monospace}
.ast-label{font-size:9px;color:var(--muted);letter-spacing:1px;text-transform:uppercase}
.book-card{background:var(--surface);border:1px solid var(--border);border-left:4px solid #c084fc;padding:20px}
.book-meta{font-size:10px;letter-spacing:1.5px;text-transform:uppercase;color:#c084fc;font-family:sans-serif;margin-bottom:4px}
.book-title{font-size:15px;font-weight:700;color:#e9d5ff;margin-bottom:12px;line-height:1.4}
.book-body{font-size:13px;line-height:1.9;color:#ccc;margin-bottom:16px}
.book-quote{font-size:13px;font-style:italic;color:#c084fc;border-left:3px solid #c084fc;padding-left:12px;margin-bottom:16px}
.book-action{background:#1e1333;border:1px solid #6d28d9;border-radius:6px;padding:14px;font-size:12px;color:#ddd;line-height:1.7}
.book-action strong{color:#c084fc;display:block;margin-bottom:4px;font-size:11px;letter-spacing:1px;text-transform:uppercase}

/* MARKETS */
.mkt-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px}
.mkt-card{background:var(--surface);border:1px solid var(--border);padding:12px}
.mkt-card.u{border-left:2px solid var(--green)} .mkt-card.d{border-left:2px solid var(--red)}
.mkt-name{font-size:10px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;font-family:sans-serif}
.mkt-price{font-size:18px;font-weight:700;font-family:monospace;margin:3px 0}
.mkt-chg{font-size:13px;font-weight:700;font-family:monospace}

/* QUOTE */
.quote-card{background:var(--surface);border:1px solid var(--border);border-left:4px solid var(--gold);
  padding:24px;text-align:center}
.quote-text{font-size:18px;font-style:italic;line-height:1.6;color:var(--text);margin-bottom:14px;max-width:800px;margin-left:auto;margin-right:auto}
.quote-name{font-size:11px;letter-spacing:3px;text-transform:uppercase;color:var(--gold);font-family:sans-serif;font-weight:700}
.quote-num{font-size:9px;color:var(--muted);margin-top:4px;font-family:monospace}

/* LESSON */
.lesson-card{background:var(--surface);border:1px solid var(--border);border-left:4px solid var(--blue);padding:20px}
.lesson-tradition{font-size:9px;letter-spacing:3px;text-transform:uppercase;color:var(--blue);font-family:sans-serif;font-weight:700;margin-bottom:8px}
.lesson-text{font-size:15px;line-height:1.7;color:var(--text);margin-bottom:8px;font-style:italic}
.lesson-source{font-size:10px;color:var(--muted);font-family:monospace}

/* CASE STUDY */
.case-card{background:var(--surface);border:1px solid var(--border);border-left:4px solid var(--purple);padding:20px}
.case-title{font-size:14px;font-weight:700;color:var(--purple);margin-bottom:10px;line-height:1.4}
.case-story{font-size:13px;line-height:1.7;color:#ccc;margin-bottom:12px}
.case-lesson{background:#0d0e12;border:1px solid var(--border);padding:12px;font-size:12px;color:var(--gold);line-height:1.6}
.case-lesson::before{content:'💡 Lesson: ';font-weight:700}

/* PICKS */
.pick-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px}
.pick-card{background:var(--surface);border:1px solid var(--border);border-left:3px solid var(--green);padding:14px}
.pick-sym{font-size:17px;font-weight:900;font-family:monospace;letter-spacing:1px}
.pick-price{font-size:22px;font-weight:700;font-family:monospace;margin:4px 0}
.pick-thesis{font-size:11px;color:#aaa;margin:8px 0;line-height:1.5;font-style:italic}
.pick-tgt{font-size:11px;padding-top:8px;border-top:1px solid var(--border);margin-top:8px}
.score-badge{display:inline-block;padding:2px 7px;font-size:9px;letter-spacing:1px;border-radius:2px;
  background:rgba(34,197,94,.1);color:var(--green);border:1px solid rgba(34,197,94,.25);font-family:sans-serif}

/* TRACKER */
.tbl{width:100%;border-collapse:collapse;font-size:12px}
.tbl th{background:#0a0b0e;color:var(--muted);font-size:9px;letter-spacing:1px;text-transform:uppercase;
  padding:8px 10px;text-align:left;border-bottom:1px solid var(--border)}
.tbl td{padding:9px 10px;border-bottom:1px solid #111;vertical-align:middle}
.tbl tr:hover td{background:#0f1014}
.pnl-u{color:var(--green);font-weight:700} .pnl-d{color:var(--red);font-weight:700}
.overflow{overflow-x:auto}

/* TIPS */
.tip-card{background:var(--surface);border:1px solid var(--border);border-left:3px solid var(--accent);padding:16px}
.tip-card h3{color:var(--accent);font-size:14px;margin-bottom:8px}
.tip-card p{font-size:13px;line-height:1.7}
.two{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:640px){.two{grid-template-columns:1fr}}

/* FORMS */
.form-box{background:var(--surface);border:1px solid var(--border);padding:15px;margin-top:14px}
.form-box h4{color:var(--accent);font-size:9px;letter-spacing:2px;text-transform:uppercase;margin-bottom:10px}
.frow{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:7px}
.frow input{background:#060709;border:1px solid var(--border);color:var(--text);padding:6px 10px;font-size:12px;flex:1;min-width:100px}
.frow input:focus{outline:none;border-color:var(--accent)}
.btn{background:var(--accent);color:#000;border:none;padding:7px 14px;font-size:11px;font-weight:700;cursor:pointer;letter-spacing:1px}
.btn-exit{background:transparent;color:var(--red);border:1px solid var(--red);padding:3px 9px;font-size:10px;cursor:pointer}
.btn-obs{background:transparent;color:var(--purple);border:1px solid var(--purple);padding:3px 9px;font-size:9px;cursor:pointer;letter-spacing:1px}
.footer{border-top:2px double var(--border);padding:20px;text-align:center;color:var(--muted);font-size:11px;margin-top:40px}
</style>
</head>
<body>

<div class="sticky-header">
<div class="masthead">
  <div class="paper-name">THE DAILY SIGNAL</div>
  <div class="paper-meta">
    <span>{{ date_str }}</span>
    <span>{{ updated_at }} IST · <a href="javascript:window.location.reload()" style="color:var(--muted)">↻</a></span>
  </div>
</div>

<nav class="nav">
  <a href="#weather">🌤 Weather</a>
  <a href="#news">🌍 World</a>
  <a href="#markets">📊 Markets</a>
  <a href="#lichess">♟ My Games</a>
  <a href="#quote">💬 Quote</a>
  <a href="#lesson">🌏 Lesson</a>
  <a href="#wisdom">🧘 Wisdom</a>
  <a href="#chess">♟ Chess</a>
  <a href="#case">📚 Case Study</a>
  <a href="#fpna">🎓 FP&A</a>
  <a href="#cfo">🏆 FC→CFO</a>
  <a href="#book">📚 Books</a>
  <a href="#alerts">🔔 Alerts</a>
  <a href="#picks">🔥 Top 5</a>
  <a href="#tracker">📈 Tracker</a>
  <a href="#hacks">💰 Money</a>
</nav>
</div><!-- /sticky-header -->

<div class="ticker">
  {% for m in markets %}
  <div class="t-item">
    <span class="t-name">{{ m.name }}</span>
    <span>{{ m.price }}</span>
    <span class="{{ 'up' if m.up else 'dn' }}">{{ m.change }}</span>
  </div>
  {% endfor %}
</div>

<div class="main">

<!-- WEATHER -->
<section class="section" id="weather">
  <div class="label">🌤 Weather Today — Bikaner · Kolkata · Kuala Lumpur</div>
  <div class="weather-grid">
    {% for w in weather %}
    <div class="wx-card">
      <div class="wx-country">{{ w.country }}</div>
      <div class="wx-city">{{ w.city }}</div>
      <div class="wx-emoji">{{ w.emoji }}</div>
      <div class="wx-temp">{{ w.temp }}°C</div>
      <div class="wx-cond">{{ w.condition }}</div>
      <div class="wx-range">↑ {{ w.temp_max }}° · ↓ {{ w.temp_min }}°</div>
      <div class="wx-meta">
        <span>💧 {{ w.humidity }}%</span>
        <span>💨 {{ w.wind }} km/h</span>
        <span>🌡 Feels {{ w.feels }}°C</span>
      </div>
      <div class="wx-rain {{ 'hi' if w.rain_alert else 'lo' }}">
        🌧 Rain chance: {{ w.rain_pct }}%{% if w.rain_alert %} — carry umbrella{% endif %}
      </div>
    </div>
    {% endfor %}
    {% if not weather %}
    <p style="color:var(--muted);grid-column:1/-1">Weather loading...</p>
    {% endif %}
  </div>
</section>

<!-- WORLD NEWS -->
<section class="section" id="news">
  <div class="label">🌍 World News — Last 24 Hours</div>
  <div class="news-grid">
    {% if news %}
      {% set lead = news[0] %}
      <div class="ncard lead">
        <div class="lead-main">
          <div class="src">{{ lead.source }} · LEAD</div>
          <h2>{% if lead.link %}<a href="{{ lead.link }}" target="_blank" style="color:var(--text)">{{ lead.title }}</a>{% else %}{{ lead.title }}{% endif %}</h2>
          <p style="font-size:13px;line-height:1.7;color:#aaa">{{ lead.summary }}</p>
        </div>
        <div class="lead-side">
          {% for item in news[1:6] %}
          <div style="margin-bottom:11px;padding-bottom:11px;border-bottom:1px solid var(--border)">
            <div class="src">{{ item.source }}</div>
            <div style="font-size:12px;font-weight:700;line-height:1.4">
              {% if item.link %}<a href="{{ item.link }}" target="_blank" style="color:var(--text)">{{ item.title }}</a>{% else %}{{ item.title }}{% endif %}
            </div>
          </div>
          {% endfor %}
        </div>
      </div>
      {% for item in news[6:15] %}
      <div class="ncard">
        <div class="src">{{ item.source }}</div>
        <h3>{% if item.link %}<a href="{{ item.link }}" target="_blank" style="color:var(--text)">{{ item.title }}</a>{% else %}{{ item.title }}{% endif %}</h3>
        <p>{{ item.summary[:140] }}</p>
        <div class="ts">{{ item.published }}</div>
      </div>
      {% endfor %}
    {% else %}
      <p style="color:var(--muted)">Loading feeds...</p>
    {% endif %}
  </div>
</section>

<!-- LICHESS GAMES -->
<section class="section" id="lichess">
  <div class="label">♟ Chess History — AKK_010 on Lichess · Last 30 Days
    <a href="https://lichess.org/@/AKK_010" target="_blank" style="font-size:9px;color:var(--muted);margin-left:8px">profile →</a>
  </div>
  {% if lichess_games %}
  <div class="lichess-summary">
    <div class="ls-stat"><div class="ls-num" style="color:var(--green)">{{ lichess_summary.wins }}</div><div class="ls-label">Wins</div></div>
    <div class="ls-stat"><div class="ls-num" style="color:var(--red)">{{ lichess_summary.losses }}</div><div class="ls-label">Losses</div></div>
    <div class="ls-stat"><div class="ls-num" style="color:var(--muted)">{{ lichess_summary.draws }}</div><div class="ls-label">Draws</div></div>
    <div class="ls-stat"><div class="ls-num" style="color:var(--accent)">{{ lichess_summary.score }}</div><div class="ls-label">Score</div></div>
    <div style="margin-left:auto;font-size:11px;color:var(--muted)">
      {% if lichess_summary.top_opening %}Most played: <strong style="color:var(--gold)">{{ lichess_summary.top_opening }}</strong>{% endif %}
    </div>
  </div>
  <div class="chess-game-grid">
    {% for g in lichess_games %}
    <div class="game-card {{ g.result.lower() }}">
      <div style="display:flex;justify-content:space-between;align-items:start">
        <div>
          <span class="game-result {{ g.result.lower() }}">{{ g.result_icon }} {{ g.result }}</span>
          <span style="font-size:10px;color:var(--muted);margin-left:8px">as {{ g.my_side }}</span>
        </div>
        <a href="{{ g.url }}" target="_blank" style="font-size:9px;color:var(--muted)">review →</a>
      </div>
      <div class="game-opening">{{ g.eco }} {{ g.opening }}</div>
      <div class="game-meta">
        vs <strong style="color:var(--text)">{{ g.opponent }}</strong> ({{ g.opp_rating }}) · {{ g.tc }} · {{ g.moves }} moves · {{ g.termination }}
      </div>
      {% if g.key_moves %}
      <div class="game-moves">Final moves: {{ g.key_moves }}</div>
      {% endif %}
      {% if g.analysis %}
      <div class="game-analysis">💡 {{ g.analysis }}</div>
      {% endif %}
    </div>
    {% endfor %}
  </div>
  {% else %}
  <div style="padding:16px;background:var(--surface);border:1px solid var(--border);color:var(--muted);font-size:12px">
    No games found in the last 30 days · <a href="https://lichess.org/@/AKK_010" target="_blank">Play on Lichess →</a>
  </div>
  {% endif %}
</section>

<!-- WISDOM: BETTER PERSON · BETTER DAD -->
<section class="section" id="wisdom">
  <div class="label">🧘 Daily Wisdom — Better Person · Better Dad · Happy Life · {{ wisdom.index }}/{{ wisdom.total }}</div>
  <div class="wisdom-card">
    <div class="wisdom-title">{{ wisdom.title }}</div>
    <div class="wisdom-body">{{ wisdom.body }}</div>
  </div>
</section>

<!-- CHESS TUTOR -->
<section class="section" id="chess">
  <div class="label">♟ Chess Tutor — Daily Lesson · {{ chess.index }}/{{ chess.total }}</div>
  <div class="chess-card">
    <div class="chess-title">{{ chess.title }}</div>
    <div class="chess-body">{{ chess.body }}</div>
    <div style="margin-top:12px;font-size:10px;color:var(--muted);font-family:monospace">
      Practice on: <a href="https://lichess.org/study" target="_blank">Lichess Study</a> · <a href="https://chess.com/puzzles" target="_blank">Chess.com Puzzles</a>
    </div>
  </div>
</section>

<!-- MARKETS -->
<section class="section" id="markets">
  <div class="label">📊 Markets Now</div>
  <div class="mkt-grid">
    {% for m in markets %}
    <div class="mkt-card {{ 'u' if m.up else 'd' }}">
      <div class="mkt-name">{{ m.name }}</div>
      <div class="mkt-price">{{ m.price }}</div>
      <div class="mkt-chg {{ 'up' if m.up else 'dn' }}">{{ m.change }}</div>
    </div>
    {% endfor %}
  </div>
</section>

<!-- ENTREPRENEUR QUOTE -->
<section class="section" id="quote">
  <div class="label">💬 Entrepreneur Quote · {{ quote.index }}/{{ quote.total }}</div>
  <div class="quote-card">
    <div class="quote-text">"{{ quote.quote }}"</div>
    <div class="quote-name">— {{ quote.name }}</div>
    <div class="quote-num">Quote {{ quote.index }} of {{ quote.total }} · Rotates daily</div>
  </div>
</section>

<!-- WORLD LESSON -->
<section class="section" id="lesson">
  <div class="label">🌏 Daily Lesson from the World</div>
  <div class="lesson-card">
    <div class="lesson-tradition">{{ lesson.tradition }}</div>
    <div class="lesson-text">"{{ lesson.lesson }}"</div>
    <div class="lesson-source">— {{ lesson.source }}</div>
  </div>
</section>

<!-- BUSINESS CASE STUDY -->
<section class="section" id="case">
  <div class="label">📚 Business Case Study</div>
  <div class="case-card">
    <div class="case-title">{{ case.title }}</div>
    <div class="case-story">{{ case.story }}</div>
    <div class="case-lesson">{{ case.lesson }}</div>
  </div>
</section>

<!-- FP&A LEARN -->
<section class="section" id="fpna">
  <div class="label">🎓 FP&A Learn · {{ fpna.index }}/{{ fpna.total }}</div>
  <div class="two">
    <div class="tip-card">
      <h3>{{ fpna.title }}</h3>
      <p>{{ fpna.body }}</p>
    </div>
    <div class="tip-card" style="border-left-color:var(--purple)">
      <h3 style="color:var(--purple)">🇦🇪 Dubai Corner</h3>
      <p>AED 30K+ stack: CA/ACCA + SAP or Oracle + Power BI + IFRS 9/16.<br><br>
      Targets: ADNOC · Emirates · Majid Al Futtaim · DP World · FAB · Emaar.<br><br>
      Keyword tip: Put "IFRS 16 implementation" and "rolling forecast" in your cover letter.</p>
    </div>
  </div>
</section>

<!-- FC → CFO PATH -->
<section class="section" id="cfo">
  <div class="label">🏆 Financial Controller → CFO Path · Practical Guidance · {{ cfo.index }}/{{ cfo.total }}</div>
  <div class="cfo-card">
    <div class="cfo-title">{{ cfo.title }}</div>
    <div class="cfo-body">{{ cfo.body }}</div>
  </div>
</section>

<!-- BOOK LESSONS -->
<section class="section" id="book">
  <div class="label">📚 Book Library · Daily Lesson · {{ book.index }}/{{ book.total }} · Top Business &amp; Productivity Books</div>
  <div class="book-card">
    <div class="book-meta">{{ book.book }} · {{ book.author }}</div>
    <div class="book-title">{{ book.chapter }}</div>
    <div class="book-body">{{ book.lesson }}</div>
    <div class="book-quote">"{{ book.key_quote }}"</div>
    <div class="book-action"><strong>Today's Action</strong>{{ book.action }}</div>
  </div>
</section>

<!-- TOP 5 PICKS -->
<section class="section" id="picks">
  <div class="label">🔥 Top 5 Trade Ideas · Global 200 Universe (India + US + Global) · Refreshes Weekly · 20–30% Target</div>
  {% if top5 %}
  <div class="pick-grid">
    {% for s in top5 %}
    <div class="pick-card">
      <div style="display:flex;justify-content:space-between;align-items:start">
        <div class="pick-sym">{{ s.name }}</div>
        <span class="score-badge">{{ s.score }}/100</span>
      </div>
      <div class="pick-price">{{ s.currency }}{{ s.price }}</div>
      <div class="{{ 'up' if s.change_1d >= 0 else 'dn' }}" style="font-size:12px;font-family:monospace">
        1D {{ '+' if s.change_1d >= 0 else '' }}{{ s.change_1d }}% · 1M {{ '+' if s.mom_1m >= 0 else '' }}{{ s.mom_1m }}% · 3M {{ '+' if s.mom_3m >= 0 else '' }}{{ s.mom_3m }}%
      </div>
      {% if s.thesis %}<div class="pick-thesis">"{{ s.thesis }}"</div>{% endif %}
      <div class="pick-tgt">
        🎯 <strong>{{ s.currency }}{{ s.target }}</strong> · 🛡 {{ s.currency }}{{ s.stop_loss }}<br>
        <span style="color:var(--muted);font-size:10px">⏱ {{ s.timeframe }}</span>
        <form action="/tracker/add" method="post" style="margin-top:8px">
          <input type="hidden" name="symbol" value="{{ s.symbol }}">
          <input type="hidden" name="name" value="{{ s.name }}">
          <input type="hidden" name="entry_price" value="{{ s.price }}">
          <input type="hidden" name="target_price" value="{{ s.target }}">
          <input type="hidden" name="stop_loss" value="{{ s.stop_loss }}">
          <input type="hidden" name="thesis" value="{{ s.thesis }}">
          <input type="hidden" name="timeframe" value="{{ s.timeframe }}">
          <button type="submit" class="btn" style="font-size:9px;padding:4px 10px">+ TRACK</button>
        </form>
      </div>
    </div>
    {% endfor %}
  </div>
  {% else %}
  <div style="padding:20px;color:var(--muted);font-size:13px;font-style:italic;text-align:center;background:var(--surface);border:1px solid var(--border)">
    📅 Top 5 picks refresh every Monday morning via GitHub Actions · Check back after 6 AM IST Monday
  </div>
  {% endif %}
</section>

<!-- STOCK TRACKER -->
<section class="section" id="tracker">
  <div class="label">📈 My Stock Tracker
    <form action="/tracker/obsidian" method="post" style="display:inline;margin-left:10px">
      <button type="submit" class="btn-obs">SYNC OBSIDIAN</button>
    </form>
    <a href="/tracker/history" target="_blank" style="font-size:9px;margin-left:8px;color:var(--muted)">exit history →</a>
  </div>
  {% if tracker %}
  <div class="overflow">
    <table class="tbl">
      <thead><tr>
        <th>Symbol</th><th>Entry</th><th>Current</th><th>Target</th><th>Stop</th>
        <th>P&L</th><th>Timeframe</th><th>Thesis</th><th>Added</th><th></th>
      </tr></thead>
      <tbody>
        {% for s in tracker %}
        <tr>
          <td><strong>{{ s.symbol }}</strong></td>
          <td style="font-family:monospace">{{ s.currency }}{{ s.entry_price }}</td>
          <td style="font-family:monospace" class="{{ 'up' if s.winning else 'dn' }}">{{ s.currency }}{{ s.current_price }}</td>
          <td style="font-family:monospace">{{ s.currency }}{{ s.target_price }}</td>
          <td style="font-family:monospace;color:var(--muted)">{{ s.currency }}{{ s.stop_loss }}</td>
          <td class="{{ 'pnl-u' if s.winning else 'pnl-d' }}">{{ '+' if s.winning else '' }}{{ s.pnl_pct }}%</td>
          <td style="color:var(--muted);font-size:10px">{{ s.timeframe }}</td>
          <td style="font-size:10px;max-width:180px">{{ s.thesis[:55] }}</td>
          <td style="font-size:10px;color:var(--muted)">{{ s.added_date }}</td>
          <td><form action="/tracker/exit/{{ s.id }}" method="post">
            <button type="submit" class="btn-exit">EXIT</button></form></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% else %}
  <p style="color:var(--muted);font-size:12px;padding:14px;background:var(--surface);border:1px solid var(--border)">
    No stocks tracked. Hit <strong>+ TRACK</strong> on any Top 5 pick or add manually below.
  </p>
  {% endif %}
  <div class="form-box">
    <h4>+ Add Stock Manually</h4>
    <form action="/tracker/add" method="post">
      <div class="frow">
        <input type="text" name="symbol" placeholder="Symbol e.g. RELIANCE.NS" required>
        <input type="text" name="name" placeholder="Name">
        <input type="number" step="0.01" name="entry_price" placeholder="Entry Price" required>
        <input type="number" step="0.01" name="target_price" placeholder="Target Price" required>
      </div>
      <div class="frow">
        <input type="number" step="0.01" name="stop_loss" placeholder="Stop Loss">
        <input type="text" name="timeframe" placeholder="Timeframe" value="2-3 months">
        <input type="text" name="thesis" placeholder="Why this stock?" style="flex:3">
      </div>
      <button type="submit" class="btn">ADD TO TRACKER</button>
    </form>
  </div>
</section>

<!-- MONEY + PRODUCTIVITY -->
<section class="section" id="hacks">
  <div class="label">💰 Money Hack &amp; ⚡ Productivity</div>
  <div class="two">
    <div class="tip-card">
      <h3>💰 {{ money_hack.title }}</h3>
      <p>{{ money_hack.body }}</p>
    </div>
    <div class="tip-card" style="border-left-color:var(--green)">
      <h3 style="color:var(--green)">⚡ Today's Rule</h3>
      <p>{{ productivity_tip }}</p>
    </div>
  </div>
</section>

<!-- TELEGRAM ALERTS LOG -->
<section class="section" id="alerts">
  <div class="label">🔔 Telegram Alert Log · All Signals Sent · {{ alerts|length }} total
    <a href="alerts.json" target="_blank" style="font-size:9px;color:var(--muted);margin-left:12px;font-family:monospace">↓ alerts.json</a>
  </div>

  {% if alerts %}
  {% set wins = alerts | selectattr("badge","eq","win") | list | length %}
  {% set losses = alerts | selectattr("badge","eq","loss") | list | length %}
  {% set opens = alerts | selectattr("badge","eq","open") | list | length %}
  {% set closed = wins + losses %}
  {% set winrate = ((wins / closed * 100) | round(0) | int) if closed > 0 else 0 %}
  <div class="alert-stats">
    <div class="ast"><div class="ast-num" style="color:var(--green)">{{ wins }}</div><div class="ast-label">Targets Hit</div></div>
    <div class="ast"><div class="ast-num" style="color:var(--red)">{{ losses }}</div><div class="ast-label">SL Hit</div></div>
    <div class="ast"><div class="ast-num" style="color:#60a5fa">{{ opens }}</div><div class="ast-label">Open</div></div>
    <div class="ast"><div class="ast-num" style="color:var(--gold)">{{ winrate }}%</div><div class="ast-label">Win Rate</div></div>
    <div class="ast"><div class="ast-num">{{ alerts|length }}</div><div class="ast-label">Total Alerts</div></div>
  </div>

  <div class="alert-filters">
    <button class="af-btn active" onclick="filterAlerts('all',this)">ALL</button>
    <button class="af-btn" onclick="filterAlerts('open',this)">OPEN</button>
    <button class="af-btn" onclick="filterAlerts('win',this)">TARGET HIT</button>
    <button class="af-btn" onclick="filterAlerts('loss',this)">SL HIT</button>
    <button class="af-btn" onclick="filterAlerts('cancelled',this)">CANCELLED</button>
  </div>

  <div class="overflow">
    <table class="alert-tbl" id="alertTable">
      <thead><tr>
        <th>Alert Date</th>
        <th>Symbol</th>
        <th>Signal</th>
        <th>Timeframe</th>
        <th>Entry</th>
        <th>SL</th>
        <th>Target 1</th>
        <th>Target 2</th>
        <th>RR</th>
        <th>Exit Price</th>
        <th>P&L</th>
        <th>Close Date</th>
        <th>Status</th>
      </tr></thead>
      <tbody>
      {% for a in alerts %}
        <tr data-badge="{{ a.badge }}">
          <td style="font-family:monospace;color:var(--muted)">{{ a.alert_date }}</td>
          <td>
            <a class="alert-sym" href="https://www.tradingview.com/chart/?symbol=NSE:{{ a.symbol }}" target="_blank">{{ a.symbol }}</a>
          </td>
          <td class="{{ 'alert-buy' if a.action == 'BUY' else 'alert-sell' }}">{{ a.action }}{% if a.signal_type %} <span style="color:var(--muted);font-size:9px">· {{ a.signal_type }}</span>{% endif %}</td>
          <td style="color:var(--muted)">{{ a.timeframe or '—' }}</td>
          <td style="font-family:monospace">{% if a.entry %}₹{{ "%.2f"|format(a.entry) }}{% else %}—{% endif %}</td>
          <td style="font-family:monospace;color:var(--red)">{% if a.sl %}₹{{ "%.2f"|format(a.sl) }}{% else %}—{% endif %}</td>
          <td style="font-family:monospace;color:var(--green)">{% if a.target1 %}₹{{ "%.2f"|format(a.target1) }}{% else %}—{% endif %}</td>
          <td style="font-family:monospace;color:var(--green)">{% if a.target2 %}₹{{ "%.2f"|format(a.target2) }}{% else %}—{% endif %}</td>
          <td style="font-family:monospace;color:var(--gold)">{{ a.rr or '—' }}{% if a.rr %}x{% endif %}</td>
          <td style="font-family:monospace">{% if a.exit_price %}₹{{ "%.2f"|format(a.exit_price) }}{% else %}—{% endif %}</td>
          <td style="font-family:monospace" class="{{ 'pnl-u' if (a.pnl_pct or 0) > 0 else ('pnl-d' if (a.pnl_pct or 0) < 0 else '') }}">{{ a.pnl_str }}</td>
          <td style="font-family:monospace;color:var(--muted)">{{ a.close_date }}</td>
          <td><span class="badge badge-{{ a.badge }}">{{ a.status or 'OPEN' }}</span></td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
  {% else %}
  <div style="padding:20px;color:var(--muted);font-size:13px;text-align:center;background:var(--surface);border:1px solid var(--border)">
    No alerts logged yet · Alerts appear here after Telegram signals are sent
  </div>
  {% endif %}
</section>

</div>

<div class="footer">
  <strong style="color:var(--accent)">THE DAILY SIGNAL</strong> · Akshay Kothari · @askakshayfinance<br>
  <span style="color:#333">news.askakshay.com · Refreshes 6 AM IST · Built with Claude Code</span>
</div>

<script>
// Auto-refresh every 5 min
setTimeout(() => window.location.reload(), 5 * 60 * 1000);

// Alert log filter
function filterAlerts(badge, btn) {
  document.querySelectorAll('.af-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('#alertTable tbody tr').forEach(row => {
    row.style.display = (badge === 'all' || row.dataset.badge === badge) ? '' : 'none';
  });
}

// Block tracker form submissions on static GitHub Pages (routes don't exist)
document.addEventListener('DOMContentLoaded', () => {
  const staticHost = location.hostname.includes('github.io');
  if (staticHost) {
    document.querySelectorAll('form[action^="/tracker"], form[action^="/api"]').forEach(f => {
      f.addEventListener('submit', e => {
        e.preventDefault();
        alert('Stock Tracker requires the live server. This static page auto-refreshes daily with market data — tracker features are not available here.');
      });
    });
    document.querySelectorAll('a[href^="/tracker"], a[href^="/api"]').forEach(a => {
      a.addEventListener('click', e => { e.preventDefault(); });
      a.style.opacity = '0.3';
      a.style.cursor = 'not-allowed';
      a.style.pointerEvents = 'auto';
    });
  }
});
</script>
</body>
</html>"""

# ─────────────────────────────────────────────────────────────
# FLASK ROUTES
# ─────────────────────────────────────────────────────────────

@app.route("/")
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
        top5           = get_top5_picks()
        tracker        = get_tracker_stocks()
        money          = get_money_hack()
        prod           = get_productivity_tip()
        weather        = fetch_weather()
        quote          = get_entrepreneur_quote()
        lesson         = get_world_lesson()
        case           = get_case_study()
        lichess_games  = fetch_lichess_games()
        lichess_summary = get_lichess_summary(lichess_games)
        alerts         = fetch_alert_log()

        return render_template_string(TEMPLATE,
            date_str=now.strftime("%A, %B %d %Y"),
            updated_at=now.strftime("%H:%M"),
            markets=markets, news=news, fpna=fpna, cfo=cfo,
            chess=chess, wisdom=wisdom, book=book,
            top5=top5, tracker=tracker, money_hack=money,
            productivity_tip=prod, weather=weather,
            quote=quote, lesson=lesson, case=case,
            lichess_games=lichess_games, lichess_summary=lichess_summary,
            alerts=alerts,
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
            top5=[], tracker=[], money_hack={"title":"Loading","body":""},
            productivity_tip="Loading...", weather=[],
            quote={"quote":"","name":"","index":0,"total":1},
            lesson={"tradition":"","lesson":"","source":""},
            case={"title":"","story":"","lesson":""},
            lichess_games=[], lichess_summary={},
            alerts=[],
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
    init_newspaper_db()
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
