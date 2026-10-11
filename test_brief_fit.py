#!/usr/bin/env python3
"""
test_brief_fit.py — the brief must never exceed what Telegram will accept.

Telegram rejects a sendMessage over 4096 characters OUTRIGHT. It does not
truncate. So an over-long brief does not arrive shortened — it does not arrive
at all, and the only trace is a send error nobody reads.

_fit() used to end with `return render(2, False)`: it measured the text, found
it still too long, and returned it anyway. Nothing was watching, because until
2026-08-27 nothing had ever grown enough to reach that branch. Adding the
CAREERS section reached it immediately — the evening brief came to 4,104
characters — and the failure would have been a silent non-delivery.

These checks stand between that branch and production.

Usage:
    python test_brief_fit.py
"""
from __future__ import annotations

import os
import sys

# ── THIS SUITE MEASURES TEXT LENGTH AND MUST NOT NEED A BOT TOKEN ───────────
#
# daily_brief imports telegram_bot, which imports config, which RAISES at
# import time if TELEGRAM_TOKEN is unset. So merely importing the module under
# test demanded a live credential — and when this file was wired into
# newspaper.yml, a workflow that builds a static page and has no business
# holding a bot token, the whole newspaper build went red on a missing secret.
#
# Placeholders are set only when the real ones are absent, so a local run with
# a real .env is untouched. Nothing here sends: every check calls _fit() and
# counts characters. A token that cannot possibly authenticate is the correct
# thing for a test that must never reach the network.
os.environ.setdefault("TELEGRAM_TOKEN", "test-token-not-a-real-credential")
os.environ.setdefault("TELEGRAM_CHAT_ID", "0")

import daily_brief as D

TELEGRAM_HARD_CAP = 4096

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


@check("TG_LIMIT leaves headroom under Telegram's hard 4096")
def _():
    assert D.TG_LIMIT < TELEGRAM_HARD_CAP, \
        f"TG_LIMIT {D.TG_LIMIT} is not below the {TELEGRAM_HARD_CAP} hard cap"


@check("every real slot builds a sendable brief")
def _():
    for slot in ("morning", "midday", "evening"):
        n = len(D.build_section_brief(slot))
        assert n <= TELEGRAM_HARD_CAP, f"{slot} brief is {n} chars"


@check("_fit NEVER returns over the limit, even when nothing can be dropped")
def _():
    # The exact branch that used to lie. One undroppable 20k line: no lever
    # can help, so the fitter must hard-trim rather than hand back an
    # unsendable string.
    out = D._fit(["X" * 20000])
    assert len(out) <= D.TG_LIMIT, \
        f"_fit returned {len(out)} chars — the silent non-delivery bug is back"


@check("a hard trim cuts on a line boundary, never mid-Markdown")
def _():
    # An unbalanced * makes Telegram 400 the whole message: the same silent
    # non-delivery, one step later.
    lines = [f"*line {i} bold*" for i in range(600)]
    out = D._fit(lines)
    assert len(out) <= D.TG_LIMIT
    assert out.count("*") % 2 == 0, "hard trim left an unbalanced Markdown pair"


@check("_Opt gives up its detail before the section disappears")
def _():
    opt = D._Opt(["ESSENTIAL"], ["DETAIL " + "y" * 4000])
    out = D._fit([opt])
    assert "ESSENTIAL" in out, "the summary was dropped instead of the detail"
    assert "DETAIL" not in out, "oversized detail survived"


@check("_Opt keeps its detail when there is room")
def _():
    out = D._fit([D._Opt(["ESSENTIAL"], ["DETAIL"])])
    assert "ESSENTIAL" in out and "DETAIL" in out


@check("careers reports the counts in every slot, rows or no rows")
def _():
    # The counts ARE the answer to "what happened to new and existing roles".
    # Rows are a nice-to-have; losing them is fine, losing the answer is not.
    for slot in ("morning", "evening"):
        b = D.build_section_brief(slot)
        assert "CAREERS" in b, f"{slot} lost the careers section entirely"
        assert ("NEW today" in b), f"{slot} careers lost its new-roles count"
        assert "Standing pipeline" in b, f"{slot} careers lost the live count"


# ── Every link goes to alpha ──────────────────────────────────────────────
# 2026-10-12: Signal, Vision and Gems became alpha.askakshay.com. The brief used to
# send readers to three different hosts depending on the section.

SECTIONS = ("home", "markets", "screen", "magic", "map", "ipo", "funds", "reads",
            "news", "wire", "world", "health")


@check("every section links to alpha")
def _():
    for key in SECTIONS:
        assert D._link(key).startswith("https://alpha.askakshay.com/"), \
            f"{key} -> {D._link(key)}"


@check("an unknown section links to alpha's front page rather than raising")
def _():
    # A mistyped key must not be able to stop the brief from sending.
    assert D._link("no_such_section") == "https://alpha.askakshay.com/"


@check("no section names a host outside the link table")
def _():
    # A hardcoded host anywhere in the builder is a link that will rot in place.
    import inspect, re
    for fn in (D.build_section_brief, D._alpha_markets, D._alpha_barometer,
               D._alpha_sectors, D._alpha_movers, D._alpha_ipo, D._alpha_engines):
        stray = re.findall(r"[\"']([a-z]+\.askakshay\.com)", inspect.getsource(fn))
        assert not stray, f"hardcoded hosts in {fn.__name__}: {set(stray)}"


# ── Offline fixtures: the builder, fed by canned feeds ────────────────────

FEEDS = {
    "/api/quotes": {"live": 3, "total": 3, "quotes": [
        {"id": "nifty", "name": "Nifty 50", "price": 22520.45, "pct": 1.3, "dp": 0, "prefix": ""},
        {"id": "vix", "name": "India VIX", "price": 14.38, "pct": -5.9, "dp": 2, "prefix": ""},
        {"id": "gold", "name": "Gold (futures)", "price": None, "pct": None, "dp": 1, "prefix": "$"},
    ]},
    "/data/barometer.json": {"today": {"score": 29, "band": {"t": "Weak"}, "parts": [
        {"label": "Trend", "score": 1}, {"label": "Volatility", "score": 70}]}},
    "/data/pulse.json": {
        "breadth": {"counted": 100, "up": 40, "down": 60, "above_200dma": 47},
        "sectors_day": [{"name": n, "median": m} for n, m in
                        (("Tech", 2.6), ("Energy", 0.5), ("Auto", -1.2), ("Banks", 0.9), ("Metals", -2.0))],
        "movers_up": [{"sym": "AAA", "r1w": 20.0}], "movers_dn": [{"sym": "ZZZ", "r1w": -15.0}]},
    "/engines.json": {"engines": {"breakout": {"id": 3, "key": "breakout", "name": "Breakout", "status": "LEANING POSITIVE", "signal_count": 1},
                                  "rebound": {"id": 1, "key": "rebound", "name": "Rebound", "status": "WORSE THAN RANDOM", "signal_count": 1}},
                     "signals": [{"engine": "breakout", "engine_name": "Breakout", "sym": "TCS", "status": "pending", "entry_zone": [3100.0, 3131.0], "sl": 2950.0, "targets": [3250.0, 3400.0, 3700.0], "max_hold_days": 40},
                                 {"engine": "rebound", "engine_name": "Rebound", "sym": "BADCO", "status": "pending", "entry_zone": [100.0, 101.0], "sl": 90.0, "targets": [110.0, 120.0, 130.0], "max_hold_days": 60}]},
    "/data/ipo.json": {"open": [{"company": "Example Ltd", "price_band": "Rs.100 to Rs.110",
                                 "close_date": "2026-10-14", "subscription_x": 1.5}],
                       "awaiting_listing": [{"symbol": "EXM"}]},
}

# Words that belong to the retired engines and paper books. Their absence is the point.
RETIRED = ("TRADE IDEAS", "SIGNAL LOG", "PERFORMANCE", "APEX", "expectancy", "win rate",
           "ledger", "paper trad", "signal.askakshay", "vision.askakshay",
           "gems.askakshay", "terminal.askakshay", "news.askakshay")


def _build_offline(slot, feeds=FEEDS):
    real_a, real_n = D._alpha_get, D._news_get
    D._alpha_get = lambda path, timeout=15: feeds.get(path)
    D._news_get = lambda path, timeout=12: None
    try:
        return D.build_section_brief(slot)
    finally:
        D._alpha_get, D._news_get = real_a, real_n


@check("the brief renders alpha's own sections from alpha's feeds")
def _():
    b = _build_offline("morning")
    for needle in ("ALPHA BRIEF", "01 · MARKETS", "Nifty 50", "02 · BAROMETER", "29/100",
                   "03 · SECTORS", "04 · MOVERS", "`AAA`", "07 · ENGINES", "08 · IPOs", "Example Ltd",
                   "https://alpha.askakshay.com/markets/"):
        assert needle in b, f"missing {needle!r}:\n{b[:600]}"


@check("the brief carries nothing from the retired engines, books or sites")
def _():
    for slot in ("morning", "evening"):
        b = _build_offline(slot)
        for word in RETIRED:
            assert word.lower() not in b.lower(), f"{slot} brief still says {word!r}"


@check("engine plans are listed only for engines whose own tests are positive")
def _():
    b = _build_offline("morning")
    assert "`TCS`" in b and "stop 2,950" in b, b[:900]
    assert "BADCO" not in b, "a plan from an engine that does not beat random stocks was listed"
    assert "Worse Than Random" in b, "that engine's honest record is not stated"


@check("a dead quote is omitted, never shown as 0%")
def _():
    b = _build_offline("morning")
    assert "Gold" not in b, "a quote with no price was printed"


@check("every alpha source down: the brief still sends, without those sections")
def _():
    b = _build_offline("morning", feeds={})
    assert len(b) <= TELEGRAM_HARD_CAP
    assert "01 · MARKETS" not in b and "ALPHA BRIEF" in b


@check("send_brief posts the brief and nothing else alongside it")
def _():
    import inspect
    src = inspect.getsource(D.send_brief)
    assert "AKK Times" not in src and "RAILWAY_PUBLIC_DOMAIN" not in src, \
        "the newspaper-link post is back"
    assert src.count("_post(") == 1, "send_brief posts more than the brief"


def main() -> int:
    failed = 0
    for name, fn in CHECKS:
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as e:
            print(f"FAIL  {name}\n      {e}")
            failed += 1
    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
