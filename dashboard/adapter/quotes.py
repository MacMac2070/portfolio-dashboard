"""Daily closes, kept: the cache under every index line, sparkline and FX series.

Nothing in the dashboard remembered a price. The world board re-fetched 420
days of eleven indices every sixty seconds; the sparklines fetched thirty
days of every symbol once a day; a Yahoo outage emptied all of it, and the
equity curve's benchmark line was blank until the market feed had warmed.
Every mature tracker keeps a quotes table with one row per symbol and day —
Wealthfolio's is keyed `{asset}_{date}_{source}` — and fills gaps from it.

This is that table, in SQLite beside the symbol directory. Writers upsert
whatever a provider returned; readers ask for a series and get the union of
every fetch, oldest first. `plan_fetch` says how far back a poller needs to
ask — a week when coverage is current, the full window when it is not — so a
minute-by-minute poller costs eleven symbols × seven days, not × 420.

Two rules the readers rely on. A missing day is never filled from the future:
`at_or_before` walks backwards only (Ghostfolio's issue #7751 is what happens
otherwise). And a stored close is served with its own date, so a caller can
say "as of Friday" rather than pass it off as today's.

The file is gitignored: it is rebuilt from the providers and is never the
only copy of anything.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone

import store

log = logging.getLogger("quotes")

DB_PATH = store.DATA_DIR / "quotes.sqlite3"
SCHEMA_VERSION = 1
FULL_DAYS = 420          # what the world board draws a year of returns from
LOOKBACK_DAYS = 14       # coverage older than this is re-fetched in full
OVERLAP_DAYS = 7         # a current series still re-asks the last week, for restatements
STALE_WEEKDAYS = 3

_lock = threading.Lock()
_ready = False


def _connect() -> sqlite3.Connection:
    global _ready
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    con.execute("PRAGMA busy_timeout=2000")
    with _lock:
        if not _ready:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            con.execute("PRAGMA journal_size_limit=4194304")
            con.execute("""CREATE TABLE IF NOT EXISTS closes (
                symbol TEXT NOT NULL, date TEXT NOT NULL, close REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (symbol, date))""")
            con.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
            con.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('schema_version', ?)",
                        (str(SCHEMA_VERSION),))
            _ready = True
    return con


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- write

def upsert(symbol: str, rows, *, currency: str = "", source: str = "") -> int:
    """Store (date, close) pairs for a symbol. Returns how many were written.

    Non-finite or non-positive closes are skipped; a provider's placeholder
    is not a price. Idempotent: the same row twice is one row.
    """
    clean = []
    for day, close in rows or ():
        try:
            value = float(close)
        except (TypeError, ValueError):
            continue
        if not (value > 0) or value != value:
            continue
        day = str(day)[:10]
        if len(day) != 10:
            continue
        clean.append((symbol, day, value, currency or "", source or "", _now()))
    if not clean:
        return 0
    con = _connect()
    try:
        con.executemany("INSERT OR REPLACE INTO closes VALUES (?, ?, ?, ?, ?, ?)", clean)
    finally:
        con.close()
    return len(clean)


# ---------------------------------------------------------------- read

def series(symbol: str, start: str | None = None, end: str | None = None) -> list[tuple[str, float]]:
    """(date, close) oldest first, bounded inclusively when asked."""
    con = _connect()
    try:
        sql = "SELECT date, close FROM closes WHERE symbol = ?"
        args: list = [symbol]
        if start:
            sql += " AND date >= ?"
            args.append(str(start)[:10])
        if end:
            sql += " AND date <= ?"
            args.append(str(end)[:10])
        sql += " ORDER BY date"
        return [(d, c) for d, c in con.execute(sql, args)]
    finally:
        con.close()


def latest(symbol: str) -> tuple[str, float] | None:
    con = _connect()
    try:
        row = con.execute("SELECT date, close FROM closes WHERE symbol = ? ORDER BY date DESC LIMIT 1",
                          (symbol,)).fetchone()
        return (row[0], row[1]) if row else None
    finally:
        con.close()


def at_or_before(symbol: str, day: str) -> tuple[str, float] | None:
    """The close on `day`, or the nearest earlier one. Never a later one."""
    con = _connect()
    try:
        row = con.execute("SELECT date, close FROM closes WHERE symbol = ? AND date <= ? "
                          "ORDER BY date DESC LIMIT 1", (symbol, str(day)[:10])).fetchone()
        return (row[0], row[1]) if row else None
    finally:
        con.close()


def coverage(symbol: str) -> tuple[str | None, str | None, int]:
    con = _connect()
    try:
        row = con.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM closes WHERE symbol = ?",
                          (symbol,)).fetchone()
        return (row[0], row[1], row[2] or 0) if row else (None, None, 0)
    finally:
        con.close()


def symbols() -> list[str]:
    con = _connect()
    try:
        return [r[0] for r in con.execute("SELECT DISTINCT symbol FROM closes ORDER BY symbol")]
    finally:
        con.close()


# ---------------------------------------------------------------- planning

def plan_fetch(symbol: str, today: date | None = None, *, lookback_days: int = LOOKBACK_DAYS,
               full_days: int = FULL_DAYS, overlap_days: int = OVERLAP_DAYS) -> str:
    """The start date a poller should ask a provider for.

    The full window when there is no coverage, or when the newest stored
    close is older than `lookback_days` (a long outage, a new symbol); a
    short overlap otherwise, so restated closes still land.
    """
    today = today or date.today()
    _, newest, _ = coverage(symbol)
    if not newest or (today - date.fromisoformat(newest)).days > lookback_days:
        return (today - timedelta(days=full_days)).isoformat()
    return (date.fromisoformat(newest) - timedelta(days=overlap_days)).isoformat()


def plan_fetch_all(symbols_: list[str], today: date | None = None, **kw) -> str:
    """One start date for a batched call: the earliest any symbol needs."""
    starts = [plan_fetch(s, today, **kw) for s in symbols_]
    return min(starts) if starts else (today or date.today()).isoformat()


def stale_symbols(symbols_: list[str], today: date | None = None,
                  *, weekdays: int = STALE_WEEKDAYS) -> dict[str, str | None]:
    """{symbol: newest date} for symbols whose newest close is more than
    `weekdays` weekdays old, or absent altogether."""
    import health  # noqa: PLC0415 — the weekday arithmetic lives there

    today = today or date.today()
    out: dict[str, str | None] = {}
    for symbol in symbols_:
        newest = latest(symbol)
        behind = health.weekdays_behind(newest[0] if newest else None, today)
        if behind is None or behind > weekdays:
            out[symbol] = newest[0] if newest else None
    return out


if __name__ == "__main__":
    for symbol in symbols():
        first, last, n = coverage(symbol)
        print(f"{symbol:<12} {n:>5} closes  {first} → {last}")
