"""Per-instrument detail, for the stock page. Serves /api/instrument/<key>.

Fourth independent source, after the IB feed, the watchlist and the world board.
Unlike those three it is **click-driven**, not a poller, and that shapes almost
every decision here.

Why the warmup thread exists
----------------------------
`from openbb import obb` costs several seconds of pure CPU building its extension
registry, and the GIL means that blocks every other thread in the process. On
29 Jul that starved the IB feed's asyncio loop and its startup requests timed
out, leaving the feed connected with no positions — see `watchlist.py._await_gate`.

The watchlist and markets feeds solve this by waiting before their *first poll*.
That is not enough for a click-driven endpoint: the first click could land during
IB Gateway startup and pay the import cost on an HTTP worker thread, reproducing
the incident exactly. So the import happens on our own warmup thread behind the
same gate, and until it finishes every request returns `state: "warming"` in
microseconds. By the time the gate opens the watchlist has usually already paid
the import, so warmup is nearly free.

Provider order
--------------
yfinance first, the default tier second. That inverts openbb's own precedence on
purpose. An FMP key is configured in ~/.openbb_platform/user_settings.json, so
`provider=None` resolves to cboe/finviz/fmp rather than failing — and for US
names cboe *succeeds* with worse data: a day-stale daily history, a different
prev_close, and **no `currency` field at all**, which is the field that detects
GBp pence pricing (see units.py). Serving that would mean the same stock showing
one prev_close on Overview and a different one here. Overview's numbers win.

The default tier is still attempted whenever yfinance returns nothing, and every
attempt lands in the coverage map so what actually served a field is visible in
the payload rather than guessed at.

The yfinance-direct hatch
-------------------------
Four fields the design needs have no openbb path on this install:

    EPS TTM              YFinanceKeyMetricsFetcher's extract list omits
                         trailingEps, so eps_ttm is always None
    next earnings date   equity.calendar.earnings silently ignores `symbol` —
                         it is a date-window endpoint, not a per-symbol one
    ex-dividend date     equity.fundamental.dividends returns only past dates
    buy/hold/sell counts no installed provider has a model for these at all

`_yf_extras` reads them straight from the yfinance library, which openbb's own
fetchers already import. It is tagged `provider: "yfinance-direct"` in the
coverage map so it is never mistaken for an openbb result. Without it the
design's Recommendations bar is permanently empty on all 35 names.

Forward FY revenue/EPS estimates have no source at all here — fmp's free tier
402s and seeking_alpha returns nothing — so they are null and the page shows an
em-dash. That is deliberate: this dashboard is wired to a real account and a
fabricated estimate is worse than a blank.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

import directory
import markets
import resilience
import units
import universe

log = logging.getLogger("instrument")

# --------------------------------------------------------------------------
# Tuning
# --------------------------------------------------------------------------
# One table so the cadences are comparable at a glance. quote matches
# WatchlistFeed.QUOTE_SECONDS so the two never disagree by more than a minute.
TTL: dict[str, float] = {
    "quote":     60.0,
    "hist1m":    300.0,      # minute bars; 5 min is invisible on a 1D chart
    "hist1d":    6 * 3600.0,  # daily closes do not move intraday
    "bench1m":   300.0,
    "bench1d":   6 * 3600.0,
    "news":      30 * 60.0,
    "metrics":   6 * 3600.0,
    "consensus": 6 * 3600.0,
    "ratings":   6 * 3600.0,
    "dividends": 6 * 3600.0,
    "extras":    6 * 3600.0,
    "profile":   24 * 3600.0,
}

MAX_ENTRIES = 1024        # 35 symbols x ~12 groups ~= 420; this is a backstop
FOLLOWER_WAIT = 6.0       # how long a second caller waits on the leader's fetch
NEG_TTL = 60.0            # an empty answer is remembered this long, not the group's TTL
BREAKER = "yfinance"      # shared with every other module that asks Yahoo
REQUEST_BUDGET = 8.0      # never hold an HTTP worker longer than this
GATE_TIMEOUT = 90.0
MAX_POINTS = 400          # MAX on INTC is 11,688 raw daily bars

RANGES = ("1D", "5D", "1M", "6M", "YTD", "1Y", "5Y", "MAX")
INTRADAY_RANGES = ("1D", "5D")

# markets.REGION_TO_MARKET covers the six regions a *holding* can have. The
# venue axis in universe.py adds two watchlist-only regions, so they are mapped
# here rather than in markets.py, which stays untouched.
BENCHMARK_MARKETS = {**markets.REGION_TO_MARKET, "Taiwan": "TW"}
_MARKET_BY_CODE = {m.code: m for m in markets.MARKETS}
FALLBACK_MARKET = "US"


def _finite(value) -> float | None:
    """Fourth copy in this package, deliberately — see marketdata.py:59."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------
@dataclass
class _Entry:
    value: Any
    stored: float
    ttl: float
    provider: str
    errors: list = field(default_factory=list)

    def fresh(self) -> bool:
        return (time.monotonic() - self.stored) < self.ttl

    @property
    def age(self) -> int:
        return int(time.monotonic() - self.stored)


class InstrumentService:
    """Cached per-instrument detail. Never raises, never blocks the IB feed."""

    def __init__(self, gate: Callable[[], bool] | None = None,
                 gate_timeout: float = GATE_TIMEOUT):
        self._gate = gate
        self._gate_timeout = gate_timeout
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None

        self._lock = threading.RLock()
        self._cache: dict[str, _Entry] = {}
        self._inflight: dict[str, threading.Event] = {}
        # Two whole-request fan-outs at once. Rapid clicks across five symbols
        # must not put sixty calls in flight and starve the feed's event loop.
        self._slots = threading.BoundedSemaphore(2)
        self._warm_error: str | None = None

    # ---------------- lifecycle ----------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._warmup, name="instrument-warmup",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)
        if self._thread:
            self._thread.join(timeout=8)

    def _await_gate(self) -> None:
        """Identical predicate and timeout to WatchlistFeed._await_gate."""
        if not self._gate:
            return
        deadline = time.monotonic() + self._gate_timeout
        while not self._stop.is_set() and time.monotonic() < deadline:
            try:
                if self._gate():
                    return
            except Exception:
                return  # a broken gate must not block the endpoint forever
            self._stop.wait(1.0)
        log.warning("instrument: gate timed out after %.0fs, warming anyway",
                    self._gate_timeout)

    def _warmup(self) -> None:
        self._await_gate()
        if self._stop.is_set():
            return
        try:
            t0 = time.monotonic()
            self._obb()
            self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="instr")
            self._ready.set()
            log.info("instrument: openbb ready in %.1fs", time.monotonic() - t0)
        except Exception as exc:
            self._warm_error = str(exc)
            log.error("instrument: openbb import failed: %s", exc)

    @staticmethod
    def _obb():
        from openbb import obb
        obb.user.preferences.output_type = "dataframe"
        return obb

    # ---------------- cache primitives ----------------

    def _evict(self) -> None:
        if len(self._cache) <= MAX_ENTRIES:
            return
        for k in [k for k, e in self._cache.items() if not e.fresh()]:
            self._cache.pop(k, None)
        if len(self._cache) > MAX_ENTRIES:
            oldest = sorted(self._cache.items(), key=lambda kv: kv[1].stored)
            for k, _ in oldest[: len(self._cache) - MAX_ENTRIES]:
                self._cache.pop(k, None)

    def _get(self, key: str, ttl: float, producer: Callable[[], tuple]) -> _Entry | None:
        """Single-flight read-through. Two clicks on one symbol issue one fetch.

        An expired entry is kept rather than dropped: if the refresh fails, the
        stale value is returned instead of blanking a working page. Same rule
        watchlist.py already follows for its last-good quotes.
        """
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
            value, provider, errors = producer()
            # An empty answer is remembered briefly — not for the group's full
            # TTL, and not for no time at all: the four ETFs Yahoo files no
            # fundamentals for were re-asked on every single click.
            empty = value is None or (hasattr(value, "__len__") and len(value) == 0)
            entry = _Entry(value, time.monotonic(), NEG_TTL if empty else ttl, provider, errors)
            with self._lock:
                self._cache[key] = entry
                self._evict()
            return entry
        except Exception as exc:                      # producer bug, not a provider error
            log.exception("instrument: producer failed for %s", key)
            with self._lock:
                stale = self._cache.get(key)
            if stale:
                stale.errors = list(stale.errors) + [("producer", str(exc)[:160])]
            return stale
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            waiter.set()

    # ---------------- provider fallback ----------------

    def _attempt(self, call: Callable, required: tuple[str, ...] = ()) -> tuple:
        """yfinance first, then the default tier. Returns (records, tag, errors).

        `required` is the minimum field set that makes a response usable. It is
        how cboe self-rejects on `quote`: it never returns `currency`, and
        without that the GBp divisor cannot be resolved.
        """
        errors: list = []
        breaker = resilience.get(BREAKER)
        for tag, kwargs in (("yfinance", {"provider": "yfinance"}), ("default", {})):
            if tag == "yfinance" and not breaker.allow():
                errors.append((tag, breaker.reason()))
                continue
            try:
                df = call(**kwargs)
            except Exception as exc:
                errors.append((tag, f"{type(exc).__name__}: {exc}"[:160]))
                if tag == "yfinance":
                    breaker.record_failure(exc)
                continue
            if tag == "yfinance":
                breaker.record_success()
            try:
                df = df.reset_index()
            except Exception:
                pass
            if df is None or not len(df):
                errors.append((tag, "empty"))
                continue
            try:
                records = df.to_dict("records")
            except Exception as exc:
                errors.append((tag, f"not a frame: {exc}"[:160]))
                continue
            missing = [f for f in required if records[0].get(f) is None]
            if missing:
                errors.append((tag, f"missing {','.join(missing)}"))
                continue
            return records, tag, errors
        return None, "none", errors

    # ---------------- field groups ----------------

    def _quote(self, symbol: str):
        obb = self._obb()
        # `currency` is required so cboe — which omits it — falls through to
        # yfinance. Without it a GBp name would be shown 100x too high.
        return self._attempt(lambda **kw: obb.equity.price.quote(symbol=symbol, **kw),
                             required=("currency",))

    def _profile(self, symbol: str):
        obb = self._obb()
        return self._attempt(lambda **kw: obb.equity.profile(symbol=symbol, **kw))

    def _metrics(self, symbol: str):
        obb = self._obb()
        return self._attempt(
            lambda **kw: obb.equity.fundamental.metrics(symbol=symbol, **kw),
            required=("market_cap",))

    def _consensus(self, symbol: str):
        obb = self._obb()
        return self._attempt(
            lambda **kw: obb.equity.estimates.consensus(symbol=symbol, **kw),
            required=("target_consensus",))

    def _news(self, symbol: str):
        obb = self._obb()
        # yfinance ignores `limit` and returns 10; sliced client-side below.
        return self._attempt(
            lambda **kw: obb.news.company(symbol=symbol, limit=10, **kw),
            required=("title",))

    def _ratings(self, symbol: str):
        obb = self._obb()
        # finviz only, and it 404s every non-US symbol. Empty is the honest
        # answer for those, not a fabricated trend.
        return self._attempt(
            lambda **kw: obb.equity.estimates.price_target(symbol=symbol, **kw))

    def _dividends(self, symbol: str):
        obb = self._obb()
        return self._attempt(
            lambda **kw: obb.equity.fundamental.dividends(symbol=symbol, **kw),
            required=("ex_dividend_date",))

    def _history(self, symbol: str, intraday: bool, index: bool = False):
        obb = self._obb()
        ns = obb.index.price.historical if index else obb.equity.price.historical

        if intraday:
            # interval="1m" forces period="5d" inside the yfinance extension, so
            # one call covers both 1D and 5D. The 5m retry is for symbols whose
            # minute data Yahoo rate-limits or does not keep.
            recs, tag, errs = self._attempt(
                lambda **kw: ns(symbol=symbol, interval="1m", extended_hours=True, **kw),
                required=("close",))
            if recs:
                return recs, tag, errs
            start = (date.today() - timedelta(days=58)).isoformat()
            recs2, tag2, errs2 = self._attempt(
                lambda **kw: ns(symbol=symbol, interval="5m", start_date=start, **kw),
                required=("close",))
            return recs2, tag2, errs + errs2

        # One call back to listing. INTC returns 11,688 rows to 1980; every
        # daily range is a slice of this, so MAX costs no extra request.
        return self._attempt(
            lambda **kw: ns(symbol=symbol, interval="1d",
                            start_date="1970-01-01", **kw),
            required=("close",))

    @staticmethod
    def _yf_extras(symbol: str):
        """The approved non-openbb hatch. See the module docstring.

        One yfinance round-trip for the four fields openbb cannot serve, plus the
        post-market price (which openbb's quote model has no field for). Any
        failure yields all-None rather than a partial guess.
        """
        blank = {"eps_ttm": None, "next_earnings": None, "ex_dividend": None,
                 "ratings": None, "after_hours": None, "shares_outstanding": None}
        breaker = resilience.get(BREAKER)
        if not breaker.allow():
            return blank, "none", [("yfinance-direct", breaker.reason())]
        try:
            from yfinance import Ticker
            t = Ticker(symbol)
            info = t.get_info() or {}
            breaker.record_success()

            out = dict(blank)
            out["eps_ttm"] = _finite(info.get("trailingEps"))
            out["shares_outstanding"] = _finite(info.get("sharesOutstanding"))

            post = _finite(info.get("postMarketPrice"))
            if post is not None:
                out["after_hours"] = {
                    "price": post,
                    "change_pct": _finite(info.get("postMarketChangePercent")),
                }

            try:
                cal = t.calendar or {}
                earnings = cal.get("Earnings Date") or []
                if earnings:
                    first = earnings[0] if isinstance(earnings, (list, tuple)) else earnings
                    out["next_earnings"] = str(first)[:10]
                exdiv = cal.get("Ex-Dividend Date")
                if exdiv:
                    out["ex_dividend"] = str(exdiv)[:10]
            except Exception:
                pass

            try:
                recs = t.recommendations
                if recs is not None and len(recs):
                    rows = recs.to_dict("records")
                    row = next((r for r in rows if str(r.get("period")) == "0m"), rows[0])
                    counts = {k: int(row.get(k) or 0) for k in
                              ("strongBuy", "buy", "hold", "sell", "strongSell")}
                    if sum(counts.values()):
                        out["ratings"] = {
                            "strong_buy": counts["strongBuy"], "buy": counts["buy"],
                            "hold": counts["hold"], "sell": counts["sell"],
                            "strong_sell": counts["strongSell"],
                        }
            except Exception:
                pass

            return out, "yfinance-direct", []
        except Exception as exc:
            breaker.record_failure(exc)
            return blank, "none", [("yfinance-direct", f"{type(exc).__name__}: {exc}"[:160])]

    # ---------------- series shaping ----------------

    @staticmethod
    def _rows(records, intraday: bool) -> list[tuple[str, float]]:
        """(timestamp, close) oldest-first, non-finite closes dropped."""
        out: list[tuple[str, float]] = []
        for r in records or []:
            close = _finite(r.get("close"))
            if close is None:
                continue
            raw = r.get("date") or r.get("index") or r.get("timestamp")
            if raw is None:
                continue
            stamp = str(raw)
            out.append((stamp if intraday else stamp[:10], close))
        out.sort(key=lambda p: p[0])
        return out

    @staticmethod
    def _downsample(rows: list) -> list:
        n = len(rows)
        if n <= MAX_POINTS:
            return rows
        stride = math.ceil(n / MAX_POINTS)
        out = rows[::stride]
        if out[-1] != rows[-1]:
            out.append(rows[-1])
        return out

    @staticmethod
    def _slice(rows: list[tuple[str, float]], key: str) -> list[tuple[str, float]]:
        if not rows:
            return []
        today = date.today()
        if key == "MAX":
            return rows
        if key == "5D":
            return rows
        if key == "1D":
            last_day = rows[-1][0][:10]
            return [r for r in rows if r[0][:10] == last_day]
        if key == "YTD":
            cut = f"{today.year}-01-01"
        else:
            days = {"1M": 31, "6M": 183, "1Y": 365, "5Y": 1826}[key]
            cut = (today - timedelta(days=days)).isoformat()
        return [r for r in rows if r[0][:10] >= cut] or rows[-2:]

    @staticmethod
    def _rebase(inst: list[tuple[str, float]],
                bench: list[tuple[str, float]]) -> list | None:
        """Benchmark onto the instrument's own axis and first close.

        Venues keep different holidays — LSE and HKEX diverge on Boxing Day and
        Golden Week — so the benchmark is forward-filled onto the instrument's
        dates rather than zipped positionally, which would silently shear the
        two series apart. Leading dates with no benchmark point emit null; they
        are not back-filled, because an invented earlier value would read as a
        real one.
        """
        if not inst or not bench:
            return None
        by_stamp = dict(bench)
        base = None
        carried = None
        out: list = []
        for stamp, _ in inst:
            hit = by_stamp.get(stamp)
            if hit is None and len(stamp) > 10:
                hit = by_stamp.get(stamp[:10])
            if hit is not None:
                carried = hit
                if base is None:
                    base = hit
            if carried is None or base in (None, 0):
                out.append(None)
            else:
                out.append(carried / base)
        if base is None:
            return None
        anchor = inst[0][1]
        return [None if v is None else round(anchor * v, 4) for v in out]

    # ---------------- composition ----------------

    @staticmethod
    def _resolve(key: str) -> universe.Ticker | None:
        """Curated registry first, then the symbol directory.

        This is what lets the stock page open a name you found through search
        but have never tracked. A directory row becomes a synthetic Ticker with
        con_id=None, so every ownership-gated path downstream — quantity, cost,
        P&L, the allocation donut — already excludes it without a single extra
        conditional.
        """
        t = universe.TICKERS.get(key)
        if t is not None:
            return t
        try:
            row = directory.row(key)
        except Exception:
            log.exception("instrument: directory lookup failed for %r", key)
            return None
        if not row:
            return None
        return universe.synthetic(
            row["symbol"], row.get("name") or "",
            exchange=row.get("exchange") or "",
            currency=row.get("currency") or "",
            region=(row.get("region")
                    or directory.EXCHANGE_REGION.get(row.get("exchange") or "", "")))

    def snapshot(self, key: str) -> dict:
        ticker = self._resolve(key)
        if ticker is None:
            return _empty(key, f"unknown ticker {key!r}")

        if not self._ready.is_set():
            return _empty(key, self._warm_error, state="warming", retry_after=2,
                          ticker=ticker)

        if not self._slots.acquire(timeout=1.0):
            return _empty(key, "service busy", state="busy", retry_after=1,
                          ticker=ticker)
        try:
            return self._compose(ticker)
        finally:
            self._slots.release()

    def _compose(self, t: universe.Ticker) -> dict:
        symbol = t.symbol
        bench_symbol, bench_name, bench_market, bench_fallback = _benchmark_for(
            t.region, t.exchange)

        jobs: dict[str, tuple[str, float, Callable]] = {
            "quote":     (f"{symbol}|quote", TTL["quote"], lambda: self._quote(symbol)),
            "profile":   (f"{symbol}|profile", TTL["profile"], lambda: self._profile(symbol)),
            "metrics":   (f"{symbol}|metrics", TTL["metrics"], lambda: self._metrics(symbol)),
            "consensus": (f"{symbol}|consensus", TTL["consensus"], lambda: self._consensus(symbol)),
            "news":      (f"{symbol}|news", TTL["news"], lambda: self._news(symbol)),
            "ratings":   (f"{symbol}|ratings", TTL["ratings"], lambda: self._ratings(symbol)),
            "dividends": (f"{symbol}|dividends", TTL["dividends"], lambda: self._dividends(symbol)),
            "extras":    (f"{symbol}|extras", TTL["extras"], lambda: self._yf_extras(symbol)),
            "hist1d":    (f"{symbol}|hist1d", TTL["hist1d"], lambda: self._history(symbol, False)),
            "hist1m":    (f"{symbol}|hist1m", TTL["hist1m"], lambda: self._history(symbol, True)),
            "bench1d":   (f"{bench_symbol}|bench1d", TTL["bench1d"],
                          lambda: self._history(bench_symbol, False, index=True)),
            "bench1m":   (f"{bench_symbol}|bench1m", TTL["bench1m"],
                          lambda: self._history(bench_symbol, True, index=True)),
        }

        entries: dict[str, _Entry | None] = {}
        pending: list[str] = []
        deadline = time.monotonic() + REQUEST_BUDGET
        futures = {self._pool.submit(self._get, ck, ttl, prod): name
                   for name, (ck, ttl, prod) in jobs.items()}
        try:
            for fut in as_completed(futures, timeout=max(0.1, deadline - time.monotonic())):
                entries[futures[fut]] = fut.result()
        except Exception:
            pass  # TimeoutError -> whatever landed is what we ship
        for name in jobs:
            if name not in entries:
                pending.append(name)
                # The future keeps running and fills the cache, so the client's
                # retry is instant rather than paying the same wait again.
                with self._lock:
                    entries[name] = self._cache.get(jobs[name][0])

        def rec(name: str) -> dict:
            e = entries.get(name)
            if not e or not e.value:
                return {}
            v = e.value
            return v[0] if isinstance(v, list) else v

        def recs(name: str) -> list:
            e = entries.get(name)
            return e.value if e and isinstance(e.value, list) else []

        quote, profile, metrics = rec("quote"), rec("profile"), rec("metrics")
        consensus, extras = rec("consensus"), rec("extras")

        scale = units.scale_for(
            quote.get("currency") or profile.get("currency") or consensus.get("currency"),
            fallback=t.currency)
        quote = units.scale_record(quote, scale)
        consensus = units.scale_record(consensus, scale)

        series = self._series(entries, scale)
        daily = series["daily"]
        last, prev, price_source = _resolve_price(quote, daily)

        payload = {
            "meta": _meta(entries, pending, jobs, self._lock, self._cache),
            "coverage": _coverage(entries, pending),
            "instrument": _identity(t, profile, scale),
            "price": _price(last, prev, price_source, quote, extras, scale,
                            daily[-1][0] if daily else None),
            "key_stats": _key_stats(quote, profile, metrics, extras,
                                    recs("dividends"), prev, daily),
            "profile": _profile_block(profile),
            "news": _news_block(recs("news")),
            "analyst": _analyst(consensus, extras, recs("ratings"), last, scale),
            "ranges": self._ranges(series),
            "benchmark": {"market": bench_market, "symbol": bench_symbol,
                          "name": bench_name, "fallback": bench_fallback,
                          "note": "rebased to the instrument's first close in each range"},
        }

        # Teach the directory what this fetch proved. The currency is the one
        # that matters: search refuses to show a price for a remote result until
        # it knows one, because Yahoo's search returns a bare number and a
        # London name's is in pence. Both queue onto the directory's writer
        # thread and return immediately.
        try:
            if quote.get("currency"):
                directory.learn_currency(symbol, quote["currency"], t.region)
            directory.note_hit(symbol)
        except Exception:
            log.debug("instrument: directory write-back skipped for %s", symbol,
                      exc_info=True)

        return payload

    def _series(self, entries: dict, scale: units.PriceScale) -> dict:
        """Every close series, once, already in the major unit."""
        def rows_for(name: str, intraday: bool):
            e = entries.get(name)
            return self._rows(e.value if e else None, intraday)

        daily = rows_for("hist1d", False)
        minute = rows_for("hist1m", True)
        if scale.divisor != 1.0:
            daily = [(d, v / scale.divisor) for d, v in daily]
            minute = [(d, v / scale.divisor) for d, v in minute]
        # The benchmark is an index level, never a price, so it is never scaled —
        # and it is rebased onto the instrument's own axis anyway.
        return {"daily": daily, "minute": minute,
                "bench_daily": rows_for("bench1d", False),
                "bench_minute": rows_for("bench1m", True)}

    def _ranges(self, series: dict) -> dict:
        daily, minute = series["daily"], series["minute"]
        bench_daily, bench_minute = series["bench_daily"], series["bench_minute"]

        out: dict[str, dict] = {}
        for key in RANGES:
            intraday = key in INTRADAY_RANGES
            src, bsrc = (minute, bench_minute) if intraday else (daily, bench_daily)
            rows = self._slice(src, key)
            if len(rows) < 2:
                out[key] = {"available": False, "points": 0,
                            "interval": "1m" if intraday else "1d",
                            "reason": "no data from provider"}
                continue
            rows = self._downsample(rows)
            bench = self._rebase(rows, self._slice(bsrc, key)) if bsrc else None
            start, end = rows[0][1], rows[-1][1]
            out[key] = {
                "available": True,
                "points": len(rows),
                "interval": "1m" if intraday else "1d",
                "start": round(start, 4), "end": round(end, 4),
                "change": round(end - start, 4),
                "change_pct": round((end / start - 1) * 100, 4) if start else None,
                "first_date": rows[0][0],
                "labels": [r[0] for r in rows],
                "series": [round(r[1], 4) for r in rows],
                "benchmark": bench,
            }
        return out


# --------------------------------------------------------------------------
# Block builders — pure functions over provider records
# --------------------------------------------------------------------------
def _benchmark_for(region: str, exchange: str = "") -> tuple[str, str, str, bool]:
    code = BENCHMARK_MARKETS.get(region)
    fallback = code is None
    market = _MARKET_BY_CODE.get(code or FALLBACK_MARKET) or _MARKET_BY_CODE[FALLBACK_MARKET]
    index = market.benchmark

    # Region picks the market, but within the US the listing venue picks the
    # index: a Nasdaq name reads against the Nasdaq 100, not the S&P 500. The
    # design does exactly this for INTC. markets.MARKETS already carries ^NDX
    # as the US market's second index, so this is a pick, not a new symbol.
    if market.code == "US" and exchange.upper() == "NMS":
        index = next((i for i in market.indices if i.symbol == "^NDX"), index)

    return index.symbol, index.name, market.code, fallback


def _meta(entries, pending, jobs, lock, cache) -> dict:
    ages = {name: e.age for name, e in entries.items() if e}
    stale = sorted(name for name, e in entries.items() if e and not e.fresh())
    served = any(e and e.value for e in entries.values())
    return {
        "source": "openbb",
        "state": "ready",
        "connected": served,
        "served_at": _now(),
        "cache_age_s": ages,
        "stale": stale,
        "pending": sorted(pending),
        "retry_after": 2 if pending else None,
        "error": None if served else "no provider returned data",
    }


def _coverage(entries, pending) -> dict:
    out = {}
    for name, e in entries.items():
        if name in pending and not e:
            out[name] = {"provider": "pending", "errors": []}
            continue
        out[name] = {
            "provider": e.provider if e else "none",
            "errors": [list(x) for x in (e.errors if e else [])],
        }
    return out


def _identity(t: universe.Ticker, profile: dict, scale: units.PriceScale) -> dict:
    return {
        "key": t.key, "symbol": t.symbol, "name": t.name,
        "exchange": t.exchange, "exchange_name": t.exchange_display,
        "currency": t.currency,
        "display_currency": units.display_currency(scale, t.currency),
        "divisor": scale.divisor,
        "region": t.region, "owned": t.owned, "con_id": t.con_id,
        "sector": t.sector, "logo": t.logo_url, "mono": t.mono,
        # The tile colours, so the stock page can paint a name the live universe
        # has never heard of. Same blend to_dict() ships, from one helper.
        **universe.tile_colours(t),
        "tracked": t.key in universe.TICKERS,
        # STK / ETF / GDR. The stock page uses it to disable the Financials
        # pill for a fund, which files no company statements. A heuristic —
        # the financials page's own empty state is the real backstop.
        "kind": ("ETF" if str(t.sector).lower() == "etfs"
                 else "GDR" if " GDR" in f" {t.name}".upper() else "STK"),
        # The design's sub-line: "NasdaqGS · Real-time price · USD · Semiconductors"
        "quote_type": "Delayed price",
    }


def _resolve_price(quote: dict, daily: list) -> tuple:
    """(last, prev_close, source). Falls back to the daily closes.

    The thinly-quoted ETFs — 3115.HK, and VUSA.L / IUCS.L / XDJP.L on the
    watchlist — return a row from `equity.price.quote` with `last_price` empty.
    Their daily history is fine, so the last two closes stand in rather than the
    page showing an em-dash for a price we actually have. Same fallback
    `WatchlistFeed._with_fallback` (watchlist.py:107-133) already applies, and it
    reports `source` so the page can say where the number came from.
    """
    last = _finite(quote.get("last_price"))
    prev = _finite(quote.get("prev_close"))
    if last is not None and prev is not None:
        return last, prev, "quote"
    closes = [v for _, v in daily]
    if last is None and closes:
        last = closes[-1]
        if prev is None and len(closes) >= 2:
            prev = closes[-2]
        return last, prev, "history"
    return last, prev, "quote" if last is not None else "none"


def _price(last, prev, source: str, quote: dict, extras: dict,
           scale: units.PriceScale, as_of: str | None) -> dict:
    change = (last - prev) if (last is not None and prev is not None) else None
    after = extras.get("after_hours")
    if after and scale.divisor != 1.0 and after.get("price") is not None:
        after = {**after, "price": after["price"] / scale.divisor}
    return {
        "last": last, "prev_close": prev,
        "change": change,
        "change_pct": (change / prev * 100) if (change is not None and prev) else None,
        "source": source,
        "as_of": str(quote.get("date") or "")[:10] or as_of,
        "after_hours": after,
    }


def _key_stats(quote, profile, metrics, extras, dividends, prev_close, daily) -> dict:
    def q(name):
        return _finite(quote.get(name))

    # 52-week range is derivable from the daily series when the provider omits
    # it, which the thin ETFs do. One year of trading days ~= 252.
    year_low, year_high = q("year_low"), q("year_high")
    if (year_low is None or year_high is None) and len(daily) >= 2:
        window = [v for _, v in daily[-252:]]
        year_low = year_low if year_low is not None else min(window)
        year_high = year_high if year_high is not None else max(window)

    ex_div = None
    if dividends:
        past = [str(r.get("ex_dividend_date"))[:10] for r in dividends
                if r.get("ex_dividend_date")]
        past = sorted(p for p in past if p <= date.today().isoformat())
        ex_div = past[-1] if past else None
    ex_div = ex_div or extras.get("ex_dividend")

    yield_pct = _finite(metrics.get("dividend_yield"))
    if yield_pct is None:
        yield_pct = _finite(profile.get("dividend_yield"))

    return {
        "previous_close": q("prev_close") if q("prev_close") is not None else prev_close,
        "open": q("open"),
        "bid": q("bid"), "ask": q("ask"),
        "volume": _finite(quote.get("volume")),
        "average_volume": _finite(quote.get("volume_average")),
        "day_low": q("low"), "day_high": q("high"),
        "year_low": year_low, "year_high": year_high,
        "market_cap": _finite(metrics.get("market_cap")) or _finite(profile.get("market_cap")),
        "enterprise_value": _finite(metrics.get("enterprise_value")),
        "pe_trailing": _finite(metrics.get("pe_ratio")),
        "pe_forward": _finite(metrics.get("forward_pe")),
        "eps_ttm": _finite(extras.get("eps_ttm")),
        "beta_5y_monthly": _finite(metrics.get("beta")) or _finite(profile.get("beta")),
        # Already a percent from the provider — do not multiply by 100.
        "dividend_yield_pct": yield_pct,
        "ex_dividend_date": ex_div,
        "next_earnings_date": extras.get("next_earnings"),
        "shares_outstanding": (_finite(profile.get("shares_outstanding"))
                               or _finite(extras.get("shares_outstanding"))),
    }


def _profile_block(profile: dict) -> dict:
    city = profile.get("hq_address_city") or ""
    state = profile.get("hq_state") or ""
    country = profile.get("hq_country") or ""
    where = ", ".join(p for p in (city, state, country) if p)
    return {
        "sector": profile.get("sector") or None,
        "industry": profile.get("industry_category") or profile.get("industry") or None,
        "headquarters": where or None,
        "employees": _finite(profile.get("employees")),
        "website": profile.get("company_url") or profile.get("website") or None,
        "description": profile.get("long_description") or profile.get("short_description") or None,
    }


def _relative(stamp: str) -> str:
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - when
    mins = delta.total_seconds() / 60
    if mins < 60:
        return f"{int(max(1, mins))}m ago"
    if mins < 60 * 24:
        return f"{int(mins // 60)}h ago"
    days = int(mins // (60 * 24))
    if days == 1:
        return "Yesterday"
    if days < 30:
        return f"{days}d ago"
    return f"{days // 30}mo ago"


def _news_block(records: list) -> list:
    out = []
    for r in records[:8]:
        title = r.get("title")
        if not title:
            continue
        published = r.get("date") or r.get("published")
        out.append({
            "title": str(title),
            "source": r.get("source") or r.get("publisher") or None,
            "url": r.get("url") or None,
            "published": str(published) if published else None,
            "relative": _relative(published) if published else "",
        })
    return out


def _analyst(consensus: dict, extras: dict, ratings: list, last,
             scale: units.PriceScale) -> dict:
    mean = _finite(consensus.get("target_consensus"))
    counts = extras.get("ratings")
    broker_count = _finite(consensus.get("number_of_analysts"))
    if broker_count is None and counts:
        broker_count = sum(counts.values())

    mean_rec = _finite(consensus.get("recommendation_mean"))
    label = consensus.get("recommendation")
    if not label and mean_rec is not None:
        label = "buy" if mean_rec < 2.5 else "hold" if mean_rec < 3.5 else "sell"

    trend = []
    cutoff = (date.today() - timedelta(days=90)).isoformat()
    for r in ratings or []:
        when = str(r.get("published_date") or "")[:10]
        if not when or when < cutoff:
            continue
        trend.append({
            "date": when,
            "firm": r.get("analyst_company") or None,
            "action": r.get("status") or None,
            "rating": r.get("rating_change") or None,
            "target": units.price(_finite(r.get("adj_price_target")), scale),
            "target_prior": units.price(_finite(r.get("price_target")), scale),
        })

    return {
        "consensus": str(label).lower() if label else None,
        "recommendation_mean": mean_rec,
        "broker_count": int(broker_count) if broker_count else None,
        "ratings": counts,
        "target": {
            "low": _finite(consensus.get("target_low")),
            "mean": mean,
            "high": _finite(consensus.get("target_high")),
            "median": _finite(consensus.get("target_median")),
            "upside_pct": ((mean - last) / last * 100)
                          if (mean is not None and last) else None,
        },
        "rating_trend_90d": trend,
        # No installed provider serves forward FY estimates. Null, never guessed.
        "estimates": {"revenue_fy": None, "revenue_fy_label": None,
                      "eps_fy": None, "eps_fy_label": None,
                      "revenue_growth_pct": None},
    }


def _empty(key: str, error: str | None, state: str = "ready",
           retry_after: int | None = None,
           ticker: universe.Ticker | None = None) -> dict:
    """The shape the page renders as em-dashes plus an honest banner.

    Always HTTP 200 at the serve.py layer — an error status would give the page
    neither the identity it can still show nor a reason to display.
    """
    ident = None
    if ticker is not None:
        ident = _identity(ticker, {}, units.scale_for(ticker.currency))
    return {
        "meta": {"source": "openbb", "state": state, "connected": False,
                 "served_at": _now(), "cache_age_s": {}, "stale": [], "pending": [],
                 "retry_after": retry_after, "error": error},
        "coverage": {},
        "instrument": ident or {"key": key},
        "price": {}, "key_stats": {}, "profile": {}, "news": [],
        "analyst": {}, "ranges": {}, "benchmark": {},
    }


# --------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    keys = sys.argv[1:] or ["INTC"]

    svc = InstrumentService(gate=None)
    svc.start()
    for _ in range(300):
        if svc._ready.is_set():
            break
        time.sleep(0.1)

    for key in keys:
        t0 = time.monotonic()
        payload = svc.snapshot(key)
        took = time.monotonic() - t0
        ident = payload["instrument"]
        print("\n" + "=" * 78)
        print(f"{key}  {ident.get('symbol')}  {ident.get('exchange_name')}  "
              f"{ident.get('currency')} (divisor {ident.get('divisor')})  "
              f"[{took:.1f}s]")
        print("=" * 78)

        cov = payload.get("coverage", {})
        print(f"{'group':<12} {'provider':<16} first error")
        print("-" * 78)
        for name in sorted(cov):
            c = cov[name]
            err = c["errors"][0][1][:44] if c["errors"] else ""
            print(f"{name:<12} {c['provider']:<16} {err}")

        ks = payload.get("key_stats", {})
        filled = [k for k, v in ks.items() if v is not None]
        print(f"\nkey_stats {len(filled)}/{len(ks)} filled")
        print("  missing:", ", ".join(k for k, v in ks.items() if v is None) or "none")
        pr = payload.get("price", {})
        print(f"  last={pr.get('last')} prev={pr.get('prev_close')} "
              f"after_hours={(pr.get('after_hours') or {}).get('price')}")
        an = payload.get("analyst", {})
        print(f"  analyst: consensus={an.get('consensus')} brokers={an.get('broker_count')} "
              f"ratings={an.get('ratings')} target={(an.get('target') or {}).get('mean')}")
        print(f"  profile: {json.dumps(payload.get('profile', {}))[:120]}")
        print(f"  news: {len(payload.get('news', []))} items")
        rg = payload.get("ranges", {})
        print("  ranges:", "  ".join(
            f"{k}={'x' if not rg.get(k, {}).get('available') else rg[k]['points']}"
            f"{'/b' if rg.get(k, {}).get('benchmark') else ''}"
            for k in RANGES))
    svc.stop()
