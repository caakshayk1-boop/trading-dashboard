#!/usr/bin/env python3
"""
daily_brief.py — 6 AM IST personal morning brief for Akshay Kothari
Sends to Telegram: Dubai jobs · markets · habit · productivity · learning · quote
Stores each brief in signals.db (daily_briefs table) for history.
Pulls Obsidian habits via GitHub API if GITHUB_TOKEN is set.
"""
from __future__ import annotations

import os, sys, json, logging, sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta, date
from typing import Optional
import requests
import yfinance as yf
import feedparser

sys.path.insert(0, os.path.dirname(__file__))
from telegram_bot import _post

log = logging.getLogger(__name__)

IST      = timezone(timedelta(hours=5, minutes=30))
DB_PATH  = os.path.join(os.path.dirname(__file__), "signals.db")
OBS_REPO = os.environ.get("OBSIDIAN_GITHUB_REPO", "caakshayk1-boop/obsidian-brain")


# ────────────────────────────────────────────────────────────────────────────
# MARKET TICKERS
# ────────────────────────────────────────────────────────────────────────────
TICKERS = [
    ("Gold",     "GC=F",    "$",  ".0f"),
    ("Silver",   "SI=F",    "$",  ".2f"),
    ("USD/INR",  "USDINR=X","₹",  ".2f"),
    ("S&P 500",  "^GSPC",   "",   ".0f"),
    ("Nifty 50", "^NSEI",   "",   ".0f"),
]


# ────────────────────────────────────────────────────────────────────────────
# HABITS  (fallback if Obsidian pull fails)
# ────────────────────────────────────────────────────────────────────────────
HABITS = [
    ("Morning routine complete", "5:45 AM",  "Anchor that structures the day"),
    ("No shisha",               "All day",   "T-suppressor + brain fog drain"),
    ("Workout / mobility",      "6:15 AM",   "BDNF released = sharp for 3–4 hrs"),
    ("8h sleep target",         "10:30 PM",  "80% of T made during deep sleep"),
    ("Protein 130g",            "All day",   "Muscle + mood + cognitive function"),
    ("Deep work block",         "7:45 AM",   "Identity as a high-performer"),
    ("No social after 9 PM",   "9:00 PM",   "Protects sleep architecture"),
    ("Gratitude / journaling",  "AM+PM",     "Prefrontal activation"),
    ("Screens off 10:30 PM",   "10:30 PM",  "Melatonin suppression ends"),
    ("Cold exposure (face)",    "5:50 AM",   "Free cortisol spike, no caffeine dependency"),
]


# ────────────────────────────────────────────────────────────────────────────
# PRODUCTIVITY HACKS  (50 items, rotates daily)
# ────────────────────────────────────────────────────────────────────────────
PRODUCTIVITY = [
    "Eat the frog: do the hardest task before checking any messages.",
    "2-minute rule: if it takes <2 min, do it now. Don't queue it.",
    "Time-block your calendar. Unblocked time = wasted time.",
    "Single-tasking beats multitasking. IQ drops 15 pts when task-switching.",
    "Write tomorrow's top 3 tasks tonight. Wake up with a plan, not a question.",
    "90-min deep work sprints. No phone. Door closed. Results compound.",
    "Say no to protect your yes. Every new commitment costs another.",
    "Clear your inbox to zero before 9 AM. Empty inbox = no mental overhead.",
    "Use Parkinson's Law: set shorter deadlines. Work expands to fill time given.",
    "Start meetings 5 min early. Late starts signal other people's time doesn't matter.",
    "Weekly review: 15 min every Sunday. What worked, what didn't, what's next week's #1.",
    "Done > perfect. Ship at 80%, iterate based on real feedback.",
    "Batch similar tasks. Answer all messages in one sitting, not throughout the day.",
    "Phone in another room during deep work. Physical distance reduces urge by 60%.",
    "Keep a 'waiting for' list. Never drop a ball because you forgot you delegated it.",
    "Use templates for recurring work. Never write the same email twice.",
    "End every meeting with: who does what by when. No action = no meeting needed.",
    "Morning exercise before work. BDNF released = sharper for 3–4 hours.",
    "Read 10 pages of a non-fiction book daily. 10 pages × 365 = 12 books/year.",
    "Automate recurring decisions. Same breakfast, same morning routine, same gym time.",
    "Review your goals weekly, not just annually. Quarterly check-ins are too slow.",
    "Keep a swipe file of great writing + ideas. Reference it before every creation.",
    "Respond to Slack/WhatsApp at set times. Real-time response is a myth.",
    "Track your energy, not just your time. Hard work when energy is highest.",
    "Remove apps from your phone home screen. Friction kills bad habits automatically.",
    "Define 'done' before starting. Vague tasks never finish.",
    "Learn keyboard shortcuts for your top tools. 5 min saved × 365 = 30 hours/year.",
    "Body double technique: work alongside someone (even on a call). Output increases.",
    "End your day at the same time every day. A hard stop protects your recovery.",
    "Capture everything immediately. If it's not written down, it doesn't exist.",
    "Set a 'shutdown complete' ritual. Signals your brain to stop ruminating.",
    "Create a not-to-do list. What you stop matters as much as what you start.",
    "Build systems not goals. Goals are outcomes; systems produce them.",
    "Under-promise, over-deliver. Every time. Build a reputation.",
    "Take a 10-min walk after lunch. Glucose spike → walk → sharper afternoon.",
    "Weekly financial review: 10 min. Net worth, cash flow, investments. Numbers don't lie.",
    "Write thoughts before reacting in difficult conversations. Words land better.",
    "10-10-10 rule: how will this feel in 10 min, 10 months, 10 years?",
    "Sleep is the highest-leverage productivity tool. 7 hours minimum, non-negotiable.",
    "Friction audit: what makes your best habits hard? Remove friction systematically.",
    "Read about the industry you want to enter. 30 min/day = expert in 6 months.",
    "Delegate outcomes, not tasks. Tell people what done looks like.",
    "Review your top 5 priorities every Monday. Are you working on what matters?",
    "Celebrate small wins. Dopamine from small completions fuels bigger work.",
    "Reduce optionality before starting. Too many options = decision fatigue = no action.",
    "Own your mornings. The first hour sets the tone for the next 8.",
    "Schedule thinking time. Block 1 hour/week to think about the big picture.",
    "Your environment is your autopilot. Design it so the right choice is the easy choice.",
    "Measure what you want to improve. Unmeasured goals stay wishes.",
    "Keep an idea journal. Your best insights won't come while sitting at a desk.",
]


# ────────────────────────────────────────────────────────────────────────────
# LEARNING TIPS  (finance · AI · career — rotates daily)
# ────────────────────────────────────────────────────────────────────────────
LEARNING = [
    ("INDEX-MATCH beats VLOOKUP",
     "Doesn't scan every column → 10× faster on 100K+ rows."),
    ("Contribution margin",
     "Revenue − Variable Costs. Everything above fixed costs is profit."),
    ("IRR vs NPV",
     "IRR = % return. NPV = absolute value created. Use both together."),
    ("Rule of 72",
     "72 ÷ annual return rate = years to double money. 8% = 9 years."),
    ("Working capital",
     "Current Assets − Current Liabilities. Negative = danger signal."),
    ("EV/EBITDA vs P/E",
     "EV/EBITDA is capital-structure neutral. P/E is equity-only. Use EV for M&A."),
    ("DuPont analysis",
     "ROE = Net Margin × Asset Turnover × Leverage. Diagnoses what drives returns."),
    ("Waterfall charts",
     "Stacked bar → set first/last bars invisible. Shows variance drivers cleanly."),
    ("Scenario analysis",
     "Best/Base/Worst cases. Sensitivity on 2–3 key drivers only."),
    ("WACC",
     "Weighted avg of cost of debt (after-tax) + cost of equity (CAPM). Used in DCF."),
    ("CAPM formula",
     "Expected Return = Risk-free rate + Beta × (Market Return − Risk-free rate)."),
    ("Cash conversion cycle",
     "DIO + DSO − DPO. Lower = better. Amazon runs negative CCC."),
    ("yfinance basics",
     "yf.download('AAPL', period='1y') → instant OHLCV. No API key needed."),
    ("Pandas pivot_table",
     "df.pivot_table(values='Revenue', index='Region', columns='Quarter', aggfunc='sum')"),
    ("Power Query M",
     "Table.SelectRows(Source, each [Date] >= #date(2024,1,1)) → dynamic date filter."),
    ("Break-even analysis",
     "Fixed Costs ÷ (Price − Variable Cost per unit) = break-even units."),
    ("Operating leverage",
     "High fixed costs → small revenue change → big profit change. Double-edged."),
    ("Free cash flow",
     "Net Income + D&A − CapEx − ΔWorking Capital. Not the same as EBITDA."),
    ("Terminal value in DCF",
     "Gordon Growth: FCF × (1+g) ÷ (WACC−g). Drives 70%+ of total DCF value."),
    ("Cohort analysis",
     "Group users by acquisition date. Shows retention curves, LTV, payback periods."),
    ("Three-statement model",
     "Net Income flows to retained earnings. Cash Flow starts from Net Income."),
    ("Budget vs Actuals reporting",
     "Never report just variance. Always: variance + driver + action."),
    ("Storytelling with data",
     "One chart = one insight. Title charts with the conclusion, not the metric name."),
    ("Monte Carlo in FP&A",
     "10,000 simulations with randomized assumptions. Shows probability of outcomes."),
    ("Excel XLOOKUP",
     "=XLOOKUP(value, lookup_array, return_array, [not found]) — VLOOKUP killer."),
    ("Claude API for finance",
     "claude-sonnet-4-6 + tool_use automates variance commentary. 50ms, $0.003/call."),
    ("Prompt caching (Anthropic)",
     "Cache system prompts >1024 tokens. Up to 90% cost reduction on repeated calls."),
    ("Power BI DAX CALCULATE",
     "CALCULATE(SUM(Sales[Revenue]), Sales[Region]=\"Dubai\") → context-aware aggregation."),
    ("Debt covenants",
     "Build Net Debt/EBITDA + Interest Coverage tests into every model. Lenders watch these."),
    ("IFRS 16 impact",
     "Leases now on balance sheet. Boosts EBITDA (rent → D&A + interest)."),
    ("Dubai financial landscape",
     "DIFC + ADGM are common employer zones. Many MNC regional HQs there."),
    ("LinkedIn algorithm",
     "Engagement in first 60 min = viral reach. Comment-bait beats link posts."),
    ("Networking formula",
     "Give value before asking. Comment → DM → call. Converse, don't pitch."),
    ("Cover letter structure",
     "Para 1: Why this company. Para 2: What you bring (numbers). Para 3: One ask."),
    ("Salary negotiation",
     "Anchor first. Silence after offer is a tool. Always ask: 'Is this flexible?'"),
    ("CA in UAE",
     "ICAI CA is widely recognized. FP&A experience matters more than the chartered body."),
    ("FP&A interview prep",
     "Know your models, know variance drivers, tell the story behind the numbers."),
    ("EBITDA adjustments",
     "Always define adjusted vs reported EBITDA. Acquirers look at adjusted; lenders look at both."),
    ("Currency hedging basics",
     "Forward contracts lock in exchange rates. Options give the right, not obligation."),
    ("Scenario sensitivity table",
     "Two-variable data table in Excel. Rows = revenue growth, cols = margin. Fast DCF stress-test."),
]


# ────────────────────────────────────────────────────────────────────────────
# HELPERS
# ────────────────────────────────────────────────────────────────────────────

def _rotate(items: list, seed: date = None):
    d = seed or date.today()
    return items[d.toordinal() % len(items)]


def _get_markets() -> str:
    lines = []
    for name, ticker, prefix, fmt in TICKERS:
        try:
            info  = yf.Ticker(ticker).fast_info
            price = info.last_price
            prev  = info.previous_close
            pct   = ((price - prev) / prev * 100) if prev else 0
            arrow = "↑" if pct > 0.05 else ("↓" if pct < -0.05 else "→")
            val   = f"{prefix}{price:{fmt}}"
            lines.append(f"`{name:<10}` {val:<12} {arrow} {pct:+.1f}%")
        except Exception:
            lines.append(f"`{name:<10}` —")
    return "\n".join(lines)


def _get_jobs() -> str:
    """Fetch Dubai FP&A/Finance jobs posted in last 24h via Indeed RSS."""
    jobs = []
    try:
        url = (
            "https://www.indeed.com/rss"
            "?q=FP%26A+Finance+Manager+CFO+Controller"
            "&l=Dubai"
            "&sort=date&fromage=1&limit=6"
        )
        feed = feedparser.parse(url)
        for entry in feed.entries[:5]:
            title = entry.get("title", "").strip()
            link  = entry.get("link", "").strip()
            # Remove " - Company - Location" suffix if present
            title = title.split(" - ")[0][:70]
            if title and link:
                jobs.append((title, link))
    except Exception as e:
        log.warning(f"jobs RSS fetch failed: {e}")

    if not jobs:
        return (
            "• Could not fetch live listings\n"
            "• [Search LinkedIn now](https://www.linkedin.com/jobs/search/?keywords=FP%26A&location=Dubai&f_TPR=r86400)\n"
            "• [Search Indeed now](https://www.indeed.com/jobs?q=fp%26a+finance&l=Dubai&sort=date&fromage=1)"
        )

    lines = [f"• [{t}]({l})" for t, l in jobs]
    lines.append("[→ See all on Indeed](https://www.indeed.com/jobs?q=fp%26a+finance&l=Dubai&sort=date&fromage=1)")
    return "\n".join(lines)


def _get_global_headline() -> Optional[str]:
    """One Reuters business headline."""
    try:
        feed = feedparser.parse("https://feeds.reuters.com/reuters/businessNews")
        if feed.entries:
            return feed.entries[0].get("title", "").strip()
    except Exception:
        pass
    return None


def _get_quote() -> str:
    try:
        r = requests.get("https://zenquotes.io/api/random", timeout=8)
        if r.status_code == 200:
            d = r.json()[0]
            return f'"{d["q"]}"\n— {d["a"]}'
    except Exception:
        pass
    return '"The secret of getting ahead is getting started."\n— Mark Twain'


def _save_to_db(content: str):
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute("""
            CREATE TABLE IF NOT EXISTS daily_briefs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                date       TEXT NOT NULL UNIQUE,
                content    TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        today = date.today().isoformat()
        con.execute(
            "INSERT OR REPLACE INTO daily_briefs (date, content) VALUES (?, ?)",
            (today, content)
        )
        con.commit()
        con.close()
    except Exception as e:
        log.warning(f"daily_brief DB save failed: {e}")


def _push_to_gist(content: str, brief_date: str):
    """Push brief history to GitHub Gist for mobile PWA access."""
    token   = os.environ.get("GITHUB_TOKEN", "")
    gist_id = os.environ.get("BRIEFS_GIST_ID", "")
    if not token:
        log.warning("daily_brief: GITHUB_TOKEN not set — skipping Gist push")
        return

    gh_headers = {
        "Authorization": f"token {token}",
        "Accept":        "application/vnd.github.v3+json",
        "User-Agent":    "akk-daily-brief/1.0",
    }

    # Load existing briefs from Gist
    briefs = []
    if gist_id:
        try:
            r = requests.get(f"https://api.github.com/gists/{gist_id}", headers=gh_headers, timeout=10)
            if r.status_code == 200:
                raw = r.json().get("files", {}).get("briefs.json", {}).get("content", "[]")
                existing = json.loads(raw)
                if isinstance(existing, list):
                    briefs = existing
        except Exception as e:
            log.warning(f"daily_brief: Gist read failed: {e}")

    # Upsert today, keep last 30
    briefs = [b for b in briefs if b.get("date") != brief_date]
    briefs.insert(0, {
        "date":       brief_date,
        "text":       content,
        "created_at": datetime.now(IST).isoformat(),
    })
    briefs = briefs[:30]
    payload = json.dumps(briefs, ensure_ascii=False, indent=2)

    if gist_id:
        r = requests.patch(
            f"https://api.github.com/gists/{gist_id}",
            json={"files": {"briefs.json": {"content": payload}}},
            headers=gh_headers, timeout=10,
        )
        if r.status_code == 200:
            log.info("daily_brief: Gist updated ✓")
        else:
            log.warning(f"daily_brief: Gist update failed {r.status_code}")
    else:
        r = requests.post(
            "https://api.github.com/gists",
            json={
                "description": "Akshay Daily Brief — Morning Update Log",
                "public":      False,
                "files":       {"briefs.json": {"content": payload}},
            },
            headers=gh_headers, timeout=10,
        )
        if r.status_code == 201:
            new_id = r.json().get("id", "")
            log.info(f"daily_brief: Gist created — set BRIEFS_GIST_ID={new_id} on Railway")
            _post(f"📌 Set env var on Railway:\nBRIEFS\\_GIST\\_ID=`{new_id}`")
        else:
            log.warning(f"daily_brief: Gist create failed {r.status_code}")


# ────────────────────────────────────────────────────────────────────────────
# BUILD & SEND
# ────────────────────────────────────────────────────────────────────────────

def build_brief() -> str:
    today    = date.today()
    now      = datetime.now(IST)
    weekday  = now.strftime("%A")
    datestr  = now.strftime("%d %B %Y")

    markets  = _get_markets()
    jobs     = _get_jobs()
    quote    = _get_quote()

    habit_name, habit_time, habit_why = _rotate(HABITS, today)
    hack   = _rotate(PRODUCTIVITY, today)
    topic, body = _rotate(LEARNING, today)

    headline = _get_global_headline()
    global_note = f"\n🌍 _{headline}_" if headline else ""

    brief = f"""🌅 *GOOD MORNING, AKSHAY*
{weekday} · {datestr} · 6 AM IST

━━━━━━━━━━━━━━━━━━━
🇦🇪 *DUBAI MOVE*
━━━━━━━━━━━━━━━━━━━
Target: *AED 30K/month* · Mid-2026
Weekly: 5 apps · 3 recruiter connects

*New jobs (last 24h):*
{jobs}

━━━━━━━━━━━━━━━━━━━
📊 *MARKETS*
━━━━━━━━━━━━━━━━━━━
{markets}{global_note}

━━━━━━━━━━━━━━━━━━━
✅ *HABIT FOCUS*
━━━━━━━━━━━━━━━━━━━
*{habit_name}* · {habit_time}
↳ _{habit_why}_

━━━━━━━━━━━━━━━━━━━
⚡ *PRODUCTIVITY*
━━━━━━━━━━━━━━━━━━━
{hack}

━━━━━━━━━━━━━━━━━━━
🧠 *LEARN TODAY*
━━━━━━━━━━━━━━━━━━━
*{topic}*
{body}

━━━━━━━━━━━━━━━━━━━
💬 *QUOTE*
━━━━━━━━━━━━━━━━━━━
{quote}"""

    return brief


def send_brief():
    log.info("daily_brief: building...")
    brief    = build_brief()
    today    = date.today().isoformat()
    _save_to_db(brief)
    _post(brief)
    try:
        _push_to_gist(brief, today)
    except Exception as e:
        log.warning(f"daily_brief: Gist push failed (non-fatal): {e}")
    log.info("daily_brief: sent ✓")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    send_brief()
