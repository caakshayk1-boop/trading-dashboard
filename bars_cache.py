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
