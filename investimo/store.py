"""SQLite persistence: universe, pins, per-MA flag state, and the run log."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone

from .config import DB_PATH
from .market_data import is_symbol

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
"""

_schema_ready = False


@contextmanager
def connect():
    """Connection that commits on success. Creates the schema on first use."""
    global _schema_ready
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        if not _schema_ready:
            conn.executescript(SCHEMA)
            _schema_ready = True
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize(symbols: list[str]) -> list[str]:
    return [s for s in (x.strip().upper() for x in symbols) if is_symbol(s)]


# ---------- universe ----------

def never_populated() -> bool:
    """True only before the first ticker is ever added, so emptying the universe
    with /remove doesn't re-trigger seeding."""
    with connect() as c:
        return c.execute("SELECT 1 FROM tickers LIMIT 1").fetchone() is None


def universe() -> list[str]:
    with connect() as c:
        rows = c.execute("SELECT symbol FROM tickers WHERE active = 1 ORDER BY symbol")
        return [r["symbol"] for r in rows]


def add_tickers(symbols: list[str], source: str = "manual") -> list[str]:
    """Returns the symbols that were newly added or reactivated."""
    added = []
    with connect() as c:
        for s in _normalize(symbols):
            row = c.execute("SELECT active FROM tickers WHERE symbol = ?", (s,)).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO tickers (symbol, source_watchlist, active, added_at)"
                    " VALUES (?, ?, 1, ?)",
                    (s, source, _now()),
                )
            elif not row["active"]:
                c.execute("UPDATE tickers SET active = 1 WHERE symbol = ?", (s,))
            else:
                continue
            added.append(s)
    return added


def remove_tickers(symbols: list[str]) -> list[str]:
    """Deactivates symbols and forgets their pins and flag state, so a later
    re-add starts fresh. Returns the ones that were active."""
    removed = []
    with connect() as c:
        for s in _normalize(symbols):
            cur = c.execute("UPDATE tickers SET active = 0 WHERE symbol = ? AND active = 1", (s,))
            if cur.rowcount:
                removed.append(s)
                c.execute("DELETE FROM pins WHERE symbol = ?", (s,))
                c.execute("DELETE FROM flag_state WHERE symbol = ?", (s,))
    return removed


# ---------- pins ----------

def pins() -> set[str]:
    with connect() as c:
        return {r["symbol"] for r in c.execute("SELECT symbol FROM pins")}


def pin(symbol: str) -> None:
    """Pin a symbol, adding it to the universe if needed (a pin outside the
    universe would never be scanned)."""
    add_tickers([symbol])
    with connect() as c:
        c.execute(
            "INSERT OR IGNORE INTO pins (symbol, pinned_at) VALUES (?, ?)",
            (symbol.upper(), _now()),
        )


def unpin(symbol: str) -> bool:
    with connect() as c:
        return c.execute("DELETE FROM pins WHERE symbol = ?", (symbol.upper(),)).rowcount > 0


# ---------- flag state ----------

def flag_states() -> dict[tuple[str, str], dict]:
    with connect() as c:
        rows = c.execute("SELECT * FROM flag_state")
        return {(r["symbol"], r["ma_type"]): dict(r) for r in rows}


def set_flag_states(updates: dict[tuple[str, str], tuple[str, str | None]]) -> None:
    """Upsert (symbol, ma_type) -> (state, first_entered_date) in one transaction."""
    with connect() as c:
        c.executemany(
            "INSERT INTO flag_state (symbol, ma_type, state, first_entered_date)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(symbol, ma_type) DO UPDATE SET"
            " state = excluded.state, first_entered_date = excluded.first_entered_date",
            [(sym, ma, state, first) for (sym, ma), (state, first) in updates.items()],
        )


# ---------- runs ----------

def record_run(
    run_date: date,
    status: str,
    ticker_count: int = 0,
    new_count: int = 0,
    repeat_count: int = 0,
    duration_s: float = 0.0,
    error: str | None = None,
) -> None:
    with connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO runs (run_date, ran_at, status, ticker_count,"
            " new_count, repeat_count, duration_s, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_date.isoformat(), _now(), status, ticker_count,
                new_count, repeat_count, duration_s, error,
            ),
        )


def last_run(only_ok: bool = False) -> dict | None:
    where = "WHERE status = 'ok'" if only_ok else ""
    with connect() as c:
        r = c.execute(f"SELECT * FROM runs {where} ORDER BY run_date DESC LIMIT 1").fetchone()
    return dict(r) if r else None
