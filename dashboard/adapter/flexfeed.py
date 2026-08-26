"""The gateway-free positions feed: Flex EOD holdings, repriced by yfinance.

IB Gateway needs a logged-in session on a running machine; the Flex Web
Service needs a token and an HTTPS call. This feed serves the same snapshot
contract as feed.LiveFeed from the other pair of sources the repo already
has: the EOD Open Positions snapshot (store.POSITIONS_PATH, written by the
daily refresh) and the delayed yfinance quotes every other panel is priced
from. Positions are yesterday's; prices are ~15 minutes old — the meta says
exactly that, and the page renders an EOD state rather than pretending to be
live or sulking as disconnected.

Composition converges on derive.make_position()/aggregate(), the same two
functions the gateway paths use, so the payload cannot drift in shape.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

import derive
import store

log = logging.getLogger(__name__)

POLL_SECONDS = 60
# Sparklines and FX move slowly; quotes carry the cycle.
SLOW_REFRESH_SECONDS = 3600
# A repriced quote this far from the EOD mark is a units or mapping bug, not
# a market move — keep the broker's own figure instead (the same caution the
# live feed applies before trusting a repriced value).
REPRICE_SANITY = 0.5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def compose_payload(*, quotes: dict | None = None, fx: dict | None = None,
                    fx_source: str = "flex", sparks: dict | None = None) -> dict | None:
    """One snapshot payload from the stores, or None without an EOD file.

    Callers may hand in already-fetched quotes/fx/sparks (the feed thread
    caches the slow ones); left as None they are fetched here, which is what
    refresh.py's gateway fallback wants.
    """
    eod = store.read_positions_eod()
    if not eod:
        return None
    rows = eod["positions"]
    con_ids = [r["con_id"] for r in rows]

    import marketdata  # deferred: openbb import is heavy — see serve.py gates
    fallback_fx = {r["currency"]: r["fx_to_base"] for r in rows
                   if r.get("currency") and r.get("fx_to_base")}
    if fx is None:
        fx, fx_source = marketdata.fx_rates(fallback_fx)
    else:
        fx = {**fallback_fx, **fx}
    if quotes is None:
        quotes = marketdata.prior_closes(con_ids)
    if sparks is None:
        sparks = marketdata.price_series(con_ids)

    to_gbp = derive.converter(fx)

    positions = []
    for raw in rows:
        quote = quotes.get(raw["con_id"], {})
        last, prev = quote.get("last"), quote.get("prev")
        mark = raw.get("mark_price")
        value = raw.get("value")
        if value is None and mark is not None:
            value = mark * raw["quantity"]
        if value is None:
            log.warning("position %s carries no mark or value; skipped", raw.get("symbol"))
            continue

        # Reprice off the delayed quote so the page moves between Flex days.
        # prior_closes already normalises pence to pounds, the unit Flex marks
        # share, so this is plain multiplication — guarded, because a mapping
        # slip here is how a position inflates a hundredfold.
        price = mark if mark is not None else (value / raw["quantity"] if raw["quantity"] else 0.0)
        if last is not None and mark and abs(last / mark - 1.0) <= REPRICE_SANITY:
            price = last
            value = last * raw["quantity"]

        cost = raw.get("average_cost")
        unrealised = (value - cost * raw["quantity"]) if cost is not None \
            else (raw.get("unrealized_pnl") or 0.0)

        change_pct, change_source = marketdata.day_change_pct(
            prev_close=prev,
            last_price=last,
            ibkr_price=mark,
            ibkr_daily_pnl=None,
            ibkr_market_value=value,
        )
        positions.append(derive.make_position(
            con_id=raw["con_id"],
            symbol=raw["symbol"],
            currency=raw["currency"],
            exchange=raw.get("exchange", ""),
            quantity=raw["quantity"],
            price=price,
            market_value=value,
            average_cost=cost if cost is not None else 0.0,
            unrealized_pnl=unrealised,
            day_change_pct=change_pct,
            day_change_source=change_source,
            spark=sparks.get(raw["con_id"], []),
            to_gbp=to_gbp,
        ))

    # Cash is inferred, not reported: the Flex NAV for the snapshot's day
    # minus what the positions were worth that day. It only moves when the
    # EOD file does, which is the honest cadence for a figure Flex owns.
    invested = sum(p["value_gbp"] for p in positions)
    asof = eod.get("asof")
    nav_asof = store.nav_on_or_before(asof)
    eod_invested = sum((r.get("value") or 0.0) * (r.get("fx_to_base") or fx.get(r.get("currency"), 0.0))
                      for r in rows)
    cash = (nav_asof - eod_invested) if nav_asof is not None else 0.0

    agg = derive.aggregate(positions, nav=cash + invested, cash=cash,
                           account_daily_pnl=None)
    return {
        "meta": {
            "generated_at": _now(),
            "account_id": next((r.get("account_id") for r in rows if r.get("account_id")), ""),
            "base_currency": "GBP",
            "fx_source": fx_source,
            "daily_pnl_source": agg["daily_pnl_source"],
            "gateway": "not-used",
            "source": "flex-eod",
            "positions_asof": asof,
        },
        "kpis": agg["kpis"],
        "positions": positions,
        "regions": agg["regions"],
        "sectors": agg["sectors"],
        "currencies": agg["currencies"],
        "concentration": agg["concentration"],
        "movers": agg["movers"],
        "fx": fx,
    }


class FlexFeed:
    """LiveFeed's snapshot contract, no gateway: EOD positions × delayed quotes.

    Quotes refresh every POLL_SECONDS; FX and sparklines hourly; the EOD file
    is re-read from disk each cycle so the nightly refresh lands without a
    restart. If the file is missing and Flex is configured, one fetch attempt
    per hour tries to create it.
    """

    def __init__(self, gate=None):
        self.poll_seconds = POLL_SECONDS
        self._gate = gate
        self._lock = threading.Lock()
        self._payload: dict | None = None
        self._last_refresh: str | None = None
        self._error: str | None = None
        self._slow_at = 0.0
        self._fx: dict | None = None
        self._fx_source = "flex"
        self._sparks: dict | None = None
        self._fetch_attempt_at = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="flex-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=6)

    def snapshot(self) -> dict:
        """Same contract as LiveFeed.snapshot(): never raises, never blocks."""
        with self._lock:
            payload = self._payload
            meta = {
                "source": "flex-eod",
                "connected": bool(payload) and self._error is None,
                "last_refresh": self._last_refresh,
                "market_data": "delayed",
                "poll_seconds": self.poll_seconds,
                "error": self._error,
                "served_at": _now(),
            }
        if payload is None:
            return {"meta": {**meta, "warming_up": True}, "kpis": {}, "positions": [],
                    "regions": [], "currencies": [], "concentration": {},
                    "movers": {"gainers": [], "losers": []}}
        return {**payload, "meta": {**payload.get("meta", {}), **meta}}

    # ---------- feed thread ----------

    def _run(self) -> None:
        # Same courtesy the watchlist pays: wait for the gateway feed's first
        # compose (or a timeout) before openbb's heavy import, so a live feed
        # that IS starting doesn't get starved by it.
        deadline = time.monotonic() + 90
        while self._gate and not self._gate() and time.monotonic() < deadline:
            if self._stop.wait(2):
                return
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._cycle()
            except Exception as exc:
                log.exception("flex feed cycle failed")
                with self._lock:
                    self._error = str(exc)
            wait = max(5.0, self.poll_seconds - (time.monotonic() - started))
            if self._stop.wait(wait):
                return

    def _ensure_eod(self) -> bool:
        """Have a positions file, self-healing a stale one.

        The nightly job is the usual writer; this covers the machine that
        slept through it. 26h means a healthy nightly cadence never triggers
        a fetch here — only a missed run does — so the feed still spends at
        most ~one Flex call a day. A stale file keeps serving while a
        re-fetch fails: old positions beat none, and meta.positions_asof
        already tells the page how old they are.
        """
        have = store.read_positions_eod() is not None
        if have and not store.positions_eod_stale(hours=26):
            return True
        if time.monotonic() - self._fetch_attempt_at < 3600:
            return have
        self._fetch_attempt_at = time.monotonic()
        try:
            import flex
            rows = flex.fetch_positions()
            if rows:
                store.write_positions_eod(rows)
                log.info("flex feed: fetched %d EOD positions", len(rows))
                return True
        except Exception as exc:
            log.warning("flex feed: EOD fetch failed (%s); will retry in an hour", exc)
        return have

    def _cycle(self) -> None:
        if not self._ensure_eod():
            with self._lock:
                self._error = "no EOD positions snapshot yet"
            return

        import marketdata
        eod = store.read_positions_eod()
        con_ids = [r["con_id"] for r in eod["positions"]]

        if self._fx is None or time.monotonic() - self._slow_at >= SLOW_REFRESH_SECONDS:
            fallback = {r["currency"]: r["fx_to_base"] for r in eod["positions"]
                        if r.get("currency") and r.get("fx_to_base")}
            self._fx, self._fx_source = marketdata.fx_rates(fallback)
            self._sparks = marketdata.price_series(con_ids)
            self._slow_at = time.monotonic()

        quotes = marketdata.prior_closes(con_ids)
        payload = compose_payload(quotes=quotes, fx=self._fx,
                                  fx_source=self._fx_source, sparks=self._sparks)
        with self._lock:
            self._payload = payload
            self._last_refresh = _now()
            self._error = None if payload else "EOD snapshot unreadable"


class FailoverFeed:
    """Serve the gateway feed while it is live; the Flex EOD feed otherwise.

    Both run; this only chooses whose snapshot answers. The gateway winning
    whenever it is connected keeps today's behaviour bit-identical for anyone
    who still runs the Gateway — the Flex path is the floor, not a downgrade.
    """

    def __init__(self, live, fallback):
        self.live = live
        self.fallback = fallback

    def start(self) -> None:
        if self.live:
            self.live.start()
        if self.fallback:
            self.fallback.start()

    def stop(self) -> None:
        if self.live:
            self.live.stop()
        if self.fallback:
            self.fallback.stop()

    def snapshot(self) -> dict:
        live = self.live.snapshot() if self.live else None
        if live and live["meta"].get("connected"):
            return live
        fallback = self.fallback.snapshot() if self.fallback else None
        if fallback and fallback.get("positions"):
            return fallback
        return live or fallback or {
            "meta": {"connected": False, "last_refresh": None, "served_at": _now()},
            "kpis": {}, "positions": [], "regions": [], "currencies": [],
            "concentration": {}, "movers": {"gainers": [], "losers": []},
        }
