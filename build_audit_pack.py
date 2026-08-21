#!/usr/bin/env python3
"""
build_audit_pack.py — one document per signal engine: its rule, its record.

FOR AUDIT, NOT FOR MARKETING. The point is to make every engine answerable to
the same four questions:

    What fires it?  What is the horizon?  How is it graded?  What has it done?

Everything below is read from the code and the ledger at build time. The rule
text comes from tracker.REMARKS and standalone_scan's horizon tables, the record
from engine_evidence over data/all_signals.json. Nothing is typed in from
memory, so this file cannot drift from the system it describes — if an engine's
horizon changes, the next build says so.

    python3 build_audit_pack.py            # markdown to stdout
    python3 build_audit_pack.py --out X.md
    python3 build_audit_pack.py --pdf X.pdf
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
MYT = timezone(timedelta(hours=8))

# What each engine looks for. Written here because it is the ONE thing not
# recoverable from the ledger — a row records that a signal fired, never the
# condition that fired it. Kept beside the code it describes so a rule change
# and its documentation land in the same diff.
TRIGGERS = {
    "breakout":        "Price closes above a prior swing high with volume expansion; stop under the breakout base.",
    "cf_1h":           "1-hour channel break on commodities; entry on the break, stop at the opposite channel edge.",
    "4h":              "4-hour channel break; same structure as cf_1h on a slower clock.",
    "ai_4h":           "Model-scored 4-hour channel break — the AI layer ranks candidates the channel scan produced.",
    "ai_daily":        "Model-scored daily-close channel break.",
    "equity_measured": "Daily-close equity engine: the only engine whose edge was measured before it was published.",
    "ohl":             "Open ≈ Low, long only: today's open within 0.5% of today's low. Stop off the day's low; targets at 1.5R / 2.5R / 4R. Rejected when the gap exceeds 0.5%, under 30 daily bars exist, or any OHLC value is missing.",
    "commodity":       "Daily commodity scan across metals, energy and gas.",
    "intraday":        "Intraday momentum tier. RETIRED 2026-07-30 — left in the ledger, never re-enabled.",
    "magic":           "Investtech-style magic-levels screen, weekly, on the 200-name universe.",
    "magicmagic":      "Same screen, v1 engine: names 20–40% off the 52-week high. Overlaps `magic` and is reported inside it.",
    "multibagger":     "Weekly multibagger scan. A RESEARCH artefact — no capital is sized to it.",
    "ai_longterm":     "Own-the-business: multi-year compounding idea with a 200DMA structure stop. Research, not a trade.",
    "top5_pick":       "The paper's weekly front-page ranking. Research — and since 2026-08-21 it must clear MIN_RR to be published at all.",
    "sip_bucket":      "Monthly SIP allocation — what the SIP was instructed to buy. Not a trade signal.",
}

GRADING = """Every engine is graded the same way, which is what makes the records
comparable at all:

- **R-multiple** is the unit. A trade stopped out is −1R whatever its rupee size;
  one that made twice its risk is +2R. Rupee outcomes are not comparable across
  instruments, R is.
- **Stop at the level, not past it.** A fill worse than the stop is recorded at
  −1.00R; slippage is a separate question from expectancy and mixing them
  overstates the loss.
- **Time stop.** A position past its horizon is closed at that bar's close and
  booked at the real R, not dropped. Dropping unresolved trades biases the
  record toward fast movers.
- **Bars after the signal only.** A level counts only if price traded there
  while the position was open. Grading against bars that predate the signal is
  what produced the HINDALCO phantom stop at a price four days older than the
  signal itself.
- **Open positions are excluded** from win rate and expectancy, and the count of
  them is published beside every figure."""


def load_evidence():
    import engine_evidence
    rows = json.loads((ROOT / "data" / "all_signals.json").read_text())
    return engine_evidence.build(rows), rows


def horizons():
    import standalone_scan as ss
    return getattr(ss, "ENGINE_MAX_HOLD_HOURS", {}), getattr(ss, "MAX_HOLD_HOURS", {})


def hz(engine, tf_rows, eng_h, tf_h):
    if engine in eng_h:
        h = eng_h[engine]
        return f"{h}h (~{h // 24}d), set per engine"
    tfs = sorted({(r.get("timeframe") or "").upper() for r in tf_rows if r.get("timeframe")})
    got = [f"{tf} → {tf_h[tf]}h" for tf in tfs if tf in tf_h]
    return "; ".join(got) if got else "no horizon table entry — never time-stopped"


def build() -> str:
    ev, rows = load_evidence()
    eng_h, tf_h = horizons()
    import tracker
    now = datetime.now(MYT).strftime("%Y-%m-%d %H:%M MYT")

    by_engine = {}
    for r in rows:
        by_engine.setdefault(r.get("signal_type"), []).append(r)

    out = [
        "# Signal engine audit pack",
        "",
        f"**Generated {now}** · {len(rows)} ledger rows · "
        f"{len(ev['engines'])} engines reported",
        "",
        "Every figure is read from `data/all_signals.json` and the engine code at "
        "build time. Nothing is typed in from memory, so this document cannot "
        "drift from the system it describes.",
        "", "---", "", "## How grading works", "", GRADING, "", "---", "",
        "## Summary", "",
        "| Engine | Closed | Open | Win | Expectancy | t | Total R | Verdict | Funded |",
        "|---|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for e in ev["engines"]:
        funded = "no — measured loss" if e.get("suppressed") else (
            "research only" if not e["is_trade"] else "yes")
        tval = f"{e['t']:+.2f}" if e["t"] is not None else "—"
        out.append(f"| `{e['engine']}` | {e['n']} | {e['open_now']} | {e['win_rate']}% | "
                   f"{e['expectancy']:+.3f}R | {tval} | {e['total_r']:+.1f}R | "
                   f"{e['verdict']} | {funded} |")

    out += ["", f"*Verdict floor: an engine with fewer than {ev['min_n']} closed "
            "trades is reported UNPROVEN and no conclusion is drawn in either "
            "direction.*", "", "---", "", "## Engine by engine", ""]

    for e in ev["engines"]:
        name = e["engine"]
        rws = by_engine.get(name, []) + (
            by_engine.get("magicmagic", []) if name == "magic" else [])
        out += [
            f"### `{name}`", "",
            f"**What fires it.** {TRIGGERS.get(name, 'Not documented — this is a gap.')}", "",
            f"**Ledger description.** {tracker.REMARKS.get(name, '—')}", "",
            f"**Horizon.** {hz(name, rws, eng_h, tf_h)}", "",
            f"**Record.** {e['n']} closed, {e['open_now']} open. "
            f"Win rate {e['win_rate']}%. Expectancy {e['expectancy']:+.3f}R"
            + (f", t={e['t']:+.2f}" if e["t"] is not None else "")
            + f". Best {e['best']:+.2f}R, worst {e['worst']:+.2f}R. "
            f"Total {e['total_r']:+.1f}R.", "",
            f"**Verdict.** {e['verdict']} — {e['why']}", "",
        ]
        if e.get("suppressed"):
            out += ["**Capital.** Suppressed. This engine keeps firing and keeps "
                    "being scored, but receives no allocation in the paper "
                    "wallet.", ""]
        if not e["is_trade"]:
            out += ["**Capital.** Research artefact — no capital is sized to it "
                    "by design.", ""]
        out.append("")

    missing = [k for k in ev["engines"] if k["engine"] not in TRIGGERS]
    if missing:
        out += ["---", "", "## Gaps in this document", "",
                "These engines have no documented trigger, which is a real "
                "finding rather than a formatting problem — an engine nobody "
                "can describe cannot be audited:", ""]
        out += [f"- `{m['engine']}`" for m in missing] + [""]

    out += ["---", "",
            "*Not investment advice. Published so the engines can be argued "
            "with, which requires that their rules and their records sit in the "
            "same document.*"]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    ap.add_argument("--pdf")
    a = ap.parse_args()
    md = build()
    if a.out:
        Path(a.out).write_text(md)
        print(f"wrote {a.out}", file=sys.stderr)
    if a.pdf:
        src = a.out or "/tmp/_audit_pack.md"
        if not a.out:
            Path(src).write_text(md)
        try:
            subprocess.run(["pandoc", src, "-o", a.pdf, "--pdf-engine=weasyprint",
                            "-V", "geometry:margin=2cm"], check=True)
            print(f"wrote {a.pdf}", file=sys.stderr)
        except Exception as e:                        # noqa: BLE001
            print(f"pdf failed ({e}) — markdown is at {src}", file=sys.stderr)
    if not a.out and not a.pdf:
        print(md)


if __name__ == "__main__":
    main()
