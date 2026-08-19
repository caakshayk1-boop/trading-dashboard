#!/usr/bin/env python3
"""
ipo_tracker.py — every NSE name listed in the last N months, and what it did.

WHY THIS IS NOT PART OF THE STOCK SCREEN

The screen ranks companies on published annual statements. A company listed
four months ago has no annual statements to rank, no three-year CAGR, and no
one-year return — so it scores None on almost everything and sits unranked at
the bottom, which is correct for that screen and useless as an answer to "how
have this year's listings actually done".

They are different questions, so they are different artefacts.

WHAT IT REFUSES TO DO

  * No issue price. NSE's issue-price data is not reliably reachable, and
    "listing gain" computed off a guessed issue price is a fabricated number
    on a page whose whole argument is that it does not fabricate. The return
    here is measured from the FIRST TRADED CLOSE, and the field is named for
    that, so nobody can mistake it for the IPO allotment return.
  * No forecast, no target, no "should you buy". Same rule as the screen.
  * No zero-filling. A name whose history cannot be read is omitted from the
    table and counted in the coverage line instead.

THE TWO-PASS FETCH

Establishing "listed recently" needs firstTradeDate, which lives in Yahoo's
chart meta. Pulling two years of daily bars for all 750 names to find the ~40
that are recent would be ~11 minutes of fetching to discard 95% of it.

So: pass one asks every symbol for a 5-day window, which is the cheapest
request that still returns meta.firstTradeDate. Pass two pulls real history
only for the names that survived. Weekly artefact, its own workflow, never
inside the 6 AM build — the same rule the fund and stock screens already
follow for the same reason.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# A listing stops being "recent" here. Twelve months is what was asked for;
# the constant is the only place it is defined so the section heading, the
# filter and the tests cannot disagree about it.
RECENT_MONTHS = 12

# Below this many traded sessions there is not enough of a record to say
# anything. A name listed on Friday has one close and no range.
MIN_SESSIONS = 5


def months_since(ts, now: datetime | None = None) -> float | None:
    """Months between a listing timestamp and now. None if unreadable.

    Yahoo gives firstTradeDate as a unix epoch. None is never treated as
    recent by the caller — an unreadable listing date is exactly the case
    where guessing would put a decade-old company in a table of new listings.
    """
    now = now or datetime.now(timezone.utc)
    if ts is None:
        return None
    try:
        first = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    return (now - first).days / 30.44


def is_recent_listing(ts, months: int = RECENT_MONTHS, now: datetime | None = None) -> bool:
    age = months_since(ts, now)
    # A negative age means a listing date in the future — a bad epoch, not a
    # new listing, and it must not sneak in as "very recent".
    return age is not None and 0 <= age <= months


def performance(closes: list[float], first_date: str | None = None) -> dict | None:
    """What a listing has done since its first close. None if unmeasurable.

    Every figure is measured from the first CLOSE in the series, never from an
    issue price, and the keys say so.
    """
    clean = [float(c) for c in (closes or []) if c is not None and float(c) > 0]
    if len(clean) < MIN_SESSIONS:
        return None
    first, last = clean[0], clean[-1]
    high, low = max(clean), min(clean)
    return {
        "first_close": round(first, 2),
        "last_close": round(last, 2),
        "since_listing_pct": round((last - first) / first * 100, 1),
        "high": round(high, 2),
        "low": round(low, 2),
        # How far off its post-listing peak it is now. The single most useful
        # number for a recent listing and the one most often left out.
        "from_high_pct": round((last - high) / high * 100, 1),
        # Did it ever trade below its first close? A name that never did is a
        # different animal from one that halved and recovered.
        "below_first_ever": bool(low < first),
        "sessions": len(clean),
        "listed_on": first_date,
    }


def summarise(rows: list[dict]) -> dict:
    """Roll-up for the section heading. Counts only, never an average return —
    a mean across listings of wildly different ages is not a meaningful number
    and would be the first thing quoted out of context."""
    n = len(rows)
    up = sum(1 for r in rows if (r.get("since_listing_pct") or 0) > 0)
    return {
        "count": n,
        "up": up,
        "down": n - up,
        "up_pct": round(100.0 * up / n, 1) if n else None,
        "median_pct": _median([r["since_listing_pct"] for r in rows
                               if r.get("since_listing_pct") is not None]),
    }


def _median(xs: list[float]) -> float | None:
    """Median, not mean. One 300% listing should not describe the cohort."""
    if not xs:
        return None
    s = sorted(xs)
    m = len(s) // 2
    return round(s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2, 1)


# ── Fetch ────────────────────────────────────────────────────────────────────
# Kept below the pure functions and importing yfinance lazily, so the
# arithmetic above stays testable with no network and no third-party package.

def _chart(symbol: str, rng: str) -> dict | None:
    """Yahoo chart meta + closes for one symbol, or None."""
    import requests
    from symbols import to_yahoo
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{to_yahoo(symbol)}",
            params={"range": rng, "interval": "1d"},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        if r.status_code != 200:
            return None
        res = (r.json().get("chart") or {}).get("result") or []
        return res[0] if res else None
    except Exception as e:                                   # noqa: BLE001
        log.debug(f"ipo chart {symbol}: {e}")
        return None


def build(symbols: list[str] | None = None, months: int = RECENT_MONTHS,
          now: datetime | None = None) -> dict:
    """The recent-listings table. Never raises; reports its own coverage.

    Two passes — see the module docstring. The coverage numbers are recorded
    rather than the failures being swallowed, because a run that could only
    reach 200 of 750 symbols must not publish as though it had seen them all.
    """
    now = now or datetime.now(timezone.utc)
    if symbols is None:
        try:
            import stock_screen
            symbols = [u["symbol"] for u in stock_screen.universe()]
        except Exception as e:                               # noqa: BLE001
            return {"ok": False, "error": f"universe unavailable: {e}"}

    attempted = len(symbols)
    probed = 0
    candidates: list[tuple[str, float]] = []
    for sym in symbols:
        c = _chart(sym, "5d")                 # cheapest request carrying meta
        if not c:
            continue
        probed += 1
        ts = (c.get("meta") or {}).get("firstTradeDate")
        if is_recent_listing(ts, months, now):
            candidates.append((sym, ts))

    rows = []
    for sym, ts in candidates:
        c = _chart(sym, "2y")
        if not c:
            continue
        closes = (((c.get("indicators") or {}).get("quote") or [{}])[0] or {}).get("close") or []
        listed = datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d")
        perf = performance(closes, listed)
        if not perf:
            continue
        rows.append({"sym": sym, "months_listed": round(months_since(ts, now), 1), **perf})

    rows.sort(key=lambda r: r["listed_on"], reverse=True)
    return {
        "ok": True,
        "generated_at": now.isoformat(),
        "months": months,
        "rows": rows,
        "count": len(rows),
        # Coverage is the honest denominator: how many of the universe this run
        # could actually read. A partial probe must never look like a complete
        # one — the same rule the stock screen learned at 50/750.
        "attempted": attempted,
        "probed": probed,
        "summary": summarise(rows),
    }
