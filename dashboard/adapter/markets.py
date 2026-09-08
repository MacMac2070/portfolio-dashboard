"""The world board: index levels and trading sessions, market by market.

This is the data behind Market watch, and it answers a different question from
the rest of the dashboard — not "what do I own" but "what is trading right now,
and how is it moving". Exposure is the last column rather than the first.

Two things here are deliberately not what the design file does:

  Sessions use real IANA timezones via `zoneinfo`, not fixed UTC offsets. The
  design hardcodes `tz: -5` for New York, which is wrong for eight months of the
  year; `America/New_York` is currently UTC-4.

  Sparklines are the real 30-day close series. The design synthesises a plausible
  wobble from the 1-month return because it had no data source; we have one.

Coverage is thinner than the design assumes. Probed 1 Aug 2026: KOSDAQ, TOPIX,
HSCEI, TPEx, SET50 and FTSE ST Mid Cap return nothing from the provider under
any ticker variant, and CSI 300 and Thailand's SET were both fifteen days stale.
The board is therefore 8 markets and 11 indices, and six of the eight carry a
benchmark only.
"""
from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import quotes
import resilience
import store

QUOTE_SECONDS = 60.0
HISTORY_DAYS = 30
FIRST_RETRY_SECONDS = 15.0
BREAKER = "yfinance"        # shared with every other module that asks Yahoo
# The sterling crosses the benchmark restates a foreign index with. Yahoo's
# GBPUSD=X is dollars per pound; a rate into GBP is its reciprocal.
FX_SYMBOLS = ("GBPUSD=X", "GBPEUR=X", "GBPHKD=X", "GBPSGD=X", "GBPJPY=X", "GBPKRW=X")
# A close older than this is reported with its date and no day-change rather
# than being passed off as today's figure.
STALE_DAYS = 3

log = logging.getLogger("markets")


@dataclass(frozen=True)
class Index:
    symbol: str         # yfinance ticker
    name: str


@dataclass(frozen=True)
class Market:
    code: str           # two-letter, drawn on the card chip
    name: str
    city: str
    currency: str
    tz: str             # IANA zone — DST handled, unlike a fixed offset
    open_min: int       # local minutes from midnight
    close_min: int
    indices: tuple[Index, ...]

    @property
    def benchmark(self) -> Index:
        return self.indices[0]


MARKETS: tuple[Market, ...] = (
    Market("US", "United States", "New York", "USD", "America/New_York", 9 * 60 + 30, 16 * 60, (
        Index("^GSPC", "S&P 500"),
        Index("^NDX", "Nasdaq 100"),
        Index("^DJI", "Dow Jones Industrial"),
    )),
    Market("KR", "South Korea", "Seoul", "KRW", "Asia/Seoul", 9 * 60, 15 * 60 + 30, (
        Index("^KS11", "KOSPI"),
    )),
    Market("JP", "Japan", "Tokyo", "JPY", "Asia/Tokyo", 9 * 60, 15 * 60, (
        Index("^N225", "Nikkei 225"),
    )),
    Market("CN", "China", "Shanghai", "CNY", "Asia/Shanghai", 9 * 60 + 30, 15 * 60, (
        Index("000001.SS", "SSE Composite"),
    )),
    Market("TW", "Taiwan", "Taipei", "TWD", "Asia/Taipei", 9 * 60, 13 * 60 + 30, (
        Index("^TWII", "TAIEX"),
    )),
    Market("HK", "Hong Kong", "Hong Kong", "HKD", "Asia/Hong_Kong", 9 * 60 + 30, 16 * 60, (
        Index("^HSI", "Hang Seng"),
    )),
    Market("SG", "Singapore", "Singapore", "SGD", "Asia/Singapore", 9 * 60, 17 * 60, (
        Index("^STI", "Straits Times"),
    )),
    Market("GB", "United Kingdom", "London", "GBP", "Europe/London", 8 * 60, 16 * 60 + 30, (
        Index("^FTSE", "FTSE 100"),
        Index("^FTMC", "FTSE 250"),
    )),
)

# regions.py already resolves a holding to where its exposure actually is —
# XDJP to Japan though it lists in London, HY9H to Korea though it quotes in
# Frankfurt. That is exactly the mapping this page needs, so it is reused rather
# than restated.
REGION_TO_MARKET = {
    "United States": "US",
    "South Korea": "KR",
    "Japan": "JP",
    "Hong Kong / China": "HK",
    "Singapore": "SG",
    "United Kingdom": "GB",
}

ALL_SYMBOLS = [i.symbol for m in MARKETS for i in m.indices]


def _finite(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def session_of(market: Market, now_utc: datetime | None = None) -> dict:
    """Open/closed plus the local clock and the time until the state flips.

    Weekends count as closed, and the countdown then runs to Monday's open
    rather than to a session that will not happen.
    """
    now = (now_utc or datetime.now(timezone.utc)).astimezone(ZoneInfo(market.tz))
    minutes = now.hour * 60 + now.minute
    weekday = now.weekday() < 5

    is_open = weekday and market.open_min <= minutes < market.close_min

    if is_open:
        until = market.close_min - minutes
    else:
        # Minutes to the next open, skipping Saturday and Sunday.
        days = 0
        if weekday and minutes < market.open_min:
            delta = market.open_min - minutes
        else:
            days = 1
            while (now + timedelta(days=days)).weekday() >= 5:
                days += 1
            delta = (1440 - minutes) + (days - 1) * 1440 + market.open_min
        until = delta

    return {
        "is_open": is_open,
        "local": now.strftime("%H:%M"),
        "until_min": int(until),
        "date": now.date().isoformat(),
    }


def _fmt_until(minutes: int) -> str:
    if minutes >= 1440:
        return f"{minutes // 1440}d {(minutes % 1440) // 60}h"
    if minutes >= 60:
        return f"{minutes // 60}h {minutes % 60:02d}m"
    return f"{minutes}m"


class MarketFeed:
    """Index quotes and history, on the same shape as WatchlistFeed.

    Separate thread, separate endpoint, imports nothing from feed.py — a failure
    here can never touch the IB Gateway connection or the figures for what you
    actually hold.
    """

    def __init__(self, poll_seconds: float = QUOTE_SECONDS, gate=None,
                 gate_timeout: float = 90.0):
        self.poll_seconds = poll_seconds
        self.symbols = ALL_SYMBOLS
        self._gate = gate
        self._gate_timeout = gate_timeout

        self._lock = threading.Lock()
        self._series: dict[str, dict] = {}   # symbol -> derived figures
        # symbol -> [(YYYY-MM-DD, close), ...] oldest first, the full 420-day
        # window this feed already pulls. The board itself only needs the last
        # 30 closes, but the Overview benchmark line needs the whole series and
        # there is no reason to fetch it twice — see adapter/benchmark.py.
        self._history: dict[str, list[tuple[str, float]]] = {}
        self._fx_day: str | None = None
        self._series_day: str | None = None
        self._last_refresh: str | None = None
        self._error: str | None = None

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------- called from the HTTP thread ----------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="markets", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=8)

    def history(self, symbol: str) -> list[tuple[str, float]]:
        """The full daily close series for one index, oldest first.

        From the last refresh when there has been one, else straight from the
        quote store — so the benchmark line draws before the feed has warmed
        and through an outage. Copied under the lock because the worker thread
        replaces the dict wholesale on every poll.
        """
        with self._lock:
            rows = list(self._history.get(symbol, ()))
        if rows:
            return rows
        return quotes.series(symbol, start=(date.today() - timedelta(days=420)).isoformat())

    @staticmethod
    def fx_series(currency: str) -> list[tuple[str, float]]:
        """(date, rate into GBP) for a currency, oldest first, or [].

        The quote store's Yahoo cross first; the Flex ConversionRates store
        as the fallback for a day the cross lacks.
        """
        if not currency or currency == "GBP":
            return []
        rows = {d: 1.0 / c for d, c in quotes.series(f"GBP{currency}") if c}
        for (day, ccy), rate in store.fx_history().items():
            if ccy == currency and day not in rows and rate:
                rows[day] = rate
        return sorted(rows.items())

    @staticmethod
    def catalogue() -> list[dict]:
        """Every index on the board, flattened, for a benchmark picker.

        Ordered market by market as MARKETS declares them, benchmark first
        within each — the same order the board itself renders, so a person
        meets the indices in one consistent sequence.
        """
        return [
            {"symbol": index.symbol, "name": index.name,
             "market": market.code, "market_name": market.name,
             "currency": market.currency}
            for market in MARKETS for index in market.indices
        ]

    def snapshot(self, exposure: dict[str, float] | None = None,
                 nav: float = 0.0) -> dict:
        """The board. Never raises, never blocks.

        `exposure` is market code -> GBP, supplied by the caller from the live IB
        feed; this module never talks to a broker. Passing None means the caller
        does not yet know — which is not the same as knowing you hold nothing,
        and the two must not render alike.
        """
        known = exposure is not None
        exposure = exposure or {}
        with self._lock:
            series = dict(self._series)
            meta = {
                "source": "openbb-yfinance",
                "connected": bool(series) and self._error is None,
                "last_refresh": self._last_refresh,
                "indices": len(self.symbols),
                "quoted": len(series),
                "error": self._error,
                "breaker": resilience.get(BREAKER).snapshot(),
                "served_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }

        max_expo = max(exposure.values()) if exposure else 0.0
        cards = []
        for market in MARKETS:
            sess = session_of(market)
            rows = []
            for index in market.indices:
                data = series.get(index.symbol)
                rows.append({
                    "symbol": index.symbol,
                    "name": index.name,
                    **(data or {"level": None, "day_pct": None, "month_pct": None,
                                "year_pct": None, "spark": [], "as_of": None,
                                "stale": True}),
                })
            held = exposure.get(market.code, 0.0)
            cards.append({
                "code": market.code,
                "name": market.name,
                "city": market.city,
                "currency": market.currency,
                # tz/open/close travel with the card so the browser can tick the
                # clocks between polls. Both sides read the same IANA database
                # and the same two integers, so they cannot disagree — see the
                # note in js/marketwatch.js.
                "tz": market.tz,
                "open_min": market.open_min,
                "close_min": market.close_min,
                "session": {**sess, "until_text": _fmt_until(sess["until_min"])},
                "benchmark": rows[0],
                "others": rows[1:],
                "exposure_known": known,
                "exposure_gbp": held if known else None,
                "exposure_pct": (held / nav * 100.0) if nav and held else None,
                "exposure_bar": (held / max_expo * 100.0) if max_expo and held else 0.0,
            })

        open_names = [c["name"] for c in cards if c["session"]["is_open"]]
        return {
            "meta": meta,
            "markets": cards,
            "open_now": open_names,
            "trading_text": (" · ".join(open_names) + " trading now") if open_names
                            else "All markets closed",
        }

    # ---------- worker ----------

    def _await_gate(self) -> None:
        """Hold the first openbb call until the caller says it is safe.

        `from openbb import obb` is several seconds of CPU and the GIL blocks the
        IB feed's event loop while it runs — see WatchlistFeed._await_gate for
        the incident this prevents.
        """
        if not self._gate:
            return
        waited = 0.0
        while waited < self._gate_timeout and not self._stop.is_set():
            try:
                if self._gate():
                    return
            except Exception:
                return
            self._stop.wait(1.0)
            waited += 1.0

    def _run(self) -> None:
        self._await_gate()
        breaker = resilience.get(BREAKER)
        while not self._stop.is_set():
            if not breaker.allow():
            # Yahoo known to be down: keep the last good values, say so in meta,
            # and look again when the breaker's window lapses rather than
            # asking a dead provider every fifteen seconds for four days.
                with self._lock:
                    self._error = breaker.reason()
                self._sleep(min(breaker.retry_after() + 1.0, self.poll_seconds))
                continue
            ok = self._refresh()
            if ok:
                breaker.record_success()
            else:
                breaker.record_failure(self._error)
            self._sleep(self.poll_seconds if ok
                        else max(FIRST_RETRY_SECONDS, breaker.retry_after()))

    def _sleep(self, seconds: float) -> None:
        waited = 0.0
        while waited < seconds and not self._stop.is_set():
            self._stop.wait(min(0.5, seconds - waited))
            waited += 0.5

    def _refresh(self) -> bool:
        """One batched history call gives level, all three returns and the line.

        Everything is derived from the daily close series rather than the quote
        endpoint: indices quote thinly, and every market is shut at the weekend,
        so `last_price` is routinely absent while the series is complete. This is
        the same fallback already proven for VUSA on the watchlist.

        The series comes from the quote store, not from the call. The call
        fetches only what the store lacks — a week's overlap when coverage is
        current, the full window when it is not — and whatever it returns is
        stored first. So a failed call still redraws the board from stored
        closes, each marked with its own date, and eleven indices × 420 days
        every minute became eleven × seven.
        """
        today = date.today()
        start = quotes.plan_fetch_all(self.symbols, today)
        fetched = False
        try:
            from openbb import obb
            obb.user.preferences.output_type = "dataframe"
            frame = obb.index.price.historical(
                symbol=",".join(self.symbols), provider="yfinance",
                start_date=start, interval="1d").reset_index()
            if "symbol" not in frame.columns:
                log.warning("unexpected history shape; no symbol column")
            else:
                for symbol, sub in frame.groupby("symbol"):
                    quotes.upsert(str(symbol), zip((str(d)[:10] for d in sub["date"]), sub["close"]),
                                  source="yfinance")
                fetched = True
        except Exception as exc:
            log.warning("index history failed: %s", exc)
            with self._lock:
                self._error = str(exc)

        if fetched and self._fx_day != today.isoformat():
            self._refresh_fx(today)

        derived, history = self._derive(today)
        with self._lock:
            if derived:
                self._series = derived
                self._history = history
                if fetched:
                    self._last_refresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    self._error = None

        missing = [s for s in self.symbols if s not in derived]
        if missing:
            log.info("no series for: %s", ", ".join(missing))
        stale_now = [s for s, d in derived.items() if d["stale"]]
        if stale_now:
            log.info("stale (>%dd): %s", STALE_DAYS, ", ".join(stale_now))
        return fetched and bool(derived)

    def _derive(self, today: date) -> tuple[dict[str, dict], dict[str, list[tuple[str, float]]]]:
        """Level, the three returns and the sparkline for every index, from
        whatever the store holds for the last 420 days."""
        year_start = f"{today.year}-01-01"
        window_start = (today - timedelta(days=420)).isoformat()
        derived: dict[str, dict] = {}
        history: dict[str, list[tuple[str, float]]] = {}

        for symbol in self.symbols:
            rows = [(d, c) for d, c in quotes.series(symbol, start=window_start) if c is not None]
            if len(rows) < 2:
                continue

            dates = [d for d, _ in rows]
            closes = [c for _, c in rows]
            last, prev = closes[-1], closes[-2]
            as_of = dates[-1]
            stale = (today - date.fromisoformat(as_of)).days > STALE_DAYS
            # A repeated close from before today is the provider re-serving the
            # last print, not a session that moved 0.00% — an index never lands
            # exactly flat. No day figure; the card says "as of" instead.
            same_close = last == prev and date.fromisoformat(as_of) < today

            # A calendar month back, not 23 rows back — 23 trading days
            # drifts with holidays and recording gaps, and it is labelled
            # month_pct.
            month_start = (today - timedelta(days=30)).isoformat()
            month_ref = next((c for c, d in zip(closes, dates) if d >= month_start), closes[0])
            year_ref = next((c for c, d in zip(closes, dates) if d >= year_start), closes[0])

            derived[symbol] = {
                "level": last,
                # A stale close has no "today" — say so rather than printing the
                # move from whenever it last traded as if it were this session.
                "day_pct": None if (stale or same_close) else (last / prev - 1.0) * 100.0,
                "month_pct": (last / month_ref - 1.0) * 100.0 if month_ref else None,
                "year_pct": (last / year_ref - 1.0) * 100.0 if year_ref else None,
                "spark": closes[-HISTORY_DAYS:],
                "as_of": as_of,
                "stale": stale,
            }
            history[symbol] = rows
        return derived, history

    def _refresh_fx(self, today: date) -> None:
        """The sterling crosses, once a day, into the same store.

        They restate a foreign index in pounds for the benchmark line and the
        beta/alpha block — the return a sterling investor would have had, not
        the one a local investor did. Same provider, same breaker, same
        planning rule as the indices.
        """
        start = quotes.plan_fetch_all(FX_SYMBOLS, today)
        try:
            from openbb import obb
            obb.user.preferences.output_type = "dataframe"
            frame = obb.currency.price.historical(
                symbol=",".join(FX_SYMBOLS), provider="yfinance",
                start_date=start, interval="1d").reset_index()
        except Exception as exc:
            log.warning("fx history failed: %s", exc)
            return
        if "symbol" in frame.columns:
            for symbol, sub in frame.groupby("symbol"):
                quotes.upsert(str(symbol).replace("=X", ""),
                              zip((str(d)[:10] for d in sub["date"]), sub["close"]), source="yfinance")
        elif len(FX_SYMBOLS) == 1 and "close" in frame:
            quotes.upsert(FX_SYMBOLS[0].replace("=X", ""),
                          zip((str(d)[:10] for d in frame["date"]), frame["close"]), source="yfinance")
        self._fx_day = today.isoformat()


if __name__ == "__main__":
    import time

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    print(f"{len(MARKETS)} markets, {len(ALL_SYMBOLS)} indices\n")
    for m in MARKETS:
        s = session_of(m)
        state = "OPEN " if s["is_open"] else "shut "
        verb = "closes in" if s["is_open"] else "opens in"
        print(f"  {m.code}  {state} {s['local']}  {verb} {_fmt_until(s['until_min']):<8}"
              f"{m.name}")

    feed = MarketFeed()
    feed.start()
    time.sleep(25)
    snap = feed.snapshot()
    print(f"\n{snap['trading_text']}")
    print(f"quoted {snap['meta']['quoted']}/{snap['meta']['indices']}\n")
    for c in snap["markets"]:
        b = c["benchmark"]
        day = f"{b['day_pct']:+.2f}%" if b["day_pct"] is not None else "  —   "
        lvl = f"{b['level']:,.2f}" if b["level"] else "—"
        print(f"  {c['code']}  {b['name']:<22}{lvl:>13}{day:>9}  "
              f"spark {len(b['spark']):>2}  {'STALE ' + (b['as_of'] or '') if b['stale'] else ''}")
        for o in c["others"]:
            olvl = f"{o['level']:,.2f}" if o["level"] else "—"
            oday = f"{o['day_pct']:+.2f}%" if o["day_pct"] is not None else "  —   "
            print(f"       {o['name']:<22}{olvl:>13}{oday:>9}")
    feed.stop()
