#!/usr/bin/env python3
"""
test_screen_prices.py — the benchmark survives any batch arithmetic.

When the universe reached 1,000 names, 1,000 + ^NSEI made a 26th batch holding
the benchmark alone. fetch_prices assumed a one-ticker download has flat
columns; current yfinance returns (field, ticker) columns, the parse raised,
and the screen published nifty_1m, nifty_1y, price_date and every relative-
strength figure as null. yfinance is stubbed here — offline, no pytest.

Usage:
    python3 test_screen_prices.py
"""
from __future__ import annotations

import sys
import types

import pandas as pd

import stock_screen as S

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


def ohlcv(n=30, start=100.0):
    idx = pd.date_range("2026-08-01", periods=n, freq="B")
    c = [start + i for i in range(n)]
    return idx, {"Open": c, "High": [x + 1 for x in c], "Low": [x - 1 for x in c], "Close": c, "Volume": [0.0] * n}


def multi(tickers):
    idx, d = ohlcv()
    cols = pd.MultiIndex.from_tuples([(f, t) for f in d for t in tickers])
    return pd.DataFrame({(f, t): d[f] for f in d for t in tickers}, index=idx)[cols]


def flat():
    idx, d = ohlcv()
    return pd.DataFrame(d, index=idx)


def run_with(downloader, symbols):
    stub = types.ModuleType("yfinance")
    stub.download = downloader
    real = sys.modules.get("yfinance")
    sys.modules["yfinance"] = stub
    pause = S.PRICE_PAUSE
    S.PRICE_PAUSE = 0
    try:
        return S.fetch_prices(symbols)
    finally:
        S.PRICE_PAUSE = pause
        if real is not None:
            sys.modules["yfinance"] = real
        else:
            del sys.modules["yfinance"]


@check("a one-ticker batch with (field, ticker) columns parses")
def _():
    out = run_with(lambda batch, **k: multi(batch), [S.BENCHMARK])
    assert S.BENCHMARK in out and len(out[S.BENCHMARK]["c"]) == 30, out.keys()


@check("a one-ticker batch with flat columns still parses")
def _():
    out = run_with(lambda batch, **k: flat(), [S.BENCHMARK])
    assert S.BENCHMARK in out and out[S.BENCHMARK]["last_date"]


@check("1,000 names + the benchmark: the benchmark, alone in batch 26, arrives")
def _():
    names = [f"N{i:04d}" for i in range(1000)]
    seen = []
    def dl(batch, **k):
        seen.append(len(batch))
        return multi(batch)
    out = run_with(dl, names + [S.BENCHMARK])
    assert seen[-1] == 1, seen[-3:]
    assert S.BENCHMARK in out, "benchmark dropped"
    assert len(out) == 1001


def main() -> int:
    passed = failed = 0
    for name, fn in CHECKS:
        try:
            fn()
        except AssertionError as e:
            print(f"  FAIL  {name}  ({e})"); failed += 1
        except Exception as e:                              # noqa: BLE001
            print(f"  ERROR {name}  ({type(e).__name__}: {e})"); failed += 1
        else:
            print(f"  PASS  {name}"); passed += 1
    print(f"\n{passed} passed · {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
