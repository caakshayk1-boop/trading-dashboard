#!/usr/bin/env python3
"""
scan_research.py — run every RESEARCH engine and publish each with its record.

WHY THESE ARE PUBLISHED AT ALL
None of them has an edge anyone has measured. BUOY sits on zero with a
confidence interval that rules out anything worth having; ANCHOR sits on zero
with a wide one; BEDROCK measured at or below zero in all six configurations
tried and reached its first target one time in eight.

They are published anyway, on instruction, and the way they are published is
the whole point: every watchlist row travels with the engine's own numbers, and
the page leads with them. An unproven engine shown WITH its null result is a
different object from an unproven engine shown as a list of tickers. This file
exists to make the first one impossible to turn into the second — the feed
cannot carry a name without also carrying that engine's status, sample size,
expectancy and t.

TWO SOURCES OF BARS
Live from Yahoo where the address is not rate-limited, and from harvested bars
where it is. The offline path is not a degraded mode to be hidden: the bars are
real, the rule is the same, and the feed says how many names it covered so a
narrower run is never mistaken for the full universe.
"""
from __future__ import annotations
import json, os, sys
from datetime import datetime, timezone

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import alert_log
from signals.buoy import (to_4h, prepare as buoy_prepare, buoy_signal,
                          BACKTEST as BUOY_BT, ENGINE_STATUS as BUOY_STATUS, MA_N)
from signals.bedrock import (prepare as rock_prepare, bedrock_signal, anchor_signal,
                             BACKTEST as ROCK_BT, ANCHOR_BACKTEST as ANCH_BT,
                             ENGINE_STATUS as ROCK_STATUS, LOOKBACK)
from scan_buoy import _reclaim_only, LOOKBACK_B

SCR = ('/private/tmp/claude-501/-Users-akshaykumarkothari-Workspace/'
       '4387587e-0410-48f6-b6ac-50dea011672c/scratchpad')
OUT = os.path.join(HERE, "docs", "research.json")
TOP_N = 10
DAILY_LOOKBACK = 10          # sessions a daily setup stays current for


# ── what each engine is, in one place so the feed and the page cannot drift ──
ENGINES = {
    "buoy": {
        "name": "BUOY",
        "hunts": "A fallen name closing a 4-hour candle back above its 200-period average, "
                 "with a bullish RSI divergence already behind it.",
        "timeframe": "4-hour candles",
        "status": BUOY_STATUS["buoy"],
        "backtest": BUOY_BT,
        "verdict": "No measured edge. At n=348 on the wider lane the 95% interval is "
                   "[-0.108, +0.128]; clearing this book's t>=2 needs about +0.13R, the very "
                   "top of it. If there is an edge it is smaller than the bar.",
    },
    "anchor": {
        "name": "ANCHOR",
        "hunts": "A reversal bar ON a 52-week floor that has held twice — bought at the "
                 "floor, with the stop just under it.",
        "timeframe": "daily",
        "status": ROCK_STATUS["anchor"],
        "backtest": ANCH_BT,
        "verdict": "No measured edge, but the smallest stop of the three (5.1% of entry, "
                   "1.92x ATR) and a 2.5R target genuinely reached a quarter of the time. "
                   "Sits on zero with a wide interval: not an edge, not refuted.",
    },
    "bedrock": {
        "name": "BEDROCK",
        "hunts": "A close out of a base built on a tested 52-week floor.",
        "timeframe": "daily",
        "status": ROCK_STATUS["bedrock"],
        # NORMALISED. BEDROCK's own dict records the BEST of six configurations
        # under best_n/best_exp/best_t, because there was no single headline
        # worth calling "the" result. The page reads n/exp/t like every other
        # engine, so publishing it raw left the one engine whose numbers matter
        # most showing four em-dashes.
        "backtest": {**ROCK_BT,
                     "n": ROCK_BT.get("best_n"), "exp": ROCK_BT.get("best_exp"),
                     "t": ROCK_BT.get("best_t"), "win": ROCK_BT.get("best_win")},
        "verdict": "REJECTED on measurement. All six configurations tried came out at or "
                   "below zero, and the FIRST rung — 1.6R — was reached by between 0% and "
                   "12.5% of trades. Published so the evidence is visible, not because it "
                   "should be traded.",
    },
}


def _last_valid(sig_fn, bars, n, lookback, still_ok):
    """The most recent firing inside the window that has not been invalidated.

    A WATCHLIST IS NOT AN ALERT. Scanning only the newest bar asks "did this
    fire in the last session", and for rules this strict the answer is almost
    always no — a list built that way is blank essentially every run, which is
    truthful and useless. So it looks back, and keeps the name only while the
    setup still stands. The levels published are the ones from the bar that
    fired, never re-priced to today.
    """
    for k in range(n - 1, max(0, n - 1 - lookback), -1):
        s = sig_fn(bars, k)
        if s and still_ok(bars, k, n - 1):
            s["bars_ago"] = int(n - 1 - k)
            s["_i"] = k
            return s
    return None


def scan_offline():
    H = json.load(open(f"{SCR}/barsH.json"))
    D = json.load(open(f"{SCR}/barsD3y.json"))
    hits = {k: [] for k in ENGINES}

    # ── BUOY, on 4H resampled from hourly ────────────────────────────────────
    for sym, rows in H.items():
        r4 = to_4h(rows)
        if len(r4) < MA_N + 40:
            continue
        ts = np.array([r[0] for r in r4])
        o, h, l, c, v = (np.array([r[i] for r in r4], dtype=float) for i in (1, 2, 3, 4, 5))
        b = buoy_prepare(o, h, l, c, v)
        n = len(c)
        above = lambda bb, k, last: bb["close"][last] > bb["ma"][last]
        for lane, fn in (("strict", buoy_signal), ("reclaim", _reclaim_only)):
            s = _last_valid(fn, b, n, LOOKBACK_B, above)
            if not s:
                continue
            s.update(symbol=sym, lane=lane, engine="buoy",
                     at=datetime.fromtimestamp(int(ts[s["_i"]]), timezone.utc).isoformat(),
                     last=round(float(c[-1]), 2),
                     since_pct=round((c[-1] - s["entry"]) / s["entry"] * 100, 2))
            s.pop("_i", None)
            hits["buoy"].append(s)
            break                                  # strict wins if both fire

    # ── ANCHOR and BEDROCK, on daily ─────────────────────────────────────────
    for sym, rows in D.items():
        if len(rows) < LOOKBACK + 40:
            continue
        ts = np.array([r[0] for r in rows])
        o, h, l, c, v = (np.array([r[i] for r in rows], dtype=float) for i in (1, 2, 3, 4, 5))
        b = rock_prepare(o, h, l, c, v)
        n = len(c)
        # A floor setup is dead the moment price closes under the stop it was
        # given. Nothing else invalidates it: drifting sideways on the floor is
        # what these engines expect.
        for eng, fn in (("anchor", anchor_signal), ("bedrock", bedrock_signal)):
            s = _last_valid(fn, b, n, DAILY_LOOKBACK,
                            lambda bb, k, last, f=fn: True)
            if not s:
                continue
            if float(c[-1]) <= s["sl"]:
                continue                            # stop already taken out
            s.update(symbol=sym, lane=None, engine=eng,
                     at=datetime.fromtimestamp(int(ts[s["_i"]]), timezone.utc).isoformat(),
                     last=round(float(c[-1]), 2),
                     since_pct=round((c[-1] - s["entry"]) / s["entry"] * 100, 2))
            s.pop("_i", None)
            hits[eng].append(s)

    return hits, {"buoy": len(H), "anchor": len(D), "bedrock": len(D)}


def main():
    hits, scanned = scan_offline()
    now = datetime.now(timezone.utc)

    engines = []
    for key, meta in ENGINES.items():
        rows = sorted(hits[key], key=lambda x: -(x.get("from_high_pct")
                                                 or x.get("above_floor_pct") or 0))
        added, dup = alert_log.record(rows, key, now=now)
        engines.append({
            "key": key, **{k: v for k, v in meta.items()},
            "scanned": scanned[key], "fired": len(rows), "top": rows[:TOP_N],
            "new_alerts": len(added), "already_logged": dup,
        })
        print(f"  {meta['name']:<9} {meta['status']:<9} scanned {scanned[key]:>4}  "
              f"fired {len(rows):>3}  ({len(added)} new to the log, {dup} already there)",
              flush=True)

    out = {
        "ok": True,
        "generated_at": now.isoformat(timespec="seconds"),
        "source": "harvested bars (offline run)",
        "coverage_note": ("This run covered the names whose bars were already harvested. "
                          "Yahoo rate-limits by IP and had throttled this address; the "
                          "scheduled job runs the full 750."),
        "engines": engines,
        "disclaimer": ("Every engine here is RESEARCH or REJECTED. None is cleared for "
                       "capital, none carries a position, and each row is published with "
                       "the engine's own measured record beside it. A watchlist is not a "
                       "recommendation and an unproven engine is not a signal."),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(out, open(OUT, "w"), indent=1)
    print(f"  wrote {OUT}", flush=True)


if __name__ == "__main__":
    main()
