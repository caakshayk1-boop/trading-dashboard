#!/usr/bin/env python3
"""
test_bars_cache.py — every bar file a script reads must have a producer.

WHY THIS EXISTS
---------------
`barsH.json` and `barsD3y.json` were read by five files and written by none.
The path was a scratch directory on one laptop, under /private/tmp, which
macOS clears. research.yml therefore could never have completed on any machine
but that one, and its failure was hidden behind an earlier import error for
long enough that nobody looked.

The specific bug is fixed. This pins the SHAPE of it, because "a reader with
no writer" is invisible until the day something runs somewhere else:

  * no source file may hardcode an absolute path into somebody's home or
    scratch directory
  * the cache location must come from bars_cache, which one edit can move
  * a missing file must say what to run, not just which path was absent

Offline. No network, no pytest.

Usage:
    python3 test_bars_cache.py
"""
from __future__ import annotations

import os
import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

CHECKS: list[tuple[str, callable]] = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


def sources() -> list[pathlib.Path]:
    return [p for p in ROOT.glob("*.py") if p.name != "test_bars_cache.py"]


# ── Paths that only exist on one machine ────────────────────────────────────

@check("no source hardcodes a path into a home or scratch directory")
def _():
    # /Users/<name>, /home/<name>, and /private/tmp scratch folders. The repo's
    # own runtime paths (data/, logs/, docs/) are relative and unaffected.
    bad = re.compile(r"['\"](/Users/|/home/[a-z]|/private/tmp/|/tmp/claude)")
    hits = []
    for p in sources():
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue                      # a comment may quote the old path
            if bad.search(line):
                hits.append(f"{p.name}:{i}")
    assert not hits, f"machine-specific paths: {hits}"


@check("the bar files are addressed through bars_cache, not by name")
def _():
    # A literal "barsH.json" outside bars_cache.py means a second opinion about
    # where the cache lives — which is how five files came to agree on a path
    # that existed once, on one Mac.
    allowed = {"bars_cache.py", "harvest_bars.py"}
    hits = []
    for p in sources():
        if p.name in allowed:
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if "barsH.json" in line or "barsD3y.json" in line:
                hits.append(f"{p.name}:{i}")
    assert not hits, f"bar filenames outside bars_cache: {hits}"


# ── A reader with no writer ─────────────────────────────────────────────────

@check("harvest_bars.py writes both files scan_research.py reads")
def _():
    import bars_cache
    import harvest_bars
    assert set(harvest_bars.RANGES) == {bars_cache.HOURLY, bars_cache.DAILY_3Y}, \
        sorted(harvest_bars.RANGES)


@check("research.yml harvests before it scans")
def _():
    # The RUN LINES, not any mention. Both scripts are named in the comments
    # explaining why this step exists, and the first draft of this check
    # compared those instead — it failed on prose while the workflow was
    # correct, which is a test that measures the wrong thing.
    wf = (ROOT / ".github" / "workflows" / "research.yml").read_text()
    runs = [l.strip() for l in wf.splitlines()
            if re.match(r"run:\s*python3?\s", l.strip())]
    harvest = next((i for i, l in enumerate(runs) if "harvest_bars.py" in l), None)
    scan = next((i for i, l in enumerate(runs) if "scan_research.py" in l), None)
    assert harvest is not None, f"the workflow never fills the cache: {runs}"
    assert scan is not None, f"the workflow never scans: {runs}"
    assert harvest < scan, f"the scan runs before the harvest that feeds it: {runs}"


@check("the workflow installs what the harvest and the scan import")
def _():
    wf = (ROOT / ".github" / "workflows" / "research.yml").read_text()
    for pkg in ("numpy", "pandas", "ta", "requests"):
        assert re.search(rf"pip install[^\n]*\b{pkg}\b", wf), f"{pkg} not installed"


# ── Failing usefully ────────────────────────────────────────────────────────

@check("a missing bar file says what to run, not just what is absent")
def _():
    import importlib
    import bars_cache
    old = os.environ.get("RESEARCH_BARS_DIR")
    with tempfile.TemporaryDirectory() as d:
        os.environ["RESEARCH_BARS_DIR"] = d
        importlib.reload(bars_cache)
        try:
            bars_cache.load(bars_cache.HOURLY)
            raise AssertionError("a missing file did not raise")
        except FileNotFoundError as e:
            msg = str(e)
            assert "harvest_bars.py" in msg, msg
        finally:
            if old is None:
                os.environ.pop("RESEARCH_BARS_DIR", None)
            else:
                os.environ["RESEARCH_BARS_DIR"] = old
            importlib.reload(bars_cache)


@check("a save is atomic — a killed run cannot leave a half-written cache")
def _():
    import importlib
    import bars_cache
    old = os.environ.get("RESEARCH_BARS_DIR")
    with tempfile.TemporaryDirectory() as d:
        os.environ["RESEARCH_BARS_DIR"] = d
        importlib.reload(bars_cache)
        try:
            bars_cache.save("t.json", {"A": [[1, 2, 3, 4, 5, 6]]})
            assert bars_cache.load("t.json") == {"A": [[1, 2, 3, 4, 5, 6]]}
            assert not os.path.exists(bars_cache.path("t.json") + ".tmp")
        finally:
            if old is None:
                os.environ.pop("RESEARCH_BARS_DIR", None)
            else:
                os.environ["RESEARCH_BARS_DIR"] = old
            importlib.reload(bars_cache)


@check("the harvested cache is gitignored — it is working data, not history")
def _():
    gi = (ROOT / ".gitignore").read_text()
    assert "data/bars" in gi, "tens of MB of bars would be committed every run"


@check("an empty harvest refuses to overwrite a good cache")
def _():
    src = (ROOT / "harvest_bars.py").read_text()
    body = src[src.index("def main("):]
    assert "NOT overwriting" in body, \
        "an empty fetch must not be written over real bars"


def main() -> int:
    passed = failed = 0
    for name, fn in CHECKS:
        try:
            fn()
        except AssertionError as e:
            print(f"  FAIL  {name}  ({e})")
            failed += 1
        except Exception as e:                              # noqa: BLE001
            print(f"  ERROR {name}  ({type(e).__name__}: {e})")
            failed += 1
        else:
            print(f"  PASS  {name}")
            passed += 1
    print(f"\n{passed} passed · {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
