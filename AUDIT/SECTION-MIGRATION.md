# Section migration — Read/Research/Trade/Trust → SIGNAL / RESEARCH / DESK / LIFE

Written before the code changed, as the rebuild brief requires.

**Restore point: `v1.0-pre-4pillar`** (tag and branch, both on origin).
`git checkout v1.0-pre-4pillar` returns the site to the state audited below.

## The rule this document exists to prove

> Zero deleted sections. Zero orphaned functionality. Every capability that
> was reachable before is reachable after.

32 sections existed before. 32 exist after. Nothing was removed, nothing
merged away, nothing hidden behind a feature flag.

---

## `/` — SIGNAL · RESEARCH · DESK

| Existing section | Was | New pillar | Status |
|---|---|---|---|
| Market Intel | Read | **Signal** | Preserved |
| Trade Ideas | Trade | **Signal** | Preserved |
| World | Read | **Research** | Preserved |
| Findings | Research | **Research** | Preserved |
| Long-Term | Research | **Research** | Preserved |
| Stock Screen | Research | **Research** | Preserved |
| New Listings | Research | **Research** | Preserved |
| Fund Screen | Research | **Research** | Preserved |
| SIP Buckets | Research | **Research** | Preserved |
| SWP | Research | **Research** | Preserved |
| Portfolio | Research | **Desk** | Preserved |
| Paper Wallet | Trade | **Desk** | Preserved |
| Signal Log | Trade | **Desk** | Preserved |
| Performance | Trade | **Desk** | Preserved |
| Engine Log | Trust | **Desk** | Preserved |
| Data Health | Trust | **Desk** | Preserved |
| Who | Trust | **Desk** | Preserved |

## `/desk` — LIFE

The whole page becomes the LIFE pillar; the nav groups become its secondary
navigation, which is the "pillar + contextual sub-nav" the brief asks for.

| Existing section | Was | New group | Status |
|---|---|---|---|
| Finance Careers | Work | **Career** | Preserved |
| CFO Track | Work | **Career** | Preserved |
| Daily Brief | Reading | **Learning** | Preserved |
| Smart Reads | Reading | **Learning** | Preserved |
| Book | Library | **Learning** | Preserved |
| Podcasts | Drills | **Learning** | Preserved |
| Language | Practice | **Practice** | Preserved |
| Father | Practice | **Practice** | Preserved |
| Wisdom | Practice | **Mind** | Preserved |
| The Mind | Library | **Mind** | Preserved |
| The Way | Library | **Mind** | Preserved |
| The Review | Library | **Mind** | Preserved |
| The Desk | Library | **Mind** | Preserved |
| Chess | Drills | **Drills** | Preserved |
| Mind Gym | Drills | **Drills** | Preserved |

---

## Two placements where I departed from the brief's own table, and why

**Market Intel → SIGNAL, not RESEARCH.**
The brief's migration table (§3) says Research; its own pillar definition (§2)
lists FII/DII, Sector Heatmap and Corporate Actions under SIGNAL. Those three
things *are* Market Intel — the section's heading is literally "What moved the
tape today". Following §2. Reachable either way; this is the placement that
matches what the section contains.

**Who → DESK.**
Not named anywhere in the brief. It is the accountability page — who runs this
and on what basis — so it sits at the end of the operator's own pillar rather
than being orphaned.

---

## Not migrated, because they are not sections

These remain exactly where they are and are listed so the audit is complete:

| Capability | Where it lives | Note |
|---|---|---|
| `/data-health.json` | published artefact | machine-readable, unchanged |
| `/screen.json`, `/screen-detail.json` | published artefacts | unchanged |
| `/today.json` | published artefact | the Telegram bot reads it |
| `/jobs.json` | published artefact | careers feed |
| Ticker rail, market strip | global shell | above the nav on both pages |
| Command palette (⌘K) | global shell | already present |
| Signal detail sheet | modal | opened from the Signal Log |
| Stock detail sheet | modal | opened from the Stock Screen |

---

## What the brief asks for that does NOT yet exist

Named honestly rather than quietly skipped. None of these are regressions —
they are new capability the four-pillar architecture makes room for:

| Asked for | State |
|---|---|
| Watchlist | Not built. DESK has a slot for it. |
| Comparison engine (2–5 stocks/funds) | Not built. |
| Saved items across pillars | Not built. |
| Notification centre | Not built — Telegram carries alerts today. |
| Career: applications, interviews, fit tracking | Feed exists; tracking does not. |
| Chess: playbook, recurring-mistake store | Games and analysis exist; the playbook does not. |
| Books: reading queue, progress, personal notes | Summaries and takeaways exist; tracking does not. |
| Learning: skills, courses, paths | Language/Interview/Wisdom exist; no progress model. |
| Ideas: capture and status | Not built. |

The architecture below is what lets these be added without moving anything
again — which is the actual point of doing the restructure first.
