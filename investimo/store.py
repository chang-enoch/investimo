"""SQLite persistence. Universe, pins, per-MA flag state, run log, GEX cache."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickers (
    symbol TEXT PRIMARY KEY,
    source_watchlist TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pins (
    symbol TEXT PRIMARY KEY,
    pinned_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS flag_state (
    symbol TEXT NOT NULL,
    ma_type TEXT NOT NULL,
    state TEXT NOT NULL,               -- ARMED | NEW | REPEAT
    first_entered_date TEXT,
    PRIMARY KEY (symbol, ma_type)
);
CREATE TABLE IF NOT EXISTS runs (
    run_date TEXT PRIMARY KEY,
    ran_at TEXT NOT NULL,
    status TEXT NOT NULL,              -- ok | error
    ticker_count INTEGER DEFAULT 0,
    new_count INTEGER DEFAULT 0,
    repeat_count INTEGER DEFAULT 0,
    duration_s REAL DEFAULT 0,
    error TEXT
);
CREATE TABLE IF NOT EXISTS gex_cache (
    symbol TEXT NOT NULL,
    run_date TEXT NOT NULL,
    image_path TEXT,
    summary TEXT,
    PRIMARY KEY (symbol, run_date)
);
"""


@contextmanager
def connect(path: Path | None = None):
    p = path or DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init(path: Path | None = None) -> None:
    with connect(path) as c:
        c.executescript(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- universe ----------

def universe(path: Path | None = None) -> list[str]:
    with connect(path) as c:
        rows = c.execute(
            "SELECT symbol FROM tickers WHERE active = 1 ORDER BY symbol"
        ).fetchall()
    return [r["symbol"] for r in rows]


def add_tickers(symbols: list[str], source: str = "manual", path: Path | None = None) -> list[str]:
    """Returns the symbols that were newly added (or reactivated)."""
    added = []
    with connect(path) as c:
        for s in (x.strip().upper() for x in symbols if x.strip()):
            cur = c.execute("SELECT active FROM tickers WHERE symbol = ?", (s,)).fetchone()
            if cur is None:
                c.execute(
                    "INSERT INTO tickers (symbol, source_watchlist, active, added_at)"
                    " VALUES (?, ?, 1, ?)",
                    (s, source, _now()),
                )
                added.append(s)
            elif not cur["active"]:
                c.execute("UPDATE tickers SET active = 1 WHERE symbol = ?", (s,))
                added.append(s)
    return added


def remove_tickers(symbols: list[str], path: Path | None = None) -> list[str]:
    removed = []
    with connect(path) as c:
        for s in (x.strip().upper() for x in symbols if x.strip()):
            cur = c.execute(
                "UPDATE tickers SET active = 0 WHERE symbol = ? AND active = 1", (s,)
            )
            if cur.rowcount:
                removed.append(s)
    return removed


def seed_universe(symbols: list[str], path: Path | None = None) -> int:
    """Idempotent first-run load from seed_universe.yaml."""
    return len(add_tickers(symbols, source="ibkr_seed", path=path))


# ---------- pins ----------

def pins(path: Path | None = None) -> set[str]:
    with connect(path) as c:
        return {r["symbol"] for r in c.execute("SELECT symbol FROM pins")}


def pin(symbol: str, path: Path | None = None) -> None:
    with connect(path) as c:
        c.execute(
            "INSERT OR IGNORE INTO pins (symbol, pinned_at) VALUES (?, ?)",
            (symbol.upper(), _now()),
        )


def unpin(symbol: str, path: Path | None = None) -> bool:
    with connect(path) as c:
        return c.execute("DELETE FROM pins WHERE symbol = ?", (symbol.upper(),)).rowcount > 0


# ---------- flag state ----------

def flag_states(path: Path | None = None) -> dict[tuple[str, str], dict]:
    with connect(path) as c:
        rows = c.execute("SELECT * FROM flag_state").fetchall()
    return {(r["symbol"], r["ma_type"]): dict(r) for r in rows}


def set_flag_state(
    symbol: str, ma_type: str, state: str, first_entered: str | None, path: Path | None = None
) -> None:
    with connect(path) as c:
        c.execute(
            "INSERT INTO flag_state (symbol, ma_type, state, first_entered_date)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(symbol, ma_type) DO UPDATE SET"
            " state = excluded.state, first_entered_date = excluded.first_entered_date",
            (symbol, ma_type, state, first_entered),
        )


def flag_history(symbol: str, path: Path | None = None) -> list[dict]:
    with connect(path) as c:
        rows = c.execute(
            "SELECT ma_type, state, first_entered_date FROM flag_state WHERE symbol = ?",
            (symbol.upper(),),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- runs ----------

def record_run(
    run_date: date,
    status: str,
    ticker_count: int = 0,
    new_count: int = 0,
    repeat_count: int = 0,
    duration_s: float = 0.0,
    error: str | None = None,
    path: Path | None = None,
) -> None:
    with connect(path) as c:
        c.execute(
            "INSERT INTO runs (run_date, ran_at, status, ticker_count, new_count,"
            " repeat_count, duration_s, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(run_date) DO UPDATE SET ran_at = excluded.ran_at,"
            " status = excluded.status, ticker_count = excluded.ticker_count,"
            " new_count = excluded.new_count, repeat_count = excluded.repeat_count,"
            " duration_s = excluded.duration_s, error = excluded.error",
            (
                run_date.isoformat(), _now(), status, ticker_count,
                new_count, repeat_count, duration_s, error,
            ),
        )


def last_run(path: Path | None = None) -> dict | None:
    with connect(path) as c:
        r = c.execute("SELECT * FROM runs ORDER BY run_date DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def last_successful_run(path: Path | None = None) -> dict | None:
    with connect(path) as c:
        r = c.execute(
            "SELECT * FROM runs WHERE status = 'ok' ORDER BY run_date DESC LIMIT 1"
        ).fetchone()
    return dict(r) if r else None


# ---------- gex cache ----------

def get_gex(symbol: str, run_date: date, path: Path | None = None) -> dict | None:
    with connect(path) as c:
        r = c.execute(
            "SELECT * FROM gex_cache WHERE symbol = ? AND run_date = ?",
            (symbol.upper(), run_date.isoformat()),
        ).fetchone()
    return dict(r) if r else None


def put_gex(
    symbol: str, run_date: date, image_path: str | None, summary: str, path: Path | None = None
) -> None:
    with connect(path) as c:
        c.execute(
            "INSERT INTO gex_cache (symbol, run_date, image_path, summary)"
            " VALUES (?, ?, ?, ?) ON CONFLICT(symbol, run_date) DO UPDATE SET"
            " image_path = excluded.image_path, summary = excluded.summary",
            (symbol.upper(), run_date.isoformat(), image_path, summary),
        )
