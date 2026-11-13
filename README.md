# Investimo

An end-of-day moving-average scanner for swing trading. After every NYSE close it
checks a watchlist for names pulling into their moving averages (by default the
21 EMA, 50 EMA, and 200 SMA; any set of EMAs/SMAs is configurable) and sends a
ranked report to Telegram. Each report section carries a grid of ticker
buttons; tapping one lets you pin the name or pull its gamma-exposure chart.

```
scan:  last completed session → Alpaca daily bars (one batched call)
       → indicators → classify + score → report → Telegram
bot:   section message → [TICKER…] → 📌 Pin/Unpin · 📊 GEX
       /scan /status /list /add /remove /pins /pin /unpin /gex /config
```

## How it decides what to report

A name is **in band** when `|close − MA| ≤ k · ATR`. Only _first touches_ are
reported: a stock that sits on its 50 EMA for a week is one event, not five.
Each `(symbol, MA)` pair runs its own small state machine:

```
ARMED  ─ enters band ──────────────────▶ NEW     → reported
NEW    ─ still in band next session ───▶ REPEAT  → silent (counted only)
REPEAT ─ still in band ────────────────▶ REPEAT  → silent
any    ─ leaves band past k·rearm_margin ▶ ARMED → eligible again
```

With the defaults (`k = 0.5`, `rearm_margin = 1.5`) a name re-arms only once it
is more than 0.75 ATR away. Between 0.5 and 0.75 ATR is a hysteresis gap where the
prior state is held, so a stock chopping around the band edge doesn't re-fire
every day.

The report has two sections, each sorted by a weighted score (proximity, MA slope,
whether shorter MAs sit above longer ones, relative volume; weights are in
`config.yaml`):

- **📌 Pinned:** names you've pinned, shown whenever they're in band, even on a
  repeat day.
- **⭐ New:** first touches.

Repeat touches show up only as a count at the bottom of the report.

State only advances after the report is delivered, so a failed Telegram send
doesn't swallow the day's signals. It also never advances for a session that
has already been processed. Re-running today, or replaying an old date, is
always safe.

## Design choices

- **The math is deterministic.** Indicators, distances, classification, and
  scoring are plain pandas. The optional LLM layer (a CrewAI analyst → editor
  pair) only writes prose about numbers already computed, and any failure falls
  back to the plain report. With no `ANTHROPIC_API_KEY` the plain report is what
  you get, and that's the normal mode.
- **Bars are split-adjusted, not dividend-adjusted**, so the 200 SMA matches
  broker charts. Alpaca is called over raw REST because `alpaca-py`
  [has been reported](https://github.com/alpacahq/alpaca-py/issues/207) to drop
  the `adjustment` parameter. `--check-data` checks this against the NVDA 10:1
  split.
- **In-progress sessions are never scanned**, even when you name the date
  explicitly. A partial daily bar has the wrong close and a fraction of normal
  volume, which would quietly corrupt every distance and score. A symbol with no
  bar for the session (halted, late data) is skipped, not judged on stale data.
- **GEX is a screenshot, not an interpretation.** Playwright captures the chart
  from optioncharts.io on demand (never during the scheduled scan) and the bot
  sends you the image. No model reads it, because a misread call wall is worse
  than no summary. Images are cached per symbol per day.
- **The bot only answers its owner.** Every command except `/start` is limited
  to `TELEGRAM_CHAT_ID`.

## Setup

Requires Python 3.13+, a free [Alpaca](https://alpaca.markets) account, and a
Telegram bot token from [@BotFather](https://t.me/BotFather).

```sh
python3 -m venv .venv
.venv/bin/pip install -e .          # or -e '.[llm]' for the optional prose writeup
.venv/bin/playwright install chromium

cp data/seed_universe.example.yaml data/seed_universe.yaml   # edit your watchlist
```

`.env` in the repo root:

```
ALPACA_KEY=...
ALPACA_SECRET=...
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...       # send /start to the bot to learn it
ANTHROPIC_API_KEY=...      # optional, enables the prose writeup
```

The seed file is read once, on the very first run. After that, manage the
watchlist from Telegram with `/add` and `/remove`.

Everything else (the MA set, band width, re-arm margin, ATR/slope/volume
windows, scoring weights, GEX sources) lives in `config.yaml`. It is validated
at startup, so a typo fails loudly instead of halfway through a scan.

## Usage

```sh
.venv/bin/python -m investimo.main                    # scan and send
.venv/bin/python -m investimo.main --dry-run          # print only; no send, no state change
.venv/bin/python -m investimo.main --date 2026-08-07  # replay a past close (state untouched)
.venv/bin/python -m investimo.main --no-state         # send, but don't advance state
.venv/bin/python -m investimo.main --no-llm           # deterministic report only
.venv/bin/python -m investimo.main --check-data       # verify split adjustment
.venv/bin/python -m investimo.main --status           # last-run health
.venv/bin/python -m investimo.bot                     # run the Telegram bot
```

`/scan` from Telegram never advances state, so an ad-hoc scan can't use up a
ticker's NEW status before the scheduled report shows it.

To schedule the daily scan and keep the bot running on macOS, see
[`launchd/README.md`](launchd/README.md).

## Layout

```
investimo/
  config.py       .env secrets + config.yaml → frozen Config
  store.py        SQLite: tickers, pins, flag_state, runs
  market_data.py  Alpaca REST via httpx
  indicators.py   EMA / SMA / ATR / slope (pure functions)
  screener.py     band state machine + scoring
  crew.py         optional LLM writeup + deterministic fallback
  notify.py       Telegram formatting and delivery
  gex.py          Playwright chart capture
  bot.py          Telegram polling service
  main.py         scan orchestration + CLI
config.yaml       thresholds, MA set, scoring weights, LLM, GEX sources
launchd/          macOS scheduling
```
