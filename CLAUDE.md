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

## Page structure
`SECTION_MAP` order IS document order, and the nav is generated from it.
`python3 test_page_structure.py` fails the build if the two drift, and requires
every nav group to be CONTIGUOUS — a group that stops and restarts prints its
heading twice and stops being navigation.

Main page runs, in order: **Read · Research · Trade · Trust**. Moving a section
means moving its template block AND its SECTION_MAP row; the test checks both.

## Rules
- NEVER read or modify `config.py` (contains API keys)
- All market data: fetch live, never hardcode prices
- Log everything to `logs/`
- Signal logic lives in `scanner.py` — don't scatter it

## Out of Scope
- `config.py` — secrets, don't touch
- `data/` — raw market data cache, don't modify manually
