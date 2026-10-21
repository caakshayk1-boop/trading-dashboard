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

# A02 (alpha audit, 21 Oct 2026): the 17 Oct SBI Funds study shipped both of these.
check("69.9% margin as 'seven paise' is caught",
      bool(claim_problems("Net profit margin 69.9 % – after tax, roughly seven paise of each revenue rupee stay as profit.")))
check("69.9% margin as '70 paise' passes",
      not claim_problems("Net profit margin 69.9 % – roughly 70 paise of each revenue rupee stay as profit."))
check("48th percentile as 'above its historical average' is caught",
      bool(claim_problems("The PE sits at the 48th percentile of its own history, slightly above its historical average.")))
check("46th percentile as 'above the median' is caught",
      bool(claim_problems("At the 46th percentile of its own range, slightly above the median.")))
check("58th percentile as 'above its median' passes",
      not claim_problems("At the 58th percentile of its own history, the stock trades above its median multiple."))
check("a margin change in % (not points) is caught",
      bool(claim_problems("The EBIT margin improved 1.1 % year on year.")))
check("a reversed sign is caught", bool(claim_problems("EPS grew -3.2% last year.")))
check("'more than half' of a 55.7% margin passes",
      not claim_problems("An EBIT margin of 73.6 % and net margin of 55.7 % turn more than half of every rupee into profit."))
check("a ROCE above 100% is not a share of observations",
      not claim_problems("At more than 100 % ROCE the firm earns over a rupee per rupee employed."))
check("royalties billed quarterly in a music business are not fund fees",
      not claim_problems("Royalties are billed quarterly to the platforms."))

from weekly_reads import review_problems  # noqa: E402
check("an asset manager's 'capital employed' story needs primary review",
      bool(review_problems({"study": "Capital employed rose as fund flows grew.", "sector": "Financial Services", "name": "SBI Funds Management Ltd."})))
check("the same words for a manufacturer do not",
      not review_problems({"study": "Capital employed rose with the new plant.", "sector": "Industrials", "name": "Acme Engineering Ltd."}))

# A normal weekly write keeps the corrections log (review finding on #55): run main() against a copy with the model
# stubbed out and the week already written, then a forced rewrite with no studies, and check the log is still there.
import shutil, tempfile, pathlib  # noqa: E401,E402
import weekly_reads as W  # noqa: E402
tmp = pathlib.Path(tempfile.mkdtemp())
src = pathlib.Path(os.path.dirname(__file__)) / "docs" / "weekly_reads.json"
shutil.copy(src, tmp / "weekly_reads.json")
before = json.load(open(src)).get("corrections", [])
W.OUT = tmp / "weekly_reads.json"
W.SCREEN = tmp / "screen.json"
(tmp / "screen.json").write_text(json.dumps({"rows": [{"sym": "ZZZ", "name": "Z Ltd", "sector": "Industrials", "mcap_cr": 9000}]}))
W.study_for = lambda r: {"sym": r["sym"], "name": r["name"], "sector": r["sector"], "industry": None, "mcap_cr": 9000, "price": 10, "words": 1, "read_minutes": 1, "facts": [], "study": "x", "model": "stub", "approximate_figures": [], "review": {"status": "machine_checked", "problems": [], "checked_at": "2026-10-24", "reviewed_by_person": False}}
W.pick = lambda rows, recent, seed: rows
W.send_to_telegram = lambda e: None
W.time.sleep = lambda s: None
W.saturday_of = lambda d: __import__("datetime").date(2099, 1, 3)
W.main()
after = json.load(open(W.OUT)).get("corrections", [])
check("a new weekly edition keeps every corrections entry", after == before and len(after) > 0)

# Every published study either passes today's checks or is marked under review with its reasons, and logged as a
# correction. A study is never silently corrected in place.
d = json.load(open(os.path.join(os.path.dirname(__file__), "docs", "weekly_reads.json")))
logged = {(c["week"], c["sym"]) for c in d.get("corrections", [])}
for e in d["editions"]:
    for s in e["studies"]:
        p = review_problems(s)
        r = s.get("review") or {}
        if p:
            check(f"{e['week']} {s['sym']}: under review, with reasons, and logged ({p[:1]})",
                  r.get("status") == "under_review" and r.get("problems") == p and (e["week"], s["sym"]) in logged)
        else:
            check(f"{e['week']} {s['sym']}: passes", r.get("status") in (None, "machine_checked"))

sys.exit(1 if fails else 0)
