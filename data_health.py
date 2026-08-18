#!/usr/bin/env python3
"""
data_health.py — one vocabulary for how current every dataset on the site is.

Before this module, every section answered "is this current?" its own way.
#funds printed "Built 2026-08-17 · 0.5d old", the stock screen printed a
coverage count, the brief printed nothing, and the picks section printed a
week key. A reader comparing two sections could not tell which was older, and
neither could a test. Worse, three different phrasings — "updated", "live",
"latest" — were used interchangeably for three different things.

The audit's finding was blunt: the site can look more current than its data.
That is the one failure mode this module exists to make impossible.

WHAT IT PROVIDES

  assess()   turns a payload plus its job history into one metadata block
  register() files that block in a per-process registry
  snapshot() returns every filed block, worst-first, for the health page

WHAT IT DELIBERATELY DOES NOT DO

  It does not fetch, build, cache or repair anything. It reports. A module
  that could also fix things would eventually be tempted to hide the thing it
  could not fix, and the whole point is that failures stay visible.

THE STATUS VOCABULARY IS CLOSED

Six values, ordered by severity, and nothing else is ever rendered:

  LIVE         built within a quarter of its refresh interval
  FRESH        built within its refresh interval
  STALE        older than its refresh interval, still valid
  DEGRADED     serving valid data behind a known problem — a failed newer
               attempt, thin coverage, or a vintage that cannot be read
  FAILED       the build failed and there is no valid data to fall back to
  UNAVAILABLE  nothing has ever been published

The severity order matters as much as the names: when several conditions
fire at once the WORST one is reported, never the friendliest. A dataset that
is both stale and short on coverage reports DEGRADED, not STALE.
"""
from __future__ import annotations

from datetime import datetime, timezone

# ── The closed vocabulary ────────────────────────────────────────────────────
LIVE = "LIVE"
FRESH = "FRESH"
STALE = "STALE"
DEGRADED = "DEGRADED"
FAILED = "FAILED"
UNAVAILABLE = "UNAVAILABLE"

# Severity is the whole contract. assess() collects every condition that fires
# and reports max(severity), so adding a new condition can only ever make a
# status worse — never quietly upgrade a broken dataset to FRESH.
SEVERITY = {
    LIVE: 0,
    FRESH: 1,
    STALE: 2,
    DEGRADED: 3,
    FAILED: 4,
    UNAVAILABLE: 5,
}

# Statuses at or above this severity mean "do not present this as current".
# The template uses it to decide whether a section gets a warning treatment,
# so the threshold lives here rather than being re-guessed per section.
NOT_CURRENT = SEVERITY[STALE]

# A dataset is only LIVE inside this fraction of its own refresh interval.
# Relative rather than absolute because "live" means something different for a
# 5-minute quote feed and a weekly screen; a fixed 15-minute rule would mark
# every weekly artefact STALE within an hour of being correct.
LIVE_FRACTION = 0.25


def _parse(ts) -> datetime | None:
    """UTC datetime from an ISO string, or None when it cannot be read.

    None is never treated as fresh by any caller in this module — an
    unreadable timestamp is exactly the case where assuming the best would
    publish data of unknown vintage under today's date. It downgrades to
    DEGRADED instead.

    Naive timestamps are read as UTC. Two clocks feed this: job_runs is always
    UTC, while the stock screen stamps its payload IST. Both parse here rather
    than being string-compared, because a raw string compare misorders an
    IST-stamped payload against a UTC attempt by up to 5.5 hours — which is
    long enough to hide a failed overnight rebuild behind a morning payload.
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _age_hours(ts, now: datetime) -> float | None:
    dt = _parse(ts)
    if dt is None:
        return None
    return (now - dt).total_seconds() / 3600.0


def humanise_age(hours: float | None) -> str:
    """"3 days old" / "2h old" / "unknown vintage" — one phrasing, everywhere.

    Every section used to invent its own. The point of centralising it is not
    tidiness: it is that "0.5d old" and "12h old" describing the same number
    on the same page made the site look like several products glued together.
    """
    if hours is None:
        return "unknown vintage"
    if hours < 0:
        # A payload stamped in the future is a clock problem, not freshness.
        return "clock skew"
    if hours < 1:
        m = int(hours * 60)
        return "just built" if m < 2 else f"{m} min old"
    if hours < 48:
        return f"{int(round(hours))}h old"
    days = hours / 24.0
    return f"{int(round(days))} days old"


def humanise_interval(hours: float | None) -> str:
    """"every 24h" / "weekly" — for the expected_refresh field."""
    if not hours:
        return "on demand"
    if hours < 1:
        return f"every {int(hours * 60)} min"
    if hours < 24:
        return f"every {int(round(hours))}h"
    if abs(hours - 24) < 0.01:
        return "daily"
    if abs(hours - 168) < 0.01:
        return "weekly"
    return f"every {round(hours / 24.0, 1)} days"


def assess(dataset: str, *,
           source: str,
           expected_refresh_hours: float | None,
           generated_at=None,
           record_count: int | None = None,
           expected_records: int | None = None,
           coverage_floor: float = 0.9,
           job: dict | None = None,
           fallback: bool = False,
           error: str | None = None,
           build_version: str | None = None,
           now: datetime | None = None) -> dict:
    """One dataset's health, as the twelve fields the audit specified.

    `job` is whatever get_job_status() returned for this dataset — {} when no
    attempt has ever been recorded. It is the difference between "unchanged
    since last week" and "tried this week and broke", which is the single
    distinction the previous site could not make.

    `expected_records` is what a COMPLETE build looks like (750 for the stock
    screen). Passing it is what lets a 50-row build report DEGRADED instead of
    quietly presenting itself as the universe.
    """
    now = now or datetime.now(timezone.utc)
    job = job or {}

    age = _age_hours(generated_at, now)
    has_payload = bool(generated_at) or bool(record_count)

    # Every condition that fires appends its status. The worst one wins.
    reasons: list[tuple[str, str]] = []

    if not has_payload:
        # Nothing has ever been published, or the payload is empty. FAILED
        # when we know an attempt broke, UNAVAILABLE when nothing ever ran —
        # the reader deserves to know which.
        if job.get("status") == "failed":
            reasons.append((FAILED, f"build failed: {job.get('detail') or 'no detail recorded'}"))
        else:
            reasons.append((UNAVAILABLE, "never published"))
    else:
        if age is None:
            # Data exists but we cannot say when it was built. Never FRESH.
            reasons.append((DEGRADED, "build timestamp could not be read"))
        elif expected_refresh_hours:
            if age <= expected_refresh_hours * LIVE_FRACTION:
                reasons.append((LIVE, ""))
            elif age <= expected_refresh_hours:
                reasons.append((FRESH, ""))
            else:
                reasons.append((STALE, f"older than its {humanise_interval(expected_refresh_hours)} refresh"))
        else:
            reasons.append((FRESH, ""))

        # A failed attempt that happened AFTER the payload we are serving is
        # the case the audit called out by name: the page is not showing old
        # data because nothing changed, it is showing old data because the
        # newer build broke. Valid data, broken pipeline — DEGRADED, not STALE.
        if job.get("status") == "failed" and job.get("attempted_after_serve"):
            reasons.append((DEGRADED,
                            f"latest refresh attempt failed: {job.get('detail') or 'no detail recorded'}"))

        # Thin coverage. This is the 750-vs-50 fix: a build that returned a
        # fraction of the universe must never render as the universe.
        if expected_records and record_count is not None:
            if record_count < expected_records * coverage_floor:
                reasons.append((DEGRADED,
                                f"partial coverage — {record_count}/{expected_records} records"))

    if error:
        reasons.append((DEGRADED, error))

    status, _ = max(reasons, key=lambda r: SEVERITY[r[0]])
    notes = [why for _, why in reasons if why]

    coverage_pct = None
    if expected_records:
        coverage_pct = round(100.0 * (record_count or 0) / expected_records, 1)

    return {
        "dataset": dataset,
        "source": source,
        "last_successful_update": _iso(generated_at),
        "last_attempted_update": _iso(job.get("run_at")) or _iso(generated_at),
        "expected_refresh": humanise_interval(expected_refresh_hours),
        "expected_refresh_hours": expected_refresh_hours,
        "freshness_age": humanise_age(age),
        "freshness_age_hours": None if age is None else round(age, 2),
        "status": status,
        "severity": SEVERITY[status],
        "record_count": record_count,
        "expected_records": expected_records,
        "coverage": (f"{record_count}/{expected_records}"
                     if expected_records and record_count is not None else None),
        "coverage_pct": coverage_pct,
        "attempt_status": job.get("status") or None,
        "attempt_detail": job.get("detail") or None,
        "attempt_after_serve": bool(job.get("attempted_after_serve")),
        "error": error,
        "fallback_used": bool(fallback),
        "build_version": build_version,
        "notes": notes,
        "headline": _headline(dataset, status, age, notes),
        "is_current": SEVERITY[status] < NOT_CURRENT,
    }


def _iso(ts) -> str | None:
    dt = _parse(ts)
    return None if dt is None else dt.astimezone(timezone.utc).isoformat()


def _headline(dataset: str, status: str, age: float | None, notes: list[str]) -> str:
    """One sentence a reader can act on, not a status word on its own.

    "STALE" alone tells someone nothing; "STALE — 3 days old, older than its
    weekly refresh" tells them whether to trust the table underneath it.
    """
    if status == UNAVAILABLE:
        return f"{dataset} has never been published."
    if status == FAILED:
        detail = notes[0] if notes else "no data available"
        return f"{dataset} is unavailable — {detail}."
    tail = "; ".join(notes)
    base = f"{dataset} · {humanise_age(age)}"
    return f"{base} — {tail}." if tail else f"{base}."


# ── The registry ─────────────────────────────────────────────────────────────
# Filled during a build, read once at the end. A plain module-level list rather
# than a database table on purpose: this is the state of ONE build, and
# persisting it would create a second source of truth about freshness sitting
# next to job_runs, which is the actual durable record.
_REGISTRY: list[dict] = []


def register(block: dict) -> dict:
    """File a health block. Returns it, so callers can inline the call."""
    _REGISTRY[:] = [b for b in _REGISTRY if b.get("dataset") != block.get("dataset")]
    _REGISTRY.append(block)
    return block


def track(dataset: str, **kw) -> dict:
    """assess() + register() in one call. The normal entry point."""
    return register(assess(dataset, **kw))


def reset() -> None:
    """Drop everything filed. Tests and repeated builds in one process."""
    _REGISTRY.clear()


def snapshot(now: datetime | None = None) -> dict:
    """Every filed dataset, worst-first, plus a roll-up for the header badge.

    Worst-first rather than alphabetical because the only reason to open this
    page is to find what is broken, and a list sorted by name buries it.
    """
    now = now or datetime.now(timezone.utc)
    items = sorted(_REGISTRY, key=lambda b: (-b["severity"], b["dataset"]))
    counts: dict[str, int] = {}
    for b in items:
        counts[b["status"]] = counts.get(b["status"], 0) + 1
    worst = max((b["status"] for b in items), key=lambda s: SEVERITY[s], default=UNAVAILABLE)
    return {
        "generated_at": now.astimezone(timezone.utc).isoformat(),
        "datasets": items,
        # Keyed lookup for the template. A macro that scanned `datasets` for a
        # name would be O(n) per badge across ~15 sections, and — worse — would
        # silently render nothing for a typo'd name. A dict lookup makes the
        # miss visible in one place instead of fifteen.
        "by_name": {b["dataset"]: b for b in items},
        "counts": counts,
        "worst": worst,
        "total": len(items),
        "current": sum(1 for b in items if b["is_current"]),
        "degraded": sum(1 for b in items if not b["is_current"]),
    }
