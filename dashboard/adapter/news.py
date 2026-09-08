"""The Outlet: ranked news for the industries and markets the book holds.

Three layers off one free source (yfinance, same as everything else here):
market headlines read from index tickers, industry headlines from sector-proxy
ETFs, and the per-holding news the desk file already fetches nightly — merged,
deduped, and ranked by how much of the account actually sits behind each tag.
The weights ARE the personalisation: a headline tagged Hong Kong / China
outranks one tagged Singapore because 30% of the book does.

Earnings ride along as structured rows, not headlines: upcoming report dates
(from the desk calendar) and recently reported quarters with estimate vs
actual (yfinance earnings_dates), because "how did the quarter land" is a
figure, not a story.

No API key, no gateway, no always-on machine: everything is re-fetchable, so
the poller simply runs while serve.py runs and caches to data/news.json.
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import resilience
import store
import universe

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
NEWS_PATH = DATA_DIR / "news.json"
DESK_PATH = DATA_DIR / "desk.json"
PORTFOLIO_PATH = DATA_DIR / "portfolio.json"

POLL_SECONDS = 1200          # headlines cadence; a newsroom, not a ticker
FIRST_RETRY_SECONDS = 60
BREAKER = "yfinance"         # shared with every other module that asks Yahoo
FETCH_PAUSE = 0.4            # same pacing courtesy desk.py pays Yahoo
EARNINGS_STALE_HOURS = 20    # earnings figures move once a day at most
KEEP_ITEMS = 40
HALF_LIFE_HOURS = 36.0       # a story loses half its pull in a day and a half

# Market headlines are read from each region's index ticker. Keys must match
# regions.py's REGION_ORDER names, which is what portfolio positions carry.
REGION_NEWS = {
    "Hong Kong / China": ("^HSI", "HK/CN"),
    "United States": ("^GSPC", "US"),
    "South Korea": ("^KS11", "KR"),
    "Japan": ("^N225", "JP"),
    "Singapore": ("^STI", "SG"),
    "United Kingdom": ("^FTSE", "UK"),
}
# Industry headlines from sector-proxy ETFs. The ETFs sector has no proxy of
# its own — its exposure is the regions above.
SECTOR_NEWS = {
    universe.SEMIS: ("SOXX", "SEMIS"),
    universe.BIGTECH: ("QQQ", "TECH"),
    universe.FINANCIALS: ("XLF", "FINS"),
    universe.AIRLINES: ("JETS", "AIR"),
}
# Video carousels and opinion mills — filler, not news.
PUBLISHER_DENYLIST = {
    "yahoo finance video", "motley fool", "the motley fool", "zacks",
    "simply wall st.", "simply wall st", "insider monkey",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def interest_weights() -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    """£-share of invested per region, per sector, per holding key.

    From the last built portfolio.json — the weights only drift with prices,
    so a snapshot is plenty. Falls back to equal weights when no build exists,
    so a fresh clone still ranks something.
    """
    payload = _read_json(PORTFOLIO_PATH) or {}
    positions = payload.get("positions") or []
    invested = sum(p.get("value_gbp") or 0.0 for p in positions)
    regions: dict[str, float] = {}
    sectors: dict[str, float] = {}
    holdings: dict[str, float] = {}
    if invested > 0:
        for p in positions:
            share = (p.get("value_gbp") or 0.0) / invested
            regions[p.get("region") or ""] = regions.get(p.get("region") or "", 0.0) + share
            sym = p.get("symbol") or ""
            if sym:
                holdings[sym] = share
            canon = universe.key_for(p.get("con_id"), sym)
            if canon:
                holdings[canon] = share
    else:
        regions = {name: 1.0 / len(REGION_NEWS) for name in REGION_NEWS}
        sectors = {name: 1.0 / len(SECTOR_NEWS) for name in SECTOR_NEWS}
    return regions, sectors, holdings


def _normalise_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _parse_items(raw_news, *, kind: str, tag: str, weight: float) -> list[dict]:
    """yfinance news entries → outlet items, desk.py's normaliser idiom."""
    out = []
    for raw in (raw_news or [])[:10]:
        c = raw.get("content") or raw
        title = (c.get("title") or "").strip()
        publisher = ((c.get("provider") or {}).get("displayName") or "").strip()
        if not title or publisher.lower() in PUBLISHER_DENYLIST:
            continue
        url = ((c.get("canonicalUrl") or {}).get("url")
               or (c.get("clickThroughUrl") or {}).get("url") or "")
        out.append({
            "title": title,
            "url": url,
            "publisher": publisher,
            "at": (c.get("pubDate") or "").strip(),
            "kind": kind,
            "tag": tag,
            "weight": weight,
        })
    return out


def _score(item: dict) -> float:
    """Tag weight decayed by age — the whole ranking, on purpose this simple."""
    age_hours = 999.0
    if item.get("at"):
        try:
            at = datetime.fromisoformat(item["at"].replace("Z", "+00:00"))
            age_hours = max(0.0, (datetime.now(timezone.utc) - at).total_seconds() / 3600)
        except ValueError:
            pass
    return item.get("weight", 0.0) * math.pow(0.5, age_hours / HALF_LIFE_HOURS)


def fetch_headlines() -> list[dict]:
    """One sweep of the proxy tickers plus the desk file's holding news."""
    import yfinance as yf   # deferred: heavy, and news.py is imported by serve.py

    regions, sectors, holdings = interest_weights()
    items: list[dict] = []

    for region, (symbol, tag) in REGION_NEWS.items():
        share = regions.get(region, 0.0)
        if share <= 0:
            continue
        try:
            items += _parse_items(yf.Ticker(symbol).news, kind="market", tag=tag, weight=share)
        except Exception as exc:
            log.debug("market news %s: %s", symbol, exc)
        time.sleep(FETCH_PAUSE)

    for sector, (symbol, tag) in SECTOR_NEWS.items():
        share = sectors.get(sector, 0.0)
        if share <= 0:
            continue
        try:
            items += _parse_items(yf.Ticker(symbol).news, kind="sector", tag=tag, weight=share)
        except Exception as exc:
            log.debug("sector news %s: %s", symbol, exc)
        time.sleep(FETCH_PAUSE)

    # Holding news rides in from the desk file — fetched nightly there, so the
    # outlet never pays for it twice.
    desk = _read_json(DESK_PATH) or {}
    for key, h in (desk.get("holdings") or {}).items():
        share = holdings.get(key, 0.0)
        for item in (h.get("news") or []):
            publisher = (item.get("publisher") or "").strip()
            if not item.get("title") or publisher.lower() in PUBLISHER_DENYLIST:
                continue
            items.append({**item, "kind": "holding", "tag": key, "weight": share})

    # Dedupe by normalised title, the digest's own trick — keep the copy with
    # the most specific tag (highest weight wins ties toward the reader).
    best: dict[str, dict] = {}
    for item in items:
        k = _normalise_title(item["title"])
        if k not in best or _score(item) > _score(best[k]):
            best[k] = item

    # A front page, not a wire: no tag gets more than three slots, however
    # fresh its feed runs — US pre-market chatter would otherwise wall out
    # everything else every morning.
    ranked, seen_per_tag = [], {}
    for item in sorted(best.values(), key=_score, reverse=True):
        if seen_per_tag.get(item["tag"], 0) >= 3:
            continue
        seen_per_tag[item["tag"]] = seen_per_tag.get(item["tag"], 0) + 1
        item["score"] = round(_score(item), 5)
        item.pop("weight", None)
        ranked.append(item)
        if len(ranked) >= KEEP_ITEMS:
            break
    return ranked


def fetch_earnings() -> dict:
    """Upcoming report dates and recently reported quarters for the equities.

    Upcoming comes from the desk calendar (already fetched nightly); reported
    quarters come from yfinance earnings_dates — estimate vs actual with the
    surprise, which is the whole summary a card row needs.
    """
    import yfinance as yf

    _, _, holdings = interest_weights()
    today = datetime.now(timezone.utc).date()
    upcoming: list[dict] = []
    reported: list[dict] = []

    desk = _read_json(DESK_PATH) or {}
    for key, h in (desk.get("holdings") or {}).items():
        for d in (h.get("next_earnings") or []):
            try:
                when = datetime.fromisoformat(d).date()
            except ValueError:
                continue
            if 0 <= (when - today).days <= 45:
                upcoming.append({"key": key, "date": d, "weight": holdings.get(key, 0.0)})
                break                      # one next date per name

    equities = [t for t in universe.TICKERS.values()
                if t.owned and t.sector != universe.ETFS]
    for ticker in equities:
        try:
            df = yf.Ticker(ticker.symbol).earnings_dates
        except Exception as exc:
            log.debug("earnings %s: %s", ticker.key, exc)
            continue
        time.sleep(FETCH_PAUSE)
        if df is None or df.empty:
            continue
        for when, row in df.iterrows():
            day = when.date()
            actual = row.get("Reported EPS")
            if (today - day).days < 0 or (today - day).days > 45:
                continue
            if actual is None or (isinstance(actual, float) and math.isnan(actual)):
                continue
            surprise = row.get("Surprise(%)")
            reported.append({
                "key": ticker.key,
                "date": day.isoformat(),
                "eps_estimate": None if _nan(row.get("EPS Estimate")) else float(row.get("EPS Estimate")),
                "eps_reported": float(actual),
                "surprise_pct": None if _nan(surprise) else float(surprise),
                "weight": holdings.get(ticker.key, 0.0),
            })
            break                          # the most recent reported quarter

    upcoming.sort(key=lambda r: r["date"])
    reported.sort(key=lambda r: r["date"], reverse=True)
    for row in upcoming + reported:
        row.pop("weight", None)
    return {"upcoming": upcoming, "reported": reported, "fetched_at": _now()}


def _nan(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


class NewsFeed:
    """The serve.py service: poll, cache to disk, serve from memory.

    Same never-raise snapshot contract as every other feed. Serves the cached
    file from the moment of construction, so the card has yesterday's outlet
    while the first sweep runs.
    """

    def __init__(self, gate=None, poll_seconds: float = POLL_SECONDS):
        self.poll_seconds = poll_seconds
        self._gate = gate
        self._gate_timeout = 90.0
        self._lock = threading.Lock()
        self._payload: dict | None = _read_json(NEWS_PATH)
        self._error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="news-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=6)

    def snapshot(self) -> dict:
        with self._lock:
            payload = self._payload
            error = self._error
        breaker = resilience.get(BREAKER).snapshot()
        if not payload:
            return {"meta": {"fetched_at": None, "items": 0, "error": error, "breaker": breaker},
                    "items": [], "earnings": {"upcoming": [], "reported": []}}
        return {**payload, "meta": {**payload.get("meta", {}), "error": error, "breaker": breaker}}

    # ---------- worker ----------

    def _await_gate(self) -> None:
        waited = 0.0
        while self._gate and waited < self._gate_timeout and not self._stop.is_set():
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
        try:
            items = fetch_headlines()
        except Exception as exc:
            log.warning("outlet: headline sweep failed: %s", exc)
            with self._lock:
                self._error = str(exc)
            return False

        # Earnings figures move once a day; keep the previous block inside the
        # staleness window rather than re-asking Yahoo for the same table.
        earnings = None
        with self._lock:
            prev = (self._payload or {}).get("earnings")
        if prev and prev.get("fetched_at"):
            try:
                fetched = datetime.fromisoformat(prev["fetched_at"].replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - fetched).total_seconds() < EARNINGS_STALE_HOURS * 3600:
                    earnings = prev
            except ValueError:
                pass
        if earnings is None:
            try:
                earnings = fetch_earnings()
            except Exception as exc:
                log.warning("outlet: earnings fetch failed: %s", exc)
                earnings = prev or {"upcoming": [], "reported": [], "fetched_at": None}

        payload = {
            "meta": {"fetched_at": _now(), "items": len(items)},
            "items": items,
            "earnings": earnings,
        }
        with self._lock:
            self._payload = payload
            self._error = None
        try:
            store.write_json(NEWS_PATH, payload, indent=1)
        except OSError as exc:
            log.warning("outlet: could not cache news.json: %s", exc)
        log.info("outlet: %d headlines, %d upcoming / %d reported earnings",
                 len(items), len(earnings.get("upcoming", [])), len(earnings.get("reported", [])))
        return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    feed = NewsFeed()
    feed._refresh()
    snap = feed.snapshot()
    print(f"{snap['meta']['items']} items · earnings "
          f"{len(snap['earnings']['upcoming'])} upcoming / {len(snap['earnings']['reported'])} reported")
    for item in snap["items"][:8]:
        print(f"  [{item['kind']:7}] {item['tag']:6} {item['title'][:60]} · {item['publisher']}")
