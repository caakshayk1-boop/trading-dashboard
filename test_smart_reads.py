#!/usr/bin/env python3
"""
test_smart_reads.py — the only place a model writes interpretation here.

Every check is a way this could publish something it should not: an invented
number, an invented name, a buy call, or prose generated from an article too
short to support it. The gate is shared with brief_engine, so these tests also
prove the wiring to it actually runs rather than being decorative.

The AI is a stub. No network, no key, no pytest.

Usage:
    python3 test_smart_reads.py
"""
from __future__ import annotations

import json
import sys

import smart_reads as sr

ARTICLE = {
    "title": "RBI holds repo rate at 6.5% for the tenth straight meeting",
    "summary": ("The Reserve Bank of India kept the repo rate unchanged at 6.5% on Friday, "
                "citing food inflation that remains above its 4% target. Governor Sanjay "
                "Malhotra said the stance stays withdrawal of accommodation. Economists at "
                "Nomura had expected a cut of 25 basis points."),
}

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


def stub(payload):
    """An AI that returns exactly this payload."""
    def _ai(prompt, max_tokens=None):
        return json.dumps(payload) if isinstance(payload, dict) else payload
    return _ai


GOOD = {
    "headline": "RBI holds repo rate at 6.5%",
    "bullets": ["The Reserve Bank of India kept the repo rate unchanged at 6.5%",
                "Governor Sanjay Malhotra said the stance stays withdrawal of accommodation"],
    "whyItMatters": "Food inflation above the 4% target keeps the Reserve Bank of India on hold.",
    "whatToWatch": "Whether food inflation falls back toward the 4% target.",
}


# ── The happy path has to actually work ──────────────────────────────────────

@check("a clean, source-grounded output publishes")
def _():
    out = sr.structure(ARTICLE, stub(GOOD))
    assert out is not None, "a good output was rejected"
    assert len(out["what_happened"]) == 2
    assert out["why_it_matters"]
    assert out["read_seconds"] >= 20


@check("fact and interpretation land in separately named fields")
def _():
    out = sr.structure(ARTICLE, stub(GOOD))
    # The template cannot render interpretation as reported fact if they are
    # never in the same bucket.
    assert "what_happened" in out and "why_it_matters" in out
    assert isinstance(out["what_happened"], list)


# ── Truthfulness, via the shared gate ────────────────────────────────────────

@check("an invented NUMBER is rejected")
def _():
    bad = dict(GOOD, whyItMatters="Inflation ran at 7.9% last month, the highest since 2013.")
    assert sr.structure(ARTICLE, stub(bad)) is None


@check("an invented NAME is rejected")
def _():
    # The exact failure the shared gate was built for.
    bad = dict(GOOD, bullets=["The Reserve Bank of India kept the repo rate unchanged at 6.5%",
                              "President Biden welcomed the decision"])
    assert sr.structure(ARTICLE, stub(bad)) is None


@check("numbers that ARE in the article pass")
def _():
    ok = dict(GOOD, whatToWatch="Whether the 25 basis point cut Nomura expected arrives later.")
    assert sr.structure(ARTICLE, stub(ok)) is not None


# ── It must never recommend ──────────────────────────────────────────────────

@check("a buy call is rejected even when every fact checks out")
def _():
    bad = dict(GOOD, whatToWatch="Buy banking stocks before the next meeting.")
    assert sr.structure(ARTICLE, stub(bad)) is None


@check("a price target is rejected")
def _():
    bad = dict(GOOD, whyItMatters="A target price of 6.0% on the repo looks reachable.")
    assert sr.structure(ARTICLE, stub(bad)) is None


@check("every advice word is actually caught")
def _():
    for word in ("buy", "sell", "short", "accumulate", "overweight",
                 "stop loss", "price target", "allocate", "recommend"):
        ev = {"headline": "x", "bullets": ["a"], "whyItMatters": f"We {word} this.",
              "whatToWatch": ""}
        assert sr.advice_reject(ev), f"{word!r} slipped through"


@check("ordinary reporting language is NOT mistaken for advice")
def _():
    # "sell-off" and "buyers" are description, not instruction. A gate that
    # blocks normal financial English would quietly disable the whole feature.
    ev = {"headline": "Bond yields rise", "bullets": ["Yields rose on Friday"],
          "whyItMatters": "The move reflects positioning, not policy.", "whatToWatch": ""}
    assert sr.advice_reject(ev) is None


# ── Refusing to try ──────────────────────────────────────────────────────────

@check("too little article means no attempt at all")
def _():
    # A model given three lines of context invents the rest. Declining is the
    # entire point of this module.
    thin = {"title": "Markets fall", "summary": "Stocks fell today."}
    assert sr.structure(thin, stub(GOOD)) is None


@check("no AI configured means no prose, not a crash")
def _():
    assert sr.structure(ARTICLE, None) is None


@check("unparseable model output is survived")
def _():
    for junk in ("", "I'm sorry, I can't help with that.", "{not json", None):
        assert sr.structure(ARTICLE, stub(junk)) is None


@check("an AI that raises does not take the build down")
def _():
    def boom(prompt, max_tokens=None):
        raise RuntimeError("429 rate limited")
    assert sr.structure(ARTICLE, boom) is None


# ── enrich() leaves the fallback intact ──────────────────────────────────────

@check("a rejected read is left exactly as it was")
def _():
    reads = [dict(ARTICLE)]
    bad = dict(GOOD, whyItMatters="Inflation hit 9.9%.")
    sr.enrich(reads, stub(bad))
    assert "smart" not in reads[0], "a rejected read was still decorated"
    assert reads[0]["title"] == ARTICLE["title"]


@check("enrich reports what it rejected rather than hiding it")
def _():
    reads = [dict(ARTICLE), dict(ARTICLE)]
    stats = sr.enrich(reads, stub(dict(GOOD, whatToWatch="Buy the dip.")))
    assert stats["structured"] == 0 and stats["rejected"] == 2


@check("the budget cap is respected")
def _():
    reads = [dict(ARTICLE) for _ in range(20)]
    stats = sr.enrich(reads, stub(GOOD), limit=3)
    assert stats["attempted"] == 3 and stats["structured"] == 3
    assert stats["total"] == 20
    assert sum(1 for r in reads if "smart" in r) == 3


def main() -> int:
    passed = failed = 0
    for name, fn in CHECKS:
        try:
            fn()
        except AssertionError as e:
            print(f"  FAIL  {name}  ({e})"); failed += 1
        except Exception as e:
            print(f"  ERROR {name}  ({type(e).__name__}: {e})"); failed += 1
        else:
            print(f"  PASS  {name}"); passed += 1
    print(f"\n{passed} passed · {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
