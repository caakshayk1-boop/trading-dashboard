#!/usr/bin/env python3
"""
Walk-forward backtest for BUOY, on hourly bars.

HOW THIS AVOIDS FLATTERING ITSELF
· No lookahead. The detector sees bars[:i+1]. The 200-day average carried onto
  each hour comes from the last COMPLETED day (test_buoy.py asserts this).
· Entry is the OPEN of bar i+1. A scan that runs on an hourly close cannot buy
  that close.
· One position per symbol at a time.
· The stop is checked BEFORE the target on any bar where both are possible.
· A trade still open at the end of the data is EXCLUDED, not marked flat —
  counting unresolved trades as 0R is how a losing engine reads as neutral.
· Survivorship bias is unfixable: the universe is the names in the screen
  today, so anything that collapsed and fell out is missing. Stated, not buried.
"""
from __future__ import annotations
import json, math, sys, statistics as st
import numpy as np

sys.path.insert(0, '/Users/akshaykumarkothari/Downloads/trading-dashboard')
from signals.buoy import prepare, attach_dma, buoy_signal

SCR = '/private/tmp/claude-501/-Users-akshaykumarkothari-Workspace/4387587e-0410-48f6-b6ac-50dea011672c/scratchpad'
HOURS = f'{SCR}/barsH.json'
DAYS  = f'{SCR}/barsD3y.json'
MAX_HOLD_H = 240          # ~40 sessions of 6 hourly bars


def replay(hrows, drows, max_hold=MAX_HOLD_H):
    ts = np.array([r[0] for r in hrows])
    o, h, l, c, v = (np.array([r[k] for r in hrows], dtype=float) for k in (1, 2, 3, 4, 5))
    dts = np.array([r[0] for r in drows])
    dc  = np.array([r[4] for r in drows], dtype=float)
    b = attach_dma(prepare(o, h, l, c, v, dts, dc), ts)

    out, i, n = [], 300, len(c)
    while i < n - 2:
        s = buoy_signal(b, i)
        if not s:
            i += 1
            continue
        entry = float(o[i + 1])               # the NEXT bar's open
        risk = entry - s["sl"]
        if risk <= 0:
            i += 1
            continue
        tgt = s["target2"]                    # measured to T2, the house middle rung
        r_mult, closed_at = None, None
        for j in range(i + 1, min(n, i + 1 + max_hold)):
            if l[j] <= s["sl"]:               # stop first, always
                r_mult, closed_at = -(entry - s["sl"]) / risk, j
                break
            if h[j] >= tgt:
                r_mult, closed_at = (tgt - entry) / risk, j
                break
        if r_mult is None:
            j = min(n - 1, i + max_hold)
            if j >= n - 1:
                i += 1                        # unresolved at the edge of the data
                continue
            r_mult, closed_at = (c[j] - entry) / risk, j   # time stop
        out.append({"r": float(r_mult), "i": i, "exit": closed_at,
                    "risk_pct": s["risk_pct"], "hold": closed_at - i,
                    "from_high": s["from_high_pct"]})
        i = closed_at + 1                     # one position per symbol at a time
    return out


def stats(rs):
    if not rs:
        return None
    n = len(rs)
    m = st.mean(rs)
    sd = st.pstdev(rs) if n < 2 else st.stdev(rs)
    t = m / (sd / math.sqrt(n)) if sd else float('nan')
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x <= 0]
    gp, gl = sum(wins), -sum(losses)
    cum, peak, dd = 0.0, 0.0, 0.0
    for x in rs:
        cum += x; peak = max(peak, cum); dd = min(dd, cum - peak)
    return {"n": n, "exp": m, "t": t, "win": len(wins) / n * 100,
            "pf": (gp / gl) if gl else float('inf'), "maxdd": dd,
            "best": max(rs), "worst": min(rs), "total": sum(rs)}


def main():
    H = json.load(open(HOURS))
    D = json.load(open(DAYS))
    syms = [s for s in H if s in D]
    print(f"symbols with both hourly and daily bars: {len(syms)}")

    rs, meta, per = [], [], {}
    for s in syms:
        try:
            t = replay(H[s], D[s])
        except Exception as e:
            print(f"  {s}: {e}")
            continue
        if t:
            per[s] = len(t)
            rs.extend(x["r"] for x in t)
            meta.extend(t)

    a = stats(rs)
    if not a:
        print("\nNo BUOY signal fired on this universe. That is a result: the "
              "rule is strict enough that a two-year hourly window over "
              f"{len(syms)} names produced nothing.")
        return
    print(f"\nBUOY — hourly 200-DMA reclaim + RSI divergence")
    print(f"  n        {a['n']}")
    print(f"  exp      {a['exp']:+.3f}R")
    print(f"  t        {a['t']:+.2f}")
    print(f"  win      {a['win']:.1f}%")
    print(f"  PF       {a['pf']:.2f}")
    print(f"  maxDD    {a['maxdd']:.2f}R")
    print(f"  best     {a['best']:+.2f}R   worst {a['worst']:+.2f}R")
    print(f"  total    {a['total']:+.1f}R over {len(per)} names")
    if meta:
        print(f"  median stop {st.median(m['risk_pct'] for m in meta):.1f}% of entry")
        print(f"  median hold {st.median(m['hold'] for m in meta):.0f} hours "
              f"(~{st.median(m['hold'] for m in meta)/6:.0f} sessions)")
        print(f"  median fall from 52w high at entry "
              f"{st.median(m['from_high'] for m in meta):.1f}%")
    print("\nSurvivorship bias: the universe is today's screen, so names that "
          "collapsed and left it are absent. This flatters every long-only result.")


if __name__ == "__main__":
    main()
