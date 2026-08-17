#!/usr/bin/env python3
"""
fund_portfolio.py — what each screened fund actually HOLDS.

The fund screen ranks on the NAV series, which says how a fund performed and
nothing at all about what it owns. Two flexi-caps with the same 3Y CAGR can be
a banks-and-IT portfolio and a smallcap-industrials portfolio; the NAV cannot
tell them apart, and that is the difference that decides whether adding one to
the SIP diversifies anything.

Source
------
ET Money's per-scheme portfolio page. Chosen after testing the alternatives:

- **api.mfapi.in** (already used for NAV) publishes NAV only, no holdings.
- **Kuvera's** public fund_schemes endpoint returns `[]` for these ISINs.
- **Advisorkhoj** URLs are derivable from the AMFI scheme name — which is why
  it was tried first — but its portfolio charts render client-side and the
  "sectors" block is actually the market-cap split (Large/Mid/Small), not
  sectors. Wrong data, confidently labelled.
- **ET Money** server-renders the real table: stock, sector and % of holding
  per row, plus the portfolio "as on" date. Verified with a plain requests
  GET — HTTP 200, no JS execution, no API key. That matters: FIRECRAWL_API_KEY
  is not set on this project, so a Firecrawl-dependent design would have
  shipped dark.

URL resolution
--------------
ET Money has no public search API, so scheme → URL comes from their own
sitemap (`mf-schemes-sitemap.xml`, ~1,585 direct-plan schemes) matched against
the AMFI scheme name by token overlap. The resolved map is cached in
`data/fund_portfolio_urls.json` so the sitemap is fetched once, not per fund.

Nothing here is estimated. A fund whose page cannot be resolved or parsed is
simply absent from the output — the card then renders without a composition
block rather than with a guessed one.
"""
from __future__ import annotations

import json
import logging
import pathlib
import re
import time
import urllib.parse
import urllib.request

log = logging.getLogger("fund_portfolio")

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

SITEMAP = "https://www.etmoney.com/mf-schemes-sitemap.xml"
URL_CACHE = pathlib.Path(__file__).parent / "data" / "fund_portfolio_urls.json"

TOP_SECTORS = 3          # what the card shows
TOP_STOCKS = 5

# Words that carry no discriminating power between scheme names. "fund",
# "direct", "growth" etc. appear in nearly all 1,585, so leaving them in makes
# every candidate look similar and the best match arbitrary.
_NOISE = {
    "fund", "direct", "plan", "growth", "option", "scheme", "mutual",
    "the", "of", "and", "reinvestment", "payout", "idcw", "dividend",
}


def _get(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _tokens(name: str) -> set[str]:
    """Comparable token set for a scheme name.

    AMFI writes "Motilal Oswal Flexi cap Fund Direct Plan-Growth Option";
    ET Money's slug is "motilal-oswal-flexi-cap-fund-direct-growth". Neither
    is a transform of the other, so matching is on tokens rather than string
    surgery. 'flexicap' and 'flexi cap' must agree, hence the de-spacing pass
    below is NOT applied — instead both sides keep their split tokens and the
    overlap score tolerates the difference.
    """
    words = re.split(r"[^a-z0-9]+", (name or "").lower())
    return {w for w in words if w and w not in _NOISE and not w.isdigit()}


def load_url_map(refresh: bool = False) -> dict[str, str]:
    """{slug: id} for every direct-plan scheme ET Money publishes.

    Cached on disk — the sitemap is ~335KB and changes slowly, and re-fetching
    it once per fund would be 24 downloads to answer one question.
    """
    if not refresh and URL_CACHE.exists():
        try:
            return json.loads(URL_CACHE.read_text(encoding="utf-8"))
        except Exception as e:                               # noqa: BLE001
            log.warning(f"url cache unreadable, refetching: {e}")

    xml = _get(SITEMAP, timeout=60)
    out: dict[str, str] = {}
    for loc in re.findall(r"<loc>(.*?)</loc>", xml):
        m = re.search(r"/mutual-funds/([^/]+)/(\d+)/?$", loc)
        if m:
            out[m.group(1)] = m.group(2)
    if out:
        URL_CACHE.parent.mkdir(parents=True, exist_ok=True)
        URL_CACHE.write_text(json.dumps(out, indent=0, sort_keys=True), encoding="utf-8")
    log.info(f"etmoney sitemap: {len(out)} direct-plan schemes")
    return out


def resolve(scheme_name: str, url_map: dict[str, str]) -> str | None:
    """Best-matching ET Money portfolio URL for an AMFI scheme name, or None.

    Scored on token overlap, and deliberately strict: a wrong match publishes
    ANOTHER fund's holdings under this fund's name, which is worse than showing
    nothing. Requires the AMFI tokens to be almost entirely present in the slug.
    """
    want = _tokens(scheme_name)
    if not want:
        return None
    best, best_score = None, 0.0
    for slug, sid in url_map.items():
        have = _tokens(slug.replace("-", " "))
        if not have:
            continue
        inter = len(want & have)
        # Jaccard-ish, but weighted towards covering the AMFI name: extra
        # tokens in the slug (e.g. "most-focused-multicap-35") are tolerated,
        # missing ones are not.
        score = inter / len(want)
        if score > best_score or (score == best_score and best and len(have) < len(_tokens(best.replace("-", " ")))):
            best, best_score = slug, score
    if not best or best_score < 0.8:
        log.debug(f"no confident ETMoney match for {scheme_name!r} (best={best_score:.2f})")
        return None
    return f"https://www.etmoney.com/mutual-funds/{best}/portfolio-details/{url_map[best]}"


_ROW_RE = re.compile(
    r'<tr>\s*<td class="company-name">\s*<a[^>]*>(?P<name>[^<]+)</a>\s*</td>\s*'
    r"<td>(?P<sector>[^<]*)</td>\s*<td>\s*(?P<pct>[\d.]+)\s*%\s*</td>",
    re.I)
_ASON_RE = re.compile(r"as on\s+([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})")


def parse(html: str) -> dict:
    """Holdings, sector weights and portfolio date from an ET Money page.

    Sector weights are SUMMED FROM THE HOLDINGS rather than read off the
    separate sector chart: the chart is drawn client-side, and a number that
    cannot be reconciled against the rows beside it is a number that will
    eventually disagree with them.
    """
    rows = []
    for m in _ROW_RE.finditer(html):
        try:
            pct = float(m.group("pct"))
        except ValueError:
            continue
        name = re.sub(r"\s+", " ", m.group("name")).strip()
        sector = re.sub(r"\s+", " ", m.group("sector")).strip() or None
        if name and pct > 0:
            rows.append({"name": name, "sector": sector, "pct": round(pct, 2)})
    if not rows:
        return {}

    by_sector: dict[str, float] = {}
    for r in rows:
        if r["sector"]:
            by_sector[r["sector"]] = by_sector.get(r["sector"], 0.0) + r["pct"]
    sectors = [{"name": k, "pct": round(v, 2)}
               for k, v in sorted(by_sector.items(), key=lambda kv: -kv[1])]

    ason = _ASON_RE.search(html)
    return {
        "as_on": ason.group(1) if ason else None,
        "holdings_count": len(rows),
        "equity_pct": round(sum(r["pct"] for r in rows), 2),
        "top_sectors": sectors[:TOP_SECTORS],
        "top_stocks": [{"name": r["name"], "pct": r["pct"]}
                       for r in sorted(rows, key=lambda r: -r["pct"])[:TOP_STOCKS]],
    }


def fetch_one(scheme_name: str, url_map: dict[str, str]) -> dict:
    url = resolve(scheme_name, url_map)
    if not url:
        return {}
    try:
        data = parse(_get(url))
    except Exception as e:                                   # noqa: BLE001
        log.warning(f"portfolio fetch {scheme_name[:40]}: {e}")
        return {}
    if data:
        data["source_url"] = url
    return data


def enrich(categories: list[dict], pause: float = 0.7) -> int:
    """Attach `portfolio` to every fund in a fund-screen payload, in place.

    Returns how many funds got one. Best-effort throughout: a fund that cannot
    be resolved or parsed is left without the key and its card simply renders
    no composition block.
    """
    try:
        url_map = load_url_map()
    except Exception as e:                                   # noqa: BLE001
        log.warning(f"etmoney sitemap unavailable: {e}")
        return 0
    if not url_map:
        return 0

    done = 0
    for cat in categories or []:
        for f in cat.get("funds") or []:
            p = fetch_one(f.get("name") or "", url_map)
            if p:
                f["portfolio"] = p
                done += 1
            time.sleep(pause)          # be a polite client
    return done


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    m = load_url_map()
    print(f"{len(m)} schemes in sitemap\n")
    for nm in ("Motilal Oswal Flexi cap Fund Direct Plan-Growth Option",
               "HDFC Flexi Cap Fund - Growth Option - Direct Plan",
               "Parag Parikh Flexi Cap Fund - Direct Plan - Growth"):
        d = fetch_one(nm, m)
        print(f"── {nm[:58]}")
        if not d:
            print("   no portfolio resolved\n")
            continue
        print(f"   as on {d['as_on']} · {d['holdings_count']} holdings "
              f"· {d['equity_pct']}% equity")
        print("   sectors:", ", ".join(f"{s['name']} {s['pct']}%" for s in d["top_sectors"]))
        print("   stocks: ", ", ".join(f"{s['name'][:24]} {s['pct']}%" for s in d["top_stocks"]))
        print()
