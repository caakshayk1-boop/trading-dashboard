#!/usr/bin/env python3
"""
backfill_picks_sip.py — put historical Top 5 picks and SIP allocations in the ledger.

tracker.log_top5_picks() and log_sip_bucket() are wired into the points where
those artefacts are CREATED — a new ISO week's picks being built, a monthly SIP
bucket being proposed. Correct for everything from now on, and it means the
ledger stays empty of both until the next Monday and the next month roll around.

Everything needed already exists:
  * `newspaper_stocks_picked` holds every week's five ideas, keyed by week.
  * `sip_buckets` + `sip_holdings` hold every allocation ever proposed.

So this replays the real history rather than waiting for it. Nothing is
invented: an idea missing a price, stop or target is skipped, not completed
with a guess.

Usage
-----
    python3 backfill_picks_sip.py            # dry run
    python3 backfill_picks_sip.py --apply    # writes

Idempotent — both loggers replace their own period's rows, so re-running
rewrites rather than duplicates.
"""
from __future__ import annotations

import json
import sys

import db as _db
import tracker


def _week_to_date(week_key: str) -> str:
    """'2026-W34' (optionally engine-tagged) -> the Monday of that ISO week.

    The ledger is keyed by date, and stamping every historical week with
    today's date would pile the entire history onto one day — the same trap
    log_batch_to_all_signals hit when it stamped _today_ist() unconditionally
    and a backfill of 4 scans wrote all 56 rows on the day it ran.
    """
    import datetime
    part = week_key.split("-v")[0]
    try:
        y, w = part.split("-W")
        return datetime.date.fromisocalendar(int(y), int(w), 1).isoformat()
    except Exception:                                        # noqa: BLE001
        return part


def backfill_picks(apply: bool) -> int:
    with _db.connect() as c:
        try:
            rows = c.execute(
                "SELECT pick_date, picks FROM newspaper_stocks_picked "
                "ORDER BY pick_date").fetchall()
        except Exception as e:                               # noqa: BLE001
            print(f"  no newspaper_stocks_picked table ({e})")
            return 0

    total = 0
    for week, blob in rows:
        try:
            picks = json.loads(blob)
        except Exception:                                    # noqa: BLE001
            continue
        if not picks:
            continue
        usable = [p for p in picks
                  if (p.get("price") or p.get("entry"))
                  and (p.get("stop_loss") or p.get("sl"))
                  and (p.get("target") or p.get("t1"))]
        print(f"  {week:16} {len(picks):2} picks, {len(usable):2} with full levels")
        if apply and usable:
            ids = tracker.log_top5_picks(usable, week, date=_week_to_date(week))
            total += len([i for i in ids if i])
    return total


def backfill_sip(apply: bool) -> int:
    with _db.connect() as c:
        try:
            buckets = c.execute(
                "SELECT bucket, monthly_amount FROM sip_buckets ORDER BY bucket"
            ).fetchall()
        except Exception as e:                               # noqa: BLE001
            print(f"  no sip_buckets table ({e})")
            return 0
        holdings = {}
        for bname, _amt in buckets:
            # ref_price is what build_bucket() records at proposal time, but a
            # bucket that was actually bought carries buy_price, and one that
            # has only been marked-to-market carries last_price. Take whichever
            # exists, most-authoritative first, and SAY which one was used —
            # "the price the allocation was decided at" and "what it is worth
            # now" are different claims and must not be silently interchanged.
            holdings[bname] = c.execute(
                "SELECT symbol, allocated, ref_price, buy_price, last_price, rank "
                "FROM sip_holdings WHERE bucket=? ORDER BY rank", (bname,)).fetchall()

    total = 0
    for bname, amount in buckets:
        hs = holdings.get(bname) or []
        allocs, basis = [], {}
        for h in hs:
            symbol, allocated, ref, buy, last = h[0], h[1], h[2], h[3], h[4]
            price = ref or buy or last
            if not price:
                continue
            which = "ref_price" if ref else ("buy_price" if buy else "last_price")
            basis[which] = basis.get(which, 0) + 1
            allocs.append({
                "symbol": symbol,
                "price": price,
                "pct": (round((allocated or 0) / amount * 100, 1) if amount else None),
                "bucket": bname,
                "price_basis": which,
            })
        detail = ", ".join(f"{k}×{v}" for k, v in basis.items()) or "no usable price"
        print(f"  {bname:16} {len(hs):2} holdings, {len(allocs):2} priced ({detail})")
        if apply and allocs:
            ids = tracker.log_sip_bucket(allocs, bname)
            total += len([i for i in ids if i])
    return total


def main() -> int:
    apply = "--apply" in sys.argv
    tracker.init_db()

    print("Top 5 weekly picks")
    n_picks = backfill_picks(apply)
    print("\nSIP monthly allocations")
    n_sip = backfill_sip(apply)

    if not apply:
        print("\nDRY RUN — re-run with --apply to write")
        return 0

    print(f"\nwrote {n_picks} pick row(s) and {n_sip} SIP row(s)")
    with _db.connect() as c:
        for t in ("top5_pick", "sip_bucket"):
            n = c.execute("SELECT COUNT(*) FROM all_signals WHERE signal_type=?",
                          (t,)).fetchone()[0]
            print(f"  ledger now holds {n} {t} row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
