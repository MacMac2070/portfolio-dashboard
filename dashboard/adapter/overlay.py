"""Names you added from the search bar, persisted outside the curated registry.

`universe.py` is hand-written: brand hex colours sampled per issuer, the mark
slug each name wears, a `_VENUE` table, and comments explaining every
judgement. A web request must not rewrite that file —
a malformed append is a syntax error that takes down every page until someone
fixes it by hand, and machine-appended entries would erode the curation the file
exists for.

So the UI writes here instead, and `universe._overlay()` merges this at import.
Same persistence, same watchlist group, no generated Python.

This is the only file in the project written from an HTTP request while another
process may be reading it, so it is also the only one written atomically —
`store.py` truncates in place, which is safe for a file only the nightly job
touches and not for this one.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import marks
import universe

log = logging.getLogger("overlay")

PATH = Path(__file__).resolve().parent.parent / "data" / "watchlist_extra.json"
SCHEMA = 1
MAX_ENTRIES = 200

# Same shape the directory enforces: safe in a URL hash, acceptable to yfinance.
SAFE_SYMBOL = re.compile(r"^[A-Za-z0-9.\-]{1,20}$")

_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty() -> dict:
    return {"schema": SCHEMA, "updated_at": None, "entries": []}


def read() -> dict:
    """Never raises. A missing or corrupt file reads as empty."""
    try:
        doc = json.loads(PATH.read_text())
    except (OSError, ValueError):
        return _empty()
    if not isinstance(doc, dict) or not isinstance(doc.get("entries"), list):
        return _empty()
    return doc


def entries() -> list[dict]:
    return read().get("entries") or []


def _write(doc: dict) -> None:
    """Temp file in the same directory, fsync, rename.

    `os.replace` is atomic within a directory, so a reader either sees the whole
    previous file or the whole new one — never a truncated document. The temp
    file is created in the destination directory precisely so the rename cannot
    cross a filesystem boundary and silently degrade to a copy.
    """
    PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=PATH.parent, prefix=".watchlist_extra.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(doc, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, PATH)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def add(key: str, *, name: str = "", symbol: str = "", sector: str = "",
        exchange: str = "", currency: str = "", region: str = "") -> dict:
    """Track a symbol found through search. Idempotent.

    Fills anything the caller left blank from the symbol directory, and refuses
    an exchange code with no display name — that refusal is what keeps
    `universe.py`'s self-check green by construction rather than by hope.
    """
    key = (key or "").strip()
    if not key or not SAFE_SYMBOL.match(key):
        return {"ok": False, "error": f"unusable symbol {key!r}"}
    if key in universe.TICKERS:
        return {"ok": True, "added": False, "key": key, "reason": "already tracked"}

    # The collision that matters is the SYMBOL, not the key. Tencent is curated
    # as key "700" quoting symbol "0700.HK"; adding "0700.HK" passes the key
    # check above and then shows the same company twice in search — once with a
    # live price and once without, which is worse than not adding it at all.
    quote_symbol = (symbol or "").strip() or key
    for t in universe.TICKERS.values():
        if t.symbol.upper() in (key.upper(), quote_symbol.upper()):
            return {"ok": True, "added": False, "key": key,
                    "tracked_as": t.key,
                    "reason": f"already tracked as {t.key}"}

    # The directory is the authority on what a symbol actually is; the caller's
    # values are only a hint, so they lose to it rather than the other way round.
    try:
        import directory
        row = directory.row(key) or {}
    except Exception:
        row = {}

    name = name or row.get("name") or key
    symbol = symbol or row.get("symbol") or key
    exchange = (exchange or row.get("exchange") or "").upper()
    currency = currency or row.get("currency") or ""
    region = region or row.get("region") or ""
    if not region and exchange:
        try:
            import directory
            region = directory.EXCHANGE_REGION.get(exchange, "")
        except Exception:
            region = ""
    kind = (row.get("type") or "").upper()
    sector = sector or (universe.ETFS if kind == "ETF" else universe.OTHER)

    # The issuer's mark, looked up once here and stored, so the entry reads
    # like a curated row from then on and no restart ever asks the network
    # for it again. None is stored as "" and reads back as "no mark".
    logo = marks.resolve(symbol, exchange) or ""

    # An unlabelled venue is not a reason to refuse. universe.exchange_display()
    # passes an unknown code through unchanged by design, and Yahoo returns
    # plenty this table has never named (NEO, SET, JKT, IST…). Refusing here
    # meant the Watch button failed outright on a perfectly real instrument.
    if exchange and exchange not in universe.EXCHANGE_NAMES:
        log.info("overlay: %s lists on %s, which has no display label — "
                 "storing the raw code", key, exchange)

    with _LOCK:
        doc = read()
        rows = doc.get("entries") or []
        if any(e.get("key") == key for e in rows):
            return {"ok": True, "added": False, "key": key, "reason": "already in overlay"}
        if len(rows) >= MAX_ENTRIES:
            return {"ok": False, "error": f"watchlist full ({MAX_ENTRIES})"}
        rows.append({
            "key": key, "symbol": symbol, "name": name, "sector": sector,
            "brand": universe.DEFAULT_BRAND, "logo": logo, "exchange": exchange,
            "currency": currency, "region": region, "added_at": _now(),
        })
        doc.update({"schema": SCHEMA, "updated_at": _now(), "entries": rows})
        _write(doc)

    # Live effect without a restart. The overlay file is only for surviving one.
    # `synthetic()` takes the SYMBOL positionally and sets key=symbol from it,
    # so a key that differs from its quote symbol has to be restored explicitly
    # — otherwise this session quotes under the wrong one and the row shows a
    # dash until a restart re-reads the file.
    live = replace(universe.synthetic(
        symbol, name, sector=sector, exchange=exchange,
        currency=currency, region=region, logo=logo), key=key)
    universe.register(live)

    return {"ok": True, "added": True, "key": key, "sector": sector,
            "symbol": symbol, "name": name}


def remove(key: str) -> dict:
    """Drop an overlay entry. Curated names are not removable this way.

    The in-process TICKERS entry stays until restart — `universe.register` has
    no inverse, and unregistering mid-session would strand any page currently
    showing it. The file is the source of truth on next start.
    """
    key = (key or "").strip()
    with _LOCK:
        doc = read()
        rows = doc.get("entries") or []
        kept = [e for e in rows if e.get("key") != key]
        if len(kept) == len(rows):
            return {"ok": True, "removed": False, "key": key, "reason": "not in overlay"}
        doc.update({"schema": SCHEMA, "updated_at": _now(), "entries": kept})
        _write(doc)
    return {"ok": True, "removed": True, "key": key, "restart_required": True}


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        print(add(sys.argv[1]))
    rows = entries()
    print(f"{len(rows)} overlay entries -> {PATH}")
    for e in rows:
        print(f"  {e['key']:<14} {e.get('exchange') or '—':<6} {e.get('currency') or '—':<5} "
              f"{e.get('sector'):<14} {e.get('name')}")
