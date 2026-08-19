#!/usr/bin/env python3
"""
test_ipo_tracker.py — the recent-listings table cannot invent a number.

The risk in this artefact is not a crash. It is a plausible-looking return
computed off something that is not what the label says: an issue price that
was guessed, a mean that one 300% listing dominates, or a company from 2011
sitting in a table headed "listed in the last 12 months" because its listing
date could not be read.

Offline — every check runs against the pure functions, which is why the fetch
layer is kept separate from them.

Usage:
    python3 test_ipo_tracker.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import ipo_tracker as ip

NOW = datetime(2026, 8, 18, tzinfo=timezone.utc)
CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


def epoch(days_ago: int) -> float:
    return (NOW - timedelta(days=days_ago)).timestamp()


# ── What counts as a recent listing ──────────────────────────────────────────

@check("a listing from three months ago is recent")
def _():
    assert ip.is_recent_listing(epoch(90), now=NOW)


@check("a listing from three years ago is not")
def _():
    assert not ip.is_recent_listing(epoch(1100), now=NOW)


@check("an unreadable listing date is NOT treated as recent")
def _():
    # Guessing here is how a 2011 company lands in a table of new listings.
    for bad in (None, "", "yesterday", float("nan")):
        assert not ip.is_recent_listing(bad, now=NOW), bad


@check("a listing date in the future is rejected, not read as very recent")
def _():
    assert not ip.is_recent_listing((NOW + timedelta(days=30)).timestamp(), now=NOW)


@check("the boundary is the window, not a rounding of it")
def _():
    assert ip.is_recent_listing(epoch(360), months=12, now=NOW)
    assert not ip.is_recent_listing(epoch(400), months=12, now=NOW)


# ── The arithmetic ───────────────────────────────────────────────────────────

@check("return is measured from the first CLOSE, and the key says so")
def _():
    # Five closes, because MIN_SESSIONS is the floor — fewer returns None,
    # which is the behaviour the "too few sessions" check pins.
    p = ip.performance([100, 105, 108, 112, 130])
    assert p["first_close"] == 100 and p["since_listing_pct"] == 30.0
    assert "issue" not in " ".join(p.keys()), "no key may imply an issue price"


@check("drawdown from the post-listing high is reported")
def _():
    p = ip.performance([100, 200, 150, 150, 150])
    assert p["high"] == 200 and p["from_high_pct"] == -25.0


@check("a name that never traded below its first close is flagged as such")
def _():
    assert ip.performance([100, 105, 110, 120, 130])["below_first_ever"] is False
    assert ip.performance([100, 95, 110, 120, 130])["below_first_ever"] is True


@check("too few sessions means no answer, not a zero")
def _():
    # A name listed on Friday has one close and no range. Publishing 0% would
    # be a claim about a stock that has not traded.
    assert ip.performance([100]) is None
    assert ip.performance([100, 101]) is None
    assert ip.performance([]) is None
    assert ip.performance(None) is None


@check("null and non-positive prices are dropped, never zero-filled")
def _():
    p = ip.performance([100, None, 0, -5, 120, 130, 140, 150])
    assert p["sessions"] == 5, p["sessions"]
    assert p["first_close"] == 100 and p["last_close"] == 150


@check("a series that is all nulls yields nothing")
def _():
    assert ip.performance([None, None, None, None, None]) is None


# ── The roll-up ──────────────────────────────────────────────────────────────

@check("the cohort figure is a MEDIAN, not a mean")
def _():
    # One 300% listing must not describe the cohort. Mean here would be 64.2%.
    rows = [{"since_listing_pct": x} for x in (-20, -5, 2, 8, 300)]
    s = ip.summarise(rows)
    assert s["median_pct"] == 2, s["median_pct"]


@check("the split of winners and losers is counted, not estimated")
def _():
    rows = [{"since_listing_pct": x} for x in (-20, -5, 2, 8)]
    s = ip.summarise(rows)
    assert (s["count"], s["up"], s["down"], s["up_pct"]) == (4, 2, 2, 50.0)


@check("an empty cohort reports nothing rather than 0%")
def _():
    s = ip.summarise([])
    assert s["count"] == 0 and s["median_pct"] is None and s["up_pct"] is None


@check("a row with no return does not drag the median toward zero")
def _():
    rows = [{"since_listing_pct": 10}, {"since_listing_pct": 20}, {"other": 1}]
    assert ip.summarise(rows)["median_pct"] == 15


@check("months_since is unreadable-safe and monotonic")
def _():
    assert ip.months_since(None) is None
    assert ip.months_since("nonsense") is None
    a, b = ip.months_since(epoch(30), NOW), ip.months_since(epoch(300), NOW)
    assert 0.9 < a < 1.1 and b > a


@check("build() reports coverage rather than swallowing unreachable symbols")
def _():
    # No network here — every symbol fails to fetch, which is exactly the
    # partial-run case. It must say so instead of publishing an empty table
    # as though the market had no new listings.
    out = ip.build(symbols=[], now=NOW)
    assert out["ok"] is True
    assert out["attempted"] == 0 and out["count"] == 0
    assert "probed" in out, "coverage is not reported"


def main() -> int:
    passed = failed = 0
    for name, fn in CHECKS:
        try:
            fn()
        except AssertionError as e:
            print(f"  FAIL  {name}  ({e})"); failed += 1
        except Exception as e:
            print(f"  ERROR {name}  ({type(e).__name__}: {e})"); failed += 1
        else:
            print(f"  PASS  {name}"); passed += 1
    print(f"\n{passed} passed · {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
