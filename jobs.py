#!/usr/bin/env python3
"""jobs.py — senior-finance job discovery engine.

Writes docs/jobs.json to the frozen schema in JOBS_CONTRACT.md. The renderer
half (newspaper.py) only READS that file; neither side may change a field name
without changing the contract first.

    python3 jobs.py          # full refresh, writes docs/jobs.json
    import jobs; jobs.build() # same pipeline, returns the dict

Pipeline
    discover → scrape → normalize → deduplicate → validate URLs
             → classify freshness → score → rank → diff → write

Design notes that matter
------------------------
*No fabrication.* Unknown is `null`, never a guess. A source that yields
nothing yields nothing — there is no synthetic filler anywhere in this file.
Every `application_url` is checked with a real request before it is called
verified. The entire point is that the apply links can be trusted.

*Endpoints are re-resolved, not hardcoded.* Career URLs rot. `discover()`
re-reads each employer's public careers page and pulls the live ATS link out
of it, so a tenant migration is picked up on the next run. The values in
SOURCES are a seed/fallback cache (all verified 2026-08-18), not an article of
faith; whatever discovery resolves wins, and the result is cached to
data/jobs_endpoints.json.

*Detail page is the canonical record.* A search-result card is never enough to
build a row from — it has no responsibilities, and this scores responsibilities
rather than titles. Cards are only used to decide which detail pages are worth
fetching (TITLE_PREFILTER), because Landmark alone posts 500+ roles and almost
none of them are finance.

*Firecrawl is optional.* The MCP tools are not available inside GitHub Actions,
so the Firecrawl-backed sources call the HTTP API directly with requests. When
FIRECRAWL_API_KEY is unset those sources degrade to status "error" and the run
still produces a valid file from the plain-requests sources.

*A failed source never blanks the dataset.* Previous jobs are carried forward
and the source is marked blocked/error with its old last_success intact.
"""

from __future__ import annotations

import hashlib
import html as _html
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Iterable
from urllib.parse import quote, urljoin, urlparse

import requests

log = logging.getLogger("jobs")

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(ROOT, "docs", "jobs.json")
ENDPOINT_CACHE = os.path.join(ROOT, "data", "jobs_endpoints.json")

UTC = timezone.utc

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = 30
DETAIL_WORKERS = 6
MAX_DETAIL_PER_SOURCE = 40

FIRECRAWL_API = "https://api.firecrawl.dev/v2/scrape"

# ---------------------------------------------------------------------------
# Source registry
# ---------------------------------------------------------------------------
# `endpoint` values were resolved live on 2026-08-18 and are the fallback used
# when runtime discovery cannot reach the employer's careers page. `discover`
# is the public page discovery re-reads to find the current ATS link.

SOURCES: list[dict[str, Any]] = [
    # -- Tier 1: employer career sites (source_confidence "high") -----------
    {"name": "Majid Al Futtaim", "kind": "employer", "adapter": "phenom",
     "group": "Majid Al Futtaim", "confidence": "high",
     "endpoint": {"host": "https://careers.majidalfuttaim.com", "locale": "en_global",
                  "country": "global", "site": "global/en"},
     "discover": "https://www.majidalfuttaim.com/en/people-and-careers"},

    {"name": "Chalhoub Group", "kind": "employer", "adapter": "teamtailor",
     "group": "Chalhoub", "confidence": "high",
     "endpoint": {"host": "https://careers.chalhoubgroup.com"},
     "discover": "https://www.chalhoubgroup.com/en/careers"},

    {"name": "GMG", "kind": "employer", "adapter": "teamtailor",
     "group": "GMG", "confidence": "high",
     "endpoint": {"host": "https://careers.gmg.com"},
     "discover": "https://www.gmg.com/career/"},

    {"name": "Al-Futtaim", "kind": "employer", "adapter": "successfactors",
     "group": "Al-Futtaim", "confidence": "high",
     "endpoint": {"host": "https://www.afuturewithus.com"},
     "discover": "https://www.alfuttaim.com/en/careers/"},

    {"name": "Abdul Latif Jameel", "kind": "employer", "adapter": "successfactors",
     "group": "Abdul Latif Jameel", "confidence": "high",
     "endpoint": {"host": "https://careers.alj.com"},
     "discover": "https://alj.com/en/about/careers/"},

    {"name": "Landmark Group", "kind": "employer", "adapter": "oracle",
     "group": "Landmark", "confidence": "high",
     "endpoint": {"host": "https://efhi.fa.em3.oraclecloud.com", "site": "CX_1"},
     "discover": "https://www.landmarkgroup.com/careers"},

    {"name": "Al Tayer", "kind": "employer", "adapter": "oracle",
     "group": "Al Tayer", "confidence": "high",
     "endpoint": {"host": "https://hchx.fa.em2.oraclecloud.com", "site": "CX_1"},
     "discover": "https://www.altayer.com/careers"},

    {"name": "Apparel Group", "kind": "employer", "adapter": "oracle",
     "group": "Apparel Group", "confidence": "high",
     "endpoint": {"host": "https://ediu.fa.em2.oraclecloud.com", "site": "CX_1"},
     "discover": "https://www.apparelgroup.com/en/careers/"},

    {"name": "Emaar", "kind": "employer", "adapter": "oracle",
     "group": "Emaar", "confidence": "high",
     "endpoint": {"host": "https://emhm.fa.em2.oraclecloud.com", "site": "CX_1001"},
     "discover": "https://www.emaar.com/en/careers"},

    {"name": "Jumeirah", "kind": "employer", "adapter": "oracle",
     "group": "Dubai Holding", "confidence": "high",
     "endpoint": {"host": "https://esbe.fa.em8.oraclecloud.com", "site": "CX_1"},
     "discover": "https://www.jumeirah.com/en/careers"},

    {"name": "Americana Restaurants", "kind": "employer", "adapter": "oracle",
     "group": "Americana", "confidence": "high",
     "endpoint": {"host": "https://fa-eucb-saasfaprod1.fa.ocs.oraclecloud.com",
                  "site": "Americana"},
     "discover": "https://www.americanarestaurants.com/people/"},

    # Aviation. Added 2026-08-19 on request, and because the Abu Dhabi
    # employers were entirely absent — every existing source is Dubai-weighted,
    # so "UAE coverage" in practice meant Dubai coverage.
    #
    # Etihad runs SmartRecruiters. Verified before shipping: 92 live postings,
    # 51 of them Abu Dhabi, 8 surviving the finance title prefilter.
    #
    # Emirates Group is NOT here yet, deliberately. Its careers site advertises
    # Taleo in the page source but actually posts through Avature
    # (emiratesjobs.avature.net/careersmarketplace, SearchJobs returns 200).
    # That needs its own adapter, and registering it now would add a source
    # that returns zero rows — which would push the Careers coverage ratio
    # down while pretending to be progress. Left out until it is built.
    {"name": "Etihad Airways", "kind": "employer", "adapter": "smartrecruiters",
     "group": "Etihad", "confidence": "high",
     "endpoint": {"slug": "EtihadAirways5"},
     "discover": "https://careers.etihad.com"},

    {"name": "Aldar", "kind": "employer", "adapter": "lever",
     "group": "Aldar", "confidence": "high",
     "endpoint": {"slug": "aldar"},
     "discover": "https://www.aldar.com/en/careers"},

    # Alshaya fronts its careers site with a bot filter that returns 403 to
    # plain requests. Routed through Firecrawl, which means it only runs when
    # FIRECRAWL_API_KEY is set.
    {"name": "Alshaya Group", "kind": "employer", "adapter": "firecrawl_html",
     "group": "Alshaya", "confidence": "high",
     "endpoint": {"url": "https://www.alshaya.com/en/careers/vacancies",
                  "link_re": r"vacancies\?job=(\d+)",
                  "detail_tpl": "https://www.alshaya.com/en/careers/vacancies?job={id}"},
     "discover": "https://www.alshaya.com/en/careers"},

    # Resolved to an ATS only at runtime — no verified endpoint as of the last
    # discovery pass, so these attempt discovery and report honestly if it fails.
    {"name": "Azadea", "kind": "employer", "adapter": "discover_only",
     "group": "Azadea", "confidence": "high", "endpoint": {},
     "discover": "https://www.azadea.com/en/careers"},

    {"name": "Lulu Group", "kind": "employer", "adapter": "discover_only",
     "group": "Lulu", "confidence": "high", "endpoint": {},
     "discover": "https://www.lulugroupinternational.com/careers.html"},

    {"name": "Cenomi", "kind": "employer", "adapter": "discover_only",
     "group": "Cenomi", "confidence": "high", "endpoint": {},
     "discover": "https://www.cenomi.com/careers"},

    # -- Tier 2: aggregators and recruiters --------------------------------
    {"name": "LinkedIn", "kind": "aggregator", "adapter": "linkedin",
     "group": None, "confidence": "medium", "endpoint": {}, "discover": None},

    {"name": "Michael Page", "kind": "recruiter", "adapter": "michaelpage",
     "group": None, "confidence": "medium",
     "endpoint": {"host": "https://www.michaelpage.ae"}, "discover": None},

    {"name": "GulfTalent", "kind": "aggregator", "adapter": "firecrawl_html",
     "group": None, "confidence": "medium",
     "endpoint": {"url": "https://www.gulftalent.com/uae/jobs/finance-jobs",
                  "link_re": r"(https://www\.gulftalent\.com/[a-z\-]+/jobs/[a-z0-9\-]+-\d+)",
                  "detail_tpl": "{id}"},
     "discover": None},

    {"name": "Bayt", "kind": "aggregator", "adapter": "firecrawl_html",
     "group": None, "confidence": "low",
     "endpoint": {"url": "https://www.bayt.com/en/uae/jobs/finance-manager-jobs/",
                  "link_re": r'href="(/en/uae/jobs/[a-z0-9\-]+-\d+/)"',
                  "detail_tpl": "https://www.bayt.com{id}"},
     "discover": None},

    {"name": "Indeed", "kind": "aggregator", "adapter": "firecrawl_html",
     "group": None, "confidence": "low",
     "endpoint": {"url": "https://ae.indeed.com/jobs?q=finance+manager&l=Dubai",
                  "link_re": r"/rc/clk\?jk=([0-9a-f]{16})",
                  "detail_tpl": "https://ae.indeed.com/viewjob?jk={id}"},
     "discover": None},
]

# Search terms pushed into each source's own keyword search.
SEARCH_TERMS = [
    "finance manager", "financial planning and analysis", "fp&a",
    "commercial finance", "financial controller", "head of finance",
    "business finance", "finance business partner",
]

# A card only earns a detail fetch if its title looks finance-ish. Without this
# the run would pull thousands of retail-store detail pages to find a dozen
# finance roles.
TITLE_PREFILTER = re.compile(
    r"\b(financ\w*|fp&a|fpna|controller|controlling|treasur\w*|account\w*|"
    r"commercial|business\s+partner\w*|planning\s*&?\s*analysis|budget\w*|"
    r"cost\w*|audit\w*|tax|cfo|revenue|profit)\b", re.I)

# ---------------------------------------------------------------------------
# Geography
# ---------------------------------------------------------------------------

COUNTRY_ALIASES = {
    "uae": "UAE", "united arab emirates": "UAE", "u.a.e": "UAE", "ae": "UAE",
    "emirates": "UAE",
    "saudi arabia": "Saudi Arabia", "saudi": "Saudi Arabia", "ksa": "Saudi Arabia",
    "sa": "Saudi Arabia", "kingdom of saudi arabia": "Saudi Arabia",
    "malaysia": "Malaysia", "my": "Malaysia",
    "oman": "Oman", "om": "Oman", "sultanate of oman": "Oman",
}

CITY_COUNTRY = {
    "dubai": "UAE", "abu dhabi": "UAE", "sharjah": "UAE", "ajman": "UAE",
    "al ain": "UAE", "fujairah": "UAE", "ras al-khaimah": "UAE",
    "ras al khaimah": "UAE", "umm al quwain": "UAE", "dic": "UAE",
    "riyadh": "Saudi Arabia", "jeddah": "Saudi Arabia", "khobar": "Saudi Arabia",
    "al khobar": "Saudi Arabia", "dammam": "Saudi Arabia", "medina": "Saudi Arabia",
    "mecca": "Saudi Arabia", "makkah": "Saudi Arabia", "al ahsa": "Saudi Arabia",
    "kuala lumpur": "Malaysia", "selangor": "Malaysia", "petaling jaya": "Malaysia",
    "shah alam": "Malaysia", "penang": "Malaysia", "johor": "Malaysia",
    "muscat": "Oman", "salalah": "Oman", "sohar": "Oman",
    # Non-target cities these employers also post in. They must resolve to their
    # OWN country — before this, "Cairo" fell through every lookup and the
    # description-text fallback labelled an Egyptian role "UAE".
    "cairo": "Egypt", "alexandria": "Egypt", "giza": "Egypt",
    "doha": "Qatar", "manama": "Bahrain", "kuwait city": "Kuwait",
    "amman": "Jordan", "beirut": "Lebanon", "casablanca": "Morocco",
    "istanbul": "Turkey", "karachi": "Pakistan", "colombo": "Sri Lanka",
    "mumbai": "India", "bangalore": "India", "bengaluru": "India",
    "new delhi": "India", "gurgaon": "India", "chennai": "India",
    "singapore": "Singapore", "jakarta": "Indonesia", "bangkok": "Thailand",
    "london": "United Kingdom", "paris": "France",
}

# Countries these employers post in beyond the four targets. Present so a real
# country name resolves to itself instead of falling through to a guess.
OTHER_COUNTRIES = {
    "egypt", "qatar", "bahrain", "kuwait", "jordan", "lebanon", "iraq",
    "morocco", "tunisia", "turkey", "pakistan", "india", "sri lanka",
    "singapore", "indonesia", "thailand", "vietnam", "philippines", "china",
    "united kingdom", "france", "germany", "spain", "italy", "georgia",
    "azerbaijan", "kazakhstan", "armenia", "cyprus", "kenya", "nigeria",
    "south africa", "united states", "canada", "australia",
}

TARGET_COUNTRIES = {"UAE", "Saudi Arabia", "Malaysia", "Oman"}
GCC = {"UAE", "Saudi Arabia", "Oman", "Kuwait", "Qatar", "Bahrain"}
SEA = {"Malaysia", "Indonesia", "Singapore", "Thailand", "Vietnam", "Philippines"}

# Peer multi-country retail groups — the resume transfers directly.
PEER_RETAIL = {
    "alshaya", "majid al futtaim", "chalhoub", "al-futtaim", "al futtaim",
    "gmg", "landmark", "americana", "apparel group", "azadea", "lulu",
    "al tayer", "cenomi", "carrefour", "max fashion", "babyshop", "centrepoint",
}

EMPLOYER_BRAND = {
    "majid al futtaim": 90, "chalhoub group": 88, "al-futtaim": 88,
    "alshaya group": 86, "landmark group": 82, "gmg": 80, "al tayer": 82,
    "apparel group": 76, "americana restaurants": 80, "azadea": 74,
    "lulu group": 72, "cenomi": 76, "emaar": 88, "aldar": 86,
    "jumeirah": 84, "abdul latif jameel": 82,
}


# ===========================================================================
# HTTP
# ===========================================================================

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


SESSION = _session()


def _get(url: str, **kw) -> requests.Response:
    kw.setdefault("timeout", HTTP_TIMEOUT)
    kw.setdefault("allow_redirects", True)
    return SESSION.get(url, **kw)


def _post_json(url: str, payload: dict, **kw) -> requests.Response:
    kw.setdefault("timeout", HTTP_TIMEOUT)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    headers.update(kw.pop("headers", {}))
    return SESSION.post(url, json=payload, headers=headers, **kw)


class SourceBlocked(Exception):
    """The site actively refused us (403/429/captcha). Not a code bug."""


class SourceError(Exception):
    """Anything else that stopped this source from producing rows."""


def _check(resp: requests.Response, what: str) -> requests.Response:
    if resp.status_code in (401, 403, 429) or resp.status_code == 451:
        raise SourceBlocked(f"{what}: HTTP {resp.status_code} {resp.reason}")
    if resp.status_code >= 400:
        raise SourceError(f"{what}: HTTP {resp.status_code} {resp.reason}")
    return resp


# ===========================================================================
# Text helpers
# ===========================================================================

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_LI_RE = re.compile(r"<li[^>]*>(.*?)</li>", re.I | re.S)


def strip_html(raw: str | None) -> str:
    """HTML → readable plain text, with list items kept as separate lines."""
    if not raw:
        return ""
    txt = re.sub(r"<\s*(br|/p|/div|/li|/tr)\s*/?>", "\n", raw, flags=re.I)
    txt = _TAG_RE.sub(" ", txt)
    txt = _html.unescape(txt)
    txt = re.sub(r"[ \t ]+", " ", txt)
    txt = re.sub(r"\n\s*\n\s*\n+", "\n\n", txt)
    return txt.strip()


def html_bullets(raw: str | None) -> list[str]:
    """Pull <li> items out of an HTML description."""
    if not raw:
        return []
    out = []
    for m in _LI_RE.finditer(raw):
        t = strip_html(m.group(1))
        t = _WS_RE.sub(" ", t).strip(" .;•-•")
        if 8 <= len(t) <= 400:
            out.append(t)
    return out


def text_bullets(text: str) -> list[str]:
    """Fallback bullet extraction from already-plain text."""
    out = []
    for line in (text or "").splitlines():
        t = line.strip()
        if not t:
            continue
        t = re.sub(r"^[•●▪\-\*·◦o]\s+", "", t)
        t = t.strip(" .;")
        if 15 <= len(t) <= 400 and not t.endswith(":"):
            out.append(t)
    return out


def normalize_title(title: str | None) -> str:
    """Lowercased, de-noised title used for fingerprints and matching."""
    if not title:
        return ""
    t = title.lower()
    t = re.sub(r"[|/\\(\)\[\]{}<>]", " ", t)
    t = re.sub(r"[–—_,;:]+", " ", t)
    # Strip hiring-programme tags that vary between sources for what is the
    # same posting. Strip the TAG ONLY — an earlier version consumed the rest
    # of the string, so "UAE National_Senior Accountant" normalized to the
    # empty string and slipped past every exclusion rule.
    t = re.sub(r"\b(emiratis(?:ation|ed)|emiratiz(?:ation|ed)|emirati|"
               r"saudi[sz]ation|uae nationals?|saudi nationals?|omani nationals?|"
               r"emirati nationals?)\s*"
               r"(talent|candidates?|programme|program|hire[sd]?)?\b", " ", t)
    t = re.sub(r"\b(m/f/d|all genders|full[ -]?time|part[ -]?time|permanent|"
               r"contract|remote|hybrid|onsite|urgent|hiring|new)\b", " ", t)
    t = re.sub(r"\bsr\.?\b", "senior", t)
    t = re.sub(r"\bjr\.?\b", "junior", t)
    t = re.sub(r"\bmgr\.?\b", "manager", t)
    t = re.sub(r"\bfp\s*&\s*a\b|\bfpna\b", "fp&a", t)
    t = re.sub(r"\bavp\b", "assistant vice president", t)
    t = re.sub(r"[^a-z0-9&\s]", " ", t)
    return _WS_RE.sub(" ", t).strip()


def normalize_company(name: str | None) -> str:
    if not name:
        return ""
    c = name.lower()
    c = re.sub(r"\b(group|holding|holdings|llc|l\.l\.c|fzco|fz-llc|dmcc|plc|"
               r"ltd|limited|inc|co|company|corporation|int'l|international|"
               r"sdn bhd|bhd|pjsc|psc|wll|est|establishment)\b", " ", c)
    c = re.sub(r"[^a-z0-9\s]", " ", c)
    return _WS_RE.sub(" ", c).strip()


def normalize_location(loc: str | None) -> str:
    if not loc:
        return ""
    l = loc.lower()
    l = re.sub(r"[^a-z\s,]", " ", l)
    # keep only the leading city token so "Dubai, United Arab Emirates" and
    # "Dubai" fingerprint the same
    head = l.split(",")[0]
    return _WS_RE.sub(" ", head).strip()


def resolve_country(*parts: str | None) -> str | None:
    """Country from LOCATION strings only. None if unknown.

    Pass location fields here, never the job description — a description that
    mentions the group's Dubai head office would otherwise relabel a Cairo role
    as UAE, which is exactly the kind of quiet fabrication this file exists to
    avoid.
    """
    blob = " , ".join(p for p in parts if p)
    if not blob:
        return None
    low = blob.lower()
    for frag in re.split(r"[,/;|\-]", low):
        f = frag.strip()
        if f in COUNTRY_ALIASES:
            return COUNTRY_ALIASES[f]
        if f in OTHER_COUNTRIES:
            return f.title()
    for alias, country in COUNTRY_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", low):
            return country
    for city, country in CITY_COUNTRY.items():
        if re.search(rf"\b{re.escape(city)}\b", low):
            return country
    for name in OTHER_COUNTRIES:
        if re.search(rf"\b{re.escape(name)}\b", low):
            return name.title()
    return None


def region_for(country: str | None) -> str:
    if country in GCC:
        return "GCC"
    if country in SEA:
        return "SEA"
    return "Other"


def primary_city(*parts: str | None) -> str | None:
    """City name, or None. A bare country name is not a city — return None
    rather than printing "United Arab Emirates" in the location slot."""
    blob = " , ".join(p for p in parts if p)
    if not blob:
        return None
    low = blob.lower()
    for city in CITY_COUNTRY:
        if re.search(rf"\b{re.escape(city)}\b", low):
            return city.title()
    head = blob.split(",")[0].strip()
    if not head:
        return None
    if head.lower() in COUNTRY_ALIASES or head.lower() in OTHER_COUNTRIES:
        return None
    return head


def parse_date(value: Any) -> str | None:
    """Anything a source hands us → ISO date, or None. Never guesses."""
    if value in (None, "", "null"):
        return None
    if isinstance(value, (int, float)):
        # Lever uses epoch milliseconds.
        try:
            ts = float(value) / (1000.0 if float(value) > 1e11 else 1.0)
            return datetime.fromtimestamp(ts, UTC).date().isoformat()
        except (ValueError, OSError, OverflowError):
            return None
    s = str(value).strip()
    if not s:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%d-%b-%Y", "%b %d, %Y", "%d %B %Y",
                "%B %d, %Y", "%d %b %Y",
                # SuccessFactors itemprop meta: "Wed Aug 12 00:00:00 UTC 2026"
                "%a %b %d %H:%M:%S %Z %Y", "%a %b %d %H:%M:%S %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _first_int(text: str, pattern: str) -> int | None:
    m = re.search(pattern, text or "", re.I)
    if not m:
        return None
    try:
        return int(m.group(1))
    except (ValueError, IndexError):
        return None


def parse_experience(text: str) -> tuple[int | None, int | None]:
    """Years of experience explicitly stated in the posting. None if not stated."""
    t = text or ""
    m = re.search(r"(\d{1,2})\s*(?:\+|plus)?\s*(?:-|to|–)\s*(\d{1,2})\s*\+?\s*year", t, re.I)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if 0 < a <= b <= 40:
            return a, b
    m = re.search(r"(?:minimum|min\.?|at least|over)?\s*(\d{1,2})\s*\+?\s*year[s]?"
                  r"[^.]{0,40}?(?:experience|exp\b)", t, re.I)
    if m:
        v = int(m.group(1))
        if 0 < v <= 40:
            return v, None
    m = re.search(r"experience[^.]{0,30}?(\d{1,2})\s*\+?\s*year", t, re.I)
    if m:
        v = int(m.group(1))
        if 0 < v <= 40:
            return v, None
    return None, None


def parse_salary(text: str) -> tuple[int | None, int | None, str | None]:
    """Only returns a salary when the posting actually prints one."""
    t = text or ""
    m = re.search(r"\b(AED|SAR|MYR|OMR|USD|QAR|KWD|BHD|RM)\s*"
                  r"([\d][\d,\.]{2,})\s*(?:-|to|–)\s*([\d][\d,\.]{2,})", t, re.I)
    if m:
        cur = m.group(1).upper()
        cur = "MYR" if cur == "RM" else cur
        try:
            lo = int(float(m.group(2).replace(",", "")))
            hi = int(float(m.group(3).replace(",", "")))
            if 0 < lo <= hi:
                return lo, hi, cur
        except ValueError:
            pass
    return None, None, None


# ===========================================================================
# Requirement / exclusion detection
# ===========================================================================

NATIONALITY_ONLY = re.compile(
    r"\b(?:only|exclusively|must be an?)\s+(?:uae|emirati|saudi|omani)\s*"
    r"(?:national|citizen)s?\b"
    r"|\b(?:uae|emirati|saudi|omani)\s*(?:national|citizen)s?\s+only\b"
    r"|\bopen\s+(?:only\s+)?to\s+(?:uae|emirati|saudi|omani)\s*(?:national|citizen)s?\b"
    r"|\breserved\s+for\s+(?:uae|emirati|saudi|omani)\s*(?:national|citizen)s?\b",
    re.I)

NATIONALITY_PREF = re.compile(
    r"\b(emiratis?ation|emiratiz\w+|saudi[sz]ation|omanis\w+|nafis|nitaqat)\b"
    r"|\b(uae|emirati|saudi|omani)\s*(national|citizen)s?\b", re.I)

# Titles that are materially below FP&A Manager, or in a function he does not
# want. Matched against the NORMALIZED title.
EXCLUDE_TITLE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(intern|internship|trainee|apprentice|graduate\s+(program|scheme|trainee)|"
                r"fresh\s+graduate|student)\b"), "intern / graduate programme"),
    (re.compile(r"^(?:.*\b)?(junior|assistant|associate)\s+(analyst|accountant|"
                r"finance\s+officer|officer)\b"), "junior / associate level role"),
    # "Junior" in front of ANY title is a junior role — the original rule only
    # fired on junior+analyst/accountant/officer, so "Junior Financial
    # Controller" scored as a B-tier controllership job.
    (re.compile(r"\bjunior\b"), "junior-grade role"),
    # F&B / hotel cost control is a unit-level operational job, not commercial
    # finance — "Assistant Cost Controller" was reaching the feed.
    (re.compile(r"\bcost\s+controller\b"), "operational cost-control role"),
    # Assistant Manager is the grade he left in 2023 — a step backwards, and
    # squarely "materially below FP&A Manager level".
    (re.compile(r"\bassistant\s+manager\b"),
     "assistant manager grade, below his current FP&A Manager level"),
    # "Account Management" here means customer accounts, not accounting. Credit
    # card / client-portfolio roles are commercial product jobs, not finance.
    (re.compile(r"\bcredit\s+cards?\b|\b(key\s+)?account\s+management\b|"
                r"\bkey\s+accounts?\b|\brelationship\s+manager\b"),
     "commercial account-management role, not a finance role"),
    (re.compile(r"\b(accounts?\s+payable|accounts?\s+receivable|\bap\b\s+(clerk|officer|"
                r"accountant|supervisor|manager)|\bar\b\s+(clerk|officer|accountant|"
                r"supervisor|manager)|payables?|receivables?|credit\s+control(ler)?|"
                r"collections?)\b"), "transactional AP/AR/collections role"),
    (re.compile(r"\bpayroll\b"), "payroll role"),
    (re.compile(r"\b(bookkeeper|book\s*keeping|cashier|billing|invoicing|"
                r"data\s+entry|clerk)\b"), "transactional bookkeeping role"),
    (re.compile(r"^(?:senior\s+|sr\s+|general\s+|chief\s+|staff\s+)?accountant\b"),
     "accountant role, materially below FP&A Manager"),
    (re.compile(r"\baccountant\b(?!.*\b(manager|head|director|lead)\b)"),
     "accountant role, materially below FP&A Manager"),
    (re.compile(r"\b(internal\s+audit|audit(or)?\s+(manager|senior|lead|officer)|"
                r"^auditor\b|external\s+audit|audit\s+&?\s*assurance)\b"),
     "audit-only role"),
    (re.compile(r"^(?:senior\s+|group\s+|head\s+of\s+)?tax\b|\btax\s+(manager|"
                r"specialist|analyst|accountant|director|lead|advisor)\b"), "tax-only role"),
    (re.compile(r"^(?:senior\s+|group\s+|head\s+of\s+)?treasury\b|\btreasury\s+"
                r"(manager|analyst|accountant|specialist|dealer)\b"), "treasury-only role"),
    (re.compile(r"\b(sales|store|shop|boutique|retail\s+store|restaurant|kitchen|"
                r"barista|cashier|waiter|driver|technician|engineer|nurse|chef|"
                r"housekeep\w+|security|warehouse|merchandis\w+|beauty|fragrance|"
                r"stylist|advisor)\b(?!.*\bfinance\b)"), "not a finance role"),
    # Adjacent-but-not-finance roles that sit inside a "Financial Services"
    # business unit and therefore survived the finance escape hatch below.
    # Every one of these reached the live feed on the first run.
    (re.compile(r"\b(business\s+development|product\s+owner|product\s+manager|"
                r"underwrit\w+|actuar\w+|claims|broker\w*|"
                r"insurance\s+(advisor|executive|consultant|agent|specialist)|"
                r"cross\s+sell|renewal\s+specialist)\b"),
     "commercial / insurance role, not a finance function"),
    (re.compile(r"\b(financial\s+crime|aml\b|anti[- ]money|fraud\s+investigat\w+|"
                r"investigator|sanctions)\b"), "financial-crime / AML role"),
    # Risk, compliance and governance ONLY — excluded unless the title also
    # carries a real finance-function word, same test as the escape hatch.
    (re.compile(r"\b(risk\s+manager|compliance\s+manager|governance[, ]|"
                r"grc\b|process\s*&?\s*compliance)\b"),
     "risk / compliance role, not commercial finance"),
    (re.compile(r"\b(transformation\s+lead|system\s+transformation|"
                r"core\s+system|solution\s+architect|scrum|agile\s+coach)\b"),
     "systems / transformation role, not a finance function"),
]

# Reasons that a genuine finance-function title overrides. A lookahead cannot
# do this job: it only scans FORWARD from the match, so "FP&A Transformation
# Lead" — a target role from §4 of the brief — kept its exclusion because the
# "FP&A" sat behind the matched phrase.
SOFT_EXCLUSIONS = {
    "not a finance role",
    "risk / compliance role, not commercial finance",
    "systems / transformation role, not a finance function",
}

# A title mentioning "Financial Services" names a BUSINESS UNIT, not a finance
# function — Al-Futtaim brands its insurance arm that way. Treating it as a
# finance signal is what let "Insurance Advisor | Financial Services" and
# "Outbound Sales - Cross Sell | Financial Services" into the feed.
FINANCE_FUNCTION = re.compile(
    r"\b(fp&a|financial\s+planning|controller|controlling|controllership|"
    r"finance\s+(manager|lead|head|director|business\s+partner)|"
    r"head\s+of\s+finance|commercial\s+finance|business\s+finance|"
    r"management\s+account\w*|financial\s+report\w*)\b", re.I)

# Functional signals read off the DETAIL text, not the title.
FUNCTIONAL_SIGNALS = {
    "fpa": (re.compile(r"\b(fp&a|financial planning (?:and|&) analysis|budgeting and "
                       r"forecasting|forecast\w*|budget\w*|variance analys\w+|"
                       r"management report\w*|mis report\w*)\b", re.I), 7),
    "commercial": (re.compile(r"\b(commercial finance|business partner\w*|decision support|"
                              r"pricing|margin analys\w+|profitability analys\w+|"
                              r"category (?:finance|performance))\b", re.I), 6),
    "controllership": (re.compile(r"\b(financial control\w*|controllership|month[- ]end close|"
                                  r"statutory report\w*|consolidat\w+|general ledger|"
                                  r"financial report\w*)\b", re.I), 5),
    "pl": (re.compile(r"\b(p&l|profit (?:and|&) loss|bottom line|ebitda|"
                      r"revenue and cost|top line)\b", re.I), 4),
    "capital": (re.compile(r"\b(capex|capital allocation|investment appraisal|"
                           r"feasibilit\w+|business case|roi\b|irr\b|npv\b|"
                           r"payback)\b", re.I), 3),
}

SCOPE_SIGNALS = re.compile(
    r"\b(multi[- ]countr\w+|multi[- ]entit\w+|multi[- ]market|regional|"
    r"across (?:the )?(?:region|markets|countries|gcc|mena)|group[- ]level|"
    r"cluster|pan[- ](?:gcc|mena|regional)|several countries|"
    r"p&l ownership|own(?:s|ing)? the p&l|full p&l|entities)\b", re.I)

LEADERSHIP_SIGNALS = re.compile(
    r"\b(lead(?:ing|s)? a team|manage(?:s|ment of)? a team|team of \d+|"
    r"direct reports?|mentor\w*|coach\w*|line manage\w*|"
    r"board|audit committee|c[- ]suite|cfo\b|ceo\b|executive committee|"
    r"exco\b|senior (?:leadership|management) team|stakeholder management)\b", re.I)

QUALIFICATION_SIGNALS = re.compile(
    r"\b(chartered accountant|\bca\b|\bacca\b|\bcpa\b|\bcima\b|\bicai\b|"
    r"\bcfa\b|qualified accountant|professional accounting qualification)\b", re.I)

TOOLING_SIGNALS = re.compile(
    r"\b(power\s*bi|powerbi|tableau|d365|dynamics 365|microsoft dynamics|"
    r"\bsap\b|oracle|hyperion|anaplan|essbase|advanced excel|"
    r"financial model\w*|data visuali\w+|\bsql\b|erp\b)\b", re.I)

IFRS_SIGNALS = re.compile(
    r"\b(ifrs\s*16|ifrs|mfrs|mpers|lease accounting|consolidat\w+|"
    r"group reporting|statutory account\w*)\b", re.I)

RETAIL_SIGNALS = re.compile(
    r"\b(retail|fashion|apparel|lifestyle|fmcg|consumer|store|omnichannel|"
    r"e[- ]?commerce|merchandis\w+|brand|hypermarket|supermarket|"
    r"food\s*&?\s*beverage|f&b|restaurant|grocery|beauty|luxury)\b", re.I)

REALESTATE_SIGNALS = re.compile(
    r"\b(real estate|property|development|leasing|mall|construction|"
    r"asset management|facilit\w+ management)\b", re.I)

SENIOR_TITLE = re.compile(
    r"\b(head of|director|vice president|\bvp\b|chief|senior manager|"
    r"group manager|general manager)\b", re.I)
MANAGER_TITLE = re.compile(r"\b(manager|controller|lead|principal)\b", re.I)
# NB: no bare "partner" here — it would swallow "Finance Business Partner",
# which is a target role. Audit-firm partner titles are already caught by the
# audit-only exclusion.
TOO_SENIOR = re.compile(r"\b(chief financial officer|cfo|group cfo|"
                        r"finance director|managing director)\b", re.I)


def exclusion_for(normalized: str, detail_text: str) -> str | None:
    """Hard-exclusion reason, or None. Reads title AND detail text."""
    for pattern, reason in EXCLUDE_TITLE_RULES:
        if pattern.search(normalized):
            # "Finance Manager - Accounts" style titles keep their finance
            # signal; only exclude when nothing senior-finance is present.
            # Escape hatch: a title like "Finance Manager - Retail Sales" or
            # "FP&A Transformation Lead" is still a finance job. It must test
            # for a finance FUNCTION, not a bare `financ\w+` — that matched the
            # business-unit name "Financial Services" and readmitted insurance
            # sales roles.
            if reason in SOFT_EXCLUSIONS and FINANCE_FUNCTION.search(normalized):
                continue
            return reason
    if NATIONALITY_ONLY.search(detail_text or ""):
        return "restricted to nationals he cannot be"
    # Roles demanding a track record he does not have.
    lo, _hi = parse_experience(detail_text or "")
    if lo is not None and lo >= 15:
        return f"requires {lo}+ years, beyond his 10-year track record"
    if TOO_SENIOR.search(normalized) and not re.search(r"\b(manager|controller)\b", normalized):
        return "CFO / FD-level role beyond his current track record"
    return None


# ===========================================================================
# Freshness
# ===========================================================================

def freshness_status(posted_date: str | None, today: str | None = None) -> str:
    """Contract table. posted_date None → ACTIVE (never fabricate a date)."""
    if not posted_date:
        return "ACTIVE"
    try:
        d = datetime.strptime(posted_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return "ACTIVE"
    ref = (datetime.strptime(today, "%Y-%m-%d").date() if today
           else datetime.now(UTC).date())
    age = (ref - d).days
    if age < 0:
        return "NEW"
    if age <= 7:
        return "NEW"
    if age <= 21:
        return "ACTIVE"
    if age <= 45:
        return "AGING"
    return "STALE"


# ===========================================================================
# Scoring
# ===========================================================================

def _seniority_points(normalized: str, text: str) -> int:
    """Max 20. Penalises junior AND roles needing a CFO-level track record."""
    pts = 0
    if SENIOR_TITLE.search(normalized):
        pts = 18
    elif MANAGER_TITLE.search(normalized):
        pts = 15
    elif re.search(r"\b(senior|specialist|business partner)\b", normalized):
        pts = 10
    else:
        pts = 6
    lo, _ = parse_experience(text)
    if lo is not None:
        if 6 <= lo <= 12:
            pts += 2           # squarely his 7+ post-qualification band
        elif lo >= 13:
            pts -= 5           # stretch
        elif lo <= 3:
            pts -= 6           # too junior
    if re.search(r"\b(own(?:s|ership)?|accountab\w+|responsible for the)\b", text, re.I):
        pts += 1
    return max(0, min(20, pts))


def _functional_points(text: str) -> int:
    """Max 20, from responsibilities actually present in the detail text."""
    pts = 0
    for _name, (pattern, weight) in FUNCTIONAL_SIGNALS.items():
        if pattern.search(text or ""):
            pts += weight
    return max(0, min(20, pts))


def _industry_points(company: str, text: str) -> int:
    """Max 15. Retail/consumer top, real estate strong, others partial."""
    cnorm = normalize_company(company)
    if any(peer in cnorm for peer in PEER_RETAIL):
        base = 15
    elif RETAIL_SIGNALS.search(text or ""):
        base = 13
    elif REALESTATE_SIGNALS.search(text or ""):
        base = 11
    else:
        base = 6
    return min(15, base)


def _geographic_points(country: str | None) -> int:
    """Max 10."""
    if country == "UAE":
        return 10
    if country == "Saudi Arabia":
        return 9
    if country == "Malaysia":
        return 8
    if country == "Oman":
        return 8
    if country in GCC:
        return 5
    if country in SEA:
        return 4
    return 1 if country else 2


def _scope_points(text: str) -> int:
    """Max 10."""
    pts = 0
    hits = SCOPE_SIGNALS.findall(text or "")
    pts += min(7, 3 * len(set(h.lower() for h in hits)))
    if re.search(r"\b(p&l|profit (?:and|&) loss)\b", text or "", re.I):
        pts += 3
    return max(0, min(10, pts))


def _leadership_points(text: str) -> int:
    """Max 10."""
    pts = 0
    if re.search(r"\b(lead(?:ing|s)? a team|team of \d+|direct reports?|"
                 r"mentor\w*|line manage\w*)\b", text or "", re.I):
        pts += 5
    if re.search(r"\b(board|cfo\b|ceo\b|c[- ]suite|exco\b|executive committee|"
                 r"senior leadership)\b", text or "", re.I):
        pts += 5
    elif re.search(r"\b(stakeholder|business partner\w*|cross[- ]function\w*)\b",
                   text or "", re.I):
        pts += 2
    return max(0, min(10, pts))


def _qualification_points(text: str) -> int:
    """Max 5."""
    pts = 0
    if QUALIFICATION_SIGNALS.search(text or ""):
        pts += 3
    if TOOLING_SIGNALS.search(text or ""):
        pts += 2
    return max(0, min(5, pts))


def _upside_points(company: str, normalized: str, text: str) -> int:
    """Max 10. Brand quality plus progression toward FD/CFO."""
    brand = EMPLOYER_BRAND.get((company or "").lower().strip(), 0)
    pts = 6 if brand >= 85 else 5 if brand >= 78 else 4 if brand >= 70 else 2
    if SENIOR_TITLE.search(normalized):
        pts += 3
    elif re.search(r"\b(controller|controlling)\b", normalized):
        pts += 2
    if re.search(r"\b(report(?:s|ing)? (?:directly )?to the (?:cfo|ceo|finance director)|"
                 r"succession|career progression|growth path)\b", text or "", re.I):
        pts += 2
    return max(0, min(10, pts))


def score_job(job: dict) -> dict:
    """Fill in every score field. Reads responsibilities, not just the title."""
    text = " \n ".join(filter(None, [
        job.get("description") or "",
        " ".join(job.get("responsibilities") or []),
        " ".join(job.get("requirements") or []),
    ]))
    normalized = job.get("normalized_title") or ""
    company = job.get("company") or ""

    bd = {
        "seniority":     _seniority_points(normalized, text),
        "functional":    _functional_points(text),
        "industry":      _industry_points(company, text),
        "geographic":    _geographic_points(job.get("country")),
        "scope":         _scope_points(text),
        "leadership":    _leadership_points(text),
        "qualification": _qualification_points(text),
        "upside":        _upside_points(company, normalized, text),
    }
    fit = sum(bd.values())

    employer = EMPLOYER_BRAND.get(company.lower().strip())
    if employer is None:
        cnorm = normalize_company(company)
        employer = 70 if any(p in cnorm for p in PEER_RETAIL) else 55
        if job.get("source_confidence") == "low":
            employer -= 5
    upside = min(100, bd["upside"] * 7 + (18 if SENIOR_TITLE.search(normalized) else 8))

    opportunity = round(0.60 * fit + 0.25 * employer + 0.15 * upside)

    job["score_breakdown"] = bd
    job["candidate_fit_score"] = int(fit)
    job["employer_score"] = int(max(0, min(100, employer)))
    job["career_upside_score"] = int(max(0, min(100, upside)))
    job["opportunity_score"] = int(max(0, min(100, opportunity)))
    job["tier"] = tier_for(job["opportunity_score"])
    job["application_priority"] = priority_for(job["tier"], job.get("is_excluded", False))
    return job


def tier_for(score: int) -> str:
    if score >= 85:
        return "S"
    if score >= 75:
        return "A"
    if score >= 65:
        return "B"
    if score >= 50:
        return "C"
    return "D"


def priority_for(tier: str, excluded: bool = False) -> str:
    if excluded or tier == "D":
        return "SKIP"
    return {"S": "APPLY NOW", "A": "HIGH PRIORITY",
            "B": "GOOD FIT", "C": "OPTIONAL"}[tier]


# ---------------------------------------------------------------------------
# why_fit / watch_out — must cite the real resume, never generic filler
# ---------------------------------------------------------------------------

def build_why_fit(job: dict) -> list[str]:
    """2-4 specific reasons, each tied to something real on his resume."""
    text = " \n ".join(filter(None, [
        job.get("description") or "",
        " ".join(job.get("responsibilities") or []),
        " ".join(job.get("requirements") or []),
    ]))
    normalized = job.get("normalized_title") or ""
    company = job.get("company") or ""
    cnorm = normalize_company(company)
    country = job.get("country")

    # (priority, text). Lower priority sorts first. The FP&A line is the most
    # generic thing that can be said about him, so it only leads when the role
    # is explicitly an FP&A role; otherwise the distinctive overlap leads and
    # every row does not open with the same sentence.
    fpa_titled = bool(re.search(r"\bfp&a|financial planning\b", normalized))
    scored: list[tuple[int, str]] = []

    if FUNCTIONAL_SIGNALS["fpa"][0].search(text) or fpa_titled:
        scored.append((0 if fpa_titled else 6,
                       "Core FP&A remit — he runs budgeting, forecasting and variance "
                       "analysis on an MYR 400M+ P&L as FP&A Manager at Lifestyle "
                       "Retail Malaysia (Landmark Group) since May 2023."))
    if SCOPE_SIGNALS.search(text):
        scored.append((1, "Multi-country / multi-entity scope maps directly onto his "
                          "Malaysia + Indonesia P&L ownership across 60+ Max Fashion "
                          "and Babyshop stores."))
    if any(peer in cnorm for peer in PEER_RETAIL) or RETAIL_SIGNALS.search(text):
        scored.append((2, f"{company} is peer retail to his current employer — the "
                          "60+ store, multi-brand lifestyle retail P&L he already "
                          "owns transfers without a sector learning curve."))
    if IFRS_SIGNALS.search(text):
        scored.append((3, "IFRS/MPERS group consolidation is day-to-day for him, "
                          "including IFRS 16 across 60+ retail leases."))
    if re.search(r"\b(capex|investment appraisal|feasibilit\w+|business case|"
                 r"roi\b|irr\b|npv\b|new store)\b", text, re.I):
        scored.append((4, "Capital allocation fit — he builds the ROI/IRR/NPV store "
                          "feasibility models that decide new-store capex."))
    if REALESTATE_SIGNALS.search(text) and not RETAIL_SIGNALS.search(text):
        scored.append((4, "Real-estate exposure is on his record — 3 years as Asst "
                          "Manager Accounts at Emami Realty in Kolkata before the "
                          "retail move."))
    if TOOLING_SIGNALS.search(text):
        scored.append((5, "Systems fit — he led the D365 ERP implementation that cut "
                          "the close cycle 40%, and builds the Power BI reporting on "
                          "top of it."))
    if re.search(r"\b(board|cfo\b|ceo\b|c[- ]suite|exco\b|executive committee|"
                 r"senior leadership|investor)\b", text, re.I):
        scored.append((7, "The role reports into senior leadership; he already "
                          "prepares quarterly Board, CEO and CFO reporting and briefs "
                          "Investor Relations."))
    if re.search(r"\b(lead(?:ing|s)? a team|team of \d+|direct reports?|mentor\w*)\b",
                 text, re.I):
        scored.append((8, "Team leadership is proven — he mentors a team of 4 analysts "
                          "and business-partners Ops, Retail Heads, Buying and IR."))
    if country in TARGET_COUNTRIES:
        scored.append((9, f"{country} is one of his four active target markets, and he "
                          "is already GCC-adjacent inside a Dubai-headquartered group."))

    scored.sort(key=lambda p: p[0])
    out = [t for _p, t in scored][:4]

    # The contract wants 2-4 entries. When the posting is too thin to yield
    # them, top up with TRUE statements — that nothing else matched, and what
    # he actually has — rather than inventing another reason to like the role.
    if not out:
        out.append("No specific overlap could be extracted from the posted text "
                   "— the description is too thin to match against his record. "
                   "Read it in full before applying.")
    if len(out) < 2:
        out.append(
            "Nothing further in the posted text matches his record. His lane is "
            "retail FP&A — MYR 400M+ P&L across 60+ Max Fashion and Babyshop "
            "stores in Malaysia and Indonesia, IFRS/MPERS consolidation, and a "
            "D365 ERP implementation. Judge the rest of this role against that.")
    return out


def build_watch_out(job: dict) -> list[str]:
    """1-2 honest negatives. These are the reasons NOT to apply."""
    text = " \n ".join(filter(None, [
        job.get("description") or "",
        " ".join(job.get("responsibilities") or []),
        " ".join(job.get("requirements") or []),
    ]))
    normalized = job.get("normalized_title") or ""
    out: list[str] = []

    if not job.get("posted_date"):
        out.append("The posting date is not published by the source, so this could "
                   "be an old req — age is unknown, not fresh.")
    if job.get("status") in ("AGING", "STALE"):
        out.append(f"Posted {job.get('posted_date')} — this req is {job.get('status').lower()} "
                   "and may already be filled.")
    if not job.get("application_url_verified"):
        out.append("The application link could not be confirmed reachable — verify "
                   "it before relying on it.")
    if NATIONALITY_PREF.search(text) and not NATIONALITY_ONLY.search(text):
        out.append("Emiratisation/Saudization preference is stated — a non-national "
                   "applicant is at a disadvantage even though it is not a hard bar.")
    lo, _ = parse_experience(text)
    if lo is not None and lo >= 12:
        out.append(f"Asks for {lo}+ years; he has 10+ total and 7+ post-qualification, "
                   "so this is a stretch on paper.")
    if re.search(r"\b(sap|hyperion|anaplan|essbase|oracle fusion|jde|netsuite)\b",
                 text, re.I) and not re.search(r"\bd365|dynamics\b", text, re.I):
        sysname = re.search(r"\b(sap|hyperion|anaplan|essbase|oracle fusion|jde|netsuite)\b",
                            text, re.I).group(1).upper()
        out.append(f"{sysname} is named and his ERP depth is D365, not {sysname} — "
                   "expect to address that gap directly.")
    if re.search(r"\b(cfa|cpa)\b", text, re.I) and not re.search(r"\b(ca|acca|cima|"
                 r"chartered accountant)\b", text, re.I):
        out.append("The posting names a qualification he does not hold; his is CA (ICAI, 2017).")
    if not RETAIL_SIGNALS.search(text) and not REALESTATE_SIGNALS.search(text):
        out.append("Sector is outside retail and real estate, so the industry half of "
                   "his track record does not carry over.")
    if job.get("source_confidence") != "high":
        out.append(f"Sourced via {job.get('source')} rather than the employer's own "
                   "site — confirm the role exists directly with the company.")
    if TOO_SENIOR.search(normalized):
        out.append("Title sits at FD/CFO level — above his current track record.")

    if not out:
        out.append("Nothing disqualifying surfaced in the posted text, which itself "
                   "means the description is thin — probe scope and reporting line.")
    return out[:2]


def build_resume_match(job: dict) -> dict | None:
    """Only required for tier S and A; None otherwise (contract)."""
    if job.get("tier") not in ("S", "A"):
        return None
    text = " \n ".join(filter(None, [
        job.get("description") or "",
        " ".join(job.get("responsibilities") or []),
        " ".join(job.get("requirements") or []),
    ]))
    strong, missing = [], []

    if FUNCTIONAL_SIGNALS["fpa"][0].search(text):
        strong.append("FP&A — budgeting, forecasting, variance analysis on MYR 400M+ P&L")
    if SCOPE_SIGNALS.search(text):
        strong.append("Multi-country P&L (Malaysia + Indonesia, 60+ stores)")
    if IFRS_SIGNALS.search(text):
        strong.append("IFRS / MPERS group consolidation, IFRS 16 across 60+ leases")
    if re.search(r"\b(d365|dynamics|erp|power\s*bi)\b", text, re.I):
        strong.append("D365 ERP implementation lead (close −40%), Power BI reporting")
    if re.search(r"\b(board|cfo\b|ceo\b|c[- ]suite|exco\b)\b", text, re.I):
        strong.append("Board / CEO / CFO quarterly reporting")
    if re.search(r"\b(roi\b|irr\b|npv\b|feasibilit\w+|capex|business case)\b", text, re.I):
        strong.append("ROI/IRR/NPV modelling and store feasibility")
    if RETAIL_SIGNALS.search(text):
        strong.append("Retail / fashion / lifestyle sector depth (Max Fashion, Babyshop)")
    if re.search(r"\b(lead(?:ing|s)? a team|team of \d+|direct reports?)\b", text, re.I):
        strong.append("Team leadership — 4 analysts mentored")

    for pat, label in [
        (r"\bsap\b", "SAP"), (r"\bhyperion\b", "Hyperion"), (r"\banaplan\b", "Anaplan"),
        (r"\bessbase\b", "Essbase"), (r"\bnetsuite\b", "NetSuite"),
        (r"\boracle fusion\b", "Oracle Fusion"), (r"\bcfa\b", "CFA"),
        (r"\bcpa\b", "CPA"), (r"\bcima\b", "CIMA"),
        (r"\barabic\b", "Arabic language"),
        (r"\bus gaap\b", "US GAAP"),
        (r"\bsox\b", "SOX compliance"),
        (r"\b(gcc|uae) experience\b", "In-market GCC experience"),
    ]:
        if re.search(pat, text, re.I):
            missing.append(label)

    if not strong:
        strong.append("General finance management experience (10+ yrs, CA ICAI 2017)")
    return {
        "strong": strong[:6],
        "missing": missing[:5],
        "tailoring_recommended": bool(missing) or job.get("tier") == "A",
    }


# ===========================================================================
# Deduplication
# ===========================================================================

def fingerprint(company: str | None, title: str | None, location: str | None) -> str:
    key = f"{normalize_company(company)}|{normalize_title(title)}|{normalize_location(location)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def job_id(job: dict) -> str:
    """Stable across refreshes: requisition id / canonical URL when available,
    otherwise the company+title+location fingerprint."""
    req = job.get("_req_id")
    if req:
        seed = f"{normalize_company(job.get('company'))}|req|{req}"
    else:
        canon = job.get("source_url") or job.get("application_url") or ""
        canon = canon.split("?")[0].rstrip("/")
        if canon:
            seed = f"{normalize_company(job.get('company'))}|url|{canon}"
        else:
            return fingerprint(job.get("company"), job.get("title"), job.get("location"))
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a[:1500], b[:1500]).ratio()


CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


def deduplicate(jobs: list[dict]) -> tuple[list[dict], int]:
    """Fold duplicates into one row, preserving every source in `sources`.

    Fingerprint is normalized company + title + location. Rows sharing a
    requisition id or canonical URL merge regardless of fingerprint; rows whose
    fingerprints match but whose descriptions diverge badly stay separate.
    """
    groups: dict[str, list[dict]] = {}
    by_req: dict[str, str] = {}

    for j in jobs:
        fp = fingerprint(j.get("company"), j.get("title"), j.get("location"))
        req = j.get("_req_id")
        canon = (j.get("source_url") or "").split("?")[0].rstrip("/")
        hard_key = None
        if req:
            hard_key = f"req::{normalize_company(j.get('company'))}::{req}"
        elif canon:
            hard_key = f"url::{canon}"

        target = by_req.get(hard_key) if hard_key else None
        if target is None:
            # description-similarity tiebreak within the same fingerprint
            target = fp
            existing = groups.get(fp)
            if existing:
                base = existing[0].get("description") or ""
                cand = j.get("description") or ""
                if base and cand and _similar(base, cand) < 0.45:
                    target = f"{fp}::{len(existing)}"
            if hard_key:
                by_req[hard_key] = target
        groups.setdefault(target, []).append(j)

    merged: list[dict] = []
    removed = 0
    for key, rows in groups.items():
        removed += len(rows) - 1
        rows.sort(key=lambda r: (
            CONFIDENCE_RANK.get(r.get("source_confidence"), 0),
            1 if r.get("is_direct_apply") else 0,
            len(r.get("description") or ""),
        ), reverse=True)
        win = rows[0]

        seen, all_sources = set(), []
        for r in rows:
            for s in [r.get("source")] + list(r.get("sources") or []):
                if s and s not in seen:
                    seen.add(s)
                    all_sources.append(s)
        win["sources"] = all_sources
        win["duplicate_group"] = hashlib.sha1(key.encode("utf-8")).hexdigest()

        # Winner keeps the highest-confidence direct-apply URL available across
        # the whole group, even if that row lost on description length.
        best = max(rows, key=lambda r: (
            CONFIDENCE_RANK.get(r.get("source_confidence"), 0),
            1 if r.get("is_direct_apply") else 0,
        ))
        if best.get("application_url") and (
                CONFIDENCE_RANK.get(best.get("source_confidence"), 0)
                >= CONFIDENCE_RANK.get(win.get("source_confidence"), 0)):
            win["application_url"] = best["application_url"]
            win["is_direct_apply"] = best.get("is_direct_apply", False)
            win["source_confidence"] = best.get("source_confidence", win.get("source_confidence"))

        # Fill nulls from the other rows — never overwrite a known value.
        for r in rows[1:]:
            for f in ("posted_date", "closing_date", "salary_min", "salary_max",
                      "salary_currency", "department", "employment_type",
                      "experience_min", "experience_max"):
                if win.get(f) in (None, "") and r.get(f) not in (None, ""):
                    win[f] = r[f]
        merged.append(win)

    return merged, removed


# ===========================================================================
# URL validation
# ===========================================================================

BAD_URL_RE = re.compile(
    r"^https?://(?:www\.)?(?:google|bing|duckduckgo)\.[a-z.]+/(?:search|url)\b"
    r"|^https?://[^/]+/?$", re.I)


def is_acceptable_apply_url(url: str | None) -> bool:
    """Reject search engines and bare homepages outright."""
    if not url:
        return False
    u = url.strip()
    if not u.lower().startswith(("http://", "https://")):
        return False
    if BAD_URL_RE.match(u):
        return False
    path = urlparse(u).path.strip("/")
    if not path:
        return False
    # A generic careers landing page is not a job application URL.
    if path.lower() in ("careers", "career", "jobs", "en/careers", "vacancies",
                        "en/careers/vacancies", "search"):
        return False
    return True


def verify_url(url: str | None) -> bool:
    """HEAD, falling back to GET. True only on a real status < 400."""
    if not is_acceptable_apply_url(url):
        return False
    for method in ("head", "get"):
        try:
            resp = SESSION.request(
                method, url, timeout=20, allow_redirects=True,
                stream=(method == "get"))
            if method == "get":
                resp.close()
            if resp.status_code < 400:
                return True
            if resp.status_code in (403, 405, 429) and method == "head":
                continue        # some hosts refuse HEAD only
            if resp.status_code >= 400 and method == "get":
                return False
        except requests.RequestException:
            continue
    return False


def validate_all(jobs: list[dict]) -> dict[str, int]:
    stats = {"verified": 0, "unverified": 0, "broken": 0, "missing": 0}
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        futs = {pool.submit(verify_url, j.get("application_url")): j for j in jobs}
        for fut in as_completed(futs):
            j = futs[fut]
            try:
                ok = fut.result()
            except Exception:
                ok = False
            j["application_url_verified"] = bool(ok)
            if not j.get("application_url"):
                stats["missing"] += 1
            elif ok:
                stats["verified"] += 1
            else:
                stats["unverified"] += 1
                # A link we previously saw working that now fails is broken.
                if j.get("_was_verified"):
                    j["status"] = "LINK_BROKEN"
                    stats["broken"] += 1
    return stats


# ===========================================================================
# Adapters — each returns a list of raw dicts with a common shape
# ===========================================================================

def _raw(**kw) -> dict:
    base = {
        "title": None, "company": None, "location": None, "country": None,
        "department": None, "employment_type": None, "posted_date": None,
        "closing_date": None, "description": "", "responsibilities": [],
        "requirements": [], "source_url": None, "application_url": None,
        "is_direct_apply": False, "req_id": None, "skills": [],
    }
    base.update(kw)
    return base


# -- Oracle Fusion Recruiting (Al Tayer, Landmark, Apparel, Emaar, Jumeirah,
#    Americana) ----------------------------------------------------------
def fetch_oracle(src: dict) -> list[dict]:
    host = src["endpoint"]["host"].rstrip("/")
    site = src["endpoint"]["site"]
    base = f"{host}/hcmRestApi/resources/latest"
    seen: dict[str, dict] = {}

    for term in SEARCH_TERMS:
        offset = 0
        while offset < 200:
            q = (f"{base}/recruitingCEJobRequisitions?onlyData=true"
                 f"&expand=requisitionList.secondaryLocations,flexFieldsFacet.values"
                 f"&finder=findReqs;siteNumber={site},limit=50,offset={offset},"
                 f"sortBy=POSTING_DATES_DESC,keyword={quote(term)}")
            resp = _check(_get(q, headers={"Accept": "application/json"}),
                          f"{src['name']} list")
            try:
                items = resp.json()["items"][0]
            except (ValueError, KeyError, IndexError) as e:
                raise SourceError(f"{src['name']}: unexpected list payload ({e})")
            reqs = items.get("requisitionList") or []
            for r in reqs:
                if r.get("Id"):
                    seen.setdefault(str(r["Id"]), r)
            if len(reqs) < 50:
                break
            offset += 50

    candidates = [r for r in seen.values()
                  if TITLE_PREFILTER.search(r.get("Title") or "")]
    candidates = candidates[:MAX_DETAIL_PER_SOURCE]

    def detail(r: dict) -> dict | None:
        rid = str(r["Id"])
        q = (f"{base}/recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
             f'&finder=ById;Id="{rid}",siteNumber={site}')
        try:
            resp = _get(q, headers={"Accept": "application/json"})
            if resp.status_code >= 400:
                return None
            d = resp.json()["items"][0]
        except (requests.RequestException, ValueError, KeyError, IndexError):
            return None

        desc_html = " ".join(filter(None, [
            d.get("ExternalDescriptionStr") or "",
            d.get("ExternalResponsibilitiesStr") or "",
            d.get("ExternalQualificationsStr") or "",
        ]))
        if not strip_html(desc_html):
            return None
        url = (f"{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{rid}/")
        loc = d.get("PrimaryLocation") or r.get("PrimaryLocation")
        return _raw(
            title=(d.get("Title") or r.get("Title") or "").strip() or None,
            company=src["name"],
            location=primary_city(loc),
            country=resolve_country(loc, d.get("PrimaryLocationCountry")),
            department=d.get("JobFunction") or r.get("JobFunction") or d.get("Department"),
            employment_type=d.get("JobType") or r.get("JobType"),
            posted_date=parse_date(d.get("ExternalPostedStartDate") or r.get("PostedDate")),
            closing_date=parse_date(d.get("ExternalPostedEndDate") or r.get("PostingEndDate")),
            description=strip_html(desc_html),
            responsibilities=html_bullets(d.get("ExternalResponsibilitiesStr")
                                          or d.get("ExternalDescriptionStr")),
            requirements=html_bullets(d.get("ExternalQualificationsStr")),
            source_url=url, application_url=url, is_direct_apply=True,
            req_id=rid,
        )

    out = []
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        for fut in as_completed([pool.submit(detail, r) for r in candidates]):
            row = fut.result()
            if row:
                out.append(row)
    return out


# -- Phenom People (Majid Al Futtaim) -----------------------------------
def fetch_phenom(src: dict) -> list[dict]:
    ep = src["endpoint"]
    host = ep["host"].rstrip("/")
    widgets = f"{host}/widgets"
    site_path = ep.get("site", "global/en")
    seen: dict[str, dict] = {}

    for term in SEARCH_TERMS:
        for start in (0, 10, 20):
            payload = {
                "lang": ep.get("locale", "en_global"), "deviceType": "desktop",
                "country": ep.get("country", "global"), "pageName": "search-results",
                "ddoKey": "refineSearch", "sortBy": "", "subsearch": "",
                "from": start, "jobs": True, "counts": True, "size": 10,
                "clearAll": False, "jdsource": "facets", "isSliderEnable": False,
                "pageId": "page63", "siteType": "external", "keywords": term,
                "global": True, "selected_fields": {}, "locationData": {},
            }
            resp = _check(_post_json(widgets, payload), f"{src['name']} search")
            try:
                data = resp.json()
            except ValueError as e:
                raise SourceError(f"{src['name']}: non-JSON search response ({e})")
            block = data.get("refineSearch") or data.get("eagerLoadRefineSearch") or {}
            rows = (block.get("data") or {}).get("jobs") or []
            for r in rows:
                if r.get("jobSeqNo"):
                    seen.setdefault(r["jobSeqNo"], r)
            if len(rows) < 10:
                break

    candidates = [r for r in seen.values()
                  if TITLE_PREFILTER.search(r.get("title") or "")]
    candidates = candidates[:MAX_DETAIL_PER_SOURCE]

    def detail(card: dict) -> dict | None:
        payload = {
            "lang": ep.get("locale", "en_global"), "deviceType": "desktop",
            "country": ep.get("country", "global"), "pageName": "job-details",
            "ddoKey": "jobDetail", "jobSeqNo": card["jobSeqNo"],
            "isSliderEnable": False, "pageId": "page62", "siteType": "external",
        }
        try:
            resp = _post_json(widgets, payload)
            if resp.status_code >= 400:
                return None
            data = resp.json()
        except (requests.RequestException, ValueError):
            return None
        d = ((data.get("jobDetail") or data.get("eagerLoadRefineSearch") or {})
             .get("data") or {}).get("job") or {}
        if not d:
            return None
        desc_html = d.get("description") or ""
        if not strip_html(desc_html):
            return None

        jid = str(d.get("jobId") or card.get("jobId") or "")
        slug = re.sub(r"[^A-Za-z0-9]+", "-", d.get("title") or card.get("title") or "").strip("-")
        detail_url = f"{host}/{site_path}/job/{jid}/{slug}" if jid else None
        apply_url = f"{host}/{site_path}/apply?jobSeqNo={card['jobSeqNo']}"
        loc = d.get("cityStateCountry") or d.get("cityState") or d.get("location")
        return _raw(
            title=(d.get("title") or card.get("title") or "").strip() or None,
            company=src["name"],
            location=primary_city(d.get("city") or d.get("cityState") or loc),
            country=resolve_country(d.get("country"), loc),
            department=d.get("category") or d.get("department"),
            employment_type=d.get("type") or d.get("jobType"),
            posted_date=parse_date(d.get("postedDate") or d.get("atsPostedDate")
                                   or card.get("postedDate")),
            description=strip_html(desc_html),
            responsibilities=html_bullets(desc_html),
            requirements=[],
            skills=[s for s in (d.get("ml_skills") or []) if isinstance(s, str)][:12],
            source_url=detail_url or apply_url,
            application_url=apply_url, is_direct_apply=True,
            req_id=str(d.get("jobRequisitionId") or d.get("reqId") or jid or ""),
        )

    out = []
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        for fut in as_completed([pool.submit(detail, c) for c in candidates]):
            row = fut.result()
            if row:
                out.append(row)
    return out


# -- Teamtailor (Chalhoub, GMG) -----------------------------------------
_TT_JOB_RE = re.compile(r'href="(?:https?://[^"]+)?(/(?:[a-z]{2}/)?jobs/(\d+)-[^"#?]*)"')


def fetch_teamtailor(src: dict) -> list[dict]:
    host = src["endpoint"]["host"].rstrip("/")
    found: dict[str, str] = {}
    for page in range(1, 6):
        url = f"{host}/jobs" if page == 1 else f"{host}/jobs/show_more?page={page}"
        resp = _check(_get(url), f"{src['name']} list p{page}")
        hits = _TT_JOB_RE.findall(resp.text)
        if not hits:
            break
        for path, jid in hits:
            found.setdefault(jid, urljoin(host, path))
        if len(hits) < 5:
            break

    # Teamtailor list cards carry the title in the link slug — prefilter on it
    # so we only pull detail pages that could plausibly be finance.
    candidates = [(jid, u) for jid, u in found.items()
                  if TITLE_PREFILTER.search(u.rsplit("/", 1)[-1].replace("-", " "))]
    candidates = candidates[:MAX_DETAIL_PER_SOURCE]

    def detail(item: tuple[str, str]) -> dict | None:
        jid, url = item
        try:
            resp = _get(url)
            if resp.status_code >= 400:
                return None
        except requests.RequestException:
            return None
        m = re.search(r'<script type="application/ld\+json">(.*?)</script>',
                      resp.text, re.S)
        if not m:
            return None
        try:
            ld = json.loads(m.group(1))
        except ValueError:
            return None
        if ld.get("@type") != "JobPosting":
            return None
        desc_html = ld.get("description") or ""
        if not strip_html(desc_html):
            return None

        locs = ld.get("jobLocation")
        locs = locs if isinstance(locs, list) else ([locs] if locs else [])
        city = country_raw = None
        if locs:
            addr = (locs[0] or {}).get("address") or {}
            city = addr.get("addressLocality")
            country_raw = addr.get("addressCountry")
        org = (ld.get("hiringOrganization") or {}).get("name") or src["name"]
        return _raw(
            title=(ld.get("title") or "").strip() or None,
            company=org,
            location=primary_city(city),
            country=resolve_country(country_raw, city),
            employment_type=(ld.get("employmentType") or "").replace("_", "-").title() or None,
            posted_date=parse_date(ld.get("datePosted")),
            closing_date=parse_date(ld.get("validThrough")),
            description=strip_html(desc_html),
            responsibilities=html_bullets(desc_html),
            requirements=[],
            source_url=url,
            application_url=f"{url.rstrip('/')}/applications/new",
            is_direct_apply=True,
            req_id=str(ld.get("identifier", {}).get("value") if isinstance(
                ld.get("identifier"), dict) else jid),
        )

    out = []
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        for fut in as_completed([pool.submit(detail, c) for c in candidates]):
            row = fut.result()
            if row:
                out.append(row)
    return out


# -- SAP SuccessFactors / Jobs2Web (Al-Futtaim, Abdul Latif Jameel) ------
_SF_JOB_RE = re.compile(r'href="((?:/[A-Za-z0-9_]+)?/job/[^"?#]+/(\d+)/)"')


def fetch_successfactors(src: dict) -> list[dict]:
    host = src["endpoint"]["host"].rstrip("/")
    found: dict[str, str] = {}
    for term in SEARCH_TERMS:
        for startrow in (0, 25):
            url = f"{host}/search/?q={quote(term)}&startrow={startrow}"
            resp = _check(_get(url), f"{src['name']} search")
            hits = _SF_JOB_RE.findall(_html.unescape(resp.text))
            for path, jid in hits:
                found.setdefault(jid, urljoin(host, path))
            if len(hits) < 25:
                break

    candidates = [(jid, u) for jid, u in found.items()
                  if TITLE_PREFILTER.search(u.replace("-", " ").replace("/", " "))]
    candidates = candidates[:MAX_DETAIL_PER_SOURCE]

    def detail(item: tuple[str, str]) -> dict | None:
        jid, url = item
        try:
            resp = _get(url)
            if resp.status_code >= 400:
                return None
        except requests.RequestException:
            return None
        page = resp.text
        title = None
        m = re.search(r'<h1[^>]*class="[^"]*job[^"]*"[^>]*>(.*?)</h1>', page, re.I | re.S)
        m = m or re.search(r'<h1[^>]*>(.*?)</h1>', page, re.I | re.S)
        if m:
            title = strip_html(m.group(1)) or None
        # The description lives in <span itemprop="description"
        # class="jobdescription"> and contains nested divs/spans, so a lazy
        # match up to the next </span> stops at the first inner tag and yields
        # nothing. Slice from the opening tag to the first trailing marker
        # instead and let strip_html do the cleanup.
        desc_html = ""
        start = re.search(r'<(?:span|div)[^>]*(?:itemprop="description"|'
                          r'class="[^"]*jobdescription[^"]*")[^>]*>', page, re.I)
        if start:
            tail = page[start.end():]
            cut = len(tail)
            for marker in (r'<div[^>]*class="[^"]*job(?:Share|Social|Footer|Nav)',
                           r'id="job-share"', r'<footer', r'</main>',
                           r'class="[^"]*apply[Bb]utton'):
                m2 = re.search(marker, tail, re.I)
                if m2:
                    cut = min(cut, m2.start())
            desc_html = tail[:cut]
        desc = strip_html(desc_html)
        if len(desc) < 120:
            return None

        def itemprop(name: str) -> str | None:
            m2 = re.search(rf'<meta[^>]*itemprop="{name}"[^>]*content="([^"]*)"',
                           page, re.I)
            return _html.unescape(m2.group(1)).strip() if m2 else None

        # SuccessFactors emits schema.org meta tags with the real posting date
        # and address. Prefer those over anything scraped out of the layout.
        loc_raw = itemprop("streetAddress") or itemprop("addressLocality")
        if not loc_raw:
            city_m = re.search(r'<(?:span|p)[^>]*(?:class="[^"]*jobGeoLocation[^"]*"'
                               r'|id="job-location")[^>]*>(.*?)</(?:span|p)>',
                               page, re.I | re.S)
            loc_raw = strip_html(city_m.group(1)) if city_m else None
        if not loc_raw:
            # SuccessFactors slugs are City-Title-Region; the leading token is
            # the city. Only trust it when it is a city we actually know.
            slug = url.rstrip("/").rsplit("/", 2)[-2]
            head = slug.split("-")[0].replace("+", " ")
            loc_raw = head if head.lower() in CITY_COUNTRY else None

        posted = parse_date(itemprop("datePosted"))
        if not posted:
            date_m = re.search(r'<(?:span|p)[^>]*(?:class="[^"]*jobDate[^"]*"'
                               r'|id="job-date")[^>]*>(.*?)</(?:span|p)>',
                               page, re.I | re.S)
            if date_m:
                raw_date = strip_html(date_m.group(1))
                posted = parse_date(re.sub(r"^\s*Date\s*:\s*", "", raw_date, flags=re.I))
        return _raw(
            title=title, company=src["name"],
            location=primary_city(loc_raw),
            country=resolve_country(loc_raw),
            posted_date=posted,
            closing_date=parse_date(itemprop("validThrough")),
            employment_type=itemprop("employmentType"),
            description=desc,
            responsibilities=html_bullets(desc_html) or text_bullets(desc),
            requirements=[],
            source_url=url,
            application_url=urljoin(host, f"/talentcommunity/apply/{jid}/?locale=en_GB"),
            is_direct_apply=True, req_id=jid,
        )

    out = []
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        for fut in as_completed([pool.submit(detail, c) for c in candidates]):
            row = fut.result()
            if row:
                out.append(row)
    return out


# -- Lever (Aldar) -------------------------------------------------------
def fetch_lever(src: dict) -> list[dict]:
    slug = src["endpoint"]["slug"]
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    resp = _check(_get(url, headers={"Accept": "application/json"}), f"{src['name']} list")
    try:
        postings = resp.json()
    except ValueError as e:
        raise SourceError(f"{src['name']}: non-JSON response ({e})")

    out = []
    for p in postings:
        title = (p.get("text") or "").strip()
        if not TITLE_PREFILTER.search(title):
            continue
        cats = p.get("categories") or {}
        # Lever's payload IS the detail record — description, opening and the
        # requirement lists all come from the posting page itself.
        blocks = [p.get("descriptionPlain") or strip_html(p.get("description")),
                  p.get("openingPlain") or ""]
        reqs: list[str] = []
        for lst in (p.get("lists") or []):
            blocks.append(strip_html(lst.get("text") or ""))
            reqs.extend(html_bullets(lst.get("content")))
        desc = "\n\n".join(b for b in blocks if b).strip()
        if len(desc) < 120:
            continue
        loc = cats.get("location") or p.get("country")
        out.append(_raw(
            title=title or None, company=src["name"],
            location=primary_city(loc),
            country=resolve_country(loc, p.get("country")),
            department=cats.get("team") or cats.get("department"),
            employment_type=cats.get("commitment"),
            posted_date=parse_date(p.get("createdAt")),
            description=desc,
            responsibilities=reqs[:20] or text_bullets(desc),
            requirements=reqs[:20],
            source_url=p.get("hostedUrl"),
            application_url=p.get("applyUrl") or p.get("hostedUrl"),
            is_direct_apply=True, req_id=p.get("id"),
        ))
    return out


# -- SmartRecruiters (Etihad) --------------------------------------------
# Two calls per posting: the list carries no description, and a posting with
# no description fails the length floor below and would be dropped — so this
# would have shipped an employer that produced zero rows while reporting "ok".
def fetch_smartrecruiters(src: dict) -> list[dict]:
    slug = src["endpoint"]["slug"]
    base = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    resp = _check(_get(f"{base}?limit=100", headers={"Accept": "application/json"}),
                  f"{src['name']} list")
    try:
        listing = resp.json()
    except ValueError as e:
        raise SourceError(f"{src['name']}: non-JSON response ({e})")

    out = []
    for p in (listing.get("content") or []):
        title = (p.get("name") or "").strip()
        if not TITLE_PREFILTER.search(title):
            continue
        pid = p.get("id")
        if not pid:
            continue
        try:
            det = _get(f"{base}/{pid}", headers={"Accept": "application/json"}).json()
        except Exception:
            continue                       # one dead posting is not a dead source

        sections = ((det.get("jobAd") or {}).get("sections") or {})
        def _sec(key):
            return strip_html(((sections.get(key) or {}).get("text")) or "")
        desc = "\n\n".join(x for x in (_sec("jobDescription"),
                                        _sec("qualifications"),
                                        _sec("additionalInformation")) if x).strip()
        if len(desc) < 120:
            continue

        loc = det.get("location") or p.get("location") or {}
        # fullLocation is "Abu Dhabi, , United Arab Emirates" — the city field
        # on its own is what the location filter and the city chips read.
        city = loc.get("city") or ""
        full = loc.get("fullLocation") or city
        out.append(_raw(
            title=title or None, company=src["name"],
            location=primary_city(city or full),
            country=resolve_country(full, loc.get("country")),
            department=(det.get("function") or {}).get("label"),
            employment_type=(det.get("typeOfEmployment") or {}).get("label"),
            posted_date=parse_date(det.get("releasedDate") or p.get("releasedDate")),
            description=desc,
            responsibilities=text_bullets(_sec("jobDescription"))[:20],
            requirements=text_bullets(_sec("qualifications"))[:20],
            source_url=det.get("postingUrl") or p.get("ref"),
            application_url=det.get("applyUrl") or det.get("postingUrl"),
            is_direct_apply=True, req_id=str(pid),
        ))
    return out


# -- LinkedIn guest job search ------------------------------------------
_LI_CARD_RE = re.compile(
    r'<a[^>]+href="(https://[a-z]{0,3}\.?linkedin\.com/jobs/view/[^"?]+)[^"]*"', re.I)


def fetch_linkedin(src: dict) -> list[dict]:
    base = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    locations = ["Dubai, United Arab Emirates", "United Arab Emirates",
                 "Saudi Arabia", "Malaysia", "Oman"]
    found: dict[str, str] = {}
    for term in SEARCH_TERMS[:5]:
        for loc in locations:
            url = (f"{base}?keywords={quote(term)}&location={quote(loc)}"
                   f"&f_TPR=r2592000&start=0")
            try:
                resp = _get(url)
            except requests.RequestException as e:
                raise SourceError(f"{src['name']}: {type(e).__name__}")
            if resp.status_code in (403, 429):
                raise SourceBlocked(f"{src['name']} search: HTTP {resp.status_code}")
            if resp.status_code >= 400:
                continue
            for link in _LI_CARD_RE.findall(resp.text):
                jid_m = re.search(r"-(\d+)$", link)
                if jid_m:
                    found.setdefault(jid_m.group(1), link)
            time.sleep(0.4)

    candidates = [(jid, u) for jid, u in found.items()
                  if TITLE_PREFILTER.search(u.rsplit("/", 1)[-1].replace("-", " "))]
    candidates = candidates[:MAX_DETAIL_PER_SOURCE]

    def detail(item: tuple[str, str]) -> dict | None:
        jid, url = item
        api = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{jid}"
        try:
            resp = _get(api)
            if resp.status_code >= 400:
                return None
        except requests.RequestException:
            return None
        page = resp.text
        m = re.search(r'<div[^>]*class="[^"]*(?:show-more-less-html__markup|'
                      r'description__text)[^"]*"[^>]*>(.*?)</div>', page, re.S | re.I)
        desc_html = m.group(1) if m else ""
        desc = strip_html(desc_html)
        if len(desc) < 150:
            return None
        t = re.search(r'<h2[^>]*class="[^"]*top-card-layout__title[^"]*"[^>]*>(.*?)</h2>',
                      page, re.S | re.I)
        c = re.search(r'<a[^>]*class="[^"]*topcard__org-name-link[^"]*"[^>]*>(.*?)</a>',
                      page, re.S | re.I)
        l = re.search(r'<span[^>]*class="[^"]*topcard__flavor--bullet[^"]*"[^>]*>(.*?)</span>',
                      page, re.S | re.I)
        company = strip_html(c.group(1)) if c else None
        if not company:
            return None
        loc_raw = strip_html(l.group(1)) if l else None
        return _raw(
            title=strip_html(t.group(1)) if t else None,
            company=company,
            location=primary_city(loc_raw),
            country=resolve_country(loc_raw),
            description=desc,
            responsibilities=html_bullets(desc_html) or text_bullets(desc),
            requirements=[],
            source_url=url,
            # LinkedIn's public view IS the application entry point for a guest;
            # it is a job board, not the employer, hence is_direct_apply False.
            application_url=url, is_direct_apply=False, req_id=None,
        )

    out = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for fut in as_completed([pool.submit(detail, c) for c in candidates]):
            row = fut.result()
            if row:
                out.append(row)
    return out


# -- Michael Page --------------------------------------------------------
_MP_RE = re.compile(r'href="(/job-detail/[^"#?]+)"')


def fetch_michaelpage(src: dict) -> list[dict]:
    host = src["endpoint"]["host"].rstrip("/")
    found: set[str] = set()
    for path in ("/jobs/finance", "/jobs/accounting-finance",
                 "/jobs/finance?page=2"):
        try:
            resp = _get(host + path)
        except requests.RequestException as e:
            raise SourceError(f"{src['name']}: {type(e).__name__}")
        if resp.status_code in (403, 429):
            raise SourceBlocked(f"{src['name']} list: HTTP {resp.status_code}")
        if resp.status_code >= 400:
            continue
        found.update(urljoin(host, p) for p in _MP_RE.findall(resp.text))

    candidates = [u for u in found
                  if TITLE_PREFILTER.search(u.replace("-", " ").replace("/", " "))]
    candidates = candidates[:MAX_DETAIL_PER_SOURCE]

    def detail(url: str) -> dict | None:
        try:
            resp = _get(url)
            if resp.status_code >= 400:
                return None
        except requests.RequestException:
            return None
        page = resp.text
        ld = None
        for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>',
                             page, re.S):
            try:
                cand = json.loads(m.group(1))
            except ValueError:
                continue
            if isinstance(cand, dict) and cand.get("@type") == "JobPosting":
                ld = cand
                break
        if not ld:
            return None
        desc_html = ld.get("description") or ""
        desc = strip_html(desc_html)
        if len(desc) < 150:
            return None
        loc = ld.get("jobLocation")
        loc = loc[0] if isinstance(loc, list) and loc else loc
        addr = (loc or {}).get("address") or {}
        org = (ld.get("hiringOrganization") or {}).get("name")
        # Michael Page posts most roles confidentially; the recruiter IS the
        # employer of record for the application in that case.
        if not org or org.lower().startswith("michael page"):
            org = "Michael Page (client confidential)"
        return _raw(
            title=(ld.get("title") or "").strip() or None,
            company=org,
            location=primary_city(addr.get("addressLocality")),
            country=resolve_country(addr.get("addressCountry"),
                                    addr.get("addressLocality")),
            employment_type=(ld.get("employmentType") or "").replace("_", "-").title() or None,
            posted_date=parse_date(ld.get("datePosted")),
            closing_date=parse_date(ld.get("validThrough")),
            description=desc,
            responsibilities=html_bullets(desc_html) or text_bullets(desc),
            requirements=[],
            source_url=url, application_url=url, is_direct_apply=False,
            req_id=None,
        )

    out = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for fut in as_completed([pool.submit(detail, c) for c in candidates]):
            row = fut.result()
            if row:
                out.append(row)
    return out


# -- Firecrawl-backed HTML (Alshaya, GulfTalent, Bayt, Indeed) -----------
def firecrawl_key() -> str | None:
    return os.environ.get("FIRECRAWL_API_KEY") or None


def firecrawl_scrape(url: str, formats: Iterable[str] = ("markdown", "links")) -> dict:
    """Direct Firecrawl HTTP API — MCP is not available inside GitHub Actions."""
    key = firecrawl_key()
    if not key:
        raise SourceError("FIRECRAWL_API_KEY not set")
    resp = requests.post(
        FIRECRAWL_API,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"url": url, "formats": list(formats), "onlyMainContent": False,
              "proxy": "auto", "maxAge": 0},
        timeout=90)
    if resp.status_code in (401, 402, 403):
        raise SourceError(f"Firecrawl auth/credit error: HTTP {resp.status_code}")
    if resp.status_code >= 400:
        raise SourceError(f"Firecrawl: HTTP {resp.status_code}")
    body = resp.json()
    if not body.get("success"):
        raise SourceError(f"Firecrawl: {body.get('error', 'unknown error')}")
    return body.get("data") or {}


def fetch_firecrawl_html(src: dict) -> list[dict]:
    """Sources that block plain requests. Requires FIRECRAWL_API_KEY."""
    if not firecrawl_key():
        raise SourceError("FIRECRAWL_API_KEY not set")
    ep = src["endpoint"]
    listing = firecrawl_scrape(ep["url"], formats=("markdown", "links", "rawHtml"))
    blob = " ".join(filter(None, [
        listing.get("rawHtml") or "", listing.get("markdown") or "",
        " ".join(listing.get("links") or []),
    ]))
    ids: list[str] = []
    for m in re.finditer(ep["link_re"], blob):
        val = m.group(1)
        if val not in ids:
            ids.append(val)
    ids = ids[:MAX_DETAIL_PER_SOURCE]
    if not ids:
        return []

    out = []
    for ident in ids:
        url = ep["detail_tpl"].format(id=ident)
        try:
            page = firecrawl_scrape(url, formats=("markdown",))
        except SourceError:
            continue
        md = page.get("markdown") or ""
        text = strip_html(md)
        if len(text) < 200:
            continue
        meta = page.get("metadata") or {}
        title = meta.get("title") or (md.splitlines()[0].lstrip("# ").strip()
                                      if md else None)
        if not title or not TITLE_PREFILTER.search(title):
            continue
        # Location comes from an explicit "Location: X" line if the page has
        # one. Scanning the body prose for a city name guesses, and a guessed
        # country is a fabricated country.
        loc_m = re.search(r"(?:^|\n)\s*(?:\*\*)?(?:Location|City|Based in)"
                          r"(?:\*\*)?\s*[:\-]\s*([A-Za-z ,'\-]{3,60})", text, re.I)
        loc_raw = loc_m.group(1).strip() if loc_m else None
        out.append(_raw(
            title=title.strip(), company=src["name"] if src["kind"] == "employer" else None,
            location=primary_city(loc_raw),
            country=resolve_country(loc_raw),
            description=text,
            responsibilities=text_bullets(text),
            requirements=[],
            source_url=url, application_url=url,
            is_direct_apply=(src["kind"] == "employer"),
            req_id=str(ident),
        ))
    return out


def fetch_discover_only(src: dict) -> list[dict]:
    """Employers with no resolved ATS. Discovery runs before this; if it found
    nothing there is genuinely nothing to scrape — say so, invent nothing."""
    raise SourceError(
        "no ATS endpoint could be resolved from the public careers page")


ADAPTERS = {
    "oracle": fetch_oracle,
    "phenom": fetch_phenom,
    "teamtailor": fetch_teamtailor,
    "successfactors": fetch_successfactors,
    "lever": fetch_lever,
    "smartrecruiters": fetch_smartrecruiters,
    "linkedin": fetch_linkedin,
    "michaelpage": fetch_michaelpage,
    "firecrawl_html": fetch_firecrawl_html,
    "discover_only": fetch_discover_only,
}


# ===========================================================================
# Runtime endpoint discovery
# ===========================================================================

ATS_PATTERNS = [
    ("oracle", re.compile(
        r"https?://([a-z0-9\-]+\.fa\.[a-z0-9\-]+\.(?:ocs\.)?oraclecloud\.com)"
        r"/hcmUI/CandidateExperience/[a-z\-]+/sites/([A-Za-z0-9_]+)", re.I)),
    ("lever", re.compile(r"https?://jobs\.lever\.co/([a-z0-9\-]+)", re.I)),
    ("teamtailor", re.compile(r"https?://([a-z0-9\-]+\.teamtailor\.com|"
                              r"careers\.[a-z0-9\-\.]+)/jobs\b", re.I)),
    ("phenom", re.compile(r"https?://(careers\.[a-z0-9\-\.]+)/[a-z\-]+/[a-z]{2}/"
                          r"(?:search-results|job/)", re.I)),
]


def load_endpoint_cache() -> dict:
    try:
        with open(ENDPOINT_CACHE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_endpoint_cache(cache: dict) -> None:
    try:
        os.makedirs(os.path.dirname(ENDPOINT_CACHE), exist_ok=True)
        with open(ENDPOINT_CACHE, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=2, sort_keys=True)
    except OSError as e:
        log.warning("endpoint cache not written: %s", e)


def discover_endpoint(src: dict) -> dict | None:
    """Re-read the employer's public careers page and pull the live ATS link.

    Career URLs rot, so the SOURCES values are a fallback rather than truth.
    Returns an endpoint dict on success, None to keep the existing one.
    """
    page_url = src.get("discover")
    if not page_url:
        return None
    try:
        resp = _get(page_url, timeout=20)
        if resp.status_code >= 400:
            return None
        body = _html.unescape(resp.text)
    except requests.RequestException:
        return None

    for adapter, pattern in ATS_PATTERNS:
        m = pattern.search(body)
        if not m:
            continue
        if adapter == "oracle":
            return {"_adapter": "oracle", "host": f"https://{m.group(1)}",
                    "site": m.group(2)}
        if adapter == "lever":
            return {"_adapter": "lever", "slug": m.group(1)}
        if adapter == "teamtailor":
            return {"_adapter": "teamtailor", "host": f"https://{m.group(1)}"}
        if adapter == "phenom":
            return {"_adapter": "phenom", "host": f"https://{m.group(1)}",
                    "locale": "en_global", "country": "global", "site": "global/en"}
    return None


def resolve_sources(sources: list[dict]) -> list[dict]:
    """Apply runtime discovery over the seed config, caching what resolved."""
    cache = load_endpoint_cache()
    resolved = []
    for src in sources:
        s = json.loads(json.dumps(src))   # deep copy; SOURCES stays pristine
        found = None
        try:
            found = discover_endpoint(s)
        except Exception as e:                      # discovery must never fail a run
            log.debug("discovery failed for %s: %s", s["name"], e)
        if found:
            adapter = found.pop("_adapter")
            s["adapter"] = adapter
            s["endpoint"] = {**s.get("endpoint", {}), **found}
            cache[s["name"]] = {"adapter": adapter, "endpoint": found,
                                "resolved_at": datetime.now(UTC).isoformat()}
            log.info("  discovered %s → %s %s", s["name"], adapter, found)
        elif s["name"] in cache and not s.get("endpoint"):
            c = cache[s["name"]]
            s["adapter"] = c.get("adapter", s["adapter"])
            s["endpoint"] = {**s.get("endpoint", {}), **c.get("endpoint", {})}
            log.info("  %s → cached endpoint", s["name"])
        resolved.append(s)
    save_endpoint_cache(cache)
    return resolved


# ===========================================================================
# Normalization into contract shape
# ===========================================================================

def normalize_job(raw: dict, src: dict, now_iso: str) -> dict | None:
    """Raw adapter dict → a contract-shaped Job. None if unusable."""
    title = (raw.get("title") or "").strip()
    company = (raw.get("company") or src["name"] or "").strip()
    if not title or not company:
        return None
    desc = (raw.get("description") or "").strip()
    if len(desc) < 100:
        return None      # a card, not a detail page — refuse to build a row

    normalized = normalize_title(title)
    # Country comes from the location fields only. Never from the description —
    # a Cairo role whose blurb name-checks the Dubai head office must stay
    # Egypt, not become UAE.
    country = raw.get("country") or resolve_country(raw.get("location"))
    text = " \n ".join([desc, " ".join(raw.get("responsibilities") or []),
                        " ".join(raw.get("requirements") or [])])

    exp_min, exp_max = parse_experience(text)
    sal_min, sal_max, sal_cur = parse_salary(text)

    resp = [r for r in (raw.get("responsibilities") or []) if r][:20]
    reqs = [r for r in (raw.get("requirements") or []) if r][:20]
    if not resp:
        resp = text_bullets(desc)[:20]

    skills = list(raw.get("skills") or [])
    if not skills:
        for label, pat in (("FP&A", r"fp&a|financial planning"),
                           ("IFRS", r"\bifrs\b"), ("Consolidation", r"consolidat"),
                           ("Budgeting", r"budget"), ("Forecasting", r"forecast"),
                           ("Power BI", r"power\s*bi"), ("SAP", r"\bsap\b"),
                           ("D365", r"d365|dynamics 365"), ("Excel", r"excel"),
                           ("Business Partnering", r"business partner"),
                           ("IFRS 16", r"ifrs\s*16|lease account"),
                           ("Variance Analysis", r"variance analys")):
            if re.search(pat, text, re.I):
                skills.append(label)
    skills = list(dict.fromkeys(skills))[:12]

    job: dict[str, Any] = {
        "id": None,
        "company": company,
        "company_group": src.get("group"),
        "title": title,
        "normalized_title": normalized,
        "location": raw.get("location"),
        "country": country,
        "region": region_for(country),
        "department": raw.get("department") or None,
        "employment_type": raw.get("employment_type") or None,
        "posted_date": raw.get("posted_date"),
        "closing_date": raw.get("closing_date"),
        "scraped_at": now_iso,
        "last_verified_at": now_iso,
        "status": "ACTIVE",
        "source": src["name"],
        "sources": [src["name"]],
        "source_url": raw.get("source_url"),
        "application_url": raw.get("application_url") if is_acceptable_apply_url(
            raw.get("application_url")) else None,
        "is_direct_apply": bool(raw.get("is_direct_apply")),
        "application_url_verified": False,
        "source_confidence": src.get("confidence", "low"),
        "salary_min": sal_min, "salary_max": sal_max, "salary_currency": sal_cur,
        "experience_min": exp_min, "experience_max": exp_max,
        "description": desc[:8000],
        "responsibilities": resp,
        "requirements": reqs,
        "skills": skills,
        "nationality_requirement": None,
        "work_authorization_requirement": None,
        "emiratisation_requirement": False,
        "saudization_requirement": False,
        "candidate_fit_score": 0, "employer_score": 0,
        "career_upside_score": 0, "opportunity_score": 0,
        "score_breakdown": {}, "tier": "D", "application_priority": "SKIP",
        "why_fit": [], "watch_out": [], "resume_match": None,
        "duplicate_group": None, "is_excluded": False, "exclusion_reason": None,
        "_req_id": raw.get("req_id") or None,
    }

    low = text.lower()
    if re.search(r"\bemiratis|\bemiratiz|\bnafis\b", low):
        job["emiratisation_requirement"] = True
    if re.search(r"\bsaudi[sz]ation|\bnitaqat\b", low):
        job["saudization_requirement"] = True
    m = NATIONALITY_ONLY.search(text)
    if m:
        job["nationality_requirement"] = m.group(0).strip()
    m = re.search(r"\b(valid|must (?:hold|have))[^.]{0,60}?"
                  r"(work (?:visa|permit)|residence visa|right to work)[^.]{0,40}", text, re.I)
    if m:
        job["work_authorization_requirement"] = m.group(0).strip()[:200]

    job["id"] = job_id(job)
    job["status"] = freshness_status(job["posted_date"])
    reason = exclusion_for(normalized, text)
    if reason:
        job["is_excluded"] = True
        job["exclusion_reason"] = reason
    return job


# ===========================================================================
# Pipeline
# ===========================================================================

def load_previous(path: str = OUT_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def scrape_all(sources: list[dict], now_iso: str
               ) -> tuple[list[dict], list[dict]]:
    """Run every adapter. Returns (jobs, source status rows)."""
    jobs: list[dict] = []
    statuses: list[dict] = []

    for src in sources:
        fn = ADAPTERS.get(src["adapter"])
        row = {"name": src["name"], "kind": src["kind"], "status": "error",
               "jobs_found": 0, "detail": "", "last_success": None}
        if fn is None:
            row["detail"] = f"no adapter '{src['adapter']}'"
            statuses.append(row)
            continue

        t0 = time.time()
        try:
            raws = fn(src)
            normalized = []
            for r in raws:
                nj = normalize_job(r, src, now_iso)
                if nj:
                    normalized.append(nj)
            jobs.extend(normalized)
            row["jobs_found"] = len(normalized)
            row["status"] = "ok" if normalized else "empty"
            row["last_success"] = now_iso if normalized else None
            if not normalized:
                row["detail"] = "reachable, but no senior-finance roles matched"
            log.info("  %-24s %-7s %3d jobs  (%.1fs)", src["name"],
                     row["status"], row["jobs_found"], time.time() - t0)
        except SourceBlocked as e:
            row["status"] = "blocked"
            row["detail"] = str(e)[:400]
            log.info("  %-24s blocked  %s", src["name"], row["detail"])
        except SourceError as e:
            row["status"] = "error"
            row["detail"] = str(e)[:400]
            log.info("  %-24s error    %s", src["name"], row["detail"])
        except requests.RequestException as e:
            row["status"] = "error"
            row["detail"] = f"{type(e).__name__}: {str(e)[:300]}"
            log.info("  %-24s error    %s", src["name"], row["detail"])
        except Exception as e:                      # never let one source kill the run
            row["status"] = "error"
            row["detail"] = f"{type(e).__name__}: {str(e)[:300]}"
            log.warning("  %-24s error    %s", src["name"], row["detail"])
        statuses.append(row)

    return jobs, statuses


def carry_forward(statuses: list[dict], previous: dict) -> None:
    """A failed source keeps its previous last_success (contract)."""
    prev_by_name = {s.get("name"): s for s in (previous.get("sources") or [])}
    for row in statuses:
        if row["status"] != "ok":
            old = prev_by_name.get(row["name"]) or {}
            row["last_success"] = old.get("last_success")


def merge_with_previous(fresh: list[dict], previous: dict,
                        statuses: list[dict], now_iso: str) -> tuple[list[dict], int]:
    """Keep prior jobs from sources that failed this run, so a blocked scrape
    never blanks the dataset. Returns (jobs, stale_removed)."""
    ok_sources = {s["name"] for s in statuses if s["status"] in ("ok", "empty")}
    fresh_ids = {j["id"] for j in fresh}
    kept, stale_removed = list(fresh), 0

    for old in (previous.get("jobs") or []):
        if not isinstance(old, dict) or old.get("id") in fresh_ids:
            continue
        primary = old.get("source")
        if primary in ok_sources:
            # That source ran fine and did not re-confirm this row — it is gone.
            stale_removed += 1
            continue
        status = freshness_status(old.get("posted_date"))
        if status == "STALE":
            stale_removed += 1
            continue
        carried = dict(old)
        carried["status"] = status          # age it, do not re-verify it
        kept.append(carried)                # last_verified_at deliberately untouched
    return kept, stale_removed


def finalize(jobs: list[dict]) -> None:
    """Score, rank, and attach the narrative fields."""
    for j in jobs:
        score_job(j)
        j["why_fit"] = build_why_fit(j)
        j["watch_out"] = build_watch_out(j)
        j["resume_match"] = build_resume_match(j)
        j.pop("_req_id", None)
        j.pop("_was_verified", None)
    jobs.sort(key=lambda j: (
        0 if j.get("is_excluded") else 1,
        j.get("opportunity_score", 0),
        j.get("candidate_fit_score", 0),
    ), reverse=True)


def build(write: bool = False, path: str = OUT_PATH) -> dict:
    """Full refresh. Returns the contract dict; writes it when write=True."""
    started = datetime.now(UTC)
    now_iso = started.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    previous = load_previous(path)

    log.info("resolving career endpoints…")
    sources = resolve_sources(SOURCES)

    if not firecrawl_key():
        log.info("FIRECRAWL_API_KEY not set — Firecrawl-backed sources will "
                 "report status 'error' and be skipped")

    log.info("scraping %d sources…", len(sources))
    fresh, statuses = scrape_all(sources, now_iso)
    carry_forward(statuses, previous)

    # Remember which links were good last time, so a newly-failing link can be
    # told apart from one that was never verified.
    prev_verified = {j.get("id") for j in (previous.get("jobs") or [])
                     if isinstance(j, dict) and j.get("application_url_verified")}
    for j in fresh:
        if j["id"] in prev_verified:
            j["_was_verified"] = True

    merged, stale_removed = merge_with_previous(fresh, previous, statuses, now_iso)
    deduped, dup_removed = deduplicate(merged)

    log.info("validating %d application URLs…", len(deduped))
    url_stats = validate_all(deduped)

    finalize(deduped)

    active = [j for j in deduped if not j.get("is_excluded")
              and j.get("status") not in ("CLOSED", "REMOVED")]
    by_country = {"UAE": 0, "Saudi Arabia": 0, "Malaysia": 0, "Oman": 0}
    for j in active:
        c = j.get("country")
        if c in by_country:
            by_country[c] += 1
        elif c:
            by_country[c] = by_country.get(c, 0) + 1

    payload = {
        "generated_at": now_iso,
        "next_refresh": (started + timedelta(days=1)).replace(
            microsecond=0).isoformat().replace("+00:00", "Z"),
        "sources": statuses,
        "stats": {
            "total_active": len(active),
            "high_fit": sum(1 for j in active if j["opportunity_score"] >= 75),
            "s_tier": sum(1 for j in active if j["tier"] == "S"),
            "by_country": by_country,
            "duplicates_removed": dup_removed,
            "stale_removed": stale_removed,
            "sources_ok": sum(1 for s in statuses if s["status"] == "ok"),
            "sources_attempted": len(statuses),
            "urls_verified": url_stats["verified"],
            "urls_unverified": url_stats["unverified"],
            "urls_broken": url_stats["broken"],
        },
        "jobs": deduped,
    }

    if write:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        log.info("wrote %s (%d jobs, %.0f KB)", path, len(deduped),
                 os.path.getsize(path) / 1024)
    return payload


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        stream=sys.stdout)
    data = build(write=True)
    st = data["stats"]
    print()
    print(f"sources : {st['sources_ok']}/{st['sources_attempted']} ok")
    for s in data["sources"]:
        flag = {"ok": "  ok", "empty": "  --", "blocked": " BLK", "error": " ERR"}[s["status"]]
        print(f"  {flag} {s['name']:<24s} {s['jobs_found']:>3d}  {s['detail'][:70]}")
    print()
    print(f"active  : {st['total_active']}   high-fit: {st['high_fit']}   S-tier: {st['s_tier']}")
    print(f"dedupe  : {st['duplicates_removed']} removed   stale: {st['stale_removed']} removed")
    print(f"urls    : {st['urls_verified']} verified, {st['urls_unverified']} unverified, "
          f"{st['urls_broken']} broken")
    print(f"country : {st['by_country']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
