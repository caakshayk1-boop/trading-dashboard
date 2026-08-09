#!/usr/bin/env python3
"""
verify_closures.py — a trade may only be closed by a bar that actually printed.

The problem this exists for
---------------------------
On 2026-08-09 the ledger showed HINDALCO and GRASIM both SL_HIT, signalled
2026-08-07 after Friday's close. There had been ZERO trading sessions since.
No bar had ever existed against which either could resolve, yet both were
booked as losses, on a public page, with an R attached.

fix_phantom_exits.py corrected the exit PRICE on rows like these — it moved
HINDALCO from an impossible 990.00 to its stop at 1013.892 — but it assumed the
closure itself was real and only the fill was wrong. It was not. Correcting the
price of a stop-out that never happened still publishes a loss that never
happened.

The rule
--------
A stop-out requires a bar, strictly after the signal, whose low (long) or high
(short) reached the stop. A target hit requires the same in the other
direction. If no such bar exists, the trade was never resolved and belongs back
in OPEN — where it will resolve on its own once the market actually trades
there.

Deliberately NOT touched:
  · TIME_STOP / EXPIRED — those close by elapsed time, not by a level, so the
    absence of a touching bar is the whole point of them.
  · VOID / CANCELLED — never entered.

Usage
-----
    python verify_closures.py            # report only
    python verify_closures.py --apply    # reopen the unresolvable ones
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

import tracker
from symbols import to_yahoo

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("closures")

# Only statuses that claim a PRICE LEVEL was reached.
LEVEL_CLOSED = ("SL_HIT", "STOPPED", "T1_HIT", "T2_HIT", "TARGET_HIT")
TOUCH_TOL = 0.001          # 0.1%, for float noise on an OHLC comparison


def _f(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def bars_after(symbol: str, sig_date: str, until: str | None):
    """Sessions strictly after the signal date. None when data is unavailable —
    which must never be read as 'no bars', or every unfetchable symbol would be
    reopened."""
    try:
        start = datetime.fromisoformat(sig_date[:10]).date() + timedelta(days=1)
    except ValueError:
        return None
    end = datetime.now().date() + timedelta(days=1)
    if until:
        try:
            end = min(end, datetime.fromisoformat(until[:10]).date() + timedelta(days=3))
        except ValueError:
            pass
    if start >= end:
        # The signal is newer than any session that could resolve it. That is a
        # real, knowable answer — not missing data.
        return []
    try:
        df = yf.download(to_yahoo(symbol), start=start.isoformat(), end=end.isoformat(),
                         interval="1d", progress=False, auto_adjust=True)
    except Exception as e:
        log.warning(f"  {symbol}: download failed ({e})")
        return None
    if df is None:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    tracker.init_db()
    placeholders = ",".join("?" for _ in LEVEL_CLOSED)
    with tracker._conn() as c:
        rows = pd.read_sql(
            f"SELECT id, symbol, date, closed_at, action, entry, sl, target1, target2, "
            f"exit_price, r_multiple, status, signal_type FROM all_signals "
            f"WHERE upper(coalesce(status,'')) IN ({placeholders})",
            c, params=list(LEVEL_CLOSED)
        ).to_dict("records")

    log.info(f"verifying {len(rows)} level-closed signals\n")
    reopen, checked, nodata = [], 0, 0

    for s in rows:
        entry, sl = _f(s["entry"]), _f(s["sl"])
        t1, t2 = _f(s["target1"]), _f(s["target2"])
        if entry is None or sl is None:
            continue
        is_long = str(s.get("action", "BUY")).upper() != "SELL"
        st = str(s["status"]).upper()

        df = bars_after(s["symbol"], str(s["date"]), str(s.get("closed_at") or ""))
        if df is None:
            nodata += 1
            continue
        checked += 1

        if len(df) == 0:
            reopen.append((s, "no trading session has occurred since the signal"))
            continue

        lo, hi = float(df["Low"].min()), float(df["High"].max())
        if st in ("SL_HIT", "STOPPED"):
            hit = (lo <= sl * (1 + TOUCH_TOL)) if is_long else (hi >= sl * (1 - TOUCH_TOL))
            if not hit:
                reopen.append((s, f"stop {sl:.2f} never reached "
                                  f"({'low' if is_long else 'high'} "
                                  f"{(lo if is_long else hi):.2f})"))
        else:
            lvl = t2 if st == "T2_HIT" and t2 is not None else t1
            if lvl is None:
                continue
            hit = (hi >= lvl * (1 - TOUCH_TOL)) if is_long else (lo <= lvl * (1 + TOUCH_TOL))
            if not hit:
                reopen.append((s, f"target {lvl:.2f} never reached "
                                  f"({'high' if is_long else 'low'} "
                                  f"{(hi if is_long else lo):.2f})"))

    log.info(f"\nchecked {checked} · {nodata} unfetchable (left alone) · "
             f"{len(reopen)} closed without a qualifying bar\n")
    for s, why in reopen:
        log.info(f"  REOPEN {s['symbol']:12} {s['date']} {str(s['signal_type'])[:14]:14} "
                 f"{s['status']:8} r={s['r_multiple']} — {why}")

    if not reopen or not args.apply:
        if reopen:
            log.info("\nreport only — pass --apply to reopen")
        return 0

    with tracker._conn() as c:
        for s, _why in reopen:
            c.execute(
                "UPDATE all_signals SET status='OPEN', lifecycle_status='Triggered', "
                "exit_price=NULL, pnl_pct=NULL, r_multiple=NULL, closed_at=NULL, "
                "exit_ambiguous=0, regraded_at=? WHERE id=?",
                (datetime.utcnow().isoformat(), int(s["id"]))
            )
        c.commit()
        tracker._db.sync(c)
    log.info(f"\nreopened {len(reopen)} signals")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
