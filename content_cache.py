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
import re
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
TTL_READS   = 21600  # 6h — long reads do not change by the half hour
TTL_QUOTE   = 86400  # 24 hours — quote of the day

# Several publishers answer a default feedparser/urllib agent with 404 or 403
# rather than a useful error. Lichess does the same thing (see LICHESS_UA in
# newspaper.py) and it cost a day to find there, so it is named once here.
_UA = "Mozilla/5.0 (compatible; DailySignal/1.0; +https://news.askakshay.com)"

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

# Named mastheads only. The Google News aggregator that used to sit at the
# bottom of this list is gone: it republishes whatever ranks, so the "source"
# on a card was "Google Finance" for an article written by someone else, and
# there was no way to tell a wire desk from a content farm.
#
# Every URL here was fetched and checked before being added. Ones that do NOT
# work, so nobody re-adds them: Reuters (404 — the public feed was withdrawn),
# Forbes /money (404), Equitymaster (403), Morningstar (302 redirect loop).
# Forbes /business RESOLVES but its top item was a Liam Neeson film review;
# it is a general-interest feed wearing a business label, so it is excluded
# rather than filtered — see FINANCE_TOKENS for why filtering alone is not
# enough to make an off-topic feed worth carrying.
NEWS_FEEDS = [
    # India
    ("Economic Times",   "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("ET Economy",       "https://economictimes.indiatimes.com/news/economy/rssfeeds/1373380680.cms"),
    ("Livemint",         "https://www.livemint.com/rss/markets"),
    ("Business Standard", "https://www.business-standard.com/rss/markets-106.rss"),
    ("Moneycontrol",     "https://www.moneycontrol.com/rss/marketreports.xml"),
    ("BusinessLine",     "https://www.thehindubusinessline.com/markets/feeder/default.rss"),
    # Global
    ("Bloomberg",        "https://feeds.bloomberg.com/markets/news.rss"),
    ("Financial Times",  "https://www.ft.com/rss/home"),
    ("WSJ Markets",      "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain"),
    ("BBC Business",     "http://feeds.bbci.co.uk/news/business/rss.xml"),
    ("CNBC",             "https://www.cnbc.com/id/10001147/device/rss/rss.html"),
    ("MarketWatch",      "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
]

# Longer analytical pieces rather than the wire. Same reliability bar, and the
# same relevance filter — opinion desks carry film reviews and language
# columns next to the money writing ("Gen ji, and the great phonological
# divide" was the top item on ET Opinion the day this was built).
# (source, url, category). The category drives BOTH the relevance gate applied
# to the feed and the mix at the end.
#
# This section used to be finance-only — seven money feeds behind one finance
# token gate — which made it a second markets section on a page that already
# has several. The ask was to read in order to get better, not only richer, so
# the money feeds are now one category of five and cannot take more than their
# share of the slots. Nothing about the finance path changed; it was joined.
SMART_READ_FEEDS = [
    # ── money ──
    ("Economic Times",    "https://economictimes.indiatimes.com/wealth/rssfeeds/837555174.cms", "money"),
    ("ET Opinion",        "https://economictimes.indiatimes.com/opinion/rssfeeds/897228639.cms", "money"),
    ("Livemint",          "https://www.livemint.com/rss/money", "money"),
    ("Livemint Opinion",  "https://www.livemint.com/rss/opinion", "money"),
    ("Business Standard", "https://www.business-standard.com/rss/opinion-105.rss", "money"),
    ("Zerodha",           "https://zerodha.com/z-connect/feed", "money"),
    ("The Economist",     "https://www.economist.com/finance-and-economics/rss.xml", "money"),
    # ── habits, discipline, getting better at things ──
    ("Farnam Street",     "https://fs.blog/feed/", "habits"),
    ("James Clear",       "https://jamesclear.com/feed", "habits"),
    ("Ness Labs",         "https://nesslabs.com/feed", "habits"),
    ("Scott Young",       "https://www.scotthyoung.com/blog/feed/", "habits"),
    # ── health and longevity ──
    ("Peter Attia",       "https://peterattiamd.com/feed/", "health"),
    ("Harvard Health",    "https://www.health.harvard.edu/blog/feed", "health"),
    ("NYT Well",          "https://rss.nytimes.com/services/xml/rss/nyt/Well.xml", "health"),
    # ── mind, relationships, how to be a better person ──
    ("Psyche",            "https://psyche.co/feed", "mind"),
    ("Greater Good",      "https://greatergood.berkeley.edu/feeds/entries.rss", "mind"),
    ("Mark Manson",       "https://markmanson.net/feed", "mind"),
    # ── thinking, philosophy, life lessons ──
    ("Aeon",              "https://aeon.co/feed.rss", "ideas"),
    ("Paul Graham",       "http://www.aaronsw.com/2002/feeds/pgessays.rss", "ideas"),
    ("The Marginalian",   "https://www.themarginalian.org/feed/", "ideas"),
]

# One token per category, same blunt-instrument principle as FINANCE_TOKENS: the
# job is to keep a product launch off a health card, not to grade the writing.
CATEGORY_TOKENS = {
    "habits": (
        "habit", "routine", "discipline", "focus", "attention", "procrastinat",
        "productiv", "consistency", "practice", "learn", "learning", "skill",
        "mastery", "improve", "better", "goal", "system", "decision", "thinking",
        "mental model", "clarity", "deep work", "distraction", "willpower",
        "motivation", "identity", "compound", "small step", "process",
    ),
    "health": (
        "health", "sleep", "exercise", "training", "strength", "cardio", "vo2",
        "nutrition", "diet", "protein", "fasting", "longevity", "lifespan",
        "muscle", "metabolic", "cholesterol", "blood", "heart", "brain",
        "stress", "cortisol", "walking", "steps", "weight", "fitness",
        "doctor", "medicine", "disease", "risk", "prevent", "recovery",
    ),
    "mind": (
        "emotion", "anxiety", "anger", "grief", "loneliness", "relationship",
        "friendship", "marriage", "parent", "child", "empathy", "kindness",
        "compassion", "gratitude", "generous", "help", "helping", "trust",
        "forgive", "conflict", "boundaries", "self", "identity", "therapy",
        "psychology", "mental health", "happiness", "meaning", "purpose",
        "regret", "shame", "confidence", "listen", "conversation",
    ),
    # Narrower than it first was. "society", "culture", "technology", "future"
    # and "life" are broad enough to match essentially any general-interest
    # essay, and they did: an Aeon piece on the sexual revolution qualified for
    # a section whose brief is "read in order to get better". The tokens left
    # here select for philosophy, thinking and craft rather than for commentary.
    "ideas": (
        "philosoph", "ethic", "moral", "wisdom", "virtue", "stoic", "buddhis",
        "meaning", "purpose", "character", "integrity", "humility",
        "argument", "reason", "logic", "fallacy", "bias", "judgment",
        "decision", "mental model", "first principles", "curiosity",
        "knowledge", "understand", "learn", "craft", "mastery", "discipline",
        "attention", "time", "mortality", "regret", "legacy",
    ),
}

# ── explicit / adult / graphic content gate ──────────────────────────────────
#
# Applied to EVERY ingestion path on the site — Smart Reads, podcasts, the news
# wire — not just to the section it was first written for. Broadening the
# content sources is what makes this necessary: a finance-only feed list could
# not surface this material, and a list that includes general-interest essays,
# health publishers and twenty YouTube channels certainly can.
#
# It fails CLOSED and it is absolute: a match is dropped, never softened,
# never summarised, never "included with a warning". There is no editorial
# judgement to make here and no threshold to tune.
#
# Substring matching on purpose, unlike the token gates which use word
# boundaries. A word-boundary match on "sex" would pass "sexting"; here the
# false-positive cost (losing an article about sexual harassment law) is
# acceptable and the false-negative cost is not.
EXPLICIT_BLOCK = (
    # adult industry / sexual content
    "porn", "pornhub", "onlyfans", "nsfw", "xxx", "erotic", "erotica",
    "sexual", "sex life", "sex tape", "sexting", "nude", "nudity", "naked",
    "orgasm", "masturbat", "fetish", "bdsm", "kink", "escort service",
    "brothel", "strip club", "camgirl", "sex work", "prostitut", "aphrodisiac",
    "libido", "viagra", "penis", "vagina", "genital", "incest", "hentai",
    # graphic violence / gore
    "gore", "beheading", "decapitat", "mutilat", "dismember", "snuff",
    "graphic footage", "graphic video", "disturbing footage",
    # self-harm, handled with the same absoluteness
    "suicide method", "how to kill yourself", "self-harm tutorial",
    "pro-ana", "thinspo",
    # illegal drugs how-to, gambling and scam bait
    "how to make meth", "buy cocaine", "dark web market", "silk road market",
    "betting tips", "matka", "satta", "casino bonus", "free spins",
    "guaranteed profit", "double your money", "get rich quick",
    # child-safety terms, absolute
    "child abuse", "csam", "underage",
)


# Short words that cannot be matched as substrings without absurd collisions —
# a bare "sex" hits Essex, unisex, sextant and sexagenarian; "rape" hits grape
# and drape. Matched on word boundaries instead, which still catches the case
# the substring list missed: "Sex scandal wipes 20% off the share price" clears
# every topical finance gate and is not going on this page.
_EXPLICIT_WORDS = re.compile(
    r"\b(sex|sexed|nude|nudes|rape|raped|rapist|incest|molest|molested|"
    r"orgy|orgies|slut|whore|obscene|lewd|lascivious)\b", re.I)


def is_explicit(*texts) -> bool:
    """True if any supplied text trips the explicit gate. Fails closed.

    One function, used by every content path, so a new ingestion point cannot
    quietly skip the check by forgetting to copy a blocklist.

    Two tiers: unambiguous substrings, plus short words matched on word
    boundaries. Deliberately over-blocks — losing a piece about sexual
    harassment law is an acceptable cost and passing the alternative is not.
    """
    blob = " ".join(str(t or "") for t in texts).lower()
    if any(b in blob for b in EXPLICIT_BLOCK):
        return True
    return bool(_EXPLICIT_WORDS.search(blob))


# Off-brief for a self-improvement section regardless of how well written the
# piece is. Deliberately short and about SUBJECT, not viewpoint — this is not a
# quality filter, it is a "does this belong under Smart Reads" filter, and the
# section sits on a personal site under a real name.
#
# Distinct from EXPLICIT_BLOCK above: these are merely off-topic here and could
# reasonably appear elsewhere on a site like this. Explicit material could not.
SMART_READ_BLOCK = (
    "dating app", "celebrity", "box office", "royal family",
    "horoscope", "astrolog", "weight loss pill", "miracle cure",
)

# How many slots each category may take. Money is capped at three of nine
# because on raw recency it would take all nine — those feeds publish dozens of
# items a day and the reflective ones publish weekly.
CATEGORY_SLOTS = {"money": 3, "habits": 2, "health": 2, "mind": 1, "ideas": 1}

# One word from this list has to appear in the headline or the summary. It is a
# blunt instrument and deliberately so — the job is to keep a movie review off
# a finance page, not to grade the writing.
FINANCE_TOKENS = (
    "market", "stock", "equity", "share", "index", "nifty", "sensex", "bse", "nse",
    "fund", "sip", "mutual", "etf", "portfolio", "investor", "investment", "invest",
    "rbi", "fed", "sebi", "inflation", "rate", "yield", "bond", "debt", "credit",
    "rupee", "dollar", "currency", "forex", "gold", "silver", "crude", "oil",
    "ipo", "earnings", "profit", "revenue", "margin", "valuation", "dividend",
    "tax", "gst", "budget", "gdp", "economy", "economic", "fiscal", "trade",
    "bank", "lender", "nbfc", "insurance", "pension", "retirement", "wealth",
    "crypto", "bitcoin", "startup", "funding", "ipo", "merger", "acquisition",
    "commodity", "futures", "options", "derivative", "capital", "finance",
)


def _finance_hits(title: str, summary: str) -> int:
    """How many DISTINCT finance tokens appear, on word boundaries.

    Substring matching is what let "Golden Rule on the silver screen" through
    as a finance story — "Golden" contains "gold". `\\b` fixes that class of
    false positive outright.
    """
    import re as _re
    blob = f"{title} {summary}".lower()
    return sum(1 for t in set(FINANCE_TOKENS)
               if _re.search(rf"\b{_re.escape(t)}s?\b", blob))


def _is_finance(title: str, summary: str, need: int = 1) -> bool:
    """`need` is the bar. Wire feeds are already topical, so one token is
    enough there. Opinion desks are not: they run film and language columns
    beside the money writing, and a single incidental "market" is exactly how
    a review of The Odyssey landed in a finance section. Reads need two."""
    return _finance_hits(title, summary) >= need

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
    # Gulf — primary targets
    ("🇦🇪 UAE",           "FP%26A+Manager+Finance+Controller",      "Dubai"),
    ("🇸🇦 Saudi Arabia",  "FP%26A+Finance+Manager+Controller",      "Saudi+Arabia"),
    ("🇧🇭 Bahrain",       "Finance+Manager+FP%26A+Controller",      "Bahrain"),
    ("🇰🇼 Kuwait",        "Finance+Manager+FP%26A+Controller",      "Kuwait"),
    # Asia — strong markets for Indian CA/FP&A
    ("🇲🇾 Malaysia",      "FP%26A+Finance+Manager+Controller",      "Malaysia"),
    ("🇮🇳 India",         "FP%26A+Manager+Financial+Planning",      "India"),
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
        results = _fetch_linkedin_jobs(keywords, location, country, max_items=2)
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
            feed = feedparser.parse(url, agent=_UA)
            for entry in feed.entries[:5]:
                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    try:
                        from datetime import datetime as _dt
                        published = _dt(*entry.published_parsed[:6], tzinfo=timezone.utc)
                    except Exception:
                        pass
                summary = re.sub(r"<[^>]+>", "", getattr(entry, "summary", "")).strip()[:400]
                title = entry.get("title", "")[:140]
                # Explicit gate first, and separately from the topical one. A
                # finance token is not a safety check: "sex scandal wipes 20% off
                # the share price" clears _is_finance comfortably.
                if is_explicit(title, summary):
                    continue
                # An off-topic item on a finance page costs more than a thin
                # section does. Wire feeds from general desks carry film,
                # sport and politics under a "business" label.
                if not _is_finance(title, summary):
                    continue
                articles.append({
                    "source":    source,
                    "title":     title,
                    "link":      entry.get("link", ""),
                    "summary":   summary,
                    "published": published.strftime("%b %d %H:%M") if published else "",
                    "recent":    published >= cutoff if published else True,
                })
        except Exception as e:
            log.warning(f"News feed {source}: {e}")

    # One item per source before any source gets a second, so a feed carrying
    # 50 entries cannot fill the page while a feed carrying 4 never appears.
    # Twelve sources ordered by recency alone put Economic Times in nearly
    # every slot.
    articles.sort(key=lambda x: x["recent"], reverse=True)
    by_src: dict = {}
    for a in articles:
        by_src.setdefault(a["source"], []).append(a)
    out, rnd = [], 0
    while len(out) < 18:
        wave = [v[rnd] for v in by_src.values() if len(v) > rnd]
        if not wave:
            break
        out.extend(wave[:18 - len(out)])
        rnd += 1
    return out


def _is_relevant(cat: str, title: str, summary: str) -> bool:
    """Does this entry belong to the category its feed was listed under?

    Money keeps its own two-token bar — those are general opinion desks that run
    film and language columns beside the money writing. The other four are
    single-subject publications, so one token is the right bar there: raising it
    to two threw away good pieces for using a synonym this list happens not to
    carry, which is a worse failure than an occasional off-topic card.
    """
    if is_explicit(title, summary):
        return False
    blob = f"{title} {summary}".lower()
    if any(b in blob for b in SMART_READ_BLOCK):
        return False
    if cat == "money":
        return _is_finance(title, summary, need=2)
    toks = CATEGORY_TOKENS.get(cat)
    if not toks:
        return True
    return any(t in blob for t in toks)


def _fetch_smart_reads() -> list[dict]:
    """Longer analytical pieces. Same shape as news, plus `date` and `cat`."""
    import re
    reads = []
    for source, url, cat in SMART_READ_FEEDS:
        try:
            feed = feedparser.parse(url, agent=_UA)
            for entry in feed.entries[:6]:
                summary = re.sub(r"<[^>]+>", "", getattr(entry, "summary", "")).strip()
                title = entry.get("title", "")[:140]
                # A card is a headline AND a summary. Without real summary text
                # it is just a link with a border around it, so it is dropped.
                if len(summary) < 80 or not _is_relevant(cat, title, summary):
                    continue
                published = None
                if getattr(entry, "published_parsed", None):
                    try:
                        from datetime import datetime as _dt
                        published = _dt(*entry.published_parsed[:6], tzinfo=timezone.utc)
                    except Exception:
                        pass
                reads.append({
                    "source":  source,
                    "cat":     cat,
                    "title":   title,
                    "link":    entry.get("link", ""),
                    "summary": summary[:320],
                    "date":    published.strftime("%b %d, %Y") if published else "",
                    "_sort":   published.isoformat() if published else "",
                })
        except Exception as e:
            log.warning(f"Smart read feed {source}: {e}")

    reads.sort(key=lambda x: x["_sort"], reverse=True)

    # Fill each category up to its own quota, newest first, one item per source
    # within a category so a single prolific blog cannot own its slice.
    #
    # Quota FIRST, recency second. Sorting nine slots purely by date is how this
    # section became finance-only in practice even before it was finance-only by
    # configuration: ET and Mint publish continuously, so every reflective piece
    # was older than every money piece and never made the cut.
    out: list[dict] = []
    for cat, quota in CATEGORY_SLOTS.items():
        pool = [r for r in reads if r["cat"] == cat]
        seen_src: set = set()
        picked = []
        for r in pool:                          # already newest-first
            if r["source"] in seen_src:
                continue
            seen_src.add(r["source"])
            picked.append(r)
            if len(picked) >= quota:
                break
        # A category short of its quota gives its remaining slots up rather than
        # padding with a second item from one source.
        out.extend(picked)

    # Backfill when a category came up short — a dead feed must cost the section
    # a card, not leave a hole in the grid.
    #
    # Non-money first, money only as a last resort. Backfilling purely by
    # recency handed the spare slots straight back to money (4 of 9 on the first
    # run) because those feeds publish continuously, which quietly undid the
    # quota it had just respected.
    if len(out) < 9:
        have = {r["link"] for r in out}
        for pref in (lambda r: r["cat"] != "money", lambda r: True):
            for r in reads:
                if len(out) >= 9:
                    break
                if r["link"] in have or not pref(r):
                    continue
                out.append(r)
                have.add(r["link"])
            if len(out) >= 9:
                break

    out.sort(key=lambda x: x["_sort"], reverse=True)
    return out[:9]


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


def get_cached_smart_reads() -> list[dict]:
    cache = _load_cache()
    if _is_fresh(cache, "smart_reads", TTL_READS):
        return cache["smart_reads"]["data"]
    data = _fetch_smart_reads()
    cache["smart_reads"] = {"ts": time.time(), "data": data}
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
