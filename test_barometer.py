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


@check("a re-run never moves a day's Nifty date backwards (the 13 Oct case)")
def _():
    kn = barometer.keeps_newer_reading
    assert kn({"nifty_date": "2026-10-13"}, "2026-10-12") is True          # the fault: older close would replace a newer one
    assert kn({"nifty_date": "2026-10-13"}, "2026-10-13") is False         # same close: a refresh is fine
    assert kn({"nifty_date": "2026-10-09"}, "2026-10-13") is False         # newer close replaces an older row
    assert kn(None, "2026-10-12") is False                                 # no row yet
    assert kn({"nifty_date": None}, "2026-10-12") is False                 # an old row with no date cannot block a refresh
    assert kn({"nifty_date": "2026-10-13"}, None) is False                 # nothing computed to compare


def _run_main(existing_nifty_date, bars_end):
    """Run the real main() offline: a fake yfinance whose Nifty bars end on `bars_end`, today's row recorded
    with `existing_nifty_date`. Returns (exit code, file bytes before, file bytes after)."""
    import json, tempfile, types, pathlib
    from datetime import date as real_date
    tmp = pathlib.Path(tempfile.mkdtemp())
    pulse, out = tmp / "pulse.json", tmp / "barometer.json"
    pulse.write_text(json.dumps({"breadth": {"counted": 100, "above_200dma": 60, "up": 55, "at_52w_high": 3}}))
    row = {"date": "2026-10-13", "score": 35, "band": "weak", "stage": {"k": "early", "t": "Slightly cheaper", "c": ""},
           "nifty": 22722.0, "nifty_date": existing_nifty_date, "drawdown_pct": 13.8, "above_200dma_pct": 51.0, "vix": 14.1}
    out.write_text(json.dumps({"ok": True, "today": {"nifty_date": existing_nifty_date, "score": 35}, "history": [row]}))
    idx = pd.bdate_range(end=bars_end, periods=260)
    def fr(base): return pd.DataFrame({"Open": base, "High": [x * 1.01 for x in base], "Low": [x * 0.99 for x in base], "Close": base}, index=idx)
    class FakeTicker:
        def __init__(self, sym): self.sym = sym
        def history(self, **kw): return fr([100.0 + i * 0.1 for i in range(260)] if self.sym == "^NSEI" else [14.0] * 260)
    class FakeDate(real_date):
        @classmethod
        def today(cls): return cls(2026, 10, 13)
    saved = (barometer.PULSE, barometer.OUT, barometer.date)
    sys.modules["yfinance"] = types.SimpleNamespace(Ticker=FakeTicker)
    barometer.PULSE, barometer.OUT, barometer.date = pulse, out, FakeDate
    try:
        before = out.read_bytes()
        code = barometer.main()
        return code, before, out.read_bytes()
    finally:
        barometer.PULSE, barometer.OUT, barometer.date = saved
        sys.modules.pop("yfinance", None)


@check("main(): a later run whose Nifty bars end on an OLDER day leaves the recorded reading untouched")
def _():
    code, before, after = _run_main(existing_nifty_date="2026-10-13", bars_end="2026-10-12")
    assert code == 0 and before == after, "the file was rewritten"


@check("main(): a run with a NEWER Nifty close still replaces the day's row (the rule does not freeze readings)")
def _():
    import json
    code, before, after = _run_main(existing_nifty_date="2026-10-09", bars_end="2026-10-13")
    assert code == 0 and before != after
    d = json.loads(after)
    assert len(d["history"]) == 1 and d["history"][0]["nifty_date"] == "2026-10-13", d["history"]


@check("main(): a refresh describing the SAME Nifty close as the recorded row is still written (breadth may have moved)")
def _():
    code, before, after = _run_main(existing_nifty_date="2026-10-13", bars_end="2026-10-13")
    assert code == 0 and before != after, "a same-close refresh was blocked"


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
