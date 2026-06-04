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
from content_cache import get_cached_markets, get_cached_jobs, get_cached_quote

sys.path.insert(0, os.path.dirname(__file__))
from telegram_bot import _post
import db

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
OBS_REPO = os.environ.get("OBSIDIAN_GITHUB_REPO", "caakshayk1-boop/obsidian-brain")


LICHESS_USER = "AKK_010"   # public Lichess username — no token needed


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
# CHESS — theme hints for advanced player
# ────────────────────────────────────────────────────────────────────────────
THEME_TIPS: dict = {
    "fork":             "One piece, two threats. Find the square that attacks both simultaneously.",
    "pin":              "Pin a piece to the king or queen — it can't move without material loss.",
    "skewer":           "Attack the high-value piece; the one behind it falls when it moves.",
    "discoveredAttack": "Move one piece to unleash the attack of another behind it.",
    "mateIn1":          "One move ends it. Check every check and capture first.",
    "mateIn2":          "Force mate in two. Find the move that limits all their responses.",
    "mateIn3":          "Three-move combination. The first move must be forcing.",
    "backRankMate":     "Their king is trapped. A rook or queen on the 8th rank closes the game.",
    "sacrifice":        "Give material for a decisive positional or mating advantage. Calculate 3 moves deep.",
    "deflection":       "Lure the key defender away from its post with a forcing move.",
    "interference":     "Block a piece's line of defense with a sacrifice or interpose.",
    "zugzwang":         "Any move they make worsens their position. Find the quiet, waiting move.",
    "endgame":          "King activity and pawn structure dominate. Technique over tactics here.",
    "quietMove":        "No captures, no checks — but the threat is overwhelming. Think prophylaxis.",
    "attraction":       "Lure the king or a key piece onto a bad square with a sacrifice.",
    "clearance":        "Clear a line or square for a more powerful piece to operate.",
    "trappedPiece":     "A piece has no safe square. Exploit its lack of mobility.",
    "advancedPawn":     "A passed pawn is a criminal that must be stopped or escorted home.",
    "xRayAttack":       "A piece attacks through another piece. Calculate the hidden threat.",
    "doubleCheck":      "Two simultaneous checks — the king must move. Forces unique defensive responses.",
}


# ────────────────────────────────────────────────────────────────────────────
# LIFE LESSONS — rotating case studies (40 entries)
# ────────────────────────────────────────────────────────────────────────────
LIFE_LESSONS = [
    ("Buffett's cash patience",
     "Buffett sat on $130B+ cash through 2020–22, refusing to overpay in a bull market. When others were FOMO-buying, he waited. In 2022–23 he deployed billions into Occidental and others at deep discounts. *Cash is a call option with no expiry.*"),
    ("Bezos's regret minimization",
     "In 1994, Bezos left a $1M/year Wall Street job to sell books online. His framework: imagine yourself at 80 looking back. Would you regret not trying? He said yes — and quit the same week. *Fear of failure often disguises itself as prudence.*"),
    ("Steve Jobs — fired, then returned",
     "In 1985, Apple's board fired the man who started it. Jobs used the freedom of a beginner's mind to build Pixar and NeXT. Apple bought NeXT for $429M, and Jobs came back to save the company. *Losing a title is not losing direction.*"),
    ("Munger's inversion model",
     "Charlie Munger doesn't ask 'How do I succeed?' He asks 'What would guarantee failure — and how do I avoid that?' Inverted thinking cuts through wishful analysis. Most people plan for success. The best also plan against the reasons they'd fail."),
    ("Federer's 54% rule",
     "Roger Federer won 80% of Grand Slam matches but only 54% of individual points played. The margin between elite and everyone else is tiny. What separates them: *forgetting the last point and focusing fully on the next.* Emotional reset compounds over time."),
    ("The Berkshire float model",
     "Buffett used insurance float — premiums paid before claims — as free leverage to invest. He found a legal way to use other people's money to buy assets. Lesson: *the best businesses collect cash before they deliver value.* Think: subscriptions, float, deposits."),
    ("Howard Marks on cycles",
     "Every investment boom follows the same script: good news → prices rise → 'this time is different' → crash. Marks reads the market's emotional temperature before reading valuations. *Where we stand in the cycle matters more than what we buy.*"),
    ("Pabrai's cloning strategy",
     "Mohnish Pabrai runs a $500M fund solo. His edge: wait for a great investor to buy, then clone the position. He admits he's not smarter than Buffett — he just pays attention. *You don't have to be original. You have to be disciplined.*"),
    ("Nolan's no-phone rule",
     "Christopher Nolan bans phones from all his film sets. Deep creative work requires full presence, and smartphones are distraction machines. His films are consistently among the most complex and profitable. *Environment shapes output. Control the environment.*"),
    ("Marcus Aurelius's private standard",
     "The most powerful man on earth wrote: 'You have power over your mind, not outside events.' He recorded this not for publication — it was a private journal. *Greatness often lives in what no one sees.* Build your private standard first."),
    ("Dalio's radical transparency",
     "At Bridgewater, every meeting is recorded, every decision logged. Honest feedback — even brutal — produces better outcomes than politeness. The fund returned 14%+ annually for 30 years. *Radical honesty is uncomfortable. It's also compounding.*"),
    ("Soros breaks the Bank of England",
     "In 1992, Soros shorted £10B of British pounds and made $1B in a day. He saw a structural imbalance the market ignored. Lesson: *When you're right, be confident enough to make it count. Sizing matters as much as being right.*"),
    ("The pilot checklist",
     "In 1935, a superior Boeing aircraft crashed because it was too complex to fly from memory. The solution: a simple checklist. Today checklists prevent 70% of surgical errors. *Systems beat memory. Build the checklist before you need it.*"),
    ("Kodak invented digital, then ignored it",
     "A Kodak engineer invented the digital camera in 1975. Leadership shelved it — fearing it would cannibalize film. Kodak filed for bankruptcy in 2012. *The threat that kills you is usually the one you already know about but choose not to act on.*"),
    ("Jiro Ono's obsession",
     "Jiro Ono is 98 and still perfecting sushi at his Tokyo restaurant. 3 Michelin stars. 3-month waitlist. His philosophy: *Fall in love with your work. Never think you've mastered it. The pursuit is the goal, not the arrival.*"),
    ("Peter Lynch's edge",
     "Peter Lynch turned $18M into $14B by investing in things he saw in daily life: Dunkin' Donuts, L'Eggs, Hanes. His rule: invest in businesses you understand before Wall Street discovers them. *Your personal experience is a legitimate edge.*"),
    ("Michelangelo's apprenticeship",
     "At 13, Michelangelo ground pigments and stretched canvases for years before painting a wall. The David came at 26. *The years that look like preparation are the years that build the master. Don't resent the apprenticeship.*"),
    ("The Navy SEAL 40% rule",
     "SEAL trainers teach that when your body says quit, you're at 40% of actual capacity. The other 60% is unlocked by mental decision. *The body achieves what the mind believes.* Elite performance is mostly a decision, not a physical limit."),
    ("Feynman's explanation test",
     "Richard Feynman explained everything as if teaching it to a child. When he couldn't, he knew he didn't understand it. He won a Nobel Prize this way. *Complexity is a hiding place for confusion. Simplicity is proof of mastery.*"),
    ("Musk's first principles method",
     "When battery costs seemed fixed at $600/kWh, Musk asked: what are batteries made of? Raw materials cost $80/kWh. Why is the assembly $600? *Strip every assumption and rebuild from physics, not convention. Most limits are inherited, not real.*"),
    ("The Ritz-Carlton $2,000 rule",
     "Every Ritz-Carlton employee can spend $2,000 per guest per incident — without manager approval — to resolve a problem. The result: legendary service. *Rules create bureaucracy. Principles create culture. Trust your people with authority.*"),
    ("Taleb's barbell strategy",
     "Taleb invests 90% in ultra-safe assets and 10% in extreme-risk asymmetric bets. He calls this the barbell. *Avoid the middle — moderate risk with moderate return. Go to the edges: safe + explosive. That's where asymmetry lives.*"),
    ("Blue Ocean — Cirque du Soleil",
     "Cirque eliminated animals (costly) and added theatre (storytelling), creating a market that merged circus + Broadway. Revenue exploded without competing with the traditional circus. *Instead of fighting harder in a crowded market, create one where the competition is irrelevant.*"),
    ("Sam Walton's obsession",
     "Sam Walton drove a beat-up pickup truck until he died worth $100B. He spent most days talking to shelf-stackers. His obsession: *lower the cost of living for ordinary people.* Mission clarity built a $600B empire. What's your one obsession?"),
    ("Kahneman's two systems",
     "System 1 (fast, instinctive) handles 95% of decisions. System 2 (slow, logical) handles the rest. Most financial mistakes are System 1 dressed as System 2 analysis. *The quality of your decisions improves when you slow down and recognize which system is running.*"),
    ("The Toyota andon cord",
     "Any Toyota assembly line worker can stop the entire production line if they spot a defect. Most companies fear this. Toyota sees it as quality compounding. *When frontline people can flag problems, problems stay small. Suppressed problems become disasters.*"),
    ("Churchill's darkest hour",
     "In May 1940, Churchill's war cabinet voted 3-2 for a peace deal with Hitler. Churchill delayed the vote, spoke to every MP personally, changed 25 minds in 3 hours. *Leadership isn't the loudest voice. It's the one that holds steady when everyone else panics.*"),
    ("Paul Graham — do things that don't scale",
     "Airbnb's founders flew to New York and personally photographed apartments. Completely unscalable — and exactly right. *The habits that don't scale teach you what to scale later. Start with zero distance from the customer.*"),
    ("Skin in the game",
     "Nassim Taleb's rule: never take advice from someone who doesn't carry the consequences. A doctor who recommends surgery should face the same odds as the patient. *Accountability is the single best filter for credible advice.*"),
    ("The Medici effect",
     "The Medici family funded artists, scientists, and philosophers in the same city. Ideas from different disciplines crashed into each other and produced the Renaissance. *Innovation rarely comes from within a field. It comes from the intersection.*"),
    ("Rockefeller's ledger",
     "Rockefeller tracked every cent he spent from age 16. Not because he was poor — because *what gets measured gets controlled.* He built Standard Oil the same way: measure every barrel, every pipeline, every cost. Numbers are the language of mastery."),
    ("The compounding truth",
     "Buffett made 99% of his net worth after age 52. The math: $1 at 20%/year for 30 years = $237. For 50 years = $9,100. The variable that matters most isn't return rate — it's *time in the game.* Start early. Stay long. Don't quit."),
    ("Netflix — no rules",
     "Netflix has no vacation policy, no expense policy, and no performance reviews. Their only rule: hire remarkable people and treat them like adults. *Rules are a substitute for judgment. Culture is what people do when no one is watching.*"),
    ("Seneca's time audit",
     "Seneca wrote: 'It's not that we have little time. It is that we waste so much of it.' He logged how he used every hour. 2,000 years later, the problem is identical. *Audit your time with the same rigor as your finances and you will always be ahead.*"),
    ("The FP&A edge",
     "The best FP&A professionals don't just report numbers — they translate them into decisions. When a CFO asks 'why did margins drop?' the answer isn't a formula. It's a story: which products, which geographies, what we do next. *Analysts report. Finance partners decide.*"),
    ("Dhirubhai Ambani's rules",
     "Ambani grew from a petrol station attendant in Yemen to building India's largest private company. His rule: *Think big. Think differently. Think fast.* He raised retail investor capital before institutions. Vision + speed beats capital every time."),
    ("Hormozi's offer architecture",
     "Alex Hormozi turned a failing gym into a $100M portfolio by changing one thing: the offer. Same service, same price — but he stacked guarantees, removed risk, and made saying no feel stupid. *Your product isn't the problem. Your offer architecture is.*"),
    ("The marshmallow study (revised)",
     "The original study said kids who waited were more successful. Later research found the real variable was *trust* — kids who'd been let down grabbed the first marshmallow because they couldn't trust the second would come. Environment shapes discipline more than willpower."),
    ("Graham's intrinsic value",
     "Benjamin Graham defined investing simply: buy a dollar for 50 cents. Everything else — macro, sentiment, cycles — is noise if you buy far below intrinsic value. *Margin of safety isn't a number. It's a mindset applied before every decision.*"),
    ("Diogenes and Alexander",
     "Alexander the Great visited Diogenes, the philosopher living in a barrel, and asked: 'Is there anything I can do for you?' Diogenes replied: 'Yes — stand out of my sunlight.' Alexander later said: 'If I were not Alexander, I would wish to be Diogenes.' *True freedom is needing nothing from the powerful.*"),
]


# ────────────────────────────────────────────────────────────────────────────
# HELPERS
# ────────────────────────────────────────────────────────────────────────────

def _rotate(items: list, seed: date = None):
    d = seed or date.today()
    return items[d.toordinal() % len(items)]


def _get_markets() -> str:
    # Use shared cache — avoids duplicate yfinance calls with newspaper.py
    markets = get_cached_markets()
    lines = []
    for m in markets:
        arrow = "↑" if m["change_pct"] > 0.05 else ("↓" if m["change_pct"] < -0.05 else "→")
        lines.append(f"`{m['name']:<10}` {m['price']:<12} {arrow} {m['change_pct']:+.1f}%")
    return "\n".join(lines) if lines else "—"


def _get_jobs() -> str:
    """
    Fetch Senior FP&A / Senior Manager Finance jobs — Dubai + Malaysia.
    Uses shared content_cache to avoid duplicate API calls with newspaper.py.
    """
    jobs = get_cached_jobs()
    results = [(j["city"], j["title"], j["link"]) for j in jobs]

    if not results:
        return (
            "*🇦🇪 Dubai — Senior FP&A / Finance Manager:*\n"
            "• [LinkedIn Dubai](https://www.linkedin.com/jobs/search/?keywords=Senior+FP%26A+Manager&location=Dubai&f_TPR=r86400)\n"
            "• [Bayt Dubai](https://www.bayt.com/en/uae/jobs/senior-fp-a-manager-jobs/)\n\n"
            "*🇲🇾 Malaysia — Senior FP&A / Regional (23–25K MYR):*\n"
            "• [LinkedIn Malaysia](https://www.linkedin.com/jobs/search/?keywords=Senior+FP%26A+Manager&location=Malaysia&f_TPR=r86400)\n"
            "• [JobStreet Malaysia](https://www.jobstreet.com.my/en/job-search/fp-a-manager-jobs/)"
        )

    dubai_lines = [f"• {t} →[↗]({u})" for city, t, u in results if city == "Dubai"]
    my_lines    = [f"• {t} →[↗]({u})" for city, t, u in results if city == "Malaysia"]
    out = ""
    if dubai_lines:
        out += "*🇦🇪 Dubai:*\n" + "\n".join(dubai_lines)
    if my_lines:
        if out:
            out += "\n\n"
        out += "*🇲🇾 Malaysia (23–25K MYR):*\n" + "\n".join(my_lines)
    out += (
        "\n\n[→ LinkedIn Dubai](https://linkedin.com/jobs/search/?keywords=Senior+FP%26A&location=Dubai) · "
        "[→ LinkedIn MY](https://linkedin.com/jobs/search/?keywords=Senior+FP%26A&location=Malaysia)"
    )
    return out

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
    return get_cached_quote()


def _lichess_game_headers() -> dict:
    """Headers for Lichess game export (NDJSON)."""
    h = {"Accept": "application/x-ndjson"}
    token = os.environ.get("LICHESS_TOKEN", "")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _get_yesterday_games() -> list:
    """Fetch all games played yesterday IST by LICHESS_USER."""
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist)
    yest = now - timedelta(days=1)
    day_start = datetime(yest.year, yest.month, yest.day, 0, 0, 0, tzinfo=ist)
    day_end   = datetime(yest.year, yest.month, yest.day, 23, 59, 59, tzinfo=ist)
    since_ms  = int(day_start.timestamp() * 1000)
    until_ms  = int(day_end.timestamp() * 1000)
    try:
        r = requests.get(
            f"https://lichess.org/api/games/user/{LICHESS_USER}",
            params={"since": since_ms, "until": until_ms,
                    "opening": "true", "pgnInJson": "true", "max": 50},
            headers=_lichess_game_headers(),
            timeout=15, stream=True,
        )
        games = []
        for line in r.iter_lines():
            if line:
                try:
                    games.append(json.loads(line))
                except Exception:
                    pass
        return games
    except Exception as e:
        log.warning(f"Lichess games fetch: {e}")
        return []


def _analyze_games(games: list) -> str:
    """
    Summarise yesterday's games for AKK_010.
    Shows W/L/D, time controls, openings played, a short verdict.
    """
    if not games:
        return ""

    total = len(games)
    wins = draws = losses = 0
    openings_w: list[str] = []
    openings_b: list[str] = []
    speeds: dict[str, int] = {}

    for g in games:
        players   = g.get("players", {})
        white_id  = players.get("white", {}).get("user", {}).get("name", "").lower()
        is_white  = white_id == LICHESS_USER.lower()
        winner    = g.get("winner", "")
        status    = g.get("status", "")

        if not winner or status == "draw":
            draws += 1
        elif (winner == "white" and is_white) or (winner == "black" and not is_white):
            wins += 1
        else:
            losses += 1

        op = g.get("opening", {})
        op_name = op.get("name", "")
        eco     = op.get("eco", "")
        if op_name:
            label = f"{eco} {op_name.split(':')[0].strip()}" if eco else op_name.split(":")[0].strip()
            (openings_w if is_white else openings_b).append(label)

        speed = g.get("speed", "")
        if speed:
            speeds[speed] = speeds.get(speed, 0) + 1

    pct = wins / total * 100
    icon = "✅" if pct >= 55 else ("⚖️" if pct >= 45 else "❌")
    lines = [
        f"{icon} *{total} game{'s' if total > 1 else ''}* — {wins}W · {draws}D · {losses}L ({pct:.0f}% WR)"
    ]

    tc = " · ".join(f"{v}× {k}" for k, v in sorted(speeds.items(), key=lambda x: -x[1]))
    if tc:
        lines.append(f"⏱ {tc}")

    seen_w = list(dict.fromkeys(openings_w))[:3]
    seen_b = list(dict.fromkeys(openings_b))[:3]
    if seen_w:
        lines.append(f"♙ White: {' | '.join(seen_w)}")
    if seen_b:
        lines.append(f"♟ Black: {' | '.join(seen_b)}")

    if losses > wins and total >= 3:
        lines.append("_Rough session. Review the losses — find the pattern before playing again._")
    elif wins > losses:
        lines.append("_Good session. Openings holding._")
    else:
        lines.append("_Balanced._")

    lines.append(f"[→ Review on Lichess](https://lichess.org/@/{LICHESS_USER})")
    return "\n".join(lines)


def _get_opening_study_focus() -> str:
    """
    Scan last 14 days of games for AKK_010's weakest opening (≥2 games, lowest WR).
    Returns a one-liner study tip + Lichess link.
    """
    try:
        ist = timezone(timedelta(hours=5, minutes=30))
        since_ms = int((datetime.now(ist) - timedelta(days=14)).timestamp() * 1000)
        r = requests.get(
            f"https://lichess.org/api/games/user/{LICHESS_USER}",
            params={"since": since_ms, "opening": "true", "max": 40},
            headers=_lichess_game_headers(),
            timeout=12, stream=True,
        )
        games = []
        for line in r.iter_lines():
            if line:
                try:
                    games.append(json.loads(line))
                except Exception:
                    pass

        op_stats: dict[str, list[int]] = {}  # name → [wins, total]
        for g in games:
            white_id = g.get("players", {}).get("white", {}).get("user", {}).get("name", "").lower()
            is_white = white_id == LICHESS_USER.lower()
            winner   = g.get("winner", "")
            won = (winner == "white" and is_white) or (winner == "black" and not is_white)
            op_name = g.get("opening", {}).get("name", "Unknown").split(":")[0].strip()
            if op_name not in op_stats:
                op_stats[op_name] = [0, 0]
            op_stats[op_name][1] += 1
            if won:
                op_stats[op_name][0] += 1

        # weakest: ≥2 games, lowest win rate
        weak = [(n, w, t) for n, (w, t) in op_stats.items() if t >= 2]
        if not weak:
            return ""
        weak.sort(key=lambda x: x[1] / x[2])
        name, w, t = weak[0]
        wr = w / t * 100
        slug = name.replace(" ", "_").replace("'", "")
        return (
            f"📚 *Study focus:* {name} — {w}/{t} = {wr:.0f}% WR\n"
            f"[→ Opening explorer](https://lichess.org/opening/{slug}) · "
            f"[→ Practice](https://lichess.org/study/search?q={name.replace(' ', '+')})"
        )
    except Exception as e:
        log.warning(f"opening study focus: {e}")
        return ""


def _get_chess_puzzle() -> str:
    """Daily puzzle from Lichess, rated relative to AKK_010's puzzle rating (1646)."""
    import re
    MY_PUZZLE_RATING = 1646
    try:
        r = requests.get(
            "https://lichess.org/api/puzzle/daily",
            headers={"Accept": "application/json"},
            timeout=10,
        )
        if r.status_code != 200:
            return ""
        data   = r.json()
        puzzle = data.get("puzzle", {})
        pid    = puzzle.get("id", "")
        rating = puzzle.get("rating", 0)
        themes = [t for t in puzzle.get("themes", [])
                  if t not in ("master", "masterVsMaster", "puzzleOfTheDay")]

        def fmt_theme(t: str) -> str:
            return re.sub(r'([A-Z])', r' \1', t).strip().title()

        theme_str = " · ".join(fmt_theme(t) for t in themes[:3])
        tip = next((THEME_TIPS[t] for t in themes if t in THEME_TIPS),
                   "Calculate 3 moves deep before touching a piece.")

        diff = rating - MY_PUZZLE_RATING
        level = "🔴 stretch" if diff > 150 else ("🟡 at level" if diff > -150 else "🟢 comfort zone")

        return (
            f"Rating: *{rating:,}* ({level}) · _{theme_str}_\n"
            f"💡 _{tip}_\n"
            f"[→ Solve on Lichess](https://lichess.org/training/{pid})"
        )
    except Exception as e:
        log.warning(f"chess puzzle fetch failed: {e}")
        return ""


def _save_to_db(content: str):
    try:
        con = db.connect()
        con.execute("""
            CREATE TABLE IF NOT EXISTS daily_briefs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                date       TEXT NOT NULL UNIQUE,
                content    TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        today = datetime.now(IST).date().isoformat()  # IST date
        con.execute(
            "INSERT OR REPLACE INTO daily_briefs (date, content) VALUES (?, ?)",
            (today, content)
        )
        con.commit()
        db.sync(con)
        con.close()
    except Exception as e:
        log.warning(f"daily_brief DB save failed: {e}")


def _push_to_gist(content: str, brief_date: str):
    """Push brief to data/daily_brief.json in the trading-dashboard GitHub repo.

    Replaces the old Gist approach — reads from GitHub raw URL which is public
    and doesn't require BRIEFS_GIST_ID. Dhruvedge terminal reads from this file.
    """
    token = os.environ.get("GITHUB_TOKEN", "")
    repo  = os.environ.get("TRADING_REPO", "caakshayk1-boop/trading-dashboard")
    if not token:
        log.warning("daily_brief: GITHUB_TOKEN not set — skipping GitHub push")
        return

    gh_headers = {
        "Authorization": f"token {token}",
        "Accept":        "application/vnd.github.v3+json",
        "User-Agent":    "akk-daily-brief/1.0",
    }
    api_base = f"https://api.github.com/repos/{repo}/contents/data/daily_brief.json"

    # Load existing briefs from repo file
    briefs, sha = [], None
    try:
        r = requests.get(api_base, headers=gh_headers, timeout=10)
        if r.status_code == 200:
            import base64 as _b64
            data = r.json()
            sha  = data.get("sha")
            existing = json.loads(_b64.b64decode(data["content"]).decode())
            if isinstance(existing, list):
                briefs = existing
    except Exception as e:
        log.warning(f"daily_brief: repo read failed: {e}")

    # Upsert today, keep last 30
    briefs = [b for b in briefs if b.get("date") != brief_date]
    briefs.insert(0, {
        "date":       brief_date,
        "text":       content,
        "created_at": datetime.now(IST).isoformat(),
    })
    briefs = briefs[:30]
    payload = json.dumps(briefs, ensure_ascii=False, indent=2)

    import base64 as _b64
    body: dict = {
        "message": f"data: daily brief {brief_date} [skip ci]",
        "content": _b64.b64encode(payload.encode()).decode(),
        "branch":  "main",
    }
    if sha:
        body["sha"] = sha
    r = requests.put(api_base, json=body, headers=gh_headers, timeout=15)
    if r.status_code in (200, 201):
        log.info("daily_brief: pushed to GitHub repo ✓")
    else:
        log.warning(f"daily_brief: GitHub push failed {r.status_code} {r.text[:100]}")

    # Also update Gist if BRIEFS_GIST_ID is set (legacy Dhruvedge terminal support)
    gist_id = os.environ.get("BRIEFS_GIST_ID", "")
    if gist_id:
        try:
            gr = requests.patch(
                f"https://api.github.com/gists/{gist_id}",
                json={"files": {"briefs.json": {"content": payload}}},
                headers=gh_headers, timeout=10,
            )
            if gr.status_code == 200:
                log.info("daily_brief: Gist updated ✓")
        except Exception as ge:
            log.warning(f"daily_brief: Gist update failed: {ge}")


# ────────────────────────────────────────────────────────────────────────────
# BUILD & SEND
# ────────────────────────────────────────────────────────────────────────────

def build_brief() -> str:
    now      = datetime.now(IST)
    today    = now.date()          # IST date — not UTC date
    weekday  = now.strftime("%A")
    datestr  = now.strftime("%d %B %Y")

    markets        = _get_markets()
    jobs           = _get_jobs()
    quote          = _get_quote()
    chess          = _get_chess_puzzle()
    study_focus    = _get_opening_study_focus()
    yesterday_games = _get_yesterday_games()
    game_analysis  = _analyze_games(yesterday_games)

    habit_name, habit_time, habit_why = _rotate(HABITS, today)
    hack                              = _rotate(PRODUCTIVITY, today)
    topic, body                       = _rotate(LEARNING, today)
    lesson_title, lesson_body         = _rotate(LIFE_LESSONS, today)

    headline    = _get_global_headline()
    global_note = f"\n🌍 _{headline}_" if headline else ""
    # Chess — yesterday's games + today's puzzle + study focus
    games_block = (
        f"\n━━━━━━━━━━━━━━━━━━━\n♟️ *YESTERDAY'S GAMES*\n━━━━━━━━━━━━━━━━━━━\n{game_analysis}"
    ) if game_analysis else ""

    puzzle_parts = [chess]
    if study_focus:
        puzzle_parts.append(study_focus)
    chess_block = (
        f"\n━━━━━━━━━━━━━━━━━━━\n♟️ *CHESS*\n━━━━━━━━━━━━━━━━━━━\n" +
        "\n\n".join(p for p in puzzle_parts if p)
    ) if any(puzzle_parts) else ""

    brief = f"""🌅 *GOOD MORNING, AKSHAY*
{weekday} · {datestr} · 6 AM IST

━━━━━━━━━━━━━━━━━━━
💼 *OPPORTUNITIES*
━━━━━━━━━━━━━━━━━━━
Senior FP&A · Finance Manager · Regional · Controller

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
📖 *CASE STUDY*
━━━━━━━━━━━━━━━━━━━
*{lesson_title}*
{lesson_body}{games_block}{chess_block}

━━━━━━━━━━━━━━━━━━━
💬 *QUOTE*
━━━━━━━━━━━━━━━━━━━
{quote}"""

    return brief


def send_brief():
    log.info("daily_brief: building...")
    brief    = build_brief()
    today    = datetime.now(IST).date().isoformat()   # IST date
    _save_to_db(brief)
    _post(brief)
    # Send newspaper link
    newspaper_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if newspaper_domain:
        newspaper_url = f"https://{newspaper_domain}"
        _post(f"📰 *AKK Times is live* — {today}\n{newspaper_url}\n\n_World news · Markets · FP&A · Top 5 picks · OTT · Money hacks_")
    try:
        _push_to_gist(brief, today)
    except Exception as e:
        log.warning(f"daily_brief: GitHub push failed (non-fatal): {e}")

    # Sync to Obsidian daily note
    try:
        from obsidian_sync import write_morning_brief
        open_sigs: list = []
        try:
            import db as _db
            con = _db.connect()
            rows = con.execute(
                "SELECT * FROM all_signals WHERE status='OPEN' AND score>=65 ORDER BY score DESC LIMIT 5"
            ).fetchall()
            open_sigs = [dict(zip([d[0] for d in con.execute("PRAGMA table_info(all_signals)").fetchall()], r)) for r in rows]
            con.close()
        except Exception:
            pass
        write_morning_brief({"brief": brief[:200]}, open_sigs)
    except Exception as e:
        log.debug(f"daily_brief: Obsidian sync failed (non-fatal): {e}")

    log.info("daily_brief: sent ✓")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    send_brief()
