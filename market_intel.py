#!/usr/bin/env python3
"""
market_intel.py — corporate actions, FII/DII flow, and sector heat, all from
official/free sources, no third-party data vendor.

Sources
-------
Corporate actions and FII/DII: NSE India's own public JSON API
(nseindia.com/api/...) — the same endpoints trade.askakshay.com's
TradeFlow Pro (a sibling project, ~/Workspace/AI/Trading/tradeflow-pro/)
already uses in production. Verified directly against live data before
writing this: corporate actions returns real rows with symbol/subject/
ex-date; FII/DII returns exactly 2 rows (today's provisional FII and DII
net figures) — NOT a rolling window, unlike what TradeFlow Pro's own
grouping code assumes. That means a real "7-day activity" view has to be
built by CACHING one day at a time and reading back the last 7 cached
rows — see get_market_intel() in newspaper.py, which does that. This
module only ever fetches "today."

Sector heat: Yahoo Finance sector indices, same 16-sector list TradeFlow
Pro curated (^CNXIT, ^NSEBANK, etc.) plus each sector's largest names —
the exact same data source (yfinance) already used everywhere else in
this codebase.

Refresh
-------
Daily, before the 6 AM edition — see market_intel.yml. Kept as its own
workflow for the same reason fund_screen.yml is separate from
daily_scan.yml: a hung or rate-limited third-party call here must never
take the daily paper down with it.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from datetime import date, datetime, timedelta

log = logging.getLogger("market_intel")

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nseindia.com",
}

NSE_BASE = "https://www.nseindia.com/api"


def _nse_get(path: str, timeout: int = 15):
    """NSE occasionally wants a session cookie first; occasionally doesn't
    (both endpoints here answered directly in testing). Try direct first —
    cheaper — and only pay for the cookie round-trip if that 403s."""
    req = urllib.request.Request(f"{NSE_BASE}/{path}", headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code != 403:
            raise
    # Cookie round-trip fallback.
    home = urllib.request.Request("https://www.nseindia.com", headers={
        "User-Agent": _HEADERS["User-Agent"], "Accept": "text/html"})
    with urllib.request.urlopen(home, timeout=timeout) as r:
        cookies = "; ".join(c.split(";")[0] for c in r.headers.get_all("Set-Cookie") or [])
    req2 = urllib.request.Request(f"{NSE_BASE}/{path}",
                                   headers={**_HEADERS, "Cookie": cookies})
    with urllib.request.urlopen(req2, timeout=timeout) as r:
        return json.load(r)


def fetch_corporate_actions(limit: int = 50) -> list[dict]:
    """Every corporate action NSE currently publishes — splits, bonuses,
    dividends, buybacks, everything — unranked. No "top by importance"
    logic: that would need a judgment call about which actions matter that
    this data doesn't support making honestly."""
    try:
        raw = _nse_get("corporates-corporateActions?index=equities")
    except Exception as e:
        log.warning(f"corporate actions unavailable: {e}")
        return []
    out = []
    for r in raw[:limit] if isinstance(raw, list) else []:
        out.append({
            "symbol": r.get("symbol", ""),
            "company": r.get("comp", ""),
            "subject": r.get("subject", ""),
            "ex_date": r.get("exDate", ""),
            "record_date": r.get("recDate", ""),
            "face_value": r.get("faceVal", ""),
        })
    # NSE's own order isn't guaranteed chronological — sort by ex-date so
    # the freshest actions lead, same "most recent first" convention the
    # rest of this site uses everywhere else.
    def _key(row):
        try:
            return datetime.strptime(row["ex_date"], "%d-%b-%Y")
        except (ValueError, KeyError):
            return datetime.min
    out.sort(key=_key, reverse=True)
    return out


def fetch_fii_dii_today() -> dict | None:
    """Today's provisional FII/DII net figures, in ₹ Crore. Returns None
    if NSE has nothing published yet (typically available a few hours
    after close) — never a fabricated 0, which would look identical to a
    genuinely flat day."""
    try:
        raw = _nse_get("fiidiiTradeReact")
    except Exception as e:
        log.warning(f"FII/DII unavailable: {e}")
        return None
    if not isinstance(raw, list) or not raw:
        return None
    fii = dii = None
    trade_date = None
    for r in raw:
        cat = str(r.get("category", "")).upper()
        try:
            net = float(r.get("netValue", 0))
        except (TypeError, ValueError):
            continue
        if "FII" in cat or "FPI" in cat:
            fii = net
            trade_date = trade_date or r.get("date")
        elif "DII" in cat:
            dii = net
            trade_date = trade_date or r.get("date")
    if fii is None and dii is None:
        return None
    return {
        "date": trade_date,
        "fii_cr": fii,
        "dii_cr": dii,
        "net_cr": round((fii or 0) + (dii or 0), 2),
    }


# TradeFlow Pro's original 16-ticker list included 5 that are dead on
# Yahoo (^CNXHEALTH, ^CNXFINANCE, ^CNXCONSUMP, ^CNXOILGAS, ^CNXCAPITAL —
# every one throws a 404/"possibly delisted" here, confirmed directly).
# Kept to the 11 that actually resolve rather than silently dropping dead
# entries at runtime every single day for no benefit.
SECTORS = [
    ("niftyit",     "IT",         "^CNXIT"),
    ("niftybank",   "Banking",    "^NSEBANK"),
    ("niftyfmcg",   "FMCG",       "^CNXFMCG"),
    ("niftypharma", "Pharma",     "^CNXPHARMA"),
    ("niftyauto",   "Auto",       "^CNXAUTO"),
    ("niftymetal",  "Metal",      "^CNXMETAL"),
    ("niftyenergy", "Energy",     "^CNXENERGY"),
    ("niftyrealty", "Realty",     "^CNXREALTY"),
    ("niftymedia",  "Media",      "^CNXMEDIA"),
    ("niftypsubank","PSU Bank",   "^CNXPSUBANK"),
    ("niftyinfra",  "Infra",      "^CNXINFRA"),
]


def fetch_market_heat() -> list[dict]:
    """One day's move per NSE sector index. A sector with no reachable
    quote is OMITTED, not shown at 0% — a flat sector and a broken feed
    must never look the same on a heatmap.

    fast_info, not .history() — most of these tickers have a live quote
    but sparse/no daily-bar history on Yahoo (confirmed directly: 8 of 11
    working tickers returned only 1 row from .history(period="5d") but
    resolve fine via fast_info's lastPrice/previousClose)."""
    import yfinance as yf
    out = []
    for key, label, ticker in SECTORS:
        try:
            fi = yf.Ticker(ticker).fast_info
            last = fi.get("lastPrice")
            prev = fi.get("previousClose")
            if not last or not prev or prev <= 0:
                continue
            chg = round((last - prev) / prev * 100, 2)
            out.append({"key": key, "label": label, "chg_pct": chg})
        except Exception as e:
            log.debug(f"market heat {label}: {e}")
    return out


def build() -> dict:
    """Today's full payload — what market_intel.yml caches. Any piece can
    legitimately come back empty (a source down, or before NSE has
    published for the day); the caller decides what to do with that,
    this function does not paper over a gap with fake data."""
    return {
        "ok": True,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "date": date.today().isoformat(),
        "corporate_actions": fetch_corporate_actions(),
        "fii_dii": fetch_fii_dii_today(),
        "market_heat": fetch_market_heat(),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    data = build()
    print(f"Corporate actions: {len(data['corporate_actions'])}")
    for r in data["corporate_actions"][:5]:
        print(f"   {r['ex_date']:>12}  {r['symbol']:<12} {r['subject'][:50]}")
    print(f"\nFII/DII: {data['fii_dii']}")
    print(f"\nMarket heat: {len(data['market_heat'])} sectors")
    for r in sorted(data["market_heat"], key=lambda x: -x["chg_pct"]):
        print(f"   {r['chg_pct']:+6.2f}%  {r['label']}")
