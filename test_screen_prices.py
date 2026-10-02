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


# ── the 52-week range is the TRADED range; a finished session keeps its day ──
def _year(tk, n=260, missing_last=False, div=0.0):
    """n business days ending a few days ago (never a literal date). A dividend
    of `div` makes every Adj Close before the last 20 bars that much lower."""
    end = pd.Timestamp.now(tz="Asia/Kolkata").normalize().tz_localize(None) - pd.offsets.BDay(3)
    idx = pd.bdate_range(end=end, periods=n)
    c = [100.0 + (i % 50) for i in range(n)]
    hi = [x + 2 for x in c]
    lo = [x - 2 for x in c]
    lo[-1] = 70.0                                  # the year's low is set on the last session
    adj = [x * (1 - div) if i < n - 20 else x for i, x in enumerate(c)]
    if missing_last:
        c[-1] = float("nan"); adj[-1] = float("nan")
    d = {"Open": c, "High": hi, "Low": lo, "Close": c, "Adj Close": adj, "Volume": [1e5] * n}
    cols = pd.MultiIndex.from_tuples([(f, tk) for f in d])
    return pd.DataFrame({(f, tk): d[f] for f in d}, index=idx)[cols], idx[-1]


def _hourly(tk, day, last="15:15", close=74.5):
    hours = [h for h in ("09:15", "10:15", "11:15", "12:15", "13:15", "14:15", "15:15") if h <= last]
    idx = pd.DatetimeIndex([f"{day:%Y-%m-%d} {h}" for h in hours]).tz_localize("Asia/Kolkata")
    vals = [80.0] * (len(hours) - 1) + [close]
    return pd.DataFrame({("Close", tk): vals, ("Low", tk): vals, ("High", tk): vals}, index=idx)


def _run_year(**kw):
    hourly = kw.pop("hourly", {})
    frames = {}
    def dl(batch, **k):
        tk = batch[0]
        if k.get("interval") == "1h":
            return _hourly(tk, frames[tk][1], **hourly)
        assert k.get("auto_adjust") is False, "the daily download must be raw"
        frames[tk] = _year(tk, **kw)
        return frames[tk][0]
    out = run_with(dl, ["ABLBL"])
    return out["ABLBL"], frames


@check("the 52-week range reads traded prices while returns stay dividend-adjusted")
def _():
    rec, _f = _run_year(div=0.05)
    t = S.technicals(rec, None)
    assert t["high52"] == 151.0, t["high52"]              # the raw high, not 151 x 0.95
    assert rec["c"][0] == 100.0 * 0.95, rec["c"][0]       # the return series is adjusted
    assert t["low52"] == 70.0


@check("a finished session served with no close is rebuilt from its last hourly bar (ABLBL, 1 Oct)")
def _():
    rec, f = _run_year(missing_last=True)
    day = f["ABLBL.NS" if "ABLBL.NS" in f else next(iter(f))][1]
    assert rec["last_date"] == f"{day:%Y-%m-%d}", (rec["last_date"], day)
    assert rec["c"][-1] == 74.5 and rec["lr"][-1] == 70.0
    assert S.technicals(rec, None)["low52"] == 70.0, "the session's low is in the range"


@check("hourly bars that stop before 15:15 rebuild nothing: the day stays out")
def _():
    rec, f = _run_year(missing_last=True, hourly={"last": "13:15"})
    day = next(iter(f.values()))[1]
    assert rec["last_date"] < f"{day:%Y-%m-%d}" and 70.0 not in rec["lr"]


@check("a rebuilt close outside the day's own range is refused")
def _():
    rec, f = _run_year(missing_last=True, hourly={"close": 500.0})
    day = next(iter(f.values()))[1]
    assert rec["last_date"] < f"{day:%Y-%m-%d}"


@check("a session still trading is never treated as a missing close")
def _():
    idx = pd.DatetimeIndex([pd.Timestamp("2026-10-05")])
    nan = float("nan")
    raw = {"High": pd.Series([101.0], idx), "Low": pd.Series([99.0], idx), "Close": pd.Series([nan], idx)}
    at = lambda h, m: S.datetime(2026, 10, 5, h, m, tzinfo=S.IST)
    assert S._missing_close(raw, at(14, 0)) is None
    assert S._missing_close(raw, at(21, 0))[0].isoformat() == "2026-10-05"


@check("coverage counts names a session behind and closes left unrepaired")
def _():
    S.FETCH_REPORT.clear(); S.FETCH_REPORT.update(missing_close=5, rebuilt=3)
    rows = [{"last_date": "2026-10-01"}, {"last_date": "2026-10-01"}, {"last_date": "2026-09-30"}]
    cov = S.coverage(rows)
    assert cov["behind"] == 1 and cov["missing_close"] == 2, cov
    S.FETCH_REPORT.clear()


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
