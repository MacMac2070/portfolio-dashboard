"""The cached symbol directory — every ticker the search bar can offer offline.

The curated registry in universe.py is 35 names chosen by hand. This is the
other list: ~16k symbols pulled from the enumerable providers, cached locally so
the search bar filters instantly with no network round-trip per keystroke.

Why SQLite, and why it is the first database here
-------------------------------------------------
Everything else in `data/` is JSONL the browser fetches directly. This table is
too big for that (0.8 MB) and is rewritten nightly while the server is reading
it, which is exactly the case `store.py`'s truncate-and-rewrite cannot survive.
SQLite in WAL mode gives readers a consistent snapshot across a rebuild for free.

What can and cannot be enumerated (probed 2 Aug 2026)
----------------------------------------------------
    nasdaq  is_etf=False   7,486 rows   equities, with a listing-exchange letter
    nasdaq  is_etf=True    5,563 rows   ETFs — a DISJOINT result set, hence two calls
    sec                   10,412 rows   symbol/name/cik only, no type, no exchange
    cboe                   5,331 rows   no type, no exchange — not worth a third source
    fmp etf.search           402        free tier
    tmx                                 Canada only

All of them are **US only**. There is no enumerable source for HKEX, SGX, LSE or
Frankfurt on this install — yfinance implements neither `equity.search` nor
`etf.search`. International names reach this table one at a time, through
`upsert_remote()`, when `lookup.py` resolves a query. That is why the rebuild
below deletes only rows it built and never rows it learned.

The `type` column is '' for the 3,310 sec-only rows. Guessing STK would mislabel
several hundred ETFs and dropping them would lose every ADR, so they stay
honestly unknown and self-heal: both the Yahoo lookup and the first
/api/instrument fetch carry a quoteType that fills the blank.
"""
from __future__ import annotations

import gzip
import json
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import universe

SCHEMA = 1
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "directory.sqlite3"

# A symbol has to survive being a URL hash segment and being handed to yfinance.
# This is also the filter that drops nasdaq's preferreds and warrants — ABR$D,
# AAC.U, ACHR.W — whose punctuation yfinance does not accept, so shipping them
# would mean search results that 404 the moment you click one.
SAFE_SYMBOL = re.compile(r"^[A-Za-z0-9.\-]{1,20}$")

# nasdaq's single-letter listing venue -> the vocabulary in universe.EXCHANGE_NAMES.
LISTING_EXCHANGE = {"Q": "NMS", "N": "NYQ", "A": "NYQ", "P": "NYQ", "Z": "NYQ"}

# Venue -> region, used for ONE thing: picking a benchmark index on the stock
# page. regions.py is emphatic that region means underlying exposure, not the
# listing venue (XDJP lists in London and is Japan), and for a directory name we
# cannot know the exposure. The guess is safe because a directory ticker has
# con_id=None and so can never reach the allocation donut, which reads
# `data.regions` off the IB snapshot. The stock page labels it "nearest market".
EXCHANGE_REGION = {
    "NMS": "United States", "NYQ": "United States", "NGM": "United States",
    "NCM": "United States", "NYS": "United States", "ASE": "United States",
    "PCX": "United States", "BTS": "United States", "PNK": "United States",
    "HKG": "Hong Kong / China", "SHH": "Hong Kong / China", "SHZ": "Hong Kong / China",
    "SES": "Singapore",
    "LSE": "United Kingdom", "IOB": "United Kingdom",
    "KSC": "South Korea", "KOE": "South Korea",
    "JPX": "Japan", "TAI": "Taiwan",
}

_lock = threading.RLock()
_local = threading.local()
_writer: ThreadPoolExecutor | None = None
_payload_cache: tuple[str, bytes, bytes] | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# connections
# --------------------------------------------------------------------------
def connect(readonly: bool = True) -> sqlite3.Connection:
    """A connection. Readers get mode=ro; the single writer gets read-write.

    Verified 2 Aug: mode=ro opens a WAL database both when -shm is absent (after
    a clean close) and while a writer holds it open, so no fallback is needed —
    but a missing file still has to be tolerated, because the directory is
    genuinely absent until the first build.
    """
    if readonly:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True,
                              timeout=2.0, check_same_thread=False)
    else:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None -> autocommit, so BEGIN IMMEDIATE / COMMIT below
        # are the only transaction control. With Python's default, sqlite3 opens
        # an implicit transaction before the first DML and the explicit BEGIN
        # then raises "cannot start a transaction within a transaction".
        con = sqlite3.connect(DB_PATH, timeout=10.0, check_same_thread=False,
                              isolation_level=None)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=2000")
    return con


def _reader() -> sqlite3.Connection | None:
    """Thread-local read-only connection, or None if there is no directory yet."""
    con = getattr(_local, "con", None)
    if con is not None:
        return con
    if not DB_PATH.exists():
        return None
    try:
        _local.con = connect(readonly=True)
        return _local.con
    except sqlite3.Error:
        return None


def _writer_pool() -> ThreadPoolExecutor:
    """Every write in this module goes through one thread.

    Two reasons: SQLite allows exactly one writer, and an HTTP worker must never
    block on a database write. `upsert_remote`, `learn_currency` and `note_hit`
    are all fire-and-forget onto this pool.
    """
    global _writer
    with _lock:
        if _writer is None:
            _writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dirwrite")
        return _writer


def _write_conn() -> sqlite3.Connection:
    con = getattr(_local, "wcon", None)
    if con is None:
        con = _local.wcon = connect(readonly=False)
        _ensure_schema(con)
    return con


def _ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript("""
        CREATE TABLE IF NOT EXISTS symbols (
          symbol     TEXT PRIMARY KEY,
          name       TEXT NOT NULL DEFAULT '',
          type       TEXT NOT NULL DEFAULT '',
          exchange   TEXT NOT NULL DEFAULT '',
          currency   TEXT NOT NULL DEFAULT '',
          region     TEXT NOT NULL DEFAULT '',
          source     TEXT NOT NULL DEFAULT '',
          hits       INTEGER NOT NULL DEFAULT 0,
          build_rev  INTEGER NOT NULL DEFAULT 0,
          first_seen TEXT NOT NULL DEFAULT '',
          last_seen  TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS symbols_name ON symbols(name COLLATE NOCASE);
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
    """)
    con.commit()


# --------------------------------------------------------------------------
# metadata
# --------------------------------------------------------------------------
def _meta(con: sqlite3.Connection, key: str, default: str = "") -> str:
    try:
        row = con.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
    except sqlite3.Error:
        return default
    return row["v"] if row else default


def version() -> str:
    """`"1.412"` — schema and revision. A monotonic counter rather than a
    timestamp, so the browser's comparison needs no clock reasoning."""
    con = _reader()
    if con is None:
        return ""
    return f"{SCHEMA}.{_meta(con, 'rev', '0')}"


def stale(hours: float = 20.0) -> bool:
    con = _reader()
    if con is None:
        return True
    built = _meta(con, "generated_at")
    if not built:
        return True
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(built)
    except ValueError:
        return True
    return age.total_seconds() > hours * 3600


def count() -> int:
    con = _reader()
    if con is None:
        return 0
    try:
        return con.execute("SELECT COUNT(*) AS n FROM symbols").fetchone()["n"]
    except sqlite3.Error:
        return 0


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------
def row(symbol: str) -> dict | None:
    con = _reader()
    if con is None or not symbol:
        return None
    try:
        hit = con.execute(
            "SELECT symbol, name, type, exchange, currency, region "
            "FROM symbols WHERE symbol = ?", (symbol,)).fetchone()
    except sqlite3.Error:
        return None
    return dict(hit) if hit else None


def search(query: str, limit: int = 8) -> list[dict]:
    """Server-side search. The browser normally filters its own cached copy;
    this exists for the API and for the __main__ harness."""
    con = _reader()
    q = (query or "").strip()
    if con is None or not q:
        return []
    try:
        rows = con.execute("""
            SELECT symbol, name, type, exchange, currency,
                   CASE WHEN symbol = :q THEN 0
                        WHEN symbol LIKE :pre THEN 1
                        ELSE 2 END AS bucket
            FROM symbols
            WHERE symbol LIKE :pre OR name LIKE :sub
            ORDER BY bucket, hits DESC, length(symbol), symbol
            LIMIT :limit
        """, {"q": q.upper(), "pre": q + "%", "sub": "%" + q + "%", "limit": limit}).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def payload() -> dict:
    """The whole directory, compactly, for the browser to cache and filter."""
    con = _reader()
    if con is None:
        return {"meta": {"version": "", "generated_at": None, "count": 0,
                         "error": "directory not built — run adapter/directory.py"},
                "unchanged": False, "cols": ["s", "n", "t", "x", "c"], "rows": []}
    try:
        rows = con.execute(
            "SELECT symbol, name, type, exchange, currency FROM symbols "
            "ORDER BY symbol").fetchall()
    except sqlite3.Error as exc:
        return {"meta": {"version": "", "generated_at": None, "count": 0,
                         "error": str(exc)},
                "unchanged": False, "cols": ["s", "n", "t", "x", "c"], "rows": []}
    return {
        "meta": {"version": version(), "generated_at": _meta(con, "generated_at") or None,
                 "count": len(rows), "error": None},
        "unchanged": False,
        # Positional rows, not objects — the key names would otherwise be ~60% of
        # the payload across 16k entries. `xn` is the venue's display label,
        # resolved here so the browser never maps codes itself; the strings
        # repeat across thousands of rows, so gzip costs almost nothing for it.
        "cols": ["s", "n", "t", "x", "c", "xn"],
        "rows": [[r["symbol"], r["name"], r["type"], r["exchange"], r["currency"],
                  universe.exchange_display(r["exchange"])]
                 for r in rows],
    }


def payload_bytes() -> tuple[str, bytes, bytes]:
    """(version, json, gzip), memoised on version. Serialising 16k rows costs
    ~40 ms and gzip another ~12 ms; both are paid once per rebuild, not per
    request."""
    global _payload_cache
    v = version()
    with _lock:
        if _payload_cache and _payload_cache[0] == v:
            return _payload_cache
    raw = json.dumps(payload(), separators=(",", ":")).encode()
    packed = gzip.compress(raw, 6)
    with _lock:
        _payload_cache = (v, raw, packed)
    return _payload_cache


# --------------------------------------------------------------------------
# writes — all queued onto the single writer thread
# --------------------------------------------------------------------------
def _bump_rev(con: sqlite3.Connection) -> int:
    rev = int(_meta(con, "rev", "0")) + 1
    con.execute("INSERT INTO meta(k, v) VALUES('rev', ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v", (str(rev),))
    return rev


def _do_upsert(rows: list[dict]) -> int:
    con = _write_conn()
    now = _now()
    changed = 0
    con.execute("BEGIN IMMEDIATE")
    try:
        for r in rows:
            sym = (r.get("symbol") or "").strip()
            if not sym or not SAFE_SYMBOL.match(sym):
                continue
            before = con.execute(
                "SELECT name, type, exchange, currency FROM symbols WHERE symbol = ?",
                (sym,)).fetchone()
            con.execute("""
                INSERT INTO symbols
                  (symbol, name, type, exchange, currency, region, source,
                   hits, build_rev, first_seen, last_seen)
                VALUES (:symbol, :name, :type, :exchange, :currency, :region,
                        'yahoo', 0, 0, :now, :now)
                ON CONFLICT(symbol) DO UPDATE SET
                  name     = CASE WHEN excluded.name <> ''   THEN excluded.name     ELSE symbols.name END,
                  type     = CASE WHEN symbols.type = ''     THEN excluded.type     ELSE symbols.type END,
                  exchange = CASE WHEN symbols.exchange = '' THEN excluded.exchange ELSE symbols.exchange END,
                  currency = CASE WHEN symbols.currency = '' THEN excluded.currency ELSE symbols.currency END,
                  region   = CASE WHEN symbols.region = ''   THEN excluded.region   ELSE symbols.region END,
                  last_seen = excluded.last_seen
            """, {"symbol": sym, "name": r.get("name") or "", "type": r.get("type") or "",
                  "exchange": r.get("exchange") or "", "currency": r.get("currency") or "",
                  "region": r.get("region") or "", "now": now})
            after = con.execute(
                "SELECT name, type, exchange, currency FROM symbols WHERE symbol = ?",
                (sym,)).fetchone()
            if before is None or tuple(before) != tuple(after):
                changed += 1
        # Only a change the browser would render bumps the version.
        if changed:
            _bump_rev(con)
        con.commit()
    except Exception:
        con.rollback()
        raise
    return changed


def upsert_remote(rows: Iterable[dict]) -> Any:
    """Learn symbols from a Yahoo lookup. Queued; returns a Future."""
    batch = list(rows)
    if not batch:
        return None
    return _writer_pool().submit(_do_upsert, batch)


def learn_currency(symbol: str, currency: str, region: str = "") -> Any:
    """Record the currency an /api/instrument fetch proved.

    This is what eventually lets the search dropdown show a price for a remote
    result: `lookup.py` refuses to render one until the currency is known,
    because Yahoo's search returns a bare number and BARC.L's is in pence.
    """
    if not symbol or not currency:
        return None
    return upsert_remote([{"symbol": symbol, "currency": currency, "region": region}])


def _do_note_hit(symbol: str) -> None:
    con = _write_conn()
    con.execute("UPDATE symbols SET hits = hits + 1, last_seen = ? WHERE symbol = ?",
                (_now(), symbol))
    con.commit()


def note_hit(symbol: str) -> Any:
    """Popularity, for search ranking. Never bumps the version — the browser
    filters its own copy and does not need to re-download for a counter."""
    if not symbol:
        return None
    return _writer_pool().submit(_do_note_hit, symbol)


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------
def _obb():
    from openbb import obb
    obb.user.preferences.output_type = "dataframe"
    return obb


def _records(df) -> list[dict]:
    try:
        return df.reset_index().to_dict("records")
    except Exception:
        return df.to_dict("records")


def _clean(value) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s.lower() in ("nan", "none", "<na>") else s


def _fetch_sources(log) -> tuple[dict[str, dict], dict[str, int]]:
    """Merge the three enumerable calls. Later sources fill blanks only."""
    obb = _obb()
    merged: dict[str, dict] = {}
    stats = {"nasdaq_stk": 0, "nasdaq_etf": 0, "sec": 0, "dropped": 0}

    def take(sym: str, name: str, type_: str, exchange: str, source: str) -> bool:
        if not sym or not SAFE_SYMBOL.match(sym):
            stats["dropped"] += 1
            return False
        # A dot means different things per source. In nasdaq's file it is the
        # class/unit/warrant separator — BRK.B, AAC.U, ACHR.W — which yfinance
        # does not accept; sec lists the same securities as BRK-B, which it
        # does. In a Yahoo-learned symbol the dot is the venue suffix (0700.HK)
        # and is essential. So dots are dropped for nasdaq only, never globally.
        if source == "nasdaq" and "." in sym:
            stats["dropped"] += 1
            return False
        cur = merged.get(sym)
        if cur is None:
            merged[sym] = {"symbol": sym, "name": name, "type": type_,
                           "exchange": exchange, "source": source}
            return True
        # sec's names are shorter and cleaner ("Apple Inc." vs "Agilent
        # Technologies, Inc. Common Stock"), so it wins the name; nasdaq is the
        # only source with a type, so it keeps that.
        if source == "sec" and name:
            cur["name"] = name
        if not cur["type"] and type_:
            cur["type"] = type_
        if not cur["exchange"] and exchange:
            cur["exchange"] = exchange
        return False

    for label, kwargs, type_ in (("nasdaq_stk", {"is_etf": False}, "STK"),
                                 ("nasdaq_etf", {"is_etf": True}, "ETF")):
        df = obb.equity.search(query="", provider="nasdaq", **kwargs)
        rows = _records(df)
        log(f"  nasdaq {type_:<3} {len(rows):>6} rows")
        for r in rows:
            if _clean(r.get("test_issue")).upper() == "Y":
                stats["dropped"] += 1
                continue
            if _clean(r.get("nasdaq_traded")).upper() == "N":
                stats["dropped"] += 1
                continue
            letter = _clean(r.get("Listing Exchange") or r.get("listing_exchange"))
            take(_clean(r.get("symbol")), _clean(r.get("name")), type_,
                 LISTING_EXCHANGE.get(letter.upper(), ""), "nasdaq")
            stats[label] += 1

    df = obb.equity.search(query="", provider="sec")
    rows = _records(df)
    log(f"  sec        {len(rows):>6} rows")
    for r in rows:
        if take(_clean(r.get("symbol")), _clean(r.get("name")), "", "", "sec"):
            stats["sec"] += 1

    return merged, stats


def build(log_to=print) -> dict:
    """Rebuild the enumerable part of the directory. Learned rows survive.

    One transaction, in place. The database is NOT written to a temp file and
    renamed: a running server holds open file handles, so after a rename its
    readers would keep reading the old inode forever. WAL already gives them a
    consistent snapshot for the duration of the write and flips atomically at
    COMMIT, which is the whole reason this is SQLite.
    """
    t0 = time.monotonic()
    merged, stats = _fetch_sources(log_to)

    con = _write_conn()
    now = _now()
    con.execute("BEGIN IMMEDIATE")
    try:
        rev = _bump_rev(con)
        before = con.execute("SELECT COUNT(*) AS n FROM symbols").fetchone()["n"]
        con.executemany("""
            INSERT INTO symbols
              (symbol, name, type, exchange, currency, region, source,
               hits, build_rev, first_seen, last_seen)
            VALUES (:symbol, :name, :type, :exchange, '', '', :source,
                    0, :rev, :now, :now)
            ON CONFLICT(symbol) DO UPDATE SET
              name      = excluded.name,
              type      = CASE WHEN excluded.type <> '' THEN excluded.type ELSE symbols.type END,
              exchange  = CASE WHEN excluded.exchange <> '' THEN excluded.exchange ELSE symbols.exchange END,
              source    = excluded.source,
              build_rev = excluded.build_rev,
              last_seen = excluded.last_seen
        """, [{**r, "rev": rev, "now": now} for r in merged.values()])

        # Delisted names go, but only ones this build owns. A symbol learned
        # from a Yahoo lookup is the only record of most international listings
        # and must outlive every rebuild.
        removed = con.execute(
            "DELETE FROM symbols WHERE source <> 'yahoo' AND build_rev < ?", (rev,)).rowcount
        total = con.execute("SELECT COUNT(*) AS n FROM symbols").fetchone()["n"]
        for k, v in (("schema", str(SCHEMA)), ("generated_at", now),
                     ("row_count", str(total)),
                     ("build_sources", "nasdaq,nasdaq_etf,sec")):
            con.execute("INSERT INTO meta(k, v) VALUES(?, ?) "
                        "ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, v))
        con.commit()
    except Exception:
        con.rollback()
        raise

    global _payload_cache
    with _lock:
        _payload_cache = None

    out = {"rows": total, "added": total - before, "removed": removed,
           "version": f"{SCHEMA}.{rev}", "seconds": round(time.monotonic() - t0, 1),
           **stats}
    log_to(f"  merged {len(merged)} kept, {stats['dropped']} dropped "
           f"-> {total} rows, version {out['version']} in {out['seconds']}s")
    return out


def close() -> None:
    global _writer
    with _lock:
        if _writer is not None:
            _writer.shutdown(wait=False)
            _writer = None


# --------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if "--search" in sys.argv:
        q = sys.argv[sys.argv.index("--search") + 1]
        for r in search(q, 10):
            print(f"  {r['symbol']:<12} {r['type'] or '--':<4} {r['exchange'] or '--':<5} {r['name'][:44]}")
        raise SystemExit(0)

    print(f"building directory -> {DB_PATH}")
    stats = build()
    print()
    print(f"  {stats['rows']} symbols, version {stats['version']}")
    print(f"  added {stats['added']}, removed {stats['removed']}, dropped {stats['dropped']}")

    con = _reader()
    kinds = dict(con.execute(
        "SELECT COALESCE(NULLIF(type,''),'(unknown)') AS t, COUNT(*) AS n "
        "FROM symbols GROUP BY t ORDER BY n DESC").fetchall())
    print(f"  by type: {kinds}")
    venues = dict(con.execute(
        "SELECT COALESCE(NULLIF(exchange,''),'(none)') AS x, COUNT(*) AS n "
        "FROM symbols GROUP BY x ORDER BY n DESC LIMIT 12").fetchall())
    print(f"  by venue: {venues}")

    bad_sym = con.execute(
        "SELECT symbol FROM symbols WHERE symbol GLOB '*[^A-Za-z0-9.-]*' LIMIT 5").fetchall()
    assert not bad_sym, f"unsafe symbols: {[r['symbol'] for r in bad_sym]}"
    bad_type = con.execute(
        "SELECT DISTINCT type FROM symbols WHERE type NOT IN ('STK','ETF','')").fetchall()
    assert not bad_type, f"unexpected types: {[r['type'] for r in bad_type]}"
    # Asserted for the codes this build produces, reported for the ones Yahoo
    # taught us. universe.py scopes its equivalent check the same way: a venue
    # learned at runtime having no display label is a cosmetic gap that degrades
    # to the raw code, not a reason to fail the build.
    unresolved_built = sorted({r["exchange"] for r in con.execute(
        "SELECT DISTINCT exchange FROM symbols "
        "WHERE exchange <> '' AND source <> 'yahoo'").fetchall()
        if r["exchange"].upper() not in universe.EXCHANGE_NAMES})
    assert not unresolved_built, f"built exchange codes with no display name: {unresolved_built}"

    unlabelled = sorted({r["exchange"] for r in con.execute(
        "SELECT DISTINCT exchange FROM symbols "
        "WHERE exchange <> '' AND source = 'yahoo'").fetchall()
        if r["exchange"].upper() not in universe.EXCHANGE_NAMES})
    if unlabelled:
        print(f"  learned venues with no label (render as the raw code): {' '.join(unlabelled)}")

    v, raw, packed = payload_bytes()
    print(f"  payload {len(raw)/1e6:.2f} MB json, {len(packed)/1024:.0f} KB gzip")
    print("  directory OK — symbols safe, types valid, venues resolve")
    close()
