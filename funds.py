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
import re
import time
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
    # Plain Nifty 50 trackers ONLY, and this needs a regex rather than a
    # substring. "NIFTY 50" matches "NIFTY 500", so a UTI Nifty 500 Value 50
    # fund led this table at 26.41% — against a Nifty 50 that returned ~9% over
    # the same three years. Equal Weight and Next 50 are different indices too.
    # Comparing index funds is only meaningful against the SAME benchmark,
    # where the spread is cost and tracking error rather than skill.
    ("index",    "Index — Nifty 50",   ("NIFTY 50", "NIFTY50"),
     "Other Scheme - Index Funds",
     "No manager, so no manager risk. All of these track the same index — the "
     "spread between them is cost and tracking error, nothing else."),
    ("hybrid",   "Balanced Advantage", ("BALANCED ADVANTAGE", "DYNAMIC ASSET"),
     "Hybrid Scheme - Dynamic Asset Allocation or Balanced Advantage",
     "Equity/debt mix moved by valuation. Gentler ride, lower ceiling."),

    # ── SECTORAL, THEMATIC AND THE REST ────────────────────────────────────
    #
    # AMFI files every one of these under a single scheme_category,
    # "Equity Scheme - Sectoral/ Thematic", so the category field cannot tell a
    # pharma fund from an infrastructure one — only the NAME can. That is the
    # same trap the Nifty 50 bucket has, and it is why each of these carries
    # its own keywords rather than leaning on the AMFI label.
    #
    # THEY ARE SEPARATE CATEGORIES ON PURPOSE. Ranking a gold fund against a
    # small-cap fund produces a leaderboard in which the top entry is whichever
    # asset class had a good year — which is a fact about the year, not about
    # the fund or the manager. Each of these is ranked only against its own
    # kind, and the page says which kind that is.
    ("multicap", "Multi Cap",          ("MULTI CAP", "MULTICAP"),
     "Equity Scheme - Multi Cap Fund",
     "Mandated 25% each in large, mid and small. Flexi cap with the split fixed."),
    ("focused",  "Focused",            ("FOCUSED",),
     "Equity Scheme - Focused Fund",
     "At most 30 holdings. Concentration cuts both ways and shows up in drawdown."),
    ("value",    "Value & Contra",     ("VALUE FUND", "CONTRA", "VALUE DISCOVERY", "VALUE OPP"),
     "Equity Scheme - Value Fund",
     "Buys what is cheap and unloved. Long stretches of underperformance are the cost."),
    ("divyield", "Dividend Yield",     ("DIVIDEND YIELD",),
     "Equity Scheme - Dividend Yield Fund",
     "Screens on payout. Growth plan, so the yield compounds inside the NAV."),
    ("infra",    "Infrastructure",     ("INFRASTRUCTURE", "INFRA FUND", "MANUFACTURING",
                                        "TRANSPORT", "T.I.G.E.R"),
     "Equity Scheme - Sectoral/ Thematic",
     "Capex, power, roads, defence. A policy-cycle bet more than a company one."),
    ("pharma",   "Pharma & Healthcare", ("PHARMA", "HEALTHCARE", "HEALTH CARE"),
     "Equity Scheme - Sectoral/ Thematic",
     "Regulated demand, USFDA risk. Moves on things the wider market ignores."),
    ("tech",     "Technology",         ("TECHNOLOGY", "DIGITAL", " IT FUND", "INFO TECH"),
     "Equity Scheme - Sectoral/ Thematic",
     "Indian IT earns in dollars — a currency position as much as a sector one."),
    ("banking",  "Banking & Financial", ("BANKING", "FINANCIAL SERVICES", "FINANCIAL SERV"),
     "Equity Scheme - Sectoral/ Thematic",
     "The largest weight in the index. A view here is close to a view on the market."),
    ("consume",  "Consumption",        ("CONSUMPTION", "CONSUMER", "FMCG"),
     "Equity Scheme - Sectoral/ Thematic",
     "Domestic demand, slower and less cyclical than the rest of this shelf."),
    ("multiasset", "Multi Asset",      ("MULTI ASSET", "MULTI-ASSET"),
     "Hybrid Scheme - Multi Asset Allocation",
     "Equity, debt and gold in one mandate — at least 10% in each, by rule."),
    ("gold",     "Gold",               ("GOLD FUND", "GOLD SAVINGS", "GOLD ETF FOF",
                                        "GOLD FOF"),
     "Other Scheme - FoF Domestic",
     "Not a business — no earnings, no compounding. It holds gold and moves with it."),
    ("global",   "Global & overseas",  ("GLOBAL", "US EQUITY", "NASDAQ", "S&P 500",
                                        "INTERNATIONAL", "WORLD", "OVERSEAS", "GREATER CHINA",
                                        "EMERGING MARKET"),
     "Other Scheme - FoF Overseas",
     "Rupee returns on foreign assets, so the currency is part of the result. "
     "Several are capped for new money under the RBI overseas limit."),
]

EXCLUDE = ("IDCW", "DIVIDEND", "REGULAR", "BONUS", "PAYOUT", "REINVEST")


def _is_payout_plan(name_upper: str) -> bool:
    """Is this the IDCW/payout variant rather than the growth plan?

    "DIVIDEND" is in EXCLUDE to drop payout plans, and it silently deleted an
    entire category: every Dividend Yield fund contains the word, so the
    dividend-yield screen matched ZERO of 5,057 direct-growth schemes and
    published as an empty table.

    Dividend Yield is a STRATEGY — it screens on payout and holds the units —
    and has nothing to do with how the plan distributes. Masked before the
    plan-type check so the two meanings stop colliding.
    """
    probe = name_upper.replace("DIVIDEND YIELD", "DIVYIELD-STRATEGY")
    return any(x in probe for x in EXCLUDE)

# Extra rejects for the Nifty 50 bucket. AMFI files every index tracker under
# one scheme_category ("Other Scheme - Index Funds"), so the category field
# cannot separate a Nifty 50 fund from a Nifty 500 Value 50 one — only the name
# can, and the name needs care:
#
#   · "NIFTY 50" is a prefix of "NIFTY 500", which is how a UTI Nifty 500
#     Value 50 fund led the table at 26.41% against a Nifty 50 that did ~9%.
#   · Equal Weight, Next 50, Value 20 and the rest track different indices.
#     Ranking them together implies a skill difference that cannot exist
#     between funds tracking the same benchmark.
_NIFTY50_RE = re.compile(r"NIFTY\s*-?\s*50(?!\d)")
_NIFTY50_REJECT = (
    "500", "NEXT", "EQUAL", "VALUE", "MIDCAP", "MID CAP", "SMALLCAP",
    "SMALL CAP", "ARBITRAGE", "TOP", "QUALITY", "ALPHA", "LOW VOL",
    "MOMENTUM", "ESG", "SHARIAH",
)
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


def _get(url: str, timeout: int = 25, tries: int = 3):
    """Fetch with a short backoff on the failures that are worth retrying.

    THE WEEK OF 29 AUGUST WAS LOST TO ONE 502. The scheme list came back
    "HTTP Error 502: Bad Gateway", build() returned ok:False, the job exited 1,
    and the cached screen stayed at the previous week's — which is how the site
    was still publishing a 22 August screen on 3 September. Nobody noticed
    because a stale screen looks exactly like a fresh one apart from a date
    nobody reads.

    mfapi.in mirrors AMFI and is a free service; an occasional 502 or timeout
    is its normal behaviour, not an outage. Retrying three times over about six
    seconds costs nothing and turns a lost week into a slow minute.

    5xx and timeouts are retried. A 404 is not — that is a scheme code that
    does not exist, and asking again will not change it.
    """
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "askakshay-funds/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = e
            if e.code < 500:
                raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
        if attempt < tries - 1:
            time.sleep(1.5 * (attempt + 1))
    raise last


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


def _sip_value(series: list[tuple[date, float]], monthly: float = 10_000.0,
               years: int = 10):
    """What a fixed monthly SIP would be worth today, from the NAV series.

    Every fund table on the internet quotes this number and none of them shows
    its working. It is not a return: it depends on WHEN each instalment bought,
    so a fund that fell early and recovered late can beat a steadier one with
    the same CAGR — which is the whole reason a SIP is not a lump sum.

    Computed the only honest way: walk the actual NAV on the nearest trading
    day to each monthly anniversary, buy units at that NAV, and value the total
    at the latest NAV. No assumed rate, no smoothing.

    Returns None rather than a shortened number when the fund is younger than
    the window. A 10-year SIP figure on a 4-year-old fund is the most flattering
    kind of wrong: it silently becomes a 4-year figure and gets compared against
    real 10-year ones.
    """
    if not series:
        return None
    end_d, end_nav = series[-1]
    if end_nav <= 0:
        return None
    first_d = series[0][0]
    start_d = end_d - timedelta(days=365 * years)
    # The fund must actually have the history. 30 days of slack for a NAV file
    # that starts mid-month; anything more and the window is not what it says.
    if (end_d - first_d).days < 365 * years - 30:
        return None
    units = 0.0
    paid = 0.0
    for m in range(years * 12):
        buy_on = start_d + timedelta(days=int(30.44 * m))
        if buy_on > end_d:
            break
        nav = _nav_on_or_before(series, buy_on)
        if not nav or nav <= 0:
            continue
        units += monthly / nav
        paid += monthly
    if paid <= 0 or units <= 0:
        return None
    return {"invested": round(paid), "value": round(units * end_nav),
            "months": int(paid // monthly)}


def _volatility(series: list[tuple[date, float]], years: int = 3):
    """Annualized stdev of monthly returns over the window — the ride behind
    the CAGR number. Two funds can post the same 3Y return while compounding
    completely differently; drawdown already shows one side of that (the
    worst single stretch), this shows the other (how bumpy the whole ride
    was). One NAV per calendar month (last observation in that month), not
    daily — daily NAV noise from the fund's own valuation cycle would inflate
    this past what an investor actually experiences holding month to month.
    """
    if not series:
        return None
    cutoff = series[-1][0] - timedelta(days=365 * years)
    windowed = [(d, v) for d, v in series if d >= cutoff]
    if len(windowed) < 60:
        return None
    monthly: dict[tuple[int, int], float] = {}
    for d, v in windowed:
        monthly[(d.year, d.month)] = v  # later date in the same month wins
    vals = [monthly[k] for k in sorted(monthly.keys())]
    if len(vals) < 12:
        return None
    rets = [(vals[i] / vals[i - 1] - 1) for i in range(1, len(vals)) if vals[i - 1] > 0]
    if len(rets) < 11:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return round((var ** 0.5) * (12 ** 0.5) * 100, 1)


def _bar_pct(cagr, cap: float = 30.0):
    """Fill width for a CAGR bar, 0-100. 30% is the cap because it is a rare,
    exceptional annual equity return — a fund at or above it shows a full
    bar rather than the scale being stretched to accommodate one outlier and
    making every ordinary 12-15% return look empty."""
    if cagr is None:
        return 0
    return round(min(100.0, max(0.0, cagr / cap * 100)))


def _percentile(value, population: list[float]):
    """Where `value` sits among `population` — 100 means nothing in the
    category's own screened set beat it, 0 means everything did. Turns an
    isolated "14.2%" into "top of 23 screened," which is the actual context
    a return number needs."""
    if value is None or not population:
        return None
    below = sum(1 for x in population if x < value)
    return round(below / len(population) * 100)


def _category_facts(scored: list[dict]):
    """Real, derived facts from the category's FULL screened population, not
    just the top 3 published — a fund with the best 5Y return is not always
    the top 3Y performer, and that gap is itself worth surfacing rather than
    hidden by only ever showing the current-ranking-metric leaders."""
    with_r5 = [f for f in scored if f.get("r5") is not None]
    if not with_r5:
        return None
    best = max(with_r5, key=lambda f: f["r5"])
    worst = min(with_r5, key=lambda f: f["r5"])

    steadiest = None
    with_vol = [f for f in scored if f.get("volatility") is not None and f.get("r3") is not None]
    if with_vol:
        r3_desc = sorted((f["r3"] for f in with_vol), reverse=True)
        cut = r3_desc[max(0, len(r3_desc) // 4 - 1)]
        top_quartile = [f for f in with_vol if f["r3"] >= cut]
        if top_quartile:
            steadiest = min(top_quartile, key=lambda f: f["volatility"])

    return {
        "best_5y":  {"name": best["name"],  "r5": best["r5"]},
        "worst_5y": {"name": worst["name"], "r5": worst["r5"]},
        "dispersion_5y": round(best["r5"] - worst["r5"], 2),
        "steadiest_top_quartile": (
            {"name": steadiest["name"], "volatility": steadiest["volatility"], "r3": steadiest["r3"]}
            if steadiest else None
        ),
    }


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


def _calendar_years(series: list[tuple[date, float]], n: int = 5):
    """Return per completed calendar year, newest first.

    A single point-to-point CAGR hides the path completely: a fund that made
    +55% in one year and lost money in the other two can post the same 3Y
    number as one that ground out +14% three times. Only whole years the
    series actually covers are reported — a fund that launched in June does
    not get a partial first year dressed up as an annual return.
    """
    if not series:
        return []
    by_year: dict[int, list[tuple[date, float]]] = {}
    for d, v in series:
        by_year.setdefault(d.year, []).append((d, v))
    out = []
    last_year = series[-1][0].year
    for y in sorted(by_year, reverse=True):
        if y >= last_year:
            continue                      # current year is incomplete
        prev = by_year.get(y - 1)
        if not prev:
            continue                      # no opening mark — cannot measure
        start, end = prev[-1][1], by_year[y][-1][1]
        if start <= 0:
            continue
        out.append({"year": y, "ret": round((end / start - 1) * 100, 1)})
        if len(out) >= n:
            break
    return out


def _rolling_3y(series: list[tuple[date, float]]):
    """Best / worst / median 3-year CAGR across every month-end start point.

    This is the honest version of "3Y return". The headline 3Y CAGR is one
    arbitrary window ending today, and it moves a lot with where that window
    happens to start. The spread across all available start dates is what
    tells you whether a fund is consistently good or was rescued by one entry
    point. Needs at least 6 distinct windows to be worth reporting.
    """
    if not series or (series[-1][0] - series[0][0]).days < 365 * 4:
        return None
    month_ends: dict[tuple[int, int], tuple[date, float]] = {}
    for d, v in series:
        month_ends[(d.year, d.month)] = (d, v)
    marks = [month_ends[k] for k in sorted(month_ends)]
    rets = []
    for i, (d0, v0) in enumerate(marks):
        if v0 <= 0:
            continue
        target = d0 + timedelta(days=365 * 3)
        if target > series[-1][0]:
            break
        v1 = _nav_on_or_before(series, target)
        if not v1 or v1 <= 0:
            continue
        rets.append(((v1 / v0) ** (1 / 3) - 1) * 100)
    if len(rets) < 6:
        return None
    rets.sort()
    mid = len(rets) // 2
    median = rets[mid] if len(rets) % 2 else (rets[mid - 1] + rets[mid]) / 2
    return {
        "best": round(rets[-1], 1),
        "worst": round(rets[0], 1),
        "median": round(median, 1),
        "windows": len(rets),
        # The number that actually matters to someone choosing a fund: how
        # often a 3-year hold beat a fixed deposit rather than how often it
        # merely avoided a loss.
        "above_7pct": round(sum(1 for r in rets if r >= 7.0) / len(rets) * 100),
    }


def _spark(series: list[tuple[date, float]], points: int = 48):
    """Month-end NAV, downsampled, normalised to 100 at the start — enough to
    draw the shape of the ride without shipping 3,000 daily points per fund
    into the page."""
    if len(series) < 24:
        return []
    month_ends: dict[tuple[int, int], float] = {}
    for d, v in series:
        month_ends[(d.year, d.month)] = v
    vals = [month_ends[k] for k in sorted(month_ends)][-points:]
    if not vals or vals[0] <= 0:
        return []
    return [round(v / vals[0] * 100, 1) for v in vals]


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
              and not _is_payout_plan(f["schemeName"].upper())]

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

            # Index funds all share one AMFI category, so the benchmark has to
            # be pinned by name. See _NIFTY50_REJECT for why this is not a
            # simple substring test.
            # "MID CAP" IS A SUBSTRING OF "LARGE AND MID CAP".
            #
            # The mid-cap bucket matched 114 schemes against large-and-mid's
            # 44, because every Large & Mid Cap fund contains the words "Mid
            # Cap". So the Mid Cap table was ranking a different category's
            # funds alongside its own — two mandates, one leaderboard, which is
            # exactly what the category split exists to prevent.
            if key == "midcap":
                nm = (meta.get("scheme_name") or "").upper()
                if any(x in nm for x in ("LARGE & MID", "LARGE AND MID",
                                         "LARGEMID", "LARGE&MID")):
                    continue

            if key == "index":
                nm = (meta.get("scheme_name") or "").upper()
                if not _NIFTY50_RE.search(nm):
                    continue
                if any(x in nm for x in _NIFTY50_REJECT):
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
                "r1": (r1 := _cagr(series, 1)),
                "r3": r3,
                "r5": (r5 := _cagr(series, 5)),
                "dd3": _max_drawdown(series, 3),
                "volatility": _volatility(series, 3),
                # The number every fund table quotes and none shows the working
                # for. None when the fund is younger than the window, rather
                # than a shortened figure passed off as ten years.
                "sip10": _sip_value(series, 10_000.0, 10),
                # Age, from the first NAV the fund ever published. It is the
                # one "fund fact" the NAV feed can actually answer, and it
                # qualifies every return beside it: a 3-year number on a
                # 3-year-old fund has never seen a bad year.
                "inception": series[0][0].isoformat(),
                "age_years": round((series[-1][0] - series[0][0]).days / 365.25, 1),
                "bar_r1": _bar_pct(r1),
                "bar_r3": _bar_pct(r3),
                "bar_r5": _bar_pct(r5),
                # ── advanced detail, all derived from the SAME series already
                # fetched above: no extra request, no second source to
                # disagree with, and nothing here that isn't in the NAV
                # history this ranking was computed from.
                "isin": (meta.get("isin_growth") or "").strip().upper() or None,
                "scheme_type": meta.get("scheme_type") or None,
                "inception": series[0][0].isoformat(),
                "history_years": round((series[-1][0] - series[0][0]).days / 365.25, 1),
                "since_inception": _cagr(
                    series, max(1, int((series[-1][0] - series[0][0]).days // 365))),
                "calendar": _calendar_years(series),
                "rolling3y": _rolling_3y(series),
                "spark": _spark(series),
                # The data source, so also the honest place to send someone to
                # check the series behind the ranking. It is raw JSON, not a
                # fund page — labelled as such at the call site rather than
                # dressed up as somewhere to go and read about the fund.
                "url": f"https://api.mfapi.in/mf/{f['schemeCode']}",
            })
        scored.sort(key=lambda x: x["r3"], reverse=True)

        # Percentile against the category's OWN full screened population —
        # computed before truncating to the top 3, and category_facts too,
        # so a fund that's #1 on 5Y but not top-3 on 3Y still surfaces.
        r3_population = [f["r3"] for f in scored]
        for f in scored:
            f["percentile_r3"] = _percentile(f["r3"], r3_population)
        facts = _category_facts(scored)

        out.append({
            "key": key, "label": label, "blurb": blurb,
            "screened": len(scored), "funds": scored[:TOP_N],
            "facts": facts,
        })
        log.info(f"{label}: {len(scored)} screened, top r3={scored[0]['r3'] if scored else '—'}")

    # What each published fund actually HOLDS. Separate module and separate
    # source (ET Money's server-rendered portfolio table) because AMFI's feed
    # is NAV-only — it can rank funds but cannot say what is inside them, and
    # two funds with the same 3Y CAGR can own completely different markets.
    #
    # Best-effort by design: this runs AFTER the screen is fully built, so a
    # slow or unreachable portfolio source costs the section its composition
    # block and nothing else. The ranking above never depends on it.
    try:
        import fund_portfolio
        n = fund_portfolio.enrich(out)
        log.info(f"portfolio composition attached to {n} fund(s)")
    except Exception as e:                                   # noqa: BLE001
        log.warning(f"portfolio enrichment skipped: {e}")

    return {
        "ok": True,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "source": "AMFI daily NAV via api.mfapi.in",
        "portfolio_source": ("Holdings, sector weights and the portfolio date come from "
                             "ET Money's published scheme portfolio. Sector weights are "
                             "summed from the individual holdings, not read off a chart."),
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
