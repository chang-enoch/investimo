# Investimo

Daily EOD scanner. At every market close it checks a watchlist for names
approaching their 21 EMA, 50 EMA, or 200 SMA, and sends a sectioned report to Telegram.
Inline buttons on each ticker pin it or pull its GEX chart.

## Two processes

- **`main.py run_daily()`** — launchd, 16:30 ET weekdays. Scan → classify → report.
- **`bot.py`** — always-on polling service. Button callbacks and slash commands.

```
run_daily: calendar guard → load universe/pins/state → Alpaca (1 batched call)
           → indicators → classify + score → crew → Telegram
bot:       📌 Pin/Unpin · 📊 GEX · /gex /add /remove /list /pins
```

## Rules that must not be broken

- **MA math is never an LLM.** Indicators, distances, and classification are pure
  deterministic functions. Agents only write prose about numbers already computed.
- **Alpaca bars must be fetched via REST + `httpx`, not `alpaca-py`.** The SDK is
  [reported](https://github.com/alpacahq/alpaca-py/issues/207) to drop the `adjustment`
  param. We need `adjustment=split` — split-adjusted but NOT dividend-adjusted — or the
  200 SMA won't match IBKR charts. Sanity check: NVDA pre-2024-06-07 closes ≈ $120.
- **No test files in this repo.** Verification is a manual pass; keep it lightweight.
- **GEX never runs during the scheduled scan** — only on button tap or `/gex`.
- **GEX is a screenshot, not an interpretation.** No vision model reads the chart; the
  bot sends the image and you read it. Do not add a model here — a misread call wall is
  worse than no summary, because it would get acted on.
- **The LLM layer is optional and off by default.** With no `ANTHROPIC_API_KEY`,
  `crew.plain_report()` renders the same numbers without prose. That is the normal
  operating mode, not a degraded one.

## Report sections

Everything in range is reported, split into three sections. Per `(symbol, ma_type)`:

```
ARMED  ─ enters band (|close − MA| ≤ k·ATR) ─▶ NEW     → §2 New
NEW    ─ still in band next run ─────────────▶ REPEAT  → §3 Repeat
REPEAT ─ still in band ──────────────────────▶ REPEAT  → §3 Repeat
any    ─ leaves band by k · rearm_margin ────▶ ARMED
```

§1 Pinned (always, never duplicated below) · §2 New (the section to read) · §3 Repeat
(one compact line each). `k=1.0`, `rearm_margin=1.5` in `config.yaml`. Each MA tracks
independently.

## Layout

```
investimo/
  config.py       pydantic-settings
  store.py        SQLite (tickers, pins, flag_state, runs, gex_cache)
  market_data.py  Alpaca REST via httpx
  indicators.py   EMA / SMA / ATR / slope — pure functions
  screener.py     classification + scoring
  crew.py         CrewAI Analyst → Editor; LangChain @tool wrappers
  notify.py       Telegram formatting + send
  gex.py          Playwright screenshot + GEX Reader (vision)
  bot.py          polling service
  main.py         run_daily()
```

Model: `claude-opus-5`. Adaptive thinking is on by default — keep `max_tokens` at 16000
or responses truncate.

## Commands

```sh
.venv/bin/python -m investimo.main                    # run the scan and send it
.venv/bin/python -m investimo.main --dry-run          # print, send nothing
.venv/bin/python -m investimo.main --date 2026-08-08  # replay a past close
.venv/bin/python -m investimo.main --no-llm           # deterministic report only
.venv/bin/python -m investimo.main --check-data       # verify Alpaca split adjustment
.venv/bin/python -m investimo.main --status           # last-run health
.venv/bin/python -m investimo.bot                     # bot in the foreground
```

Telegram: `/scan [date]` `/status` `/list` `/add` `/remove` `/pins` `/pin` `/unpin`
`/gex TICKER` `/config` `/help`. Buttons on every flagged ticker: 📌 Pin / 📍 Unpin, 📊 GEX.

`/scan` always runs with `advance_state=False` so an ad-hoc scan can't consume a
ticker's NEW status before the scheduled report shows it.

`.env`: `ALPACA_KEY` `ALPACA_SECRET` `ANTHROPIC_API_KEY` `TELEGRAM_TOKEN`
`TELEGRAM_CHAT_ID`. Without `ANTHROPIC_API_KEY` the scan still runs and reports —
`crew.plain_report()` renders the same numbers without prose.

## Environment gotchas found during the first build

- **`data.alpaca.markets` fails to resolve on some DNS servers** (this machine's ISM
  resolver and 8.8.8.8 both return NXDOMAIN; 1.1.1.1 resolves it). If bars fail with a
  connect error, set DNS to 1.1.1.1. `market_data.fetch_bars` raises a `MarketDataError`
  spelling this out.
- **Never scan an in-progress session.** `last_session()` drops today until the NYSE
  close has passed — a partial daily bar has the wrong close and volume reading ~0.2x
  normal, which silently corrupts every distance and score.

## Watchlist

Seeded once from IBKR into `data/seed_universe.yaml` (IBKR watchlists live only in the
Client Portal Web API, whose session needs near-daily 2FA — so we never run a gateway).
After seeding, manage the universe entirely through Telegram.

`data/seed_universe.yaml` is **gitignored** — a watchlist is personal and this repo is
public. `data/seed_universe.example.yaml` is the tracked template; copy it to start.
A missing file is not an error: `load_seed_universe()` returns `[]` and the DB universe
(already seeded, or built via `/add`) is the source of truth.

Long-form design notes: `~/.claude/plans/i-do-swing-trading-mossy-catmull.md`
