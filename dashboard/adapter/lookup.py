"""Symbol lookup beyond the cached directory. Serves /api/lookup?q=.

The directory in `directory.py` is US-only, because no provider on this install
can enumerate anything else. This is the other half: a query-time lookup that
does reach HKEX, SGX, LSE, Frankfurt, Korea and the rest — one call, after the
typing pauses, never per keystroke.

Why yfinance rather than openbb
-------------------------------
openbb's `equity.search` has no yfinance fetcher at all (providers are cboe,
intrinio, nasdaq, sec, tmx, tradier — all US or Canada), and `etf.search` 402s
on the FMP free tier. Raw HTTP to query2.finance.yahoo.com answers **429 to a
cold client even with a browser User-Agent** (probed 2 Aug 2026); the yfinance
library works because it carries Yahoo's cookie+crumb session. So this is the
second use of the `yfinance-direct` hatch documented in instrument.py, and it is
tagged as such in every response.

The price trap
--------------
`yfinance.Lookup` returns `regularMarketPrice` and **no currency**. BARC.L comes
back as 508.70 — pence — against the 5.09 the tracked row shows. Two prices for
one instrument on one screen is exactly what search.js's header comment exists
to prevent. So the price ships but `price_shown` is False until the currency is
known, which happens when the stock page is opened and
`directory.learn_currency` records what the quote proved.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import directory
import units
import universe

log = logging.getLogger("lookup")

TTL = 15 * 60.0        # a name's identity does not move
NEG_TTL = 60.0         # a miss cached briefly, so a typo is not re-fetched
MAX_ENTRIES = 512
FOLLOWER_WAIT = 3.0
MIN_QUERY = 2
YF_TIMEOUT = 3.0
FETCH_COUNT = 12
GATE_TIMEOUT = 90.0

# Yahoo's quoteType -> the chip the search dropdown draws. Everything else is
# dropped: probes returned `index` (^ATMP-IV, DE000SL0AR60.SG) and
# `cryptocurrency` (NVDAX-USD), neither of which this dashboard can price.
KIND = {"equity": "STK", "etf": "ETF"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean(value) -> str:
    if value is None:
        return ""
    try:
        if isinstance(value, float) and math.isnan(value):
            return ""
    except TypeError:
        pass
    s = str(value).strip()
    return "" if s.lower() in ("nan", "none", "<na>") else s


def _finite(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


@dataclass
class _Entry:
    value: Any
    stored: float
    ttl: float
    provider: str
    errors: list = field(default_factory=list)

    def fresh(self) -> bool:
        return (time.monotonic() - self.stored) < self.ttl


class LookupService:
    """Cached, single-flighted symbol search. Never raises, never blocks long."""

    def __init__(self, gate: Callable[[], bool] | None = None,
                 gate_timeout: float = GATE_TIMEOUT):
        self._gate = gate
        self._gate_timeout = gate_timeout
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._cache: dict[str, _Entry] = {}
        self._inflight: dict[str, threading.Event] = {}
        self._slots = threading.BoundedSemaphore(2)
        self._warm_error: str | None = None

    # ---------------- lifecycle ----------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._warmup, name="lookup-warmup",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _warmup(self) -> None:
        """Import yfinance off the request path.

        It costs ~0.3s, not openbb's several seconds — but a cold import on an
        HTTP worker during IB Gateway startup is the same shape as the 29 Jul
        incident, and the gate is already built. Cheap insurance.
        """
        if self._gate:
            deadline = time.monotonic() + self._gate_timeout
            opened = False
            while not self._stop.is_set() and time.monotonic() < deadline:
                try:
                    if self._gate():
                        opened = True
                        break
                except Exception:
                    opened = True   # a broken gate must not hold search hostage
                    break
                self._stop.wait(1.0)
            if not opened:
                # Same fallback as instrument.py: the gate exists to protect the
                # IB feed's startup, not to gate search on the Gateway being
                # healthy. A Gateway that never reaches IBKR must not leave the
                # search bar permanently "warming".
                log.warning("lookup: gate timed out after %.0fs, warming anyway",
                            self._gate_timeout)
        if self._stop.is_set():
            return
        try:
            import yfinance  # noqa: F401
            self._ready.set()
            log.info("lookup: yfinance ready")
        except Exception as exc:
            self._warm_error = str(exc)
            log.error("lookup: yfinance import failed: %s", exc)

    # ---------------- fetch ----------------

    @staticmethod
    def _yahoo(query: str) -> tuple[list[dict], str, list]:
        try:
            from yfinance import Lookup
            # Bounded: this call holds one of two request slots and the
            # single-flight leader. Left to yfinance's own default, one slow
            # Yahoo response parks an HTTP worker long past FOLLOWER_WAIT and
            # two of them make every later search answer "busy".
            frame = Lookup(query, timeout=YF_TIMEOUT).get_all(count=FETCH_COUNT)
        except Exception as exc:
            return [], "none", [("yahoo", f"{type(exc).__name__}: {exc}"[:160])]

        rows: list[dict] = []
        try:
            records = frame.reset_index().to_dict("records")
        except Exception as exc:
            return [], "none", [("yahoo", f"unreadable frame: {exc}"[:160])]

        for r in records:
            symbol = _clean(r.get("symbol") or r.get("index"))
            kind = KIND.get(_clean(r.get("quoteType")).lower())
            if not symbol or not kind:
                continue
            if not directory.SAFE_SYMBOL.match(symbol):
                continue
            exchange = _clean(r.get("exchange")).upper()
            rows.append({
                "symbol": symbol,
                "name": _clean(r.get("shortName")) or symbol,
                "kind": kind,
                "exchange": exchange,
                "exchange_name": universe.exchange_display(exchange),
                "price": _finite(r.get("regularMarketPrice")),
                "day_change_pct": _finite(r.get("regularMarketPercentChange")),
            })
        return rows, "yfinance-direct", []

    # ---------------- cache ----------------

    def _evict(self) -> None:
        if len(self._cache) <= MAX_ENTRIES:
            return
        for k in [k for k, e in self._cache.items() if not e.fresh()]:
            self._cache.pop(k, None)
        if len(self._cache) > MAX_ENTRIES:
            oldest = sorted(self._cache.items(), key=lambda kv: kv[1].stored)
            for k, _ in oldest[: len(self._cache) - MAX_ENTRIES]:
                self._cache.pop(k, None)

    def _fetch(self, key: str) -> _Entry | None:
        """Single-flight, same shape as instrument.py's `_get`."""
        with self._lock:
            entry = self._cache.get(key)
            if entry and entry.fresh():
                return entry
            waiter = self._inflight.get(key)
            leader = waiter is None
            if leader:
                waiter = self._inflight[key] = threading.Event()

        if not leader:
            waiter.wait(FOLLOWER_WAIT)
            with self._lock:
                return self._cache.get(key)

        try:
            rows, provider, errors = self._yahoo(key)
            entry = _Entry(rows, time.monotonic(),
                           TTL if rows else NEG_TTL, provider, errors)
            with self._lock:
                self._cache[key] = entry
                self._evict()
            if rows:
                # Grow the directory with what we just learned, so the next
                # search for this name is instant and offline. Queued onto the
                # directory's writer thread; never touches this request.
                try:
                    directory.upsert_remote([{
                        "symbol": r["symbol"], "name": r["name"], "type": r["kind"],
                        "exchange": r["exchange"],
                        "region": directory.EXCHANGE_REGION.get(r["exchange"], ""),
                    } for r in rows])
                except Exception:
                    log.exception("lookup: directory write-back failed for %r", key)
            return entry
        except Exception as exc:
            log.exception("lookup: producer failed for %r", key)
            with self._lock:
                return self._cache.get(key) or _Entry(
                    [], time.monotonic(), NEG_TTL, "none",
                    [("lookup", str(exc)[:160])])
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            waiter.set()

    # ---------------- public ----------------

    def lookup(self, query: str, limit: int = 8) -> dict:
        q = (query or "").strip()
        base = {"source": "yfinance-direct", "query": q, "state": "ready",
                "cached": False, "served_at": _now(), "error": None}

        if len(q) < MIN_QUERY:
            return {"meta": {**base, "error": None}, "results": []}
        if not self._ready.is_set():
            return {"meta": {**base, "state": "warming", "retry_after": 2,
                             "error": self._warm_error}, "results": []}
        if not self._slots.acquire(timeout=1.0):
            return {"meta": {**base, "state": "busy", "retry_after": 1,
                             "error": "lookup busy"}, "results": []}
        try:
            key = q.lower()
            with self._lock:
                cached = self._cache.get(key)
                was_fresh = bool(cached and cached.fresh())
            entry = self._fetch(key)
            rows = list(entry.value) if entry else []
            errors = entry.errors if entry else [("lookup", "no result")]

            # A price is only rendered once the currency is proven — see the
            # module docstring. `directory.row` knows it after the stock page
            # has been opened once, or after a build that carried it.
            out = []
            for r in rows[:limit]:
                known = directory.row(r["symbol"]) or {}
                quoted = known.get("currency") or ""
                # Yahoo's price is in the QUOTED unit, so a London name arrives
                # in pence: BARC.L comes back as 508.70 against the 5.09 its own
                # stock page shows. Knowing the currency is not enough — the
                # figure has to be divided too, or search prints "£508.70" for a
                # £5.09 share. Same divisor the stock page applies.
                scale = units.scale_for(quoted)
                out.append({**r,
                            "currency": units.display_currency(scale, quoted),
                            "quote_currency": quoted,
                            "price": units.price(r["price"], scale),
                            "price_shown": bool(quoted),
                            "tracked": r["symbol"] in universe.TICKERS})
            return {"meta": {**base, "cached": was_fresh,
                             "error": None if out else (errors[0][1] if errors else None)},
                    "results": out}
        finally:
            self._slots.release()


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    svc = LookupService(gate=None)
    svc.start()
    for _ in range(100):
        if svc._ready.is_set():
            break
        time.sleep(0.1)

    for q in (sys.argv[1:] or ["tencent", "vanguard s&p 500", "barclays", "zzzznope"]):
        t0 = time.monotonic()
        payload = svc.lookup(q)
        took = time.monotonic() - t0
        meta = payload["meta"]
        print(f"\n{q!r}  [{took:.2f}s] state={meta['state']} cached={meta['cached']} "
              f"error={meta['error']}")
        for r in payload["results"]:
            px = f"{r['price']}" if r["price_shown"] else f"({r['price']} hidden)"
            print(f"  {r['symbol']:<14} {r['kind']:<4} {r['exchange_name'] or r['exchange']:<18} "
                  f"{px:<18} {r['name'][:34]}")
    svc.stop()
    directory.close()
