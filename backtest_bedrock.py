#!/usr/bin/env python3
"""
Walk-forward backtest for BEDROCK, on daily bars.

Same discipline as the others: no lookahead, entry at the OPEN of bar i+1, one
position per symbol at a time, stop checked BEFORE target on any bar where both
are possible, and a trade still open at the end of the data is EXCLUDED rather
than booked flat.

IT ALSO MEASURES REACH. For a bottom-fishing engine the temptation is a huge
target justified by the geometry — a small stop under a floor and the whole
recovery above it. So each rung reports the share of trades whose best move got
there, and the 200-day average is measured the same way. A target reached by
five percent of trades is decoration whatever its R multiple says.
"""
from __future__ import annotations
import json, math, sys, statistics as st
import numpy as np

sys.path.insert(0, '/Users/akshaykumarkothari/Downloads/trading-dashboard')
from signals.bedrock import prepare, bedrock_signal, TARGET_R

SCR = ('/private/tmp/claude-501/-Users-akshaykumarkothari-Workspace/'
       '4387587e-0410-48f6-b6ac-50dea011672c/scratchpad')
BARS = f'{SCR}/barsD3y.json'
MAX_HOLD = 60          # trading days


def replay(rows, target_r=2.5, max_hold=MAX_HOLD):
    o, h, l, c, v = (np.array([r[k] for r in rows], dtype=float) for k in (1, 2, 3, 4, 5))
    b = prepare(o, h, l, c, v)
    n = len(c)
    out, i = [], 260
    while i < n - 2:
        s = bedrock_signal(b, i)
        if not s:
            i += 1
            continue
        entry = float(o[i + 1])
        risk = entry - s["sl"]
        if risk <= 0:
            i += 1
            continue
        tgt = entry + target_r * risk
        rm = ex = None
        mfe = 0.0
        for j in range(i + 1, min(n, i + 1 + max_hold)):
            mfe = max(mfe, (h[j] - entry) / risk)
            if l[j] <= s["sl"]:
                rm, ex = -1.0, j
                break
            if h[j] >= tgt:
                rm, ex = target_r, j
                break
        if rm is None:
            j = min(n - 1, i + max_hold)
            if j >= n - 1:
                i += 1
                continue
            rm, ex = (c[j] - entry) / risk, j
        out.append({"r": float(rm), "mfe": float(mfe), "hold": ex - i,
                    "risk_pct": s["risk_pct"], "touches": s["touches"],
                    "above_floor": s["above_floor_pct"], "stretch": s["stretch_r"]})
        i = ex + 1
    return out


def stats(rs):
    n = len(rs)
    if not n:
        return None
    m = st.mean(rs); sd = st.stdev(rs) if n > 1 else 0
    se = sd / math.sqrt(n) if sd else float('nan')
    t = m / se if se else float('nan')
    wins = [x for x in rs if x > 0]; losses = [x for x in rs if x <= 0]
    gp, gl = sum(wins), -sum(losses)
    cum = peak = dd = 0.0
    for x in rs:
        cum += x; peak = max(peak, cum); dd = min(dd, cum - peak)
    return {"n": n, "exp": m, "t": t, "win": len(wins) / n * 100,
            "pf": gp / gl if gl else float('inf'), "maxdd": dd,
            "lo": m - 1.96 * se, "hi": m + 1.96 * se,
            "best": max(rs), "worst": min(rs)}


def main():
    B = json.load(open(BARS))
    print(f"BEDROCK — daily bars, {len(B)} names\n")

    meta = []
    for s, rows in B.items():
        try:
            meta += replay(rows)
        except Exception as e:
            print(f"  {s}: {e}")
    if not meta:
        print("No BEDROCK signal fired. That is a result: three separate tests of a "
              "52-week floor, a base on it, and a volume break out of that base is a "
              "narrow thing to ask for.")
        return

    a = stats([m["r"] for m in meta])
    print(f"  n        {a['n']}")
    print(f"  exp      {a['exp']:+.3f}R   95% CI [{a['lo']:+.3f}, {a['hi']:+.3f}]")
    print(f"  t        {a['t']:+.2f}")
    print(f"  win      {a['win']:.1f}%")
    print(f"  PF       {a['pf']:.2f}")
    print(f"  maxDD    {a['maxdd']:.2f}R")
    print(f"  best {a['best']:+.2f}R   worst {a['worst']:+.2f}R")
    print(f"\n  median stop        {st.median(m['risk_pct'] for m in meta):.1f}% of entry")
    print(f"  median hold        {st.median(m['hold'] for m in meta):.0f} sessions")
    print(f"  median touches     {st.median(m['touches'] for m in meta):.0f}")
    print(f"  median entry above the floor  {st.median(m['above_floor'] for m in meta):.1f}%")

    mfes = [m["mfe"] for m in meta]
    print("\n  REACH — share of trades whose best move got this far:")
    for r_ in (1.0, 1.6, 2.5, 3.3, 5.0, 8.0):
        hit = sum(1 for x in mfes if x >= r_) / len(mfes) * 100
        print(f"    {r_:>4.1f}R  {hit:5.1f}%")
    st_r = [m["stretch"] for m in meta if m["stretch"] is not None]
    if st_r:
        med = st.median(st_r)
        got = sum(1 for m in meta if m["stretch"] is not None and m["mfe"] >= m["stretch"])
        print(f"\n  the 200-day average sits a median {med:.1f}R away, and was reached by "
              f"{got / len(st_r) * 100:.1f}% of trades")

    print("\n  target sweep (same trades, different exit):")
    for tr in (1.6, 2.5, 3.3, 5.0):
        rs = []
        for s, rows in B.items():
            rs += [m["r"] for m in replay(rows, target_r=tr)]
        b = stats(rs)
        if b:
            print(f"    T={tr:>4.1f}R  n={b['n']:>4}  exp={b['exp']:+.3f}R  t={b['t']:+5.2f}  "
                  f"win={b['win']:4.1f}%  CI [{b['lo']:+.3f}, {b['hi']:+.3f}]")

    print("\nSurvivorship bias bites hardest here: the universe is today's screen, so the "
          "names whose floor gave way and which fell out of it are missing entirely.")


if __name__ == "__main__":
    main()
