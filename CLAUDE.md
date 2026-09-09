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

## Stock Screen (`#stocks` on news.askakshay.com)
Nifty 500 research screen sitting directly above the Signal Log.

- `stock_screen.py` — the engine. Four SEPARATE scores (quality / growth /
  valuation / technical) plus a declared weighted composite. Weights live in
  `WEIGHTS`, nowhere else.
- `fundamentals.py` — `statements()` gives 4 fiscal years from yfinance and is
  where **real ROCE** comes from (EBIT / invested capital). Yahoo publishes no
  ROCE field; it is computed. 30-day cache, separate from the 7-day `.info` one.
- `.github/workflows/stock_screen.yml` — weekly, Sunday 02:30 IST, own clock.
  **Never build this inside the 6 AM job** — ~11 min of sequential fetches.
- Payload → Turso `newspaper_screen` (NOT `newspaper_stocks_picked`, which is
  the daily picks). `generate.py` reads it and writes `docs/screen.json`.
- The UI lives in **`static/app.js`** — `docs/app.js` is a build artefact that
  generate.py overwrites. Editing the artefact silently loses the work.
- `docs/screen.json` needs allow-listing in THREE places: generate.py writes it,
  `.vercelignore` names it, `vercel-news/build.js` copies it.
- `python3 test_stock_screen.py` — 133 checks, offline, no pytest.

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
- `python3 test_data_health.py` — 44 checks, offline, no pytest.

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
  the day's only scan. Crons live in `scheduled_tasks.yml` and `daily_scan.yml`;
  the 13:00 MYT "signals open" scan is **gone**, and no morning scan replaces it
  (08:00 MYT is 05:30 IST, before the NSE opens — it could only re-report the
  previous close).
- The **Cloudflare watchdog** (`src/watchdog.js` in the *signal* repo) holds its
  own copy of that schedule. Moving a cron here without moving it there does not
  remove a slot — it moves it into the watchdog, which then dispatches it daily
  with no cron anywhere to explain why.
- `engine_names.py` — the PUBLISHED name of an engine (`breakout` → BREACH),
  mirroring `ENGINE_REGISTRY` in the signal site's `signal.js`. Alerts print
  names, never database keys. `published_tally()` states the arithmetic —
  **8 names over 9 configurations**, because TIDAL runs two bands — which is the
  number that used to say 8 on one page and 9 on another.
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

Rules the layer must keep (all pinned by `test_alert_pipeline.py`, 119 checks,
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
- `stats.js` totals must account for every row. `closed` is win|loss, so
  time-stopped trades were in none of the four reported numbers and the
  remainder was unexplained. They are OUT of expectancy on purpose — a time
  stop's R is marked at the last close, not realised at an exit — and the
  `basis` string now says that instead of claiming to cover "closed signals".

## Page structure
`SECTION_MAP` order IS document order, and the nav is generated from it.
`python3 test_page_structure.py` fails the build if the two drift, and requires
every nav group to be CONTIGUOUS — a group that stops and restarts prints its
heading twice and stops being navigation.

Main page runs, in order: **Read · Research · Trade · Trust**. Moving a section
means moving its template block AND its SECTION_MAP row; the test checks both.

## Hosting cost
Turso is the only paid line. Everything else must stay inside a free tier.

- **news.askakshay.com** is on Vercel and hit **100% of the 10 GB free Function
  Storage**. Two causes, both now fixed in config, neither of which reclaims
  what is already stored:
  - `api/_db.js` imports `@libsql/client/web`, NOT `@libsql/client`. The default
    entrypoint statically imports the native `libsql` package — 18.8 MB of
    compiled binary — and **sixteen of the eighteen routes** reach `_db.js`.
    Vercel bundles per function and keeps every deployment's output.
  - `vercel.json`'s `ignoreCommand` skips the build unless the commit touched
    `docs/` or `vercel-news/`. About a third of pushes here are Python-only and
    were each producing a retained deployment that served nothing new.
  - `vercel-news/test/bundle.test.js` pins the first; it runs in `newspaper.yml`.
- **Reclaiming the 10 GB needs the account**: delete old deployments in the
  Vercel dashboard (Project → Deployments), or `vercel remove <project> --safe`.
  Nothing in this repo can do it.
- **signal.askakshay.com** is on Cloudflare Workers and costs nothing at this
  traffic. It is the pattern to migrate toward if the newspaper's bill returns.

## Rules
- NEVER read or modify `config.py` (contains API keys)
- All market data: fetch live, never hardcode prices
- Log everything to `logs/`
- Signal logic lives in `scanner.py` — don't scatter it

## Out of Scope
- `config.py` — secrets, don't touch
- `data/` — raw market data cache, don't modify manually
