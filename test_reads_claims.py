#!/usr/bin/env python3
"""
test_reads_claims.py: the Weekly Reads interpretation checks.

The 2026-10-10 edition shipped a 40% margin described as "four-fifths", a
65th-percentile PE called cheap against its own history, and a fund fee
described as billed to the investor. Matching numbers against the dossier
could not see any of it. These checks read the interpretation.

Usage:
    python test_reads_claims.py
"""
import json
import os
import sys

os.environ.setdefault("GROQ_API_KEY", "x")
from weekly_reads import claim_problems  # noqa: E402

fails = 0


def check(name, ok):
    global fails
    print(("PASS " if ok else "FAIL ") + name)
    fails += 0 if ok else 1


check("four-fifths of a 40% margin is caught",
      bool(claim_problems("With an EBIT margin of 40 %, four-fifths of revenue remain.")))
check("40 paise wording passes",
      not claim_problems("With an EBIT margin of 40%, 40 paise of every rupee remain."))
check("65th percentile called cheap vs own past is caught",
      bool(claim_problems("At the 65th percentile it is cheap relative to its own past.")))
check("65th percentile with 'higher than 35%' is caught",
      bool(claim_problems("The PE sits at the 65th percentile, higher than 35% of observations.")))
check("65th percentile 'higher than about 65%' passes",
      not claim_problems("The PE sits at the 65th percentile, higher than about 65% of its history."))
check("19th percentile cheap vs own past passes",
      not claim_problems("At the 19th percentile it is cheap against its own history."))
check("bad ordinal is caught", bool(claim_problems("It ranks 82th among peers.")))
check("good ordinals pass", not claim_problems("It ranks 82nd, 21st and 13th."))
check("'higher than 100%' is caught",
      bool(claim_problems("The multiple is higher than 100% of observations.")))
check("allegation language is caught",
      bool(claim_problems("The profit comes from accounting manipulation by the promoter.")))
check("quarterly fund billing is caught",
      bool(claim_problems("The fee is billed quarterly and deducted from the investor's account.")))

# The published edition itself must pass.
d = json.load(open(os.path.join(os.path.dirname(__file__), "docs", "weekly_reads.json")))
for s in d["editions"][0]["studies"]:
    p = claim_problems(s["study"])
    check(f"published study {s['sym']} passes ({p[:1]})", not p)

sys.exit(1 if fails else 0)
