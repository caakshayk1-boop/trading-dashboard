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

PACED THE WAY THE COMMENTS SAY TO PACE IT
-----------------------------------------
scan_buoy.py earned this address a ten-minute 429 at four-way concurrency and
has not at three, alternating Yahoo's two hosts with a quarter-second between
requests. That is measured, not guessed, so it is reused verbatim rather than
re-tuned. Two ranges per symbol doubles the request count, so the pacing
matters more here than it did there, not less.

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

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from queue import Queue

import bars_cache

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}

# Lifted from scan_buoy.py, where they are the measured safe values. Three
# workers over two hosts with a quarter second of pace; four earned a 429.
WORKERS = 3
PACE = 0.25
HOSTS = ("query1", "query2")
TIMEOUT = 20
RETRIES = 2

# What each engine needs. BUOY reads 4H candles resampled from hourly, so 60
# days of hourly is ~4 months of 4H — enough for its 200-period average plus
# the lookback. ANCHOR and BEDROCK read daily over a long base.
RANGES = {
    bars_cache.HOURLY:   {"interval": "1h", "range": "60d",  "min_bars": 320},
    bars_cache.DAILY_3Y: {"interval": "1d", "range": "3y",   "min_bars": 200},
}

_print_lock = threading.Lock()


def _log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def _chart(sym: str, host: str, interval: str, rng: str) -> list:
    """One Yahoo chart call, as [ts, o, h, l, c, v] rows with gaps dropped.

    A bar with a None in any of OHLCV is dropped rather than carried: every
    reader of this file does arithmetic on all five, and None propagates into
    a nan that compares False against every threshold without raising — the
    silent-pass failure this codebase has already paid for once.
    """
    url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}.NS"
           f"?interval={interval}&range={rng}")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        q = json.load(r)["chart"]["result"][0]
    k, ts = q["indicators"]["quote"][0], q.get("timestamp") or []
    rows = []
    for i in range(len(ts)):
        o, h, l, c, v = (k["open"][i], k["high"][i], k["low"][i],
                         k["close"][i], k["volume"][i])
        if None in (o, h, l, c, v):
            continue
        rows.append((ts[i], o, h, l, c, v))
    return rows


def _fetch(sym: str, worker: int, interval: str, rng: str) -> list | None:
    """One symbol, with the hosts alternated and a bounded retry.

    Returns None on any failure. A missing symbol narrows the run; it must not
    end it, and it must not be silently indistinguishable from a symbol that
    genuinely has no setup.
    """
    for attempt in range(RETRIES + 1):
        host = HOSTS[(worker + attempt) % len(HOSTS)]
        try:
            return _chart(sym, host, interval, rng)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None                      # delisted or renamed; not a retry
            if e.code == 429:
                # Backing off rather than hammering: the whole point of the
                # pacing above is to not be here.
                time.sleep(2.0 * (attempt + 1))
            else:
                time.sleep(0.5)
        except Exception:                                    # noqa: BLE001
            time.sleep(0.5)
    return None


def harvest(symbols: list, name: str) -> tuple[dict, dict]:
    """Fetch one range for every symbol. Returns (bars, coverage)."""
    spec = RANGES[name]
    out, skipped = {}, {"no_data": 0, "too_short": 0}
    q: Queue = Queue()
    for s in symbols:
        q.put(s)
    lock = threading.Lock()

    def work(worker: int):
        while True:
            try:
                sym = q.get_nowait()
            except Exception:                                # noqa: BLE001
                return
            rows = _fetch(sym, worker, spec["interval"], spec["range"])
            with lock:
                if rows is None:
                    skipped["no_data"] += 1
                elif len(rows) < spec["min_bars"]:
                    skipped["too_short"] += 1
                else:
                    out[sym] = rows
            time.sleep(PACE)
            q.task_done()

    threads = [threading.Thread(target=work, args=(i,), daemon=True)
               for i in range(WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    cov = {"asked": len(symbols), "got": len(out), **skipped}
    return out, cov


def universe(limit: int | None) -> list:
    from signals.universe import load_nifty500
    syms = [s.replace(".NS", "") for s in load_nifty500()]
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
