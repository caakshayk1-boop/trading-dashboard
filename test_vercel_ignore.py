#!/usr/bin/env python3
"""
test_vercel_ignore.py — the deploy gate must fail toward BUILDING.

WHY THIS EXISTS, AND WHY IT IS ITS OWN FILE
-------------------------------------------
vercel.json's `ignoreCommand` decides whether Vercel builds a commit at all.
Its contract is inverted and easy to get backwards:

    exit 0  -> SKIP the build
    exit 1  -> BUILD

So the dangerous failure is not "it builds too much". It is a command that
exits 0 for a reason nobody predicted — a shallow clone, a renamed directory, a
`cd` that lands somewhere unexpected — because then **news.askakshay.com stops
deploying and nothing says so**. The workflow stays green, the commits keep
landing, and the paper silently serves the same edition until somebody notices
the date. That is the exact shape of failure this repo keeps writing tests
against, and the gate itself had none.

The command is a string inside a JSON file. No linter reads it, no CI step runs
it, and it executes on Vercel's infrastructure rather than ours. A hermetic
temp repo is the only way to exercise it here: it works at any checkout depth,
which matters because actions/checkout defaults to fetch-depth 1 and `HEAD^`
does not exist in this repo's own CI clone.

Offline. No network, no pytest.

Usage:
    python3 test_vercel_ignore.py
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent
CMD = json.loads((ROOT / "vercel-news" / "vercel.json").read_text())["ignoreCommand"]

SKIP, BUILD = 0, 1

CHECKS: list[tuple[str, callable]] = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


def _git(cwd, *args):
    subprocess.run(("git",) + args, cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _repo(commits, subdir=None):
    """A throwaway repo whose HEAD is the last of `commits`.

    Each commit is a dict of path -> contents. Returns the directory the gate
    should be run from — `subdir` when given, mimicking Vercel's Root Directory
    setting, which is `vercel-news` on this project.
    """
    d = pathlib.Path(tempfile.mkdtemp())
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    for i, files in enumerate(commits):
        for rel, body in files.items():
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
        _git(d, "add", "-A")
        _git(d, "commit", "-q", "-m", f"c{i}", "--allow-empty")
    run_in = d / subdir if subdir else d
    run_in.mkdir(parents=True, exist_ok=True)
    return d, run_in


def gate(commits, subdir="vercel-news"):
    """Run the REAL ignoreCommand and return its exit code."""
    d, run_in = _repo(commits, subdir)
    try:
        return subprocess.run(CMD, shell=True, cwd=run_in,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode
    finally:
        shutil.rmtree(d, ignore_errors=True)


BASE = {"docs/index.html": "shell v1",
        "vercel-news/build.js": "// build",
        "data/signals.json": "[]",
        "scanner.py": "# code"}


# ── The two cases the gate exists to separate ───────────────────────────────

@check("a commit that changes the site BUILDS")
def _():
    after = dict(BASE, **{"docs/index.html": "shell v2"})
    assert gate([BASE, after]) == BUILD


@check("a commit that changes only data SKIPS")
def _():
    # "data: update signals ... [skip ci]" — ~17 of these a day, and Vercel
    # does NOT honour [skip ci] on this project. Measured: 34 of 55 commits
    # over 14 days touch neither docs/ nor vercel-news/.
    after = dict(BASE, **{"data/signals.json": '[{"a":1}]'})
    assert gate([BASE, after]) == SKIP


@check("a Python-only commit SKIPS")
def _():
    after = dict(BASE, **{"scanner.py": "# code v2"})
    assert gate([BASE, after]) == SKIP


@check("a commit touching vercel-news BUILDS")
def _():
    after = dict(BASE, **{"vercel-news/build.js": "// build v2"})
    assert gate([BASE, after]) == BUILD


@check("a commit touching both BUILDS")
def _():
    after = dict(BASE, **{"data/signals.json": "[1]", "docs/index.html": "v2"})
    assert gate([BASE, after]) == BUILD


# ── Failing toward BUILD, which is the whole safety property ────────────────
#
# Every one of these would, if it returned SKIP instead, stop the site
# deploying with a green workflow and no error anywhere.

@check("the very first commit BUILDS — there is no HEAD^ to compare")
def _():
    assert gate([BASE]) == BUILD


@check("an empty commit BUILDS rather than being skipped on a technicality")
def _():
    # Nothing changed anywhere, so `git diff --quiet` succeeds and the gate
    # would skip. That is correct — but it must be reached through the diff,
    # not through an error. Pinned so a future edit cannot make "the command
    # broke" and "nothing changed" produce the same answer.
    assert gate([BASE, BASE]) == SKIP


@check("running outside a git repo BUILDS")
def _():
    d = pathlib.Path(tempfile.mkdtemp())
    try:
        rc = subprocess.run(CMD, shell=True, cwd=d,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL).returncode
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert rc == BUILD, "no repo must not read as 'nothing changed'"


@check("a repo with neither watched directory BUILDS")
def _():
    # If docs/ and vercel-news/ were ever renamed, the gate must not silently
    # decide nothing ever changes again.
    only = {"scanner.py": "# code"}
    assert gate([only, dict(only, **{"scanner.py": "# v2"})],
                subdir=None) == BUILD


@check("it works from the repo root as well as from vercel-news/")
def _():
    # Vercel's Root Directory is a project setting this repo does not control.
    # The command cds to the toplevel precisely so either value works.
    after = dict(BASE, **{"docs/index.html": "v2"})
    assert gate([BASE, after], subdir=None) == BUILD
    assert gate([BASE, after], subdir="vercel-news") == BUILD


@check("the command is still the inverted-exit-code shape it is documented as")
def _():
    # A rewrite that drops `|| exit 1` would flip the safe direction.
    assert "|| exit 1" in CMD, CMD
    assert "--quiet" in CMD, CMD


def main() -> int:
    passed = failed = 0
    for name, fn in CHECKS:
        try:
            fn()
        except AssertionError as e:
            print(f"  FAIL  {name}  ({e})")
            failed += 1
        except Exception as e:                          # noqa: BLE001
            print(f"  ERROR {name}  ({type(e).__name__}: {e})")
            failed += 1
        else:
            print(f"  PASS  {name}")
            passed += 1
    print(f"\n{passed} passed · {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
