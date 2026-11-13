"""Orchestration. Scheduled by launchd, and reachable from Telegram via /scan."""

from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
import pandas_market_calendars as mcal

from . import crew, indicators, market_data, notify, screener, store
from .config import Config, ConfigError, Secrets, load_config, load_seed_universe
from .market_data import SIP_DELAY, market_today


@dataclass
class RunResult:
    run_date: date
    sections: screener.Sections
    report: str
    scanned: int
    skipped: list[str]
    duration_s: float
    committed: bool = False   # flag_state advanced and run recorded


def last_session(on: date | None = None) -> date:
    """Most recent *completed* NYSE session on or before `on` (default: today).

    A session counts only once its bar is final: the close has passed and the
    SIP delay has elapsed. An in-progress session would give a partial bar —
    wrong close, volume a fraction of normal — and quietly corrupt every
    distance and score. This holds for explicit dates too.
    """
    on = on or market_today()
    sched = mcal.get_calendar("NYSE").schedule(start_date=on - timedelta(days=14), end_date=on)
    final = sched[sched["market_close"] + SIP_DELAY <= pd.Timestamp.now(tz="UTC")]
    if final.empty:
        raise ValueError(f"No completed NYSE session on or before {on}.")
    return final.index[-1].date()


def ensure_universe() -> list[str]:
    """Active universe, seeded from seed_universe.yaml the very first time."""
    if store.never_populated() and (seed := load_seed_universe()):
        store.add_tickers(seed, source="seed")
    return store.universe()


def scan(
    on: date | None = None,
    *,
    cfg: Config | None = None,
    secrets: Secrets | None = None,
    use_llm: bool = True,
) -> RunResult:
    """Fetch, compute, classify, and write the report. Writes nothing to the DB:
    committing state is run_daily's job, after the report is delivered."""
    cfg = cfg or load_config()
    secrets = secrets or Secrets()
    run_date = last_session(on)
    started = time.monotonic()

    symbols = ensure_universe()
    if not symbols:
        raise RuntimeError(
            "Universe is empty. Populate `universe:` in data/seed_universe.yaml "
            "or use /add in Telegram."
        )

    start = run_date - timedelta(days=cfg.lookback_days)
    bars = market_data.fetch_bars(symbols, start=start, end=run_date, secrets=secrets)

    snapshots: dict[str, indicators.Snapshot] = {}
    skipped: list[str] = []
    for symbol in symbols:
        df = bars.get(symbol)
        snap = indicators.compute(symbol, df, cfg) if df is not None else None
        # A missing bar for run_date (halt, late data) would otherwise be read
        # as "still in band" and silently turn a NEW into a REPEAT.
        if snap is None or snap.as_of != run_date:
            skipped.append(symbol)
        else:
            snapshots[symbol] = snap

    sections = screener.classify(snapshots, store.flag_states(), store.pins(), cfg, run_date)
    report = (
        crew.write_report(sections, run_date, cfg, secrets)
        if use_llm
        else crew.plain_report(sections, run_date)
    )
    return RunResult(
        run_date, sections, report, len(snapshots), skipped,
        duration_s=round(time.monotonic() - started, 2),
    )


def run_daily(
    on: date | None = None,
    *,
    advance_state: bool = True,
    send: bool = True,
    use_llm: bool = True,
) -> RunResult:
    """Scan, deliver, then commit. State only advances once the report is out,
    so a failed send doesn't consume the day's NEW signals.

    State never advances for a session at or before the last successful run:
    re-running it would turn every NEW into REPEAT, and replaying an old date
    would overwrite current state with stale state.
    """
    run_date = last_session(on)
    if advance_state:
        last_ok = store.last_run(only_ok=True)
        if last_ok and last_ok["run_date"] >= run_date.isoformat():
            advance_state = False

    secrets = Secrets()
    try:
        cfg = load_config()  # inside the try so a bad config shows up in /status
        result = scan(run_date, cfg=cfg, secrets=secrets, use_llm=use_llm)
        if send:
            asyncio.run(notify.send_report(result.report, result.sections, secrets))
    except Exception as exc:  # noqa: BLE001 - log the failure so /status can see it
        if advance_state:
            store.record_run(run_date, status="error", error=str(exc))
        raise

    if advance_state:
        store.set_flag_states(result.sections.state_updates)
        store.record_run(
            run_date,
            status="ok",
            ticker_count=result.scanned,
            new_count=result.sections.new_count,
            repeat_count=result.sections.repeat_count,
            duration_s=result.duration_s,
        )
        result.committed = True
    return result


def status_text() -> str:
    last = store.last_run()
    if not last:
        return "No runs recorded yet."

    expected = last_session()
    lines = [
        f"Last run: {last['run_date']} ({last['status']})",
        f"Scanned {last['ticker_count']} · {last['new_count']} new · "
        f"{last['repeat_count']} held back · {last['duration_s']}s",
    ]
    if last["error"]:
        lines.append(f"Error: {last['error']}")

    ok = store.last_run(only_ok=True)
    if not ok or ok["run_date"] < expected.isoformat():
        lines.append("")
        lines.append(
            f"⚠️ No successful run for {expected.isoformat()} — the scheduled job "
            "may not be running (Mac asleep, bad key, or network)."
        )
    return "\n".join(lines)


def cli() -> None:
    p = argparse.ArgumentParser(prog="investimo", description="EOD moving-average scan")
    p.add_argument("--date", help="scan the last session on or before YYYY-MM-DD")
    p.add_argument("--dry-run", action="store_true", help="print, do not send")
    p.add_argument("--no-llm", action="store_true", help="skip the writeup agents")
    p.add_argument("--no-state", action="store_true", help="do not advance flag_state")
    p.add_argument("--check-data", action="store_true", help="verify Alpaca split adjustment")
    p.add_argument("--status", action="store_true", help="print last-run health")
    args = p.parse_args()

    if args.check_data:
        ok, msg = market_data.check_split_adjustment()
        print(msg)
        raise SystemExit(0 if ok else 1)

    if args.status:
        print(status_text())
        return

    on = date.fromisoformat(args.date) if args.date else None
    try:
        result = run_daily(
            on,
            advance_state=not (args.no_state or args.dry_run),
            send=not args.dry_run,
            use_llm=not args.no_llm,
        )
    except ConfigError as exc:
        raise SystemExit(str(exc)) from None

    print(result.report)
    for text, _ in notify.section_messages(result.sections):
        print(f"\n{text}")
    if not result.committed and not (args.no_state or args.dry_run):
        print(f"\n(state not advanced: {result.run_date} was already processed)")
    if result.skipped:
        print(f"\nSkipped (no/short data): {', '.join(result.skipped)}")


if __name__ == "__main__":
    cli()
