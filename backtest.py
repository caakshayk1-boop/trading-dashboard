#!/usr/bin/env python3
"""
backtest.py — replay cf_engine over history and measure whether it has an edge.

Runs the IDENTICAL evaluate() the live scanner calls, bar by bar, so results
describe the deployed logic rather than a reimplementation of it.

    python backtest.py                     # default config, all symbols
    python backtest.py --symbols GOLD,SILVER
    python backtest.py --sweep min_rr=1.5,1.8,2.2 --sweep min_sl_atr=1.0,1.5,2.0

Exit outcomes per signal, resolved on 1H bars after entry:
    T2 hit before SL  -> win  (+rr R)
    SL hit before T2  -> loss (-1 R)
    neither by horizon -> open, marked flat and excluded from win rate

Look-ahead is avoided by slicing every frame to [:i] before calling evaluate,
and by resolving outcomes only on bars strictly after the signal bar. Where
SL and T2 fall inside the same 1H bar the loss is taken — the pessimistic
assumption, since intrabar order is unknowable from OHLC.
"""

import argparse, itertools, sys
from dataclasses import replace

import pandas as pd
import yfinance as yf

from cf_engine import Config, CONFIG, evaluate, REJECTS
from symbols import to_yahoo

SYMBOLS = ["GOLD", "SILVER", "CRUDE", "NATGAS"]
HORIZON = 48          # 1H bars allowed for a trade to resolve (~2 sessions)
WARMUP  = 120         # bars before the first evaluation


def load(symbol: str):
    """Fetch the three frames evaluate() needs. Returns None if data is short."""
    t = to_yahoo(symbol)
    d1h = yf.download(t, period="730d", interval="1h", progress=False, auto_adjust=True)
    d4h = yf.download(t, period="730d", interval="4h", progress=False, auto_adjust=True)
    d1d = yf.download(t, period="730d", interval="1d", progress=False, auto_adjust=True)
    if any(d is None or len(d) < WARMUP for d in (d1h, d4h, d1d)):
        return None
    for d in (d1h, d4h, d1d):
        d.index = pd.to_datetime(d.index, utc=True)
    return d1h, d4h, d1d


def resolve(sig, future: pd.DataFrame):
    """Walk forward on 1H bars. Returns (outcome, r_multiple)."""
    # to_numpy().ravel() rather than .squeeze(): a single-row slice squeezes
    # down to a scalar and loses .iloc entirely.
    hi = future["High"].to_numpy().ravel()
    lo = future["Low"].to_numpy().ravel()
    buy = sig["bias"] == "BUY"
    sl, t2, rr = sig["sl"], sig["t2"], sig["rr"]

    for i in range(len(hi)):
        h, l = float(hi[i]), float(lo[i])
        hit_sl = l <= sl if buy else h >= sl
        hit_t2 = h >= t2 if buy else l <= t2
        if hit_sl and hit_t2:
            return "loss", -1.0            # pessimistic: intrabar order unknown
        if hit_sl:
            return "loss", -1.0
        if hit_t2:
            return "win", rr

    # Time stop: exit at the last close and book the actual R. Discarding
    # unresolved trades would bias the sample toward fast movers and quietly
    # inflate the edge.
    entry = sig["price"]
    risk  = abs(entry - sl)
    close = float(future["Close"].to_numpy().ravel()[-1])
    r = (close - entry) / risk if buy else (entry - close) / risk
    return "flat", r


def run_symbol(symbol, frames, cfg):
    d1h, d4h, d1d = frames
    trades, last_ts = [], None

    for i in range(WARMUP, len(d1h) - 1):
        ts = d1h.index[i]
        # 4h/1d frames sliced to information available at ts — no look-ahead.
        s4 = d4h[d4h.index <= ts]
        s1 = d1d[d1d.index <= ts]
        if len(s4) < 60 or len(s1) < 2:
            continue
        # One open trade per symbol at a time.
        if last_ts is not None and (ts - last_ts).total_seconds() < HORIZON * 3600:
            continue

        sig = evaluate(symbol, d1h.iloc[:i + 1], s4, s1,
                       price=float(d1h["Close"].squeeze().iloc[i]), cfg=cfg)
        if not sig:
            continue

        window = d1h.iloc[i + 1:i + 1 + HORIZON]
        if len(window) < 2:
            continue                      # not enough future data to judge
        outcome, r = resolve(sig, window)
        trades.append({"symbol": symbol, "ts": ts, "bias": sig["bias"],
                       "rr": sig["rr"], "score": sig["score"],
                       "sl_atr": sig["sl_atr_mult"], "src": sig["target_source"],
                       "outcome": outcome, "r": r})
        last_ts = ts
    return trades


def report(trades, label=""):
    if not trades:
        print(f"  {label}no resolved trades")
        return None
    df = pd.DataFrame(trades)
    n    = len(df)
    wins = int((df.r > 0).sum())          # any positive R, incl. time-stop exits
    wr   = wins / n * 100
    exp  = df.r.mean()
    tot  = df.r.sum()
    eq   = df.r.cumsum()
    dd   = float((eq - eq.cummax()).min())
    print(f"  {label}trades {n:4d} | win {wr:5.1f}% | expectancy {exp:+.3f}R "
          f"| total {tot:+.1f}R | maxDD {dd:.1f}R")
    return {"n": n, "win_rate": wr, "expectancy": exp, "total_r": tot, "max_dd": dd}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=",".join(SYMBOLS))
    ap.add_argument("--sweep", action="append", default=[],
                    help="field=v1,v2,v3 — repeatable, cartesian product")
    args = ap.parse_args()

    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    print(f"Loading {len(syms)} symbols (730d of 1h/4h/1d)...")
    data = {}
    for s in syms:
        f = load(s)
        if f is None:
            print(f"  {s}: insufficient data — skipped")
            continue
        data[s] = f
        print(f"  {s}: {len(f[0])} 1H bars")
    if not data:
        sys.exit("No usable data.")

    # Build the config grid.
    grid = [{}]
    for spec in args.sweep:
        field, _, vals = spec.partition("=")
        parsed = [float(v) for v in vals.split(",")]
        grid = [dict(g, **{field.strip(): v}) for g in grid for v in parsed]

    print(f"\n{len(grid)} config(s) to evaluate\n" + "=" * 66)
    results = []
    for overrides in grid:
        cfg = replace(CONFIG, **overrides) if overrides else CONFIG
        label = ", ".join(f"{k}={v}" for k, v in overrides.items()) or "default"
        print(f"\n[{label}]")
        allt = []
        for s, frames in data.items():
            t = run_symbol(s, frames, cfg)
            report(t, f"{s:8} ")
            allt += t
        agg = report(allt, "ALL      ")
        if agg:
            results.append((label, agg))
        if REJECTS:
            tot = sum(REJECTS.values())
            bars = ", ".join(f"{k} {v} ({v/tot*100:.0f}%)"
                             for k, v in REJECTS.most_common())
            print(f"  rejected by: {bars}")
            REJECTS.clear()

    if len(results) > 1:
        print("\n" + "=" * 66 + "\nRanked by expectancy:")
        for label, r in sorted(results, key=lambda x: -x[1]["expectancy"]):
            print(f"  {r['expectancy']:+.3f}R  win {r['win_rate']:5.1f}%  "
                  f"n={r['n']:4d}  {label}")


if __name__ == "__main__":
    main()
