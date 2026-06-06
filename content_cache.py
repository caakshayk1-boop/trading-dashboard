"""
content_cache.py — Shared daily content fetch layer.

Both newspaper.py (web) and daily_brief.py (Telegram) fetch markets, Dubai jobs,
and news independently — doubling API calls and risking inconsistent content.

This module fetches once and caches results in a JSON file for TTL_SECONDS.
Import and call get_cached_* functions instead of fetching directly.

Usage:
    from content_cache import get_cached_markets, get_cached_jobs, get_cached_quote, get_cached_news
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import feedparser
import requests
import yfinance as yf

log = logging.getLogger(__name__)

_DATA_DIR  = "/app/data" if os.path.isdir("/app/data") else os.path.dirname(__file__)
_CACHE_FILE = os.path.join(_DATA_DIR, "content_cache.json")

TTL_MARKETS = 900    # 15 min — prices change
TTL_JOBS    = 3600   # 1 hour — job listings stable
TTL_NEWS    = 1800   # 30 min — news refreshes
TTL_QUOTE   = 86400  # 24 hours — quote of the day

IST = timezone(timedelta(hours=5, minutes=30))

# ── Market tickers (shared definition) ──────────────────────────────────────

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

NEWS_FEEDS = [
    ("BBC Business",     "http://feeds.bbci.co.uk/news/business/rss.xml"),
    ("CNBC",             "https://www.cnbc.com/id/10001147/device/rss/rss.html"),
    ("Yahoo Finance",    "https://finance.yahoo.com/news/rssindex"),
    ("MarketWatch",      "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("Investing.com",    "https://www.investing.com/rss/news.rss"),
    ("Google Finance",   "https://news.google.com/rss/search?q=stock+market+finance+business&hl=en&gl=US&ceid=US:en"),
]

DUBAI_JOB_FEEDS = [
    ("Google Jobs", "https://news.google.com/rss/search?q=FP%26A+Finance+Manager+jobs+Dubai+hiring&hl=en&gl=AE&ceid=AE:en"),
]
DUBAI_JOB_KEYWORDS = ["fp&a", "financial planning", "financial analyst", "finance manager",
                       "budget", "forecasting", "controller", "treasury"]


# ── Cache I/O — dual-write: JSON file + Turso (survives Railway restarts) ─────

def _load_cache() -> dict:
    # Try Turso first (persists across redeploys), fallback to JSON file
    try:
        import db as _db
        con = _db.connect()
        con.execute("CREATE TABLE IF NOT EXISTS content_cache (key TEXT PRIMARY KEY, data TEXT, ts REAL)")
        row = con.execute("SELECT data, ts FROM content_cache WHERE key='main'").fetchone()
        con.close()
        if row and (time.time() - float(row[1])) < max(TTL_MARKETS, TTL_NEWS):
            return json.loads(row[0])
    except Exception:
        pass
    try:
        with open(_CACHE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(data: dict) -> None:
    # Save to both JSON file and Turso
    try:
        with open(_CACHE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log.warning(f"content_cache JSON write error: {e}")
    try:
        import db as _db
        con = _db.connect()
        con.execute("CREATE TABLE IF NOT EXISTS content_cache (key TEXT PRIMARY KEY, data TEXT, ts REAL)")
        con.execute("INSERT OR REPLACE INTO content_cache VALUES ('main',?,?)",
                    (json.dumps(data), time.time()))
        con.commit()
        _db.sync(con)
        con.close()
    except Exception as e:
        log.debug(f"content_cache Turso write: {e}")


def _is_fresh(cache: dict, key: str, ttl: int) -> bool:
    entry = cache.get(key, {})
    ts = entry.get("ts", 0)
    return (time.time() - ts) < ttl


# ── Fetchers ─────────────────────────────────────────────────────────────────

def _fetch_markets() -> list[dict]:
    """Returns list of {name, price, change_pct, up}."""
    out = []
    for name, ticker, prefix, fmt in MARKET_TICKERS:
        try:
            hist  = yf.Ticker(ticker).history(period="5d", interval="1d", auto_adjust=True)
            if hist.empty:
                raise ValueError("no data")
            price = float(hist["Close"].iloc[-1])
            prev  = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else price
            pct   = ((price - prev) / prev * 100) if prev else 0
            out.append({
                "name":       name,
                "price_raw":  price,
                "price":      f"{prefix}{price:{fmt}}",
                "change_pct": round(pct, 2),
                "up":         pct >= 0,
                "prefix":     prefix,
                "fmt":        fmt,
            })
        except Exception as e:
            log.warning(f"Markets {ticker}: {e}")
            out.append({"name": name, "price": "—", "change_pct": 0, "up": True,
                        "price_raw": 0, "prefix": "", "fmt": ".0f"})
    return out


_LI_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

_GULF_SEARCHES = [
    ("UAE",          "FP%26A+Manager+Finance+Controller",   "Dubai"),
    ("Saudi Arabia", "FP%26A+Finance+Manager+Controller",   "Saudi+Arabia"),
    ("Kuwait",       "Finance+Manager+FP%26A+Controller",   "Kuwait"),
    ("India",        "FP%26A+Manager+Finance+Controller",   "India"),
    ("Malaysia",     "FP%26A+Finance+Manager+Controller",   "Malaysia"),
]


def _fetch_linkedin_jobs(keywords: str, location: str, country: str, max_items: int = 3) -> list[dict]:
    """Scrape LinkedIn public job search (no login required)."""
    import re as _re
    url = (
        f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
        f"?keywords={keywords}&location={location}&start=0&count=10&f_TPR=r604800"
    )
    try:
        r = requests.get(url, headers=_LI_HEADERS, timeout=12)
        if r.status_code != 200:
            return []
        html = r.text
        titles    = _re.findall(r'class="base-search-card__title"[^>]*>\s*([^<]+?)\s*<', html)
        companies = _re.findall(r'class="base-search-card__subtitle">\s*<[^>]+>\s*([^<]+?)\s*<', html)
        locations = _re.findall(r'class="job-search-card__location"[^>]*>\s*([^<]+?)\s*<', html)
        links     = _re.findall(r'href="(https://www\.linkedin\.com/jobs/view/[^"?]+)', html)
        out = []
        for i in range(min(max_items, len(titles))):
            title   = titles[i].strip().replace("&amp;", "&")
            company = companies[i].strip().replace("&amp;", "&") if i < len(companies) else ""
            loc     = locations[i].strip() if i < len(locations) else country
            link    = links[i] if i < len(links) else ""
            display = f"{title} — {company}" if company else title
            out.append({"source": "LinkedIn", "title": display[:90], "link": link, "city": loc})
        return out
    except Exception as e:
        log.warning(f"LinkedIn jobs {country}: {e}")
        return []


def _fetch_jobs() -> list[dict]:
    """Live Gulf + India FP&A jobs scraped from LinkedIn public search."""
    jobs: list[dict] = []

    for country, keywords, location in _GULF_SEARCHES:
        results = _fetch_linkedin_jobs(keywords, location, country, max_items=3)
        jobs.extend(results)
        if results:
            log.info(f"LinkedIn jobs {country}: {len(results)} fetched")

    # Static curated fallback — only if scraping fully fails
    if len(jobs) < 3:
        log.warning("LinkedIn job scraping returned <3 results, using curated fallback")
        jobs += [
            {"source": "Apply", "title": "FP&A Manager — ADNOC Group",         "link": "https://careers.adnoc.ae",            "city": "Dubai"},
            {"source": "Apply", "title": "Senior Financial Analyst — Emirates", "link": "https://www.emiratesgroupcareers.com", "city": "Dubai"},
            {"source": "Apply", "title": "Finance Business Partner — MAF",      "link": "https://careers.majidalfuttaim.com",   "city": "Dubai"},
            {"source": "Apply", "title": "Group FP&A Analyst — DP World",       "link": "https://careers.dpworld.com",          "city": "Dubai"},
            {"source": "Apply", "title": "FP&A Lead — First Abu Dhabi Bank",    "link": "https://jobs.bankfab.com",             "city": "Dubai"},
        ]
    return jobs[:12]


def _fetch_news() -> list[dict]:
    """Returns list of {source, title, link, published}."""
    cutoff   = datetime.now(timezone.utc) - timedelta(hours=24)
    articles = []
    import re
    for source, url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:5]:
                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    try:
                        from datetime import datetime as _dt
                        published = _dt(*entry.published_parsed[:6], tzinfo=timezone.utc)
                    except Exception:
                        pass
                summary = re.sub(r"<[^>]+>", "", getattr(entry, "summary", ""))[:300]
                articles.append({
                    "source":    source,
                    "title":     entry.get("title", "")[:120],
                    "link":      entry.get("link", ""),
                    "summary":   summary,
                    "published": published.strftime("%b %d %H:%M") if published else "",
                    "recent":    published >= cutoff if published else True,
                })
        except Exception as e:
            log.warning(f"News feed {source}: {e}")
    articles.sort(key=lambda x: x["recent"], reverse=True)
    return articles[:18]


def _fetch_quote() -> str:
    try:
        r = requests.get("https://zenquotes.io/api/random", timeout=8)
        if r.status_code == 200:
            d = r.json()[0]
            return f'"{d["q"]}"\n— {d["a"]}'
    except Exception:
        pass
    return '"The secret of getting ahead is getting started."\n— Mark Twain'


# ── Public API ───────────────────────────────────────────────────────────────

def get_cached_markets() -> list[dict]:
    cache = _load_cache()
    if _is_fresh(cache, "markets", TTL_MARKETS):
        return cache["markets"]["data"]
    data = _fetch_markets()
    cache["markets"] = {"ts": time.time(), "data": data}
    _save_cache(cache)
    return data


def get_cached_jobs() -> list[dict]:
    cache = _load_cache()
    if _is_fresh(cache, "jobs", TTL_JOBS):
        return cache["jobs"]["data"]
    data = _fetch_jobs()
    cache["jobs"] = {"ts": time.time(), "data": data}
    _save_cache(cache)
    return data


def get_cached_news() -> list[dict]:
    cache = _load_cache()
    if _is_fresh(cache, "news", TTL_NEWS):
        return cache["news"]["data"]
    data = _fetch_news()
    cache["news"] = {"ts": time.time(), "data": data}
    _save_cache(cache)
    return data


def get_cached_quote() -> str:
    cache = _load_cache()
    if _is_fresh(cache, "quote", TTL_QUOTE):
        return cache["quote"]["data"]
    data = _fetch_quote()
    cache["quote"] = {"ts": time.time(), "data": data}
    _save_cache(cache)
    return data


def invalidate(key: Optional[str] = None) -> None:
    """Force-expire a cache key (or all if None)."""
    cache = _load_cache()
    if key:
        cache.pop(key, None)
    else:
        cache.clear()
    _save_cache(cache)
