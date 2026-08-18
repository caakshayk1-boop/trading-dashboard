#!/usr/bin/env python3
"""
test_data_health.py — the honesty gate on the freshness vocabulary.

Every case here exists because the audit of 2026-08-18 found the live site
capable of presenting data as more current than it was. The rules these
assertions pin are the ones that make that impossible:

  * a failed newer attempt behind valid data is DEGRADED, never STALE
  * a partial build is DEGRADED, never a complete one
  * an unreadable timestamp is DEGRADED, never FRESH
  * when several conditions fire, the WORST wins

Offline. No network, no database, no pytest.

Usage:
    python3 test_data_health.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import data_health as dh

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def ago(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat()


def ahead(**kw) -> str:
    return (NOW + timedelta(**kw)).isoformat()


CHECKS: list[tuple[str, callable]] = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


def daily(**kw):
    """A dataset that refreshes every 24h, with sensible defaults."""
    base = dict(source="test", expected_refresh_hours=24, now=NOW)
    base.update(kw)
    return dh.assess("Test dataset", **base)


# ── The six statuses ─────────────────────────────────────────────────────────

@check("built minutes ago on a daily refresh is LIVE")
def _():
    assert daily(generated_at=ago(minutes=30))["status"] == dh.LIVE


@check("built 10h ago on a daily refresh is FRESH, not LIVE")
def _():
    assert daily(generated_at=ago(hours=10))["status"] == dh.FRESH


@check("built 40h ago on a daily refresh is STALE")
def _():
    assert daily(generated_at=ago(hours=40))["status"] == dh.STALE


@check("no payload and no attempt history is UNAVAILABLE")
def _():
    assert daily(generated_at=None)["status"] == dh.UNAVAILABLE


@check("no payload but a recorded failure is FAILED, not UNAVAILABLE")
def _():
    b = daily(generated_at=None, job={"status": "failed", "detail": "yahoo 503"})
    assert b["status"] == dh.FAILED
    assert "yahoo 503" in b["headline"]


# ── The rule the audit was actually about ────────────────────────────────────

@check("valid data behind a FAILED newer attempt is DEGRADED, not STALE")
def _():
    b = daily(generated_at=ago(hours=6),
              job={"status": "failed", "detail": "only 50/750 priced",
                   "attempted_after_serve": True})
    assert b["status"] == dh.DEGRADED, b["status"]
    assert b["attempt_after_serve"] is True


@check("a failure OLDER than the served payload does not degrade it")
def _():
    # The build recovered. Reporting DEGRADED forever after one bad night
    # would train the reader to ignore the badge, which is worse than no badge.
    b = daily(generated_at=ago(hours=2),
              job={"status": "failed", "detail": "transient",
                   "attempted_after_serve": False})
    assert b["status"] == dh.LIVE, b["status"]


@check("a successful attempt never degrades anything")
def _():
    b = daily(generated_at=ago(hours=2),
              job={"status": "success", "detail": "750 companies"})
    assert b["status"] == dh.LIVE


# ── Coverage: the 750-vs-50 contradiction ────────────────────────────────────

@check("50 of 750 records is DEGRADED even when built seconds ago")
def _():
    b = daily(generated_at=ago(minutes=1), record_count=50, expected_records=750)
    assert b["status"] == dh.DEGRADED, b["status"]
    assert b["coverage"] == "50/750"
    assert b["coverage_pct"] == 6.7


@check("full coverage on a fresh build stays LIVE")
def _():
    b = daily(generated_at=ago(minutes=1), record_count=750, expected_records=750)
    assert b["status"] == dh.LIVE
    assert b["coverage_pct"] == 100.0


@check("coverage just above the floor is not degraded")
def _():
    b = daily(generated_at=ago(minutes=1), record_count=680, expected_records=750,
              coverage_floor=0.9)
    assert b["status"] == dh.LIVE, b["status"]


@check("coverage just below the floor is degraded")
def _():
    b = daily(generated_at=ago(minutes=1), record_count=674, expected_records=750,
              coverage_floor=0.9)
    assert b["status"] == dh.DEGRADED


@check("coverage is not reported when the universe size is unknown")
def _():
    # Inventing a denominator would be worse than omitting one: it would put a
    # confident "50/50" next to a build that lost 700 rows.
    b = daily(generated_at=ago(hours=1), record_count=50)
    assert b["coverage"] is None and b["coverage_pct"] is None


# ── Unreadable and impossible timestamps ─────────────────────────────────────

@check("an unreadable build timestamp is DEGRADED, never FRESH")
def _():
    b = daily(generated_at="not-a-date", record_count=10)
    assert b["status"] == dh.DEGRADED, b["status"]
    assert b["freshness_age"] == "unknown vintage"


@check("a payload stamped in the future reports clock skew, not freshness")
def _():
    b = daily(generated_at=ahead(hours=3))
    assert b["freshness_age"] == "clock skew"


@check("a naive timestamp is read as UTC, not as local time")
def _():
    naive = (NOW - timedelta(hours=2)).replace(tzinfo=None).isoformat()
    assert daily(generated_at=naive)["freshness_age_hours"] == 2.0


@check("an IST-stamped payload is not string-compared against a UTC attempt")
def _():
    # +05:30 stamps sort AFTER a UTC stamp of the same instant as raw strings.
    # This is the 5.5-hour hole that could hide a failed overnight rebuild.
    ist = (NOW - timedelta(hours=1)).astimezone(timezone(timedelta(hours=5, minutes=30)))
    assert abs(daily(generated_at=ist.isoformat())["freshness_age_hours"] - 1.0) < 0.01


# ── Severity: the worst condition wins ───────────────────────────────────────

@check("stale AND partial reports DEGRADED, the worse of the two")
def _():
    b = daily(generated_at=ago(hours=100), record_count=50, expected_records=750)
    assert b["status"] == dh.DEGRADED


@check("both reasons survive into notes even though one status is reported")
def _():
    b = daily(generated_at=ago(hours=100), record_count=50, expected_records=750)
    assert len(b["notes"]) == 2, b["notes"]
    assert any("older than" in n for n in b["notes"])
    assert any("50/750" in n for n in b["notes"])


@check("an explicit error degrades an otherwise perfect dataset")
def _():
    b = daily(generated_at=ago(minutes=1), error="price feed returned NaN")
    assert b["status"] == dh.DEGRADED
    assert "NaN" in b["headline"]


@check("severity ordering is the documented one")
def _():
    order = [dh.LIVE, dh.FRESH, dh.STALE, dh.DEGRADED, dh.FAILED, dh.UNAVAILABLE]
    assert [dh.SEVERITY[s] for s in order] == [0, 1, 2, 3, 4, 5]


@check("is_current is false for everything from STALE downwards")
def _():
    assert daily(generated_at=ago(hours=1))["is_current"] is True
    assert daily(generated_at=ago(hours=40))["is_current"] is False
    assert daily(generated_at=None)["is_current"] is False


# ── Weekly artefacts must not be judged on a daily clock ─────────────────────

@check("a 3-day-old weekly screen is FRESH, not STALE")
def _():
    b = dh.assess("Stock screen", source="yahoo", expected_refresh_hours=168,
                  generated_at=ago(days=3), now=NOW)
    assert b["status"] == dh.FRESH, b["status"]
    assert b["expected_refresh"] == "weekly"


@check("a 9-day-old weekly screen is STALE")
def _():
    b = dh.assess("Stock screen", source="yahoo", expected_refresh_hours=168,
                  generated_at=ago(days=9), now=NOW)
    assert b["status"] == dh.STALE


@check("LIVE is relative to the refresh interval, not an absolute clock")
def _():
    # 24h into a weekly cycle is still LIVE; 24h into a daily cycle is not.
    weekly = dh.assess("W", source="s", expected_refresh_hours=168,
                       generated_at=ago(hours=24), now=NOW)
    day = daily(generated_at=ago(hours=24))
    assert weekly["status"] == dh.LIVE and day["status"] == dh.FRESH


# ── Phrasing is centralised so two sections cannot disagree ──────────────────

@check("age phrasing is one vocabulary")
def _():
    assert dh.humanise_age(0.01) == "just built"
    assert dh.humanise_age(0.5) == "30 min old"
    assert dh.humanise_age(5) == "5h old"
    assert dh.humanise_age(72) == "3 days old"
    assert dh.humanise_age(None) == "unknown vintage"


@check("interval phrasing is one vocabulary")
def _():
    assert dh.humanise_interval(24) == "daily"
    assert dh.humanise_interval(168) == "weekly"
    assert dh.humanise_interval(6) == "every 6h"
    assert dh.humanise_interval(0.25) == "every 15 min"
    assert dh.humanise_interval(None) == "on demand"


@check("the twelve fields the spec named are all present")
def _():
    b = daily(generated_at=ago(hours=1), record_count=1, expected_records=2,
              build_version="v3", fallback=True)
    for f in ("dataset", "source", "last_successful_update", "last_attempted_update",
              "expected_refresh", "freshness_age", "status", "record_count",
              "coverage", "error", "fallback_used", "build_version"):
        assert f in b, f
    assert b["fallback_used"] is True and b["build_version"] == "v3"


@check("timestamps are normalised to UTC on the way out")
def _():
    ist = (NOW - timedelta(hours=1)).astimezone(timezone(timedelta(hours=5, minutes=30)))
    out = daily(generated_at=ist.isoformat())["last_successful_update"]
    assert out.endswith("+00:00"), out


# ── The registry ─────────────────────────────────────────────────────────────

@check("snapshot sorts worst-first, not alphabetically")
def _():
    dh.reset()
    dh.track("Alpha", source="s", expected_refresh_hours=24, generated_at=ago(hours=1), now=NOW)
    dh.track("Zulu", source="s", expected_refresh_hours=24, generated_at=None, now=NOW)
    names = [d["dataset"] for d in dh.snapshot(now=NOW)["datasets"]]
    assert names == ["Zulu", "Alpha"], names
    dh.reset()


@check("registering the same dataset twice replaces rather than duplicates")
def _():
    dh.reset()
    dh.track("Same", source="s", expected_refresh_hours=24, generated_at=ago(hours=1), now=NOW)
    dh.track("Same", source="s", expected_refresh_hours=24, generated_at=ago(hours=2), now=NOW)
    snap = dh.snapshot(now=NOW)
    assert snap["total"] == 1
    assert snap["datasets"][0]["freshness_age_hours"] == 2.0
    dh.reset()


@check("snapshot rolls up the worst status across all datasets")
def _():
    dh.reset()
    dh.track("Good", source="s", expected_refresh_hours=24, generated_at=ago(hours=1), now=NOW)
    dh.track("Bad", source="s", expected_refresh_hours=24, generated_at=ago(hours=1),
             record_count=1, expected_records=100, now=NOW)
    snap = dh.snapshot(now=NOW)
    assert snap["worst"] == dh.DEGRADED
    assert snap["current"] == 1 and snap["degraded"] == 1
    dh.reset()


@check("snapshot exposes a keyed lookup for the template")
def _():
    dh.reset()
    dh.track("Stock screen", source="s", expected_refresh_hours=168,
             generated_at=ago(hours=1), now=NOW)
    snap = dh.snapshot(now=NOW)
    assert snap["by_name"]["Stock screen"]["status"] == dh.LIVE
    assert snap["by_name"].get("Typo") is None
    dh.reset()


@check("an empty registry reports UNAVAILABLE rather than raising")
def _():
    dh.reset()
    snap = dh.snapshot(now=NOW)
    assert snap["total"] == 0 and snap["worst"] == dh.UNAVAILABLE


@check("assess never mutates the job dict it was handed")
def _():
    job = {"status": "failed", "detail": "x", "attempted_after_serve": True}
    before = dict(job)
    daily(generated_at=ago(hours=1), job=job)
    assert job == before


# ── The three-place rule ─────────────────────────────────────────────────────
# A docs/ artefact needs allow-listing in THREE places to reach the web, and
# today.json shipped with two of them and 404'd silently for days. screen.json
# has these assertions in test_stock_screen.py; data-health.json is the file
# where a silent 404 is worst, because it is the one people curl precisely when
# they already suspect the page is lying.

ROOT = __import__("pathlib").Path(__file__).parent


@check("generate.py writes docs/data-health.json")
def _():
    assert 'out_dir / "data-health.json"' in (ROOT / "generate.py").read_text()


@check(".vercelignore allow-lists docs/data-health.json by name")
def _():
    assert "!docs/data-health.json" in (ROOT / ".vercelignore").read_text()


@check("newspaper.yml commits docs/data-health.json")
def _():
    # The fourth place. test_engine_regressions.py caught this one when the
    # first three were done — a file written, ignored-except and copied still
    # never reaches production if the workflow does not commit it.
    assert "docs/data-health.json" in (ROOT / ".github" / "workflows" / "newspaper.yml").read_text()


@check("build.js copies data-health.json into public/")
def _():
    assert '"data-health.json"' in (ROOT / "vercel-news" / "build.js").read_text()


@check("generate.py registers health before it renders, not after")
def _():
    src = (ROOT / "generate.py").read_text()
    # Rendering first would let the page and the health page disagree about
    # the same build — the whole point is that both read one snapshot.
    assert src.index("_register_health(") < src.index("tpl.render(")


@check("record_job_status carries the attempt's own coverage")
def _():
    src = (ROOT / "newspaper.py").read_text()
    assert "def record_job_status(job: str, status: str, detail: str = \"\"," in src
    assert "records: int | None = None" in src
    assert "expected: int | None = None" in src


@check("get_job_status returns the attempt coverage separately from the payload's")
def _():
    src = (ROOT / "newspaper.py").read_text()
    assert '"attempt_coverage"' in src
    assert "SELECT run_at, status, detail, records, expected" in src


@check("the stock screen workflow records coverage on FAILURE, not only success")
def _():
    wf = (ROOT / ".github" / "workflows" / "stock_screen.yml").read_text()
    failed = wf.index('record_job_status(JOB, "failed"')
    tail = wf[failed:failed + 300]
    assert "records=attempt_records" in tail and "expected=attempt_expected" in tail


@check("attempt counters are initialised outside the try that reports them")
def _():
    # Inside the try, an early crash would NameError in the handler and lose
    # the failure record — the one thing that must survive a failed build.
    wf = (ROOT / ".github" / "workflows" / "stock_screen.yml").read_text()
    assert wf.index("attempt_records = attempt_expected = None") < wf.index("          try:")


def main() -> int:
    passed = failed = 0
    for name, fn in CHECKS:
        try:
            fn()
        except AssertionError as e:
            print(f"  FAIL  {name}  ({e})")
            failed += 1
        except Exception as e:
            print(f"  ERROR {name}  ({type(e).__name__}: {e})")
            failed += 1
        else:
            print(f"  PASS  {name}")
            passed += 1
    print(f"\n{passed} passed · {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
