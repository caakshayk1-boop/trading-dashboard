#!/usr/bin/env python3
"""
audit_ledger_grades.py — read-only forensics on all_signals grading.

Answers one question: does each row's stored r_multiple agree with the row's own
entry / sl / exit_price, and does its status agree with where it actually exited?

READ-ONLY BY DEFAULT. It writes nothing. `--report` prints the full defect list;
`--csv PATH` dumps it for review. There is deliberately no --apply here: repairing
the ledger changes numbers that have already been published, so the repair lives in
a separate script that has to be run knowingly.

Three checks, each independent:

  A. exit_price == sl (to 1e-6) but r_multiple != -1.00
     An exit at the stop is -1R by definition, whatever the status says.

  B. status == T2_HIT but exit_price != target2 and pnl_pct < 0
     A target hit cannot be a loss.

  C. status == EXPIRED
     regrade.py excludes EXPIRED, and standalone_scan.py books it at last_close —
     the price when the job ran, not at the end of the signal's horizon. Flagged
     wholesale as ungraded rather than mis-graded.

Usage:
    python audit_ledger_grades.py --report
    python audit_ledger_grades.py --csv /tmp/ledger_defects.csv
    python audit_ledger_grades.py --report --days 30
"""

import argparse
import csv
import datetime as dt
import json
import math
import statistics
from pathlib import Path

SRC = Path(__file__).parent / "data" / "all_signals.json"
EPS = 1e-6
NOT_CLOSED = ("OPEN", "T1_HIT", "CANCELLED", "VOID")


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _same(a, b):
    a, b = _f(a), _f(b)
    return a is not None and b is not None and abs(a - b) < EPS


def true_r(row):
    """R implied by the row's own entry/sl/exit_price. None if uncomputable.

    Floored at -1R: a stop that is honoured cannot lose more than the risk it
    defined. A row exiting worse than its stop is a slippage question, not an
    expectancy question, and letting it through would understate the engine in
    the opposite direction from the defect being measured.
    """
    e, s, x = _f(row.get("entry")), _f(row.get("sl")), _f(row.get("exit_price"))
    if None in (e, s, x):
        return None
    risk = abs(e - s)
    if risk == 0:
        return None
    r = (x - e) / risk if row.get("action") == "BUY" else (e - x) / risk
    return max(r, -1.0)


def classify(row):
    """Return a defect code, or None if the row grades cleanly."""
    if row.get("status") == "EXPIRED":
        return "C_EXPIRED_MARKED_TO_LAST_CLOSE"
    stored = _f(row.get("r_multiple"))
    if stored is None or row.get("exit_price") is None:
        return None
    if _same(row.get("exit_price"), row.get("sl")) and abs(stored + 1.0) > 0.02:
        return "A_EXIT_AT_STOP_NOT_MINUS_1R"
    pnl = _f(row.get("pnl_pct"))
    if (row.get("status") == "T2_HIT"
            and not _same(row.get("exit_price"), row.get("target2"))
            and pnl is not None and pnl < 0):
        return "B_TARGET_HIT_BUT_LOST_MONEY"
    # D is the largest group and the mirror of B: the trade made money and the
    # ledger booked it as a stop-out. Checked on sign only, because pnl_pct is
    # the column that reproduces from entry/exit_price on every graded row --
    # it is the one signal of direction that is never self-contradictory.
    if pnl is not None and abs(pnl) > 0.01 and (pnl > 0) != (stored > 0):
        return "D_STATUS_CONTRADICTS_PNL_SIGN"
    return None


def load(days=None):
    rows = json.loads(SRC.read_text())
    if days is None:
        return rows
    cut = dt.date.today() - dt.timedelta(days=days)
    out = []
    for r in rows:
        try:
            if dt.date.fromisoformat(str(r.get("date"))[:10]) >= cut:
                out.append(r)
        except (TypeError, ValueError):
            continue
    return out


def summarise(rows, label, regrade=False, drop_expired=False):
    vals = []
    for r in rows:
        if r.get("status") in NOT_CLOSED:
            continue
        if drop_expired and r.get("status") == "EXPIRED":
            continue
        v = true_r(r) if regrade else _f(r.get("r_multiple"))
        if v is not None:
            vals.append(v)
    if not vals:
        print(f"{label:42} (no rows)")
        return
    n = len(vals)
    mu = sum(vals) / n
    sd = statistics.stdev(vals) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 1 else 0.0
    t = mu / se if se else 0.0
    win = 100 * sum(1 for v in vals if v > 0) / n
    print(f"{label:42} n={n:4} exp={mu:+.3f}R  SE={se:.3f}  t={t:+.2f}  "
          f"win={win:4.1f}%  total={sum(vals):+.1f}R")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None,
                    help="restrict to the last N days (default: whole ledger)")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--csv", metavar="PATH")
    args = ap.parse_args()

    rows = load(args.days)
    defects = [(classify(r), r) for r in rows]
    defects = [(c, r) for c, r in defects if c]

    scope = f"last {args.days}d" if args.days else "full ledger"
    print(f"\n=== ledger grade audit — {scope} — {len(rows)} rows ===\n")

    counts = {}
    r_overstated = 0.0
    for code, r in defects:
        counts[code] = counts.get(code, 0) + 1
        if code.startswith(("A_", "B_")):
            stored, actual = _f(r.get("r_multiple")), true_r(r)
            if stored is not None and actual is not None:
                r_overstated += stored - actual

    for code, n in sorted(counts.items()):
        print(f"  {code:36} {n:4}")
    print(f"\n  R overstated by A+B defects: {r_overstated:+.1f}R")

    print()
    summarise(rows, "AS PUBLISHED")
    summarise(rows, "drop EXPIRED only", drop_expired=True)
    summarise(rows, "re-grade R, keep EXPIRED", regrade=True)
    summarise(rows, "re-grade R, drop EXPIRED", regrade=True, drop_expired=True)

    if args.report:
        print("\n--- defect rows ---")
        for code, r in sorted(defects, key=lambda x: str(x[1].get("date")), reverse=True):
            if code.startswith("C_"):
                continue
            print(f"{str(r.get('date'))[:10]}  {str(r.get('symbol'))[:12]:12} "
                  f"{str(r.get('status')):10} {str(r.get('action')):4} "
                  f"entry={r.get('entry')} sl={r.get('sl')} exit={r.get('exit_price')} "
                  f"pnl%={r.get('pnl_pct')} storedR={r.get('r_multiple')} "
                  f"trueR={true_r(r)}  [{code}]")

    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["defect", "id", "date", "symbol", "signal_type", "status",
                        "action", "entry", "sl", "target2", "exit_price", "pnl_pct",
                        "stored_r", "true_r", "regraded_at"])
            for code, r in defects:
                w.writerow([code, r.get("id"), r.get("date"), r.get("symbol"),
                            r.get("signal_type"), r.get("status"), r.get("action"),
                            r.get("entry"), r.get("sl"), r.get("target2"),
                            r.get("exit_price"), r.get("pnl_pct"),
                            r.get("r_multiple"), true_r(r), r.get("regraded_at")])
        print(f"\nwrote {len(defects)} rows to {args.csv}")


if __name__ == "__main__":
    main()
