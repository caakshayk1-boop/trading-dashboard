#!/usr/bin/env python3
"""
smart_reads.py — the "so what" layer over the reading list.

A Smart Read was a headline, a summary and a link. This adds structure to the
ones worth it: what happened, why it matters, what to watch. It is the only
place on this site where a model writes interpretation rather than
description, so the rules below are stricter than anywhere else.

THE FOUR RULES

1. NOTHING IS PUBLISHED THAT THE GATE REJECTS.
   brief_engine.qa_reject is reused verbatim rather than reimplemented. It
   already refuses any number or name absent from the source — the check that
   caught a model inventing "President Biden" beside a real article about Mark
   Carney. A second gate would be a second thing to keep correct.

2. A REJECTED READ FALLS BACK TO THE PLAIN CARD.
   Failure costs a read its prose and nothing else. The section never shows a
   gap, and never shows unverified prose.

3. FACT AND INTERPRETATION ARE LABELLED, NOT BLENDED.
   "What happened" is drawn from the source and marked FACT. "Why it matters"
   and "What to watch" are the model's reading of it and are marked
   INTERPRETATION in the payload and on the page. A reader must never have to
   guess which is which.

4. NO RECOMMENDATION, EVER.
   No buy, sell, target, allocation or price call — checked here, on top of
   the shared gate. This is a news product; a page that tells a reader what to
   do with a security is a different regulated thing, and the line is enforced
   in code rather than left to a prompt.

BUDGET

Groq is rate limited (8k TPM), and the briefing engine is already spending it.
Only the first MAX_STRUCTURED reads get a pass; the rest render as they always
did. Ordering is the section's own, so the most prominent reads are the ones
that gain structure.
"""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger(__name__)

# How many reads get an AI pass per build. The cap is a budget, not a quality
# judgement — see the module docstring.
MAX_STRUCTURED = 6

# Language that turns a news product into a recommendation. Checked in
# addition to the shared gate, because the shared gate is about truthfulness
# and this is about staying on the right side of what the product IS.
_ADVICE = re.compile(
    r"\b(buy|sell|short|accumulate|book profit|target price|price target|"
    r"stop loss|entry point|overweight|underweight|allocate|invest in|"
    r"should own|worth buying|avoid this stock|recommend)\b", re.I)

PROMPT = """You are summarising ONE news article for a financial reader.

Return ONLY valid JSON with exactly these keys:
{{
  "headline": "under 14 words, factual, no adjectives",
  "bullets": ["what happened, sentence one", "what happened, sentence two"],
  "whyItMatters": "one paragraph on significance",
  "whatToWatch": "one specific, checkable thing next"
}}

HARD RULES — output violating any of these is discarded:
- Use ONLY facts stated in the article below. Invent nothing.
- Every number and every name you write MUST appear in the article.
- Do NOT recommend buying, selling or holding anything.
- Do NOT give a price target, allocation or trade idea.
- Do NOT predict prices.

ARTICLE
Title: {title}
{summary}
"""


def _parse(raw: str) -> dict | None:
    """The model's JSON, or None. Never raises on bad output."""
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def advice_reject(ev: dict) -> str | None:
    """Reason this output must not publish because it advises. None if clean.

    Separate from qa_reject on purpose: that gate asks "is this true?", this
    one asks "is this the kind of thing we publish at all?". They fail for
    different reasons and the log should say which.
    """
    text = " ".join([
        str(ev.get("headline") or ""),
        " ".join(str(b) for b in (ev.get("bullets") or [])),
        str(ev.get("whyItMatters") or ""),
        str(ev.get("whatToWatch") or ""),
    ])
    m = _ADVICE.search(text)
    return f"recommendation language: {m.group(0)!r}" if m else None


def structure(read: dict, ai) -> dict | None:
    """One structured Smart Read, or None if it cannot be trusted.

    `ai` is a text-completion callable, injected rather than imported so this
    is testable with a stub and no network — the same pattern the wallet and
    badge modules use.
    """
    if not ai:
        return None
    title = (read or {}).get("title") or ""
    summary = (read or {}).get("summary") or ""
    source_text = f"{title}. {summary}"
    # Under this there is not enough article to interpret, and a model given
    # too little context fills the gap itself. That is the failure this whole
    # module exists to prevent, so it declines rather than risks it.
    if len(summary) < 120:
        return None

    try:
        raw = ai(PROMPT.format(title=title, summary=summary), max_tokens=420)
    except Exception as e:                                   # noqa: BLE001
        log.info(f"smart read AI failed for {title[:50]!r}: {e}")
        return None

    ev = _parse(raw)
    if not ev:
        return None

    reason = advice_reject(ev)
    if reason:
        log.info(f"smart read REJECTED ({title[:40]!r}): {reason}")
        return None

    try:
        from brief_engine import qa_reject
    except Exception:
        return None                       # no gate available, so nothing ships
    reason = qa_reject(ev, source_text)
    if reason:
        log.info(f"smart read QA-rejected ({title[:40]!r}): {reason}")
        return None

    return {
        # FACT — drawn from the article.
        "what_happened": [str(b).strip() for b in ev["bullets"] if str(b).strip()],
        # INTERPRETATION — the model's reading. Labelled in the payload so the
        # template cannot render it as though it were reported fact.
        "why_it_matters": str(ev.get("whyItMatters") or "").strip(),
        "what_to_watch": str(ev.get("whatToWatch") or "").strip(),
        "headline": str(ev.get("headline") or "").strip(),
        "read_seconds": max(20, min(90, len(summary) // 12)),
    }


def enrich(reads: list[dict], ai, limit: int = MAX_STRUCTURED) -> dict:
    """Attach structure to the first `limit` reads. Returns build stats.

    Mutates the reads in place — a read that gains nothing is left exactly as
    it was, which is what makes the fallback free.
    """
    done = rejected = 0
    for r in (reads or [])[:limit]:
        s = structure(r, ai)
        if s:
            r["smart"] = s
            done += 1
        else:
            rejected += 1
    return {"structured": done, "rejected": rejected,
            "attempted": min(len(reads or []), limit), "total": len(reads or [])}
