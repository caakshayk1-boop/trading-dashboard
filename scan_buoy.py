#!/usr/bin/env python3
"""
scan_buoy.py — run BUOY over the 750-name screen and write the top 10.

WHY THIS PUBLISHES A WATCHLIST AND NOT SIGNALS
BUOY measured +0.074R at t=+0.23 over 18 backtested trades, and the rule
without its divergence filter measured +0.065R at t=+0.77 over 217. Neither is
distinguishable from zero, and this book does not give an engine capital until
it has 30 closed trades at t>=2. So this writes a RANKED WATCHLIST with the
levels attached, it does not file trades into the ledger, and every surface
that renders it has to carry the sample size.

ONE REQUEST PER NAME, NOT TWO — and why that is not a shortcut
=============================================================
The first version fetched hourly bars AND two years of daily bars per symbol,
to compute the 200-DAY average itself. 1,500 requests at a 0.7s pace: it ran
for 46 minutes and used ten seconds of CPU. Every second of that was waiting.

screen.json already carries `sma200` for 741 of the 750 names. It is the same
200-day average, computed by the daily build that produces the screen, so
fetching daily bars to recompute it was work done twice.

THE COST, STATED: that average is stamped at the screen's `price_date`, which
on a Monday is Friday's close. A 200-day mean moves by roughly one two-hundredth
of the difference between the newest close and the one dropping out — a
fraction of a percent per day — so a few days of staleness cannot flip "price
is above its 200-day average" except for a name sitting exactly on it. Those
are the names this engine is about, so the drift is written into the feed as
`dma_as_of` and every surface has to show it. It is a real imprecision, named,
not an assumption hidden behind a faster number.
"""
from __future__ import annotations
import json, os, sys, time, threading, queue
import urllib.request, urllib.error
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from signals.basebreak import rsi, atr, swing_lows, _liquid, FROM_HIGH_MIN, ATR_FLOOR_MULT
import alert_log
import bars_cache
from signals.buoy import (BACKTEST, ENGINE_STATUS, BELOW_MIN_BARS, RECLAIM_MAX_ATR,
                          VOL_MULT, STOP_ATR_MULT, TARGET_R, MA_N,
                          DIV_MIN_GAP_B, DIV_MAX_GAP_B, RSI_DIV_MIN_LIFT,
                          to_4h, prepare, buoy_signal)

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
HERE = os.path.dirname(os.path.abspath(__file__))
SCREEN = os.path.expanduser("~/Workspace/Apps/Websites/signal/public/screen.json")
OUT = os.path.join(HERE, "docs", "buoy.json")
TOP_N = 10
# How far back a reclaim still counts as current for a WATCHLIST. 60 hourly
# bars is about ten trading sessions.
LOOKBACK_B = 60    # 4H candles; ~30 sessions

# Three workers over Yahoo's two hosts. FOUR-way concurrency has already earned
# this address a ten-minute 429; three, alternating query1/query2, has not.
WORKERS = 3
PACE = 0.25


def _chart(sym: str, host: str):
    url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}.NS"
           f"?interval=1h&range=60d")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        q = json.load(r)["chart"]["result"][0]
    k, ts = q["indicators"]["quote"][0], q.get("timestamp") or []
    rows = []
    for i in range(len(ts)):
        o, h, l, c, v = k["open"][i], k["high"][i], k["low"][i], k["close"][i], k["volume"][i]
        if None in (o, h, l, c, v):
            continue
        rows.append((ts[i], o, h, l, c, v))
    return rows


def detect(sym, rows, dma_val):
    """BUOY on the most recent completed hourly bar, against the screen's
    200-day average. A scan is a question about NOW: re-reporting a cross from
    three days ago as today's is the stale-as-live fault this repo has already
    shipped once."""
    if len(rows) < 320 or not dma_val:
        return None
    ts = np.array([r[0] for r in rows])
    o, h, l, c, v = (np.array([r[i] for r in rows], dtype=float) for i in (1, 2, 3, 4, 5))
    a, r_ = atr(h, l, c, 14), rsi(c, 14)
    dma = float(dma_val)
    n = len(c)
    if c[n - 1] <= dma:
        return None                     # invalidated: back under the line
    hit = None
    for i in range(n - 1, max(300, n - 1 - LOOKBACK_B), -1):
        if np.isfinite(a[i]) and np.isfinite(r_[i]) and a[i] > 0 \
           and c[i] > dma and c[i - 1] <= dma:
            hit = i
            break
    if hit is None:
        return None
    i = hit
    if not (np.isfinite(a[i]) and np.isfinite(r_[i]) and a[i] > 0):
        return None
    ok, turn = _liquid(c, v, i, window=20)
    if not ok:
        return None
    hi = float(c.max())
    if not hi or (hi - c[i]) / hi < FROM_HIGH_MIN:
        return None
    # The average is one number here, not a series, so "was below" is measured
    # against that same number rather than against its own history.
    if not np.all(c[i - BELOW_MIN_BARS:i] <= dma):
        return None
    if (c[i] - dma) > RECLAIM_MAX_ATR * a[i]:
        return None
    if v[i] < VOL_MULT * float(np.mean(v[max(0, i - 20):i])):
        return None
    piv = [p for p in swing_lows(l[:i + 1]) if p <= i - 3]
    if len(piv) < 2:
        return None
    lo2 = piv[-1]
    cand = [p for p in piv[:-1] if DIV_MIN_GAP_B <= (lo2 - p) <= DIV_MAX_GAP_B]
    if not cand:
        return None
    lo1 = cand[-1]
    if not (l[lo2] < l[lo1]):
        return None
    if not (np.isfinite(r_[lo1]) and np.isfinite(r_[lo2])
            and r_[lo2] > r_[lo1] + RSI_DIV_MIN_LIFT):
        return None
    entry = float(c[i])
    struct = float(l[lo2])
    stop = min(struct, entry - ATR_FLOOR_MULT * STOP_ATR_MULT * float(a[i]))
    risk = entry - stop
    if risk <= 0 or risk / entry > 0.25:
        return None
    return {"symbol": sym, "entry": round(entry, 2), "sl": round(stop, 2),
            "target1": round(entry + TARGET_R[0] * risk, 2),
            "target2": round(entry + TARGET_R[1] * risk, 2),
            "target3": round(entry + TARGET_R[2] * risk, 2),
            "rr": TARGET_R[0], "dma": round(dma, 2),
            "from_high_pct": round((hi - entry) / hi * 100, 2),
            "rsi": round(float(r_[i]), 1),
            "rsi_lift": round(float(r_[lo2] - r_[lo1]), 1),
            "turnover_cr": round(turn, 2), "struct_low": round(struct, 2),
            "risk_pct": round(risk / entry * 100, 2),
            "bars_ago": int(n - 1 - i), "last": round(float(c[n - 1]), 2),
            "since_pct": round((c[n - 1] - entry) / entry * 100, 2),
            "at": datetime.fromtimestamp(int(ts[i]), timezone.utc).isoformat()}



def _reclaim_only(b, i):
    """BUOY without the RSI-divergence filter — the variant that measured
    +0.010R at t=+0.16 over 348 trades, against the strict rule's +0.012R at
    t=+0.06 over 36. Same gates otherwise; the stop falls back to the lowest
    low of the last 30 candles because there is no divergence low to sit under."""
    import numpy as _np
    h, l, c, v = b["high"], b["low"], b["close"], b["volume"]
    r_, a, ma = b["rsi14"], b["atr14"], b["ma"]
    if i < MA_N + 30:
        return None
    if not (_np.isfinite(ma[i]) and _np.isfinite(a[i]) and _np.isfinite(r_[i]) and a[i] > 0):
        return None
    ok, turn = _liquid(c, v, i, window=20)
    if not ok:
        return None
    hi = float(c[max(0, i - 500):i + 1].max())
    if not hi or (hi - c[i]) / hi < FROM_HIGH_MIN:
        return None
    if not (c[i] > ma[i] and c[i - 1] <= ma[i - 1]):
        return None
    prev, pm = c[i - BELOW_MIN_BARS:i], ma[i - BELOW_MIN_BARS:i]
    if len(prev) < BELOW_MIN_BARS or not _np.all(_np.isfinite(pm)) or not _np.all(prev <= pm):
        return None
    if (c[i] - ma[i]) > RECLAIM_MAX_ATR * a[i]:
        return None
    if v[i] < VOL_MULT * float(_np.mean(v[max(0, i - 20):i])):
        return None
    entry = float(c[i])
    struct = float(l[max(0, i - 30):i + 1].min())
    stop = min(struct, entry - ATR_FLOOR_MULT * STOP_ATR_MULT * float(a[i]))
    risk = entry - stop
    if risk <= 0 or risk / entry > 0.25:
        return None
    return {"entry": round(entry, 2), "sl": round(stop, 2),
            "target1": round(entry + TARGET_R[0] * risk, 2),
            "target2": round(entry + TARGET_R[1] * risk, 2),
            "target3": round(entry + TARGET_R[2] * risk, 2),
            "rr": TARGET_R[0], "ma": round(float(ma[i]), 2),
            "from_high_pct": round((hi - entry) / hi * 100, 2),
            "rsi": round(float(r_[i]), 1), "rsi_lift": None,
            "turnover_cr": round(turn, 2), "struct_low": round(struct, 2),
            "risk_pct": round(risk / entry * 100, 2)}


def from_cache():
    """Run the detector over hourly bars already on disk, resampled to 4H.
    No network at all.

    WHY THIS MODE EXISTS: Yahoo rate-limits by IP, and this address earned a
    429 on every host after a long scan. A throttle is not a reason to publish
    nothing — the harvested bars are real bars, and running the rule over them
    produces a real, if narrower, answer. The feed says how many names it
    covered so the coverage is never mistaken for the full universe.
    """
    H = bars_cache.load(bars_cache.HOURLY)
    hits = []
    for s, hr in H.items():
        rows4 = to_4h(hr)
        if len(rows4) < MA_N + 40:
            continue
        ts = np.array([r[0] for r in rows4])
        o, h, l, c, v = (np.array([r[i] for r in rows4], dtype=float) for i in (1, 2, 3, 4, 5))
        b = prepare(o, h, l, c, v)
        n = len(c)
        # ── A WATCHLIST IS NOT AN ALERT ─────────────────────────────────────
        # Scanning only the newest candle asks "did this cross in the last four
        # hours", and the answer is almost always no: the strict rule fired 36
        # times in two years across 188 names. A top-ten list built that way is
        # blank essentially every scan, which is truthful and useless. So it
        # looks back LOOKBACK_B candles and keeps the name only if it is STILL
        # above the line. The rule is untouched — the entry and stop published
        # are the ones from the candle that crossed.
        for lane in ("strict", "reclaim"):
            found = at_i = None
            for k in range(n - 1, max(MA_N + 40, n - 1 - LOOKBACK_B), -1):
                sg = buoy_signal(b, k) if lane == "strict" else _reclaim_only(b, k)
                if sg:
                    found, at_i = sg, k
                    break
            if not found or not (c[-1] > b["ma"][-1]):
                continue
            found["symbol"] = s
            found["lane"] = lane
            found["at"] = datetime.fromtimestamp(int(ts[at_i]), timezone.utc).isoformat()
            found["bars_ago"] = int(n - 1 - at_i)
            found["last"] = round(float(c[-1]), 2)
            found["since_pct"] = round((c[-1] - found["entry"]) / found["entry"] * 100, 2)
            hits.append(found)
            break                                   # strict wins if both fire
    return hits, len(H)


def main():
    if "--from-cache" in sys.argv:
        hits, seen = from_cache()
        hits.sort(key=lambda x: -x["from_high_pct"])
        top = hits[:TOP_N]
        prev = {}
        if os.path.exists(OUT):
            try:
                prev = json.load(open(OUT))
            except Exception:
                prev = {}
        history = (prev.get("history") or [])
        history.append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "scanned": seen, "fired": len(hits),
                        "symbols": [x["symbol"] for x in top]})
        out = {"ok": True, "engine": "buoy", "status": ENGINE_STATUS["buoy"],
               "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "universe": seen, "scanned": seen, "fired": len(hits),
               "errors": 0, "throttled": 0,
               "source": "harvested hourly bars (offline run)",
               # Read off the harvest manifest, not asserted here. See
               # bars_cache.coverage_note for why this stopped being a constant.
               "coverage_note": bars_cache.coverage_note(bars_cache.HOURLY),
               "timeframe": "4H candles (09:15-13:15, 13:15-15:30 IST)",
               "took_secs": 0, "top": top, "backtest": BACKTEST,
               "history": history[-60:],
               "disclaimer": ("BUOY is RESEARCH. It measured +0.074R at t=+0.23 over 18 "
                              "backtested trades; without its RSI-divergence filter, +0.065R "
                              "at t=+0.77 over 217. Neither is distinguishable from zero. "
                              "This is a watchlist being run forward to grow a sample — it "
                              "is not a signal and carries no capital.")}
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        json.dump(out, open(OUT, "w"), indent=1)
        # The DURABLE record. buoy.json is a snapshot and is overwritten every
        # run; this appends only what has not been alerted recently, so the
        # forward sample counts setups rather than counting cron ticks.
        added, dup = alert_log.record(hits, "buoy")
        print(f"  offline run: {seen} names, {len(hits)} fired "
              f"({len(added)} new to the log, {dup} already there)", flush=True)
        for x in top:
            print(f"    {x['symbol']:<12} entry {x['entry']:>9.2f}  stop {x['sl']:>9.2f}  "
                  f"{x['from_high_pct']:>5.1f}% off high  RSI {x['rsi']:.0f}", flush=True)
        print(f"  wrote {OUT}", flush=True)
        return

    scr = json.load(open(SCREEN))
    rows = scr["rows"]
    dma_of = {r["sym"]: r.get("sma200") for r in rows if r.get("sym")}
    universe = [r["sym"] for r in rows if r.get("sym") and r.get("sma200")]
    print(f"scanning {len(universe)} names "
          f"({len(rows) - len(universe)} have no 200-day average on the screen)",
          flush=True)

    q = queue.Queue()
    for n, s in enumerate(universe):
        q.put((n, s))
    hits, lock = [], threading.Lock()
    counts = {"seen": 0, "err": 0, "thr": 0}
    t0 = time.time()

    def worker(wid):
        while True:
            try:
                n, sym = q.get_nowait()
            except queue.Empty:
                return
            host = "query2" if n % 2 else "query1"
            got = None
            for attempt in range(3):
                try:
                    got = _chart(sym, host)
                    break
                except urllib.error.HTTPError as e:
                    if e.code in (429, 999):
                        with lock:
                            counts["thr"] += 1
                        time.sleep(4 * (attempt + 1))
                        continue
                    break
                except Exception:
                    time.sleep(0.6 * (attempt + 1))
            with lock:
                if got is None:
                    counts["err"] += 1
                else:
                    counts["seen"] += 1
                    try:
                        s = detect(sym, got, dma_of.get(sym))
                        if s:
                            hits.append(s)
                    except Exception:
                        counts["err"] += 1
                done = counts["seen"] + counts["err"]
                if done % 100 == 0:
                    print(f"  {done}/{len(universe)}  fired={len(hits)} "
                          f"err={counts['err']} thr={counts['thr']} "
                          f"{time.time()-t0:.0f}s", flush=True)
            time.sleep(PACE)

    ts_ = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(WORKERS)]
    [t.start() for t in ts_]
    [t.join() for t in ts_]

    # Ranked by how far it fell before reclaiming — the deeper the fall, the
    # more the 200-day line actually meant. Not by a "score": there isn't one,
    # and inventing a composite here would be a model nobody measured.
    hits.sort(key=lambda x: -x["from_high_pct"])
    top = hits[:TOP_N]

    prev = {}
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT))
        except Exception:
            prev = {}
    history = (prev.get("history") or [])
    history.append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "scanned": counts["seen"], "fired": len(hits),
                    "symbols": [h["symbol"] for h in top]})
    history = history[-60:]

    out = {"ok": True, "engine": "buoy", "status": ENGINE_STATUS["buoy"],
           "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "universe": len(universe), "scanned": counts["seen"], "fired": len(hits),
           "errors": counts["err"], "throttled": counts["thr"],
           "dma_as_of": scr.get("price_date"),
           "dma_note": ("The 200-day average is taken from the screen's own daily build, "
                        "stamped price_date. A 200-day mean drifts a fraction of a percent "
                        "a day, so a few days of staleness matters only for a name sitting "
                        "exactly on the line."),
           "took_secs": round(time.time() - t0, 1),
           "top": top, "backtest": BACKTEST, "history": history,
           "disclaimer": ("BUOY is RESEARCH. It measured +0.074R at t=+0.23 over 18 "
                          "backtested trades; without its RSI-divergence filter, +0.065R "
                          "at t=+0.77 over 217. Neither is distinguishable from zero. This "
                          "is a watchlist being run forward to grow a sample — it is not a "
                          "signal and carries no capital.")}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(out, open(OUT, "w"), indent=1)
    added, dup = alert_log.record(hits, "buoy")
    print(f"\n  logged {len(added)} new alerts, {dup} were already recorded", flush=True)
    print(f"  scanned {counts['seen']}/{len(universe)}  fired {len(hits)}  "
          f"errors {counts['err']}  throttled {counts['thr']}  "
          f"in {time.time()-t0:.0f}s", flush=True)
    for h in top:
        print(f"    {h['symbol']:<12} entry {h['entry']:>9.2f}  stop {h['sl']:>9.2f}  "
              f"{h['from_high_pct']:>5.1f}% off high  RSI {h['rsi']:.0f}", flush=True)
    print(f"  wrote {OUT}", flush=True)


if __name__ == "__main__":
    main()
