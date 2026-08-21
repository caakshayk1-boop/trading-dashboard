#!/usr/bin/env python3
"""
ipo_radar.py — mainboard NSE IPOs: what is open, what is coming, and a verdict.

NOT the same artefact as ipo_tracker.py. That one answers "how have this year's
LISTINGS performed", measured from the first traded close. This one answers
"should I apply to what is open right now", which is a different question with
a different data source and a different failure mode.

WHERE THE DATA COMES FROM

NSE's own public endpoints, warmed through a session cookie the way a browser
gets one:

    /api/all-upcoming-issues?category=ipo   name, symbol, band, size, dates
    /api/ipo-current-issue                  the same plus LIVE subscription
    /api/public-past-issues                 1,400+ closed issues, for context

MAINBOARD ONLY. The upcoming feed mixes `series: EQ` (mainboard) and
`series: SME` in one response, and SME issues are a different asset class with
different lot sizes, different liquidity and a different risk profile. Filtered
on series, not on issue size, because size is a proxy and series is the fact.

WHAT IT REFUSES TO INVENT

The brief asks for lot size, sector, valuation multiples, anchor detail and GMP.
NSE's public feeds carry none of them. So they are reported as NOT AVAILABLE and
excluded from the score — never estimated, never back-filled from a guess.

That is not a shortfall to apologise for, it is the whole argument of this site:
a score computed from four real inputs and honest about the five it lacks is
worth more than a /100 that looks complete because the gaps were filled in with
plausible numbers. GMP especially — it is an unofficial grey-market quote with
no audit trail, and the brief itself says it must not drive the decision.

THE SCORE

Only the dimensions with real data underneath them:

    Demand      /40   subscription multiple, the strongest public signal there is
    Size        /25   issue size — bigger books get institutional scrutiny
    Pricing     /20   band width; a wide band is the underwriter hedging
    Window      /15   whether the book is still open long enough to act

Capped at what is measurable. A verdict is never issued on demand alone before
the book has run: an IPO in hour one of day one is WATCH, not AVOID, because
there is not yet evidence either way.

VERDICTS
    APPLY         strong, broad demand and a mainboard-sized book
    APPLY - SMALL   good demand but thin evidence or a small book
    WATCH         open but undecided, or not enough has happened yet
    AVOID         demand has failed to materialise near the close

Run:  python3 ipo_radar.py            # print
      python3 ipo_radar.py --json     # write data/ipo_radar.json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
from urllib.parse import quote_plus, unquote
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

NSE = "https://www.nseindia.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
REFERER = f"{NSE}/market-data/all-upcoming-issues-ipo"
ROOT = Path(__file__).parent
OUT = ROOT / "data" / "ipo_radar.json"

# Mainboard. The upcoming feed mixes both classes in one response.
MAINBOARD_SERIES = {"EQ"}


def _session():
    """A warmed NSE session. The API 403s without the cookie the homepage sets."""
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json",
                      "Accept-Language": "en-US,en;q=0.9", "Referer": REFERER})
    try:
        s.get(NSE, timeout=20)          # 403 is fine; it still sets the cookie
    except Exception as e:              # noqa: BLE001
        log.warning(f"NSE warmup failed: {e}")
    return s


def _get(s, path):
    try:
        r = s.get(f"{NSE}/{path}", timeout=25)
        if r.status_code != 200:
            log.warning(f"NSE {path}: HTTP {r.status_code}")
            return []
        d = r.json()
        return d.get("data", d) if isinstance(d, dict) else d
    except Exception as e:              # noqa: BLE001
        log.warning(f"NSE {path}: {e}")
        return []


def _date(s):
    """NSE ships '25-Aug-2026' in one feed and '20-AUG-2026' in another."""
    if not s or s == "-":
        return None
    for fmt in ("%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(str(s).strip().title(), fmt).date()
        except ValueError:
            continue
    return None


def _band(s):
    """'Rs.750 to Rs.788' -> (750.0, 788.0). A single price returns (p, p)."""
    if not s:
        return None, None
    nums = [float(n.replace(",", "")) for n in re.findall(r"[\d,]+\.?\d*", str(s))]
    if not nums:
        return None, None
    return (min(nums), max(nums)) if len(nums) > 1 else (nums[0], nums[0])


def _num(v):
    try:
        f = float(str(v).replace(",", ""))
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def score(row):
    """Score only what there is data for, and say what was not measured."""
    parts, missing = {}, []
    sub = row.get("subscription_x")
    lo, hi = row.get("price_low"), row.get("price_high")
    shares = _num(row.get("issue_size_shares"))
    amount = row.get("issue_size_cr")

    # Demand /40 — the strongest public signal, and the only one that reflects
    # what money actually did rather than what a document claims.
    if sub is None:
        missing.append("subscription (book not open yet)")
    else:
        parts["Demand"] = (40 if sub >= 10 else 32 if sub >= 5 else 24 if sub >= 3
                           else 16 if sub >= 1 else 6, 40)

    # Size /25 — a bigger book is priced under more institutional scrutiny and
    # trades with more liquidity afterwards. Not a quality claim about the
    # business, and labelled so.
    if amount is None:
        missing.append("issue size in rupees")
    else:
        parts["Size"] = (25 if amount >= 2000 else 20 if amount >= 750 else
                         14 if amount >= 250 else 8, 25)

    # Pricing /20 — a wide band is the underwriter hedging its own uncertainty.
    if lo and hi and lo > 0:
        w = (hi - lo) / lo * 100
        parts["Pricing"] = (20 if w <= 3 else 15 if w <= 6 else 10 if w <= 10 else 5, 20)
    else:
        missing.append("price band")

    # Window /15 — can this still be acted on.
    d = row.get("days_left")
    if d is None:
        missing.append("issue window")
    else:
        parts["Window"] = (15 if d >= 2 else 10 if d >= 1 else 4, 15)

    got = sum(v for v, _ in parts.values())
    of = sum(m for _, m in parts.values())

    # "Not measured" has to be computed from the row, not hardcoded. The static
    # list said "lot size · anchor book · GMP" on cards that were displaying a
    # lot size, an anchor book and a GMP three lines above it — the enrichment
    # pass filled them in and this list never learned. A card that contradicts
    # itself is worse than one that admits a gap.
    #
    # Two different kinds of gap, kept separate:
    #   absent  — nothing anywhere publishes it for this issue
    #   unscored — present on the card, deliberately outside the score
    absent = list(missing)
    for label, key in (("lot size", "lot_size"),
                       ("minimum application", "min_investment"),
                       ("fresh/OFS split", "fresh_issue_cr"),
                       ("anchor book", "anchor_cr"),
                       ("listing date", "listing_date")):
        if row.get(key) in (None, "", 0):
            absent.append(label)
    # Never available from any source this build reads.
    absent += ["sector", "audited financials", "valuation multiples"]

    unscored = []
    if row.get("gmp_text"):
        unscored.append("grey market premium — shown, deliberately not scored")
    if row.get("lot_size"):
        unscored.append("lot size and minimum application — facts, not judgements")

    return {"points": got, "of": of,
            "pct": round(got / of * 100) if of else None,
            "parts": {k: f"{v}/{m}" for k, (v, m) in parts.items()},
            "not_measured": absent,
            "shown_not_scored": unscored}


def verdict(row, sc):
    """A verdict, plus the reason and the reason it might be wrong."""
    sub, d = row.get("subscription_x"), row.get("days_left")
    amount = row.get("issue_size_cr")

    if row["phase"] == "upcoming":
        return ("WATCH", "Book has not opened — no demand evidence exists yet.",
                "Re-check once subscription starts; nothing here is a judgement "
                "on the business.")
    if sub is None:
        # A book past its close date with no figure is not "not open yet" — it
        # is closed and NSE has dropped it from the live feed. Saying the first
        # about the second is the worst kind of wrong: confidently backwards.
        if row.get("close_date") and row["close_date"] < date.today().isoformat():
            return ("WATCH", "Book has closed; NSE no longer publishes a live "
                    "subscription figure for it.",
                    "The final multiple is not in the public feed, so no verdict "
                    "on demand can be offered after the fact.")
        return ("WATCH", "Open, but NSE has not published a subscription figure yet.",
                "Demand is the only dimension measurable from public data, and it "
                "is not there yet.")

    strong = sub >= 5
    late = d is not None and d <= 1
    big = amount is not None and amount >= 750

    if strong and big:
        return ("APPLY", f"Subscribed {sub:.1f}x on a mainboard book"
                + (f" of about Rs.{amount:,.0f} Cr" if amount else "") + ".",
                "Demand is the ONLY dimension measured. Nothing here says the "
                "business or the valuation is good.")
    if strong:
        return ("APPLY - SMALL", f"Subscribed {sub:.1f}x, but the book is small"
                + (f" (about Rs.{amount:,.0f} Cr)" if amount else "") + ".",
                "Small books move violently both ways after listing. Size the "
                "application accordingly.")
    if late and sub < 1:
        return ("AVOID", f"Only {sub:.2f}x subscribed with the book about to close.",
                "An undersubscribed mainboard issue usually lists at or below "
                "its band.")
    if late and sub < 3:
        return ("WATCH", f"{sub:.1f}x near the close — demand is real but thin.",
                "Retail allotment is likely, which in a weak book is not the "
                "advantage it sounds like.")
    return ("WATCH", f"{sub:.1f}x so far, with time left on the book.",
            "Most subscription arrives on the final day; this figure is not final.")


# ── Enrichment ───────────────────────────────────────────────────────────────
# NSE's feeds are authoritative for band, size, dates and subscription, and they
# carry none of: lot size, minimum application, the fresh/OFS split, face value,
# anchor book, or GMP. Those live on Chittorgarh, which sits behind a Cloudflare
# challenge that plain HTTP cannot pass. crawler.py gets through it.
#
# Rules this pass follows:
#   * Only OPEN and UPCOMING issues are enriched. Enriching 1,400 closed issues
#     would burn credits to decorate history nobody is deciding on.
#   * Every enriched field is tagged with its source, separately from the NSE
#     fields, so a reader can see which numbers are official and which are not.
#   * GMP is carried but never scored. It is an unofficial grey-market quote
#     with no audit trail and no regulator behind it; the brief says it must not
#     drive the decision, and here it does not — it is context, labelled as such.
#   * Enrichment NEVER blanks a field it fails to fetch. A failed pass leaves the
#     previous value in place, the same rule the rest of this site follows: a
#     partial build must not replace a good dataset.
CHITTORGARH_FIELDS = {
    "lot_size": "number of shares in one application",
    "min_investment": "rupees for one retail lot",
    "fresh_issue_cr": "fresh issue in Rs crore",
    "ofs_cr": "offer for sale in Rs crore",
    "face_value": "face value per share in rupees",
    "anchor_cr": "amount raised from anchor investors in Rs crore",
    "listing_date": "expected listing date",
    "gmp_text": "grey market premium as quoted, verbatim",
}

# Business performance. A second extract against the same page, kept separate
# because these are ACCOUNTING figures rather than issue mechanics, and because
# a failure here must not cost the lot size a reader can otherwise still use.
#
# None of it enters the score. The score measures public DEMAND, which is a
# fact about what money did; these are the company's own filed numbers, and
# turning them into points would mean asserting a valuation view the site has
# no basis for. They are published so a reader can form theirs.
FINANCIAL_FIELDS = {
    "revenue_cr": "most recent full-year revenue in Rs crore",
    "revenue_growth_pct": "year-on-year revenue growth percent",
    "pat_cr": "most recent full-year profit after tax in Rs crore",
    "pat_growth_pct": "year-on-year PAT growth percent",
    "pat_margin_pct": "PAT as a percent of revenue",
    "roe_pct": "return on equity percent",
    "debt_to_equity": "debt to equity ratio",
    "pe_post_issue": "post-issue price to earnings ratio",
    "peer_pe": "average price to earnings of listed peers",
    "fy_label": "the financial year these figures cover, e.g. FY26",
    "strengths": "the two or three strongest points in the company's favour, "
                 "as a single semicolon-separated string",
    "risks": "the two or three most serious risks or weaknesses, as a single "
             "semicolon-separated string",
    "use_of_proceeds": "what the fresh issue money is for, one short phrase",
}


# ── Enrichment, without any paid provider ────────────────────────────────────
# Firecrawl expired mid-build and every enriched field vanished with it. The
# replacement uses crawler.py, which has no API key anywhere in it:
#
#   discovery  DuckDuckGo HTML through Jina Reader → the exact Chittorgarh URL
#   detail     that Chittorgarh page → lot size, bands, fresh/OFS, anchor, dates
#   gmp        investorgain.com's live GMP table → the grey-market quote
#
# Discovery is a SEARCH, never a constructed URL. /ipo/<slug>/<id>/ resolves on
# the id alone and ignores the slug, so a guessed id returns a different
# company's IPO with a 200 and nothing to flag it — verified: id 2000 with the
# augmont slug returned A-One Steels.
#
# Extraction is regex over stated phrasing, not an LLM. It costs nothing, cannot
# rate-limit, and when it fails it returns None instead of a confident wrong
# number — which is the only acceptable failure mode on this page.
DDG = "https://duckduckgo.com/html/?q="
GMP_PAGE = "https://www.investorgain.com/report/live-ipo-gmp/331/ipo/"

CG_PATTERNS = {
    "lot_size":       r"lot size (?:for an application )?is\s*([\d,]+)",
    "min_investment": r"minimum amount required for application is\s*(?:₹|Rs\.?)?\s*([\d,]+)",
    "issue_size_cr":  r"book build[^₹]{0,80}₹\s*([\d,.]+)\s*crore",
    "fresh_issue_cr": r"fresh issue[^₹]{0,140}?aggregating to\s*₹\s*([\d,.]+)\s*crore",
    "ofs_cr":         r"offer for sale[^₹]{0,140}?aggregating to\s*₹\s*([\d,.]+)\s*crore",
    "anchor_cr":      r"raises\s*₹\s*([\d,.]+)\s*crore[^.]{0,40}anchor",
    "face_value":     r"face value[^₹]{0,40}₹\s*([\d,.]+)",
    "listing_date":   r"[Ll]isting [Dd]ate[^)]{0,80}?(?:fixed as|is)\s*(\w{3}\s+\d{1,2},\s*\d{4})",
}
_NUMERIC = {"lot_size", "min_investment", "issue_size_cr", "fresh_issue_cr",
            "ofs_cr", "anchor_cr", "face_value"}


def _num(s):
    try:
        return float(str(s).replace(",", "").strip(" .")) if s is not None else None
    except (TypeError, ValueError):
        return None


def _find_page(company: str) -> str | None:
    """The company's Chittorgarh URL, by search."""
    import crawler
    q = quote_plus(f"{company} IPO site:chittorgarh.com")
    p = crawler.fetch(DDG + q, timeout=50)
    if not p.ok:
        return None
    # DuckDuckGo wraps every result in /l/?uddg=<percent-encoded target>, so the
    # real URL is not present as plain text and a naive regex finds nothing on a
    # page that plainly contains the answer. Unquote first, then match.
    text = unquote(p.content)
    hits = re.findall(r"(https?://www\.chittorgarh\.com/ipo/[a-z0-9\-]+/\d+/?)", text)
    return (hits[0].rstrip("/") + "/") if hits else None


def _gmp_table() -> dict:
    """Live grey-market quotes, keyed by a normalised company name. One fetch."""
    import crawler
    out = {}
    p = crawler.fetch(GMP_PAGE, timeout=50)
    if not p.ok:
        return out
    # "Augmont Enterprises](...)IPO O | ₹**310** (39.34%)"
    for m in re.finditer(r"([A-Z][A-Za-z0-9&.,()\- ]{3,60})\]\([^)]*\)IPO\s*\w?\s*\|\s*"
                         r"₹\*\*([\d,]+)\*\*\s*\(([\d.]+)%\)", p.content):
        key = re.sub(r"[^a-z]", "", m.group(1).lower())
        out[key] = f"₹{m.group(2)} ({m.group(3)}%)"
    return out


def enrich(rows, key=None, previous=None):
    """Fill the fields NSE does not publish. Fails soft, never blanks.

    `key` is accepted and ignored. It was the paid provider's API key; the
    signature is kept so a caller passing one still works during migration.
    """
    prev = {r.get("symbol"): r for r in (previous or [])}
    for r in rows:
        for f in list(CHITTORGARH_FIELDS) + list(FINANCIAL_FIELDS):
            r.setdefault(f, None)

    gmp = {}
    try:
        gmp = _gmp_table()
    except Exception as e:                            # noqa: BLE001
        log.warning(f"gmp table unavailable: {e}")

    for r in rows:
        was = prev.get(r["symbol"]) or {}
        # Carry last good values forward FIRST, so a failure below leaves the
        # row no worse than it was.
        for f in list(CHITTORGARH_FIELDS) + list(FINANCIAL_FIELDS):
            if r.get(f) is None and was.get(f) is not None:
                r[f] = was[f]
        if r.get("enriched_source") is None:
            r["enriched_source"] = was.get("enriched_source")

        name = r.get("company") or r.get("symbol") or ""
        nkey = re.sub(r"[^a-z]", "", name.lower())
        for k, v in gmp.items():
            if k and (k in nkey or nkey.startswith(k[:14])):
                r["gmp_text"] = v
                break

        try:
            url = _find_page(name)
            if not url:
                log.warning(f"enrich {r['symbol']}: no Chittorgarh page found")
                continue
            import crawler
            got = crawler.extract(url, CG_PATTERNS, timeout=50)
            if not got.get("_ok"):
                log.warning(f"enrich {r['symbol']}: {got['_page'].error}")
                continue
            for f, raw in got.items():
                if f.startswith("_") or raw is None:
                    continue
                r[f] = _num(raw) if f in _NUMERIC else raw
            r["enriched_source"] = "chittorgarh.com"
            r["enriched_url"] = url
        except Exception as e:                        # noqa: BLE001
            log.warning(f"enrich {r['symbol']}: {type(e).__name__} {e} — keeping previous")

    # Min investment is derivable once lot size is known; deriving it from the
    # CAP is the honest direction — the most a retail applicant can be asked for.
    for r in rows:
        if r.get("min_investment") is None and r.get("lot_size") and r.get("price_high"):
            r["min_investment"] = round(r["lot_size"] * r["price_high"])
            r["min_investment_derived"] = True
    return rows


def build(listing_perf=None):
    """listing_perf: ipo_tracker rows, passed in by generate.py.

    Passed rather than read from docs/today.json, because that artefact carries
    a TRUNCATED preview of the listings — five rows out of thirty-two — and
    reading it silently measured five of eighty-five listings while reporting
    the number as though it were the real coverage.
    """
    s = _session()
    upcoming = _get(s, "api/all-upcoming-issues?category=ipo")
    current = _get(s, "api/ipo-current-issue")
    past = _get(s, "api/public-past-issues")
    # The IPO calendar is an INDIAN calendar and the runner is on UTC. At
    # 18:44 UTC on the 21st, date.today() is still the 21st while it is already
    # the 22nd in Mumbai — so GAJA, whose book closed on the 21st, was still
    # being classed "open" and, with NSE's live feed no longer carrying it,
    # rendered as "not open yet". A closed issue advertised as not yet started.
    #
    # IST, not MYT: these are NSE dates. The operator's timezone governs the
    # page's own clock; the exchange's governs the exchange's calendar.
    today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date()

    # Live subscription, keyed by symbol. 'Total' is the headline; the
    # category rows (QIB / NII / Retail) are kept for the detail line.
    subs, cats = {}, {}
    for r in current:
        sym = r.get("symbol")
        n = _num(r.get("noOfTime"))
        if not sym or n is None:
            continue
        if (r.get("category") or "").strip().lower() == "total":
            subs[sym] = n
        else:
            cats.setdefault(sym, {})[r.get("category")] = n

    rows = []
    for r in upcoming:
        if (r.get("series") or "").upper() not in MAINBOARD_SERIES:
            continue                                     # SME is a different asset class
        sym = r.get("symbol")
        start, end = _date(r.get("issueStartDate")), _date(r.get("issueEndDate"))
        lo, hi = _band(r.get("issuePrice"))
        shares = _num(r.get("issueSize"))
        amount_cr = round(shares * hi / 1e7, 1) if (shares and hi) else None

        phase = ("open" if start and end and start <= today <= end
                 else "upcoming" if start and today < start
                 else "closed")
        row = {
            "symbol": sym, "company": r.get("companyName"), "exchange": "NSE",
            "series": r.get("series"), "phase": phase, "status": r.get("status"),
            "open_date": start.isoformat() if start else None,
            "close_date": end.isoformat() if end else None,
            "days_left": (end - today).days if end and phase == "open" else None,
            "price_band": r.get("issuePrice"), "price_low": lo, "price_high": hi,
            "issue_size_shares": shares, "issue_size_cr": amount_cr,
            "subscription_x": subs.get(sym),
            "subscription_by_category": cats.get(sym) or None,
            # Stated, not guessed. Min investment needs lot size, and NSE's
            # public feed does not carry it; multiplying by an assumed lot is
            # exactly the fabricated number this file exists to avoid.
            "lot_size": None, "min_investment": None,
        }
        for f in list(CHITTORGARH_FIELDS) + list(FINANCIAL_FIELDS):
            row.setdefault(f, None)
        sc = score(row)
        v, why, caveat = verdict(row, sc)
        row.update(score=sc, verdict=v, verdict_why=why, verdict_caveat=caveat)
        rows.append(row)

    # Enrich only what someone can still act on.
    live = [r for r in rows if r["phase"] in ("open", "upcoming")]
    if live:
        previous = []
        if OUT.exists():
            try:
                pj = json.loads(OUT.read_text())
                previous = ((pj.get("open") or []) + (pj.get("upcoming") or [])
                            + (pj.get("awaiting_listing") or []))
            except Exception:                     # noqa: BLE001
                pass
        enrich(live, previous=previous)
        # Re-score: lot size and the fresh/OFS split change nothing in the score
        # (they are facts, not judgements), but min investment and GMP are now
        # available to the verdict's context lines.
        for r in live:
            sc = score(r)
            v, why, caveat = verdict(r, sc)
            r.update(score=sc, verdict=v, verdict_why=why, verdict_caveat=caveat)

    order = {"open": 0, "upcoming": 1, "closed": 2}
    rows.sort(key=lambda x: (order.get(x["phase"], 3), x["close_date"] or ""))

    # The past feed is newest-first and carries 14 years of history. Two
    # different things live in it and the old code conflated them by taking a
    # flat first-60 slice, which is why the section showed a short, arbitrary
    # and largely stale list.
    #
    #   awaiting listing — subscription CLOSED, listingDate still "-". These are
    #     live decisions: allotment, refunds and a listing date are all still
    #     ahead. They belong beside open and upcoming, not buried in history.
    #   recently listed — closed AND listed, inside twelve months, which is what
    #     was actually asked for.
    cutoff = today - timedelta(days=365)
    awaiting, listed = [], []
    for r in past:
        if (r.get("securityType") or "").upper() not in MAINBOARD_SERIES:
            continue
        closed_on = _date(r.get("ipoEndDate"))
        if not closed_on or closed_on < cutoff:
            continue                                  # feed is sorted, but do not rely on it
        listed_on = _date(r.get("listingDate"))
        row = {"symbol": r.get("symbol"), "company": r.get("company"),
               "price_band": r.get("priceRange"),
               "close_date": closed_on.isoformat(),
               "listing_date": listed_on.isoformat() if listed_on else None,
               "days_since_close": (today - closed_on).days}
        (listed if listed_on else awaiting).append(row)

    # A book that closed months ago with no listing date is not "awaiting
    # listing", it is a withdrawn or stalled issue — and presenting it as
    # pending would be the fabrication this file exists to avoid. Three weeks
    # is longer than any normal T+3 mainboard timetable.
    stalled = [r for r in awaiting if r["days_since_close"] > 21]
    awaiting = [r for r in awaiting if r["days_since_close"] <= 21]
    awaiting.sort(key=lambda x: x["close_date"], reverse=True)

    # Awaiting-listing rows are enriched too. What is outstanding for them is
    # precisely the listing date and where the grey market is pricing the
    # debut — the two fields NSE's closed feed does not carry — so leaving them
    # bare is leaving out the only things still undecided.
    for r in awaiting + stalled:
        for f in list(CHITTORGARH_FIELDS) + list(FINANCIAL_FIELDS):
            r.setdefault(f, None)
    if awaiting:
        for r in awaiting:
            r["company"] = r.get("company") or r["symbol"]
        enrich(awaiting, previous=(json.loads(OUT.read_text()).get("awaiting_listing")
                                   if OUT.exists() else []) or [])
    listed.sort(key=lambda x: x["listing_date"] or "", reverse=True)

    # Attach measured post-listing performance. ipo_tracker.py already computes
    # it for every recent listing it can reach — return since the first traded
    # close, high, low, distance from the high — measured from the FIRST CLOSE
    # rather than the issue price, because NSE's issue-price data is not
    # reliable and a listing gain computed off a guessed one is fabricated.
    #
    # NSE says 88 mainboard issues closed and listed inside the window;
    # ipo_tracker can measure a subset, because the rest sit outside the
    # 750-name screen universe or have no usable history. Both numbers are
    # published rather than quietly showing whichever is more flattering.
    perf = {}
    for row in (listing_perf or []):
        if row.get("sym"):
            perf[row["sym"]] = row
    if not perf:
        log.warning("no listing performance passed in — the listed table will "
                    "show dates only")
    for r in listed:
        m = perf.get(r["symbol"])
        if m:
            for k in ("first_close", "last_close", "since_listing_pct",
                      "from_high_pct", "high", "low", "months_listed", "sessions"):
                r.setdefault(k, None)
            r.update(measured=True,
                     first_close=m.get("first_close"), last_close=m.get("last_close"),
                     since_listing_pct=m.get("since_listing_pct"),
                     from_high_pct=m.get("from_high_pct"),
                     high=m.get("high"), low=m.get("low"),
                     months_listed=m.get("months_listed"), sessions=m.get("sessions"))
        else:
            # Set every performance key to None rather than leaving it absent.
            # A missing key is Undefined in Jinja, `x is not none` passes for it,
            # and the format filter then raises and takes the whole page down.
            # Third time this exact trap has fired; present-and-None is testable,
            # absent is a landmine that only goes off on the row where the data
            # happens to be missing.
            r["measured"] = False
            for k in ("first_close", "last_close", "since_listing_pct",
                      "from_high_pct", "high", "low", "months_listed", "sessions"):
                r.setdefault(k, None)
    # Measured rows first: a row with a return on it is worth more than one
    # with only a date, and burying them under unmeasured names by pure
    # recency hides the only part of this table that answers anything.
    listed.sort(key=lambda x: (not x["measured"], x["listing_date"] or ""), reverse=False)
    listed.sort(key=lambda x: (not x["measured"],))
    recent = listed

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "NSE public issue endpoints",
        "mainboard_only": True,
        "open": [r for r in rows if r["phase"] == "open"],
        "upcoming": [r for r in rows if r["phase"] == "upcoming"],
        "closed": [r for r in rows if r["phase"] == "closed"],
        "awaiting_listing": awaiting,
        "stalled": stalled,
        "recent_listed": recent,
        "recent_window_months": 12,
        "counts": {"open": sum(1 for r in rows if r["phase"] == "open"),
                   "upcoming": sum(1 for r in rows if r["phase"] == "upcoming"),
                   "awaiting": len(awaiting),
                   "listed_12m": len(recent),
                   "listed_measured": sum(1 for r in recent if r.get("measured")),
                   "apply": sum(1 for r in rows if r["verdict"].startswith("APPLY")),
                   "avoid": sum(1 for r in rows if r["verdict"] == "AVOID")},
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="write data/ipo_radar.json")
    args = ap.parse_args()

    d = build()
    print(f"IPO Radar — {d['counts']['open']} open, {d['counts']['upcoming']} upcoming "
          f"(mainboard only)\n")
    for r in d["open"] + d["upcoming"]:
        sc = r["score"]
        print(f"  {r['verdict']:14} {r['symbol']:12} {r['company'][:36]:38} "
              f"{r['price_band'] or '—':24} "
              f"{('%.2fx' % r['subscription_x']) if r['subscription_x'] is not None else '—':>9} "
              f"{sc['points']}/{sc['of']}")
        print(f"                 {r['verdict_why']}")
    if args.json:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(d, indent=1))
        print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
