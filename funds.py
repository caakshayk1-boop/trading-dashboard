#!/usr/bin/env python3
"""
funds.py — a SIP screener over official AMFI NAV data.

What this is
------------
A ranking of public data, not a recommendation. It answers one narrow
question per category: which schemes actually returned the most over three
and five years, measured from the same NAV series AMFI publishes daily.

Source: api.mfapi.in, which mirrors AMFI's official NAV file. Returns are
computed here from the NAV series rather than taken from anybody's marketing
page, so they can be reproduced from the raw data.

On expense ratio
----------------
The user asked for lowest expense ratio. It is NOT in the free AMFI feed —
per-scheme TER is published by each AMC as a monthly PDF and there is no
reliable free API for it, so this screener does not claim to know it.

The one cost lever that IS visible in the data is Direct vs Regular, and it is
the big one: a Regular plan carries the distributor commission inside its TER,
typically 0.5-1.2% a year more than the same scheme's Direct plan. Same
portfolio, same manager, different cost. So this screens DIRECT + GROWTH only.
That is the low-cost half of the universe by construction, and it is stated
rather than implied.

Growth over IDCW for the same reason: IDCW payouts are taxed at slab rate and
break compounding, so an IDCW NAV series is not comparable to a growth one.

Refresh
-------
Weekly, cached like the stock picks. ~700 NAV downloads take a few minutes, so
this must never run inside a page request.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from datetime import date, datetime, timedelta

log = logging.getLogger("funds")

LIST_URL = "https://api.mfapi.in/mf"
FUND_URL = "https://api.mfapi.in/mf/{code}"

# Categories are AMFI's own `scheme_category`, matched exactly — NOT keywords
# against the scheme name. Name matching put "Bandhan Large & Mid Cap Fund"
# inside Mid Cap, because "MID CAP" is a substring of "LARGE & MID CAP".
# `keywords` is only a cheap pre-filter to decide which schemes are worth a
# NAV download; `amfi` is what actually decides the bucket.
CATEGORIES: list[tuple[str, str, tuple[str, ...], str, str]] = [
    ("flexi",    "Flexi Cap",          ("FLEXI CAP",),
     "Equity Scheme - Flexi Cap Fund",
     "Manager picks across large, mid and small. One fund, whole market."),
    ("largecap", "Large Cap",          ("LARGE CAP",),
     "Equity Scheme - Large Cap Fund",
     "Top 100 by market cap. The steadiest equity shelf, and the hardest to beat."),
    ("largemid", "Large & Mid Cap",    ("LARGE & MID", "LARGE AND MID", "LARGEMID", "LARGE & MIDCAP"),
     "Equity Scheme - Large & Mid Cap Fund",
     "Mandated to hold both. Less concentrated than mid cap, more than large."),
    ("midcap",   "Mid Cap",            ("MID CAP", "MIDCAP"),
     "Equity Scheme - Mid Cap Fund",
     "101st to 250th by market cap. More growth, more drawdown — size accordingly."),
    ("smallcap", "Small Cap",          ("SMALL CAP", "SMALLCAP"),
     "Equity Scheme - Small Cap Fund",
     "251st onward. Highest dispersion; a 3-year number hides brutal years."),
    ("elss",     "ELSS (tax saving)",  ("ELSS", "TAX SAVER", "TAX SAVING"),
     "Equity Scheme - ELSS",
     "80C deduction with a 3-year lock-in — the shortest lock-in of any 80C option."),
    # Nifty 50 trackers ONLY. An unrestricted "Index" bucket put a Nifty Next
    # 50 fund (18.91%) above a Nifty 50 fund (8.99%) and made it look like the
    # better fund. It is not — it tracks a different index. Comparing index
    # funds is only meaningful against the SAME benchmark, where the spread is
    # cost and tracking error, not skill.
    ("index",    "Index — Nifty 50",   ("NIFTY 50", "NIFTY50"),
     "Other Scheme - Index Funds",
     "No manager, so no manager risk. All of these track the same index — the "
     "spread between them is cost and tracking error, nothing else."),
    ("hybrid",   "Balanced Advantage", ("BALANCED ADVANTAGE", "DYNAMIC ASSET"),
     "Hybrid Scheme - Dynamic Asset Allocation or Balanced Advantage",
     "Equity/debt mix moved by valuation. Gentler ride, lower ceiling."),
]

EXCLUDE = ("IDCW", "DIVIDEND", "REGULAR", "BONUS", "PAYOUT", "REINVEST")
MAX_PER_CATEGORY = 60          # candidates fetched per category
TOP_N = 3                      # published per category
MIN_YEARS = 3

# A scheme whose NAV stopped updating is dead — merged, wound up, or renamed.
# AMFI keeps the historical series, and _cagr() measures backwards from the LAST
# available NAV, so a dead fund reports the three years before it died. "IDBI
# NIFTY 50 Index Fund" last priced on 2023-07-27 and ranked top of the Index
# table at 21.69% — its window was 2020-2023, the post-COVID run — against live
# funds returning ~9% over the actual last three years. An index fund beating
# its own index by 12 points is impossible on its face; this is why.
MAX_NAV_AGE_DAYS = 14


def _get(url: str, timeout: int = 25):
    req = urllib.request.Request(url, headers={"User-Agent": "askakshay-funds/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _parse_nav(rows: list[dict]) -> list[tuple[date, float]]:
    """API returns newest-first, dd-mm-yyyy. Normalise to oldest-first floats."""
    out = []
    for r in rows:
        try:
            out.append((datetime.strptime(r["date"], "%d-%m-%Y").date(), float(r["nav"])))
        except (KeyError, ValueError):
            continue
    out.sort(key=lambda x: x[0])
    return out


def _nav_on_or_before(series: list[tuple[date, float]], target: date):
    """Last NAV at or before `target`. NAVs are published only on business
    days, so an exact date match would silently drop most funds."""
    best = None
    for d, v in series:
        if d <= target:
            best = v
        else:
            break
    return best


def _cagr(series: list[tuple[date, float]], years: int):
    if not series:
        return None
    end_d, end_v = series[-1]
    start = _nav_on_or_before(series, end_d - timedelta(days=365 * years))
    if not start or start <= 0 or end_v <= 0:
        return None
    # Tolerate a short history rather than reporting a fake number for it.
    if (end_d - series[0][0]).days < 365 * years - 30:
        return None
    return round(((end_v / start) ** (1 / years) - 1) * 100, 2)


def _max_drawdown(series: list[tuple[date, float]], years: int = 3):
    """Worst peak-to-trough over the window. A 3-year CAGR with no drawdown
    beside it tells you the return and hides the ride."""
    if not series:
        return None
    cutoff = series[-1][0] - timedelta(days=365 * years)
    vals = [v for d, v in series if d >= cutoff]
    if len(vals) < 30:
        return None
    peak, worst = vals[0], 0.0
    for v in vals:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, (v - peak) / peak * 100)
    return round(worst, 1)


def build(limit_per_cat: int = MAX_PER_CATEGORY) -> dict:
    """Screen every category. Returns the payload the site and bot render."""
    try:
        allf = _get(LIST_URL, timeout=90)
    except Exception as e:
        log.warning(f"fund list unavailable: {e}")
        return {"ok": False, "error": str(e), "categories": []}

    direct = [f for f in allf
              if "DIRECT" in f["schemeName"].upper()
              and "GROWTH" in f["schemeName"].upper()
              and not any(x in f["schemeName"].upper() for x in EXCLUDE)]

    out = []
    for key, label, keywords, amfi_cat, blurb in CATEGORIES:
        cands = [f for f in direct
                 if any(k in f["schemeName"].upper() for k in keywords)][:limit_per_cat]
        scored, seen = [], set()
        for f in cands:
            try:
                d = _get(FUND_URL.format(code=f["schemeCode"]))
            except Exception:
                continue
            meta = d.get("meta", {})

            # AMFI's own category is the arbiter. The name pre-filter is loose
            # on purpose so nothing is missed; this is what keeps buckets clean.
            if (meta.get("scheme_category") or "").strip() != amfi_cat:
                continue

            # AMFI lists one scheme under several codes (growth/IDCW ISIN
            # splits), so an unguarded run printed HDFC Flexi Cap twice in a
            # top-three of three. Dedupe on the ISIN, falling back to the name.
            ident = (meta.get("isin_growth") or "").strip().upper() \
                or " ".join((meta.get("scheme_name") or "").upper().split())
            if ident in seen:
                continue
            seen.add(ident)

            series = _parse_nav(d.get("data", []))
            if not series:
                continue

            # Every fund must be measured over the SAME three years. A stale
            # series silently shifts the window (see MAX_NAV_AGE_DAYS).
            age = (date.today() - series[-1][0]).days
            if age > MAX_NAV_AGE_DAYS:
                log.debug(f"skip {meta.get('scheme_name','?')[:40]}: NAV {age}d stale")
                continue

            r3 = _cagr(series, 3)
            if r3 is None:
                continue                      # under three years — not comparable
            scored.append({
                "code": f["schemeCode"],
                "name": meta.get("scheme_name") or f["schemeName"],
                "house": meta.get("fund_house", ""),
                "category": meta.get("scheme_category", label),
                "nav": round(series[-1][1], 4),
                "nav_date": series[-1][0].isoformat(),
                "r3": r3,
                "r5": _cagr(series, 5),
                "dd3": _max_drawdown(series, 3),
                # Deep link to the scheme's own AMFI-coded page. mfapi is the
                # data source, so it is also the honest place to send someone
                # to check the series this ranking was computed from.
                "url": f"https://api.mfapi.in/mf/{f['schemeCode']}",
            })
        scored.sort(key=lambda x: x["r3"], reverse=True)
        out.append({
            "key": key, "label": label, "blurb": blurb,
            "screened": len(scored), "funds": scored[:TOP_N],
        })
        log.info(f"{label}: {len(scored)} screened, top r3={scored[0]['r3'] if scored else '—'}")

    return {
        "ok": True,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "source": "AMFI daily NAV via api.mfapi.in",
        "basis": ("Direct + Growth plans only. Returns are CAGR computed from the "
                  "published NAV series, not taken from any factsheet."),
        "ter_note": ("Per-scheme expense ratio is not in the free AMFI feed. Direct "
                     "plans are screened because they exclude distributor commission "
                     "— typically 0.5-1.2%/yr less than the same scheme's Regular plan."),
        "categories": out,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    data = build(limit_per_cat=n)
    for c in data.get("categories", []):
        print(f"\n{c['label']}  ({c['screened']} screened)")
        for f in c["funds"]:
            print(f"   {f['r3']:>6.2f}% 3y   {str(f['r5']):>6}% 5y   dd {f['dd3']}%   {f['name'][:58]}")
