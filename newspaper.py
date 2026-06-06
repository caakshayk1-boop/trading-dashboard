#!/usr/bin/env python3
"""
THE DAILY SIGNAL — Akshay's Personal Intelligence Brief
Sections: Weather · World News · Dubai Jobs · Markets · FP&A · Entrepreneur Quote
          Daily Lesson · Business Case Study · Top 5 Picks · Stock Tracker
          Money Hack · Productivity
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
# TOP 5 STOCK PICKS
# ─────────────────────────────────────────────────────────────

WATCHLIST = [
    # NSE — Large Cap
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS",
    "KOTAKBANK.NS", "HINDUNILVR.NS", "SBIN.NS", "BAJFINANCE.NS", "BHARTIARTL.NS",
    # NSE — Mid/Small Cap momentum
    "ADANIENT.NS", "TATAMOTORS.NS", "SUNPHARMA.NS", "IRCTC.NS", "TATAPOWER.NS",
    "ZOMATO.NS", "DIXON.NS", "POWERGRID.NS", "PIDILITIND.NS", "DMART.NS",
    "PERSISTENT.NS", "LTIM.NS", "COFORGE.NS", "MPHASIS.NS", "KPITTECH.NS",
    "TATAELXSI.NS", "POLYCAB.NS", "ASTRAL.NS", "CAMS.NS", "ANGELONE.NS",
    "LALPATHLAB.NS", "METROPOLIS.NS", "NUVAMA.NS", "360ONE.NS", "BIKAJI.NS",
    # NSE — infra/energy
    "NTPC.NS", "NHPC.NS", "SJVN.NS", "COALINDIA.NS", "BPCL.NS",
    "IOC.NS", "GAIL.NS", "ONGC.NS", "TORNTPOWER.NS", "CESC.NS",
    # US — Large Cap
    "NVDA", "META", "AMD", "TSLA", "MSFT", "GOOGL", "TSM", "NVO",
    # US — Mid Cap growth
    "CRWD", "SNOW", "DDOG", "NET", "MDB", "PANW",
    "AXON", "PODD", "CELH", "DUOL",
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

.masthead{border-bottom:3px double var(--border);padding:20px 20px 14px;text-align:center;background:#060709}
.paper-name{font-size:42px;font-weight:900;letter-spacing:8px;color:var(--accent);line-height:1;font-family:sans-serif}
.paper-sub{font-style:italic;color:var(--muted);font-size:12px;margin-top:4px;letter-spacing:2px}
.paper-meta{display:flex;justify-content:space-between;margin-top:10px;font-size:10px;color:var(--muted);
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

/* JOBS */
.jobs-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:580px){.jobs-grid{grid-template-columns:1fr}}
.jcard{background:var(--surface);border:1px solid var(--border);border-left:3px solid var(--purple);padding:13px}
.jcard .src{font-size:9px;letter-spacing:2px;text-transform:uppercase;color:var(--purple);margin-bottom:4px;font-family:sans-serif}
.jcard h4{font-size:13px;font-weight:700;line-height:1.4;margin-bottom:5px}
.jcard p{font-size:11px;color:var(--muted)}

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

<div class="masthead">
  <div class="paper-name">THE DAILY SIGNAL</div>
  <div class="paper-sub">Akshay's Personal Intelligence Brief · Numbers First · Always</div>
  <div class="paper-meta">
    <span>news.askakshay.com · Personal Edition</span>
    <span>{{ date_str }}</span>
    <span>{{ updated_at }} IST · <a href="/api/refresh" style="color:var(--muted)">↻ refresh</a></span>
  </div>
</div>

<nav class="nav">
  <a href="#weather">🌤 Weather</a>
  <a href="#news">🌍 World</a>
  <a href="#jobs">🇦🇪 Jobs</a>
  <a href="#markets">📊 Markets</a>
  <a href="#quote">💬 Quote</a>
  <a href="#lesson">🌏 Lesson</a>
  <a href="#case">📚 Case Study</a>
  <a href="#fpna">🎓 FP&A</a>
  <a href="#picks">🔥 Top 5</a>
  <a href="#tracker">📈 Tracker</a>
  <a href="#hacks">💰 Money</a>
</nav>

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

<!-- GULF JOBS -->
<section class="section" id="jobs">
  <div class="label">🌍 Gulf + India FP&A Jobs · Live from LinkedIn · AED 30K+ Target</div>
  <div class="jobs-grid">
    {% for j in dubai_jobs %}
    <div class="jcard">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
        <div class="src">{{ j.source }}</div>
        <div style="font-size:10px;color:var(--muted)">{{ j.city[:30] }}</div>
      </div>
      <h4>{% if j.link %}<a href="{{ j.link }}" target="_blank" style="color:var(--text)">{{ j.title }}</a>{% else %}{{ j.title }}{% endif %}</h4>
    </div>
    {% endfor %}
    {% if not dubai_jobs %}<p style="color:var(--muted)">Fetching live jobs...</p>{% endif %}
  </div>
  <div style="margin-top:10px;font-size:11px;color:var(--muted);padding:10px;background:var(--surface);border:1px solid var(--border)">
    <strong style="color:var(--purple)">Search More:</strong>
    <a href="https://www.linkedin.com/jobs/search/?keywords=FP%26A+Manager&location=Dubai" target="_blank">LinkedIn UAE</a> ·
    <a href="https://www.linkedin.com/jobs/search/?keywords=FP%26A+Finance+Manager&location=Saudi+Arabia" target="_blank">LinkedIn KSA</a> ·
    <a href="https://www.naukrigulf.com/fp-a-jobs-in-uae" target="_blank">NaukriGulf</a> ·
    <a href="https://www.bayt.com/en/uae/jobs/financial-planning-analysis-manager-jobs/" target="_blank">Bayt</a>
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

<!-- TOP 5 PICKS -->
<section class="section" id="picks">
  <div class="label">🔥 Top 5 Trade Ideas · 60-Stock Universe · Refreshes Weekly (Mon) · 20–30% Target</div>
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
    ⏳ Scanning 60 stocks for best momentum setups... first load ~90s
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

</div>

<div class="footer">
  <strong style="color:var(--accent)">THE DAILY SIGNAL</strong> · Akshay Kothari · @askakshayfinance<br>
  <span style="color:#333">news.askakshay.com · Refreshes 6 AM IST · Built with Claude Code</span>
</div>

<script>
setTimeout(() => window.location.reload(), 5 * 60 * 1000);
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
        markets = fetch_markets()
        news    = fetch_global_news()
        fpna    = get_fpna_tip()
        top5    = get_top5_picks()
        tracker = get_tracker_stocks()
        money   = get_money_hack()
        prod    = get_productivity_tip()
        jobs    = fetch_dubai_jobs()
        weather = fetch_weather()
        quote   = get_entrepreneur_quote()
        lesson  = get_world_lesson()
        case    = get_case_study()

        return render_template_string(TEMPLATE,
            date_str=now.strftime("%A, %B %d %Y"),
            updated_at=now.strftime("%H:%M"),
            markets=markets, news=news, fpna=fpna,
            top5=top5, tracker=tracker, money_hack=money,
            productivity_tip=prod, dubai_jobs=jobs, weather=weather,
            quote=quote, lesson=lesson, case=case,
        )
    except Exception as e:
        log.error(f"index error: {e}")
        import traceback; traceback.print_exc()
        now = datetime.now(IST)
        return render_template_string(TEMPLATE,
            date_str=now.strftime("%A, %B %d %Y"),
            updated_at=f"{now.strftime('%H:%M')} (partial)",
            markets=[], news=[], fpna={"title":"Loading","body":"","index":0,"total":1},
            top5=[], tracker=[], money_hack={"title":"Loading","body":""},
            productivity_tip="Loading...", dubai_jobs=[], weather=[],
            quote={"quote":"","name":"","index":0,"total":1},
            lesson={"tradition":"","lesson":"","source":""},
            case={"title":"","story":"","lesson":""},
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
