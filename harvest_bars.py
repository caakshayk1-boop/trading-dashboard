#!/usr/bin/env python3
"""
harvest_bars.py — fetch the bars the research engines read, and say what it got.

WHY THIS FILE EXISTS
--------------------
scan_research.py, scan_buoy.py and the two research backtests all read
`barsH.json` and `barsD3y.json`. Nothing wrote them. Five readers, zero
writers — the files were harvested by hand into a scratch folder on one Mac,
and `research.yml` has therefore never completed a single run in CI. It failed
earlier than this, on a missing package, which is what hid the real problem.

This writes them.

FETCHED WITH yfinance, AND THE FIRST VERSION WAS NOT
---------------------------------------------------
This originally called Yahoo's chart endpoint over urllib, copied from
scan_buoy.py. Measured on 2026-09-09 from a GitHub runner: 500 of 500 symbols
returned nothing, over 35 minutes, every one of them. Not slow — zero.

Yahoo was not blocking the runner. Ninety minutes earlier, on the same
infrastructure, standalone_scan fetched 113 tickers in a single yfinance call
in about four seconds. The difference is the cookie-and-crumb handshake
yfinance performs and a raw urllib GET does not: scan_buoy's approach works
from a laptop IP and is refused from a datacenter one, which is very likely
why the bars were being harvested by hand on a Mac in the first place.

So this uses yfinance, batched, which also collapses ~1000 sequential requests
into a dozen. The three-worker pacing the old version copied was governing a
request pattern that no longer exists.

COVERAGE IS REPORTED, NEVER ASSUMED
-----------------------------------
A symbol that 404s, times out, or comes back with too few bars is skipped and
counted. The manifest written alongside the bars records how many names were
asked for and how many answered, because the scan that reads these files
publishes a "scanned" number and a narrower run must never be mistaken for the
full universe. That rule is already written into scan_research.py's docstring;
this is the other half of it.

Usage:
    python3 harvest_bars.py                 # Nifty 500, both ranges
    python3 harvest_bars.py --limit 50      # a slice, for a smoke test
    python3 harvest_bars.py --hourly-only   # skip the 3-year daily pull
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

import bars_cache

# Chunked rather than one call for the whole universe: a single request for 500
# symbols of hourly bars is a large payload and one failure loses everything.
# A chunk failing loses fifty names and says so.
CHUNK = 50
PACE = 1.0            # between chunks, not between symbols — there are ~20 now
PROGRESS_EVERY = 1    # every chunk

# What each engine needs. BUOY reads 4H candles resampled from hourly, so 60
# days of hourly is ~4 months of 4H — enough for its 200-period average plus
# the lookback. ANCHOR and BEDROCK read daily over a long base.
RANGES = {
    bars_cache.HOURLY:   {"interval": "1h", "period": "60d", "min_bars": 320},
    bars_cache.DAILY_3Y: {"interval": "1d", "period": "3y",  "min_bars": 200},
}


def _log(msg: str) -> None:
    print(msg, flush=True)


def _rows(frame) -> list:
    """A yfinance frame -> [(epoch_seconds, o, h, l, c, v), ...].

    That tuple shape is what signals/buoy.to_4h and the two daily engines read,
    and the epoch seconds are what they hand to datetime.fromtimestamp.
    """
    out = []
    for ts, bar in frame.iterrows():
        try:
            o = float(bar["Open"]); h = float(bar["High"])
            l = float(bar["Low"]);  c = float(bar["Close"])
            v = float(bar["Volume"]) if "Volume" in bar else 0.0
        except (KeyError, TypeError, ValueError):
            continue
        if not all(x == x for x in (o, h, l, c)):      # NaN check without numpy
            continue
        out.append((int(ts.timestamp()), o, h, l, c, v))
    return out


def harvest(symbols: list, name: str) -> tuple[dict, dict]:
    """Fetch one range for every symbol, in chunks. Returns (bars, coverage)."""
    import yfinance as yf
    from scanner import _own_frame
    from symbols import to_yahoo

    spec = RANGES[name]
    out, skipped = {}, {"no_data": 0, "too_short": 0}
    chunks = [symbols[i:i + CHUNK] for i in range(0, len(symbols), CHUNK)]
    t0 = time.time()

    for n, chunk in enumerate(chunks, 1):
        tickers = [to_yahoo(s) for s in chunk]
        try:
            # group_by="column" so _own_frame finds the ticker at column level
            # -1, which is where the default layout puts it. Under "ticker" it
            # finds Open/High/Low/Close there instead and resolves nothing —
            # the same trap the position-grading batch documents.
            raw = yf.download(tickers, period=spec["period"],
                              interval=spec["interval"], group_by="column",
                              threads=True, progress=False, auto_adjust=True,
                              timeout=60)
        except Exception as e:                          # noqa: BLE001
            # LOUD. The first version swallowed every failure and reported only
            # "no_data", so a total, systematic refusal looked exactly like a
            # universe of illiquid names.
            _log(f"  {name}: chunk {n}/{len(chunks)} FAILED — {type(e).__name__}: {e}")
            skipped["no_data"] += len(chunk)
            continue

        for sym, t in zip(chunk, tickers):
            f = _own_frame(raw, t)
            if f is None or f.empty:
                skipped["no_data"] += 1
                continue
            rows = _rows(f)
            if len(rows) < spec["min_bars"]:
                skipped["too_short"] += 1
                continue
            out[sym] = rows

        if n % PROGRESS_EVERY == 0 or n == len(chunks):
            el = time.time() - t0
            done = min(n * CHUNK, len(symbols))
            _log(f"  {name}: {done}/{len(symbols)} in {el:.0f}s "
                 f"({len(out)} kept, {skipped['no_data']} no data, "
                 f"{skipped['too_short']} too short)")
        time.sleep(PACE)

    return out, {"asked": len(symbols), "got": len(out), **skipped}


def universe(limit: int | None) -> list:
    """The names to fetch, narrowable without editing this file.

    scan_research.py publishes a `scanned` count per engine precisely so a
    narrower run is a supported, visible outcome rather than a silent one. So
    the size is a knob: RESEARCH_UNIVERSE_LIMIT, or --limit on the command
    line. Neither is set by default and the full Nifty 500 is the default.
    """
    from signals.universe import load_nifty500
    syms = [s.replace(".NS", "") for s in load_nifty500()]
    if limit is None:
        try:
            limit = int(os.environ["RESEARCH_UNIVERSE_LIMIT"])
        except (KeyError, TypeError, ValueError):
            limit = None
    return syms[:limit] if limit else syms


def main() -> int:
    argv = sys.argv[1:]
    limit = None
    if "--limit" in argv:
        limit = int(argv[argv.index("--limit") + 1])
    wanted = [bars_cache.HOURLY]
    if "--hourly-only" not in argv:
        wanted.append(bars_cache.DAILY_3Y)

    syms = universe(limit)
    if not syms:
        _log("harvest: the universe came back empty — nothing to fetch, and "
             "writing an empty cache would look like a scan that found nothing")
        return 1
    _log(f"harvest: {len(syms)} symbols, ranges {wanted}")

    manifest = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "files": {}}
    started = time.time()
    for name in wanted:
        t0 = time.time()
        bars, cov = harvest(syms, name)
        if not bars:
            _log(f"harvest: {name} came back empty ({cov}) — NOT overwriting "
                 f"the cache with nothing")
            return 1
        p = bars_cache.save(name, bars)
        cov["seconds"] = round(time.time() - t0, 1)
        manifest["files"][name] = cov
        _log(f"harvest: {name} — {cov['got']}/{cov['asked']} symbols "
             f"({cov['no_data']} no data, {cov['too_short']} too short) "
             f"in {cov['seconds']}s → {p}")

    bars_cache.save("manifest.json", manifest)
    _log(f"harvest: done in {round(time.time() - started, 1)}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
