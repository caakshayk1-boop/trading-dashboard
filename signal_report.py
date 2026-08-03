#!/usr/bin/env python3
"""
signal_report.py — the ledger: every alert the bot sent, and what happened.

Reads the all_signals table (which every scan writes to) and produces one
report in three renderings:

    build()      -> structured dict, the single source of truth
    to_telegram()-> Markdown block for the bot
    to_markdown()-> Obsidian note body

Run standalone:
    python signal_report.py                 # 30d to stdout
    python signal_report.py --days 90 --send        # Telegram
    python signal_report.py --days 90 --obsidian    # Obsidian vault
    python signal_report.py --days 90 --send --obsidian

Every metric is measured, not asserted. R-multiple is the unit throughout so
live results are directly comparable with backtest.py output.
"""

import argparse
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

IST = timezone(timedelta(hours=5, minutes=30))
log = logging.getLogger(__name__)

# Closed = anything that is neither still running nor manually voided. A
# denylist rather than an allowlist: the writers emit SL_HIT / T1_HIT /
# T2_HIT today, and a new exit status added later must not silently vanish
# from the ledger.
# VOID = rows that leaked before the time stop existed (see reconcile_positions.py).
# They were never managed as positions, so they carry no usable outcome and must
# not be averaged into expectancy. EXPIRED is deliberately absent: a time stop is
# a real result and backtest.py counts it, so the live ledger must too.
NOT_CLOSED = ("OPEN", "T1_HIT", "CANCELLED", "VOID")


def _fetch(days: int) -> pd.DataFrame:
    import tracker
    tracker.init_db()
    since = (datetime.now(IST).date() - timedelta(days=days)).isoformat()
    with tracker._conn() as c:
        return pd.read_sql(
            "SELECT date, signal_type, symbol, action, timeframe, entry, sl, "
            "target1, target2, rr, score, status, exit_price, pnl_pct, "
            "r_multiple FROM all_signals WHERE date >= ? ORDER BY date DESC",
            # Tuple, not list. pandas hands `params` straight to the DBAPI
            # cursor, and libsql_experimental only accepts a tuple — a list
            # raises "argument 'parameters': 'list' object cannot be converted
            # to 'PyTuple'", which killed the whole EOD ledger post.
            c, params=(since,),
        )


def _agg(r: pd.Series) -> dict:
    r = pd.to_numeric(r, errors="coerce").dropna()
    if r.empty:
        return {"n": 0, "win_rate": 0.0, "expectancy": 0.0,
                "total_r": 0.0, "best": 0.0, "worst": 0.0}
    return {
        "n": int(len(r)),
        "win_rate": round(float((r > 0).mean()) * 100, 1),
        "expectancy": round(float(r.mean()), 3),
        "total_r": round(float(r.sum()), 1),
        "best": round(float(r.max()), 2),
        "worst": round(float(r.min()), 2),
    }


def build(days: int = 30) -> dict:
    """Assemble the ledger. Everything downstream renders from this dict."""
    df = _fetch(days)
    if df.empty:
        return {"days": days, "generated": datetime.now(IST).isoformat(),
                "total": 0, "open": 0, "closed": 0, "by_type": {},
                "overall": _agg(pd.Series(dtype=float)), "recent": [],
                "open_rows": []}

    closed = df[~df["status"].isin(NOT_CLOSED) & df["r_multiple"].notna()]
    open_  = df[df["status"] == "OPEN"]

    by_type = {}
    for st, g in closed.groupby("signal_type"):
        by_type[st] = _agg(g["r_multiple"])

    recent = closed.head(15).to_dict("records")
    return {
        "days": days,
        "generated": datetime.now(IST).isoformat(),
        "total": int(len(df)),
        "open": int(len(open_)),
        "closed": int(len(closed)),
        "by_type": by_type,
        "overall": _agg(closed["r_multiple"]),
        "recent": recent,
        "open_rows": open_.head(20).to_dict("records"),
    }


def _f(v, dp=2):
    try:
        return f"{float(v):,.{dp}f}"
    except (TypeError, ValueError):
        return "—"


def to_telegram(rep: dict) -> str:
    o = rep["overall"]
    if rep["total"] == 0:
        return f"\U0001F4D2 *Signal Ledger* — no alerts in the last {rep['days']}d"

    edge = "profitable" if o["expectancy"] > 0 else "losing" if o["expectancy"] < 0 else "flat"
    out = [
        f"\U0001F4D2 *Signal Ledger* — last {rep['days']}d",
        f"_{rep['total']} sent · {rep['closed']} closed · {rep['open']} open_",
        "",
        f"*Overall:* win `{o['win_rate']}%` · exp `{o['expectancy']:+.3f}R` "
        f"· total `{o['total_r']:+.1f}R`  ({edge})",
        f"best `{o['best']:+.2f}R` · worst `{o['worst']:+.2f}R`",
    ]

    if rep["by_type"]:
        out += ["", "*By signal type*"]
        for st, v in sorted(rep["by_type"].items(),
                            key=lambda x: -x[1]["expectancy"]):
            out.append(f"`{st[:16]:16}` n=`{v['n']}` win `{v['win_rate']}%` "
                       f"exp `{v['expectancy']:+.2f}R`")

    if rep["recent"]:
        out += ["", "*Last closed*"]
        for r in rep["recent"][:8]:
            rm = r.get("r_multiple")
            mark = "✅" if (rm or 0) > 0 else "❌"
            out.append(f"{mark} `{str(r['symbol'])[:12]}` {r.get('action','')} "
                       f"{_f(r.get('entry'))} → {_f(r.get('exit_price'))} "
                       f"`{float(rm):+.2f}R`")

    if rep["open"]:
        out += ["", f"_{rep['open']} still open — not counted above_"]
    return "\n".join(out)


def to_markdown(rep: dict) -> str:
    o = rep["overall"]
    gen = rep["generated"][:16].replace("T", " ")
    md = [f"## \U0001F4D2 Signal Ledger — last {rep['days']}d",
          f"*Generated {gen} IST*", ""]

    if rep["total"] == 0:
        md.append("No alerts in this window.")
        return "\n".join(md)

    md += [
        "| Metric | Value |", "|---|---|",
        f"| Alerts sent | {rep['total']} |",
        f"| Closed | {rep['closed']} |",
        f"| Still open | {rep['open']} |",
        f"| Win rate | **{o['win_rate']}%** |",
        f"| Expectancy | **{o['expectancy']:+.3f}R** |",
        f"| Total | {o['total_r']:+.1f}R |",
        f"| Best / worst | {o['best']:+.2f}R / {o['worst']:+.2f}R |",
        "",
    ]

    if rep["by_type"]:
        md += ["### By signal type", "",
               "| Type | n | Win % | Expectancy | Total R |", "|---|---|---|---|---|"]
        for st, v in sorted(rep["by_type"].items(), key=lambda x: -x[1]["expectancy"]):
            md.append(f"| {st} | {v['n']} | {v['win_rate']}% | "
                      f"{v['expectancy']:+.3f}R | {v['total_r']:+.1f}R |")
        md.append("")

    if rep["recent"]:
        md += ["### Closed trades", "",
               "| Date | Symbol | Type | Side | Entry | Exit | R |",
               "|---|---|---|---|---|---|---|"]
        for r in rep["recent"]:
            md.append(
                f"| {r.get('date','')} | {r.get('symbol','')} | "
                f"{r.get('signal_type','')} | {r.get('action','')} | "
                f"{_f(r.get('entry'))} | {_f(r.get('exit_price'))} | "
                f"{float(r.get('r_multiple') or 0):+.2f}R |")
        md.append("")

    if rep["open_rows"]:
        md += ["### Open", "",
               "| Date | Symbol | Type | Side | Entry | SL | T1 |",
               "|---|---|---|---|---|---|---|"]
        for r in rep["open_rows"]:
            md.append(
                f"| {r.get('date','')} | {r.get('symbol','')} | "
                f"{r.get('signal_type','')} | {r.get('action','')} | "
                f"{_f(r.get('entry'))} | {_f(r.get('sl'))} | "
                f"{_f(r.get('target1'))} |")
    return "\n".join(md)


def to_obsidian(rep: dict) -> bool:
    """Write the ledger to 03-WEEKLY/YYYY-Www.md, replacing any prior block."""
    import obsidian_sync as ob

    today = datetime.now(IST).date()
    year, week, _ = today.isocalendar()
    path = f"03-WEEKLY/{year}-W{week:02d}.md"

    content, sha = ob._gh_get_file(path)
    if not content:
        content = f"# Week {week} · {year}\n\n"

    marker = "## \U0001F4D2 Signal Ledger"
    if marker in content:
        start = content.index(marker)
        end   = content.find("\n## ", start + 1)
        content = content[:start] + (content[end + 1:] if end != -1 else "")

    content = content.rstrip("\n") + "\n\n" + to_markdown(rep) + "\n"
    ok = ob._gh_put_file(path, content,
                         f"ledger: {rep['closed']} closed, "
                         f"{rep['overall']['expectancy']:+.3f}R [skip ci]", sha)
    log.info(f"signal_report: obsidian {'ok' if ok else 'FAILED'} → {path}")
    return ok


def send(days: int = 30, telegram: bool = True, obsidian: bool = False) -> dict:
    rep = build(days)
    if telegram:
        from telegram_bot import _post
        _post(to_telegram(rep))
    if obsidian:
        try:
            to_obsidian(rep)
        except Exception as e:
            log.warning(f"signal_report: obsidian write failed — {e}")
    return rep


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--send", action="store_true", help="post to Telegram")
    ap.add_argument("--obsidian", action="store_true", help="write to vault")
    a = ap.parse_args()

    if a.send or a.obsidian:
        send(a.days, telegram=a.send, obsidian=a.obsidian)
    else:
        print(to_telegram(build(a.days)))
