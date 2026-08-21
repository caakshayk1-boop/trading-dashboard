#!/usr/bin/env python3
"""
rebuild_ledger_from_bars.py — re-derive every closed signal's outcome from price history.

WHY THIS EXISTS
---------------
The 2026-08-08 re-grade left `status` and `r_multiple` contradicting the price
columns on 168 of 573 graded rows, in both directions:

  * 85 rows: SL_HIT / -1.00R while exit_price sits at the TARGET and pnl_pct is
    positive.  (SILVER 2026-07-23 BUY 60.10 -> 62.07, +3.28%, booked -1.00R.)
  * 26 rows: T2_HIT / +2.50R while exit_price sits exactly at the STOP and
    pnl_pct is negative.  (HAL BUY 4646.50, sl 4558.86, exit 4558.86, -1.89%.)
  * 31 TIME_STOP rows exit at their stop but book less than -1R, one at +1.048R.

Because the errors run in BOTH directions, no in-place arithmetic repair can be
trusted: re-deriving R from `exit_price` moves the full ledger to +0.274R and the
last-30-day window to -0.308R.  The disagreement is the point.  `exit_price`
itself was clobbered on all 65 EXPIRED rows (standalone_scan.py:529 books them at
whatever `last_close` the job happened to see), so it cannot be assumed sound on
the rest either.

So this script does not repair the ledger.  It rebuilds it: fetch the bars, walk
them forward from the signal, and record what price actually did.

METHOD
------
For each graded row:
  1. Resolve the hold horizon with standalone_scan._max_hold_hours -- the same
     function the live engine uses.  Never reimplemented here: a second horizon
     table that drifts from the first is how "1W" came to force-close 6-to-12
     month positions.
  2. Fetch OHLC covering the signal's life.  1h bars for intraday timeframes,
     daily for the rest.  A 1h bar's High/Low contains every 15m High/Low inside
     it, so 1h is sufficient to detect whether a level was TOUCHED; only the
     ordering within the hour is lost, and that is flagged, not guessed.
  3. Walk bars strictly after the signal (never before -- grading against bars
     that predate the signal is what produced the HINDALCO phantom stop at a
     price four days older than the signal itself).
  4. First level touched wins.  SL -> SL_HIT at sl.  T2 -> T2_HIT at target2.
     Horizon reached with neither -> TIME_STOP at that bar's close.  Neither and
     horizon not reached -> still OPEN.
  5. If ONE bar's range spans both sl and target2, the order inside that bar is
     unknowable from this data.  Resolve to SL_HIT and set exit_ambiguous=1.
     A stop you cannot prove you missed is a stop you took; the opposite
     assumption is how a ledger flatters itself.
  6. R is floored at -1.00.  A stop that is honoured cannot lose more than the
     risk it defined.

SAFETY
------
Dry run by default.  Writes nothing without --apply.  --apply refuses to run
without --backup-dir, snapshots the source first, and never touches rows whose
status is OPEN / CANCELLED / VOID.  This changes numbers that have already been
published: run --report and read the diff before you apply anything.

USAGE
    python3 rebuild_ledger_from_bars.py --dry-run --report
    python3 rebuild_ledger_from_bars.py --dry-run --out /tmp/rebuilt.json
    python3 rebuild_ledger_from_bars.py --apply --backup-dir data/backups
"""

import argparse
import datetime as dt
import json
import math
import pickle
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).parent
SRC = ROOT / "data" / "all_signals.json"
CACHE = ROOT / "cache" / "rebuild_bars.pkl"

NOT_GRADED = ("OPEN", "CANCELLED", "VOID")
INTRADAY_TF = {"15M", "1H", "4H"}


def _f(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


# ── bar fetching ─────────────────────────────────────────────────────────────
def fetch_all(rows, refresh=False):
    """One fetch per (symbol, interval), sliced per signal afterwards.

    714 individual downloads would be slow and would get rate-limited long
    before it finished; the whole ledger spans 11 weeks and 131 symbols.
    """
    import yfinance as yf
    from symbols import to_yahoo

    if CACHE.exists() and not refresh:
        with CACHE.open("rb") as fh:
            bars = pickle.load(fh)
        print(f"loaded {len(bars)} cached series from {CACHE}", file=sys.stderr)
        return bars

    need = set()
    for r in rows:
        tf = (r.get("timeframe") or "").upper()
        need.add((r["symbol"], "1h" if tf in INTRADAY_TF else "1d"))

    bars = {}
    for i, (sym, interval) in enumerate(sorted(need), 1):
        ysym = to_yahoo(sym)
        period = "180d" if interval == "1d" else "120d"
        try:
            df = yf.download(ysym, period=period, interval=interval,
                             progress=False, auto_adjust=True, timeout=15)
        except Exception as e:                      # noqa: BLE001
            print(f"  [{i}/{len(need)}] {sym} {interval}: FAILED {e}", file=sys.stderr)
            continue
        if df is None or df.empty:
            print(f"  [{i}/{len(need)}] {sym} {interval}: no data", file=sys.stderr)
            continue
        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            df.columns = df.columns.get_level_values(0)
        bars[(sym, interval)] = df[["Open", "High", "Low", "Close"]]
        print(f"  [{i}/{len(need)}] {sym} {interval}: {len(df)} bars", file=sys.stderr)

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with CACHE.open("wb") as fh:
        pickle.dump(bars, fh)
    print(f"cached {len(bars)} series to {CACHE}", file=sys.stderr)
    return bars


# ── the walk ─────────────────────────────────────────────────────────────────
def replay(row, bars):
    """Return (status, exit_price, r, pnl_pct, ambiguous, note) or None if unknowable."""
    import pandas as pd
    import standalone_scan as ss

    entry = _f(row.get("entry"))
    sl = _f(row.get("sl"))
    t2 = _f(row.get("target2")) or _f(row.get("target1"))
    if None in (entry, sl, t2):
        return None, "missing entry/sl/target"
    risk = abs(entry - sl)
    if risk == 0:
        return None, "zero risk"

    buy = (row.get("action") or "BUY").upper() == "BUY"
    tf = (row.get("timeframe") or "").upper()
    interval = "1h" if tf in INTRADAY_TF else "1d"
    df = bars.get((row["symbol"], interval))
    if df is None or df.empty:
        return None, "no bars"

    try:
        opened = dt.date.fromisoformat(str(row.get("date"))[:10])
    except (TypeError, ValueError):
        return None, "unparseable date"

    idx = df.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    win = df[pd.Series(idx, index=df.index).dt.date > opened]
    if win.empty:
        return None, "no bars after signal"

    # Same horizon table the live engine uses -- never a local copy.
    meta = {}
    try:
        meta = json.loads(row.get("metadata") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    limit_h = ss._max_hold_hours(row.get("timeframe") or "",
                                 engine=str(row.get("signal_type") or ""),
                                 horizon=str(meta.get("horizon") or ""))
    bar_h = 1 if interval == "1h" else 24
    max_bars = None if limit_h is None else max(1, int(limit_h / bar_h))

    def out(status, px, ambiguous=0, note=""):
        r = (px - entry) / risk if buy else (entry - px) / risk
        pnl = ((px - entry) / entry * 100) * (1 if buy else -1)
        return (status, round(px, 4), round(max(r, -1.0), 3),
                round(pnl, 2), ambiguous, note), None

    for n, (_ts, bar) in enumerate(win.iterrows(), 1):
        hi, lo, close = _f(bar["High"]), _f(bar["Low"]), _f(bar["Close"])
        if None in (hi, lo, close):
            continue
        sl_hit = lo <= sl if buy else hi >= sl
        t2_hit = hi >= t2 if buy else lo <= t2

        if sl_hit and t2_hit:
            # One bar spanned both. Order inside the bar is not in this data.
            return out("SL_HIT", sl, ambiguous=1,
                       note="bar spanned both levels; resolved to stop")
        if sl_hit:
            return out("SL_HIT", sl)
        if t2_hit:
            return out("T2_HIT", t2)
        if max_bars is not None and n >= max_bars:
            return out("TIME_STOP", close, note=f"horizon {limit_h}h reached")

    return None, ("still open" if max_bars is None or len(win) < max_bars
                  else "horizon passed but no bar resolved it")


# ── reporting ────────────────────────────────────────────────────────────────
def summarise(vals, label):
    if not vals:
        print(f"{label:44} (no rows)")
        return
    n = len(vals)
    mu = sum(vals) / n
    sd = statistics.stdev(vals) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 1 else 0.0
    print(f"{label:44} n={n:4} exp={mu:+.3f}R  SE={se:.3f}  "
          f"t={(mu / se if se else 0):+.2f}  win={100 * sum(1 for v in vals if v > 0) / n:4.1f}%  "
          f"total={sum(vals):+.1f}R")


def load_rows():
    """Read the ledger from the DATABASE when one is configured, else the export.

    This originally read data/all_signals.json only, which made --apply
    unverifiable: the export is regenerated by the scanner job, not by this one,
    so re-running after a successful apply re-read the stale file and reported
    the same 150 rows still needing a change. The DB is the ledger; the JSON is
    a snapshot of it, and the two are hours apart by design.
    """
    import os
    if os.environ.get("TURSO_URL"):
        import tracker
        tracker.init_db()
        with tracker._conn() as c:
            cur = c.execute(
                "SELECT id, date, signal_type, symbol, action, timeframe, entry, sl, "
                "target1, target2, rr, score, status, exit_price, pnl_pct, r_multiple, "
                "metadata FROM all_signals")
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        print(f"read {len(rows)} rows from the DATABASE", file=sys.stderr)
        return rows
    print(f"TURSO_URL unset -- reading the export at {SRC}", file=sys.stderr)
    return json.loads(SRC.read_text())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--apply", action="store_true",
                    help="write the rebuilt grades back to data/all_signals.json")
    ap.add_argument("--backup-dir", help="required with --apply")
    ap.add_argument("--refresh", action="store_true", help="ignore the bar cache")
    ap.add_argument("--report", action="store_true", help="print every changed row")
    ap.add_argument("--days", type=int, help="also report this trailing window")
    ap.add_argument("--out", help="write the rebuilt ledger to this path (no DB write)")
    args = ap.parse_args()

    if args.apply and not args.backup_dir:
        sys.exit("--apply requires --backup-dir. Refusing to overwrite published "
                 "grades without a snapshot.")

    rows = load_rows()
    graded = [r for r in rows if r.get("status") not in NOT_GRADED]
    print(f"{len(rows)} rows, {len(graded)} graded\n", file=sys.stderr)

    bars = fetch_all(graded, refresh=args.refresh)

    changed, unchanged, skipped = [], 0, []
    rebuilt = {}
    for r in graded:
        res, why = replay(r, bars)
        if res is None:
            skipped.append((r, why))
            continue
        status, px, rmult, pnl, amb, note = res
        rebuilt[r["id"]] = res
        if (status != r.get("status")
                or abs((_f(r.get("r_multiple")) or 0) - rmult) > 0.02):
            changed.append((r, res))
        else:
            unchanged += 1

    print(f"\n=== rebuild from bars ===")
    print(f"  regraded and CHANGED : {len(changed)}")
    print(f"  regraded, unchanged  : {unchanged}")
    print(f"  could not regrade    : {len(skipped)}")
    if skipped:
        reasons = {}
        for _r, why in skipped:
            reasons[why] = reasons.get(why, 0) + 1
        for why, n in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"      {n:4}  {why}")

    old = [_f(r.get("r_multiple")) for r in graded if _f(r.get("r_multiple")) is not None]
    new = [v[2] for v in rebuilt.values()]
    print()
    summarise(old, "PUBLISHED (current ledger)")
    summarise(new, "REBUILT FROM BARS")
    amb = sum(1 for v in rebuilt.values() if v[4])
    print(f"\n  ambiguous (one bar spanned both levels): {amb}")

    if args.days:
        cut = dt.date.today() - dt.timedelta(days=args.days)
        def inwin(r):
            try:
                return dt.date.fromisoformat(str(r.get("date"))[:10]) >= cut
            except (TypeError, ValueError):
                return False
        print(f"\n--- trailing {args.days}d ---")
        summarise([_f(r.get("r_multiple")) for r in graded
                   if inwin(r) and _f(r.get("r_multiple")) is not None], "PUBLISHED")
        summarise([v[2] for r, v in ((r, rebuilt.get(r["id"])) for r in graded)
                   if v and inwin(r)], "REBUILT FROM BARS")

    if args.report and changed:
        print("\n--- changed rows ---")
        for r, (status, px, rmult, pnl, ambg, note) in sorted(
                changed, key=lambda x: str(x[0].get("date")), reverse=True):
            flag = " [AMBIGUOUS]" if ambg else ""
            print(f"{str(r.get('date'))[:10]}  {str(r.get('symbol'))[:12]:12} "
                  f"{str(r.get('action')):4} "
                  f"was {str(r.get('status')):10} {(_f(r.get('r_multiple')) or 0):+6.2f}R "
                  f"@ {r.get('exit_price')}"
                  f"   ->  now {status:10} {rmult:+6.2f}R @ {px}{flag} {note}")

    if args.out:
        merged = []
        for r in rows:
            r = dict(r)
            if r["id"] in rebuilt:
                status, px, rmult, pnl, ambg, note = rebuilt[r["id"]]
                r.update(status=status, exit_price=px, r_multiple=rmult,
                         pnl_pct=pnl, exit_ambiguous=ambg,
                         regraded_at=dt.datetime.now().isoformat(),
                         remarks=(r.get("remarks") or "") + " | rebuilt from bars")
            merged.append(r)
        Path(args.out).write_text(json.dumps(merged, indent=1))
        print(f"\nwrote rebuilt ledger to {args.out} (source untouched)")

    if args.apply:
        if not rebuilt:
            sys.exit("nothing to apply")
        bd = Path(args.backup_dir)
        bd.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")

        import tracker
        tracker.init_db()

        # Snapshot the DB rows themselves, not just the JSON export. The export
        # is regenerated from the DB on the next scan, so a JSON-only backup
        # would restore nothing.
        with tracker._conn() as c:
            cur = c.execute(
                "SELECT id, status, exit_price, pnl_pct, r_multiple, exit_ambiguous, "
                "regraded_at FROM all_signals WHERE id IN (%s)"
                % ",".join(str(i) for i in rebuilt))
            cols = [d[0] for d in cur.description]
            before = [dict(zip(cols, row)) for row in cur.fetchall()]
        snap = bd / f"all_signals_pre_bar_rebuild_{stamp}.json"
        snap.write_text(json.dumps(before, indent=1, default=str))
        print(f"\nbacked up {len(before)} pre-change rows to {snap}")

        # Refuse to write to the wrong database. Locally TURSO_URL is unset and
        # tracker falls back to signals.db, a 5-row stub -- an --apply there
        # would report success having changed nothing that matters.
        if len(before) < len(rebuilt) * 0.5:
            sys.exit(f"target DB holds only {len(before)} of {len(rebuilt)} ids -- "
                     "this is not the production ledger. Run this in CI, where "
                     "TURSO_URL and TURSO_TOKEN are set. Nothing written.")

        now = dt.datetime.now().isoformat()
        with tracker._conn() as c:
            for sid, (status, px, rmult, pnl, ambg, _note) in rebuilt.items():
                c.execute(
                    "UPDATE all_signals SET status=?, exit_price=?, pnl_pct=?, "
                    "r_multiple=?, exit_ambiguous=?, regraded_at=? WHERE id=?",
                    (status, px, pnl, rmult, ambg, now, sid))
            c.commit()
            tracker._db.sync(c)
        print(f"applied {len(rebuilt)} rebuilt grades to the ledger")
        print(f"to roll back, restore from {snap}")


if __name__ == "__main__":
    main()
