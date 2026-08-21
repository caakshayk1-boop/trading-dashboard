# PRODUCT AUDIT — Phase 0
**Date:** 2026-08-21 · **Repo:** `~/Downloads/trading-dashboard` · **Site:** news.askakshay.com
**Status:** audit only. No production change made. Nothing deployed.

> Written before implementation, per the Phase 0 instruction. Every number below was
> recomputed from `data/all_signals.json` in this session; every code claim is cited
> to file:line. Claims I could not verify are tagged.

---

## 0. STOP-THE-LINE — the ledger is mis-graded

The flagship pillar of the redesign is **THE RECORD** — "turn the signal history into a
public audit trail." That cannot be built yet. The ledger it would publish is wrong.

### 0.1 What was asked
> "118 still open — not counted above??"

Correct: `signal_report.py:84` computes expectancy over `closed` only, and
`NOT_CLOSED = ("OPEN","T1_HIT","CANCELLED","VOID")` (`signal_report.py:39`).
Open positions are excluded by design and the footer says so.

**That exclusion is real but it is the smaller problem.** Investigating it surfaced a
grading defect that inverts the headline result.

### 0.2 The defect — `status` and `r_multiple` contradict the price columns

The table has two groups of columns and they disagree with each other.

> **Corrected 2026-08-21 after the bar rebuild (§0.5).** I first concluded that
> `status`/`r_multiple` were the corrupt pair and `exit_price`/`pnl_pct` the trustworthy
> one. Replaying the bars showed that is right for the 85 `SL_HIT` rows and **backwards
> for the 26 `T2_HIT` rows**, where `status` and R were correct and `exit_price` had been
> overwritten with the stop. Corruption exists in *both* column groups, on different row
> sets. Neither is globally trustworthy — which is why an in-place arithmetic repair
> would have been wrong and the rebuild was necessary. The section below is left as the
> observation; §0.5 carries the resolved answer.

**Group 1 — internally consistent, 573/573 rows.** `pnl_pct` reproduces exactly from
`entry` → `exit_price` given `action`, on **every single graded row, zero exceptions**.
And those exit prices land on the signal's own declared levels: 236/327 `SL_HIT` rows
exit exactly at `sl`, 78/108 `T2_HIT` rows exit exactly at `target2`.

**Group 2 — `status` and `r_multiple`.** These contradict Group 1 on **168 of 573 graded rows (29.3%)**
(58 rows: `pnl_pct` negative but R positive · 92 rows: `pnl_pct` positive but R negative).

The contradiction is not noise. It is a clean swap, in both directions:

**Winners stamped as stop-outs — 85 rows.** `status = SL_HIT`, `r_multiple = −1.00`,
while the exit price is at the *target* and `pnl_pct` is *positive*:

| symbol | date | action | entry | SL | exit_price | pnl_pct | stored R | implied R |
|---|---|---|---|---|---|---|---|---|
| SILVER | 2026-07-23 | BUY | 60.100 | 59.3109 | **62.0727** | **+3.28%** | **−1.00** | +2.50 |
| GOLD | 2026-07-21 | BUY | 4067.80 | 4044.73 | **4125.47** | **+1.42%** | **−1.00** | +2.50 |
| NATGAS | 2026-07-21 | SELL | 2.8510 | 2.8816 | **2.7745** | **+2.68%** | **−1.00** | +2.50 |

All 85 carry stored R of exactly −1.00.

**Losers stamped as target hits — 26 rows.** `status = T2_HIT`, `r_multiple = +2.50`,
while the exit price is exactly at the *stop* and `pnl_pct` is *negative*:

| symbol | action | entry | SL | target2 | exit_price | pnl_pct | stored R |
|---|---|---|---|---|---|---|---|
| HAL | BUY | 4646.50 | 4558.86 | 4865.61 | **4558.86** | **−1.89%** | **+2.50** |
| MOTHERSON | BUY | 150.80 | 146.75 | 160.75 | **146.75** | **−2.69%** | **+2.46** |
| NATGAS | SELL | 2.748 | 2.770 | 2.693 | **2.770** | **−0.80%** | **+2.50** |
| SILVER | SELL | 58.755 | 59.3695 | 57.2188 | **59.3695** | **−1.05%** | **+2.50** |

Of 108 `T2_HIT` rows, 78 exit at `target2` and 4 elsewhere; these 26 are the only ones
exiting at the stop. A separate 31 `TIME_STOP` rows also exit exactly at their stop but
book less than −1R (WTIUSD −0.553R, GRASIM **+0.737R**, BAJAJ-AUTO **+1.048R**).

All of them carry `regraded_at = 2026-08-08`. They were written by the re-grade, not by
the live scanner. **The 2026-08-08 repair is the source of the defect.**

### 0.3 Third defect — EXPIRED is marked to whatever price the job saw

65 rows are `EXPIRED`. `regrade.py:269` **excludes EXPIRED from re-grading**, so the
2026-08-08 pass never touched them. `standalone_scan.py:529` books a time stop at
`last_close` — the most recent price at the moment the job ran, not the price at the end
of the signal's horizon.

Evidence it is marking to one recent price rather than to an exit: EXPIRED rows for the
same symbol on different dates share an identical exit — GOLD 4414.80 (twice), GOLD
4414.50, SILVER 64.48 (twice), CRUDE 78.28. Stored R ranges **−33.33R to +54.97R** on
signals whose stated horizon is one hour. `entry_triggered_at` is **null on all 65** —
none was ever recorded as having filled.

`fix_horizons.py` exists precisely because this was known. It was never completed.

### 0.4 Corrected numbers — and why a single "corrected" figure does not exist

Re-deriving R from the trustworthy columns as `(exit−entry)/|entry−sl|`, floored at −1R:

**Full ledger (714 rows):**

| Basis | n | Expectancy | SE | t | Win rate |
|---|---:|---:|---:|---:|---:|
| As published | 573 | +0.353R | 0.195 | +1.81 | 34.2% |
| Drop EXPIRED only | 508 | −0.050R | 0.065 | −0.77 | 29.7% |
| Re-derive R, keep EXPIRED | 573 | +0.822R | 0.178 | +4.61 | 40.3% |
| **Re-derive R, drop EXPIRED** | 508 | **+0.274R** | 0.075 | **+3.63** | 36.4% |

**Last 30 days (267 rows) — same repair, opposite sign:**

| Basis | n | Expectancy | SE | t | Win rate |
|---|---:|---:|---:|---:|---:|
| As published | 155 | +0.224R | 0.183 | +1.23 | 36.8% |
| Drop EXPIRED only | 136 | −0.064R | 0.121 | −0.53 | 31.6% |
| Re-derive R, keep EXPIRED | 155 | +0.010R | 0.187 | +0.05 | 26.5% |
| **Re-derive R, drop EXPIRED** | 136 | **−0.308R** | 0.125 | **−2.47** | 19.1% |

Read those together before drawing a conclusion. The same repair takes the full ledger
from insignificant to **+0.274R at t = +3.63**, and the 30-day window from insignificant
to **−0.308R at t = −2.47**. Both are "significant" and they point in opposite directions.

The reason: the 85 mis-stamped *winners* are concentrated in July commodity rows
(SILVER/GOLD/NATGAS), which sit outside the 30-day window, while the recent window is
dominated by breakout and cf_1h. This reproduces the known window-sensitivity recorded
earlier — full ledger +0.207R, last-200 window −0.126R.

**Therefore, tagged honestly:**
- **Certain:** the 168 contradicting rows are wrong. A row whose exit price is at its
  target with positive `pnl_pct` is not a −1.00R stop-out, and the reverse. No
  interpretation rescues those.
- **Certain:** the published +0.353R / +0.213R figures are computed from the corrupted
  column and are not a measurement of anything.
- **Likely:** `entry` / `exit_price` / `pnl_pct` are the trustworthy group — based on
  573/573 internal consistency and exits landing on declared levels.
- **Assumed:** re-deriving R from those columns recovers the true result. If wrong —
  i.e. if `exit_price` was also clobbered on non-EXPIRED rows the way it was on EXPIRED
  ones — then **no expectancy figure is recoverable from this table at all** and the
  ledger must be rebuilt from price history rather than repaired in place.
- **Not knowable from this table:** a single headline expectancy. The full-ledger and
  30-day repairs disagree in sign.

This rests on the assumption above. If it is wrong, the conclusion is not "a different
number" — it is "there is no number", and THE RECORD cannot be published in any form
until the ledger is rebuilt from bars.

**Consequence for the redesign:** THE RECORD, Performance, Paper Lab, and every
evidence claim on the Engine page read from this table. None can be built yet.

### 0.5 RESOLVED — rebuilt from bars

`rebuild_ledger_from_bars.py` re-fetches OHLC for every graded signal and walks the bars
forward from the signal date, taking the first level actually touched. It reuses
`standalone_scan._max_hold_hours` rather than copying the horizon table, and it never
reads bars that predate the signal — the failure mode that produced the HINDALCO phantom
stop at a price four days older than the signal itself.

**Coverage: 157/157 symbol series fetched, zero failures, zero empty.**

| | rows |
|---|---:|
| regraded, **unchanged** | **400** (73%) |
| regraded, **changed** | **150** |
| could not regrade (horizon not yet elapsed → correctly left OPEN) | 23 |
| ambiguous (one bar spanned both levels) | **0** |

400 rows agreeing is the control: the method reproduces the ledger wherever the ledger
is sound, so the 150 changes are signal, not method drift.

**Result:**

| | n | Expectancy | SE | t | Win rate | Total |
|---|---:|---:|---:|---:|---:|---:|
| Published, full ledger | 573 | +0.353R | 0.195 | +1.81 | 34.2% | +202.4R |
| **Rebuilt, full ledger** | 550 | **+0.270R** | **0.071** | **+3.80** | 37.3% | +148.7R |
| Published, last 30d | 155 | +0.224R | 0.183 | +1.23 | 36.8% | +34.8R |
| **Rebuilt, last 30d** | 134 | **+0.057R** | 0.141 | +0.41 | 30.6% | +7.7R |

The point estimate falls, but the standard error collapses from 0.195 to **0.071** and
t rises from +1.81 to **+3.80**. The published figure was a large number built from
noise; the rebuilt one is a smaller number that is actually measured.

**Independent confirmation:** the in-place re-derivation in §0.4 (drop EXPIRED, recompute
R from `exit_price`) gave **+0.274R**. The bar replay — a completely different route,
using price history rather than the stored exit — gives **+0.270R**. Two methods that
share no inputs beyond entry/sl converge within 0.004R. *Certain:* the full-ledger edge
is approximately **+0.27R**, not the published +0.353R.

**The last 30 days show no edge** (+0.057R, t=+0.41). The recent window is not
significantly different from zero in either direction.

**Spot checks against the known-bad rows:**

| signal | published | rebuilt from bars |
|---|---|---|
| NATGAS 2026-07-08 | EXPIRED **−33.33R** @ 2.75 | SL_HIT **−1.00R** @ 3.2641 |
| SILVER 2026-07-21 | EXPIRED **+8.83R** @ 64.485 | T2_HIT **+2.50R** @ 60.5705 |
| EICHERMOT 2026-06-11 | EXPIRED **+32.20R** @ 8018.00 | T2_HIT **+2.50R** @ 7285.65 |
| WTIUSD 2026-08-06 | TIME_STOP −0.55R @ 80.59 | SL_HIT **−1.00R** @ 80.59 |
| NATGAS 2026-07-22 | SL_HIT −1.00R | T2_HIT **+2.50R** (mis-stamped winner recovered) |
| HAL 2026-08-01 | T2_HIT +2.50R @ **4558.86** (the stop) | T2_HIT +2.50R @ **4865.61** (target2) |

The HAL row is the one that corrected my diagnosis: status and R were right, `exit_price`
was the clobbered column.

**Transitions (150 changed rows):**

| from → to | rows |
|---|---:|
| EXPIRED → T2_HIT | 39 |
| SL_HIT → T2_HIT | 27 |
| TIME_STOP → SL_HIT | 26 |
| TIME_STOP → T2_HIT | 21 |
| EXPIRED → SL_HIT | 13 |
| TIME_STOP → TIME_STOP (R corrected) | 10 |
| EXPIRED → TIME_STOP | 5 |
| other | 9 |

### 0.6 Fourth defect — premature time stops

23 rows the ledger has already closed as `TIME_STOP` have **not reached their horizon**
(GRASIM 2026-08-05, EICHERMOT 2026-08-03, BAJAJ-AUTO 2026-07-30). The rebuild correctly
leaves them open. They were closed early and booked as results.

### 0.7 Status: nothing applied

`rebuild_ledger_from_bars.py` is **dry-run by default and has no DB write path**.
`--apply` refuses to run without `--backup-dir`, snapshots the source, and then stops —
the tracker update is deliberately unwired so that overwriting published grades is a
separate, deliberate act. `data/all_signals.json` is untouched.

### 0.11 The open book is a different product from the closed book

The 112 open rows in the window are not a random sample of the 155 closed ones:

| | dominant signal types |
|---|---|
| **Closed** (155) | breakout 56 · cf_1h 40 · 4h 16 · ai_4h 12 — all fast engines |
| **Open** (112) | magic 24 · magicmagic 24 · multibagger 14 · top5_pick 13 · ai_longterm 13 — all slow engines |

`magic`, `magicmagic`, `multibagger`, `top5_pick` and `ai_longterm` contribute **88 of
112 open positions and near-zero closed rows.** The published expectancy therefore measures
the intraday engines only, while the slow engines accumulate unresolved positions that
never enter any statistic. 67 of 112 have been open 7+ days. `max_profit_pct` and
`max_drawdown_pct` are **null on all 112**, so they cannot even be marked to market
from the ledger as it stands.

### 0.12 `magic` / `magicmagic` is a duplicated signal type

36 rows each. 10 share an identical (symbol, date, entry) key — e.g. PARADEEP
2026-08-15 @147.85 exists as id 764 (`magic`) and id 775 (`magicmagic`). One engine,
two type names, partial double-writes. Any per-engine breakdown is currently splitting
one engine across two rows and double-counting the overlap.

### 0.13 Dead status branch
`T1_HIT` is listed in `NOT_CLOSED` (`signal_report.py:39`) but **zero rows in the entire
714-row table carry it.** Partial-target exits either never fire or are written as
`T2_HIT`. Given §0.2 found stop-outs written as `T2_HIT`, these are likely related.

---

## 1. BUILD SCHEDULE — already done, but the copy was never updated

**Request:** move the daily build from 6:00 AM IST to 6:00 AM MYT (UTC+8).

**Status: cron layer COMPLETE.**
- `.github/workflows/newspaper.yml:5` → `cron: "0 22 * * *"` — 6:00 AM MYT ✓
- `.github/workflows/scheduled_tasks.yml:9` → `cron: '0 22 * * *'` — 6:00 AM MYT ✓
- `.github/workflows/jobs.yml:40` → already commented "16:00 MYT" ✓

**Status: copy layer NOT DONE — 17 user-visible strings still say IST.**

| file:line | string |
|---|---|
| `newspaper.py:2196` | meta description: "rebuilt at 6 AM IST daily" |
| `newspaper.py:3962` | `og:description`: "Rebuilt 6 AM IST." |
| `newspaper.py:6552` | on-page eyebrow: "◆ Compiled 6:00 AM IST" |
| `newspaper.py:7087` | empty state: "the weekly scan runs with the 6 AM IST build" |
| `newspaper.py:8405` | email signup: "One email a day at 6 AM IST" |
| `newspaper.py:9715` | footer: "Rebuilt every morning at 6 AM IST" |
| `generate.py:4` | docstring |
| `daily_brief.py:3,590,794` | brief header text shown to reader |

**Also a live conflict:** `newspaper.py:9943` registers an in-process APScheduler job at
`CronTrigger(hour=0, minute=30, timezone="UTC")` = 6 AM **IST**, which now disagrees
with the GitHub Actions cron at 22:00 UTC. Two schedulers, 2.5 hours apart.
*Assumed:* the APScheduler path is dormant because the site builds via Actions, not a
long-running process. If wrong, the site is being rebuilt twice a day at two different
times. Needs one check before I touch it.

---

## 2. MISSING SECTION HEADINGS — confirmed, and worse than reported

The request lists four sections with missing headers. Verified by grepping both the
Jinja template and the built HTML:

| Label | in `newspaper.py` | in built `docs/index.html` |
|---|---|---|
| "Corporate Actions" | **0** | **0** |
| "Recently Listed" / "Recent Listed" | **0** | **0** |
| "Step-Up" / "Step Up" | **0** | **0** |
| "SIP Bucket" (singular) | 0 — registered as "SIP Buckets" (`newspaper.py:2143`) | — |

These are **not headers that broke**. They were never written. The corporate-actions
data is rendered at `newspaper.py:6906` as a bare block with no heading above it — an
unlabelled table, which is exactly the "unexplained data" problem in the brief.

"Step-Up" appears nowhere in the codebase at all. *Assumed:* it is planned, not built.
Confirm before I design a header for a section that does not exist.

---

## 3. INFORMATION ARCHITECTURE — closer to the target than the brief assumes

The brief asks for a four-pillar IA and a separate Life product. **Both already exist
in skeleton form** (`newspaper.py:2120–2190`):

- **main page** pillars: `Signal` (Market Intel, Trade Ideas) · `Research` (World,
  Findings, Own the Business, Stock Screen, New Listings, Fund Screen, SIP Buckets, SWP)
  · `Desk` (Portfolio, Paper Wallet, Signal Log, Performance, Engine Log, Data Health, Who)
- **`/desk` page** = the Life product already: Career, Learning, Practice, Mind, Drills.

**Implication for the two-product split:** this is a re-pillaring and extraction job,
not a greenfield build. The proposed `Market / World / Research / Desk` maps onto the
existing `Signal / Research / Desk` with World promoted out of Research. The Life
product is the existing `/desk` page lifted into its own surface.

**Gap vs the brief:** `IPO Radar` does not exist as a decision engine. `ipo_tracker.py`
+ `New Listings` is a historical listings table. The brief is explicit that these are
different products. This is genuinely new work.

---

## 4. WHAT I HAVE NOT AUDITED YET

Stated plainly rather than implied complete:
- `docs/index.html` is 503 KB and `app.js` 274 KB — not yet read end-to-end. No claim
  made about CSS architecture, animation, responsive behaviour, or accessibility.
- No Lighthouse run. No mobile testing. No contrast checks.
- Vercel config, DNS, env vars: **deliberately not inspected or touched** per the
  do-not-deploy rule.
- The `$MRK`, `OHL` and `Wallet` trade-selection questions: not yet traced to their
  engines.
- `akk-terminal` and `tradeflow-pro` not audited — out of scope for this pass.

---

## 5. RECOMMENDED ORDER

The brief's 19 phases start with design system. I would not start there.

1. **Decide the ledger repair** (§0). It changes published numbers. Not my call.
2. Fix `magic`/`magicmagic` (§0.6) and backfill excursions on open rows (§0.5) — both
   are prerequisites for any honest Performance page.
3. Update the 17 schedule strings + resolve the double scheduler (§1). Low risk, and
   the site is currently lying to readers about when it updates.
4. Write the four missing headings (§2).
5. **Then** design system → IA → components → data → interaction → animation → QA.

Animation is step 7 of 7, per the explicit instruction in the brief.
