#!/usr/bin/env python3
"""
podcasts.py — the week's Indian long-form podcast episodes, with takeaways.

Everything here is REAL or it is not published
----------------------------------------------
Episode titles, guests, publish dates and links come from each channel's own
Atom feed (`youtube.com/feeds/videos.xml`). No API key, no scraping, no
inference. The takeaways are compressed from the publisher's OWN episode
description and nothing else — never from the title, never from what a model
happens to know about the guest. An episode whose description carries no real
prose is DROPPED rather than described, because the alternative is inventing
claims and attributing them to a named person who did not make them.

Two guards exist because of one specific failure. Resolving the handle
`@WTFisWithNikhilKamath` returned a channel id that served movie trailers and
a Doraemon film. A channel id is not a promise about whose content it is, so:

  · every channel carries the author name we expect, and a feed whose
    <author><name> no longer matches is dropped, not published;
  · an entry needs real description prose to survive at all.

Both fail closed. A shorter list is correct; a list with someone else's video
in it wearing Raj Shamani's name is not.

Weekly by design — see WEEK_KEY in newspaper.get_podcasts().
"""
from __future__ import annotations

import logging
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

NS = {"a": "http://www.w3.org/2005/Atom", "m": "http://search.yahoo.com/mrss/"}
FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
UA = {"User-Agent": "Mozilla/5.0 (compatible; DailySignal/1.0)"}

# (channel_id, expected author, show name, category)
# Channel ids are pinned, not resolved from handles at runtime: handle
# resolution is what produced the Doraemon channel above, and a pinned id
# paired with an author check fails closed instead.
CHANNELS = [
    ("UCzwCEE_PchiBULMnAJqhGVg", "Raj Shamani",        "Figuring Out",      "Business"),
    ("UCPxMZIFE856tbTfdkdjzTSQ", "BeerBiceps",         "BeerBiceps",        "Overall"),
    ("UCneyi-aYq4VIBYIAQgWmk_w", "Ranveer Allahbadia", "The Ranveer Show",  "Philosophy"),
    ("UCRzYN32xtBf3Yxsx5BvJWJw", "warikoo",            "Ankur Warikoo",     "Career"),
    ("UCcYzLCs3zrQIBVHYA1sK2sw", "Sadhguru",           "Sadhguru",          "Philosophy"),
    ("UCJQeGpaGN5sR_CHA-UKu4PQ",
     "The Habit Coach | Alex Poole",                   "The Habit Coach",   "Health"),
]
# Zerodha was here and is not a podcast — its channel is a daily NIFTY/BANK
# NIFTY outlook. It posted twice a day and took two of ten slots with
# "Analysis for Tomorrow", which is both off-topic for this section and
# already covered by the rest of the page.

MAX_EPISODES = 10
MIN_PROSE = 180        # chars of real description prose before we'll publish one
MAX_AGE_DAYS = 90      # a "this week" list must not surface a 4-month-old upload

# Lines that are plumbing, not content. Checked case-insensitively as a
# prefix/substring on a single line — these are the standing promo blocks
# every one of these channels carries above the actual synopsis.
NOISE = (
    "subscribe", "follow us", "follow me", "check out my", "check out our",
    "order '", "buy now", "download", "instagram", "twitter", "linkedin",
    "facebook", "telegram", "whatsapp", "sponsor", "use code", "coupon",
    "timestamps", "chapters", "0:00", "00:00", "disclaimer", "copyright",
    "all rights reserved", "for business", "for collaborations", "contact us",
    "watch the full", "playlist", "part 1", "part 2", "click here",
    "join our", "our other youtube", "in this episode of", "app:",
    # Observed leaking through on the first live run. Brand and product names
    # rather than categories, because these channels advertise inside the
    # synopsis rather than in a separate block.
    "social media", "skillhouse", "webveda", "queries email", "email:",
    "nationwide search", "subscription", "our daily show", "we provide expert",
    "join sadhguru", "register", "enrol", "enroll", "book your", "tickets",
    "merch", "course", "webinar", "giveaway", "link in", "bio",
    # Second live run. Affiliate disclosures and payment-support boilerplate
    # are prose, pass every structural test, and are the LAST thing a
    # "key learnings" list should contain.
    "payment", "tagmango", "referral", "affiliate", "i stand to make",
    "the above link", "these links", "stream ", "listen on", "available on",
    "spotify", "apple podcast", "jiosaavn", "amazon music",
    "episodes on", "advice by", "ditto", "powered by", "brought to you",
)
# Chapter lists ("03:27 - Why Leave Your Company") read as prose to every rule
# above. Two or more clock stamps on one line is a table of contents.
# A single "03:27 - " chapter marker is enough; requiring two missed the
# line that opened with a promo and carried one stamp.
TIMESTAMPS = re.compile(r"\d{1,2}:\d{2}\s*[-–—]")
# The promo blocks on the Hindi channels are written in Devanagari, so an
# English substring list never sees them. Any line carrying a Devanagari
# character AND a Latin brand word is a bilingual promo line — that exact
# shape ("BeerBiceps SkillHouse को Social Media पर Follow करे") is what put a
# subscribe prompt in the takeaways on the first run.
DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def _is_noise(text: str) -> bool:
    low = (text or "").lower()
    if any(n in low for n in NOISE):
        return True
    if TIMESTAMPS.search(text or ""):
        return True
    if DEVANAGARI.search(text or "") and re.search(
            r"follow|social|subscribe|channel|email|link", low):
        return True
    return False

# Clip-style uploads. These channels post 60-second cuts from an episode with
# the same "ft. Guest" shape as the episode itself; they are not episodes and
# padding the list with them is how ten slots become three real ones.
CLIPPY = re.compile(r"#shorts|#short\b|\bshorts\b", re.I)


def _get(url: str, timeout: int = 20) -> bytes:
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=UA), timeout=timeout).read()


def _prose(desc: str) -> str:
    """The publisher's own synopsis, with the promo furniture removed.

    Returns '' when the description is all plumbing — which is the signal to
    drop the episode rather than describe it from its title.
    """
    out = []
    for raw in (desc or "").split("\n"):
        line = raw.strip()
        if not line or "http" in line or len(line) < 40:
            continue
        if _is_noise(line):
            continue
        # A line that is mostly punctuation/emoji separators is a divider.
        if sum(c.isalpha() for c in line) < len(line) * 0.6:
            continue
        out.append(line)
    return " ".join(out).strip()


def _sentences(text: str, n: int = 2) -> list[str]:
    """First n whole sentences of real prose. Deterministic fallback path."""
    parts = re.split(r"(?<=[.!?])\s+", text)
    # Filter at SENTENCE level too. Line-level filtering alone let a promo
    # sentence ride along inside an otherwise-clean line, which is how
    # "WebVeda is the smartest subscription" became a "key learning".
    keep = [p.strip() for p in parts if len(p.strip()) > 35 and not _is_noise(p)]
    return keep[:n]


def _takeaways(title: str, prose: str, ai) -> list[str]:
    """Two takeaways, compressed from the DESCRIPTION only.

    `ai` is newspaper.groq_complete, injected so this module stays importable
    without pulling the whole newspaper in. When the key is absent — which is
    the normal state locally — this falls back to the publisher's own opening
    sentences verbatim. Both paths are the publisher's claims, not ours.
    """
    if ai:
        try:
            out = ai(
                "Below is a podcast episode description written by its publisher. "
                "Extract at most 2 concrete takeaways that are STATED IN THE TEXT. "
                "Use only what the text says — do not add context, do not use prior "
                "knowledge of the guest, do not speculate. If the text does not "
                "support two, give one. One short line each, no numbering, no "
                "preamble, plain text.\n\n"
                f"TITLE: {title}\n\nDESCRIPTION:\n{prose[:1800]}",
                max_tokens=150,
            )
            lines = [re.sub(r"^\s*[-*•\d.)]+\s*", "", l).strip()
                     for l in (out or "").split("\n")]
            lines = [l for l in lines if 25 < len(l) < 240 and not _is_noise(l)]
            if lines:
                return lines[:2]
        except Exception as e:
            log.warning(f"podcasts: takeaway generation failed — {e}")
    return _sentences(prose, 2)


def _guest(title: str) -> str:
    """Guest name where the title states one. Never guessed."""
    m = re.search(r"\b(?:ft\.?|feat\.?|with)\s+([A-Z][\w.'-]+(?:\s+[A-Z][\w.'-]+){0,3})",
                  title)
    return m.group(1).strip() if m else ""


def _channel(cid: str, author: str, show: str, cat: str, ai) -> list[dict]:
    try:
        root = ET.fromstring(_get(FEED.format(cid)))
    except Exception as e:
        log.warning(f"podcasts: {show} feed unavailable — {e}")
        return []

    # The Doraemon guard. A pinned id that starts serving someone else is
    # dropped outright — publishing it under this show's name is the failure
    # this whole module is shaped around.
    got = (root.findtext("a:author/a:name", default="", namespaces=NS) or "").strip()
    if got.lower() != author.lower():
        log.error(f"podcasts: {show} ({cid}) now reports author {got!r}, "
                  f"expected {author!r} — dropped")
        return []

    eps = []
    for e in root.findall("a:entry", NS):
        title = (e.findtext("a:title", default="", namespaces=NS) or "").strip()
        if not title or CLIPPY.search(title):
            continue
        grp = e.find("m:group", NS)
        desc = grp.findtext("m:description", default="", namespaces=NS) if grp is not None else ""
        prose = _prose(desc)
        if len(prose) < MIN_PROSE:
            continue

        pub = (e.findtext("a:published", default="", namespaces=NS) or "")[:10]
        link = ""
        le = e.find("a:link", NS)
        if le is not None:
            link = le.get("href") or ""

        takeaways = _takeaways(title, prose, ai)
        # No surviving takeaway means every candidate line was promo. The ask
        # was "key learnings from each podcast", so an entry that cannot carry
        # one is dropped rather than listed as a bare title.
        if not takeaways:
            continue

        eps.append({
            "show": show, "author": author, "category": cat,
            "title": title, "guest": _guest(title),
            "published": pub, "link": link,
            "takeaways": takeaways,
        })
        # Two per show at most, so one prolific channel cannot own the list.
        if len(eps) >= 2:
            break
    return eps


def build(ai=None) -> dict:
    """The week's list. Always returns a dict; `ok` says whether to render it."""
    eps = []
    for cid, author, show, cat in CHANNELS:
        eps.extend(_channel(cid, author, show, cat, ai))

    # Drop anything stale before ranking. A weekly list surfacing a 4-month-old
    # upload reads as a broken feed, and The Habit Coach last posted in April.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)).strftime("%Y-%m-%d")
    eps = [e for e in eps if (e["published"] or "0000-00-00") >= cutoff]

    # Round-robin by show, NOT a global date sort. Sadhguru and BeerBiceps post
    # several times a day and Raj Shamani posts a long-form episode every other
    # day; sorting ten slots purely by date handed every slot to the daily
    # posters and dropped the actual interview podcasts off the list entirely.
    # One per show first, then second episodes, each pass newest-first.
    by_show = {}
    for e in sorted(eps, key=lambda x: x["published"] or "0000-00-00", reverse=True):
        by_show.setdefault(e["show"], []).append(e)

    ordered, rnd = [], 0
    while len(ordered) < MAX_EPISODES:
        wave = [v[rnd] for v in by_show.values() if len(v) > rnd]
        if not wave:
            break
        wave.sort(key=lambda x: x["published"] or "0000-00-00", reverse=True)
        ordered.extend(wave[:MAX_EPISODES - len(ordered)])
        rnd += 1
    eps = ordered

    return {
        "ok": bool(eps),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "episodes": eps,
        "shows": len({e["show"] for e in eps}),
        "source": "each channel's public Atom feed",
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    d = build()
    print(f"ok={d['ok']}  {len(d['episodes'])} episodes from {d['shows']} shows\n")
    for e in d["episodes"]:
        print(f"{e['published']}  [{e['category']:10}] {e['show']}")
        print(f"   {e['title'][:88]}")
        for t in e["takeaways"]:
            print(f"     · {t[:110]}")
        print()
