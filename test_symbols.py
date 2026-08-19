#!/usr/bin/env python3
"""
test_symbols.py — a symbol must resolve to the right market, ticker and unit.

Written for a live bug found 2026-08-18: SNOW, SMCI and MSFT were being shown
on news.askakshay.com priced in RUPEES. The cause was not the display layer.
to_yahoo() appends ".NS" to anything it does not recognise, and it recognised
only commodities, FX and indices — so all 77 US names in the watchlist were
being quoted as SNOW.NS, MSFT.NS and so on.

Those tickers do not exist. The signals never priced, sat OPEN with a NULL
entry price indefinitely, and rendered with the schema's ₹ default.

Offline. No network, no pytest.

Usage:
    python3 test_symbols.py
"""
from __future__ import annotations

import logging
import sys

logging.basicConfig(level=logging.ERROR)

import re
from pathlib import Path

import symbols as sy

ROOT = Path(__file__).resolve().parent

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


# ── The bug ──────────────────────────────────────────────────────────────────

@check("US equities keep their bare ticker — never SNOW.NS")
def _():
    for s in ("SNOW", "SMCI", "MSFT", "NVDA", "AAPL", "BABA", "BRK-B"):
        assert sy.to_yahoo(s) == s, f"{s} -> {sy.to_yahoo(s)}"


@check("US equities price in dollars, not rupees")
def _():
    for s in ("SNOW", "SMCI", "MSFT", "TSLA", "COIN"):
        assert sy.currency_of(s) == "$", f"{s} -> {sy.currency_of(s)!r}"


@check("US equities are not labelled NSE")
def _():
    for s in ("SNOW", "SMCI", "MSFT"):
        assert sy.market_of(s) == "US"


# ── Nothing that already worked may break ────────────────────────────────────

@check("NSE equities still get .NS and still price in rupees")
def _():
    assert sy.to_yahoo("DMART") == "DMART.NS"
    assert sy.currency_of("DMART") == "₹"
    assert sy.market_of("DMART") == "NSE"


@check("an already-suffixed NSE ticker is left alone")
def _():
    assert sy.to_yahoo("RELIANCE.NS") == "RELIANCE.NS"
    assert sy.currency_of("RELIANCE.NS") == "₹"


@check("commodities still resolve to their futures contract, in dollars")
def _():
    assert sy.to_yahoo("CRUDE") == "CL=F"
    assert sy.currency_of("CRUDE") == "$"
    assert sy.asset_type_of("CRUDE") == "Commodity"


@check("GOLD stays the COMEX contract, not Barrick Gold the NYSE equity")
def _():
    # A real collision: NYSE:GOLD is Barrick. Every engine here means the
    # metal, so NON_EQUITY is checked before US_EQUITY and commodity wins.
    assert sy.to_yahoo("GOLD") == "GC=F"
    assert sy.market_of("GOLD") == "COMEX"


@check("an FX rate carries no currency symbol")
def _():
    # "$1.0842" for EURUSD states the wrong thing twice.
    assert sy.currency_of("EURUSD") == ""
    assert sy.asset_type_of("EURUSD") == "Currency"


@check("USDINR is quoted IN rupees — rupees per dollar")
def _():
    assert sy.currency_of("USDINR") == "₹"


@check("indices are recognised and never suffixed")
def _():
    assert sy.to_yahoo("NIFTY") == "^NSEI"
    assert sy.to_yahoo("^NSEI") == "^NSEI"
    # classify() calls an index market="NSE", type="Index" — NIFTY genuinely
    # trades on the NSE. The TYPE is what distinguishes it from an equity.
    assert sy.classify("NIFTY") == ("NSE", "Index")
    assert sy.currency_of("NIFTY") == "₹"


# ── The set cannot silently fall behind the watchlist ────────────────────────

@check("every bare ticker in WATCHLIST is registered as a US equity")
def _():
    """The guard that stops this recurring.

    Adding a US name to newspaper.WATCHLIST without registering it here would
    reintroduce the exact bug — SYM.NS, no price, rupees. This fails the build
    instead.
    """
    import newspaper as np
    missing = [s for s in np.WATCHLIST
               if "." not in s and not s.startswith("^")
               and s.upper() not in sy.NON_EQUITY
               and s.upper() not in sy.US_EQUITY]
    assert not missing, f"unregistered US tickers: {sorted(set(missing))}"


@check("standalone_scan._unit agrees with symbols.currency_of")
def _():
    """Two sources of truth for one answer is how the bug survived."""
    from standalone_scan import _unit
    for s in ("SNOW", "SMCI", "DMART", "CRUDE", "GOLD", "USDINR", "EURUSD"):
        assert _unit(s) == sy.currency_of(s), f"{s}: {_unit(s)!r} vs {sy.currency_of(s)!r}"


@check("classify() is the one authority — the helpers agree with it")
def _():
    # Two functions that could each decide a market independently is how
    # _unit() and the ledger's columns disagreed in the first place.
    for x in ("SNOW", "DMART", "CRUDE", "USDINR", "EURUSD", "NIFTY", "RELIANCE.NS"):
        assert (sy.market_of(x), sy.asset_type_of(x)) == sy.classify(x)


@check("the ledger's own classifier tags US equities as US, not NSE")
def _():
    # /api/ticker quotes every market='NSE' row by appending ".NS" — the same
    # path that once published a silver trade at the price of an unrelated
    # NSE company called SILVER. A mistagged US row is the same failure.
    assert sy.classify("SNOW") == ("US", "Equity")
    assert sy.classify("DMART") == ("NSE", "Equity")


@check("the JavaScript API agrees with symbols.py about the US universe")
def _():
    """The half that actually reaches the reader.

    signals.js does `currency: currencyOf(r.symbol)` — the ledger has no
    currency column, so the LIVE value is computed in JavaScript. Fixing only
    the Python side would have fixed nothing a visitor can see. Two languages
    holding the same registry can drift, so this compares them.
    """
    js = (ROOT / "vercel-news" / "api" / "_db.js").read_text()
    block = js[js.index("const US_EQUITY = new Set(["):js.index("])", js.index("const US_EQUITY"))]
    in_js = set(re.findall(r'"([A-Z0-9.\-]+)"', block))
    assert in_js == set(sy.US_EQUITY), (
        f"drift — only in Python: {sorted(set(sy.US_EQUITY) - in_js)}; "
        f"only in JS: {sorted(in_js - set(sy.US_EQUITY))}")


@check("the JS currency precedence matches Python's — commodity beats equity")
def _():
    js = (ROOT / "vercel-news" / "api" / "_db.js").read_text()
    body = js[js.index("export function currencyOf"):]
    body = body[:body.index("}")]
    # NYSE:GOLD is Barrick; every engine here means the metal.
    assert body.index("USD_SYMBOLS.has") < body.index("US_EQUITY.has")


@check("an unknown symbol still defaults to NSE rather than raising")
def _():
    assert sy.to_yahoo("SOMETHINGNEW") == "SOMETHINGNEW.NS"
    assert sy.currency_of("") == "₹"
    assert sy.to_yahoo("") == ""


# ── Which slot grades which market ───────────────────────────────────────────
# Live on 2026-08-19: the 16:30 IST end-of-day scan sent "SL HIT — SMCI" at
# 16:41 IST, when the US market had been shut 14 hours and would not reopen for
# two. The exit itself was graded off a completed bar and was correct; the
# timing detached it from the session it described.

@check("the US slot grades US names, and only US names")
def _():
    from standalone_scan import SLOT_MARKETS
    assert SLOT_MARKETS["us"] == {"US"}


@check("the NSE slots do NOT grade US names")
def _():
    from standalone_scan import SLOT_MARKETS
    for slot in ("midday", "eod"):
        assert "US" not in SLOT_MARKETS[slot], f"{slot} still grades US symbols"


@check("commodities and FX stay in the NSE slots — they have no daily close")
def _():
    from standalone_scan import SLOT_MARKETS
    for slot in ("midday", "eod"):
        assert {"COMEX", "FX"} <= SLOT_MARKETS[slot]


@check("every cron slot arm has a market set, and vice versa")
def _():
    """A slot with no entry falls through to None, which grades EVERYTHING —
    silently reintroducing the exact bug, because nothing fails."""
    import re
    from standalone_scan import SLOT_MARKETS
    wf = (ROOT / ".github" / "workflows" / "daily_scan.yml").read_text()
    arms = set(re.findall(r"SLOT=(\w+)", wf))
    missing = arms - set(SLOT_MARKETS)
    assert not missing, f"cron slots with no market set: {sorted(missing)}"


@check("the symbols that actually alerted route to the US slot")
def _():
    # NET, MDB and SMCI are the three that arrived at the wrong hour.
    for sym in ("NET", "MDB", "SMCI"):
        assert sy.market_of(sym) == "US", f"{sym} -> {sy.market_of(sym)}"


@check("an NSE name is not swept into the US slot")
def _():
    for sym in ("RELIANCE", "COALINDIA", "TECHM", "IOC"):
        assert sy.market_of(sym) == "NSE", f"{sym} -> {sy.market_of(sym)}"


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
