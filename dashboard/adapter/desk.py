"""Desk context: the once-a-day yfinance pull behind Income and Desk features.

    /opt/anaconda3/bin/python3 adapter/desk.py        # fetch now, print summary

One batch across the owned holdings — dividend history, the next earnings
date, and recent news — written to data/desk.json and served statically by
serve.py. Deliberately not part of the live feed: none of this moves
intraday, and yfinance is slow enough that fetching it on request would hold
an HTTP worker for the better part of a minute. refresh.py runs it daily
behind the same kind of staleness gate the symbol directory uses.

Every symbol is fetched inside its own try/except: Yahoo routinely has data
for fourteen names and a tantrum about the fifteenth, and one failure must
cost one holding's panel, not the file. A failed run keeps the previous
desk.json — stale beats blank, per the house pattern.

Symbols come from universe.Ticker.symbol — the same yfinance mapping the
watchlist uses, with its hard-won gotchas (SMSN.IL not .L, HY9H.F not .DE).
Dividend amounts stay in the currency Yahoo quotes them in; conversion to
GBP happens at request time against the live feed's FX map, because a
projection made at today's rate should move with today's rate. LSE pence are
scaled here (÷100) so a GBp amount never escapes upstream.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import universe

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DESK_PATH = DATA_DIR / "desk.json"

NEWS_PER_SYMBOL = 8
FETCH_PAUSE = 0.4          # be a polite Yahoo citizen across ~15 symbols

log = logging.getLogger("desk")


def stale(hours: float = 20) -> bool:
    """Whether the desk file is old enough to re-fetch. Mirrors directory.stale."""
    if not DESK_PATH.exists():
        return True
    return (time.time() - DESK_PATH.stat().st_mtime) >= hours * 3600


def _iso(value) -> str | None:
    try:
        return value.date().isoformat() if hasattr(value, "date") else value.isoformat()
    except Exception:
        return None


def _fetch_symbol(yf, ticker: "universe.Ticker") -> dict:
    """One holding's desk block. Raises on total failure; partial is fine."""
    t = yf.Ticker(ticker.symbol)
    out: dict = {"symbol": ticker.symbol, "currency": ticker.currency or ""}

    # Yahoo quotes LSE lines in pence; nothing downstream should ever see GBp.
    try:
        quote_ccy = (t.fast_info.get("currency") or ticker.currency or "").strip()
    except Exception:
        quote_ccy = ticker.currency or ""
    pence = quote_ccy in ("GBp", "GBX")
    out["currency"] = "GBP" if pence else (quote_ccy or ticker.currency or "")

    # ---- dividends: full history, oldest first ----
    try:
        series = t.dividends
        out["dividends"] = [
            {"date": _iso(stamp), "amount": round(float(amount) / (100 if pence else 1), 6)}
            for stamp, amount in series.items()
            if amount and _iso(stamp)
        ]
    except Exception as exc:
        log.debug("%s dividends: %s", ticker.key, exc)
        out["dividends"] = []

    # ---- calendar: next earnings + declared ex-div date ----
    try:
        cal = t.calendar or {}
        dates = cal.get("Earnings Date") or []
        out["next_earnings"] = sorted(filter(None, (_iso(d) for d in dates)))
        out["next_ex_div"] = _iso(cal.get("Ex-Dividend Date"))
    except Exception as exc:
        log.debug("%s calendar: %s", ticker.key, exc)
        out["next_earnings"] = []
        out["next_ex_div"] = None

    # ---- news ----
    items = []
    try:
        for raw in (t.news or [])[:NEWS_PER_SYMBOL]:
            c = raw.get("content") or raw
            title = (c.get("title") or "").strip()
            if not title:
                continue
            url = ((c.get("canonicalUrl") or {}).get("url")
                   or (c.get("clickThroughUrl") or {}).get("url") or "")
            items.append({
                "title": title,
                "url": url,
                "publisher": ((c.get("provider") or {}).get("displayName") or "").strip(),
                "at": (c.get("pubDate") or "").strip(),
            })
    except Exception as exc:
        log.debug("%s news: %s", ticker.key, exc)
    out["news"] = items

    return out


def fetch() -> dict:
    """The whole desk file. Per-symbol failures are recorded, never fatal."""
    import yfinance as yf   # deferred: heavy, and desk.py is imported by serve.py

    holdings: dict[str, dict] = {}
    errors: dict[str, str] = {}
    for ticker in universe.TICKERS.values():
        if not ticker.owned:
            continue                      # desk context covers the book, not the watchlist
        try:
            holdings[ticker.key] = _fetch_symbol(yf, ticker)
        except Exception as exc:
            errors[ticker.key] = str(exc)
            log.warning("desk fetch failed for %s: %s", ticker.key, exc)
        time.sleep(FETCH_PAUSE)

    return {
        "meta": {
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "holdings": len(holdings),
            "errors": errors,
        },
        "holdings": holdings,
    }


def refresh_if_stale(hours: float = 20) -> bool:
    """The daily-job entry point. Returns whether a fetch ran."""
    if not stale(hours):
        log.info("desk: fresh, skipping")
        return False
    payload = fetch()
    if not payload["holdings"]:
        # Yahoo entirely down: keep yesterday's file rather than writing an
        # empty one — stale beats blank.
        log.warning("desk: fetch returned nothing; keeping the previous file")
        return False
    DESK_PATH.parent.mkdir(parents=True, exist_ok=True)
    DESK_PATH.write_text(json.dumps(payload, indent=1))
    log.info("desk: %d holdings, %d errors", len(payload["holdings"]),
             len(payload["meta"]["errors"]))
    return True


def read() -> dict | None:
    """The stored file, or None. serve.py's read path."""
    if not DESK_PATH.exists():
        return None
    try:
        return json.loads(DESK_PATH.read_text())
    except json.JSONDecodeError:
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ran = refresh_if_stale(hours=0)
    data = read() or {}
    for key, block in sorted((data.get("holdings") or {}).items()):
        print(f"{key:>6}: {len(block.get('dividends') or []):>3} divs · "
              f"earnings {block.get('next_earnings') or '—'} · "
              f"{len(block.get('news') or [])} news")
