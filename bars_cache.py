"""
bars_cache.py — where harvested bars live, in ONE place.

WHY THIS EXISTS
---------------
Five files read `barsH.json` and `barsD3y.json`. All five had the path written
out longhand as:

    /private/tmp/claude-501/-Users-akshaykumarkothari-Workspace/
    4387587e-0410-48f6-b6ac-50dea011672c/scratchpad

That is a scratch directory on one laptop, inside /private/tmp, which macOS
clears. Nothing in the repository has ever created those files — five readers,
zero writers — so `research.yml` could not have worked on any machine but that
one, on a day the folder happened to survive. Its only run in history died
before reaching them, on a missing package, which is why nobody noticed the
deeper problem underneath.

The cache is now repo-relative and overridable, and harvest_bars.py fills it.

NOT COMMITTED, DELIBERATELY. Hourly bars for the Nifty 500 are tens of
megabytes and change every session. They are working data: harvested at the
start of a run and thrown away with the runner. `data/bars/` is gitignored.
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))

# Override for a machine that keeps its bars elsewhere — a laptop with a warm
# cache it does not want to re-harvest, most obviously.
CACHE_DIR = os.environ.get("RESEARCH_BARS_DIR") or os.path.join(HERE, "data", "bars")

HOURLY = "barsH.json"
DAILY_3Y = "barsD3y.json"


def path(name: str) -> str:
    return os.path.join(CACHE_DIR, name)


def exists(name: str) -> bool:
    return os.path.isfile(path(name))


def load(name: str) -> dict:
    """Read a bar file, or say plainly what is missing and how to make it.

    The old failure was a bare FileNotFoundError naming somebody else's
    scratch folder, which tells a reader nothing about what to do next.
    """
    p = path(name)
    if not os.path.isfile(p):
        raise FileNotFoundError(
            f"{p} is not there. Harvested bars are working data and are not "
            f"committed — run `python3 harvest_bars.py` first, or point "
            f"RESEARCH_BARS_DIR at a directory that already has them."
        )
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def save(name: str, obj: dict) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    p = path(name)
    tmp = p + ".tmp"
    # Written to a temp file and moved, so a run killed mid-write leaves the
    # previous cache intact rather than a truncated file that parses as valid
    # JSON right up until it does not.
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, separators=(",", ":"))
    os.replace(tmp, p)
    return p


MANIFEST = "manifest.json"


def manifest() -> dict:
    """What the last harvest actually got, or {} if it never ran.

    Returned rather than raised on a missing file: a scan reading a cache
    somebody else filled (RESEARCH_BARS_DIR on a laptop) is a supported case,
    and it should say it cannot vouch for the coverage — not die.
    """
    try:
        with open(path(MANIFEST), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def coverage_note(*names: str) -> str:
    """One measured sentence about how much of the universe this run saw.

    WHY THIS IS NOT A CONSTANT
    --------------------------
    It used to be. Both scans shipped a hardcoded paragraph saying the run had
    covered "the names whose bars were already harvested" because "Yahoo had
    throttled this address". That was true of the laptop run it was written on
    and became false the moment the harvester worked: the run of 2026-09-09
    fetched 500 of 500 and published a note swearing it had been throttled.

    A freshness claim a payload asserts about itself, rather than reads off the
    thing it is describing, can only drift — the same fault `data_health.py`
    exists to stop a section committing on the page. So the numbers come from
    the manifest the harvest wrote, and if there is no manifest the note says
    that instead of inventing a reason.
    """
    m = manifest()
    files = (m.get("files") or {})
    rows = [(n, files[n]) for n in names if n in files]
    if not rows:
        return ("Coverage for this run is unknown — the bar cache carries no "
                "harvest manifest, so how much of the universe it holds cannot "
                "be stated. Run harvest_bars.py to produce one.")

    at = m.get("at") or "an unrecorded time"
    parts = []
    for name, cov in rows:
        asked, got = cov.get("asked"), cov.get("got")
        if not asked or got is None:
            continue
        label = {HOURLY: "hourly", DAILY_3Y: "3-year daily"}.get(name, name)
        bit = f"{got} of {asked} names on {label} bars"
        missed = (cov.get("no_data") or 0) + (cov.get("too_short") or 0)
        if missed:
            bit += (f" ({cov.get('no_data', 0)} returned nothing, "
                    f"{cov.get('too_short', 0)} had too little history)")
        parts.append(bit)
    if not parts:
        return ("Coverage for this run is unknown — the harvest manifest "
                "recorded no counts.")
    return f"Harvested {'; '.join(parts)}, at {at}."
