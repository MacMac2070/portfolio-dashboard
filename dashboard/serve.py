#!/usr/bin/env python3
"""Dev server for the dashboard, with the live IB feed attached.

    /opt/anaconda3/bin/python3 serve.py [port] [--no-live]

Serves the static files plus four endpoints:

    GET /api/snapshot         the newest account and position values, and a
                              timestamp saying when they were last refreshed
    GET /api/watchlist        openbb quotes for tickers you don't own
    GET /api/markets          the world index board behind Market watch
    GET /api/instrument/<key> per-instrument detail behind the stock page

The four sources are independent on purpose: one going down degrades its own
page and nothing else. `--no-watchlist`, `--no-markets` and `--no-instrument`
skip the openbb ones.

/api/instrument is the only one that is click-driven rather than polled, so it
caches per symbol and does its openbb import on its own warmup thread — see
adapter/instrument.py for why that matters to the IB feed.

The feed runs on a background thread inside this process and holds a single
ib_async connection to IB Gateway open for as long as the server runs — see
adapter/feed.py. Editing HTML/CSS/JS does not disturb it; only restarting the
server does.

`--no-live` skips the feed and serves static files only, which is what you want
when IB Gateway is down and you just want to look at the page.

Plain `python3 -m http.server` sends no Cache-Control, so browsers apply
heuristic caching and keep serving stale CSS/JS through an edit-reload loop.
This sends no-store on everything, which is what you want while iterating.
"""
import json
import logging
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "adapter"))

from markets import REGION_TO_MARKET  # noqa: E402  (needs the path above)


def markets_catalogue():
    """Every index the board tracks, for the benchmark picker.

    Imported lazily and defensively: the picker is a convenience, and a broken
    markets module should cost the benchmark line, not the whole server.
    """
    try:
        from markets import MarketFeed
        return MarketFeed.catalogue()
    except Exception:
        log.exception("could not build the index catalogue")
        return []


def store_read_nav():
    """The NAV history the equity curve is drawn from, parsed once here.

    The page fetches data/nav_history.jsonl directly; this reads the same file
    through the same store helper, so the benchmark is aligned against exactly
    the dates the portfolio line uses.
    """
    from store import NAV_PATH, _read
    return _read(NAV_PATH)


def _no_financials(key, error):
    """The financials page's offline shape. Built here, like _no_instrument, so
    it still answers when adapter.financials is the thing that failed to load."""
    return {
        "meta": {"source": "openbb", "state": "ready", "connected": False,
                 "period": "annual", "cache_age_s": {}, "stale": [], "pending": [],
                 "retry_after": None, "error": error},
        "coverage": {}, "instrument": {"key": key},
        "reporting": {"currency": None, "source": "unknown", "quote_currency": "",
                      "differs_from_quote": False, "note": ""},
        "periods": ["annual", "quarter"],
        "statements": {w: {"available": False, "periods": [], "sections": [],
                           "chart": [], "reason": error}
                       for w in ("income", "balance", "cash")},
    }


def _no_instrument(key, error):
    """The stock page's offline shape: identity if we have it, nulls otherwise.

    Built here rather than imported from adapter.instrument so this still
    answers when that module is the thing that failed to load.
    """
    return {
        "meta": {"source": "openbb", "state": "ready", "connected": False,
                 "cache_age_s": {}, "stale": [], "pending": [],
                 "retry_after": None, "error": error},
        "coverage": {}, "instrument": {"key": key},
        "price": {}, "key_stats": {}, "profile": {}, "news": [],
        "analyst": {}, "ranges": {}, "benchmark": {},
    }

log = logging.getLogger("serve")

EMPTY = {"kpis": {}, "positions": [], "regions": [], "currencies": [],
         "concentration": {}, "movers": {"gainers": [], "losers": []}}


class DashboardHandler(SimpleHTTPRequestHandler):
    feed = None          # IB Gateway live feed; None means static-only
    watchlist = None     # openbb quotes for tickers the IB feed does not cover
    markets = None       # world index board for Market watch
    instrument = None    # per-symbol detail for the stock page
    lookup = None        # query-time symbol search beyond the cached directory
    financials = None    # company statements behind the financials page

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def do_GET(self):
        route = self.path.split("?", 1)[0]
        if route == "/api/snapshot":
            return self._snapshot()
        if route == "/api/watchlist":
            return self._watchlist()
        if route == "/api/markets":
            return self._markets()
        if route == "/api/benchmark":
            return self._benchmark()
        if route == "/api/attribution":
            return self._attribution()
        if route == "/api/track":
            return self._track()
        if route == "/api/desk":
            return self._desk()
        if route == "/api/intent":
            return self._intent()
        # Prefix rather than equality — this is the one endpoint with the
        # instrument key in the path.
        if route.startswith("/api/instrument/"):
            return self._instrument(route[len("/api/instrument/"):])
        if route == "/api/directory":
            return self._directory()
        if route == "/api/lookup":
            return self._lookup()
        if route.startswith("/api/financials/"):
            return self._financials(route[len("/api/financials/"):])
        if route.startswith("/api/"):
            return self._json({"error": "unknown endpoint"}, status=404)
        return super().do_GET()

    def do_POST(self):
        """The only write path in the dashboard. Everything else is read-only."""
        route = self.path.split("?", 1)[0]
        if route == "/api/watch":
            return self._watch()
        return self._json({"ok": False, "error": "unknown endpoint"}, status=404)

    def _query(self, name, default=""):
        return (parse_qs(urlsplit(self.path).query).get(name) or [default])[0].strip()

    def _directory(self):
        # A version handshake rather than ETag/304: end_headers() sets no-store
        # on everything (load-bearing for the CSS/JS reload loop), and every
        # /api/* here answers 200 with meta.error. `?have=` keeps both, and a
        # match costs ~90 bytes instead of ~190 KB.
        try:
            import directory
            version, raw, packed = directory.payload_bytes()
            if version and self._query("have") == version:
                return self._json({
                    "meta": {"version": version, "count": directory.count(),
                             "error": None},
                    "unchanged": True})
            return self._bytes(raw, packed)
        except Exception as exc:
            log.exception("directory endpoint failed")
            return self._json({
                "meta": {"version": "", "count": 0, "error": str(exc)},
                "unchanged": False, "cols": ["s", "n", "t", "x", "c"], "rows": []})

    def _financials(self, raw_key):
        key = unquote(raw_key).strip()
        period = self._query("period", "annual")
        service = DashboardHandler.financials
        if service is None:
            return self._json(_no_financials(key, "financials service not running"))
        try:
            return self._json(service.snapshot(key, period))
        except Exception as exc:
            log.exception("financials snapshot failed for %s", key)
            return self._json(_no_financials(key, str(exc)))

    def _lookup(self):
        q = self._query("q")
        service = DashboardHandler.lookup
        if service is None:
            return self._json({"meta": {"source": "none", "query": q, "state": "ready",
                                        "error": "lookup service not running"},
                               "results": []})
        try:
            return self._json(service.lookup(q, limit=8))
        except Exception as exc:
            log.exception("lookup failed for %r", q)
            return self._json({"meta": {"source": "yfinance-direct", "query": q,
                                        "state": "ready", "error": str(exc)},
                               "results": []})

    def _watch(self):
        # The socket binds 127.0.0.1, so a remote peer cannot reach this at all.
        # The check is belt-and-braces against a future bind change.
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return self._json({"ok": False, "error": "forbidden"}, status=403)
        # A cross-origin <form> POST cannot set a custom header, so requiring
        # one is a sufficient CSRF guard for a localhost JSON endpoint.
        if self.headers.get("X-Requested-With") != "portfolio-dashboard":
            return self._json({"ok": False, "error": "bad request"}, status=400)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if not 0 < length <= 4096:
            return self._json({"ok": False, "error": "bad body"}, status=400)
        try:
            # Exactly Content-Length, or the connection desyncs.
            body = json.loads(self.rfile.read(length))
            key = str(body.get("key") or "").strip()
        except (ValueError, AttributeError) as exc:
            return self._json({"ok": False, "error": f"bad json: {exc}"}, status=400)

        try:
            import overlay
            result = overlay.add(
                key, name=body.get("name") or "", symbol=body.get("symbol") or "",
                exchange=body.get("exchange") or "", currency=body.get("currency") or "")
        except Exception as exc:
            log.exception("watch failed for %r", key)
            return self._json({"ok": False, "error": str(exc)})

        # Quote it from the next watchlist poll rather than the next restart.
        watchlist = DashboardHandler.watchlist
        if result.get("added") and watchlist is not None:
            try:
                watchlist.add_symbol(result.get("symbol") or key)
            except Exception:
                log.exception("could not add %r to the watchlist feed", key)
        return self._json(result)

    def _track(self):
        """The track-record derivations: daily/monthly returns, drawdown, days.

        Everything comes off the two stores the backfill writes plus the index
        history the market feed already holds; nothing here fetches.
        """
        import track  # noqa: PLC0415 — adapter/ is on sys.path

        meta = {"source": "ibkr-flex", "error": None}
        try:
            # Same source _benchmark reads: whatever the market feed last
            # pulled, keyed by index symbol. Absent feed -> no benchmark row,
            # which track.build treats as an honest gap, not an error.
            history = (DashboardHandler.markets.history
                       if DashboardHandler.markets is not None else None)
            payload = track.build(index_history=history,
                                  benchmark_symbol=self._query("symbol") or None)
        except Exception as exc:
            log.exception("track build failed")
            return self._json({"meta": {"source": "ibkr-flex", "error": str(exc)}})
        return self._json({"meta": meta, **payload})

    def _desk(self):
        """Desk context: the daily yfinance pull plus request-time derivations.

        desk.json is static (refresh.py rewrites it daily); income projections
        are composed here against the live feed's positions and FX so they move
        with the day. Absent file -> honest not-ready payload, never an error.
        """
        import desk  # noqa: PLC0415
        import income  # noqa: PLC0415

        stored = desk.read()
        if not stored:
            return self._json({
                "meta": {"source": "yfinance", "error": None, "ready": False,
                         "hint": "run adapter/desk.py once, or wait for the daily job"},
            })
        live = self._live_snapshot_or_none()
        # Gateway down: project against the last build's share counts and FX
        # rather than standing the £ column down entirely — the payload's meta
        # still says the feed is disconnected, and share counts change on
        # trades, not ticks.
        if not (live and live.get("positions")):
            try:
                snapshot_path = Path(__file__).parent / "data" / "portfolio.json"
                live = json.loads(snapshot_path.read_text())
            except Exception:
                live = None
        from store import CASH_PATH, _read as store_rows  # noqa: PLC0415
        payload = {
            "meta": {"source": "yfinance", "error": None, "ready": True,
                     "fetched_at": stored["meta"].get("fetched_at"),
                     "errors": stored["meta"].get("errors") or {}},
            "holdings": stored.get("holdings") or {},
            "income": income.build(stored, live),
            "overnight": desk.overnight(live, store_rows(CASH_PATH)),
        }
        return self._json(payload)

    def _intent(self):
        """Operator intent from config.local.json: targets and rules.

        Reads only the two keys it serves — the Flex token lives in the same
        file and must never ride along. The assertion is belt-and-braces: the
        payload is constructed from an allowlist, so the token cannot appear.
        """
        targets, rules = {}, {}
        try:
            import flex  # noqa: PLC0415
            config = flex.load_config()
            raw_t = config.get("targets") or {}
            targets = {axis: raw_t.get(axis) or {}
                       for axis in ("region", "sector", "currency")}
            # Drop the _comment keys the example carries.
            targets = {a: {k: v for k, v in m.items() if not k.startswith("_")}
                       for a, m in targets.items()}
            rules = {k: v for k, v in (config.get("rules") or {}).items()
                     if not k.startswith("_")}
        except Exception:
            pass    # unconfigured is a designed state, not an error
        # Built-in thresholds so the rules panel is alive before anything is
        # configured; the panel labels them "default" until config overrides.
        defaults = {"max_position_pct": 20, "cash_floor_pct": 5,
                    "currency_band": {"HKD": 40}}
        merged = {**defaults, **rules}
        payload = {"meta": {"error": None}, "targets": targets,
                   "rules": merged,
                   "rules_source": "config" if rules else "default"}
        assert "flex_token" not in json.dumps(payload)
        return self._json(payload)

    def _live_snapshot_or_none(self):
        """The live feed's latest composition, or None — never raises."""
        try:
            feed = DashboardHandler.feed
            return feed.snapshot() if feed is not None else None
        except Exception:
            return None

    def _instrument(self, raw_key):
        # Same contract as the other three: always 200, with meta.error carrying
        # the reason. An unknown key is not a 404 — the page's job is to render
        # em-dashes and an honest banner, and an HTTP error gives it neither.
        key = unquote(raw_key).strip()
        service = DashboardHandler.instrument
        if service is None:
            return self._json(_no_instrument(key, "instrument service not running"))
        try:
            return self._json(service.snapshot(key))
        except Exception as exc:
            log.exception("instrument snapshot failed for %s", key)
            return self._json(_no_instrument(key, str(exc)))

    def _watchlist(self):
        # Same contract as /api/snapshot: always 200, with connected saying
        # whether the numbers are moving. A failure here must never affect
        # owned rows, which come from the IB feed alone.
        if DashboardHandler.watchlist is None:
            return self._json({"meta": {"source": "none", "connected": False,
                                        "error": "watchlist feed not running"},
                               "quotes": {}, "universe": {"tickers": {}, "groups": []}})
        try:
            return self._json(DashboardHandler.watchlist.snapshot())
        except Exception as exc:
            log.exception("watchlist snapshot failed")
            return self._json({"meta": {"source": "openbb-yfinance", "connected": False,
                                        "error": str(exc)},
                               "quotes": {}, "universe": {"tickers": {}, "groups": []}})

    def _markets(self):
        # The board's own numbers come from openbb; the exposure column comes
        # from the live IB feed, converted here rather than in markets.py so
        # that module never has to know a broker exists.
        if DashboardHandler.markets is None:
            return self._json({"meta": {"source": "none", "connected": False,
                                        "error": "market feed not running"},
                               "markets": [], "open_now": [],
                               "trading_text": "Market data unavailable"})
        # None means "not known yet", which the board renders differently from
        # a genuine zero. `last_refresh` — not `connected` — is the test: the
        # feed reports a live socket well before it has composed its first set
        # of positions, and in that window every region legitimately reads 0.
        exposure, nav = None, 0.0
        try:
            feed = DashboardHandler.feed
            snap = feed.snapshot() if feed is not None else None
            if snap and snap.get("meta", {}).get("last_refresh"):
                exposure = {}
                nav = snap.get("kpis", {}).get("net_liquidation") or 0.0
                for row in snap.get("regions", []):
                    code = REGION_TO_MARKET.get(row.get("name"))
                    if code:
                        exposure[code] = exposure.get(code, 0.0) + (row.get("value_gbp") or 0.0)
        except Exception:
            # A broken IB feed costs the exposure column, not the whole board.
            log.exception("could not derive market exposure")
            exposure, nav = None, 0.0
        try:
            return self._json(DashboardHandler.markets.snapshot(exposure, nav))
        except Exception as exc:
            log.exception("markets snapshot failed")
            return self._json({"meta": {"source": "openbb-yfinance", "connected": False,
                                        "error": str(exc)},
                               "markets": [], "open_now": [],
                               "trading_text": "Market data unavailable"})

    def _benchmark(self):
        """The comparison line for Overview's equity curve.

        Reads the same nav_history the page draws its own line from, so both
        series come off one source and cannot disagree about which days exist.
        The index history is whatever Market watch last pulled — that feed
        already fetches 420 daily closes for all eleven indices on a 60s poll,
        so this endpoint adds no network traffic of its own.

        Always 200 with the catalogue attached, so an unwarmed feed still lets
        the page build its picker and simply draws no line.
        """
        import benchmark as bench  # noqa: PLC0415 — adapter/ is on sys.path

        catalogue = markets_catalogue()
        wanted = self._query("symbol") or bench.DEFAULT_SYMBOL
        entry = next((c for c in catalogue if c["symbol"] == wanted), None)
        if entry is None:
            entry = next((c for c in catalogue if c["symbol"] == bench.DEFAULT_SYMBOL),
                         catalogue[0] if catalogue else None)
        if entry is None:
            return self._json({"meta": {"source": "none", "error": "no indices configured"},
                               "available": [], "benchmark": None})

        meta = {"source": "openbb-yfinance", "error": None}
        try:
            nav_rows = [r for r in store_read_nav()
                        if r.get("date") and isinstance(r.get("nav_gbp"), (int, float))]
            nav_rows.sort(key=lambda r: r["date"])
        except Exception as exc:
            log.exception("could not read nav history for the benchmark")
            return self._json({"meta": {"source": "none", "error": str(exc)},
                               "available": catalogue, "benchmark": None})

        rows = []
        if DashboardHandler.markets is not None:
            try:
                rows = DashboardHandler.markets.history(entry["symbol"])
            except Exception as exc:
                log.exception("index history unavailable")
                meta["error"] = str(exc)
        else:
            meta["error"] = "market feed not running"

        try:
            payload = bench.build(nav_rows, rows, entry["symbol"],
                                  entry["name"], entry["currency"])
        except Exception as exc:
            log.exception("benchmark build failed")
            return self._json({"meta": {"source": "openbb-yfinance", "error": str(exc)},
                               "available": catalogue, "benchmark": None})

        return self._json({"meta": meta, "available": catalogue, "benchmark": payload})

    def _attribution(self):
        """NAV flow and the monthly income/cost bars, behind the Performance tab.

        Reads the two JSONL stores the Flex backfill writes. Both are absent
        until the Change in NAV and Cash Transactions sections are enabled on
        the Flex query, so `ready` says which of the two the page can draw and
        the view renders an empty state for whichever is missing rather than an
        error.
        """
        import attribution as attr  # noqa: PLC0415 — adapter/ is on sys.path
        from store import CASH_PATH, NAV_CHANGE_PATH, _read

        meta = {"source": "ibkr-flex", "error": None}
        change_rows, cash_rows = [], []
        try:
            change_rows = _read(NAV_CHANGE_PATH)
            cash_rows = _read(CASH_PATH)
        except Exception as exc:
            log.exception("could not read the attribution stores")
            meta["error"] = str(exc)

        # The store holds one row per reporting period. The Sankey covers the
        # whole span, so it takes the widest row rather than the newest — with
        # monthly sub-periods enabled the newest row is one month, not the
        # summary. The sub-periods themselves go to monthly() for the P&L bars.
        widest = max(change_rows, key=lambda r: r.get("span_days") or 0,
                     default=None) if change_rows else None
        try:
            flow = attr.flow(widest)
            monthly = attr.monthly(cash_rows, change_rows)
        except Exception as exc:
            log.exception("attribution build failed")
            return self._json({"meta": {"source": "ibkr-flex", "error": str(exc)},
                               "ready": {"flow": False, "monthly": False},
                               "flow": None, "monthly": None})

        return self._json({
            "meta": meta,
            "ready": {"flow": flow is not None,
                      "monthly": bool(monthly.get("months")),
                      "pnl": bool(monthly.get("pnl_available"))},
            "flow": flow,
            "monthly": monthly,
            # What the page tells the reader to do when a chart has no data.
            "hint": ("Enable the Change in NAV and Cash Transactions sections on "
                     "the Flex query, then run adapter/backfill.py"),
        })

    def _snapshot(self):
        # Always 200, even when the feed is down or absent. The page needs the
        # last values *and* an honest connected flag; an HTTP error would give
        # it neither.
        if DashboardHandler.feed is None:
            return self._json({**EMPTY, "meta": {
                "source": "none", "connected": False,
                "error": "server started with --no-live"}})
        try:
            return self._json(DashboardHandler.feed.snapshot())
        except Exception as exc:
            log.exception("snapshot failed")
            return self._json({**EMPTY, "meta": {
                "source": "ib-live", "connected": False, "error": str(exc)}})

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, raw, packed, status=200):
        """Pre-serialised JSON, gzipped when the client will take it.

        Only /api/directory uses this: it is ~0.8 MB and the other three
        endpoints are small enough that compressing them would cost more CPU
        than it saves. Both forms are memoised on the directory's version, so
        the serialise-and-compress happens once per rebuild, not per request.
        """
        body = raw
        encoding = None
        if packed and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body, encoding = packed, "gzip"
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # A line per request would be one every 3s once the page is polling.
        if getattr(self, "path", "").startswith("/api/snapshot"):
            return
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    positional = [a for a in sys.argv[1:] if not a.startswith("-")]
    live = "--no-live" not in sys.argv
    port = int(positional[0]) if positional else 5173

    feed = None
    if live:
        try:
            from feed import LiveFeed
            feed = LiveFeed()
            feed.start()
            log.info("live feed starting — IB Gateway %s:%s, client %d",
                     feed.host, feed.port, feed.client_id)
        except Exception as exc:
            log.error("could not start live feed: %s", exc)
            log.error("serving static only; the page falls back to data/portfolio.json")
            feed = None
    else:
        log.info("--no-live: static files only")

    # Started independently of the IB feed: different source, different cadence,
    # and neither should be able to take the other down.
    watchlist = None
    if live and "--no-watchlist" not in sys.argv:
        try:
            from watchlist import WatchlistFeed
            # Hold the watchlist until the IB feed has composed once. openbb's
            # import is CPU-heavy enough to starve the feed's event loop and
            # time out its startup requests — see WatchlistFeed._await_gate.
            ready = (lambda: bool(feed and feed.snapshot()["meta"]["last_refresh"]))
            watchlist = WatchlistFeed(gate=ready if feed else None)
            watchlist.start()
            log.info("watchlist feed starting — %d symbols via openbb/yfinance",
                     len(watchlist.symbols))
        except Exception as exc:
            log.error("could not start watchlist feed: %s", exc)
            log.error("owned positions are unaffected; watchlist rows will show as stale")
            watchlist = None

    # Third independent source. It shares the watchlist's openbb gate for the
    # same reason — the import is CPU-heavy enough to starve the IB event loop.
    markets = None
    if live and "--no-markets" not in sys.argv:
        try:
            from markets import MarketFeed
            ready = (lambda: bool(feed and feed.snapshot()["meta"]["last_refresh"]))
            markets = MarketFeed(gate=ready if feed else None)
            markets.start()
            log.info("market board starting — %d indices via openbb/yfinance",
                     len(markets.symbols))
        except Exception as exc:
            log.error("could not start market board: %s", exc)
            log.error("Overview and Watchlist are unaffected; Market watch will show as stale")
            markets = None

    # Fourth source, and the only click-driven one. Same gate as the other two
    # openbb consumers, but it matters more here: a poller can wait before its
    # first tick, whereas a click could otherwise pay openbb's import cost on an
    # HTTP worker thread and starve the IB feed exactly as on 29 Jul.
    instrument = None
    if live and "--no-instrument" not in sys.argv:
        try:
            from instrument import InstrumentService
            ready = (lambda: bool(feed and feed.snapshot()["meta"]["last_refresh"]))
            instrument = InstrumentService(gate=ready if feed else None)
            instrument.start()
            log.info("instrument service starting — openbb import deferred behind the feed gate")
        except Exception as exc:
            log.error("could not start instrument service: %s", exc)
            log.error("Overview, Watchlist and Market watch are unaffected; "
                      "stock pages will show as unavailable")
            instrument = None

    # Fifth source. yfinance only, no openbb, so it is cheap to start — but it
    # still warms behind the gate because a cold import on an HTTP worker during
    # Gateway startup is the same shape as the 29 Jul incident.
    lookup = None
    if live and "--no-lookup" not in sys.argv:
        try:
            from lookup import LookupService
            ready = (lambda: bool(feed and feed.snapshot()["meta"]["last_refresh"]))
            lookup = LookupService(gate=ready if feed else None)
            lookup.start()
            log.info("lookup service starting — symbol search beyond the directory")
        except Exception as exc:
            log.error("could not start lookup service: %s", exc)
            log.error("search still filters the cached directory; only the wider "
                      "market lookup is unavailable")
            lookup = None

    # Sixth source. Click-driven like the instrument service, and it holds only
    # one request slot so the two of them together cannot put more than three
    # openbb fan-outs in flight against the IB feed's event loop.
    financials = None
    if live and "--no-financials" not in sys.argv:
        try:
            from financials import FinancialsService
            ready = (lambda: bool(feed and feed.snapshot()["meta"]["last_refresh"]))
            financials = FinancialsService(gate=ready if feed else None)
            financials.start()
            log.info("financials service starting — statements behind the feed gate")
        except Exception as exc:
            log.error("could not start financials service: %s", exc)
            log.error("every other page is unaffected; financials will show as unavailable")
            financials = None

    DashboardHandler.feed = feed
    DashboardHandler.watchlist = watchlist
    DashboardHandler.markets = markets
    DashboardHandler.instrument = instrument
    DashboardHandler.lookup = lookup
    DashboardHandler.financials = financials
    handler = partial(DashboardHandler, directory=str(ROOT))

    try:
        with ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
            log.info("serving %s on http://localhost:%d", ROOT.name, port)
            if feed:
                log.info("snapshot endpoint http://localhost:%d/api/snapshot", port)
            if watchlist:
                log.info("watchlist endpoint http://localhost:%d/api/watchlist", port)
            if markets:
                log.info("markets endpoint   http://localhost:%d/api/markets", port)
            if instrument:
                log.info("instrument endpoint http://localhost:%d/api/instrument/INTC", port)
            try:
                import directory as _dir
                log.info("directory          %d symbols, version %s",
                         _dir.count(), _dir.version() or "not built")
            except Exception as exc:
                log.warning("directory unavailable: %s", exc)
            httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        if feed:
            feed.stop()
        if watchlist:
            watchlist.stop()
        if markets:
            markets.stop()
        if instrument:
            instrument.stop()
        if lookup:
            lookup.stop()
        if financials:
            financials.stop()
        try:
            import directory as _dir
            _dir.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
