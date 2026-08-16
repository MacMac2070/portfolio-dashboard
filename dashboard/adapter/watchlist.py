"""openbb quotes for tickers the IB feed does not cover.

Deliberately separate from `feed.py`. That module owns a held-open IB Gateway
connection refreshing every 3s and is the one thing on this dashboard that must
not be disturbed; this one makes slow batched HTTP calls to yfinance. They share
no state, no thread and no imports — the only thing they have in common is that
`serve.py` starts both.

Cadence differs because the sources do:

  quotes    one batched call for all 15 symbols, every 60s. yfinance tolerates
            that comfortably; polling it at the IB feed's 3s would not be
            welcome and would gain nothing, since these are delayed quotes.

  history   one batched call for 30d of daily closes, refreshed once a day.
            Sparkline shape does not change intraday, so re-fetching it every
            minute would be 60x the calls for the same picture.

A failed call keeps the last good values and flags them stale rather than
blanking the page — and can never affect an owned row, which is served entirely
by the IB feed.
"""
from __future__ import annotations

import logging
import math
import threading
from datetime import date, datetime, timedelta, timezone

import universe

QUOTE_SECONDS = 60.0
HISTORY_DAYS = 30
FIRST_RETRY_SECONDS = 15.0

log = logging.getLogger("watchlist")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _finite(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


class WatchlistFeed:
    def __init__(self, poll_seconds: float = QUOTE_SECONDS, gate=None,
                 gate_timeout: float = 90.0):
        self.poll_seconds = poll_seconds
        self.symbols = universe.watchlist_symbols()
        # Optional predicate that must return True before the first openbb call.
        # See _await_gate for why this exists.
        self._gate = gate
        self._gate_timeout = gate_timeout

        self._lock = threading.Lock()
        self._quotes: dict[str, dict] = {}
        self._spark: dict[str, list[float]] = {}
        self._spark_day: str | None = None
        self._last_refresh: str | None = None
        self._error: str | None = None
        self._polls = 0

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------- called from the HTTP thread ----------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="watchlist", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=8)

    def add_symbol(self, symbol: str) -> bool:
        """Quote one more symbol from the next poll onwards.

        Rebinds `self.symbols` rather than appending to it. The list is read
        without the lock at the two refresh sites, and rebinding means a refresh
        already iterating finishes against the old list instead of mutating
        under itself.
        """
        if not symbol:
            return False
        with self._lock:
            if symbol in self.symbols:
                return False
            self.symbols = [*self.symbols, symbol]
        return True

    def snapshot(self) -> dict:
        """Latest quotes plus honest freshness. Never raises, never blocks."""
        with self._lock:
            quotes = {
                sym: self._with_fallback(q, self._spark.get(sym, []))
                for sym, q in self._quotes.items()
            }
            meta = {
                "source": "openbb-yfinance",
                "connected": bool(self._quotes) and self._error is None,
                "last_refresh": self._last_refresh,
                "symbols": len(self.symbols),
                "quoted": len(self._quotes),
                "spark_day": self._spark_day,
                "poll_seconds": self.poll_seconds,
                "polls": self._polls,
                "error": self._error,
                "served_at": _now(),
            }
        return {"meta": meta, "quotes": quotes, "universe": universe.to_dict()}

    @staticmethod
    def _with_fallback(quote: dict, spark: list[float]) -> dict:
        """Fill a missing live price from the daily closes we already hold.

        Thinly-traded ETFs quote a prior close but no last price — VUSA.L and
        the same pattern seen on IUCS.L and XDJP.L. Owned positions can fall
        back to IBKR's price; a watchlist row has no such source, so without
        this it would render as a dash forever.

        The 30d history is already fetched for the sparkline, and its last two
        closes give exactly what the quote is missing. Flagged as `history` so
        the page can tell the difference between a live tick and yesterday's
        close.
        """
        out = {**quote, "spark": spark, "price_source": "quote"}
        if out.get("price") is not None or len(spark) < 2:
            return out

        last, prev = spark[-1], spark[-2]
        out["price"] = last
        out["price_source"] = "history"
        if out.get("prev_close") is None:
            out["prev_close"] = prev
        ref = out["prev_close"]
        if out.get("day_change_pct") is None and ref:
            out["day_change_pct"] = (last / ref - 1.0) * 100.0
        return out

    # ---------- worker ----------

    def _await_gate(self) -> None:
        """Hold off the first openbb call until the caller says it is safe.

        `from openbb import obb` costs several seconds of pure CPU as it builds
        its extension registry, and Python's GIL means that blocks every other
        thread in the process — including the IB feed's asyncio loop, which then
        cannot service its socket. Observed on 29 Jul: starting both feeds
        together made IB Gateway's startup requests time out
        ("positions request timed out", "account updates ... request timed
        out"), leaving the live feed connected but with no positions.

        Waiting for the live feed's first successful refresh costs a few seconds
        once, and removes the contention entirely: after the import, openbb work
        is network-bound and releases the GIL.
        """
        if not self._gate:
            return
        waited = 0.0
        while waited < self._gate_timeout and not self._stop.is_set():
            try:
                if self._gate():
                    log.info("live feed ready after %.0fs, starting watchlist", waited)
                    return
            except Exception:
                return          # a broken gate must not block quotes forever
            self._stop.wait(1.0)
            waited += 1.0
        log.info("gate not satisfied after %.0fs, starting watchlist anyway",
                 self._gate_timeout)

    def _run(self) -> None:
        self._await_gate()
        while not self._stop.is_set():
            ok = self._refresh_quotes()
            if self._spark_day != date.today().isoformat():
                self._refresh_history()
            # Back off only until the first success, so a slow start does not
            # leave the page empty for a full minute.
            self._sleep(self.poll_seconds if ok else FIRST_RETRY_SECONDS)

    def _sleep(self, seconds: float) -> None:
        waited = 0.0
        while waited < seconds and not self._stop.is_set():
            self._stop.wait(min(0.5, seconds - waited))
            waited += 0.5

    def _obb(self):
        from openbb import obb
        obb.user.preferences.output_type = "dataframe"
        return obb

    def _refresh_quotes(self) -> bool:
        try:
            obb = self._obb()
            df = obb.equity.price.quote(symbol=",".join(self.symbols), provider="yfinance")
        except Exception as exc:
            log.warning("watchlist quotes failed: %s", exc)
            with self._lock:
                self._error = str(exc)
            return False

        quotes: dict[str, dict] = {}
        for row in df.to_dict("records"):
            symbol = row.get("symbol")
            if not symbol:
                continue
            # No LSE names in the watchlist today, but GBp would silently be
            # 100x if one were ever added — the same trap the live feed hit.
            divisor = 100.0 if str(row.get("currency", "")).strip() == "GBp" else 1.0
            last = _finite(row.get("last_price"))
            prev = _finite(row.get("prev_close"))
            if last is not None:
                last /= divisor
            if prev is not None:
                prev /= divisor

            change = None
            if last is not None and prev and prev > 0:
                change = (last / prev - 1.0) * 100.0

            quotes[symbol] = {
                "symbol": symbol,
                "name": row.get("name") or "",
                "currency": row.get("currency") or "",
                "price": last,
                "prev_close": prev,
                "day_change_pct": change,
            }

        with self._lock:
            if quotes:
                self._quotes = quotes
                self._last_refresh = _now()
                self._error = None
            self._polls += 1
        missing = [s for s in self.symbols if s not in quotes]
        if missing:
            log.info("no quote for: %s", ", ".join(missing))
        return bool(quotes)

    def _refresh_history(self) -> None:
        """30d of daily closes for the sparklines. Once a day is plenty."""
        start = (date.today() - timedelta(days=HISTORY_DAYS * 2 + 10)).isoformat()
        try:
            obb = self._obb()
            df = obb.equity.price.historical(
                symbol=",".join(self.symbols), provider="yfinance",
                start_date=start, interval="1d").reset_index()
        except Exception as exc:
            log.warning("watchlist history failed, sparklines omitted: %s", exc)
            return

        series: dict[str, list[float]] = {}
        if "symbol" in df.columns:
            for symbol, sub in df.groupby("symbol"):
                closes = [c for c in (_finite(v) for v in sub["close"].tolist()) if c is not None]
                if len(closes) >= 2:
                    series[str(symbol)] = closes[-HISTORY_DAYS:]
        elif len(self.symbols) == 1 and "close" in df:
            closes = [c for c in (_finite(v) for v in df["close"].tolist()) if c is not None]
            if len(closes) >= 2:
                series[self.symbols[0]] = closes[-HISTORY_DAYS:]

        with self._lock:
            if series:
                self._spark = series
                self._spark_day = date.today().isoformat()
        log.info("sparkline history for %d/%d symbols", len(series), len(self.symbols))


if __name__ == "__main__":
    import time

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    feed = WatchlistFeed()
    feed.start()
    try:
        for _ in range(3):
            time.sleep(12)
            snap = feed.snapshot()
            m = snap["meta"]
            print(f"\nconnected={m['connected']} quoted={m['quoted']}/{m['symbols']} "
                  f"refreshed={m['last_refresh']}")
            for sym, q in sorted(snap["quotes"].items()):
                dc = q["day_change_pct"]
                print(f"  {sym:<9}{(q['price'] or 0):>10.2f} {q['currency']:<4}"
                      f"{(f'{dc:+.2f}%' if dc is not None else '-'):>9}"
                      f"  spark {len(q['spark'])}  {q['name'][:28]}")
    except KeyboardInterrupt:
        pass
    finally:
        feed.stop()
