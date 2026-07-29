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

# Per asset class: bars allowed for a trade to resolve, warmup before the
# first evaluation, and how long the regime frame must be.
PROFILES = {
    "cf":           {"horizon": 48, "warmup": 120, "regime_min": 60, "bar_hours": 1},
    "equity":       {"horizon": 20, "warmup": 260, "regime_min": 55, "bar_hours": 24},
    # NSE trades 6.25h/day, so ~30 hourly bars is about a week held.
    "equity_intra": {"horizon": 30, "warmup": 260, "regime_min": 55, "bar_hours": 1},
}
_EQUITY = ("equity", "equity_intra")


def load(symbol: str, asset: str = "cf"):
    """Fetch the three frames evaluate() needs. Returns None if data is short."""
    p = PROFILES[asset]
    t = to_yahoo(symbol)
    dl = lambda period, iv: yf.download(t, period=period, interval=iv,
                                        progress=False, auto_adjust=True)

    if asset == "equity_intra":
        # equity_engine.fetch() returns only a 5-day daily frame here — all the
        # live scanner needs for today's range. A backtest has to slice daily
        # history across the whole period, so fetch it long.
        frames = (dl("720d", "1h"), dl("5y", "1d"), dl("5y", "1d"))
    elif asset == "equity":
        frames = (dl("5y", "1d"), dl("5y", "1wk"), dl("5y", "1d"))
    else:
        frames = (dl("730d", "1h"), dl("730d", "4h"), dl("730d", "1d"))

    entry, regime, daily = frames
    # The daily frame only supplies today's high/low and the previous close,
    # so it needs 2 bars, not a full warmup.
    if entry is None or len(entry) < p["warmup"]:      return None
    if regime is None or len(regime) < p["regime_min"]: return None
    if daily is None or len(daily) < 2:                 return None

    for d in frames:
        d.index = pd.to_datetime(d.index, utc=True)
    return frames


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


def run_symbol(symbol, frames, cfg, asset="cf"):
    p = PROFILES[asset]
    horizon, warmup, regime_min = p["horizon"], p["warmup"], p["regime_min"]
    cooldown = horizon * p["bar_hours"] * 3600

    d1h, d4h, d1d = frames
    trades, last_ts = [], None

    for i in range(warmup, len(d1h) - 1):
        ts = d1h.index[i]
        # regime/daily frames sliced to what was knowable at ts — no look-ahead.
        s4 = d4h[d4h.index <= ts]
        s1 = d1d[d1d.index <= ts]
        if len(s4) < regime_min or len(s1) < 2:
            continue
        # One open trade per symbol at a time.
        if last_ts is not None and (ts - last_ts).total_seconds() < cooldown:
            continue

        sig = evaluate(symbol, d1h.iloc[:i + 1], s4, s1,
                       price=float(d1h["Close"].squeeze().iloc[i]), cfg=cfg)
        if not sig:
            continue

        window = d1h.iloc[i + 1:i + 1 + horizon]
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
    # t = mean / standard error. Expectancy alone is meaningless without it:
    # a small positive mean over a noisy sample is indistinguishable from
    # zero, and picking the best of N swept configs is biased upward by
    # construction. |t| < 2 means "not shown to differ from no edge".
    sd   = float(df.r.std(ddof=1)) if n > 1 else 0.0
    t    = exp / (sd / (n ** 0.5)) if sd > 0 and n > 1 else 0.0
    verdict = "edge" if t >= 2 else "noise" if abs(t) < 2 else "negative"
    print(f"  {label}trades {n:4d} | win {wr:5.1f}% | expectancy {exp:+.3f}R "
          f"| total {tot:+.1f}R | maxDD {dd:.1f}R | t={t:+.2f} ({verdict})")
    return {"n": n, "win_rate": wr, "expectancy": exp, "total_r": tot,
            "max_dd": dd, "t_stat": round(t, 2), "sd": round(sd, 3)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="cf",
                    choices=["cf", "equity", "equity_intra"])
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--sweep", action="append", default=[],
                    help="field=v1,v2,v3 — repeatable, cartesian product")
    args = ap.parse_args()

    if args.symbols:
        syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.asset in _EQUITY:
        import equity_engine
        syms = equity_engine.LIQUID
    else:
        syms = SYMBOLS

    base = CONFIG
    if args.asset in _EQUITY:
        import equity_engine
        base = equity_engine.EQUITY_CONFIG

    print(f"Loading {len(syms)} {args.asset} symbols...")
    data = {}
    for s in syms:
        f = load(s, args.asset)
        if f is None:
            print(f"  {s}: insufficient data — skipped")
            continue
        data[s] = f
        print(f"  {s}: {len(f[0])} entry bars")
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
        cfg = replace(base, **overrides) if overrides else base
        label = ", ".join(f"{k}={v}" for k, v in overrides.items()) or "default"
        print(f"\n[{label}]")
        allt = []
        for s, frames in data.items():
            t = run_symbol(s, frames, cfg, args.asset)
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
                  f"n={r['n']:4d}  t={r['t_stat']:+.2f}  {label}")
        best = max(results, key=lambda x: x[1]["expectancy"])[1]
        if best["t_stat"] < 2:
            print("\n  WARNING: the best config has |t| < 2, so it is not "
                  "distinguishable\n  from zero edge. Selecting the top of a "
                  "sweep is upward-biased —\n  treat this as 'no edge found', "
                  "not as a tuned strategy.")


if __name__ == "__main__":
    main()
