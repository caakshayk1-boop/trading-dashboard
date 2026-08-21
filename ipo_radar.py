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
import re
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger(__name__)

NSE = "https://www.nseindia.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
REFERER = f"{NSE}/market-data/all-upcoming-issues-ipo"
OUT = Path(__file__).parent / "data" / "ipo_radar.json"

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
    # Never invent the denominator. A row scored on two of four dimensions is
    # reported as "/55 measured", not normalised to /100 as though it were whole.
    return {"points": got, "of": of,
            "pct": round(got / of * 100) if of else None,
            "parts": {k: f"{v}/{m}" for k, (v, m) in parts.items()},
            "not_measured": missing + ["lot size", "sector", "financials",
                                       "valuation multiples", "anchor book", "GMP"]}


def verdict(row, sc):
    """A verdict, plus the reason and the reason it might be wrong."""
    sub, d = row.get("subscription_x"), row.get("days_left")
    amount = row.get("issue_size_cr")

    if row["phase"] == "upcoming":
        return ("WATCH", "Book has not opened — no demand evidence exists yet.",
                "Re-check once subscription starts; nothing here is a judgement "
                "on the business.")
    if sub is None:
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
# challenge that plain HTTP cannot pass — Firecrawl can.
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


FIRECRAWL_SEARCH = "https://api.firecrawl.dev/v2/search"


def _fc_search(key, company):
    """One Firecrawl search + structured extract. REST, not the SDK.

    The SDK would be a new dependency for one HTTP call, and requests is
    already here. Chittorgarh sits behind a Cloudflare challenge, which is the
    whole reason this cannot be a plain GET.

    The URL id matters and the slug does not: /ipo/<slug>/<id>/ resolves on the
    id alone, so guessing a URL returns a DIFFERENT company's IPO with a 200 and
    no indication anything is wrong. Search, never construct.
    """
    import requests
    body = {
        "query": f"{company} IPO lot size GMP price band",
        "limit": 2,
        "includeDomains": ["chittorgarh.com"],
        "scrapeOptions": {
            "formats": [{
                "type": "json",
                "prompt": ("Extract these IPO facts for THIS company only. Return "
                           "null for any field not stated on the page. Never estimate."),
                "schema": {"type": "object", "properties": {
                    k: {"type": "string" if k in ("listing_date", "gmp_text")
                        else "number"} for k in CHITTORGARH_FIELDS}},
            }],
        },
    }
    r = requests.post(FIRECRAWL_SEARCH, json=body, timeout=90,
                      headers={"Authorization": f"Bearer {key}",
                               "Content-Type": "application/json"})
    r.raise_for_status()
    d = r.json()
    return ((d.get("data") or {}).get("web")) or []


def enrich(rows, key, previous=None):
    """Fill the fields NSE does not publish. Fails soft, never blanks."""
    prev = {r.get("symbol"): r for r in (previous or [])}
    for r in rows:
        was = prev.get(r["symbol"]) or {}
        # Carry the last good values forward FIRST, so a failed fetch below
        # leaves the row no worse than it was.
        for f in CHITTORGARH_FIELDS:
            if was.get(f) is not None:
                r.setdefault(f, was[f])
        r.setdefault("enriched_source", was.get("enriched_source"))
        if not key:
            continue
        try:
            for item in _fc_search(key, r["company"]):
                j = item.get("json") or {}
                got = False
                for f in CHITTORGARH_FIELDS:
                    v = j.get(f)
                    if v not in (None, "", 0):
                        r[f] = v
                        got = True
                if got:
                    r["enriched_source"] = "chittorgarh.com"
                    break
        except Exception as e:                   # noqa: BLE001
            log.warning(f"enrich {r['symbol']}: {e} — keeping previous values")

    # Min investment is derivable once lot size is known, and deriving it from
    # the CAP price is the honest direction: it is the most a retail applicant
    # can be asked for.
    for r in rows:
        if r.get("min_investment") is None and r.get("lot_size") and r.get("price_high"):
            r["min_investment"] = round(r["lot_size"] * r["price_high"])
            r["min_investment_derived"] = True
    return rows


def build():
    s = _session()
    upcoming = _get(s, "api/all-upcoming-issues?category=ipo")
    current = _get(s, "api/ipo-current-issue")
    past = _get(s, "api/public-past-issues")
    today = date.today()

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
        sc = score(row)
        v, why, caveat = verdict(row, sc)
        row.update(score=sc, verdict=v, verdict_why=why, verdict_caveat=caveat)
        rows.append(row)

    # Enrich only what someone can still act on.
    import os
    live = [r for r in rows if r["phase"] in ("open", "upcoming")]
    if live:
        previous = []
        if OUT.exists():
            try:
                pj = json.loads(OUT.read_text())
                previous = (pj.get("open") or []) + (pj.get("upcoming") or [])
            except Exception:                     # noqa: BLE001
                pass
        enrich(live, os.environ.get("FIRECRAWL_API_KEY"), previous)
        # Re-score: lot size and the fresh/OFS split change nothing in the score
        # (they are facts, not judgements), but min investment and GMP are now
        # available to the verdict's context lines.
        for r in live:
            sc = score(r)
            v, why, caveat = verdict(r, sc)
            r.update(score=sc, verdict=v, verdict_why=why, verdict_caveat=caveat)

    order = {"open": 0, "upcoming": 1, "closed": 2}
    rows.sort(key=lambda x: (order.get(x["phase"], 3), x["close_date"] or ""))

    recent = []
    for r in past[:60]:
        if (r.get("securityType") or "").upper() not in MAINBOARD_SERIES:
            continue
        recent.append({"symbol": r.get("symbol"), "company": r.get("company"),
                       "price_band": r.get("priceRange"),
                       "close_date": (_date(r.get("ipoEndDate")) or "") and
                                     _date(r.get("ipoEndDate")).isoformat(),
                       "listing_date": (_date(r.get("listingDate")).isoformat()
                                        if _date(r.get("listingDate")) else None)})

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "NSE public issue endpoints",
        "mainboard_only": True,
        "open": [r for r in rows if r["phase"] == "open"],
        "upcoming": [r for r in rows if r["phase"] == "upcoming"],
        "closed": [r for r in rows if r["phase"] == "closed"],
        "recent_closed": recent[:12],
        "counts": {"open": sum(1 for r in rows if r["phase"] == "open"),
                   "upcoming": sum(1 for r in rows if r["phase"] == "upcoming"),
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
