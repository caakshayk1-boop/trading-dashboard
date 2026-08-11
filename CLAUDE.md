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

## Rules
- NEVER read or modify `config.py` (contains API keys)
- All market data: fetch live, never hardcode prices
- Log everything to `logs/`
- Signal logic lives in `scanner.py` — don't scatter it

## Out of Scope
- `config.py` — secrets, don't touch
- `data/` — raw market data cache, don't modify manually
