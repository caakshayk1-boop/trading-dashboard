#!/usr/bin/env python3
"""
resources.py — the Claude learning shelf rendered on /desk.

A curated list, not a scrape: these are links worth keeping, so they live in
source where they can be reviewed, rather than being re-fetched from somewhere
that might change under us.

Every URL here was checked live before it shipped. Three from the original
list did NOT survive and are deliberately absent rather than published broken:

    github.com/travisvn/claude-d3js-skill   404
    github.com/travisvn/loki-mode           404
    claudeinsider.com/docs/getting-started  402 Payment Required

`verify()` re-checks them on demand — run it before adding anything, and treat
a non-200 as a reason to drop the entry, not to ship it with a caveat. A
resource list whose links rot is worse than no list, because the reader has to
test each one themselves.
"""
from __future__ import annotations

import concurrent.futures as _cf
import urllib.request

_UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36")}

# (group, title, url, one-line note on why it is here)
RESOURCES: list[tuple[str, str, str, str]] = [
    # ── Official docs ──
    ("Docs", "Claude Code best practices",
     "https://code.claude.com/docs/en/best-practices",
     "Anthropic's own guidance on working with Claude Code day to day."),
    ("Docs", "Prompting best practices",
     "https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices",
     "The prompt-engineering reference, from the people who trained the model."),
    ("Docs", "The Complete Guide to Building Skills",
     "https://resources.anthropic.com/hubfs/The-Complete-Guide-to-Building-Skill-for-Claude.pdf",
     "How Skills work and how to write one that survives contact with real tasks. PDF."),
    ("Docs", "Claude's Constitution",
     "https://www.anthropic.com/constitution",
     "What the model is trained to value — useful context for why it refuses what it refuses."),

    # ── Repos ──
    ("Repos", "Claude Code (official)",
     "https://github.com/anthropics/claude-code",
     "The CLI itself. Issues here are the fastest signal on what is changing."),
    ("Repos", "Claude Cookbooks",
     "https://github.com/anthropics/claude-cookbooks",
     "Runnable notebooks: tool use, RAG, evaluation, structured output."),
    ("Repos", "Claude Agent SDK",
     "https://github.com/anthropics/claude-agent-sdk",
     "For building agents on the same loop Claude Code uses."),
    ("Repos", "Awesome Claude Skills",
     "https://github.com/travisvn/awesome-claude-skills",
     "Community index of published Skills — the quickest way to see the shape of a good one."),
    ("Repos", "Superpowers",
     "https://github.com/obra/superpowers",
     "A large community Skill collection; worth reading for structure even where unused."),
    ("Repos", "Skill Seekers",
     "https://github.com/yusufkaraaslan/Skill_Seekers",
     "Tooling for discovering and packaging Skills."),

    # ── Videos ──
    ("Video", "Mastering Claude Code in 30 minutes",
     "https://www.youtube.com/watch?v=6eBSHbLKuN0",
     "Fastest orientation if you want the shape of the tool before the detail."),
    ("Video", "Claude full course (1 hour)",
     "https://www.youtube.com/watch?v=KrKhfm2Xuho",
     "Longer build-and-automate walkthrough."),
    ("Video", "36 Claude tips for beginners",
     "https://www.youtube.com/watch?v=9vM4p9NN0Ts",
     "Short, practical, mostly about not fighting the tool."),
    ("Video", "Ultimate Claude guide 2026",
     "https://www.youtube.com/watch?v=WGbjP8q79i4",
     "Current-generation overview."),
    ("Video", "Automate any task — full workflow",
     "https://www.youtube.com/playlist?list=PLtPgUfajvh_YNdUozVRM15RLYAMG5x_h6",
     "Playlist. End-to-end automation rather than isolated tricks."),

    # ── Papers ──
    # The ideas the agent loop is actually built on. Worth reading in this
    # order: ReAct first, it is the one the others extend.
    ("Papers", "ReAct: reasoning + acting",
     "https://arxiv.org/abs/2210.03629",
     "The interleaved reason/act loop every tool-using agent is a descendant of."),
    ("Papers", "Chain-of-Thought prompting",
     "https://arxiv.org/abs/2201.11903",
     "Why asking for the working changes the answer."),
    ("Papers", "Tree of Thoughts",
     "https://arxiv.org/abs/2305.10601",
     "Search over reasoning paths instead of one greedy chain."),
    ("Papers", "Reflexion",
     "https://arxiv.org/abs/2303.11366",
     "Self-critique between attempts — the idea behind retry-with-feedback."),
    ("Papers", "Toolformer",
     "https://arxiv.org/abs/2302.04761",
     "Models learning when to call a tool, not just how."),
    ("Papers", "Generative Agents",
     "https://arxiv.org/abs/2304.03442",
     "Memory, reflection and planning in long-running agents."),
]

GROUP_ORDER = ["Docs", "Repos", "Video", "Papers"]


def grouped() -> list[dict]:
    """Resources bucketed for rendering, in GROUP_ORDER."""
    out = []
    for g in GROUP_ORDER:
        items = [{"title": t, "url": u, "note": n}
                 for grp, t, u, n in RESOURCES if grp == g]
        if items:
            out.append({"group": g, "items": items})
    return out


def verify(timeout: int = 25) -> list[tuple[str, int]]:
    """(url, status) for every entry. 0 means it did not resolve at all."""
    def check(u: str) -> tuple[str, int]:
        for method in ("HEAD", "GET"):
            try:
                req = urllib.request.Request(u, headers=_UA, method=method)
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return u, r.status
            except Exception as e:                           # noqa: BLE001
                code = getattr(e, "code", None)
                if code and method == "HEAD":
                    continue                                 # retry as GET
                if code:
                    return u, code
        return u, 0

    urls = [r[2] for r in RESOURCES]
    with _cf.ThreadPoolExecutor(10) as ex:
        return list(ex.map(check, urls))


if __name__ == "__main__":
    bad = [(u, s) for u, s in verify() if s != 200]
    print(f"{len(RESOURCES)} resources, {len(bad)} not returning 200")
    for u, s in bad:
        print(f"  {s:4} {u}")
    raise SystemExit(1 if bad else 0)
