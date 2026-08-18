#!/usr/bin/env python3
"""
symbols.py — single source of truth for symbol → Yahoo Finance ticker.

Why this exists: six separate call sites each did

    sym_yf = symbol if symbol.endswith(".NS") else symbol + ".NS"

which silently turned every commodity and forex symbol into a dead NSE
ticker — GOLD.NS, SILVER.NS, XAUUSD.NS, CRUDE.NS, NATGAS.NS, USDINR.NS.
Those fetches all fail, so gold/silver prices and trade outcomes never
refreshed. A single production scan on 2026-07-29 logged 1,976 failed
fetches from this one bug.

Route every symbol through to_yahoo() instead of appending ".NS".
"""

# Canonical name → Yahoo ticker, for everything that is NOT an NSE equity.
NON_EQUITY = {
    # Precious metals — COMEX futures
    "GOLD":     "GC=F",  "XAUUSD": "GC=F",  "GC": "GC=F",
    "SILVER":   "SI=F",  "XAGUSD": "SI=F",  "SI": "SI=F",
    # Energy
    "CRUDE":    "CL=F",  "WTIUSD": "CL=F",  "WTI": "CL=F",
    "BRENT":    "BZ=F",  "BRNUSD": "BZ=F",
    "NATGAS":   "NG=F",  "NGAS":   "NG=F",
    "COPPER":   "HG=F",
    # Forex
    "USDINR": "INR=X",    "USD/INR": "INR=X",
    "EURINR": "EURINR=X", "EUR/INR": "EURINR=X",
    "GBPINR": "GBPINR=X", "GBP/INR": "GBPINR=X",
    "EURUSD": "EURUSD=X", "EUR/USD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X", "GBP/USD": "GBPUSD=X",
    "AUDUSD": "AUDUSD=X", "AUD/USD": "AUDUSD=X",
    "USDJPY": "USDJPY=X", "USD/JPY": "USDJPY=X",
    # Indices
    "NIFTY":     "^NSEI",    "NIFTY50": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "SENSEX":    "^BSESN",
}

# US-listed equities and ADRs. Bare tickers with no exchange suffix, which is
# exactly what made them invisible to to_yahoo(): "SNOW" looks identical to a
# suffix-stripped NSE symbol like "DMART", so every one of these was being
# quoted as SNOW.NS — a ticker that does not exist.
#
# The damage was not cosmetic. A dead ticker never prices, so these signals sat
# OPEN with entry_price NULL indefinitely, and the ledger labelled them
# market=NSE / currency=₹ because those are the column defaults. A US equity
# was being shown to the reader in rupees at a price that had never resolved.
#
# Registered HERE rather than inferred, because there is no rule that separates
# "SNOW" from "DMART" by shape. test_symbols.py asserts this set covers every
# bare ticker in newspaper.WATCHLIST, so adding a US name to the watchlist
# without registering it fails the build instead of silently quoting SYM.NS.
US_EQUITY = frozenset({
    # Mega cap
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO",
    "BRK-B", "JPM", "LLY", "V", "UNH", "XOM", "MA", "JNJ",
    "HD", "PG", "MRK", "ABBV", "COST", "WMT", "KO", "PEP",
    "BAC", "ORCL", "CRM", "ACN", "AMD", "NFLX",
    # Growth / tech
    "CRWD", "SNOW", "DDOG", "NET", "MDB", "PANW", "ZS", "FTNT",
    "AXON", "CELH", "DUOL", "APP", "APLD", "HOOD", "COIN",
    "SMCI", "ARM", "TSM", "ASML", "NVO",
    # ADRs
    "SAP", "SIEGY", "BAYRY", "LVMUY", "IDEXY", "BNPQY", "SHEL", "AZN",
    "BABA", "JD", "PDD", "BIDU", "SE", "GRAB",
    # Energy / miners / financials
    "CVX", "COP", "SLB", "EOG", "NEM", "FCX", "RIO", "BHP",
    "GS", "MS", "C", "WFC",
})

# Suffixes that already identify a market — never append to these.
_EXPLICIT = ("=F", "=X", ".NS", ".BO", ".BSE")


def to_yahoo(symbol: str) -> str:
    """Map a canonical symbol to its Yahoo Finance ticker.

    NSE equities get ".NS". Commodities, forex and indices get their real
    ticker. Anything already carrying an explicit market marker (=F, =X,
    .NS, or a leading ^) is returned untouched.
    """
    if not symbol:
        return symbol
    s = symbol.strip().upper()
    if s.startswith("^") or s.endswith(_EXPLICIT):
        return s
    if s in NON_EQUITY:
        return NON_EQUITY[s]
    # NON_EQUITY is checked FIRST on purpose. "GOLD" is both a COMEX contract
    # and Barrick Gold on the NYSE, and every engine here means the metal.
    # Commodity wins; the equity is simply not in the US set.
    if s in US_EQUITY:
        return s                       # already a valid Yahoo ticker
    return s + ".NS"


def is_equity(symbol: str) -> bool:
    """True if the symbol resolves to an NSE equity."""
    return to_yahoo(symbol).endswith(".NS")


def classify(symbol: str) -> tuple[str, str]:
    """(market, asset_type) for the ledger's own columns.

    `all_signals.market` and `.asset_type` exist so consumers never have to
    guess an instrument's type from its ticker string — and then no writer in
    the codebase ever set them, so every row took the schema defaults 'NSE' /
    'Equity'. Commodities and forex therefore looked like NSE equities to
    every consumer that trusted the columns.

    That is not cosmetic. /api/ticker selects open rows with
    `market='NSE' AND asset_type='Equity'` and appends ".NS" to quote them, so
    a mistagged SILVER row was quoted as **SILVER.NS — a real, unrelated NSE
    company trading at ₹233** while silver itself was $66/oz. The ledger
    published ₹233 as the last price of a silver trade. GOLD.NS and CRUDE.NS
    merely 404, which is why only silver was visibly wrong; the tagging was
    equally broken for all of them.

    A wrong mark is dangerous, not just untidy — a placeholder mark price has
    already closed metals longs at their stop once (see the APEX phantom
    stop-out forensics). Derived from the same NON_EQUITY map that to_yahoo()
    uses, so the two can never disagree.
    """
    y = to_yahoo(symbol)
    if y.endswith("=F"):
        return "COMEX", "Commodity"
    if y.endswith("=X"):
        return "FX", "Currency"
    if y.startswith("^"):
        return "NSE", "Index"
    if y.endswith((".BO", ".BSE")):
        return "BSE", "Equity"
    # US listings resolve to a BARE ticker, which is indistinguishable from a
    # suffix-stripped NSE symbol by shape alone — so this asks the registry
    # rather than the string. Without it every US name fell through to the
    # NSE/Equity default below, which is how SNOW and SMCI were published in
    # rupees at a price that had never resolved (fixed 2026-08-18).
    if y.upper() in US_EQUITY:
        return "US", "Equity"
    return "NSE", "Equity"


# ── Currency ─────────────────────────────────────────────────────────────────
# Deliberately thin. classify() is the authority on what an instrument IS;
# these only translate that into the unit a price is printed in. Two functions
# that could each decide a market independently is precisely how
# standalone_scan._unit() and the ledger's columns came to disagree.

def market_of(symbol: str) -> str:
    """Exchange family — NSE / BSE / US / COMEX / FX."""
    return classify(symbol)[0]


def asset_type_of(symbol: str) -> str:
    """Equity / Commodity / Currency / Index."""
    return classify(symbol)[1]


def currency_of(symbol: str) -> str:
    """The unit a price in this symbol is denominated in.

    "" for an FX pair, because a rate is not a money amount — printing
    "$1.0842" for EURUSD states the wrong thing twice.
    """
    market, atype = classify(symbol)
    if atype == "Currency":
        s = (symbol or "").strip().upper().replace("/", "")
        # USDINR quotes rupees per dollar, so the AMOUNT is in rupees.
        return "\u20b9" if s.endswith("INR") else ""
    if market in ("US", "COMEX"):
        return "$"
    return "\u20b9"
