#!/usr/bin/env python3
"""
build_log.py — the public build log, generated from git history.

Why from git
------------
A hand-kept changelog is a second place to remember to write, and the one that
gets abandoned first. Every change to this site already carries a message
explaining what changed and why; this reads those and publishes them.

That also makes it honest in a way a curated list is not: it cannot quietly
omit the week nothing shipped, and it cannot describe work that was never
committed. If the log looks thin, the month was thin.

Only `feat:` and `fix:` commits appear. `chore:` and `data:` are the daily
bot writing signals and newspapers — hundreds of them, none of which is a
change to the product.
"""
from __future__ import annotations

import re
import subprocess

# Conventional-commit prefixes worth publishing, and how each is labelled.
KINDS = {"feat": "Shipped", "fix": "Fixed", "perf": "Faster", "refactor": "Reworked"}


def _run(args: list[str]) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=20, check=False).stdout
    except Exception:                                        # noqa: BLE001
        return ""


def build(limit: int = 60) -> list[dict]:
    """Recent product changes, newest first. Empty list on any failure — a
    missing build log must never fail the daily build."""
    raw = _run(["git", "log", f"-{limit * 6}", "--no-merges",
                "--date=short", "--pretty=format:%H%x1f%ad%x1f%s%x1f%b%x1e"])
    if not raw:
        return []

    out: list[dict] = []
    for rec in raw.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        parts = rec.split("\x1f")
        if len(parts) < 3:
            continue
        sha, date, subject = parts[0], parts[1], parts[2]
        body = parts[3] if len(parts) > 3 else ""

        m = re.match(r"^(feat|fix|perf|refactor)(\([^)]*\))?:\s*(.+)$", subject)
        if not m:
            continue

        # The first paragraph of the body is the "why". Co-author trailers and
        # the rest of the essay stay in git where they belong.
        why = ""
        for para in body.split("\n\n"):
            para = " ".join(l.strip() for l in para.strip().splitlines()).strip()
            if para and not para.startswith("Co-Authored-By"):
                why = para
                break

        out.append({
            "sha": sha[:7],
            "date": date,
            "kind": KINDS[m.group(1)],
            "title": m.group(3).strip(),
            "why": why[:340],
        })
        if len(out) >= limit:
            break
    return out


if __name__ == "__main__":
    rows = build(12)
    print(f"{len(rows)} product changes")
    for r in rows[:6]:
        print(f"  {r['date']}  {r['kind']:<8} {r['title'][:64]}")
