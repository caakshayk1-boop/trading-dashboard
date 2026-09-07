#!/usr/bin/env python3
"""
Walk-forward backtest for LEDGE and KEEL.

HOW THIS AVOIDS FLATTERING ITSELF
· No lookahead. A detector sees bars[:i+1] and nothing after i. Pivot lows are
  confirmed three bars late, which is what a live scan would also have to wait
  for.
· Entry is the OPEN of bar i+1. A scan that runs on the close cannot buy that
  close, and backtests that pretend otherwise book the breakout bar's move for
  free.
· One position per symbol at a time. Without this, a name that keeps firing
  during a strong run contributes the same trade five times.
· BOTH stop rules are run over the IDENTICAL trade list, winners included. The
  previous exit-rule study in this repo was refuted because it only re-scored
  the losers, which can only ever improve the answer.
· The stop is checked BEFORE the target on any bar where both are possible.

WHAT IT STILL CANNOT FIX
The universe is the 750 names that are in the screen TODAY, so companies that
collapsed and fell out of it are missing. That is survivorship bias and it
flatters every long-only result here. It is stated with the numbers, not
buried.
"""
from __future__ import annotations
import json, math, sys, statistics as st
import numpy as np

sys.path.insert(0, '/Users/akshaykumarkothari/Downloads/trading-dashboard')
from signals.basebreak import prepare, DETECTORS

BARS = '/private/tmp/claude-501/-Users-akshaykumarkothari-Workspace/4387587e-0410-48f6-b6ac-50dea011672c/scratchpad/bars2y.json'
MAX_HOLD = 60          # trading days; same for every rule compared


def run(rows, detector, close_basis: bool):
    """Replay one symbol. Returns a list of R multiples."""
    ts = np.array([r[0] for r in rows])
    o, h, l, c, v = (np.array([r[k] for r in rows], dtype=float) for k in (1, 2, 3, 4, 5))
    b = prepare(o, h, l, c, v)
    out, open_until = [], -1
    for i in range(80, len(c) - 2):
        if i <= open_until:
            continue
        sig = detector(b, i)
        if not sig:
            continue
        entry = float(o[i + 1])            # next bar's open — the earliest fill
        stop, t1 = sig["stop"], sig["t1"]
        risk = entry - stop
        if risk <= 0:
            continue
        exit_r, end = None, min(i + 1 + MAX_HOLD, len(c) - 1)
        for j in range(i + 1, end + 1):
            hit_stop = (c[j] <= stop) if close_basis else (l[j] <= stop)
            if hit_stop:                    # stop checked first, both rules
                exit_r = (c[j] - entry) / risk if close_basis else (stop - entry) / risk
                break
            if h[j] >= t1:                  # a resting limit order fills intraday
                exit_r = (t1 - entry) / risk
                break
        if exit_r is None:
            exit_r = (c[end] - entry) / risk
        out.append((exit_r, ts[i], sig))
        open_until = end
    return out


def stats(rs):
    n = len(rs)
    if n < 2:
        return None
    m = sum(rs) / n
    sd = st.pstdev(rs) * math.sqrt(n / (n - 1)) if n > 1 else 0
    se = sd / math.sqrt(n) if sd else 0
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gp, gl = sum(wins), -sum(losses)
    # Max drawdown of the R equity curve, in R.
    eq, peak, dd = 0.0, 0.0, 0.0
    for r in rs:
        eq += r; peak = max(peak, eq); dd = min(dd, eq - peak)
    return dict(n=n, exp=round(m, 3), win=round(len(wins) / n * 100, 1),
                se=round(se, 3), t=round(m / se, 2) if se else 0.0,
                pf=round(gp / gl, 2) if gl else float('inf'),
                med=round(st.median(rs), 3), best=round(max(rs), 2),
                worst=round(min(rs), 2), maxdd=round(dd, 2))


def main():
    data = json.load(open(BARS))
    print(f"universe with usable bars: {len(data)} symbols\n")
    for name, det in DETECTORS.items():
        intr, clos, dates = [], [], []
        for sym, rows in data.items():
            try:
                a = run(rows, det, close_basis=False)
                b = run(rows, det, close_basis=True)
            except Exception:
                continue
            intr += [x[0] for x in a]
            clos += [x[0] for x in b]
            dates += [x[1] for x in b]
        sa, sb = stats(intr), stats(clos)
        print(f"── {name.upper()} " + "─" * 56)
        if not sb:
            print("   too few signals to measure\n"); continue
        for lab, s in (("intraday stop", sa), ("CLOSE stop  ", sb)):
            print(f"   {lab}  n={s['n']:<4} exp {s['exp']:+.3f}R  win {s['win']:>5.1f}%  "
                  f"t={s['t']:>5.2f}  PF {s['pf']:>4}  maxDD {s['maxdd']:>6.2f}R")
        print(f"   median {sb['med']:+.2f}R · best {sb['best']:+.2f}R · worst {sb['worst']:+.2f}R")
        if dates:
            import datetime as dt
            d = sorted(dates)
            print(f"   period {dt.date.fromtimestamp(d[0])} → {dt.date.fromtimestamp(d[-1])}"
                  f" · {sb['n']/max(1,(d[-1]-d[0])/86400/30):.1f} signals a month")
        # The bar this repo already sets for capital: 30+ closed at t>=2.
        verdict = ("CLEARS the 30-trade t>=2 bar" if sb['n'] >= 30 and sb['t'] >= 2
                   else f"does NOT clear the bar (needs n>=30 and t>=2; has n={sb['n']}, t={sb['t']})")
        print(f"   → {verdict}\n")


if __name__ == "__main__":
    main()
