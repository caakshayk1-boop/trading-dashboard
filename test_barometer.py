#!/usr/bin/env python3
"""
test_barometer.py — the reading uses closed bars only.

On 21, 28 and 29 Sep the published barometer carried nifty: null and vix:
null, with trend and volatility (45 of 100 weight) missing. The job ran after
midnight IST and yfinance returned a row for the new day with no Close;
`iloc[-1]` took it. Offline, no network, no pytest.

Usage:
    python3 test_barometer.py
"""
from __future__ import annotations

import math
import sys

import pandas as pd

import barometer

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


def frame(closes, tail_nan=False):
    idx = pd.date_range("2026-01-01", periods=len(closes) + (1 if tail_nan else 0), freq="B")
    c = list(closes) + ([float("nan")] if tail_nan else [])
    return pd.DataFrame({"Open": c, "High": [x * 1.01 if x == x else x for x in c],
                         "Low": [x * 0.99 if x == x else x for x in c], "Close": c}, index=idx)


@check("a trailing bar with no Close is dropped")
def _():
    f = barometer.complete_bars(frame([100.0, 101.0, 102.0], tail_nan=True))
    assert len(f) == 3 and f["Close"].iloc[-1] == 102.0


@check("complete history is left untouched")
def _():
    f = barometer.complete_bars(frame([100.0, 101.0]))
    assert len(f) == 2


@check("an empty frame stays empty (the caller reports it)")
def _():
    assert barometer.complete_bars(pd.DataFrame()).empty


@check("with the NaN bar dropped, trend and volatility both score")
def _():
    nif = barometer.complete_bars(frame([100.0 + i for i in range(260)], tail_nan=True))
    vix = barometer.complete_bars(frame([13.0, 13.5], tail_nan=True))
    yr = nif.tail(252)
    t = barometer.compute(float(nif["Close"].iloc[-1]), float(yr["High"].max()), float(yr["Low"].min()),
                          {"counted": 100, "above_200dma": 60, "up": 55, "at_52w_high": 3},
                          float(vix["Close"].iloc[-1]))
    keys = {p["key"] for p in t["parts"]}
    assert {"trend", "volatility"} <= keys and t["coverage"]["complete"], t["coverage"]
    assert t["nifty"] is not None and not math.isnan(t["nifty"])


@check("taking the raw last row reproduces the fault (so the test means something)")
def _():
    raw = frame([100.0 + i for i in range(260)], tail_nan=True)
    t = barometer.compute(float(raw["Close"].iloc[-1]), float(raw["High"].max()), float(raw["Low"].min()),
                          {"counted": 100, "above_200dma": 60, "up": 55, "at_52w_high": 3}, float("nan"))
    assert "trend" not in {p["key"] for p in t["parts"]} and not t["coverage"]["complete"]


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
