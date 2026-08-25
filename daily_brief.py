#!/usr/bin/env python3
"""
daily_brief.py — 6 AM MYT personal morning brief for Akshay Kothari
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

    # ── Jainism ──────────────────────────────────────────────────────────────
    ("Jainism · Ahimsa (Non-violence)",
     "The first and highest principle of Jainism. Not just physical — ahimsa includes thoughts, words, and economic choices. Mahavira taught that every living being has a soul deserving protection. In finance: don't extract value from others through exploitation. Build something that genuinely helps. *Wealth earned without harm compounds differently.*"),
    ("Jainism · Aparigraha (Non-possession)",
     "Possess only what you need. Jain monks own nothing. Lay practitioners limit possessions deliberately. This isn't poverty — it's clarity. When you're not protecting excess, you're free to focus. *Attachment to accumulation creates anxiety. Enough, clearly defined, creates peace.*"),
    ("Jainism · Anekantavada (Many-sidedness)",
     "No single perspective holds the complete truth. Jains believe reality is experienced differently depending on standpoint. In finance: the bear and bull can both be right at different time horizons. *Intellectual humility — hearing multiple truths — is an edge. Certainty is usually a blind spot.*"),
    ("Jainism · Asteya (Non-stealing)",
     "Don't take what isn't freely given — including credit, time, attention, or opportunity. Mahavira extended this beyond objects: taking more than your fair share of resources or recognition is theft. In business: credit your team, pay fairly, don't over-promise. *Reputation built without taking is the most durable kind.*"),
    ("Jainism · Satya (Truthfulness)",
     "Speak truth — but only when it doesn't cause harm. Jains combine truth with ahimsa: harmful truths should be withheld, but silence is never the cover for deception. *In financial reporting, in client conversations, in self-assessment: honest but never cruel. This is the FP&A standard.*"),
    ("Jainism · Brahmacharya (Right conduct)",
     "Channel energy toward purpose. For lay Jains, this means intentionality — not squandering attention on distraction, conflict, or excess. Every hour is a resource. *The Jain who lives with discipline at age 25 has compounded that energy into something permanent by 40.*"),
]


# ────────────────────────────────────────────────────────────────────────────
# HELPERS
# ────────────────────────────────────────────────────────────────────────────

# ────────────────────────────────────────────────────────────────────────────
# news.askakshay.com — the one source the brief and the site share
# ────────────────────────────────────────────────────────────────────────────
#
# The morning brief used to assemble its own view of the world: yfinance
# through content_cache for markets, a Reuters RSS feed for the headline, and a
# direct Turso query for the signal recap. The website assembled a second view
# from /api/*. Two pipelines, one subject — so they drifted, and there was no
# way to tell which one was wrong.
#
# Everything factual in this brief now reads the same live API the site renders
# from. If a number is wrong in Telegram it is wrong on the site too, which is
# the only arrangement that keeps either of them honest.
#
# Every call fails soft. A brief that arrives with one section degraded is worth
# more at 6 AM than no brief at all, so each fetch returns None on failure and
# the caller falls back to what it used before.

NEWS_API = os.environ.get("NEWS_API_BASE", "https://news.askakshay.com/api")


def _news_get(path: str, timeout: int = 12) -> Optional[dict]:
    """One GET against news.askakshay.com. None on any failure, never raises."""
    try:
        r = requests.get(
            f"{NEWS_API}{path}",
            timeout=timeout,
            headers={"User-Agent": "daily-brief/2.0"},
        )
        if r.status_code != 200:
            log.warning("news api %s -> HTTP %s", path, r.status_code)
            return None
        j = r.json()
        if not isinstance(j, dict) or j.get("ok") is False:
            log.warning("news api %s -> ok=false: %s", path, str(j)[:160])
            return None
        return j
    except Exception as e:
        log.warning("news api %s failed: %s", path, e)
        return None


def _news_markets() -> Optional[str]:
    """The same market strip the site shows, formatted for Telegram."""
    j = _news_get("/markets")
    if not j:
        return None
    rows = j.get("markets") or []
    lines = []
    for m in rows:
        pct = m.get("change_pct")
        if pct is None:
            continue
        arrow = "↑" if pct > 0.05 else ("↓" if pct < -0.05 else "→")
        name = str(m.get("name", ""))[:10]
        price = str(m.get("price", "—"))
        lines.append(f"`{name:<10}` {price:<12} {arrow} {pct:+.1f}%")
    if not lines:
        return None
    live, total = j.get("live"), j.get("total")
    if live is not None and total is not None and live < total:
        lines.append(f"_{live} of {total} quotes live — the rest are last close._")
    return "\n".join(lines)


def _news_headline() -> Optional[str]:
    """
    One headline, from the same wire the site reads.

    This replaces feedparser against feeds.reuters.com. That host stopped
    resolving — the lookup returns NXDOMAIN — so `_get_global_headline` had been
    returning None on every run and the brief had silently shipped without its
    world line for as long as the feed has been dead. A source that fails to a
    blank section is worse than one that fails loudly.
    """
    j = _news_get("/news")
    if j:
        items = j.get("news") or []
        if items:
            title = str(items[0].get("title", "")).strip()
            src = str(items[0].get("source", "")).strip()
            if title:
                # Plain text only. The caller wraps this whole string in _..._,
                # so emphasising the source here nests underscores and Telegram
                # rejects the message with a 400 rather than rendering it.
                return f"{title} — {src}" if src else title
    # Second choice: the top clustered world event.
    j = _news_get("/world")
    if j:
        top = j.get("top") or []
        if top:
            title = str(top[0].get("title", "")).strip()
            if title:
                return title
    return None


def _fmt_price(v, currency: str = "") -> str:
    """
    Price at a sane precision for its magnitude.

    The old recap tried to do this inline as
    `f"{entry:.4f if entry < 100 else entry:.1f}"`, which is not a legal format
    spec — Python raises ValueError on it. That exception was caught by the
    bare `except Exception` around the whole recap, logged at warning level and
    turned into an empty string, so the 6 AM signal recap has never once been
    delivered. It failed on the first row it tried to format, every single day.
    """
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "—"
    if n >= 1000:
        body = f"{n:,.0f}"
    elif n >= 100:
        body = f"{n:.1f}"
    elif n >= 1:
        body = f"{n:.2f}"
    else:
        body = f"{n:.4f}"
    return f"{currency}{body}" if currency else body


def _rotate(items: list, seed: date = None):
    d = seed or date.today()
    return items[d.toordinal() % len(items)]


def _get_markets() -> str:
    # news.askakshay.com first, so Telegram and the site quote the same tape.
    # The yfinance cache stays as the fallback: it is what this ran on before,
    # it is already warm from newspaper.py, and 6 AM is the wrong time to have
    # no markets section because one host was slow.
    from_api = _news_markets()
    if from_api:
        return from_api

    log.warning("markets: news API unavailable, falling back to yfinance cache")
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

    dubai_lines = [f"• {t} [↗]({u})" for city, t, u in results if city == "Dubai"]
    my_lines    = [f"• {t} [↗]({u})" for city, t, u in results if city == "Malaysia"]
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
    """
    One business headline, from news.askakshay.com.

    Was feedparser against feeds.reuters.com, which no longer resolves. See
    `_news_headline` for why that mattered more than it looked.
    """
    return _news_headline()


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


def _build_signal_recap() -> str:
    """
    Yesterday and today's signals, read from news.askakshay.com.

    Two things were wrong with the version this replaces.

    First, it queried Turso directly while the website rendered the same trades
    through /api/signals, so the recap and the site could disagree about a
    trade's status and nothing would catch it.

    Second, and worse, it never ran. Every price was formatted with
    `f"{entry:.4f if entry < 100 else entry:.1f}"`, which is not a valid format
    specifier; Python raises ValueError on the first row. The bare
    `except Exception` around the whole function swallowed it into a warning and
    returned "", and `send_brief` treats "" as "nothing to report". The recap
    has been failing this way on every run that had signals to report.

    Returns "" when there is genuinely nothing to say. Any failure is logged and
    also returns "" — the brief still goes out without this block.
    """
    try:
        now       = datetime.now(IST)
        yesterday = (now - timedelta(days=1)).date().isoformat()
        today     = now.date().isoformat()

        j = _news_get("/signals?limit=400")
        if not j:
            log.warning("signal recap: news API unavailable")
            return ""

        rows = [
            r for r in (j.get("signals") or [])
            if yesterday <= str(r.get("date", ""))[:10] <= today
            or yesterday <= str(r.get("closed_at") or "")[:10] <= today
        ]
        if not rows:
            return ""

        open_sigs, winners, losers, cancelled = [], [], [], []
        for r in rows:
            sym    = str(r.get("symbol") or "")
            action = str(r.get("action") or "")
            status = str(r.get("status") or "OPEN").upper()
            cur    = str(r.get("currency") or "")
            entry  = _fmt_price(r.get("entry"), cur)
            # pnl_str is pre-formatted by the API, so Telegram and the site
            # cannot round the same trade two different ways.
            pnl    = str(r.get("pnl_str") or "—")
            rmult  = r.get("r_multiple")
            rtxt   = f" · {float(rmult):+.2f}R" if isinstance(rmult, (int, float)) else ""

            if status == "OPEN":
                open_sigs.append(f"• *{sym}* {action} @ `{entry}` — still open")
            elif status.startswith("T") and "_HIT" in status:
                tgt = _fmt_price(r.get("target2") or r.get("target1"), cur)
                winners.append(f"✅ *{sym}* {action} → target `{tgt}` ({pnl}{rtxt})")
            elif "SL" in status or status == "STOPPED":
                losers.append(f"❌ *{sym}* {action} → stop `{_fmt_price(r.get('sl'), cur)}` ({pnl}{rtxt})")
            elif status == "CANCELLED":
                cancelled.append(f"⚪ *{sym}* {action} — cancelled")
            else:
                # TIME_STOP, EXPIRED — closed, but neither a target nor a stop.
                losers.append(f"⏳ *{sym}* {action} → {status.replace('_', ' ').lower()} ({pnl}{rtxt})")

        lines = [f"📊 *SIGNAL RECAP* — {yesterday} → {today}\n"]
        if winners:
            lines.append("*✅ Closed — target hit:*")
            lines.extend(winners)
        if losers:
            lines.append("\n*❌ Closed — stopped or timed out:*")
            lines.extend(losers)
        if open_sigs:
            lines.append(f"\n*🔵 Still open ({len(open_sigs)}):*")
            lines.extend(open_sigs[:6])
            if len(open_sigs) > 6:
                lines.append(f"  _...and {len(open_sigs) - 6} more_")
        if cancelled:
            lines.append(f"\n*⚪ Cancelled: {len(cancelled)}*")

        w, l = len(winners), len(losers)
        wr   = f"{round(w / (w + l) * 100)}% WR" if (w + l) else "no closed trades"

        # Lifetime edge, from the same /stats the site publishes. Quoted with
        # its sample size, because an expectancy without an n is a mood.
        edge = ""
        st = _news_get("/stats")
        if st:
            h = st.get("headline") or {}
            trades = h.get("trades")
            exp    = h.get("expectancy_r")
            if isinstance(trades, int) and isinstance(exp, (int, float)):
                edge = f" · lifetime {exp:+.3f}R over {trades} closed"

        lines.append(f"\n_{len(rows)} signals · {wr}{edge} · not SEBI advice_")
        return "\n".join(lines)
    except Exception as e:
        log.warning(f"signal recap error: {e}")
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
    # GH Actions uses GH_PAT (can't name secrets GITHUB_*), Railway uses GITHUB_TOKEN
    token = os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GH_PAT", "")
    repo  = os.environ.get("TRADING_REPO", "caakshayk1-boop/trading-dashboard")
    if not token:
        log.warning("daily_brief: No GitHub token (GITHUB_TOKEN / GH_PAT) — skipping push")
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
{weekday} · {datestr} · 6 AM MYT

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


# ── Telegram formatting helpers ─────────────────────────────────────────────

def istNow() -> str:
    return datetime.now(IST).strftime("%d %b %Y, %I:%M %p")


def esc(v) -> str:
    """Telegram legacy Markdown breaks on an unbalanced _ * ` or [ — a headline
    with an underscore silently drops the whole message with a 400."""
    out = str(v if v is not None else "")
    for ch in ("_", "*", "`", "[", "]"):
        out = out.replace(ch, "\\" + ch)
    return out


def num(v):
    try:
        n = float(v)
        return n if math_isfinite(n) else None
    except (TypeError, ValueError):
        return None


def math_isfinite(n) -> bool:
    import math as _m
    return _m.isfinite(n)


def rate(v, digits: int = 1) -> str:
    """A LEVEL, so it carries no sign: a 34.8% win rate is not "up 34.8%"."""
    n = num(v)
    return "—" if n is None else f"{n:.{digits}f}%"


def _mandate_book() -> Optional[dict]:
    """Today's order book under the Rs 1 crore mandate. None if unavailable."""
    try:
        import swing_rulebook as RB
        import datetime as _dt
        j = _news_get("/signals?limit=400")
        if not j:
            return None
        today = _dt.date.today()

        def age(d):
            try:
                return (today - _dt.date(*map(int, str(d)[:10].split("-")))).days
            except Exception:
                return 9999

        fresh = [r for r in (j.get("signals") or [])
                 if str(r.get("status")) == "OPEN"
                 and not r.get("entry_triggered_at")
                 and age(r.get("date")) <= 30]
        return RB.build_book(fresh, {})
    except Exception as e:
        log.warning("mandate book unavailable: %s", e)
        return None


def build_section_brief(slot: str = "midday") -> str:
    """
    The whole of news.askakshay.com, in one Telegram message.

    Every section of the site gets a line here, in the site's own document
    order, so the brief is a table of contents for the page rather than a
    second, competing summary that can drift from it. Sections that have
    nothing to say are omitted rather than printed empty — the old brief padded
    its Opportunities block with two LinkedIn links and no jobs, which taught
    the reader to skip it.

    MORNING — before the Indian open, and after the site has rebuilt. What the
              book wants today, and what the world did overnight.
    MIDDAY  — NSE is live. What the book wants, and what moved to get there.
    EVENING — NSE has closed. What happened, and what is queued for tomorrow.

    Only the title and the evening extras vary by slot. The BODY is the site's
    own document order in every slot on purpose: the brief is a table of
    contents for news.askakshay.com, and a brief that reorganised itself per
    slot would be a second summary competing with the page.
    """
    evening = slot == "evening"
    title = {
        "evening": "🌇 *EVENING — THE DAILY SIGNAL*",
        "morning": "🌅 *MORNING — THE DAILY SIGNAL*",
    }.get(slot, "🌤 *MIDDAY — THE DAILY SIGNAL*")
    L = [f"{title}\n{istNow()} IST · news.askakshay.com\n"]
    ticket_blocks: list = []

    def rule(s):
        L.append(f"━━━━━━━━━━━━━━━━━━━\n{s}\n━━━━━━━━━━━━━━━━━━━")

    # ── 01 Market Intel ────────────────────────────────────────────────────
    mk = _news_markets()
    if mk:
        rule("📊 *01 · MARKET INTEL*")
        L.append(mk)

    # ── 02 Trade Ideas — the mandate's order book ──────────────────────────
    book = _mandate_book()
    if book:
        st = book["state"]
        rule("🎯 *02 · TRADE IDEAS* — Rs 1 crore mandate")
        L.append(f"*{len(book['admitted'])} to place* · heat {st['heat_pct']}% · "
                 f"deployed {st['deployed_pct']}% · cash Rs {st['cash']:,}")
        # In the EVENING the market is shut, so the full book is not actionable
        # tonight and it is the first thing that should give way to the recap.
        # Summarised deliberately rather than left to the length guard, which
        # was trimming eight tickets down to one — a book showing 1 of 8 reads
        # as a bug, and "here is tomorrow's board, one line" reads as a choice.
        if evening:
            L.append("_Placed at tomorrow's open. Full board: news.askakshay.com_")
            ticket_blocks_enabled = False
        else:
            ticket_blocks_enabled = True

        # Engine names carry underscores — top5_pick, ai_longterm — and an
        # unbalanced _ makes Telegram reject the WHOLE message with a 400.
        for t in (book["admitted"] if ticket_blocks_enabled else []):
            ticket_blocks.append([
                f"\n*{esc(t['symbol'])}* — {esc(t['horizon_label'])} · {esc(t['engine'])}",
                f"  buy `{t['qty']}` @ `{t['entry']:,.2f}`  ·  stop `{t['stop']:,.2f}` ({t['stop_pct']}%)",
                f"  target +{t['final_gain_pct']}% · {t['reward_risk']}:1 · hold {esc(t['hold_days'])}",
                *[f"    {leg['label']} sell `{leg['qty']}` @ `{leg['price']:,.2f}` (+{leg['gain_pct']}%)"
                  for leg in t["legs"]],
            ])
        L.append(_BOOK)
        if book["deferred"]:
            L.append(f"\n_{len(book['deferred'])} more valid, waiting on a cap._")
        if book["duplicates"]:
            L.append(f"_{len(book['duplicates'])} dropped as duplicate names._")

    # ── 03 World · 04 Findings ─────────────────────────────────────────────
    w = _news_get("/world")
    if w and (w.get("top") or []):
        rule("🌍 *03 · WORLD* — last 24h")
        for e in (w.get("top") or [])[:4]:
            L.append(f"  • {esc(str(e.get('title',''))[:150])}")
    nw = _news_get("/news")
    if nw and (nw.get("news") or []):
        rule("📰 *04 · THE WIRE*")
        for n in (nw.get("news") or [])[:4]:
            L.append(f"  • {esc(str(n.get('title',''))[:130])} — {esc(n.get('source'))}")

    # ── 09 SIP Buckets ─────────────────────────────────────────────────────
    sp = _news_get("/sip")
    if sp and sp.get("plan"):
        pl = sp["plan"]
        rule("🪣 *09 · SIP BUCKETS*")
        L.append(f"  Rs {pl.get('monthly_amount', 0):,}/mo · step-up {pl.get('step_up_pct')}% · "
                 f"year {pl.get('sip_year')}")

    # ── 11 Portfolio ───────────────────────────────────────────────────────
    tr = _news_get("/tracker")
    if tr:
        rule("📁 *11 · PORTFOLIO*")
        n = tr.get("count") or 0
        L.append(f"  {n} tracked position{'' if n == 1 else 's'}" if n else
                 "  Nothing held. An OPEN setup is not a position.")

    # ── 13 Signal Log · 14 Performance ─────────────────────────────────────
    if evening:
        recap = _build_signal_recap()
        if recap:
            rule("📋 *13 · SIGNAL LOG* — today")
            L.append(recap)
    stt = _news_get("/stats")
    if stt and (stt.get("headline") or {}):
        h = stt["headline"]
        rule("📈 *14 · PERFORMANCE* — closed trades only")
        L.append(f"  expectancy `{num(h.get('expectancy_r')) or 0:+.3f}R` over "
                 f"{h.get('trades','—')} closed · win rate {rate(h.get('win_rate'),1)}")
        L.append(f"  avg win `{num(h.get('avg_win_r')) or 0:+.2f}R` · "
                 f"avg loss `{num(h.get('avg_loss_r')) or 0:+.2f}R` · "
                 f"profit factor {num(h.get('profit_factor')) or 0:.2f}")

    # ── 16 Data Health ─────────────────────────────────────────────────────
    hl = _news_get("/health")
    if hl:
        rule("🩺 *16 · DATA HEALTH*")
        L.append(f"  {hl.get('signals','—')} signals · latest {hl.get('latest_signal_date','—')} · "
                 f"{hl.get('open_setups','—')} open setups")

    L.append("\n_Full board: news.askakshay.com · Life: life.askakshay.com_")
    L.append("_Not SEBI advice._")
    return _fit(L, ticket_blocks)


# Telegram rejects a sendMessage over 4096 characters outright. A full order
# book plus every section reached 4,004 on the first live build — inside the
# limit, but with no headroom for a day that fills all eight tickets with three
# legs each, and the evening brief then hit the ceiling exactly.
#
# Trimming by scanning rendered lines was fragile, so the order book is
# assembled as a list of whole ticket BLOCKS and dropped one at a time from the
# tail. Tickets are ranked by score, so what falls off is the weakest, and the
# reader is told how many and where to read them.
TG_LIMIT = 3900


def _fit(lines: list, ticket_blocks: list) -> str:
    """Join the brief, dropping whole ticket blocks until it fits."""
    def render(keep: int) -> str:
        out = []
        for item in lines:
            if item is _BOOK:
                for b in ticket_blocks[:keep]:
                    out.extend(b)
                gone = len(ticket_blocks) - keep
                if gone:
                    out.append(f"\n_{gone} more on the board — news.askakshay.com_")
            else:
                out.append(item)
        return "\n".join(out)

    keep = len(ticket_blocks)
    text = render(keep)
    while len(text) > TG_LIMIT and keep > 0:
        keep -= 1
        text = render(keep)
    return text


class _BookMarker:
    """Placeholder for where the order book is spliced in."""


_BOOK = _BookMarker()


def _build_apex_digest() -> str:
    """
    Fetch APEX bot P&L and build a Telegram digest.

    The host moved. This pointed at apex-bot-jnuc.onrender.com, a service that
    is gone — the connection does not establish at all, so the request raised,
    the bare except returned "", and `send_brief` skipped the block. APEX has
    been running behind the named Cloudflare tunnel at apex.askakshay.com; that
    is where /health actually answers.
    """
    try:
        r = requests.get(
            os.environ.get("APEX_HEALTH_URL", "https://apex.askakshay.com/health"),
            timeout=15,
        )
        if r.status_code != 200:
            return ""
        d = r.json()
        bal   = d.get("balance", 0)
        btc   = d.get("btc", 0)
        mkts  = d.get("markets", 0)
        paper = "📝 PAPER" if d.get("paper") else "🔴 LIVE"

        # Fetch more detail from state endpoint if available
        pnl = bal - 2000.0
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        pnl_emoji = "🟢" if pnl >= 0 else "🔴"

        return (
            f"\n━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ *APEX BOT — DAILY P&L*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"{paper} | BTC ${btc:,.0f}\n\n"
            f"💰 Balance: *${bal:,.2f}*\n"
            f"{pnl_emoji} P&L vs start: *{pnl_str}*\n"
            f"🌐 Markets tracked: {mkts}\n"
            f"📊 Dashboard: terminal.askakshay.com/apex"
        )
    except Exception as e:
        log.warning(f"APEX digest failed: {e}")
        return ""


def send_brief(slot: str = "midday"):
    """
    One of the two daily sends. `slot` is "morning" (08:00 MYT) or "evening"
    (17:00 MYT); "midday" is still accepted and is what the morning slot
    replaced. The slot comes from the cron that fired rather than from the wall
    clock — GitHub delays scheduled runs by hours, and an hour-equality test is
    how the 6 AM brief silently became a CF scan on late days.

    The morning send sits two hours after the newspaper build's own cron rather
    than immediately after it. Both jobs drift by the same unpredictable amount,
    and the gap is what stops the brief quoting yesterday's page. If it loses
    the race anyway the brief degrades rather than breaks: every news.askakshay
    call falls back to the local yfinance cache.
    """
    log.info("daily_brief: building %s...", slot)
    brief    = build_section_brief(slot)
    today    = datetime.now(IST).date().isoformat()   # IST date
    _save_to_db(brief)
    _post(brief)

    # APEX P&L rides the evening send only. Two alerts a day means two, and a
    # digest that arrives as its own notification is a third.
    if slot == "evening":
        apex_digest = _build_apex_digest()
        if apex_digest:
            _post(apex_digest)
            log.info("APEX P&L digest sent")

    # The recap is a SECTION of the evening brief now, not a second message.
    # Posting it separately as well is how one event became two notifications.
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
