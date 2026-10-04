# Trading Dashboard — Claude Context

## What This Is
Python trading system: market scanner + signals DB + Telegram alerts + Upstox integration.

## Stack
Python 3 | SQLite (`signals.db`) | Upstox API | Telegram Bot API

## Files
- `dashboard.py` — main dashboard
- `scanner.py` — market scanner, generates signals
- `telegram_bot.py` — Telegram alert bot
- `tracker.py` — position/portfolio tracker
- `scheduler.py` — cron-style task runner
- `upstox_provider.py` — Upstox API integration
- `mf_tracker.py` — mutual fund tracker (BMW Fund SIP tracking)
- `config.py` — config (DO NOT commit secrets)
- `config_template.py` — template for config

## Commands
- Run scanner: `python3 scanner.py`
- Run bot: `python3 telegram_bot.py`
- Run dashboard: `python3 dashboard.py`

## Signal V2 cutover — 2026-10-01 (READ FIRST)
Signal V1 is retired. Every engine this repo ran as a trade publisher — BREACH,
VECTOR, TIDAL (magic/magicmagic), LEDGE, KEEL, PIVOT, ASCENT, NORTH, GUST,
BUOY/ANCHOR/BEDROCK, PLUMB, cf_1h, commodity, ohl, the US daily picks and the
Vision legacy engines — files nothing, grades nothing and alerts nothing.

- `v1_cutover.py` is the switch, in CODE: `standalone_scan.main`,
  `scheduled_tasks_runner` (cf_scan/ai_longterm), `scan_research.main`,
  `vision_scan.run`, `ai_longterm.main` and every `tracker` ledger writer and
  grader stand down after 2026-10-01 00:00 IST. `V1_UNFREEZE=1` lifts it for a
  deliberate restore only. `test_v1_cutover.py` drives each of them.
- `daily_scan.yml`, `research.yml` and `vision_scan.yml` have NO schedule, and
  the cf_scan crons are gone from `scheduled_tasks.yml`. Removing a cron alone
  was not enough (dispatch and the watchdog can still start a job), hence the
  code switch.
- V1 rows are not deleted. `v1_archive.py` / `v1_archive.yml` copy every V1
  table to `v1_archive_<table>` with a checksummed manifest, then end OPEN V1
  rows with `status=ARCHIVED`, `lifecycle_status=ARCHIVED_V1`. No exit, P&L or
  R is invented, and `restore-open` reverses it. A private copy of the ledger
  exports and the V1 public feeds is in the private vision-engine repo,
  `archive/signal-v1/`.
- **V2 is not built here.** One private engine (`caakshayk1-boop/vision-engine`)
  publishes ONE canonical plan feed, `feeds/signal_v2.json`
  (`signal-v2-public/1`), which Signal and Vision both read. Never add
  selection logic for V2 to this public repo.
- The V1 regression suites (`test_alert_pipeline`, `test_engine_regressions`,
  `test_vision_signals`) still run, with `V1_UNFREEZE=1` set inside them, as
  tests of archived logic.
- The brief links (`daily_brief._OWNS`) point at V2 routes: /opportunities and
  /performance.

## Stock Screen (`#stocks` on news.askakshay.com)
Nifty 500 research screen sitting directly above the Signal Log.

- `stock_screen.py` — the engine. Four SEPARATE scores (quality / growth /
  valuation / technical) plus a declared weighted composite. Weights live in
  `WEIGHTS`, nowhere else.
- `fundamentals.py` — `statements()` gives 4 fiscal years from yfinance and is
  where **real ROCE** comes from (EBIT / invested capital). Yahoo publishes no
  ROCE field; it is computed. 30-day cache, separate from the 7-day `.info` one.
- `.github/workflows/stock_screen.yml` — **daily** since 2026-08-27, 21:00 IST
  with a 23:00 IST retry, on its own clock. It was Sunday-only, and the volume
  board and breakout table are built entirely from price/RSI/turnover, so on a
  weekly cadence both spent six days describing last Friday while reading as
  today. **Never build this inside the 6 AM job** — ~11 min of sequential
  fetches.
- Payload → Turso `newspaper_screen` (NOT `newspaper_stocks_picked`, which is
  the daily picks). `generate.py` reads it and writes `docs/screen.json`.
- The UI lives in **`static/app.js`** — `docs/app.js` is a build artefact that
  generate.py overwrites. Editing the artefact silently loses the work.
- `docs/screen.json` needs allow-listing in THREE places: generate.py writes it,
  `.vercelignore` names it, `vercel-news/build.js` copies it.
- `python3 test_stock_screen.py` — 449 checks, offline, no pytest.
- **The benchmark vanished at 1,000 names.** 1,000 + `^NSEI` put the index alone
  in the 26th price batch; `fetch_prices` assumed a one-ticker download is flat
  (current yfinance returns `(field, ticker)` columns), the parse raised at
  DEBUG, and `nifty_1m`, `nifty_1y`, `price_date` and every relative-strength
  figure were published null. It now branches on the frame's shape;
  `test_screen_prices.py` drives the exact 1,001-ticker split.
- **The 52-week range is the TRADED range; a finished session keeps its day.**
  Prices download raw (`auto_adjust=False`); `_adjusted()` rebuilds the
  dividend-adjusted series for returns exactly as yfinance does, and `hr`/`lr`
  keep the prices that traded for `high52`/`low52` — what NSE and TradingView
  publish. And Yahoo served 1 Oct with no Close: the per-field `dropna()` lost
  the whole day, so the screen built at 03:35 IST on 2 Oct published 30 Sep, and
  ABLBL's low read ₹75.1 against a traded ₹73.41. A finished session with no
  close is now rebuilt from its last hourly bar (15:15 IST), never invented;
  what cannot be rebuilt is counted in `coverage.missing_close`/`behind`, and
  Data Health marks the screen DEGRADED above 2%. `test_screen_prices.py`.
- **`barometer.py` reads closed bars only** (`complete_bars`). A run landing
  after midnight IST got the new day's row with no Close, and `iloc[-1]` nulled
  Nifty and VIX — 45 of 100 weight — on 21, 28 and 29 Sep. `test_barometer.py`.

- **The Magic Formula (`magic_formula()`, `mf` on every row, `magic_formula` on
  the payload).** Greenblatt's two ranks, summed: ROCE (the screen's own, latest
  year) and EBIT/EV (EV = market cap + total debt − cash). Lenders, insurers and
  real estate (the screen's lender rule), utilities, market cap under ₹1,000 cr,
  missing or >18-month-old statements, and EBIT/EV not positive are UNRANKED with
  their reason, never zero-filled. It reads the LATEST year by definition, unlike
  the medians the scores read, so a margin jump is flagged `one_off`, not hidden.
  Separate from WEIGHTS and the composite; an input to nothing. The paper book
  that buys its top names lives in the private engine (`vision_eod/magic.py`).

Honesty rules the screen must keep (all pinned by tests):
- Missing data scores `None` and leaves its parent score's denominator; it is
  never zero-filled, and confidence drops instead of the score rising.
- No statements → no composite → unranked. A company that reports nothing must
  not outrank one that does.
- Negative D/E means negative equity, which is insolvency, not a clean balance
  sheet — it scores zero on leverage, not full marks.
- Scores read the multi-year MEDIAN, not the latest year, so a demerger or
  one-off cannot top the table.
- EPS growth is withheld entirely when the share count moved structurally.
- Nothing predicts. No probability, target or forecast anywhere.

## The trading brief's Business section (`brief_fundamentals.js`)
The ONE thing both briefs share. There are two: `signal.askakshay.com/brief`
(`public/signal.js` in the *signal* repo) and
`news.askakshay.com/next.html#/brief` (`static/next.js` here). They are forks
of one renderer and the first has moved a long way ahead.

- `static/brief_fundamentals.js` — composite, the four component scores, the
  PE-against-its-own-percentile widget, six ratio tables, and the screen's own
  risk grade and flags. **Authored HERE**, beside `stock_screen.py`, which
  computes every field it reads. Both briefs call
  `BriefFundamentals.render(row, opts)`; neither owns a copy.
- The signal repo's `sync-data.yml` mirrors it with the JSON feeds, for the
  same reason it mirrors them: it is a build artefact of the screen. That does
  **not** contradict the note in that workflow about no longer mirroring the
  frontend — what was removed there was a mirror of `signal.js`/`signal.css`,
  files with two authors that had already drifted. This one has one author and
  nothing downstream to overwrite.
- **It carries its own `<style>`.** A companion `.css` would be a second file
  to allow-list in four places here and a fifth over there, and the markup
  could then reach a page whose stylesheet did not. One file cannot arrive
  half-delivered.
- **Every custom property needs a literal fallback.** `signal.css` declares
  `--t-1..--t-10` and `--r-1..--r-5`; `next.css` writes its sizes as literals
  and has none of them. A bare `var(--t-4)` resolves to nothing there and the
  whole declaration is dropped. Pinned by `test_stock_screen.py`.
- It crosses a repo boundary twice a day, so **"it did not arrive" is a state**.
  Both callers render a notice saying so — a section that vanishes silently is
  indistinguishable from one that was never meant to be there.
- Same four allow-lists as everything else in `docs/`: written by
  `generate.py`, named in `.vercelignore`, copied by `vercel-news/build.js`,
  staged by `newspaper.yml`. Plus a fifth over there, pinned by the signal
  repo's `test/guard.mjs`.

Honesty rules it must keep (pinned by tests in both repos):
- Missing renders as the WORD for its absence, never as a zero — a zero is a
  measured result and an absence is not.
- No statements → no composite → the section says *unranked*, rather than
  rendering an empty grid that reads as a loading failure.
- Negative D/E is named as negative equity, which is insolvency.
- `screen-lite.json` keeps `risk.level` and **strips `risk.flags`**. "No flags"
  and "the flags are not in this projection" are different sentences; printing
  the first for a name graded HIGH with three is a projection's omission
  rendered as a measured result.
- **A lender is read with the screen's own rule.** `_is_financial` takes
  leverage, cash conversion, interest cover, margins and ROCE out of a bank's
  or NBFC's scores and risk grade; the renderers judged them anyway, so
  EDELWEISS read "Risk LOW" beside a red "heavily geared". Every copy of the
  rule (`brief_fundamentals.js`, `next.js`, `app.js`, and in the signal repo
  `insight.js`/`signal.js`) must be the same four words — pinned by
  `test_stock_screen.py` here and `guard.mjs` there. Level ratios are printed
  as levels: no "+", no green.
- **Dividend yield is deliberately absent.** `stock_screen.py` ran yfinance's
  `dividendYield` — already percentage points — through `_pct`, so the column
  read ITC 601%, VEDL 1256%, universe median 65.5%. The generator is fixed;
  restore the row once a screen built after that fix is being served.

## Data Health (`#datahealth` on news.askakshay.com)
The honesty layer. One vocabulary for how current every dataset is, so no
section can look more current than its data.

- `data_health.py` — the whole abstraction. Six statuses, ordered by severity
  (LIVE < FRESH < STALE < DEGRADED < FAILED < UNAVAILABLE). `assess()` collects
  every condition that fires and reports the WORST, so a new rule can only make
  a status worse — never quietly upgrade a broken dataset to FRESH.
- `generate.py::_register_health()` files every dataset once, before anything
  renders. The page badges and the health table read that ONE snapshot, so
  they cannot disagree about the same build.
- The badge is a Jinja macro, `{{ dh('Dataset name') }}`. A section must never
  phrase its own freshness — that is how "0.5d old" and "12h old" ended up on
  one page describing the same number.
- `job_runs` carries `records`/`expected` — the ATTEMPT's coverage, kept
  separate from the served payload's. A free-text detail like "only 50 priced"
  is unparseable, so no badge, test or API could ever act on it.
- `python3 test_data_health.py` — 46 checks, offline, no pytest.

Rules the layer must keep (all pinned by tests):
- A failed newer attempt behind valid data is DEGRADED, never STALE. The data
  is fine; the pipeline is not, and those are different sentences.
- A partial build is DEGRADED, never presented as a complete one.
- An unreadable build timestamp is DEGRADED, never FRESH.
- `expected_records` comes from the ATTEMPT, never the served payload —
  dividing a dataset by its own length always yields 100%.
- No denominator, no ratio. A made-up universe size is worse than none.

## Telegram (`standalone_scan.py` · `daily_brief.py`)
Two touches a day, on the operator's clock. Nothing else is scheduled to post.

- **08:00 MYT** — the morning brief. **20:00 MYT** — the night brief, alongside
  the day's ENTRY scan. Crons live in `scheduled_tasks.yml` and `daily_scan.yml`;
  the 13:00 MYT "signals open" scan is **gone**, and no morning scan replaces it
  (08:00 MYT is 05:30 IST, before the NSE opens — it could only re-report the
  previous close).
- Two touches is a claim about the PHONE, not about how often anything runs.
  Two other scans exist and neither adds a third weekday notification:
  - **11:30 IST / 14:00 MYT, weekdays** — the midday slot, GUST only.
    **GUST was promoted out of research tier on 2026-09-21** and now alerts,
    so this slot IS a third weekday touch. The decision was about the clock,
    not the record: it had been out of mandate "for trading a 15-minute chart,
    which this account cannot", and the account now can. The sample did not
    change — 17 closed, thirteen short of this book's thirty, all pre-launch,
    nothing since it was re-wired — and that is written above
    `ALERTS_SUPPRESSED` in `engine_names.py` rather than glossed.
    It was **silent on success** while suppressed: the slot used to fall
    through to the completion
    summary, which "always sends so you know scan ran", so a third message
    arrived every weekday saying nothing had happened — against `daily_scan.yml`'s
    own rationale for the cron, which is that it "adds no message to anybody's
    phone ... the only basis on which an unproven engine gets to run at all".
    `quiet_on_success` reads that straight off `may_alert`, so promoting GUST
    out of research tier starts it reporting again in the same commit, with no
    second list to remember. A FAILURE still alerts.
  - **09:30 IST, Saturday** — the weekly engines (VECTOR, ASCENT, BREACH,
    TIDAL). This one does post its summary; it files entries.
  - **16:00 and 21:00 IST, weekdays** — `cf_scan`, the FX/commodity 1H channel
    engine, **filing silently**. It alerts nothing: `cf_1h` is in
    `ALERTS_SUPPRESSED` and `run_cf_scan` asks `may_alert()` before it posts.
    **It never had a cron before.** `cf_scan` was the `*)` FALLTHROUGH arm of
    `scheduled_tasks.yml`'s dispatcher — any unrecognised cron string became a
    CF scan — so its 367 signals between 03 Jun and 21 Aug were produced by
    accident, and when the cron strings were corrected the engine stopped dead
    with nothing reporting it. The arms are explicit now, the fallthrough stays
    `TASK=none`, and a test asserts every cron resolves to a named task.
    Its record is the best t-statistic in the ledger (t = +3.85) and cannot
    carry a promotion: 227 COMEX + 140 FX and zero NSE, 186 of 367 SELL against
    a long-only book, all of it pre-launch. It rebuilds a forward sample first.
    Weekdays only — COMEX is shut Saturday, and this repo has already published
    a natural-gas entry on one off Thursday's close.
  - The 22:00 and 00:00 MYT crons are RETRIES. They stand down when the slot
    already completed, so they normally post nothing.
  - `health_watch.py` runs three times a day and posts **only on failure** —
    no `--always` in the cron. Silent on success, loud on failure, which is
    the shape the midday slot now matches.
- The **Cloudflare watchdog** (`src/watchdog.js` in the *signal* repo) holds its
  own copy of that schedule. Moving a cron here without moving it there does not
  remove a slot — it moves it into the watchdog, which then dispatches it daily
  with no cron anywhere to explain why.
- `engine_names.py` — the PUBLISHED name of an engine (`breakout` → BREACH),
  mirroring `REGISTRY` in the signal site's **`public/engines.js`**. The
  registry moved there from `signal.js` on 2026-09-19 so the two browser
  bundles stop keeping separate copies; `test_engine_names.py` (56 checks)
  asserts the two are equal in both directions and now runs in CI, which it
  did not while it looked for that file at a path on one laptop.
  Alerts print names, never database keys.
  `published_tally()` states the arithmetic and currently returns **8 names
  over 8 keys**: with `magic` retired, `magicmagic` carries TIDAL alone and the
  "N names over M configurations" clause correctly disappears. It is pinned to
  the CONDITION — the clause must appear exactly when keys and names disagree —
  so it cannot come back for the wrong reason. This is the number that once
  said 8 on one page and 9 on another.
- `engine_label()` is what an alert PRINTS, and it is not `engine_name()`.
  Mirroring `ENGINE_BOOK.label()`, it appends the band where a name is SHARED:
  `magic` and `magicmagic` both publish TIDAL, and a Telegram alert has no card
  underneath to carry the band, so both arrived on the phone reading exactly
  `TIDAL` — one retired with twenty positions open, one live. The rule is
  *shared*, not *has a band*: LEDGE and KEEL carry one and print neither.
  `engine_name()` is deliberately left returning the bare name, because it is
  the half held equal to the browser in both directions.
- **Every key the code can WRITE must be named, and so must every key a feed is
  SLICED on.** Each roster check used to run one way — from a key somebody had
  already remembered to name — so `swing`, `manual` and `4h_momentum` were on no
  list in either language and would have printed their own database key back at
  a reader, uppercased. `cf_momentum` was routing a feed slice with no writer
  anywhere. Both directions are now read out of the SOURCE, not a hand-kept
  inventory, with a floor on the match count so a pattern that stops matching
  fails rather than passing everything.
- Position alerts carry the engine, when the signal was filed, how long it has
  been held, and **what fired it**. `why_lines()` reads the row's own metadata
  (LEDGE/KEEL write measured reasons at signal time); `engine_rule()` is the
  fallback and is labelled differently — a standing rule is not a claim about
  one trade. They are separate functions so the two can never be blurred.
- A **time stop** now alerts. It closes a live position and used to do it in
  silence, reported only as a count with no symbol in it.
- `daily_brief._OWNS` decides which site a section links to. Facts all come from
  one API; links do not — ideas, the ledger and the engines are on
  signal.askakshay.com, and linking them to the newspaper is how the brief
  stopped being a way into anything.

The book is fetched in BATCHES — one request per bar interval, not one per
open position. `group_by="column"`, never `"ticker"`: `_own_frame` resolves the
symbol at column level -1, which is where the default layout puts it. Under
`"ticker"` it finds `Open`/`High`/`Low`/`Close` there instead, returns None for
every symbol, and the batch degrades to one request each — still correct, never
faster, and nothing says so. A test asserts the batch is actually used.

Rules the layer must keep (all pinned by `test_alert_pipeline.py`, 175 checks,
and `test_brief_fit.py`):
- Every engine in `tracker.REMARKS` has a published name, or a reader gets a key.
- An alert with a missing field drops the field, never the message. The whole
  loop body sits inside `except Exception: continue`, so a NameError in one
  composer deletes that alert into a log nobody reads.
- No baseline, no P&L. APEX printed `balance - 2000.0` against a number that was
  typed, not measured.
- A stop-out after T1 was booked is a different sentence from one that never
  worked. So is a T2 that half the position missed.
- Grading reads `scanner._own_frame`, never `df["Close"].squeeze()`. Squeeze
  returns a SCALAR on a one-row frame, so `.iloc[-1]` raised inside the loop's
  own `except: continue` and that position was never graded at all; and it does
  not drop a partial last bar, whose NaN Close was booked into the ledger as an
  EXPIRED trade's exit price, P&L and R — all three NULL.
- An unknown horizon is `None` and every consumer must expect it.
  `tracker.update_all_outcomes` divided it by 24 and raised, which the outer
  handler swallowed — the row was then skipped ENTIRELY, no excursions, no
  resolution. `momentum_quant` now carries the 30-day horizon its own alert
  footer advertises; `sip_bucket` and `top5_pick` are ALLOCATIONS with no
  horizon by design and are named in `NO_TIME_STOP_BY_DESIGN` so the log stops
  warning about correct behaviour beside a real gap.
- `_record_delivery` takes a bool as "this outcome applies to every id". Two
  call sites passed a bare `True` from `... if blocks else True`, `list(True)`
  raised, and `_safe` logged it as "'bool' object is not iterable" while
  skipping the rest of the scan. A batch that was never SENT records the reason
  — "long only, every signal was a short" — not a Telegram failure that did not
  happen.
- **TATAMOTORS demerged; it is not a rename.** It became `TMCV` (Tata Motors
  Ltd.) and `TMPV` (Tata Motors Passenger Vehicles Ltd.), both verified in the
  750-name screen. Mapping the old ticker to either alone prices half a company
  as the whole one. It had been 404ing on every scan.
- `stats.js` totals must account for every row. `closed` is win|loss, so
  time-stopped trades were in none of the four reported numbers and the
  remainder was unexplained. They are OUT of expectancy on purpose — a time
  stop's R is marked at the last close, not realised at an exit — and the
  `basis` string now says that instead of claiming to cover "closed signals".
  `including_time_stops` reports the same arithmetic WITH them, because
  `standalone_scan` books that R specifically to avoid survivorship bias and
  dropping it here reinstates the bias. Both readings are published; neither is
  chosen silently. No win rate there — a trade that exited on the clock neither
  won nor lost.
- **A breached stop VOIDS the setup, and the CALL must say so.** The live
  overlay re-read the facts (price, off-high, stop) and left the verdict and
  score stamped at the build, so IFCI read "Buy · 89 Strong" over its own live
  "-13.1% off its high" and a breached stop, with the verdict's stated reason
  being "Broke its 52-week high". The score is NOT recomputed in the browser —
  inventing a fresh number there is the fault this site avoids elsewhere; it
  keeps its value and says "at build".

## Research floor (`#research` — BUOY · ANCHOR · BEDROCK)
Measured, published with their null results, and **never cleared to file
signals**. `research.yml` runs twice a day at the 4-hour closes.

- **It runs now.** Run 34369438172, 2026-09-09: harvest 87 seconds for 500 of
  500 hourly and 498 of 500 daily bars, scan 3 seconds. Before that it had
  never once completed.
- **It had never once completed.** Its install step was `pip install numpy`
  while `signals/indicators.py` imports `ta` at module load, so every run died
  before any scan code. Underneath that was a worse fault: `barsH.json` and
  `barsD3y.json` had **five readers and no writer** — the path was a scratch
  folder on one Mac, under `/private/tmp`, which macOS clears.
- `harvest_bars.py` is the producer. `bars_cache.py` is the one place that
  knows where the cache lives (`data/bars/`, gitignored, override with
  `RESEARCH_BARS_DIR`). The workflow harvests, then scans.
- **The harvest fetches through yfinance, never a raw GET.** The first version
  copied `scan_buoy.py`'s urllib call and returned nothing for **500 of 500**
  symbols over 35 minutes from a runner — while ninety minutes earlier the same
  infrastructure pulled 113 tickers through yfinance in ~4 seconds. Yahoo was
  not blocking the runner; it refuses a request without the cookie-and-crumb
  handshake yfinance performs. That is very likely why the bars were being
  harvested by hand on a Mac in the first place.
- Batched, `group_by="column"`, resolved with `scanner._own_frame` — the same
  trap and the same fix as the position-grading batch. A failed chunk logs its
  exception; the first version swallowed everything, so a total refusal looked
  exactly like a universe of illiquid names.
- `research.yml` sets the three `config.py` placeholders (importing `scanner`
  raises without them) and carries a `concurrency` group: two runs a minute
  apart once harvested the same 500 symbols at once.
- A partial harvest is not a failure — skipped symbols are counted in
  `manifest.json` and the feed publishes coverage, so a narrow run is never
  mistaken for the full universe. An **empty** harvest refuses to overwrite a
  good cache.
- **A scan never asserts its own coverage.** Both scans shipped a hardcoded
  `coverage_note` saying the run had been throttled to a partial universe. True
  of the laptop it was written on; the run of 2026-09-09 harvested **500 of
  500** in 87 seconds and published that sentence anyway.
  `bars_cache.coverage_note()` reads the manifest `harvest_bars.py` writes. No
  manifest, no number — it names the script instead of inventing a denominator,
  the same rule `data_health.py` holds.
- **The feed has to be MIRRORED to be published.** `signal.js` fetches
  `/research.json` and `/alerts_log.json`; the signal repo's `sync-data.yml`
  listed neither, so `#research` served a hand-committed 2026-09-08 snapshot
  while this repo rewrote both twice a day. Nothing 404'd — a present-but-frozen
  feed renders normally and every number on it is a fact about last week. The
  sync list is now pinned by `test/guard.mjs` over there, which until then **ran
  in no workflow at all** and is now a step in `deploy.yml`.
- `docs/buoy.json` is written by `scan_buoy.py`, which `research.yml` does not
  run, and `/buoy` on the site redirects to `/research`. The workflow no longer
  stages it.
- `python3 test_bars_cache.py` — 20 checks, offline. It pins the SHAPE of the
  bug, not just the instance: no source may hardcode a path into a home or
  scratch directory, bar files are addressed only through `bars_cache`, the
  workflow must harvest before it scans, and no scan may phrase its own
  coverage.

## Vision signals — RETIRED 2026-10-01 (`vision_scan.py`, grading only)
Bottom reversal and 4H breakout **no longer file**. `vision_scan.LEGACY`
(`vision-legacy-2.0.0`) switches both off; it is a versioned switch, not a
deletion, and setting `new_filings` back to True in a NEW version is the
rollback. The rules stay in `scanner.py` (VISION SIGNALS) because the open
filings are still graded by them.

- `vision_scan.yml` runs once, 10:15 UTC (15:45 IST), and fetches ONLY names
  with an open legacy filing. It grades them under the rules they were filed
  with — `vision_grade`, same bars, same horizon — until stop, T3 or horizon.
  The 07:58 UTC slot is gone here and from the signal repo's watchdog.
- `feeds/vision_signals.json` keeps the record: every filing, loss and
  timestamp, `today` always empty, a `retired` block added. **It is not shown
  or linked anywhere on either site** (owner decision 2026-10-01): the signal
  repo no longer mirrors it, and its guard fails if anything fetches it.
- Why retired (full audit in the private engine repo, `docs/AUDIT.md`):
  - 29 of 45 stops sat at the 6% cap, i.e. clamped INSIDE structure;
  - 44 of 45 targets were exactly 1.6/2.5/3.3R — risk multiples, not levels;
  - entries were the signal close, but every filing was published 3–6.6 h
    after the session ended, a price no reader could pay;
  - "4H" candles were 4h00 + 2h15, and since every run landed after the
    close, the morning candle was never evaluated;
  - `auto_adjust=True` grades dividend-adjusted bars against unadjusted
    levels.
- `python3 test_vision_signals.py` — 45 checks, offline, including the
  retirement: a matching name files nothing, open filings keep grading,
  losses are kept, only open names are fetched.

## Vision EOD — Risk-First Selection (replacement; PRIVATE repo)
The replacement engine lives in **`caakshayk1-boop/vision-engine` (private)**:
rules, thresholds, research and owner diagnostics never enter this public
repo. Its only output here is **`feeds/vision_eod.json`**, written by its
`eod.yml` through the GitHub contents API (`PUBLIC_FEED_TOKEN`), from an
allowlist (`vision_eod/publish.py`) with a leakage test. Do not add strategy
logic for it to this repo — that would publish it.

- Long-only NSE cash equities, completed daily bars, one setup family
  (controlled pullback in an uptrend + recovery close), risk-first plans:
  structural stop (never capped), three targets from existing levels,
  entry RANGE with a cap for the NEXT session, 40/35/25 partial exits,
  expiry, time exit. Fills are SIMULATED and labelled as such.
- Mode **research** until a holdout passes the pre-registered criteria in
  that repo's `docs/PROMOTION_CRITERIA.md`. **New plans are PAUSED**
  (`vision-eod-1.0.1`, 2026-10-01): the 1.0.0 setup showed no reliable edge
  in backtest (train −0.111R, validation +0.007R, −0.082R at 2× costs), and
  the next-open entry variant (H2) failed on train too. The nightly run still
  publishes status; a bottom-reversal replacement is being researched there.
- The signal repo mirrors `feeds/vision_eod.json` (sync-data.yml +
  pull-feeds.mjs) and Vision's Setups page renders it.

## Page structure
`SECTION_MAP` order IS document order, and the nav is generated from it.
`python3 test_page_structure.py` fails the build if the two drift, and requires
every nav group to be CONTIGUOUS — a group that stops and restarts prints its
heading twice and stops being navigation.

Main page runs, in order: **Read · Research · Trade · Trust**. Moving a section
means moving its template block AND its SECTION_MAP row; the test checks both.

## Hosting cost
Turso is the only paid line. Everything else must stay inside a free tier.

- **Turso: `db.py` connects DIRECTLY (remote, over HTTP), never through an
  embedded replica.** Until 2 Oct 2026 it opened a replica at
  `/tmp/signals_replica.db`, built for one long-lived Railway container. On
  GitHub Actions every run starts with an empty `/tmp`, so every run
  re-downloaded the database: **38.16 GB "bytes synced" in September, $10.15
  of a $16.14 bill**, while rows read (61.6M) and written (5,449) were nothing.
  Remote mode syncs nothing; the cost is ~0.7 s per query from a runner, fine
  for batch jobs of a few hundred statements. `TURSO_MODE=replica` is the
  rollback. At this usage the **Free plan** (500M reads, 10M writes, 5 GB
  storage, 3 GB syncs) covers everything. Never reintroduce a `sync_url`
  connection on an ephemeral runner. `test_db_remote.py` pins it offline;
  `turso_smoke.yml` proves it read-only against the real database on any push
  touching `db.py`.

- **news.askakshay.com** is on Vercel and hit **100% of the 10 GB free Function
  Storage**. Two causes, both now fixed in config, neither of which reclaims
  what is already stored:
  - `api/_db.js` imports `@libsql/client/web`, NOT `@libsql/client`. The default
    entrypoint statically imports the native `libsql` package — 18.8 MB of
    compiled binary — and **sixteen of the eighteen routes** reach `_db.js`.
    Vercel bundles per function and keeps every deployment's output.
  - `vercel.json`'s `ignoreCommand` skips the build unless the commit touched
    `docs/` or `vercel-news/`. **`[skip ci]` does not stop Vercel here** — a
    `data: update signals ... [skip ci]` commit deploys like any other. Measured
    over 14 days: **34 of 55 commits (62%)** touch neither path, ~17 deployments
    a day of which ~3 matter.
  - That gate's exit code is **inverted — 0 skips, non-zero builds** — so every
    unexpected condition must fail toward BUILDING. A gate that exits 0 by
    accident stops the site deploying with a green workflow and no error
    anywhere. `test_vercel_ignore.py` runs the real command string against a
    temp repo: no repo, no `HEAD^`, a renamed `docs/`. The `test -d` guards are
    there because `git diff` with a pathspec matching nothing exits 0.
  - The two commit types that still deploy both must: `chore: jobs` writes
    `docs/jobs.json`, served STATICALLY (allow-listed in `.vercelignore`, copied
    by `build.js`, fetched as `jobs.json`, not via `/api`), and
    `chore: newspaper` rebuilds the shell.
  - `vercel-news/test/bundle.test.js` pins the `/web` import; it runs in
    `newspaper.yml` and in `tests.yml`.
- **Reclaiming the 10 GB needs the account**: delete old deployments in the
  Vercel dashboard (Project → Deployments), or `vercel remove <project> --safe`.
  Nothing in this repo can do it.
- **signal.askakshay.com** is on Cloudflare Workers and costs nothing at this
  traffic. It is the pattern to migrate toward if the newspaper's bill returns.

## Tests
`.github/workflows/tests.yml` runs every offline suite on **every push and
pull request**. Before it existed, each suite ran only inside the job it
guards — `test_alert_pipeline.py` in `daily_scan.yml`, `node --test` in
`newspaper.yml` — so a regression merged green and surfaced at 20:00 MYT as a
scan that refused to run. The repo is public, so Actions minutes are free.

- `test_crawler.py` and `test_security.py` are **excluded by name**: both hit
  the live network (chittorgarh.com, r.jina.ai, a real browser, the served
  headers). They belong on a schedule against production, not on a diff.
- The three env vars in that workflow are placeholders, never real secrets —
  `config.py` raises at import time on a missing variable, so importing
  `scanner` needs them set to something. Nothing in the offline suites sends,
  fetches or authenticates.
- Running a suite locally needs `ta` installed. Without it every ATR is None
  and `test_stock_screen.py` fails for the missing library, not the code.
- Two classes of stale test this CI already caught, both of which had been red
  for a while with nobody looking:
  - **A date written down.** A fixture filed a row at "2026-08-12" against a
    20-day horizon. It was inside that horizon the day it was written and aged
    out a fortnight later. Fixture dates are relative to today, never literal.
  - **A fixture that stopped matching its comment.** "Orderly range" was an 11%
    daily ATR, built before the stop widened on 2026-09-03. When the rule it
    tested started refusing that candidate — correctly — the test blamed the
    code.

## Claude Code mod (`mods/askakshay/`)
A plugin with a mod that turns the Rules below from words into refused tool
calls: Read/Edit/Write/Grep/Bash on `config.py`, Edit/Write in `data/`, Edit/Write
of a `docs/` file whose source is in `static/` (generate.py overwrites it), and
`git push` to main. It also adds `/paper` (the paper setups from
`feeds/signal_v2.json`, in plain words) and puts one line under the prompt only
when that feed is over four days old. It does nothing outside this repo.
- Install: `/plugin marketplace add caakshayk1-boop/trading-dashboard`, then
  `/plugin install askakshay@askakshay`. Develop with `claude --plugin-dir ./mods/askakshay`.
- `claude plugin validate ./mods/askakshay` · `cd mods/askakshay && claude plugin test` (8 tests).
- Shell-text matching is a reminder for Claude, not a security boundary.

## Rules
- NEVER read or modify `config.py` (contains API keys)
- All market data: fetch live, never hardcode prices
- Log everything to `logs/`
- Signal logic lives in `scanner.py` — don't scatter it

## Out of Scope
- `config.py` — secrets, don't touch
- `data/` — raw market data cache, don't modify manually
