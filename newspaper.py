#!/usr/bin/env python3
"""
newspaper.py — AKK Times: Akshay's Personal Daily Newspaper
Flask web app. Deploys as Railway web: service.
Sections: Global News · Dubai Jobs · Markets · FP&A Learn · Top 5 Trade Ideas
          Stock Tracker · OTT/Bollywood · Money Hack · Productivity
"""
from __future__ import annotations

import os, json, sqlite3, logging, time, threading
from datetime import datetime, timezone, timedelta, date
from typing import Optional
import feedparser
import yfinance as yf
import requests
from flask import Flask, render_template_string, jsonify, request, redirect
from content_cache import get_cached_markets, get_cached_jobs, get_cached_news, get_cached_quote
import db as _db_mod

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

IST      = timezone(timedelta(hours=5, minutes=30))
GROQ_KEY  = os.environ.get("GROQ_API_KEY", "")
PORT      = int(os.environ.get("PORT", 5050))

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
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol        TEXT NOT NULL,
            name          TEXT,
            added_date    TEXT,
            entry_price   REAL,
            current_price REAL,
            target_price  REAL,
            stop_loss     REAL,
            thesis        TEXT,
            timeframe     TEXT,
            status        TEXT DEFAULT 'active',
            updated_at    TEXT
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS newspaper_stocks_picked (
            pick_date TEXT PRIMARY KEY,
            picks     TEXT
        )""")

# ─────────────────────────────────────────────────────────────
# GROQ AI — thesis + summary generator
# ─────────────────────────────────────────────────────────────

def groq_complete(prompt: str, max_tokens: int = 120) -> str:
    if not GROQ_KEY:
        return ""
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama3-8b-8192",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0.7,
            },
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.warning(f"Groq: {e}")
    return ""

def ai_stock_thesis(symbol: str, mom_1m: float, mom_3m: float, score: int) -> str:
    prompt = (
        f"Stock: {symbol}. 1-month return: {mom_1m:.1f}%. 3-month return: {mom_3m:.1f}%. "
        f"Momentum score: {score}/100. "
        "Write ONE concise sentence (max 20 words) explaining why this stock could return 20-30% in 1-3 months. "
        "Be specific, numbers-first, no fluff."
    )
    result = groq_complete(prompt, max_tokens=60)
    return result if result else f"Strong {mom_3m:.0f}% 3-month momentum with bullish trend structure."

# ─────────────────────────────────────────────────────────────
# CONTENT: GLOBAL NEWS
# ─────────────────────────────────────────────────────────────

NEWS_FEEDS = [
    ("Reuters World",  "https://feeds.reuters.com/reuters/worldNews"),
    ("BBC World",      "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Economic Times", "https://economictimes.indiatimes.com/rssfeedstopstories.cms"),
    ("Al Jazeera",     "https://www.aljazeera.com/xml/rss/all.xml"),
    ("Mint",           "https://www.livemint.com/rss/markets"),
    ("MoneyControl",   "https://www.moneycontrol.com/rss/business.xml"),
]

def fetch_global_news(max_per_feed: int = 4) -> list[dict]:
    # Delegate to shared cache — avoids duplicate RSS fetches with daily_brief.py
    return get_cached_news()[:max_per_feed * len(NEWS_FEEDS)]

def _fetch_global_news_direct(max_per_feed: int = 4) -> list[dict]:
    """Original direct-fetch version — used internally by content_cache.py only."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    articles = []
    for source, url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:max_per_feed]:
                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    try:
                        published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                    except Exception:
                        pass
                summary = getattr(entry, "summary", "")
                # Strip HTML tags crudely
                import re
                summary = re.sub(r"<[^>]+>", "", summary)[:300]
                articles.append({
                    "source": source,
                    "title": entry.get("title", "")[:120],
                    "link": entry.get("link", ""),
                    "summary": summary,
                    "published": published.strftime("%b %d %H:%M") if published else "",
                    "recent": published >= cutoff if published else True,
                })
        except Exception as e:
            log.warning(f"News feed {source}: {e}")
    articles.sort(key=lambda x: x["recent"], reverse=True)
    return articles[:18]

# ─────────────────────────────────────────────────────────────
# CONTENT: DUBAI JOBS
# ─────────────────────────────────────────────────────────────

DUBAI_JOB_FEEDS = [
    ("GulfTalent",   "https://www.gulftalent.com/rss/jobs/finance.xml"),
    ("Bayt Finance", "https://www.bayt.com/en/uae/jobs/financial-analyst-jobs/rss/"),
    ("LinkedIn UAE", "https://www.linkedin.com/jobs/search/?keywords=FP%26A+Manager&location=Dubai&f_TPR=r86400"),
]

DUBAI_JOB_KEYWORDS = ["fp&a", "financial planning", "financial analyst", "finance manager",
                       "budget", "forecasting", "controller", "cfo", "treasury", "fp &a"]

def fetch_dubai_jobs() -> list[dict]:
    return get_cached_jobs()

def _fetch_dubai_jobs_direct() -> list[dict]:
    """Original direct-fetch version — used internally by content_cache.py only."""
    jobs = []
    # Try RSS feeds first
    for source, url in DUBAI_JOB_FEEDS[:2]:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:6]:
                title = entry.get("title", "").lower()
                if any(k in title for k in DUBAI_JOB_KEYWORDS):
                    jobs.append({
                        "source": source,
                        "title": entry.get("title", "")[:100],
                        "link": entry.get("link", ""),
                        "summary": getattr(entry, "summary", "")[:200],
                    })
        except Exception as e:
            log.warning(f"Dubai jobs feed {source}: {e}")

    # Fallback: curated high-value targets
    if len(jobs) < 3:
        jobs += [
            {"source": "Target Company", "title": "FP&A Manager — ADNOC Group",
             "link": "https://careers.adnoc.ae", "summary": "AED 28–40K/month. CA/ACCA required. SAP, Power BI. Abu Dhabi."},
            {"source": "Target Company", "title": "Senior Financial Analyst — Emirates Group",
             "link": "https://www.emiratesgroupcareers.com", "summary": "AED 22–32K/month. ACCA/CFA. Excel + Hyperion. Dubai."},
            {"source": "Target Company", "title": "Finance Business Partner — Majid Al Futtaim",
             "link": "https://careers.majidalfuttaim.com", "summary": "AED 25–38K/month. CA preferred. Oracle Fusion. Dubai."},
            {"source": "Target Company", "title": "Group FP&A Analyst — DP World",
             "link": "https://careers.dpworld.com", "summary": "AED 20–30K/month. Big 4 background preferred. Dubai."},
            {"source": "Target Company", "title": "FP&A Lead — First Abu Dhabi Bank",
             "link": "https://jobs.bankfab.com", "summary": "AED 30–45K/month. CFA/CA + banking exp. Abu Dhabi/Dubai."},
        ]
    return jobs[:8]

# ─────────────────────────────────────────────────────────────
# CONTENT: MARKETS
# ─────────────────────────────────────────────────────────────

MARKET_TICKERS = [
    ("Nifty 50",  "^NSEI",    "₹", ".0f"),
    ("S&P 500",   "^GSPC",    "",  ".0f"),
    ("Nasdaq",    "^IXIC",    "",  ".0f"),
    ("Gold",      "GC=F",     "$", ".1f"),
    ("Crude",     "CL=F",     "$", ".2f"),
    ("USD/INR",   "USDINR=X", "₹", ".2f"),
    ("BTC",       "BTC-USD",  "$", ".0f"),
    ("Sensex",    "^BSESN",   "₹", ".0f"),
]

def fetch_markets() -> list[dict]:
    return get_cached_markets()

def _fetch_markets_direct() -> list[dict]:
    """Original direct-fetch version — used internally by content_cache.py only."""
    out = []
    for name, ticker, prefix, fmt in MARKET_TICKERS:
        try:
            info = yf.Ticker(ticker).fast_info
            price = info.last_price
            prev  = info.previous_close
            chg   = ((price - prev) / prev * 100) if prev else 0
            out.append({"name": name, "price": f"{prefix}{price:{fmt}}", "change": f"{chg:+.2f}%", "up": chg >= 0})
        except Exception as e:
            log.warning(f"Market {ticker}: {e}")
    return out

# ─────────────────────────────────────────────────────────────
# CONTENT: FP&A DAILY LEARN
# ─────────────────────────────────────────────────────────────

FPNA_TIPS = [
    ("Zero-Based Budgeting", "Start every budget from ₹0, not last year's number. Justify every line. Cuts 15–30% bloat in most orgs."),
    ("Driver-Based Forecasting", "Build forecasts on business drivers (units, headcount, utilization), not historical % growth. More accurate, easier to explain."),
    ("Rolling Forecast vs Annual Budget", "Rolling 12-month forecasts beat static annual budgets. Less time on 'budget vs actual' defense, more on forward-looking decisions."),
    ("Variance Analysis Framework", "Volume variance + Price/Rate variance + Mix variance = Total variance. Always decompose before presenting to leadership."),
    ("Working Capital Management", "DSO + DIO – DPO = Cash Conversion Cycle. Cutting CCC by 5 days can free millions in cash."),
    ("EBITDA Bridge", "Walk from prior period to current: Revenue ±, COGS ±, SG&A ±, Other ±. Bridges tell the story behind the number."),
    ("Scenario Planning", "Always model 3 scenarios: Base, Bull (+20% revenue), Bear (–20%). Present the range, not just base. Executives hate surprises."),
    ("Contribution Margin Analysis", "CM = Revenue – Variable Costs. Know your CM by product, by customer, by geography. Profitability lives in the mix."),
    ("Headcount Planning", "FTE cost = Salary × 1.3–1.5 (benefits + overhead). Always model hiring lag — 60–90 days from approval to productive."),
    ("Free Cash Flow vs Net Income", "FCF = Net Income + D&A – Capex – ΔNWC. A company can show profit and still run out of cash."),
    ("SaaS Metrics for FP&A", "ARR, MRR, Churn, NRR, CAC, LTV. If you're in tech FP&A and don't track these, you're flying blind."),
    ("Power BI for FP&A", "Replace Excel pivots with Power BI. Connect to your ERP/BI source. Saves 5+ hours/month on month-end decks."),
    ("The 80/20 of Month-End Close", "20% of accounts drive 80% of variance. Focus commentary there. The rest is noise."),
    ("Strategic Finance vs FP&A", "FP&A = forecast + budget + reporting. Strategic Finance = M&A, pricing, market expansion."),
    ("Dubai FP&A Market Reality", "AED 30K+ roles exist at ADNOC, Emirates, MAF, DP World. Required: CA/ACCA + Power BI + SAP + IFRS 9/16. Apply daily."),
    ("Three-Statement Model", "P&L → Balance Sheet → Cash Flow — they must tie. If they don't, you have a bug."),
    ("Sensitivity Tables", "Use Excel's Data Table (What-If Analysis) to show EBITDA across revenue and margin assumptions. One table > 5 slides."),
    ("Cost Centre vs Profit Centre", "Cost centres are budgeted, profit centres are managed to a P&L. Knowing the difference changes how you frame every problem."),
    ("CFO Communication Formula", "Lead with the number, then variance, then reason, then action. 'EBITDA was ₹12Cr, ₹2Cr below plan, due to X, here's the fix.'"),
    ("Financial Storytelling", "Data without narrative is noise. Frame every number in context: vs budget, vs prior year, vs industry."),
]

def get_fpna_tip() -> dict:
    idx = date.today().toordinal() % len(FPNA_TIPS)
    title, body = FPNA_TIPS[idx]
    return {"title": title, "body": body, "index": idx + 1, "total": len(FPNA_TIPS)}

# ─────────────────────────────────────────────────────────────
# CONTENT: TOP 5 STOCK IDEAS — momentum screener
# ─────────────────────────────────────────────────────────────

# Focused 18-stock watchlist (quality > quantity for speed)
WATCHLIST = [
    # India large/mid — high conviction
    "RELIANCE.NS", "TATAMOTORS.NS", "BAJFINANCE.NS", "ADANIENT.NS", "SUNPHARMA.NS",
    "IRCTC.NS", "TATAPOWER.NS", "ZOMATO.NS", "DIXON.NS", "POWERGRID.NS",
    # Global — high beta tech + conviction
    "NVDA", "META", "AMD", "TSLA", "MSFT", "GOOGL", "TSM", "NVO",
]

# In-memory cache for warm startup
_picks_cache: dict = {}
_picks_lock = threading.Lock()

def score_stock(sym: str) -> Optional[dict]:
    try:
        hist = yf.Ticker(sym).history(period="3mo")
        if hist.empty or len(hist) < 20:
            return None
        close    = hist["Close"]
        ema20    = close.ewm(span=20).mean().iloc[-1]
        ema50    = close.ewm(span=50).mean().iloc[-1]
        price    = close.iloc[-1]
        prev     = close.iloc[-2]
        mom_1m   = (price - close.iloc[-22]) / close.iloc[-22] * 100 if len(close) >= 22 else 0
        mom_3m   = (price - close.iloc[0])   / close.iloc[0]  * 100
        vol_ratio = hist["Volume"].iloc[-5:].mean() / (hist["Volume"].iloc[-20:].mean() or 1)

        score = 0
        if price > ema20:      score += 25
        if price > ema50:      score += 20
        if ema20 > ema50:      score += 15
        if mom_1m > 5:         score += 20
        if mom_3m > 10:        score += 10
        if vol_ratio > 1.2:    score += 10
        if (price - close.max()) / close.max() * 100 > -10: score += 10  # near 52w high

        currency = "₹" if ".NS" in sym or ".BO" in sym else "$"
        target   = round(price * (1.25 if mom_3m > 15 else 1.20), 2)
        return {
            "symbol":    sym,
            "name":      sym.replace(".NS", "").replace(".BO", ""),
            "price":     round(price, 2),
            "change_1d": round((price - prev) / prev * 100, 2),
            "mom_1m":    round(mom_1m, 1),
            "mom_3m":    round(mom_3m, 1),
            "score":     score,
            "target":    target,
            "stop_loss": round(price * 0.92, 2),
            "timeframe": "2–3 months",
            "currency":  currency,
            "thesis":    "",  # filled by Groq below
        }
    except Exception as e:
        log.warning(f"score_stock {sym}: {e}")
        return None

def _build_picks() -> list[dict]:
    scored = []
    for sym in WATCHLIST:
        s = score_stock(sym)
        if s:
            scored.append(s)
        time.sleep(0.05)
    scored.sort(key=lambda x: x["score"], reverse=True)
    top5 = scored[:5]
    # Add Groq thesis
    for s in top5:
        s["thesis"] = ai_stock_thesis(s["name"], s["mom_1m"], s["mom_3m"], s["score"])
        time.sleep(0.1)
    return top5

def _warm_picks_cache():
    """Background thread: pre-build picks on startup."""
    today = date.today().isoformat()
    with _db() as con:
        row = con.execute("SELECT picks FROM newspaper_stocks_picked WHERE pick_date=?", (today,)).fetchone()
        if row:
            with _picks_lock:
                _picks_cache[today] = json.loads(row["picks"])
            log.info("picks: loaded from DB cache")
            return
    log.info("picks: warming cache in background...")
    picks = _build_picks()
    with _db() as con:
        con.execute("INSERT OR REPLACE INTO newspaper_stocks_picked VALUES (?,?)",
                    (today, json.dumps(picks)))
    with _picks_lock:
        _picks_cache[today] = picks
    log.info(f"picks: cache warmed with {len(picks)} stocks")

def get_top5_picks() -> list[dict]:
    today = date.today().isoformat()
    with _picks_lock:
        if today in _picks_cache:
            return _picks_cache[today]
    with _db() as con:
        row = con.execute("SELECT picks FROM newspaper_stocks_picked WHERE pick_date=?", (today,)).fetchone()
        if row:
            picks = json.loads(row["picks"])
            with _picks_lock:
                _picks_cache[today] = picks
            return picks
    return []  # still warming

# ─────────────────────────────────────────────────────────────
# CONTENT: OTT & BOLLYWOOD
# ─────────────────────────────────────────────────────────────

OTT_FEEDS = [
    ("Bollywood Hungama", "https://www.bollywoodhungama.com/rss/news.xml"),
    ("Pinkvilla",         "https://www.pinkvilla.com/rss.xml"),
    ("Filmfare",          "https://www.filmfare.com/rss/news.xml"),
]

def fetch_ott_bollywood() -> list[dict]:
    items = []
    for source, url in OTT_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:4]:
                items.append({
                    "source": source,
                    "title":  entry.get("title", "")[:120],
                    "link":   entry.get("link", ""),
                    "summary": getattr(entry, "summary", "")[:180],
                })
        except Exception as e:
            log.warning(f"OTT {source}: {e}")
    return items[:10]

# ─────────────────────────────────────────────────────────────
# CONTENT: MONEY HACKS (15 rotating)
# ─────────────────────────────────────────────────────────────

MONEY_HACKS = [
    ("The 50-30-20 Rule", "50% needs · 30% wants · 20% savings. Automate the 20% on salary day so it never hits your spending account."),
    ("SIP on the 1st", "Set SIP date = salary day + 1. Invest before you spend. 'Pay yourself first' is the only rule that works long-term."),
    ("Expense Tracking", "Track expenses daily for 30 days. You will find ₹3–5K of invisible leaks — subscriptions, impulse buys, food delivery. Cut half."),
    ("Tax-Loss Harvesting", "Book losses in stocks at year-end to offset LTCG. Reinvest post 30 days. Saves 10–15% tax on gains — legally."),
    ("EPF Power", "EPF gives 8.25% guaranteed, tax-free on withdrawal. Max out VPF if your employer allows. Risk-free return beats most FDs."),
    ("No Lifestyle Inflation", "Got a raise? Don't upgrade your lifestyle. Invest the increment for 3 years. That delta compounds to 4–5x in 10 years."),
    ("Emergency Fund Math", "6 months of expenses in a liquid FD. Not less. Job loss + medical emergency can overlap. Be ready."),
    ("Credit Card Strategy", "Use credit card for all spends, pay in full before due date. Earn 1–2% cashback. Never pay interest — that's 36%+ APR."),
    ("NPS for Tax Saving", "₹50K in NPS under 80CCD(1B) = ₹15K saved at 30% bracket. Plus retirement corpus. Double win."),
    ("Index Funds over Active", "85% of active large-cap funds underperform Nifty over 10 years. Nifty 50 index: 0.1% expense ratio, full market returns."),
    ("Term Insurance First", "Before any investment: ₹1Cr term insurance (₹10–15K/year at 25). This is the foundation. Non-negotiable."),
    ("ELSS Lock-in Trick", "ELSS has 3-year lock-in. SIP every month → each instalment unlocks separately. Most flexible 80C option."),
    ("Gold via SGB", "Sovereign Gold Bonds: 2.5% interest + gold price appreciation. Zero storage cost. No capital gains if held to maturity."),
    ("Auto-Sweep FD", "Link savings account to sweep FD. Idle cash earns FD rates automatically. Free, instant, zero risk."),
    ("Direct Funds", "Regular mutual funds have 1–1.5% higher expense ratio than direct. Over 20 years = 20–30% of corpus lost to distributors."),
]

def get_money_hack() -> dict:
    idx = date.today().toordinal() % len(MONEY_HACKS)
    title, body = MONEY_HACKS[idx]
    return {"title": title, "body": body}

# ─────────────────────────────────────────────────────────────
# CONTENT: PRODUCTIVITY (20 rotating)
# ─────────────────────────────────────────────────────────────

PRODUCTIVITY_TIPS = [
    "Eat the frog: hardest task first, before checking any messages.",
    "2-minute rule: if it takes under 2 min, do it now. Don't queue it.",
    "Time-block your calendar. Unblocked time = wasted time.",
    "90-min deep work sprints. No phone. Door closed. Results compound.",
    "Write tomorrow's top 3 tasks tonight. Wake up with a plan, not a question.",
    "Done > perfect. Ship at 80%, iterate on real feedback.",
    "Weekly review: 15 min every Sunday. What worked, what's next week's #1.",
    "Batch similar tasks. Answer all messages in one sitting, not all day.",
    "Phone in another room during deep work. Physical distance reduces urge 60%.",
    "End every meeting with: who does what by when. No action = no meeting needed.",
    "Read 10 pages of non-fiction daily. 10 × 365 = 12 books/year.",
    "Respond to Slack/WhatsApp at set times. Real-time response is a myth.",
    "Track your energy, not just your time. Hard work when energy is highest.",
    "Define 'done' before starting. Vague tasks never finish.",
    "Set a 'shutdown complete' ritual. Signals brain to stop ruminating.",
    "Build systems, not goals. Goals are outcomes; systems produce them.",
    "Under-promise, over-deliver. Every time. Build a reputation.",
    "Weekly financial review: 10 min. Net worth, cash flow, investments. Numbers don't lie.",
    "Clear your inbox to zero before 9 AM. Empty inbox = no mental overhead.",
    "Use Parkinson's Law: shorter deadlines. Work expands to fill time given.",
]

def get_productivity_tip() -> str:
    idx = (date.today().toordinal() + 7) % len(PRODUCTIVITY_TIPS)
    return PRODUCTIVITY_TIPS[idx]

# ─────────────────────────────────────────────────────────────
# STOCK TRACKER (persistent SQLite)
# ─────────────────────────────────────────────────────────────

def get_tracker_stocks() -> list[dict]:
    with _db() as con:
        rows = con.execute(
            "SELECT * FROM stock_tracker WHERE status='active' ORDER BY added_date DESC"
        ).fetchall()
    out = []
    for r in rows:
        sym     = r["symbol"]
        current = r["current_price"] or r["entry_price"] or 0
        try:
            current = round(yf.Ticker(sym).fast_info.last_price, 2)
            with _db() as con:
                con.execute("UPDATE stock_tracker SET current_price=?, updated_at=? WHERE id=?",
                            (current, datetime.now(IST).isoformat(), r["id"]))
        except Exception:
            pass
        entry   = r["entry_price"] or current
        pnl_pct = (current - entry) / entry * 100 if entry else 0
        currency = "₹" if ".NS" in sym or ".BO" in sym else "$"
        out.append({
            "id":            r["id"],
            "symbol":        sym,
            "name":          r["name"] or sym,
            "entry_price":   entry,
            "current_price": current,
            "target_price":  r["target_price"] or 0,
            "stop_loss":     r["stop_loss"] or 0,
            "thesis":        r["thesis"] or "",
            "timeframe":     r["timeframe"] or "",
            "pnl_pct":       round(pnl_pct, 2),
            "added_date":    r["added_date"] or "",
            "currency":      currency,
            "winning":       pnl_pct >= 0,
        })
    return out

def add_to_tracker(symbol: str, entry_price: float, target_price: float,
                   stop_loss: float, thesis: str, timeframe: str = "2-3 months",
                   name: str = ""):
    with _db() as con:
        con.execute("""INSERT INTO stock_tracker
            (symbol, name, added_date, entry_price, current_price, target_price,
             stop_loss, thesis, timeframe, status, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,'active',?)""",
            (symbol.upper(), name or symbol, date.today().isoformat(),
             entry_price, entry_price, target_price, stop_loss,
             thesis, timeframe, datetime.now(IST).isoformat()))

def exit_tracker(stock_id: int):
    with _db() as con:
        con.execute("UPDATE stock_tracker SET status='exited', updated_at=? WHERE id=?",
                    (datetime.now(IST).isoformat(), stock_id))

# ─────────────────────────────────────────────────────────────
# OBSIDIAN SYNC via GitHub API
# ─────────────────────────────────────────────────────────────

def sync_tracker_to_obsidian(stocks: list[dict]) -> bool:
    import base64
    token = os.environ.get("GITHUB_TOKEN", "")
    repo  = os.environ.get("OBSIDIAN_GITHUB_REPO", "caakshayk1-boop/obsidian-brain")
    if not token:
        log.warning("GITHUB_TOKEN not set — cannot sync to Obsidian")
        return False
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    today   = date.today()
    path    = f"Daily/{today.strftime('%Y/%m')}/{today.strftime('%Y-%m-%d')}.md"
    api_url = f"https://api.github.com/repos/{repo}/contents/{path}"

    r = requests.get(api_url, headers=headers, timeout=10)
    if r.status_code == 200:
        data    = r.json()
        content = base64.b64decode(data["content"]).decode()
        sha     = data.get("sha")
    else:
        content = f"# {today.strftime('%B %d, %Y')}\n\n"
        sha     = None

    section  = "\n\n## 📈 AKK Stock Tracker\n\n"
    section += "| Symbol | Entry | Current | Target | P&L | Thesis |\n"
    section += "|--------|-------|---------|--------|-----|--------|\n"
    for s in stocks:
        pnl = f"{'▲' if s['winning'] else '▼'} {abs(s['pnl_pct']):.1f}%"
        section += f"| {s['symbol']} | {s['currency']}{s['entry_price']:.2f} | {s['currency']}{s['current_price']:.2f} | {s['currency']}{s['target_price']:.2f} | {pnl} | {str(s['thesis'])[:40]} |\n"
    section += f"\n_Updated: {datetime.now(IST).strftime('%H:%M IST')}_\n"

    anchor, end_anchor = "<!-- akk-stock-tracker -->", "<!-- /akk-stock-tracker -->"
    if anchor in content and end_anchor in content:
        s_idx = content.index(anchor)
        e_idx = content.index(end_anchor) + len(end_anchor)
        content = content[:s_idx] + anchor + section + end_anchor + content[e_idx:]
    else:
        content = content.rstrip() + "\n\n" + anchor + section + end_anchor + "\n"

    payload = {"message": f"newspaper: stock tracker {today.isoformat()}",
                "content": base64.b64encode(content.encode()).decode()}
    if sha:
        payload["sha"] = sha
    resp = requests.put(api_url, headers=headers, json=payload, timeout=15)
    ok   = resp.status_code in (200, 201)
    log.info(f"Obsidian sync: {'OK' if ok else 'FAIL'} ({resp.status_code})")
    return ok

# ─────────────────────────────────────────────────────────────
# HTML TEMPLATE
# ─────────────────────────────────────────────────────────────

TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>AKK Times — {{ date_str }}</title>
<style>
:root {
  --bg:#0d0d0d; --surface:#161616; --border:#242424; --accent:#e8c547;
  --red:#e05252; --green:#52e07a; --purple:#9b7fe8; --text:#e4e4e4; --muted:#777;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Georgia',serif;font-size:14px;line-height:1.65}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.up{color:var(--green)} .dn{color:var(--red)}

/* MASTHEAD */
.masthead{border-bottom:3px double var(--border);padding:18px 20px 12px;text-align:center;background:#0a0a0a}
.paper-name{font-size:46px;font-weight:900;letter-spacing:6px;color:var(--accent);line-height:1}
.paper-sub{font-style:italic;color:var(--muted);font-size:12px;margin-top:3px}
.paper-meta{display:flex;justify-content:space-between;margin-top:10px;font-size:10px;color:var(--muted);border-top:1px solid var(--border);padding-top:8px;font-family:monospace}

/* NAV */
.nav{display:flex;overflow-x:auto;background:#111;border-bottom:1px solid var(--border)}
.nav a{padding:9px 14px;color:var(--muted);font-size:10px;letter-spacing:1px;text-transform:uppercase;white-space:nowrap;border-right:1px solid var(--border)}
.nav a:hover{color:var(--accent);background:#161616;text-decoration:none}

/* TICKER */
.ticker{display:flex;overflow-x:auto;background:#0a0a0a;border-bottom:1px solid var(--border);padding:6px 0}
.t-item{display:flex;gap:6px;align-items:center;padding:0 16px;border-right:1px solid #1a1a1a;white-space:nowrap;font-size:12px;font-family:monospace}
.t-item:last-child{border-right:none}
.t-name{color:var(--muted);font-size:10px}

/* LAYOUT */
.main{max-width:1180px;margin:0 auto;padding:0 14px}
.section{margin:22px 0;padding-top:14px;border-top:2px solid var(--border)}
.label{font-size:9px;letter-spacing:3px;text-transform:uppercase;color:var(--accent);font-family:sans-serif;margin-bottom:12px;display:flex;align-items:center;gap:10px}
.label::after{content:'';flex:1;height:1px;background:var(--border)}

/* NEWS */
.news-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px}
@media(max-width:900px){.news-grid{grid-template-columns:1fr 1fr}}
@media(max-width:580px){.news-grid{grid-template-columns:1fr}}
.ncard{border:1px solid var(--border);padding:14px;background:var(--surface)}
.ncard .src{font-size:9px;letter-spacing:2px;text-transform:uppercase;color:var(--accent);margin-bottom:5px;font-family:sans-serif}
.ncard h3{font-size:13px;font-weight:700;line-height:1.4;margin-bottom:7px}
.ncard p{font-size:11px;color:var(--muted);line-height:1.5}
.ncard .ts{font-size:9px;color:#444;margin-top:7px;font-family:monospace}
.lead{grid-column:1/-1;display:grid;grid-template-columns:3fr 2fr;gap:20px;border:1px solid #2d2a1e;background:#0f0e09}
@media(max-width:640px){.lead{grid-template-columns:1fr}}
.lead-main{padding:16px}
.lead-side{padding:16px;border-left:1px solid var(--border)}
.lead h2{font-size:22px;line-height:1.3;margin-bottom:10px}

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

/* STOCK PICKS */
.pick-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px}
.pick-card{background:var(--surface);border:1px solid var(--border);border-left:3px solid var(--green);padding:14px}
.pick-sym{font-size:17px;font-weight:900;font-family:monospace;letter-spacing:1px}
.pick-price{font-size:22px;font-weight:700;font-family:monospace;margin:4px 0}
.pick-mom{font-size:12px;font-family:monospace}
.pick-thesis{font-size:11px;color:#aaa;margin:8px 0;line-height:1.5;font-style:italic}
.pick-tgt{font-size:11px;padding-top:8px;border-top:1px solid var(--border);margin-top:8px}
.score-badge{display:inline-block;padding:2px 7px;font-size:9px;letter-spacing:1px;border-radius:2px;background:rgba(82,224,122,.12);color:var(--green);border:1px solid rgba(82,224,122,.25);font-family:sans-serif}
.warming{padding:20px;color:var(--muted);font-size:13px;font-style:italic;text-align:center;background:var(--surface);border:1px solid var(--border)}

/* TRACKER */
.tbl{width:100%;border-collapse:collapse;font-size:12px}
.tbl th{background:#111;color:var(--muted);font-size:9px;letter-spacing:1px;text-transform:uppercase;padding:8px 10px;text-align:left;border-bottom:1px solid var(--border)}
.tbl td{padding:9px 10px;border-bottom:1px solid #1a1a1a;vertical-align:middle}
.tbl tr:hover td{background:#131313}
.pnl-u{color:var(--green);font-weight:700} .pnl-d{color:var(--red);font-weight:700}
.overflow{overflow-x:auto}

/* TIPS */
.tip-card{background:var(--surface);border:1px solid var(--border);border-left:3px solid var(--accent);padding:15px}
.tip-card h3{color:var(--accent);font-size:14px;margin-bottom:8px}
.tip-card p{font-size:13px;line-height:1.7}
.two{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:640px){.two{grid-template-columns:1fr}}

/* OTT */
.ott-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
@media(max-width:580px){.ott-grid{grid-template-columns:1fr}}
.ott-card{background:var(--surface);border:1px solid var(--border);padding:12px}
.ott-card .src{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:1px}
.ott-card h4{font-size:12px;font-weight:700;margin:4px 0;line-height:1.4}
.ott-card p{font-size:11px;color:var(--muted)}

/* FORMS */
.form-box{background:var(--surface);border:1px solid var(--border);padding:15px;margin-top:14px}
.form-box h4{color:var(--accent);font-size:9px;letter-spacing:2px;text-transform:uppercase;margin-bottom:10px}
.frow{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:7px}
.frow input{background:#0a0a0a;border:1px solid var(--border);color:var(--text);padding:6px 10px;font-size:12px;flex:1;min-width:100px}
.frow input:focus{outline:none;border-color:var(--accent)}
.btn{background:var(--accent);color:#000;border:none;padding:7px 14px;font-size:11px;font-weight:700;cursor:pointer;letter-spacing:1px}
.btn:hover{background:#d4b03c}
.btn-exit{background:transparent;color:var(--red);border:1px solid var(--red);padding:3px 9px;font-size:10px;cursor:pointer}
.btn-exit:hover{background:rgba(224,82,82,.1)}
.btn-obs{background:transparent;color:var(--purple);border:1px solid var(--purple);padding:3px 9px;font-size:9px;cursor:pointer;letter-spacing:1px}
.btn-obs:hover{background:rgba(155,127,232,.1)}

/* FOOTER */
.footer{border-top:2px double var(--border);padding:20px;text-align:center;color:var(--muted);font-size:11px;margin-top:36px}
</style>
</head>
<body>

<div class="masthead">
  <div class="paper-name">AKK TIMES</div>
  <div class="paper-sub">Your signal in a noisy world. Numbers first. Always.</div>
  <div class="paper-meta">
    <span>VOL. I · PERSONAL EDITION</span>
    <span>{{ date_str }}</span>
    <span>UPDATED {{ updated_at }} · <a href="/api/refresh" style="color:var(--muted)">↻ REFRESH</a></span>
  </div>
</div>

<nav class="nav">
  <a href="#news">🌍 World</a>
  <a href="#jobs">🇦🇪 Dubai Jobs</a>
  <a href="#markets">📊 Markets</a>
  <a href="#fpna">🎓 FP&A</a>
  <a href="#picks">🔥 Top 5</a>
  <a href="#tracker">📈 Tracker</a>
  <a href="#ott">🎬 OTT</a>
  <a href="#hacks">💰 Money</a>
  <a href="#productivity">⚡ Productivity</a>
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

<!-- WORLD NEWS -->
<section class="section" id="news">
  <div class="label">🌍 World News — Last 24 Hours</div>
  <div class="news-grid">
    {% if news %}
      {% set lead = news[0] %}
      <div class="ncard lead">
        <div class="lead-main">
          <div class="src">{{ lead.source }} · LEAD STORY</div>
          <h2>{% if lead.link %}<a href="{{ lead.link }}" target="_blank" style="color:var(--text)">{{ lead.title }}</a>{% else %}{{ lead.title }}{% endif %}</h2>
          <p style="font-size:13px;line-height:1.7;color:#aaa">{{ lead.summary }}</p>
        </div>
        <div class="lead-side">
          {% for item in news[1:6] %}
          <div style="margin-bottom:12px;padding-bottom:12px;border-bottom:1px solid var(--border)">
            <div class="src">{{ item.source }}</div>
            <div style="font-size:12px;font-weight:700;line-height:1.4">
              {% if item.link %}<a href="{{ item.link }}" target="_blank" style="color:var(--text)">{{ item.title }}</a>{% else %}{{ item.title }}{% endif %}
            </div>
            <div class="ts">{{ item.published }}</div>
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
      <p style="color:var(--muted)">Feeds loading...</p>
    {% endif %}
  </div>
</section>

<!-- DUBAI JOBS -->
<section class="section" id="jobs">
  <div class="label">🇦🇪 Dubai FP&A Jobs · Target AED 30K+/month</div>
  <div class="jobs-grid">
    {% for j in dubai_jobs %}
    <div class="jcard">
      <div class="src">{{ j.source }}</div>
      <h4>{% if j.link %}<a href="{{ j.link }}" target="_blank" style="color:var(--text)">{{ j.title }}</a>{% else %}{{ j.title }}{% endif %}</h4>
      {% if j.summary %}<p>{{ j.summary[:160] }}</p>{% endif %}
    </div>
    {% endfor %}
  </div>
  <div style="margin-top:10px;font-size:11px;color:var(--muted);padding:10px;background:var(--surface);border:1px solid var(--border)">
    <strong style="color:var(--purple)">Daily Action:</strong> Apply to 2 roles today on
    <a href="https://www.bayt.com/en/uae/jobs/financial-planning-analysis-manager-jobs/" target="_blank">Bayt</a> ·
    <a href="https://www.gulftalent.com/jobs/finance" target="_blank">GulfTalent</a> ·
    <a href="https://www.linkedin.com/jobs/search/?keywords=FP%26A+Manager&location=Dubai" target="_blank">LinkedIn UAE</a>
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

<!-- FP&A LEARN -->
<section class="section" id="fpna">
  <div class="label">🎓 FP&A Learn Today · {{ fpna.index }}/{{ fpna.total }}</div>
  <div class="two">
    <div class="tip-card">
      <h3>{{ fpna.title }}</h3>
      <p>{{ fpna.body }}</p>
    </div>
    <div class="tip-card" style="border-left-color:var(--purple)">
      <h3 style="color:var(--purple)">🇦🇪 Dubai Corner</h3>
      <p>Required stack for AED 30K+ roles: CA/ACCA/CPA + SAP or Oracle + Power BI + IFRS 9/16.<br><br>
      Top targets: ADNOC · Emirates Group · Majid Al Futtaim · DP World · First Abu Dhabi Bank · Emaar · Aldar Properties.<br><br>
      Tip: Mention "IFRS 16 implementation" and "rolling forecast" in your cover letter. These are hot keywords right now.</p>
    </div>
  </div>
</section>

<!-- TOP 5 PICKS -->
<section class="section" id="picks">
  <div class="label">🔥 Top 5 Trade Ideas · 20–30% Target · 1–3 Months</div>
  {% if top5 %}
  <div class="pick-grid">
    {% for s in top5 %}
    <div class="pick-card">
      <div style="display:flex;justify-content:space-between;align-items:start">
        <div class="pick-sym">{{ s.name }}</div>
        <span class="score-badge">{{ s.score }}/100</span>
      </div>
      <div class="pick-price">{{ s.currency }}{{ s.price }}</div>
      <div class="pick-mom {{ 'up' if s.change_1d >= 0 else 'dn' }}">
        1D {{ '+' if s.change_1d >= 0 else '' }}{{ s.change_1d }}% &nbsp;·&nbsp; 1M {{ '+' if s.mom_1m >= 0 else '' }}{{ s.mom_1m }}% &nbsp;·&nbsp; 3M {{ '+' if s.mom_3m >= 0 else '' }}{{ s.mom_3m }}%
      </div>
      {% if s.thesis %}<div class="pick-thesis">"{{ s.thesis }}"</div>{% endif %}
      <div class="pick-tgt">
        🎯 <strong>{{ s.currency }}{{ s.target }}</strong> target &nbsp;·&nbsp; 🛡 {{ s.currency }}{{ s.stop_loss }} stop<br>
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
  <div class="warming">⏳ Scanning 18 stocks + generating AI thesis... refresh in ~60s</div>
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
          <td>
            <form action="/tracker/exit/{{ s.id }}" method="post">
              <button type="submit" class="btn-exit">EXIT</button>
            </form>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% else %}
  <p style="color:var(--muted);font-size:12px;padding:14px;background:var(--surface);border:1px solid var(--border)">
    No stocks tracked yet. Hit <strong>+ TRACK</strong> on any Top 5 pick, or add manually below.
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

<!-- OTT & BOLLYWOOD -->
<section class="section" id="ott">
  <div class="label">🎬 OTT & Bollywood</div>
  <div class="ott-grid">
    {% for item in ott %}
    <div class="ott-card">
      <div class="src">{{ item.source }}</div>
      <h4>{% if item.link %}<a href="{{ item.link }}" target="_blank" style="color:var(--text)">{{ item.title }}</a>{% else %}{{ item.title }}{% endif %}</h4>
      {% if item.summary %}<p>{{ item.summary[:120] }}</p>{% endif %}
    </div>
    {% endfor %}
    {% if not ott %}<p style="color:var(--muted)">Entertainment feeds loading...</p>{% endif %}
  </div>
</section>

<!-- MONEY + PRODUCTIVITY -->
<section class="section" id="hacks">
  <div class="label">💰 Money Hack &amp; ⚡ Productivity</div>
  <div class="two">
    <div class="tip-card" id="money-hack">
      <h3>💰 {{ money_hack.title }}</h3>
      <p>{{ money_hack.body }}</p>
    </div>
    <div class="tip-card" style="border-left-color:var(--green)" id="productivity">
      <h3 style="color:var(--green)">⚡ Today's Rule</h3>
      <p>{{ productivity_tip }}</p>
    </div>
  </div>
</section>

</div>

<div class="footer">
  <strong style="color:var(--accent)">AKK TIMES</strong> · Personal Edition · Built with Claude Code<br>
  @askakshayfinance · AED 30K+ by mid-2026 🎯
</div>

<script>
// Auto-reload markets ticker every 5 min
setTimeout(() => window.location.reload(), 5 * 60 * 1000);
</script>
</body>
</html>"""

# ─────────────────────────────────────────────────────────────
# FLASK ROUTES
# ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    now = datetime.now(IST)
    markets = fetch_markets()
    news    = fetch_global_news()
    fpna    = get_fpna_tip()
    top5    = get_top5_picks()
    tracker = get_tracker_stocks()
    ott     = fetch_ott_bollywood()
    money   = get_money_hack()
    prod    = get_productivity_tip()
    jobs    = fetch_dubai_jobs()

    return render_template_string(TEMPLATE,
        date_str=now.strftime("%A, %B %d %Y"),
        updated_at=now.strftime("%H:%M IST"),
        markets=markets,
        news=news,
        fpna=fpna,
        top5=top5,
        tracker=tracker,
        ott=ott,
        money_hack=money,
        productivity_tip=prod,
        dubai_jobs=jobs,
    )

@app.route("/tracker/add", methods=["POST"])
def tracker_add():
    sym    = request.form.get("symbol", "").strip().upper()
    name   = request.form.get("name", sym)
    entry  = float(request.form.get("entry_price") or 0)
    target = float(request.form.get("target_price") or 0)
    stop   = float(request.form.get("stop_loss") or entry * 0.92)
    thesis = request.form.get("thesis", "")
    tf     = request.form.get("timeframe", "2-3 months")
    if sym and entry:
        add_to_tracker(sym, entry, target, stop, thesis, tf, name)
    return redirect("/#tracker")

@app.route("/tracker/exit/<int:stock_id>", methods=["POST"])
def tracker_exit(stock_id):
    exit_tracker(stock_id)
    return redirect("/#tracker")

@app.route("/tracker/obsidian", methods=["POST"])
def tracker_obsidian():
    stocks = get_tracker_stocks()
    sync_tracker_to_obsidian(stocks)
    return redirect("/#tracker")

@app.route("/tracker/history")
def tracker_history():
    with _db() as con:
        rows = con.execute(
            "SELECT * FROM stock_tracker WHERE status='exited' ORDER BY updated_at DESC LIMIT 50"
        ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/refresh")
def api_refresh():
    today = date.today().isoformat()
    with _db() as con:
        con.execute("DELETE FROM newspaper_stocks_picked WHERE pick_date=?", (today,))
    with _picks_lock:
        _picks_cache.pop(today, None)
    threading.Thread(target=_warm_picks_cache, daemon=True).start()
    return redirect("/")

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now(IST).isoformat()})

# ─────────────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────────────

def _startup():
    init_newspaper_db()
    threading.Thread(target=_warm_picks_cache, daemon=True).start()

if __name__ == "__main__":
    _startup()
    log.info(f"AKK Times → http://0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
else:
    # gunicorn entry
    _startup()
