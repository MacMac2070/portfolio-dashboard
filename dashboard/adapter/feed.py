"""A persistent IB Gateway connection, held open and recomposed every few seconds.

The snapshot path (build.py) connects, fetches and disconnects. This does the
opposite: one connection, opened once and kept alive, with the latest values
always available in memory for the HTTP layer to serve.

Threading model — the part that is easy to get wrong:

  ib_async is asyncio. An `IB()` binds to whichever event loop is running when
  it is created, so it is constructed *inside* the feed thread, on a loop this
  module owns. `connectAsync` is used rather than the sync `connect`, because
  the sync API drives its own loop and would fight the server's.

  Everything the HTTP thread touches goes through `_lock`. It never touches the
  `IB` object at all — only the composed dict.

Where the numbers come from:

  ib_async 2.1.0 subscribes account updates at connect time
  (StartupFetch.ACCOUNT_UPDATES), so `portfolio()` and `accountValues()` are
  kept current by TWS pushes without us polling IBKR. Those pushes arrive
  roughly every three minutes, which is why prices come from market data
  instead:

  `reqMarketDataType(3)` selects delayed (15-minute) data, which needs no paid
  subscription and covers every venue in this portfolio. Each ticker carries
  `close` — the previous close — so day-change % is computable here without
  OpenBB, and is the same figure the movers list is ranked on.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import threading
from datetime import datetime, timezone

import derive

HOST = "127.0.0.1"
PORT = 4001
# Distinct from test_ibkr_connection.py (1) and build.py / refresh.py (7) so the
# daily job can still connect while this holds its own session open.
#
# Overridable because a hard kill (SIGKILL) leaves IB Gateway holding the client
# slot half-open; reconnecting on the same id can then land in a session that
# never re-subscribes account updates, so portfolio() stays empty while the
# connection looks healthy. Switching id is the quickest way out without
# restarting Gateway.
CLIENT_ID = int(os.environ.get("IB_CLIENT_ID", "11"))

POLL_SECONDS = 3.0
CONNECT_TIMEOUT = 8.0
# Ceiling on the one-time per-session setup calls (contract details, daily
# bars). They must not be able to block the session indefinitely.
SETUP_TIMEOUT = 25.0
# Poll cycles between retries when setup came back empty (3s each -> ~2 min).
SETUP_RETRY_CYCLES = 40
RETRY_MIN = 2.0
RETRY_MAX = 30.0
MARKET_DATA_DELAYED = 3

log = logging.getLogger("feed")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _finite(value) -> float | None:
    """ib_async reports absent ticker fields as nan, not None."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


class LiveFeed:
    def __init__(self, host: str = HOST, port: int = PORT,
                 client_id: int = CLIENT_ID, poll_seconds: float = POLL_SECONDS):
        self.host, self.port, self.client_id = host, port, client_id
        self.poll_seconds = poll_seconds

        self._lock = threading.Lock()
        self._payload: dict | None = None
        self._connected = False
        self._last_refresh: str | None = None
        self._connected_since: str | None = None
        self._error: str | None = None
        self._reconnects = 0
        self._subscribed = 0

        # conId -> prior session close, from daily bars. See _fetch_prior_closes.
        self._prior_close: dict[int, float] = {}
        self._prior_close_day: str | None = None
        # conId -> IBKR priceMagnifier. 100 for LSE lines quoted in pence.
        self._magnifier: dict[int, int] = {}

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------- public, called from the HTTP thread ----------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ib-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=6)

    def snapshot(self) -> dict:
        """Latest values plus honest freshness. Never raises, never blocks on IB.

        Returns the last good payload even while disconnected — the page needs
        the values *and* the flag saying they have stopped moving.
        """
        with self._lock:
            payload = self._payload
            meta = {
                "source": "ib-live",
                "connected": self._connected,
                "last_refresh": self._last_refresh,
                "connected_since": self._connected_since,
                "reconnects": self._reconnects,
                "subscribed_tickers": self._subscribed,
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
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except Exception:
            log.exception("feed thread died")
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()

    async def _main(self) -> None:
        from ib_async import IB

        backoff = RETRY_MIN
        first = True
        while not self._stop.is_set():
            ib = IB()
            try:
                await ib.connectAsync(self.host, self.port, clientId=self.client_id,
                                      timeout=CONNECT_TIMEOUT, readonly=True)
                accounts = ib.managedAccounts()
                account = accounts[0] if accounts else ""
                log.info("connected to %s:%s as client %d, account %s",
                         self.host, self.port, self.client_id, account or "?")

                with self._lock:
                    self._connected = True
                    self._connected_since = _now()
                    self._error = None
                    if not first:
                        self._reconnects += 1
                first = False
                backoff = RETRY_MIN

                await self._session(ib, account)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("feed connection failed: %s", exc)
                with self._lock:
                    self._error = str(exc)
            finally:
                with self._lock:
                    self._connected = False
                try:
                    if ib.isConnected():
                        ib.disconnect()
                except Exception:
                    pass

            if self._stop.is_set():
                break
            log.info("reconnecting in %.0fs", backoff)
            await self._sleep(backoff)
            backoff = min(backoff * 2, RETRY_MAX)

    async def _sleep(self, seconds: float) -> None:
        """Sleep that wakes early on stop, so shutdown is not held up."""
        step = 0.25
        waited = 0.0
        while waited < seconds and not self._stop.is_set():
            await asyncio.sleep(min(step, seconds - waited))
            waited += step

    async def _session(self, ib, account: str) -> None:
        """Subscribe, then recompose on the poll interval until disconnected."""
        from ib_async import Contract

        ib.reqMarketDataType(MARKET_DATA_DELAYED)

        items = ib.portfolio(account) if account else ib.portfolio()

        # Contracts from portfolio() carry primaryExchange but leave `exchange`
        # empty, and reqMktData rejects that with "Error validating request …
        # Please enter exchange" — silently, as a warning, so every
        # subscription fails while the call still hands back a Ticker.
        # Re-qualifying from conId alone makes IBKR fill in a routable exchange
        # (SMART for most, SGX for the Singapore lines).
        # One contract-details pass, not two.
        #
        # ContractDetails carries both the fully-qualified contract *and*
        # priceMagnifier, so asking for it once gives everything. Calling
        # qualifyContractsAsync first and then reqContractDetailsAsync per
        # contract issued 30 round-trips for 15 positions, which runs into
        # IBKR's contract-details pacing and stalls the session before it ever
        # subscribes — the connection looks healthy while portfolio() stays
        # empty. Gathered rather than sequential for the same reason.
        #
        # priceMagnifier is 100 for LSE lines quoted in pence and 1 elsewhere;
        # without it a pence tick inflates a position 100-fold.
        qualified = await self._resolve_contracts(ib, items)

        tickers: dict[int, object] = {}
        for contract in qualified:
            if not contract or not contract.conId:
                continue
            try:
                tickers[contract.conId] = ib.reqMktData(contract, "", False, False)
            except Exception as exc:
                log.debug("no market data for %s: %s", contract.symbol, exc)

        with self._lock:
            self._subscribed = len(tickers)
        log.info("subscribed to %d delayed tickers", len(tickers))

        try:
            await asyncio.wait_for(self._fetch_prior_closes(ib, qualified),
                                   timeout=SETUP_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("historical bars timed out — day change unavailable this session")

        # Give the first ticks a moment so the opening compose is not all None.
        await self._sleep(2.0)

        stale_setup = 0
        while not self._stop.is_set() and ib.isConnected():
            # If the one-time setup came back empty the session is degraded —
            # that happens when Gateway is connected but not reaching IBKR. It
            # usually recovers within minutes, so retry rather than waiting for
            # the daily roll or a reconnect, which would otherwise leave day
            # change blank for the rest of the session.
            if not self._prior_close or not self._magnifier:
                stale_setup += 1
                if stale_setup % SETUP_RETRY_CYCLES == 0:
                    log.info("setup incomplete (%d contracts, %d prior closes) — retrying",
                             len(self._magnifier), len(self._prior_close))
                    try:
                        # Reassign: the retry has to use the newly resolved
                        # contracts. Reusing the old list would keep feeding the
                        # unqualified fallbacks — which carry no exchange, so
                        # every historical request fails with "Please enter
                        # exchange" and the retry can never succeed.
                        qualified = await asyncio.wait_for(
                            self._resolve_contracts(ib, items), timeout=SETUP_TIMEOUT)
                        await asyncio.wait_for(self._fetch_prior_closes(ib, qualified),
                                               timeout=SETUP_TIMEOUT)
                    except asyncio.TimeoutError:
                        log.debug("setup retry still timing out")

            # Prior closes roll over at the start of a new day.
            if self._prior_close_day != datetime.now(timezone.utc).date().isoformat():
                await self._fetch_prior_closes(ib, qualified)
            try:
                payload = self._compose(ib, account, tickers)
                with self._lock:
                    self._payload = payload
                    self._last_refresh = _now()
                    self._error = None
            except Exception as exc:
                log.warning("compose failed: %s", exc)
                with self._lock:
                    self._error = str(exc)
            await self._sleep(self.poll_seconds)

        for con_id, ticker in tickers.items():
            try:
                ib.cancelMktData(ticker.contract)
            except Exception:
                pass

    async def _resolve_contracts(self, ib, items) -> list:
        """Qualified contracts + priceMagnifier, in one contract-details pass.

        ContractDetails carries both, so asking once gives everything. Calling
        qualifyContractsAsync first and then reqContractDetailsAsync per contract
        issued 30 round-trips for 15 positions, hit IBKR's contract-details
        pacing, and stalled the session before it ever subscribed.

        Bounded, because IB Gateway can accept a connection while its upstream
        link to IBKR is down: cached portfolio pushes still arrive but any
        request needing the server never answers. Observed 29 Jul, when
        reqCurrentTime returned instantly and reqContractDetails hung forever.

        priceMagnifier is 100 for LSE lines quoted in pence and 1 elsewhere;
        without it a pence tick inflates a position 100-fold.
        """
        # Imported here rather than relying on _session's local import — this
        # method is called from two places and a missing name would raise
        # NameError inside details_for, be swallowed by its except, and look
        # exactly like every contract failing to resolve.
        from ib_async import Contract

        failures = []

        async def details_for(item):
            try:
                return await ib.reqContractDetailsAsync(Contract(conId=item.contract.conId))
            except Exception as exc:
                failures.append(f"{item.contract.symbol}: {type(exc).__name__} {exc}")
                return None

        try:
            results = await asyncio.wait_for(
                asyncio.gather(*(details_for(i) for i in items),
                               return_exceptions=True),
                timeout=SETUP_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("contract details timed out after %.0fs — Gateway is "
                        "connected but not reaching IBKR. Serving account values "
                        "only; tick repricing disabled until this succeeds.",
                        SETUP_TIMEOUT)
            results = [None] * len(items)

        qualified, magnifier = [], {}
        for item, detail in zip(items, results):
            if isinstance(detail, Exception) or not detail:
                qualified.append(item.contract)
                continue
            cd = detail[0]
            qualified.append(cd.contract)
            magnifier[cd.contract.conId] = int(getattr(cd, "priceMagnifier", 1) or 1)

        with self._lock:
            self._magnifier = magnifier
        log.info("resolved %d/%d contracts, %d quoting in minor units",
                 len(magnifier), len(items),
                 sum(1 for m in magnifier.values() if m != 1))
        # Everything failing is a bug in here, not a market being shut — say so
        # loudly rather than leaving a silent 0/N in the log.
        if items and not magnifier and failures:
            log.error("every contract failed to resolve — first: %s", failures[0])
        return qualified

    async def _fetch_prior_closes(self, ib, contracts) -> None:
        """Prior session close per position, from daily bars.

        This exists because `ticker.close` is unusable once a market shuts:
        IBKR rolls it forward to *today's* close, which is also the last price,
        so price-vs-close compares a number against itself and always lands
        near 0%. Measured 28 Jul: SMSN reported -0.13% against a real -5.21%.

        Daily bars do not have that problem. `bars[-1]` is the current (or most
        recent) session and `bars[-2]` is the one before it, so bars[-2].close
        is the right reference whether the market is open or closed.

        15 requests against IBKR's 60-per-10-minute pacing, once a day.
        """
        today = datetime.now(timezone.utc).date().isoformat()
        closes: dict[int, dict] = {}
        for contract in contracts:
            if not contract or not contract.conId:
                continue
            try:
                bars = await ib.reqHistoricalDataAsync(
                    contract, endDateTime="", durationStr="6 D",
                    barSizeSetting="1 day", whatToShow="TRADES",
                    useRTH=True, formatDate=1)
            except Exception as exc:
                log.debug("no history for %s: %s", contract.symbol, exc)
                continue
            if not bars or len(bars) < 2:
                continue

            prior = _finite(bars[-2].close)
            last = _finite(bars[-1].close)
            if not prior or prior <= 0:
                continue
            closes[contract.conId] = {
                "prior": prior,
                "last": last,
                # Whether the latest bar belongs to today decides which session
                # the day change describes.
                "traded_today": str(bars[-1].date) == today,
            }

        with self._lock:
            self._prior_close = closes
            self._prior_close_day = today
        traded = sum(1 for c in closes.values() if c["traded_today"])
        log.info("prior closes for %d/%d positions (%d trading today)",
                 len(closes), len(contracts), traded)

    # ---------- composition ----------

    def _compose(self, ib, account: str, tickers: dict) -> dict:
        values = ib.accountValues(account) if account else ib.accountValues()
        items = ib.portfolio(account) if account else ib.portfolio()

        base_currency = "GBP"
        totals: dict[str, float] = {}
        fx: dict[str, float] = {}
        for v in values:
            if v.tag == "ExchangeRate":
                rate = _finite(v.value)
                if rate is not None:
                    fx[v.currency] = rate
            elif v.tag in ("NetLiquidation", "TotalCashValue", "GrossPositionValue"):
                # These are reported per currency; take the base-currency row.
                if v.currency in ("BASE", base_currency) and v.tag not in totals:
                    amount = _finite(v.value)
                    if amount is not None:
                        totals[v.tag] = amount
                        if v.currency != "BASE":
                            base_currency = v.currency
        fx.setdefault(base_currency, 1.0)
        fx.setdefault("BASE", 1.0)

        to_gbp = derive.converter(fx)
        with self._lock:
            prior_closes = dict(self._prior_close)
            magnifier = dict(self._magnifier)

        positions = []
        for item in items:
            contract = item.contract
            ticker = tickers.get(contract.conId)
            hist_prev = prior_closes.get(contract.conId)
            # Market data arrives in the venue's quoting unit; account values do
            # not. Only the former needs scaling — IBKR already reports HSBA's
            # marketPrice as 15.62, not 1562.
            scale = magnifier.get(contract.conId, 1) or 1

            account_price = _finite(item.marketPrice)
            tick_price = tick_close = None
            if ticker is not None:
                tick_price = _finite(ticker.marketPrice())
                # IBKR returns -1 (and -100 on LSE) rather than nothing when a
                # market is shut and there is no last/bid/ask. Taken at face
                # value that reads as a -100% day.
                if tick_price is not None and tick_price <= 0:
                    tick_price = None
                elif tick_price is not None:
                    tick_price /= scale
                tick_close = _finite(getattr(ticker, "close", None))
                if tick_close is not None and tick_close <= 0:
                    tick_close = None
                elif tick_close is not None:
                    tick_close /= scale

            price = tick_price or account_price

            # Day change compares two closes of the same series, so it is
            # computed entirely in the bars' own unit — no pence/pounds
            # reconciling needed, unlike the display price.
            change_pct, source = None, "none"
            if hist_prev and hist_prev["prior"] > 0:
                # Bars arrive in the same quoting unit as the ticks, so they are
                # scaled identically. Within the close/close branch the scale
                # cancels; in the live branch it must match tick_price, which
                # has already been scaled.
                prior = hist_prev["prior"] / scale
                if hist_prev["traded_today"] and tick_price:
                    # Session in progress: live price against yesterday's close.
                    # tick_price and the bars share a unit (both from IBKR
                    # market data), so this needs no scaling.
                    ref = tick_price
                    source = "ib-hist-live"
                elif hist_prev["last"]:
                    # Nothing has traded today, so the honest figure is the last
                    # completed session's own move — close against the close
                    # before it. Using the account's current price here instead
                    # would measure across two sessions: SMSN read -5.33% that
                    # way against a true -5.21%.
                    ref = hist_prev["last"] / scale
                    source = "ib-hist-close"
                else:
                    ref = None
                if ref:
                    change_pct = (ref / prior - 1.0) * 100.0
            elif tick_price and tick_close:
                # No history for this contract; the ticker pair is at least
                # self-consistent while the market is actually trading.
                change_pct = (tick_price / tick_close - 1.0) * 100.0
                source = "ib-delayed"

            market_value = _finite(item.marketValue) or 0.0
            # Reprice off the live tick so market value moves with the price;
            # IBKR only refreshes marketValue on its own account-update
            # cadence, which would leave the two disagreeing between pushes.
            # Only when the ticker is live *and* we know its quoting unit —
            # repricing without a confirmed magnifier is how a pence tick
            # inflated a position 100-fold.
            if tick_price and item.position and contract.conId in magnifier:
                repriced = tick_price * float(item.position)
                if repriced:
                    market_value = repriced

            row = derive.make_position(
                con_id=contract.conId,
                symbol=contract.symbol,
                currency=contract.currency,
                exchange=contract.primaryExchange or contract.exchange or "",
                quantity=float(item.position),
                price=price if price is not None else 0.0,
                market_value=market_value,
                average_cost=_finite(item.averageCost) or 0.0,
                unrealized_pnl=_finite(item.unrealizedPNL) or 0.0,
                day_change_pct=change_pct,
                day_change_source=source,
                spark=[],          # historical series is not IB market data
                to_gbp=to_gbp,
            )
            # Kept only so the GrossPositionValue check below can rebuild the
            # row from IBKR's own figures; stripped before the payload leaves.
            row["_account_price"] = account_price if account_price is not None else 0.0
            row["_account_value"] = _finite(item.marketValue) or 0.0
            row["_average_cost"] = _finite(item.averageCost) or 0.0
            row["_unrealised_native"] = _finite(item.unrealizedPNL) or 0.0
            positions.append(row)

        nav = totals.get("NetLiquidation", 0.0)
        cash = totals.get("TotalCashValue", 0.0)

        # Cross-check against IBKR's own total before trusting our repriced
        # values. Repricing from ticks is what makes the page move between
        # account pushes, but it is also the one place a quoting-unit mistake
        # can inflate the whole portfolio — an unscaled pence tick once put
        # invested at £759k against a £50k NAV. GrossPositionValue is the
        # broker's own figure and costs nothing to compare, so when the two
        # disagree we drop the repricing rather than publish a wrong number.
        gross = totals.get("GrossPositionValue")
        invested = sum(p["value_gbp"] for p in positions)
        check = "ok"
        if gross and invested and abs(invested - gross) / gross > 0.02:
            log.error("invested %.2f disagrees with IBKR GrossPositionValue %.2f "
                      "(%.1f%%) — falling back to account market values",
                      invested, gross, abs(invested - gross) / gross * 100)
            check = "fallback"
            positions = [
                derive.make_position(
                    con_id=p["con_id"], symbol=p["symbol"], currency=p["currency"],
                    exchange=p["exchange"], quantity=p["quantity"],
                    price=p["_account_price"], market_value=p["_account_value"],
                    average_cost=p["_average_cost"], unrealized_pnl=p["_unrealised_native"],
                    day_change_pct=p["day_change_pct"],
                    day_change_source=p["day_change_source"],
                    spark=[], to_gbp=to_gbp)
                for p in positions
            ]

        for p in positions:
            for key in ("_account_price", "_account_value", "_average_cost",
                        "_unrealised_native"):
                p.pop(key, None)

        agg = derive.aggregate(positions, nav=nav, cash=cash, account_daily_pnl=None)

        return {
            "meta": {
                "account_id": account,
                "base_currency": base_currency,
                "fx_source": "ibkr",
                "daily_pnl_source": agg["daily_pnl_source"],
                "invested_check": check,
                "gross_position_value": gross,
                "gateway": "ok",
            },
            "kpis": agg["kpis"],
            "positions": positions,
            "regions": agg["regions"],
            "currencies": agg["currencies"],
            "concentration": agg["concentration"],
            "movers": agg["movers"],
            "fx": fx,
        }


if __name__ == "__main__":
    import json
    import time

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    feed = LiveFeed()
    feed.start()
    try:
        for _ in range(10):
            time.sleep(3)
            snap = feed.snapshot()
            meta, kpis = snap["meta"], snap.get("kpis", {})
            print(f"connected={meta['connected']} refreshed={meta['last_refresh']} "
                  f"tickers={meta['subscribed_tickers']} "
                  f"nav={kpis.get('net_liquidation')} "
                  f"day={kpis.get('daily_pnl')} n={len(snap.get('positions', []))}")
    except KeyboardInterrupt:
        pass
    finally:
        feed.stop()
