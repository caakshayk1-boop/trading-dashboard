#!/usr/bin/env python3
"""
test_brief_fit.py — the brief must never exceed what Telegram will accept.

Telegram rejects a sendMessage over 4096 characters OUTRIGHT. It does not
truncate. So an over-long brief does not arrive shortened — it does not arrive
at all, and the only trace is a send error nobody reads.

_fit() used to end with `return render(2, False)`: it measured the text, found
it still too long, and returned it anyway. Nothing was watching, because until
2026-08-27 nothing had ever grown enough to reach that branch. Adding the
CAREERS section reached it immediately — the evening brief came to 4,104
characters — and the failure would have been a silent non-delivery.

These checks stand between that branch and production.

Usage:
    python test_brief_fit.py
"""
from __future__ import annotations

import sys

import daily_brief as D

TELEGRAM_HARD_CAP = 4096

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


@check("TG_LIMIT leaves headroom under Telegram's hard 4096")
def _():
    assert D.TG_LIMIT < TELEGRAM_HARD_CAP, \
        f"TG_LIMIT {D.TG_LIMIT} is not below the {TELEGRAM_HARD_CAP} hard cap"


@check("every real slot builds a sendable brief")
def _():
    for slot in ("morning", "midday", "evening"):
        n = len(D.build_section_brief(slot))
        assert n <= TELEGRAM_HARD_CAP, f"{slot} brief is {n} chars"


@check("_fit NEVER returns over the limit, even when nothing can be dropped")
def _():
    # The exact branch that used to lie. One undroppable 20k line: no lever
    # can help, so the fitter must hard-trim rather than hand back an
    # unsendable string.
    out = D._fit(["X" * 20000])
    assert len(out) <= D.TG_LIMIT, \
        f"_fit returned {len(out)} chars — the silent non-delivery bug is back"


@check("a hard trim cuts on a line boundary, never mid-Markdown")
def _():
    # An unbalanced * makes Telegram 400 the whole message: the same silent
    # non-delivery, one step later.
    lines = [f"*line {i} bold*" for i in range(600)]
    out = D._fit(lines)
    assert len(out) <= D.TG_LIMIT
    assert out.count("*") % 2 == 0, "hard trim left an unbalanced Markdown pair"


@check("_Opt gives up its detail before the section disappears")
def _():
    opt = D._Opt(["ESSENTIAL"], ["DETAIL " + "y" * 4000])
    out = D._fit([opt])
    assert "ESSENTIAL" in out, "the summary was dropped instead of the detail"
    assert "DETAIL" not in out, "oversized detail survived"


@check("_Opt keeps its detail when there is room")
def _():
    out = D._fit([D._Opt(["ESSENTIAL"], ["DETAIL"])])
    assert "ESSENTIAL" in out and "DETAIL" in out


@check("careers reports the counts in every slot, rows or no rows")
def _():
    # The counts ARE the answer to "what happened to new and existing roles".
    # Rows are a nice-to-have; losing them is fine, losing the answer is not.
    for slot in ("morning", "evening"):
        b = D.build_section_brief(slot)
        assert "CAREERS" in b, f"{slot} lost the careers section entirely"
        assert ("NEW today" in b), f"{slot} careers lost its new-roles count"
        assert "Standing pipeline" in b, f"{slot} careers lost the live count"


def main() -> int:
    failed = 0
    for name, fn in CHECKS:
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as e:
            print(f"FAIL  {name}\n      {e}")
            failed += 1
    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
