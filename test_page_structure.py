#!/usr/bin/env python3
"""
test_page_structure.py — nav order IS document order, and both come from
SECTION_MAP.

page_context() documents this contract in prose ("nav order MUST match
document order") and nothing enforced it. The nav is generated from
SECTION_MAP; the document is hand-ordered inside TEMPLATE. Nothing stopped
the two from drifting, and the failure is silent: the nav numbers a section
07 while the reader scrolls past it in position 12.

This matters most the moment sections are REGROUPED — moving a block in the
template without moving its SECTION_MAP row, or the reverse, produces a page
whose contents page lies about itself.

Offline. Parses the template as text; no network, no database, no pytest.

Usage:
    python3 test_page_structure.py
"""
from __future__ import annotations

import logging
import re
import sys

logging.basicConfig(level=logging.ERROR)

from newspaper import SECTION_MAP, TEMPLATE

CHECKS: list[tuple[str, callable]] = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


def document_order() -> list[str]:
    """Section ids in the order the TEMPLATE actually emits them.

    Anchored on `<section class="sec" id="...">` rather than on the guard,
    because the guard and the element are on the same line for most sections
    but not all — #funds and #stocks put the guard on its own line.
    """
    return re.findall(r'<section class="sec" id="([a-z0-9]+)"', TEMPLATE)


def map_order(page: str) -> list[str]:
    return [i for i, _l, pg, _g in SECTION_MAP if pg == page]


@check("every SECTION_MAP id exists in the template")
def _():
    doc = set(document_order())
    missing = [i for i, _l, _p, _g in SECTION_MAP if i not in doc]
    assert not missing, f"in the nav but not in the document: {missing}"


@check("every template section is declared in SECTION_MAP")
def _():
    declared = {i for i, _l, _p, _g in SECTION_MAP}
    # #hero and any wrapper that is not a numbered section are excluded by
    # not carrying class="sec" — anything that does carry it is a numbered
    # section and must be in the map or it renders with no number at all.
    orphans = [i for i in document_order() if i not in declared]
    assert not orphans, f"in the document but not in the nav: {orphans}"


@check("main page: nav order equals document order")
def _():
    doc = [i for i in document_order() if i in set(map_order("main"))]
    assert doc == map_order("main"), f"\n  document: {doc}\n  SECTION_MAP: {map_order('main')}"


@check("desk page: nav order equals document order")
def _():
    doc = [i for i in document_order() if i in set(map_order("desk"))]
    assert doc == map_order("desk"), f"\n  document: {doc}\n  SECTION_MAP: {map_order('desk')}"


@check("no section id is declared twice")
def _():
    ids = [i for i, _l, _p, _g in SECTION_MAP]
    assert len(ids) == len(set(ids)), "duplicate id in SECTION_MAP"
    doc = document_order()
    assert len(doc) == len(set(doc)), f"duplicate <section> id in TEMPLATE: {doc}"


@check("every nav group on a page is contiguous")
def _():
    """A group that appears, stops, and appears again prints its heading twice.

    That was the accepted state before the 2026-08-18 audit — 'Track Record'
    printed twice on the main page — and it is exactly what the audit's
    finding was about: the reader cannot see a hierarchy that is interleaved.
    Grouping is only navigation if the groups are runs.
    """
    for page in ("main", "desk"):
        rows = [(i, g) for i, _l, pg, g in SECTION_MAP if pg == page]
        seen, runs = set(), []
        prev = None
        for _i, g in rows:
            if g != prev:
                runs.append(g)
                prev = g
        dupes = [g for g in runs if runs.count(g) > 1]
        assert not dupes, f"{page}: non-contiguous nav group(s) {sorted(set(dupes))} in {runs}"


@check("the template's section guard names the same id as its element")
def _():
    """`{% if 'x' in secs %}<section id="y">` renders a section the nav has
    dropped, or hides one it advertises. Both are the drift this file exists
    to catch, and a copy-paste of a section block is how it happens."""
    for guard, sid in re.findall(
            r"\{%\s*if\s*'([a-z0-9]+)' in secs[^%]*%\}\s*(?:\n)?<section class=\"sec\" id=\"([a-z0-9]+)\"",
            TEMPLATE):
        assert guard == sid, f"guard '{guard}' wraps <section id=\"{sid}\">"


@check("Data Health is on the main page and inside the trust run")
def _():
    row = [r for r in SECTION_MAP if r[0] == "datahealth"]
    assert row, "datahealth is not in SECTION_MAP"
    _i, label, page, group = row[0]
    assert page == "main", f"datahealth is on {page}"
    assert label == "Data Health"
    rules = [r for r in SECTION_MAP if r[0] == "rules"][0]
    assert group == rules[3], (
        f"datahealth is in group {group!r} but the Engine Log is in {rules[3]!r} — "
        "they answer the same question from two ends and belong together")


@check("the document closes AFTER the last section, and only once")
def _():
    """The bug this was written for.

    Reordering the section blocks on 2026-08-18 carried the closing furniture
    — </main>, the footer, the scripts, </body></html> — along inside the
    #alerts block, because that block's slice ran to the end of the template.
    The result parsed as valid Jinja, passed every other test in this file,
    and rendered a page whose </html> sat in the MIDDLE, with four sections
    after it.

    Nothing else catches this: Jinja does not care, the section-order checks
    above do not look at document furniture, and a browser is forgiving enough
    that it would probably have looked fine while being invalid.
    """
    last = [m.start() for m in re.finditer(r'<section class="sec" id=', TEMPLATE)][-1]
    for tag in ("</main>", "</body>", "</html>"):
        assert TEMPLATE.count(tag) == 1, f"{tag} appears {TEMPLATE.count(tag)} times"
        assert TEMPLATE.index(tag) > last, f"{tag} sits BEFORE the last section"
    assert TEMPLATE.rstrip().endswith("</html>"), "template does not end with </html>"
    assert TEMPLATE.index("</body>") < TEMPLATE.index("</html>")
    assert TEMPLATE.index("</main>") < TEMPLATE.index("</body>")
    assert TEMPLATE.index("<footer") > last, "the footer sits above a section"


@check("every section lives inside <main>")
def _():
    open_main = TEMPLATE.index("<main>")
    for m in re.finditer(r'<section class="sec" id="([a-z0-9]+)"', TEMPLATE):
        assert m.start() > open_main, f"#{m.group(1)} is outside <main>"


@check("every tab strip has exactly one pane per tab")
def _():
    """Removing a tab means removing its pane, and vice versa.

    Lifting the Book out of The Desk on 2026-08-19 removed both — but a tab
    without a pane is a dead control that switches to nothing, and a pane
    without a tab is content no reader can reach. Neither shows up in any
    other check here, and the JS silently does nothing in both cases.
    """
    for m in re.finditer(r'<section class="sec" id="([a-z0-9]+)"', TEMPLATE):
        sec = TEMPLATE[m.start():TEMPLATE.find("</section>", m.start())]
        tabs = set(re.findall(r'data-p="([a-z0-9]+)"', sec))
        panes = set(re.findall(r'class="pane[^"]*" id="([a-z0-9]+)"', sec))
        if not tabs and not panes:
            continue
        assert tabs == panes, (
            f'#{m.group(1)}: tabs {sorted(tabs)} vs panes {sorted(panes)}')


@check("the Book section carries the whole-book depth, not just the chapter")
def _():
    start = TEMPLATE.index("{% if 'book' in secs and book %}")
    sec = TEMPLATE[start:TEMPLATE.index("</section>", start)]
    for field in ("book.crux", "book.learnings", "book.examples", "book.adapt"):
        assert field in sec, f"{field} did not survive the move out of #desk"


@check("the four pillars exist, and nothing lives outside one")
def _():
    """The rebuild's core promise, as an assertion.

    32 sections before the restructure, 32 after; 33 once IPO Radar was added
    on 2026-08-21. A section that drifts into a group outside the four pillars
    is orphaned functionality — reachable by scrolling, invisible in navigation.

    The count is a canary for sections appearing by ACCIDENT, so it moves only
    with a deliberate addition and a note saying which one. Bumping it to make
    a red test go green, without knowing what the new section is, defeats the
    only thing this line does.
    """
    PILLARS = {"Signal", "Research", "Desk"}          # the / page
    LIFE = {"Career", "Learning", "Practice", "Mind", "Drills"}   # the /desk page
    for _i, _l, page, group in SECTION_MAP:
        allowed = PILLARS if page == "main" else LIFE
        assert group in allowed, f"{_i} is in {group!r}, which is no pillar"
    assert len(SECTION_MAP) == 33, f"section count changed: {len(SECTION_MAP)}"


@check("every section that existed before the rebuild still exists")
def _():
    """Named one by one rather than counted, so a swap cannot pass."""
    expected = {
        "world", "marketintel", "findings", "longterm", "tracker", "sip",
        "funds", "swp", "stocks", "ipos", "picks", "paperwallet", "alerts",
        "perf", "rules", "datahealth", "who",
        "careers", "interview", "brief", "smartreads", "podcasts", "chess",
        "language", "father", "wisdom", "book", "review", "desk", "mind",
        "way", "gym",
    }
    have = {i for i, _l, _p, _g in SECTION_MAP}
    assert not (expected - have), f"DELETED: {sorted(expected - have)}"


@check("no theme-dependent colour is hardcoded outside a token declaration")
def _():
    """A near-white or near-black hex in a rule is legible in ONE theme.

    Nine of these existed: the Trade Ideas cards rendered a near-black gradient
    under dark ink in light mode and were unreadable, every ticker symbol on the
    site was #E6EAF0 on paper, and the hero orbs — a glow tuned to be additive
    on a dark ground — read as a grey smudge. All were invisible to review
    because each looked correct in the theme its author was using.

    Token DECLARATIONS are exempt: that is the one place a literal belongs, and
    both themes declare their own value for the same name. Everything else must
    read a token.

    Threshold is relative luminance: above 0.72 is near-white, below 0.20 is
    near-black. Mid-tones are theme-agnostic enough to pass either way.
    """
    css = re.search(r"<style>(.*)</style>", TEMPLATE, re.S)
    assert css, "no <style> block in TEMPLATE"
    offenders = []
    for n, line in enumerate(css.group(1).split("\n"), 1):
        stripped = line.lstrip()
        # token declarations and comments are where literals are allowed
        if stripped.startswith(("--", "/*", "*")):
            continue
        for hexv in re.findall(r"#([0-9A-Fa-f]{6})\b", line):
            r, g, b = (int(hexv[i:i + 2], 16) for i in (0, 2, 4))
            lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
            if lum > 0.72 or lum < 0.20:
                offenders.append(f"#{hexv} (lum {lum:.2f}) in: {stripped[:70]}")
    assert not offenders, "hardcoded theme-dependent colour:\n    " + \
        "\n    ".join(offenders)


def main() -> int:
    passed = failed = 0
    for name, fn in CHECKS:
        try:
            fn()
        except AssertionError as e:
            print(f"  FAIL  {name}  {e}")
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
