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

import os
import sys

# ── THIS SUITE MEASURES TEXT LENGTH AND MUST NOT NEED A BOT TOKEN ───────────
#
# daily_brief imports telegram_bot, which imports config, which RAISES at
# import time if TELEGRAM_TOKEN is unset. So merely importing the module under
# test demanded a live credential — and when this file was wired into
# newspaper.yml, a workflow that builds a static page and has no business
# holding a bot token, the whole newspaper build went red on a missing secret.
#
# Placeholders are set only when the real ones are absent, so a local run with
# a real .env is untouched. Nothing here sends: every check calls _fit() and
# counts characters. A token that cannot possibly authenticate is the correct
# thing for a test that must never reach the network.
os.environ.setdefault("TELEGRAM_TOKEN", "test-token-not-a-real-credential")
os.environ.setdefault("TELEGRAM_CHAT_ID", "0")

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


# ── Links point at the site that owns the section ────────────────────────────
# Every link in this brief said news.askakshay.com, including the ones under
# Trade Ideas, the Signal Log and Performance — three sections that moved to
# signal.askakshay.com. A reader following "full board" from the order book
# landed on the newspaper.

@check("the ledger, the board and the engines link to the signal site")
def _():
    for key in ("ideas", "signals", "performance", "engines"):
        assert D._link(key).startswith(D.SIGNAL_SITE), \
            f"{key} -> {D._link(key)}, not the signal site"


@check("the paper's own sections still link to the paper")
def _():
    for key in ("wire", "world", "health"):
        assert D._link(key).startswith(D.NEWS_SITE), \
            f"{key} -> {D._link(key)}, not the newspaper"


@check("an unknown section links somewhere real rather than raising")
def _():
    # A mistyped key must not be able to stop the brief from sending.
    assert D._link("no_such_section") == D.NEWS_SITE


@check("no section names a site the ownership table does not know")
def _():
    # A hardcoded host anywhere in the builder is a link that will rot in
    # place. The table is the only permitted source.
    import inspect, re
    src = inspect.getsource(D.build_section_brief)
    stray = re.findall(r"[\"']([a-z]+\.askakshay\.com)", src)
    assert not stray, f"hardcoded hosts in build_section_brief: {set(stray)}"


# ── APEX is a section, not a second notification ─────────────────────────────

@check("send_brief posts the brief and nothing else alongside it")
def _():
    import inspect
    src = inspect.getsource(D.send_brief)
    # The newspaper-link post is gated on RAILWAY_PUBLIC_DOMAIN, which is not
    # set in any workflow; the APEX one was not gated on anything.
    assert "_post(apex_digest)" not in src, \
        "the APEX digest is being sent as its own message again"


@check("APEX rides inside the night brief")
def _():
    import inspect
    src = inspect.getsource(D.build_section_brief)
    assert "_build_apex_digest()" in src, "APEX is not rendered in the brief"


@check("APEX prints no P&L when no starting balance is known")
def _():
    # The old code read `bal - 2000.0` — a baseline typed into an expression,
    # with nothing anywhere saying the account started there.
    import inspect
    src = inspect.getsource(D._build_apex_digest)
    # Comments strippped first: the docstring explains the old bug and names
    # the constant, which is the point of writing it down.
    code = "\n".join(l for l in src.splitlines()
                     if not l.lstrip().startswith("#"))
    assert "2000" not in code, "the hardcoded APEX baseline is back"
    assert "APEX_START_BALANCE" in code, "no way to state a baseline deliberately"


@check("the brief's exit ladder describes the board underneath it")
def _():
    """The one line in the brief a reader would act on, and it was wrong twice.

    It read: "Ladder: T1 books half, the rest runs to T2, then the stop trails
    to T1 and never lower."

      · "T1 books half" — swing_rulebook.LADDER is 20/40/40. T1 books a fifth.
      · "the rest runs to T2" — many rows have no T2. The engine files three
        targets and the ones inside 0.5R of the one before are not separate
        exits, so build_ladder collapses them. SPLPETRO renders on the ledger
        card as "All at 938.02 · the only target" while the brief promised a
        second rung the position does not have.

    The sentence is now counted off the admitted tickets themselves, so it
    cannot describe a ladder no row on the board is on.
    """
    import inspect
    src = inspect.getsource(D.build_section_brief)
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "T1 books half" not in code, "the half-of-the-position claim is back"
    assert "_rungs = [len(t.get(\"legs\")" in code, \
        "the sentence is not counted off the tickets"
    assert "20% at T1" in code, "the ladder no longer states the real fractions"

    # And the three fractions in the sentence must be the ones the sizer uses.
    import swing_rulebook as RB
    assert [f for _, f in RB.LADDER] == [0.20, 0.40, 0.40], \
        f"LADDER moved to {RB.LADDER} — the brief's sentence has to move with it"


@check("a board of one-target names is not told it has a T2")
def _():
    """Exercised through the real builder with the book stubbed, because the
    branch is chosen by the tickets and nothing else on the page can reach it."""
    one_leg = {"symbol": "SPLPETRO", "qty": 100, "entry": 771.05, "notional": 77105,
               "notional_pct": 0.8, "reward_risk": 1.6,
               "legs": [{"label": "T1", "price": 938.02, "qty": 100}]}
    three_leg = dict(one_leg, symbol="KIRLOSENG",
                     legs=[{"label": "T1"}, {"label": "T2"}, {"label": "T3"}])

    def book(admitted):
        return {"capital": 10_000_000, "admitted": admitted, "deferred": [],
                "rejected": [], "duplicates": [],
                "state": {"deployed": 77105, "deployed_pct": 0.8, "cash": 9_922_895,
                          "heat": 10000, "heat_pct": 0.1, "heat_cap": 600000,
                          "deployed_cap": 8_000_000, "at_capacity": False}}

    real = D._mandate_book
    try:
        D._mandate_book = lambda: book([one_leg])
        txt = D.build_section_brief("morning")
        assert "runs to T2" not in txt, "a one-target board was promised a second rung"
        assert "ONE target" in txt, f"the single target is not stated:\n{txt[:400]}"

        D._mandate_book = lambda: book([one_leg, three_leg])
        txt = D.build_section_brief("morning")
        assert "20% at T1" in txt, "a mixed board loses the real ladder"
        assert "1 of 2 carry a single target" in txt, \
            f"the mixed board does not count its one-target names:\n{txt[:400]}"

        D._mandate_book = lambda: book([three_leg, three_leg])
        txt = D.build_section_brief("morning")
        assert "carry a single target" not in txt, \
            "a board with no single-target names says otherwise"
    finally:
        D._mandate_book = real


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
