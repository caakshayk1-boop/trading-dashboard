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

NS = {"a": "http://www.w3.org/2005/Atom", "m": "http://search.yahoo.com/mrss/",
      # yt:videoId is what the Shorts check needs; the id in <a:id> is prefixed
      # and would have to be parsed out of "yt:video:XXXX".
      "yt": "http://www.youtube.com/xml/schemas/2015"}
FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
UA = {"User-Agent": "Mozilla/5.0 (compatible; DailySignal/1.0)"}

# (channel_id, expected author, show name, category)
# Channel ids are pinned, not resolved from handles at runtime: handle
# resolution is what produced the Doraemon channel above, and a pinned id
# paired with an author check fails closed instead.
CHANNELS = [
    # ── business ──
    ("UCzwCEE_PchiBULMnAJqhGVg", "Raj Shamani",        "Figuring Out",      "Business"),
    ("UCKZozRVHRYsYHGEyNKuhhdA", "Think School",       "Think School",      "Business"),
    ("UCRzYN32xtBf3Yxsx5BvJWJw", "warikoo",            "Ankur Warikoo",     "Career"),
    # ── money and investing ──
    ("UCqW8jxh4tH1Z1sWPbkGWL4g", "Akshat Shrivastava", "Akshat Shrivastava", "Investing"),
    ("UCe3qdG0A_gr-sEdat5y2twQ",
     "CA Rachana Phadke Ranade",                       "CA Rachana Ranade", "Investing"),
    ("UCwAdQUuPT6laN-AQR17fe1g", "Pranjal Kamra",      "Pranjal Kamra",     "Investing"),
    ("UCUUlw3anBIkbW9W44Y-eURw", "Zero1 by Zerodha",   "Zero1 by Zerodha",  "Money"),
    ("UCwVEhEzsjLym_u1he4XWFkg", "Finance With Sharan", "Finance With Sharan", "Money"),
    # ── society, geopolitics ──
    ("UC-CSyyi47VX1lD9zyeABW3w", "Dhruv Rathee",       "Dhruv Rathee",      "Society"),
    ("UC2bBsPXFWZWiBmkRiNlz8vg", "Abhijit Chavda",     "Abhijit Chavda",    "Geopolitics"),
    ("UC7sbc0Ed3_yMu-etXVpj7cg", "Kunal Kamra",        "Shut Up Ya Kunal",  "Society"),
    # ── mind, health, philosophy ──
    ("UCPxMZIFE856tbTfdkdjzTSQ", "BeerBiceps",         "BeerBiceps",        "Overall"),
    ("UCneyi-aYq4VIBYIAQgWmk_w", "Ranveer Allahbadia", "The Ranveer Show",  "Philosophy"),
    ("UCcYzLCs3zrQIBVHYA1sK2sw", "Sadhguru",           "Sadhguru",          "Philosophy"),
    ("UCBqFKDipsnzvJdt6UT0lMIg", "Sandeep Maheshwari", "Sandeep Maheshwari", "Motivation"),
    ("UCJQeGpaGN5sR_CHA-UKu4PQ",
     "The Habit Coach | Alex Poole",                   "The Habit Coach",   "Health"),
]
# Zerodha's main channel was here and is not a podcast — a daily NIFTY/BANK
# NIFTY outlook that posted twice a day and took two of ten slots with
# "Analysis for Tomorrow". Zero1 is Zerodha's actual long-form channel and is
# a different thing.
#
# HOW THIS LIST IS BUILT, and why it is shorter than the shortlist it came from.
# Every id above was resolved from the channel's CANONICAL url, then verified
# against the feed's own <author><name>, then eyeballed against its two most
# recent titles. Nine candidates were rejected at that last step and it is
# worth recording what they were, because each is a way this list goes wrong:
#
#   · WTF is / Nikhil Kamath — the handle resolved to a channel whose author
#     really is "Nikhil" and which serves Deepwater Horizon trailers and
#     "Stand by me | Doraemon (hindi)". This is the SAME channel named in the
#     docstring above. The author check passed it, because "Nikhil" is too
#     common a first name for a name check to be a content check.
#   · Ranveer's @TheRanveerShow handle resolved to author "Being Akash",
#     @AmanGupta_ to "Riddleverse", @DrVivekBindra to "Anup Singh" — handles
#     get re-registered, and none of those are who they claim.
#   · ANI Podcast, Raj Shamani, Think School, Shwetabh Gangwar and Abhijit
#     Chavda all have separate "Clips"/"Shorts" channels that a naive handle
#     lookup finds first. Pinning one would fill this section with the exact
#     content the Shorts guard below exists to remove.
#   · Saurabh Jain resolved to a channel whose only upload is "Snorkeling in
#     Kauai"; Gaurav Thakur to Bhojpuri stage shows; Labour Law Advisor and
#     Finology's resolved channels to #LLAShorts and a 3-video stub.
#
# Prakhar Gupta, Shwetabh Gangwar and Siddharth Warrier are genuinely wanted
# here and are ABSENT because their handles 404'd during resolution — they are
# not in this list rather than in it wrongly. Add them by resolving the
# canonical id and checking two recent titles by hand, never by guessing an id.

MAX_EPISODES = 20
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
    # Observed on the first run of the expanded channel list. Both of these
    # were published AS takeaways, which is the worst version of this bug —
    # an affiliate pitch presented as the episode's key learning:
    #   "👉 I use Vested to invest in US Stocks."
    #   "EXCLUSIVE $10 bonus on 1st Deposit if you sign up using this link"
    #   "👉Become the top 1% professional by learning AI today"
    # The arrow is the reliable one: on these channels 👉 marks a call to
    # action essentially without exception, never a point being made.
    "👉", "sign up", "signup", "bonus on", "1st deposit", "first deposit",
    "i use ", "become the top", "exclusive $", "get daily stock market",
    "use this link", "using this link", "my course", "masterclass",
    "limited seats", "early bird", "enrollment", "waitlist",
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow — the redirect itself is the answer we want."""
    def redirect_request(self, *a, **k):
        return None


_no_redirect = urllib.request.build_opener(_NoRedirect)


def _is_short(video_id: str) -> bool | None:
    """Is this video a YouTube Short? True / False / None when undetermined.

    CLIPPY above only catches a Short that SAYS so in its title, and most do
    not — "Power Of Dedication | Raj Shamani" is a 30-second Short and reads
    like an episode. That is why Shorts kept appearing in this section.

    The Atom feed carries no duration, so the signal comes from YouTube's own
    routing: ask for /shorts/<id> and do not follow the redirect.

        200  → the id really is a Short, YouTube served the Shorts player
        303  → not a Short, YouTube bounced us to /watch

    Verified against this exact failure: on Raj Shamani's feed the long-form
    interviews all answered 303 and the three #Shorts all answered 200.

    Returns None on a network error, and the caller KEEPS the episode in that
    case. A transient failure should not silently empty the section — the
    prose guard has already established this entry has a real synopsis, which
    a 30-second Short almost never does.
    """
    if not video_id:
        return None
    url = f"https://www.youtube.com/shorts/{video_id}"
    try:
        r = _no_redirect.open(urllib.request.Request(url, headers=UA), timeout=12)
        return r.status == 200
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            return False
        return None
    except Exception:
        return None


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

        # Shorts guard, deliberately AFTER the cheap filters. It is one HTTP
        # request per surviving candidate, and the prose requirement above has
        # already removed almost every Short for free — this only pays for the
        # handful that carry a real synopsis and are still 40 seconds long.
        vid = e.findtext("yt:videoId", default="", namespaces=NS) or ""
        if _is_short(vid) is True:
            log.info(f"podcasts: {show} — dropped Short {vid} ({title[:48]!r})")
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
