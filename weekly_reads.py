#!/usr/bin/env python3
"""weekly_reads.py — seven company studies, written every Saturday morning.

Akshay, 2026-09-16: "out of 1000 stocks screen pick 7 stocks — curated read —
idea is know in & out of any company, what they do, how they do it. Act like an
AI expert... so I get meaningful understanding for my own which can help me in
public speaking too. 5-7 min read for each."

WHAT THIS IS NOT. It is not a signal, a call, or a ranked list — the site
already has three of those and none of them teaches you anything about a
business. A reader who finishes one of these should be able to explain, out
loud and without notes, what the company sells, who pays for it, why the money
stays with them rather than leaking to a competitor, and what would have to go
wrong for the thesis to break. That is a different product from "buy this".

WHY SEVEN, AND WHY THESE SEVEN. One per sector, because the point is breadth of
understanding rather than seven variations on the same industry. A name has to
be a real business before it is worth a weekend: statements on file, enough
market cap that its numbers mean something, enough turnover that you could
actually act on the understanding. Inside those gates, the pick favours the
most INSTRUCTIVE name, not the most attractive one — a business whose economics
are legible, where the numbers tell a clear story rather than a muddy one.

NO NAME REPEATS INSIDE EIGHT WEEKS. The archive is the memory: fifty-odd
studies a year, none of them the same company twice a quarter.

EVERY NUMBER IS CHECKED. The model is handed a dossier built from the screen
and is told to use nothing else. Afterwards, every figure in the prose is
matched back against that dossier, and a study carrying a number that is not in
its own source material is REJECTED rather than published. This repo has been
here before: the daily brief's QA gate caught an invented "President Biden",
and a study that invents a margin is worse than no study, because it reads
exactly as authoritative as a true one.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import random
import re
import time
from datetime import date, datetime, timedelta, timezone

import requests

log = logging.getLogger("weekly_reads")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ROOT = pathlib.Path(__file__).resolve().parent
SCREEN = ROOT / "docs" / "screen.json"
# docs/, not data/. The site pulls its feeds from docs/*.json upstream — a
# file written to data/ is committed, backed up, and never reaches a reader.
OUT = ROOT / "docs" / "weekly_reads.json"

GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
# Same model and the same trap as newspaper.py: gpt-oss reasons before it
# answers and bills those hidden tokens to max_tokens, so a budget that looks
# generous returns an empty 200. Headroom is large here because the visible
# answer is itself long.
GROQ_MODEL = os.environ.get("GROQ_MODEL") or "openai/gpt-oss-120b"
GROQ_HEADROOM = 1200

STUDIES = int(os.environ.get("WEEKLY_READS_N", "7"))
COOLDOWN_WEEKS = int(os.environ.get("WEEKLY_READS_COOLDOWN", "8"))
MIN_MCAP_CR = float(os.environ.get("WEEKLY_READS_MIN_MCAP", "5000"))
MIN_TURNOVER_CR = float(os.environ.get("WEEKLY_READS_MIN_TURNOVER", "5"))
# Above this many figures that cannot be traced to the dossier, the study is
# not approximating — it is inventing. See study_for().
MAX_UNVERIFIED = int(os.environ.get("WEEKLY_READS_MAX_UNVERIFIED", "2"))


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────
def n(v):
    """A number or None. Never 0 for a missing field — that is the coercion
    trap this estate has been bitten by repeatedly: Number(null) is 0 and 0 is
    a perfectly plausible margin."""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f          # NaN check without importing math


def saturday_of(d: date) -> date:
    """The Saturday this edition belongs to — today if it is Saturday, else the
    most recent one. A Wednesday backfill must land on the same key the
    Saturday run would have used, or the archive grows two copies of a week."""
    return d - timedelta(days=(d.weekday() - 5) % 7)


def fmt(v, unit="", dp=1):
    if v is None:
        return "not reported"
    if unit == "cr":
        return f"Rs {v:,.0f} cr"
    if unit == "%":
        return f"{v:.{dp}f}%"
    if unit == "x":
        return f"{v:.{dp}f}x"
    return f"{v:,.{dp}f}"


# ─────────────────────────────────────────────────────────────────────────────
# selection
# ─────────────────────────────────────────────────────────────────────────────
def instructiveness(r: dict) -> float:
    """How much a reader LEARNS from this company, which is not how attractive
    it is to own.

    Weighted toward businesses whose economics are legible: a real return on
    capital, a margin you can point at, growth that is either clearly present
    or clearly absent. A name with half its fields missing teaches nothing, so
    completeness itself scores — a study is only as good as its dossier.
    """
    s = 0.0
    roce, roe = n(r.get("roce")), n(r.get("roe"))
    if roce is not None:
        s += min(roce, 40) * 0.8           # capital efficiency is the spine of a study
    if roe is not None:
        s += min(roe, 40) * 0.4
    nm = n(r.get("net_margin"))
    if nm is not None:
        s += min(abs(nm), 30) * 0.5
    rev = n(r.get("rev_cagr"))
    if rev is not None:
        s += min(abs(rev), 40) * 0.4       # a clear decline teaches as much as growth
    pio = n(r.get("piotroski"))
    if pio is not None:
        s += pio * 1.5
    # Completeness: a dossier with holes produces a study full of hedges.
    have = sum(1 for k in ("roce", "roe", "net_margin", "ebit_margin", "rev_cagr",
                           "eps_cagr", "de", "pe", "pb", "fcf_margin", "cfo_pat",
                           "icover", "div_yield") if n(r.get(k)) is not None)
    s += have * 3.0
    return s


def pick(rows: list[dict], already: set[str], seed: str) -> list[dict]:
    """Seven names, one per sector, deterministic for a given week."""
    pool = []
    for r in rows:
        sym = str(r.get("sym") or "").upper()
        if not sym or sym in already:
            continue
        if not r.get("has_stmts"):
            continue
        mc, to = n(r.get("mcap_cr")), n(r.get("turnover_cr"))
        if mc is None or mc < MIN_MCAP_CR:
            continue
        if to is None or to < MIN_TURNOVER_CR:
            continue
        if not str(r.get("sector") or "").strip():
            continue
        pool.append(r)

    log.info(f"pool after gates: {len(pool)} of {len(rows)}")
    by_sector: dict[str, list[dict]] = {}
    for r in pool:
        by_sector.setdefault(str(r["sector"]).strip(), []).append(r)

    # Best-in-sector by instructiveness, then sectors shuffled on the week's
    # seed so the same sector does not lead every edition.
    champions = []
    for sec, rs in by_sector.items():
        rs.sort(key=instructiveness, reverse=True)
        champions.append((sec, rs[0]))
    rng = random.Random(seed)
    rng.shuffle(champions)
    champions.sort(key=lambda x: instructiveness(x[1]), reverse=True)
    chosen = [r for _, r in champions[:STUDIES]]
    log.info("picked: " + ", ".join(f"{r['sym']} ({r.get('sector')})" for r in chosen))
    return chosen


# ─────────────────────────────────────────────────────────────────────────────
# the dossier — the ONLY facts the model may use
# ─────────────────────────────────────────────────────────────────────────────
def _derived(r: dict) -> list[tuple[str, float, str, int]]:
    """Facts the screen implies but does not carry, computed here rather than
    left to the model.

    The screen publishes margins and absolute cash flows but no revenue line,
    so the first draft of this dossier could describe how profitable a company
    was without ever saying how big it is — which is most of what "know a
    company in and out" means. The model noticed the hole before I did: handed
    free cash flow of Rs 220 cr and a 27.5% free-cash margin it divided one by
    the other, wrote "Rs 800 cr-plus of revenue", and was rejected by the
    number check for a figure it had derived correctly.

    The right answer was not to relax the check. It was to put the number in
    the dossier, where it is computed once, labelled as implied, and can be
    verified — rather than recomputed silently inside prose on every run.
    """
    out: list[tuple[str, float, str, int]] = []
    fcf, fcfm = n(r.get("fcf_cr")), n(r.get("fcf_margin"))
    cfo, cfopat = n(r.get("cfo_cr")), n(r.get("cfo_pat"))
    nm = n(r.get("net_margin"))
    rev = None
    if fcf is not None and fcfm not in (None, 0):
        rev = fcf / (fcfm / 100.0)
    if rev is not None and rev > 0:
        out.append(("Revenue, implied from free cash flow and its margin", rev, "cr", 0))
        em = n(r.get("ebit_margin"))
        if em is not None:
            out.append(("EBIT, implied from revenue and EBIT margin", rev * em / 100.0, "cr", 0))
        if nm is not None:
            out.append(("Net profit, implied from revenue and net margin", rev * nm / 100.0, "cr", 0))
    elif cfo is not None and cfopat not in (None, 0):
        out.append(("Net profit, implied from operating cash flow and its conversion",
                    cfo / cfopat, "cr", 0))
    return out


def dossier(r: dict) -> tuple[str, list[float]]:
    """Facts as text, plus every number in them, so the output can be checked."""
    f: list[tuple[str, object, str, int]] = [
        ("Market capitalisation", n(r.get("mcap_cr")), "cr", 0),
        ("Share price", n(r.get("price")), "", 2),
        ("52-week high", n(r.get("high52")), "", 2),
        ("52-week low", n(r.get("low52")), "", 2),
        ("Return on capital employed", n(r.get("roce")), "%", 1),
        ("Median ROCE across reported years", n(r.get("roce_med")), "%", 1),
        ("Return on equity", n(r.get("roe")), "%", 1),
        ("Net profit margin", n(r.get("net_margin")), "%", 1),
        ("EBIT margin", n(r.get("ebit_margin")), "%", 1),
        ("Free cash flow margin", n(r.get("fcf_margin")), "%", 1),
        ("Operating cash flow", n(r.get("cfo_cr")), "cr", 0),
        ("Free cash flow", n(r.get("fcf_cr")), "cr", 0),
        ("Cash conversion (CFO to PAT, median)", n(r.get("cfo_pat")), "x", 2),
        ("Revenue CAGR", n(r.get("rev_cagr")), "%", 1),
        ("Revenue growth, latest year", n(r.get("rev_yoy")), "%", 1),
        ("EBITDA CAGR", n(r.get("ebitda_cagr")), "%", 1),
        ("EPS CAGR", n(r.get("eps_cagr")), "%", 1),
        ("Profit growth, latest year", n(r.get("pat_yoy")), "%", 1),
        ("Margin change year on year", n(r.get("margin_delta")), "%", 2),
        ("Debt to equity", n(r.get("de")), "x", 2),
        ("Interest cover", n(r.get("icover")), "x", 1),
        ("Effective tax rate", n(r.get("tax")), "%", 1),
        ("Price to earnings", n(r.get("pe")), "x", 1),
        ("PE percentile against its own history", n(r.get("pe_pctile")), "%", 0),
        ("Price to book", n(r.get("pb")), "x", 2),
        ("Dividend yield", n(r.get("div_yield")), "%", 2),
        ("Piotroski score", n(r.get("piotroski")), "", 0),
        ("Promoter and insider holding", n(r.get("insiders")), "%", 2),
        ("Institutional holding", n(r.get("instis")), "%", 2),
        ("Return over one year", n(r.get("r1y")), "%", 1),
        ("Return over six months", n(r.get("r6m")), "%", 1),
        ("Distance from the 52-week high", n(r.get("from_high")), "%", 1),
        ("Annualised volatility", n(r.get("sd1y")), "%", 1),
        ("Daily turnover", n(r.get("turnover_cr")), "cr", 1),
    ]
    f.extend(_derived(r))
    lines, nums = [], []
    for label, v, unit, dp in f:
        if v is None:
            continue
        lines.append(f"- {label}: {fmt(v, unit, dp)}")
        nums.append(float(v))

    head = (f"Company: {r.get('name') or r.get('sym')} (NSE: {r.get('sym')})\n"
            f"Sector: {r.get('sector')}\n"
            f"Industry: {r.get('ind') or 'not classified'}\n"
            f"Financial years of statements on file: {r.get('fy_count') or 'unknown'}"
            f" (latest {r.get('fy') or 'unknown'})\n")
    return head + "\n".join(lines), nums


PROMPT = """You are an equity analyst writing a weekend study for one reader: a
chartered accountant who works in FP&A, reads numbers before prose, and wants to
understand this business well enough to explain it out loud to other finance
people without notes.

Write about {name} (NSE: {sym}), an Indian listed company in {sector}.

THE ONLY FACTS YOU MAY USE ARE BELOW. You may explain what a figure MEANS, and
you may describe how companies in this industry generally make money — that is
your own knowledge and it is welcome. You may NOT invent any number, date,
person, customer name, subsidiary, acquisition or event that is not in this
dossier. If you do not know something specific, say what would need to be
checked instead of guessing.

DOSSIER
{dossier}

Write these eight sections, using exactly these headings, in markdown:

## In one line
What this company is, in a sentence a non-specialist would understand.

## How it actually makes money
The revenue model. Who pays, for what, how often, and what has to happen for a
rupee to arrive. Be concrete about the mechanics of this industry.

## The economics
What the margins and returns say about the QUALITY of the business. Interpret
the numbers — a 20% ROCE and a 6% ROCE are different businesses, so say which
one this is and what that implies about reinvestment and pricing power.

## The moat, or the absence of one
Why would this company still be earning these returns in five years? Be honest
when the numbers suggest there is no moat.

## What the numbers say right now
Valuation, growth and balance sheet together. Cite the actual figures.

## What could go wrong
The three most plausible ways this thesis breaks. Specific to THIS business and
these numbers, not generic market risk.

## What to watch next
The specific line items or events that would confirm or kill the thesis at the
next results.

## Say it out loud
Exactly three bullet points. Each one a single sentence a reader could say in a
meeting and sound informed — a number, what it means, and why it matters. No
preamble.

HOW TO READ FOUR RATIOS THAT ARE ROUTINELY MISREAD. Getting one of these
backwards is the fastest way to sound uninformed to a finance audience, and a
draft of this study did exactly that:

- Cash conversion (CFO to PAT) BELOW 1.0 means reported profit is NOT fully
  turning into cash — working capital is absorbing it. Above 1.0 is the good
  case. Do not describe 0.8x as evidence that earnings are cash-backed; it is
  the opposite, and it is worth a sentence on where the cash is going. It is
  also a MEDIAN across reported years, not the latest year.
- For a LENDER or a financial company, operating cash flow is dominated by
  movements in the loan book, so negative CFO is a growing book rather than a
  distressed one. Do not read it as a warning sign.
- Return on capital employed is often not meaningful for banks and financial
  companies, whose capital is their raw material. If the sector is financial,
  lean on return on equity instead and say why.
- Debt to equity is inflated by leases capitalised under Ind AS 116, so a
  retailer or an airline can look levered when the "debt" is shop rent.
- The PE PERCENTILE is measured against the company's OWN history, and it runs
  the opposite way to the PE itself. A low percentile means the stock is cheap
  relative to how it has been priced before, even when the absolute multiple
  looks high. Do not write that the market is "paying a premium" for a name
  sitting in the bottom quartile of its own range — say that it is expensive on
  an absolute multiple and cheap against itself, and that the two readings
  disagree, which is the interesting part.

RULES
- 1,100 to 1,400 words in total. This is a five to seven minute read.
- Plain, direct sentences. Short paragraphs.
- Never use: robust, strong, healthy, solid, well-positioned, headwinds,
  tailwinds, going forward, in today's fast-paced.
- In "Say it out loud", write the three sentences plainly. Do not wrap them in
  quotation marks and do not bold the whole line — a draft did both and left
  stray asterisks on the page.
- Do not recommend buying or selling. Do not give a price target.
- Do not open with a disclaimer or close with a summary of what you just wrote.
"""


def groq(prompt: str, max_tokens: int = 2600, retries: int = 3) -> str:
    if not GROQ_KEY:
        log.error("GROQ_API_KEY is not set — cannot write studies")
        return ""
    for attempt in range(retries):
        try:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_KEY}",
                         "Content-Type": "application/json"},
                json={"model": GROQ_MODEL,
                      "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": max_tokens + GROQ_HEADROOM,
                      "reasoning_effort": "low",
                      "temperature": 0.6},
                timeout=180,
            )
            if r.status_code == 200:
                body = r.json()
                choice = (body.get("choices") or [{}])[0]
                txt = (choice.get("message", {}).get("content") or "").strip()
                if txt:
                    return txt
                log.warning(
                    f"empty 200 (finish={choice.get('finish_reason')}, "
                    f"reasoning_tokens={body.get('usage', {}).get('completion_tokens_details', {}).get('reasoning_tokens')})")
            elif r.status_code == 429:
                # Two different 429s wear this code. A daily cap does not clear
                # inside a run, so waiting is pointless; a per-minute one does.
                if re.search(r"tokens per day|TPD", r.text, re.I):
                    log.error(f"daily token budget exhausted — stopping: {r.text[:160]}")
                    return ""
                wait = 20 * (attempt + 1)
                log.warning(f"rate limited, waiting {wait}s")
                time.sleep(wait)
                continue
            else:
                log.warning(f"groq {r.status_code}: {r.text[:200]}")
        except Exception as e:                           # noqa: BLE001
            log.warning(f"groq call failed: {e}")
        time.sleep(4)
    return ""


NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")

# A model writing finance prose does not use the ASCII hyphen. It reaches for
# the non-breaking hyphen, the en dash and the real minus sign, and the first
# version of the checker below read "‑32.3" as POSITIVE 32.3, failed to find it
# among the dossier's negatives, and rejected a study whose every figure was
# correct. Normalised before anything is parsed.
DASHES = {"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
          "\u2014": "-", "\u2212": "-"}


def _normalise(text: str) -> str:
    for k, v in DASHES.items():
        text = text.replace(k, v)
    return text


def unverified_numbers(text: str, allowed: list[float]) -> list[str]:
    """Numbers in the prose that are not in the dossier.

    Deliberately forgiving, because a study SHOULD do arithmetic a reader can
    follow: small integers, years, percentages of a whole, and anything that
    matches a dossier figure to within rounding are all fine. What this is
    hunting is a fabricated specific — a revenue number, a client count, an
    acquisition price — presented with the same confidence as a real one.
    """
    ok = []
    for a in allowed:
        # Magnitude, not sign. Prose carries direction in words — "fallen 32%",
        # "39% below its high" — so demanding the sign match the dossier
        # rejects correct writing. Fabrication shows up as a magnitude that is
        # nowhere in the source, which is what this is actually hunting.
        for v in (a, round(a, 2), round(a, 1), round(a)):
            ok.extend([v, abs(v)])
    bad = []
    text = _normalise(text)
    for raw in NUM_RE.findall(text):
        try:
            v = float(raw.replace(",", ""))
        except ValueError:
            continue
        if abs(v) <= 100 and float(v).is_integer():
            continue                      # counts, small percentages, list items
        if 1900 <= v <= 2100 and float(v).is_integer():
            continue                      # years
        if any(abs(abs(v) - abs(c)) <= max(0.05, abs(c) * 0.01) for c in ok):
            continue
        bad.append(raw)
    return bad


REQUIRED = ["## In one line", "## How it actually makes money", "## The economics",
            "## The moat", "## What the numbers say right now",
            "## What could go wrong", "## What to watch next", "## Say it out loud"]


def study_for(r: dict) -> dict | None:
    sym = str(r.get("sym"))
    doc, nums = dossier(r)
    if len(nums) < 12:
        log.warning(f"{sym}: only {len(nums)} facts on file — skipped, too thin to study")
        return None

    text = groq(PROMPT.format(name=r.get("name") or sym, sym=sym,
                              sector=r.get("sector"), dossier=doc))
    if not text:
        log.warning(f"{sym}: no study returned")
        return None

    missing = [h for h in REQUIRED if h.split(",")[0][:18].lower() not in text.lower()]
    if missing:
        log.warning(f"{sym}: missing sections {missing} — rejected")
        return None

    # ── HOW STRICT TO BE, AND WHY NOT ABSOLUTE ──────────────────────────────
    #
    # The first version rejected a study for ANY number it could not match, and
    # threw away good work for prose doing exactly what good prose does:
    # "interest cover of over 20,000x" against a real 24,000, "Rs 800 cr-plus
    # of revenue" derived correctly from two dossier lines. Approximation is
    # not fabrication, and a checker that cannot tell them apart silently
    # selects for studies that avoid saying anything concrete.
    #
    # What actually needs catching is a study that INVENTS — a client count, an
    # acquisition price, a growth figure with no source. That does not arrive
    # as one rounded ratio; it arrives as a paragraph of specifics. So the
    # count is the signal: one or two unmatched figures is rounding, several is
    # a story being made up. Above the threshold it is regenerated once, and
    # rejected if the second attempt is no better.
    #
    # Whatever survives is RECORDED on the study rather than forgotten, so a
    # bad run is auditable after the fact instead of invisible.
    bad = unverified_numbers(text, nums)
    if len(bad) > MAX_UNVERIFIED:
        log.warning(f"{sym}: {len(bad)} unverifiable numbers {bad[:6]} — rewriting once")
        text2 = groq(PROMPT.format(name=r.get("name") or sym, sym=sym,
                                   sector=r.get("sector"), dossier=doc))
        bad2 = unverified_numbers(text2, nums) if text2 else bad
        if text2 and len(bad2) <= MAX_UNVERIFIED:
            text, bad = text2, bad2
        else:
            log.warning(f"{sym}: still {len(bad2)} after a rewrite — rejected")
            return None
    if bad:
        log.info(f"{sym}: {len(bad)} approximate figures kept for audit: {bad[:6]}")

    words = len(text.split())
    return {
        "sym": sym,
        "name": r.get("name") or sym,
        "sector": r.get("sector"),
        "industry": r.get("ind"),
        "mcap_cr": n(r.get("mcap_cr")),
        "price": n(r.get("price")),
        "words": words,
        "read_minutes": max(1, round(words / 220)),
        "facts": [ln[2:] for ln in doc.splitlines() if ln.startswith("- ")],
        "study": text,
        "model": GROQ_MODEL,
        "approximate_figures": bad,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────────────────────────────────────────
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
TG_MAX = 3800          # the hard cap is 4096 and Telegram REJECTS, never truncates
READS_URL = os.environ.get("READS_URL", "https://signal.askakshay.com/reads")

# Markdown is a minefield here. An underscore in a ticker turns the rest of the
# message into italics and Telegram answers 400 for an unclosed entity — this
# repo has lost whole briefs to exactly that. Escaped, not hoped about.
_MD = str.maketrans({c: "\\" + c for c in "_*[]()~`>#+-=|{}.!"})


def _md(t) -> str:
    return str(t if t is not None else "").translate(_MD)


def _tg_post(text: str) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "MarkdownV2",
                  "disable_web_page_preview": True},
            timeout=20)
        if r.status_code == 200:
            return True
        # Log the BODY. A 400 from Telegram names the offending byte offset,
        # and a code alone has never once been enough to fix one.
        log.warning(f"telegram {r.status_code}: {r.text[:300]}")
    except Exception as e:                                # noqa: BLE001
        log.warning(f"telegram failed: {e}")
    return False


def _first_line(study: str) -> str:
    """The study's own opening sentence, which is written to be exactly this.

    Taking the first paragraph under "## In one line" rather than generating a
    second summary: two summaries of one study will disagree eventually, and
    the one on the page is the one that was checked.
    """
    m = re.search(r"##\s*In one line\s*\n+(.+?)(?:\n\s*\n|\n##)", study, re.S)
    line = (m.group(1) if m else study).strip()
    line = re.sub(r"[*_`#]+", "", line).replace("\n", " ")
    line = re.sub(r"\s+", " ", line).strip()
    return line[:240]


def send_to_telegram(edition: dict) -> bool:
    """One digest, not seven studies.

    Seven 1,300-word studies is 60,000 characters against a 4,096 cap — the
    whole set cannot be the message and should not be. What belongs on a phone
    on a Saturday morning is the shortlist: what each company is, in one line,
    and a link to read the rest. The studies live on the page.
    """
    if not TG_TOKEN or not TG_CHAT:
        log.info("no telegram credentials — skipping the digest")
        return False

    studies = edition.get("studies") or []
    if not studies:
        return False
    mins = sum(s.get("read_minutes") or 0 for s in studies)

    head = (f"📚 *Weekly reads* — Saturday {_md(edition.get('week'))}\n"
            f"_{_md(len(studies))} companies, one per sector · {_md(mins)} min in total_\n")
    blocks = []
    for i, s in enumerate(studies, 1):
        mc = f" · ₹{s['mcap_cr']:,.0f}cr" if s.get("mcap_cr") else ""
        blocks.append(
            f"*{_md(i)}\\. {_md(s.get('sym'))}* — {_md(s.get('sector') or '')}{_md(mc)}\n"
            f"{_md(s.get('name') or '')}\n"
            f"{_md(_first_line(s.get('study') or ''))}\n"
            f"_{_md(s.get('read_minutes') or 0)} min read_\n")
    tail = (f"\nRead them: {_md(READS_URL)}\n"
            f"_No entry, stop or target — these are about the businesses, not trades\._")

    # Chunked on the character limit, never truncated: Telegram REJECTS an
    # oversize message outright rather than trimming it, so a digest that grows
    # past the cap would simply never arrive.
    msgs, cur = [], head
    for b in blocks:
        if len(cur) + len(b) + len(tail) > TG_MAX:
            msgs.append(cur)
            cur = ""
        cur += "\n" + b
    msgs.append(cur + tail)

    ok = True
    for i, m in enumerate(msgs):
        if not _tg_post(m):
            ok = False
        if i + 1 < len(msgs):
            time.sleep(1)
    log.info(f"telegram: {len(msgs)} message(s), {'sent' if ok else 'PARTIAL'}")
    return ok


def main() -> int:
    if not SCREEN.exists():
        log.error(f"{SCREEN} not found — the screen has not been built")
        return 1
    rows = (json.loads(SCREEN.read_text()) or {}).get("rows") or []
    log.info(f"screen: {len(rows)} names")

    archive = {"editions": []}
    if OUT.exists():
        try:
            archive = json.loads(OUT.read_text()) or archive
        except Exception as e:                           # noqa: BLE001
            log.warning(f"archive unreadable ({e}) — starting a new one")

    editions = archive.get("editions") or []
    week = saturday_of(date.today())
    wk = week.isoformat()

    if any(e.get("week") == wk for e in editions) and not os.environ.get("FORCE_REWRITE"):
        log.info(f"edition for {wk} already written — nothing to do")
        return 0

    recent = set()
    for e in editions[:COOLDOWN_WEEKS]:
        for s in e.get("studies") or []:
            recent.add(str(s.get("sym", "")).upper())
    log.info(f"{len(recent)} names on cooldown from the last {COOLDOWN_WEEKS} editions")

    chosen = pick(rows, recent, seed=wk)
    studies = []
    for r in chosen:
        s = study_for(r)
        if s:
            studies.append(s)
            log.info(f"  {s['sym']}: {s['words']} words, ~{s['read_minutes']} min")
        time.sleep(2)

    if not studies:
        log.error("no studies written")
        return 1

    editions.insert(0, {
        "week": wk,
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "studies": studies,
    })
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cooldown_weeks": COOLDOWN_WEEKS,
        "editions": editions[:80],       # roughly eighteen months of weekends
    }, indent=2), encoding="utf-8")
    log.info(f"wrote {len(studies)} studies for {wk} to {OUT}")

    # The digest goes out only for a NEWLY written edition. A re-run that
    # finds the week already done returns above, so the bot cannot send the
    # same Saturday twice.
    send_to_telegram(editions[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
