#!/usr/bin/env python3
"""
brief_engine.py — the Daily Intelligence Brief.

Compresses the day's wire copy into a small number of EVENTS rather than a list
of articles: 50-100 headlines in, 10-15 deduplicated and ranked events out,
each keeping every source that reported it.

Why event-based
---------------
Reuters, Bloomberg, CNBC and the FT covering one Fed decision is ONE event with
four sources, not four Smart Reads. The existing news path had no clustering at
all, so the same story appeared as many times as it was syndicated.

Pipeline
--------
    fetch -> normalize -> cluster -> score -> structure (AI) -> QA gate -> cache

Only the STRUCTURE step uses a model, and only for clusters that already
cleared the importance floor. Clustering is deterministic — an LLM is not asked
whether two stories are the same, because that decision has to be reproducible
and cheap. AI writes prose about facts it was handed; it never decides what is
true, what merges, or what ranks.

Anti-hallucination, in layers
-----------------------------
1. The model is given ONLY the clustered headlines and summaries, and told the
   source material is the entire world.
2. Output is strict JSON against a fixed schema; a parse failure is a rejected
   event, never a published one.
3. The QA gate (see `qa_reject`) drops events with empty bullets, duplicated
   bullets, a headline over 14 words, no surviving source, a market-impact
   claim with no asset, or numbers that appear nowhere in the source text.
4. Anything rejected falls back to the deterministic summary built from the
   headlines themselves — the section degrades to plainer copy, never to
   invented copy.

Failure posture
---------------
Every stage is wrapped. With no GROQ key, no network, or a model outage, the
build still produces events with deterministic summaries. `build()` returning
{} drops the section from the nav rather than publishing an empty shell.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("brief_engine")

IST = timezone(timedelta(hours=5, minutes=30))

# ── source tiers ────────────────────────────────────────────────────────────
# Tier 1 primary (regulator, central bank, filing, official statement) — none
# of the RSS wires qualify, so nothing is tier 1 today. The tier exists because
# the scoring and confidence rules refer to it, and because adding an official
# feed later must not require re-plumbing anything.
TIER = {
    "Bloomberg": 2, "Financial Times": 2, "WSJ Markets": 2, "BBC Business": 2,
    "CNBC": 2, "MarketWatch": 2, "The Economist": 2,
    "Economic Times": 3, "ET Economy": 3, "Livemint": 3, "Business Standard": 3,
    "Moneycontrol": 3, "BusinessLine": 3,
}
DEFAULT_TIER = 4

CATEGORIES = ["World", "Markets & Economy", "Business", "India",
              "Malaysia & SEA", "Technology"]

# Category routing is keyword-based and deliberately simple. A story that
# matches nothing lands in Markets & Economy, which is what these feeds are.
CAT_RULES = [
    ("Malaysia & SEA", r"\b(malaysia|malaysian|kuala lumpur|ringgit|bursa|singapore|"
                       r"indonesia|jakarta|thailand|vietnam|philippines|asean)\b"),
    ("India",          r"\b(india|indian|rbi|sebi|nifty|sensex|rupee|mumbai|delhi|"
                       r"modi|gst|nse|bse)\b"),
    # World BEFORE Technology. An oil-and-Middle-East story was filed under
    # Technology because a bare `ai` token matched somewhere in the summary —
    # geopolitics is the stronger signal and must win the tie.
    ("World",          r"\b(war|ceasefire|sanction|election|treaty|summit|nato|"
                       r"ukraine|russia|israel|gaza|iran|oman|tariff|geopolit|"
                       r"middle east|mideast)\b"),
    ("Technology",     r"\b(artificial intelligence|chip|semiconductor|nvidia|"
                       r"openai|anthropic|software|cloud|cyber|data centre|data center|"
                       r"technology|startup)\b"),
    ("Business",       r"\b(earnings|profit|revenue|merger|acquisition|ipo|layoff|"
                       r"ceo|results|quarterly|stake|deal)\b"),
]

# Words that carry no signal when comparing two headlines.
_STOP = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for", "at",
    "by", "with", "from", "as", "is", "are", "was", "were", "be", "been", "it",
    "its", "this", "that", "these", "those", "after", "over", "amid", "says",
    "say", "said", "new", "up", "down", "may", "will", "would", "could", "how",
    "why", "what", "when", "who", "top", "here", "today",
}

# Tokens too common in financial wire copy to identify an EVENT. They still
# count toward the overlap score, but two headlines sharing only these have not
# been shown to be about the same thing — "Ahead of Market: 10 things", "Market
# Trading Guide", "Sensex today" and an unrelated anchor-investor story all
# share {market, stock} and are four different articles.
_WEAK = {
    "market", "markets", "stock", "stocks", "share", "shares", "trading",
    "trade", "investor", "investors", "index", "indices", "live", "update",
    "guide", "things", "ahead", "session", "close", "closing", "open",
    "india", "indian", "global", "world", "news", "report", "week", "day",
    "high", "low", "rise", "rises", "fall", "falls", "gain", "gains", "loss",
    "losses", "点", "crore", "lakh", "rs", "usd",
}

# Conservative: a false merge attributes one outlet's reporting to
# another's story, which is worse than showing two near-duplicates.
MERGE_THRESHOLD = 0.20

# Only this many events get model-written prose (see build()).
AI_EVENT_BUDGET = 8
# Seconds between model calls. 8,000 TPM on the on-demand tier / ~1,300 tokens
# a call is roughly five calls a minute.
AI_PACE_SECONDS = 13
MAX_EVENTS = 15
QUICK_COUNT = 5


# ── fetch ───────────────────────────────────────────────────────────────────
def fetch_articles(hours: int = 30) -> list[dict]:
    """Every wire article in the window, from the feeds the site already trusts.

    Reuses content_cache's own feed list and User-Agent handling rather than
    keeping a second copy — those feeds were curated by testing which ones
    actually respond (Reuters 404s, Equitymaster 403s, and so on).
    """
    try:
        import content_cache as cc
    except Exception as e:                                   # noqa: BLE001
        log.warning(f"content_cache unavailable: {e}")
        return []

    import feedparser
    # content_cache's UA, not feedparser's default. Several of these feeds
    # answer a default python UA with a 404 that is indistinguishable from a
    # dead feed (Lichess did exactly that for days), so the UA is load-bearing.
    ua = getattr(cc, "_UA", None) or (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

    out, seen = [], set()
    feeds = list(getattr(cc, "NEWS_FEEDS", []))
    for source, url in feeds:
        try:
            parsed = feedparser.parse(url, agent=ua)
            entries = getattr(parsed, "entries", []) or []
            if not entries:
                log.debug(f"feed {source}: no entries")
        except Exception as e:                               # noqa: BLE001
            log.debug(f"feed {source}: {e}")
            continue
        for e in entries[:25]:
            link = (getattr(e, "link", "") or "").strip()
            title = re.sub(r"\s+", " ", (getattr(e, "title", "") or "")).strip()
            if not title or not link or link in seen:
                continue
            seen.add(link)
            out.append({
                "title": title,
                "link": link,
                "source": source,
                "tier": TIER.get(source, DEFAULT_TIER),
                "summary": re.sub(r"<[^>]+>", " ",
                                  getattr(e, "summary", "") or "")[:600].strip(),
                "published": _published(e),
            })
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    fresh = [a for a in out if not a["published"] or a["published"] >= cutoff]
    log.info(f"brief: {len(out)} articles fetched, {len(fresh)} inside {hours}h")
    return fresh


def _published(entry) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:                                # noqa: BLE001
                pass
    return None


# ── clustering ──────────────────────────────────────────────────────────────
def _tokens(text: str) -> set[str]:
    words = re.split(r"[^a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) > 2 and w not in _STOP}


def _names_midsentence(text: str) -> set[str]:
    """Proper nouns EXCLUDING the first word of each sentence.

    Every bullet starts with a capital, so a plain capitalised-word scan treats
    "Higher", "Diplomatic", "Using" and "Strong" as names and rejects a
    perfectly good event. Six of eight events failed this way on the first live
    run. A real name almost always appears mid-sentence somewhere too, so
    dropping sentence-initial words costs the check very little and removes
    nearly all of its false positives.
    """
    out: set[str] = set()
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        words = sentence.strip().split()
        for w in words[1:]:                       # skip the sentence opener
            m = re.match(r"^([A-Z][a-zA-Z&.\u2011-]{2,})", w)
            if m:
                out.add(m.group(1).rstrip("."))
    return out


def _proper_nouns(text: str) -> set[str]:
    """Capitalised words, minus sentence-initial ones. A crude entity proxy —
    two headlines about the same event almost always share a name."""
    return {w for w in re.findall(r"\b[A-Z][a-zA-Z&.]{2,}\b", text or "")
            if w.lower() not in _STOP}


def similarity(a: dict, b: dict) -> float:
    """0-1. Token overlap, with shared proper nouns weighted higher.

    Deliberately conservative — the spec is explicit that keeping two related
    stories apart is better than merging two unrelated ones, and a merged event
    silently attributes one outlet's reporting to another's story.
    """
    ta, tb = _tokens(a["title"]), _tokens(b["title"])
    if not ta or not tb:
        return 0.0
    shared = ta & tb
    # ABSOLUTE gate, not a ratio. One shared word is a coincidence — "India
    # built the world's biggest digital payments miracle", "India's
    # microfinance overhang is easing" and "India's unemployment rate falls"
    # share exactly one token and are three different stories. Two specific
    # shared terms ({sensex, nifty}, {trump, iran}) is an event.
    #
    # A ratio cannot express this: Jaccard scored that same Sensex pair 0.087
    # purely because both headlines are long, so any threshold high enough to
    # block the India merge also blocked every genuine one.
    # Counted on DISTINCTIVE tokens only — see _WEAK.
    if len(shared - _WEAK) < 2:
        return 0.0
    # Overlap coefficient over the SHORTER headline, so a terse wire headline
    # and a long explanatory one can still match on the same event.
    overlap = len(shared) / min(len(ta), len(tb))
    # A shared name corroborates; it never carries a merge alone. Scored over
    # the union so one common proper noun cannot reach 1.0.
    pa, pb = _proper_nouns(a["title"]), _proper_nouns(b["title"])
    ent = len(pa & pb) / len(pa | pb) if (pa and pb) else 0.0
    jac = overlap
    # Time proximity is a tie-breaker, never a reason to merge on its own.
    close = 1.0
    if a.get("published") and b.get("published"):
        hrs = abs((a["published"] - b["published"]).total_seconds()) / 3600
        close = 1.0 if hrs <= 24 else 0.6
    return (jac * 0.55 + ent * 0.45) * close


def cluster(articles: list[dict]) -> list[list[dict]]:
    """Single-link clustering over `similarity`. Order-independent enough for
    a daily batch, and cheap: n is in the low hundreds."""
    clusters: list[list[dict]] = []
    for art in sorted(articles, key=lambda a: (a["tier"], a["title"])):
        placed = False
        for c in clusters:
            # Compared against the cluster SEED only, never against any member.
            # Single-link chaining is what built that six-article "India"
            # cluster: A resembled B and B resembled C, so C joined even though
            # A and C had nothing in common. Requiring similarity to one fixed
            # representative keeps every member genuinely about the same event.
            if similarity(art, c[0]) >= MERGE_THRESHOLD:
                c.append(art)
                placed = True
                break
        if not placed:
            clusters.append([art])
    return clusters


# ── scoring ─────────────────────────────────────────────────────────────────
_MARKET_WORDS = re.compile(
    r"\b(fed|rbi|ecb|rate|inflation|cpi|gdp|yield|bond|currency|dollar|rupee|"
    r"oil|crude|gold|tariff|sanction|recession|stimulus|default|bank|earnings|"
    r"policy|budget|deficit|trade war)\b", re.I)


def importance(cluster_: list[dict]) -> int:
    """1-5. Deterministic and explainable — never a model's opinion.

    Source COUNT is the strongest signal that something matters (independent
    desks each judged it worth covering), tier quality next, then whether the
    language is market-moving. Explicitly NOT recency or article volume from a
    single prolific source, which is how a syndication burst outranks a
    central-bank decision.
    """
    sources = {a["source"] for a in cluster_}
    best_tier = min(a["tier"] for a in cluster_)
    text = " ".join(a["title"] + " " + (a.get("summary") or "") for a in cluster_)

    score = 0
    score += min(len(sources), 4)                 # 0-4
    score += {2: 2, 3: 1}.get(best_tier, 0)       # 0-2
    score += 2 if _MARKET_WORDS.search(text) else 0
    return max(1, min(5, round(score / 8 * 4) + 1))


def confidence(cluster_: list[dict]) -> str:
    """Independent corroboration and source quality — NOT importance.

    A single-source tier-3 story can be the most important thing that day and
    still be Low confidence; the spec insists those stay separable.
    """
    sources = {a["source"] for a in cluster_}
    best_tier = min(a["tier"] for a in cluster_)
    if len(sources) >= 3 and best_tier <= 2:
        return "High"
    if len(sources) >= 2 or best_tier <= 2:
        return "Medium"
    return "Low"


def categorize(cluster_: list[dict]) -> str:
    text = " ".join(a["title"] + " " + (a.get("summary") or "") for a in cluster_)
    for cat, pat in CAT_RULES:
        if re.search(pat, text, re.I):
            return cat
    return "Markets & Economy"


# ── AI structuring ──────────────────────────────────────────────────────────
_PROMPT = """You are a wire-service editor. Below is EVERY article covering one \
event. Write a factual briefing entry using ONLY what these articles say.

Return ONLY valid JSON, no markdown fence, no preamble:
{"headline":"factual, max 12 words, not clickbait",
 "bullets":["3-5 short factual strings, each adding NEW information"],
 "whyItMatters":"1-2 plain-English sentences on why this matters",
 "marketImpact":[{"asset":"e.g. USD, Indian equities, Gold","direction":"Positive|Negative|Mixed|Neutral|Unclear"}],
 "watchNext":"what to watch next, or omit the key entirely"}

Hard rules:
- Never state a fact, number, date, quote or name that is not in the articles below.
- Every bullet must add something the previous bullets did not say.
- No generic openers ("This is significant because"), no adjectives for effect,
  no fake certainty.
- Omit marketImpact entirely unless the articles give real evidence of it.
  "Unclear" is an honest answer; inventing a direction is not.
- If the articles disagree, say so rather than silently picking one.

ARTICLES:
{articles}"""


def structure(cluster_: list[dict], timeout: int = 20) -> dict | None:
    """One model call per event -> schema-validated dict, or None.

    None is a normal outcome, not an error: the caller falls back to the
    deterministic summary. Nothing this returns is trusted until qa_reject has
    looked at it against the source text.
    """
    try:
        import newspaper
        if not getattr(newspaper, "GROQ_KEY", ""):
            return None
        blob = "\n\n".join(
            f"[{a['source']}] {a['title']}\n{(a.get('summary') or '')[:400]}"
            for a in cluster_[:6])
        raw = newspaper.groq_complete(_PROMPT.replace("{articles}", blob),
                                      max_tokens=420)
        if not raw:
            return None
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
        i, j = raw.find("{"), raw.rfind("}")
        if i < 0 or j < 0:
            return None
        return json.loads(raw[i:j + 1])
    except Exception as e:                                   # noqa: BLE001
        log.debug(f"structure failed: {e}")
        return None


# ── QA gate ─────────────────────────────────────────────────────────────────
# Capitalised words that are roles, corporate suffixes, calendar terms or
# generic nouns rather than the name of a person, company or place. Without
# this the name check fires on "Minister" or "Management" and never reaches the
# invented "Trudeau" beside them.
_TITLE_WORDS = {
    "president", "prime", "minister", "chief", "executive", "officer", "chair",
    "chairman", "chairwoman", "governor", "secretary", "director", "deputy",
    "management", "asset", "group", "limited", "ltd", "inc", "corp", "company",
    "bank", "capital", "holdings", "partners", "fund", "markets", "market",
    "exchange", "board", "committee", "ministry", "department", "agency",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "treasury", "yields", "stocks", "shares", "oil", "gold", "index",
    "street", "wall", "north", "south", "east", "west", "central",
}

def _norm(t: str) -> str:
    """Collapse the unicode punctuation models emit into ASCII equivalents."""
    return (t or "").replace("\u202f", " ").replace("\xa0", " ") \
                    .replace("\u2009", " ").replace("\u2013", "-") \
                    .replace("\u2014", "-").replace("\u2011", "-")


_NUM = re.compile(r"\b\d[\d,]*\.?\d*\s*(?:%|percent|bn|billion|mn|million|cr|crore|bps)?\b", re.I)


def qa_reject(ev: dict, source_text: str) -> str | None:
    """Reason this AI event must NOT publish, or None if it passes.

    The last line of defence. A failure here costs the event its generated
    prose and nothing else — it falls back to the deterministic summary — so
    the gate is deliberately strict.
    """
    head = (ev.get("headline") or "").strip()
    if not head:
        return "no headline"
    if len(head.split()) > 14:
        return f"headline too long ({len(head.split())} words)"

    bullets = [b.strip() for b in (ev.get("bullets") or []) if isinstance(b, str) and b.strip()]
    if len(bullets) < 2:
        return f"only {len(bullets)} usable bullet(s)"

    norm = [re.sub(r"[^a-z0-9 ]", "", b.lower()) for b in bullets]
    if len(set(norm)) != len(norm):
        return "duplicate bullets"
    for a_i in range(len(norm)):
        for b_i in range(a_i + 1, len(norm)):
            ta, tb = set(norm[a_i].split()), set(norm[b_i].split())
            if ta and tb and len(ta & tb) / len(ta | tb) > 0.75:
                return "near-duplicate bullets"

    banned = re.compile(r"\b(this is significant because|it is important to note|"
                        r"in conclusion|overall,|needless to say)\b", re.I)
    if banned.search(" ".join(bullets) + " " + (ev.get("whyItMatters") or "")):
        return "generic filler phrasing"

    # Joined as SENTENCES, not with a space. Bullets carry no terminating
    # punctuation, so a space-join fused them into one long sentence and every
    # bullet after the first lost its sentence-opener protection — which is how
    # "Higher costs are expected" was rejected for the invented name "Higher".
    parts = [p.rstrip(". ") for p in bullets + [ev.get("whyItMatters") or "", head] if p]
    out_text = ". ".join(parts) + "."

    # Every number in the output must appear in the source material. This is
    # the single most valuable check here — an invented figure is the failure
    # mode that would do real damage in a finance product.
    #
    # Compared on DIGITS ONLY, after unicode normalisation. The model returns
    # "4,895\u202fcrore" (narrow no-break space) where the source has
    # "4,895 crore", and a literal comparison rejected that correct event as a
    # hallucination. Normalising is not laxity — the digits still have to be
    # there — it just stops punctuation deciding the verdict.
    src_digits = set(re.findall(r"\d[\d,.]*", _norm(source_text)))
    src_digits_bare = {d.replace(",", "").rstrip(".") for d in src_digits}
    for n in re.findall(r"\d[\d,.]*", _norm(out_text)):
        bare = n.replace(",", "").rstrip(".")
        if len(bare) < 2:
            continue                      # single digits: too noisy to gate
        if bare not in src_digits_bare:
            return f"number {n!r} not present in the source articles"

    # Every NAME in the output must appear in the source material too. Numbers
    # alone were not enough: one run wrote "Canadian Prime Minister Justin
    # Trudeau intends to speak with President Biden" from an article naming
    # Mark Carney — no invented digits, two invented people. A briefing that
    # attributes a statement to the wrong head of government is worse than one
    # that says nothing.
    # Source side keeps EVERY capitalised word — a name really can open a
    # source headline. Output side skips sentence openers. Asymmetric on
    # purpose: it makes the check harder to trip and no easier to fool.
    # _norm on BOTH sides. The model emits U+2011 non-breaking hyphens, so
    # "US\u2011Iran", "SEBI\u2011registered" and "High\u2011frequency" did not match
    # the plain "US-Iran" in the source and were rejected as invented names.
    # The same normalisation already guards the number check.
    src_norm = _norm(source_text)
    src_names = {w.lower() for w in _proper_nouns(src_norm)}
    src_lower = src_norm.lower()
    for name in _names_midsentence(_norm(out_text)):
        low = name.lower().rstrip(".")
        if (low in src_names or low in _STOP or low in _TITLE_WORDS
                or len(low) < 4 or low in src_lower):
            continue
        # Demonyms and inflections: "Canadian" from "Canada", "Israeli" from
        # "Israel". Matching on a shared stem stops the gate blaming a
        # legitimate adjective and lets it name the actual invention instead.
        stem = low[:5]
        if any(n.startswith(stem) for n in src_names):
            continue
        # Hyphenated compounds ("US-Iran", "SEBI-registered") are two tokens
        # wearing one hat. If every part is accounted for, the compound is too.
        parts_ok = [p for p in re.split(r"[-/]", low) if len(p) > 2]
        if parts_ok and all(p in src_lower for p in parts_ok):
            continue
        return f"name {name!r} not present in the source articles"

    mi = ev.get("marketImpact")
    if mi:
        if not isinstance(mi, list):
            return "marketImpact is not a list"
        for m in mi:
            if not isinstance(m, dict) or not (m.get("asset") or "").strip():
                return "marketImpact entry with no asset"
            if m.get("direction") not in ("Positive", "Negative", "Mixed",
                                          "Neutral", "Unclear"):
                return f"invalid market direction {m.get('direction')!r}"
    return None


def deterministic_summary(cluster_: list[dict]) -> dict:
    """What the section shows when the model is unavailable or rejected.

    Plainer, never invented: the headline is the highest-tier outlet's own, and
    the bullets are the OTHER outlets' headlines, which is genuinely useful —
    it shows how differently the same event was framed.
    """
    best = sorted(cluster_, key=lambda a: (a["tier"], -len(a["title"])))[0]
    others, seen = [], {re.sub(r"[^a-z0-9]", "", best["title"].lower())}
    for a in cluster_:
        k = re.sub(r"[^a-z0-9]", "", a["title"].lower())
        if k in seen:
            continue
        seen.add(k)
        others.append(f"{a['source']}: {a['title']}")
    return {
        "headline": best["title"][:140],
        "bullets": others[:4] or [(best.get("summary") or best["title"])[:220]],
        "whyItMatters": "",
        "marketImpact": [],
        "generated": False,
    }


# ── build ───────────────────────────────────────────────────────────────────
def build(max_events: int = MAX_EVENTS, use_ai: bool = True) -> dict:
    """The full pipeline. Returns the payload the section renders.

    {} means "nothing worth publishing" and drops the section from the nav,
    which is the correct empty state — a briefing with no events is not a
    briefing.
    """
    t0 = time.time()
    articles = fetch_articles()
    if not articles:
        log.warning("brief: no articles fetched")
        return {}

    clusters = cluster(articles)
    scored = sorted(
        ({"cluster": c, "importance": importance(c)} for c in clusters),
        key=lambda x: (-x["importance"], -len({a["source"] for a in x["cluster"]})))

    # Single-source, low-importance items are noise in a briefing. This is the
    # cost control too: only what survives here is ever sent to a model.
    kept = [s for s in scored
            if s["importance"] >= 2 or len({a["source"] for a in s["cluster"]}) > 1
            ][:max_events]

    events, ai_ok, ai_rejected, ai_none = [], 0, 0, 0
    for rank, s in enumerate(kept):
        c = s["cluster"]
        src_text = " ".join(a["title"] + " " + (a.get("summary") or "") for a in c)

        # Cost + rate control, and the spec's own rule: deep synthesis only for
        # selected events. Groq's on-demand tier is 8,000 tokens/minute and a
        # naive loop over 15 events burned it in seconds — 9 of 15 came back
        # 429 and silently fell back to deterministic copy. The top events get
        # written prose; the tail keeps the headline-derived summary, which is
        # honest and costs nothing.
        body, reason = None, None
        if use_ai and rank < AI_EVENT_BUDGET:
            if rank:
                time.sleep(AI_PACE_SECONDS)
            cand = structure(c)
            if cand:
                reason = qa_reject(cand, src_text)
                if reason:
                    ai_rejected += 1
                    log.info(f"brief: QA rejected — {reason}")
                else:
                    body = cand
                    body["generated"] = True
                    ai_ok += 1
            else:
                ai_none += 1
        if body is None:
            body = deterministic_summary(c)

        sources = []
        for a in sorted(c, key=lambda a: a["tier"]):
            if any(x["name"] == a["source"] for x in sources):
                continue
            sources.append({"name": a["source"], "url": a["link"],
                            "tier": a["tier"],
                            "published": a["published"].isoformat() if a["published"] else None})

        events.append({
            "id": hashlib.sha1(body["headline"].encode("utf-8")).hexdigest()[:12],
            "headline": body["headline"],
            "category": categorize(c),
            "importance": s["importance"],
            "confidence": confidence(c),
            "bullets": [b for b in (body.get("bullets") or []) if b][:5],
            "whyItMatters": body.get("whyItMatters") or "",
            "marketImpact": body.get("marketImpact") or [],
            "watchNext": body.get("watchNext") or "",
            "sources": sources,
            "source_count": len(sources),
            # Whether the prose was written by the model or assembled from the
            # headlines. Surfaced in the UI — a reader is entitled to know
            # which sentences a model wrote.
            "ai_generated": bool(body.get("generated")),
        })

    events.sort(key=lambda e: (-e["importance"], -e["source_count"]))
    by_cat: dict[str, list] = {}
    for e in events:
        by_cat.setdefault(e["category"], []).append(e)

    words = sum(len(" ".join(e["bullets"] + [e["whyItMatters"]]).split()) for e in events)
    payload = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "date": datetime.now(IST).strftime("%Y-%m-%d"),
        "events": events,
        "top": events[:QUICK_COUNT],
        "by_category": by_cat,
        "categories": [c for c in CATEGORIES if by_cat.get(c)],
        "stats": {
            "articles": len(articles),
            "clusters": len(clusters),
            "events": len(events),
            "sources": len({a["source"] for a in articles}),
            "ai_written": ai_ok,
            "ai_rejected": ai_rejected,
            "ai_unavailable": ai_none,
            "read_minutes": max(1, round(words / 200)),
            "build_seconds": round(time.time() - t0, 1),
        },
    }
    log.info(f"brief: {len(articles)} articles -> {len(clusters)} clusters -> "
             f"{len(events)} events ({ai_ok} AI, {ai_rejected} QA-rejected, "
             f"{ai_none} no-model) in {payload['stats']['build_seconds']}s")
    return payload


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    import sys
    d = build(use_ai="--no-ai" not in sys.argv)
    if not d:
        print("nothing built")
        raise SystemExit(1)
    s = d["stats"]
    print(f"\n{s['articles']} articles from {s['sources']} sources "
          f"-> {s['clusters']} clusters -> {s['events']} events "
          f"(~{s['read_minutes']} min read)")
    print(f"AI: {s['ai_written']} written, {s['ai_rejected']} QA-rejected, "
          f"{s['ai_unavailable']} unavailable\n")
    for e in d["events"]:
        flag = "AI" if e["ai_generated"] else "--"
        print(f"[{e['importance']}] {flag} {e['category'][:16]:17} {e['headline'][:74]}")
        for b in e["bullets"][:3]:
            print(f"        - {b[:96]}")
        print(f"        {e['source_count']} sources · {e['confidence']} confidence · "
              + ", ".join(x["name"] for x in e["sources"][:4]))
        print()
