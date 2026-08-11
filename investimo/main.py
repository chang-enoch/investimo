"""Orchestration. Scheduled by launchd, and reachable from Telegram via /scan."""

from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass
from datetime import date, timedelta

from . import crew, indicators, market_data, notify, screener, store
from .config import Config, Secrets, load_config, load_seed_universe


@dataclass
class RunResult:
    run_date: date
    sections: screener.Sections
    report: str
    scanned: int
    skipped: list[str]


def last_session(on: date | None = None) -> date:
    """Most recent *completed* NYSE session on or before `on`.

    Today only counts once the bell has actually rung. Using an in-progress
    session would take a partial daily bar — wrong close, and volume that reads
    as a fraction of normal — which quietly corrupts every distance and score.
    """
    import pandas as pd
    import pandas_market_calendars as mcal

    explicit = on is not None
    on = on or date.today()
    cal = mcal.get_calendar("NYSE")
    sched = cal.schedule(start_date=on - timedelta(days=14), end_date=on)
    if sched.empty:
        return on

    if not explicit and len(sched):
        now = pd.Timestamp.now(tz="UTC")
        while len(sched) and now < sched["market_close"].iloc[-1]:
            sched = sched.iloc[:-1]
        if sched.empty:
            return on - timedelta(days=1)

    return sched.index[-1].date()


def ensure_universe() -> list[str]:
    """Load the DB universe, seeding from seed_universe.yaml on first run."""
    store.init()
    symbols = store.universe()
    if not symbols:
        seeded = load_seed_universe()
        if seeded:
            store.seed_universe(seeded)
            symbols = store.universe()
    return symbols


def scan(
    run_date: date | None = None,
    *,
    advance_state: bool = True,
    cfg: Config | None = None,
    secrets: Secrets | None = None,
    use_llm: bool = True,
) -> RunResult:
    """Fetch, compute, classify, and write the report.

    advance_state=False leaves flag_state untouched, so an ad-hoc /scan cannot
    consume a ticker's NEW status before the scheduled report shows it.
    """
    cfg = cfg or load_config()
    secrets = secrets or Secrets()
    run_date = last_session(run_date)
    started = time.monotonic()

    symbols = ensure_universe()
    if not symbols:
        raise RuntimeError(
            "Universe is empty. Populate `universe:` in data/seed_universe.yaml "
            "or use /add in Telegram."
        )

    start = run_date - timedelta(days=cfg.lookback_days + 120)
    bars = market_data.fetch_bars(symbols, start=start, end=run_date, secrets=secrets)

    snapshots: dict[str, indicators.Snapshot] = {}
    skipped: list[str] = []
    for symbol in symbols:
        df = bars.get(symbol)
        if df is None or df.empty:
            skipped.append(symbol)
            continue
        snap = indicators.compute(symbol, df, cfg)
        if snap is None:
            skipped.append(symbol)
        else:
            snapshots[symbol] = snap

    pinned = store.pins()
    sections = screener.classify(snapshots, store.flag_states(), pinned, cfg, run_date)

    if advance_state:
        for (symbol, ma), (state, first) in sections.state_updates.items():
            store.set_flag_state(symbol, ma, state, first)

    report = (
        crew.write_report(sections, run_date, cfg, secrets)
        if use_llm
        else crew.plain_report(sections, run_date)
    )

    if advance_state:
        store.record_run(
            run_date,
            status="ok",
            ticker_count=len(snapshots),
            new_count=len(sections.new),
            repeat_count=len(sections.repeat),
            duration_s=round(time.monotonic() - started, 2),
        )

    return RunResult(run_date, sections, report, len(snapshots), skipped)


def run_daily(
    run_date: date | None = None,
    *,
    advance_state: bool = True,
    send: bool = True,
    use_llm: bool = True,
) -> RunResult:
    cfg, secrets = load_config(), Secrets()
    try:
        result = scan(
            run_date,
            advance_state=advance_state,
            cfg=cfg,
            secrets=secrets,
            use_llm=use_llm,
        )
    except Exception as exc:  # noqa: BLE001 - log the failure so /status can see it
        if advance_state:
            store.init()
            store.record_run(last_session(run_date), status="error", error=str(exc))
        raise

    if send:
        asyncio.run(
            notify.send_report(
                result.report, result.sections, store.pins(), result.run_date, secrets
            )
        )
    return result


def status_text() -> str:
    store.init()
    last = store.last_run()
    if not last:
        return "No runs recorded yet."

    expected = last_session()
    lines = [
        f"Last run: {last['run_date']} ({last['status']})",
        f"Scanned {last['ticker_count']} · {last['new_count']} new · "
        f"{last['repeat_count']} repeat · {last['duration_s']}s",
    ]
    if last["error"]:
        lines.append(f"Error: {last['error']}")

    ok = store.last_successful_run()
    if not ok or ok["run_date"] < expected.isoformat():
        lines.append("")
        lines.append(
            f"⚠️ No successful run for {expected.isoformat()} — the scheduled job "
            "may not be running (Mac asleep, bad key, or network)."
        )
    return "\n".join(lines)


def cli() -> None:
    p = argparse.ArgumentParser(prog="investimo", description="EOD moving-average scan")
    p.add_argument("--date", help="replay a past close, YYYY-MM-DD")
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
    result = run_daily(
        on,
        advance_state=not (args.no_state or args.dry_run),
        send=not args.dry_run,
        use_llm=not args.no_llm,
    )

    print(result.report)
    if result.skipped:
        print(f"\nSkipped (no/short data): {', '.join(result.skipped)}")


if __name__ == "__main__":
    cli()
