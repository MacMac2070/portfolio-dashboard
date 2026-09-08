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

import resilience
import store
import universe

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DESK_PATH = DATA_DIR / "desk.json"
BREAKER = "yfinance"       # shared with every other module that asks Yahoo

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
    breaker = resilience.get(BREAKER)
    if not breaker.allow():
        # Yahoo is known to be down: fifteen more failures would teach nothing,
        # and an empty result keeps the previous file (see refresh_if_stale).
        log.warning("desk: %s; keeping the previous file", breaker.reason())
        errors["yfinance"] = breaker.reason()
        return {"meta": {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         "holdings": 0, "errors": errors}, "holdings": holdings}
    for ticker in universe.TICKERS.values():
        if not ticker.owned:
            continue                      # desk context covers the book, not the watchlist
        try:
            holdings[ticker.key] = _fetch_symbol(yf, ticker)
        except Exception as exc:
            errors[ticker.key] = str(exc)
            log.warning("desk fetch failed for %s: %s", ticker.key, exc)
        time.sleep(FETCH_PAUSE)

    if holdings:
        breaker.record_success()
    else:
        breaker.record_failure(next(iter(errors.values()), "no holdings fetched"))
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
    store.write_json(DESK_PATH, payload, indent=1)
    log.info("desk: %d holdings, %d errors", len(payload["holdings"]),
             len(payload["meta"]["errors"]))
    return True


CLOSE_PATH = DATA_DIR / "close_snapshot.json"


def write_close_snapshot(payload: dict) -> None:
    """The overnight baseline: whatever the daily job just built.

    Written by refresh.py after a successful build, so "overnight" means
    "since the last 23:30 run" — which is exactly the HK-open-while-London-
    sleeps window the diff exists for."""
    slim = {
        "date": payload.get("meta", {}).get("generated_at"),
        "kpis": payload.get("kpis") or {},
        "positions": [
            {"symbol": p.get("symbol"), "value_gbp": p.get("value_gbp"),
             "quantity": p.get("quantity")}
            for p in payload.get("positions") or []
        ],
    }
    store.write_json(CLOSE_PATH, slim, indent=1)


def overnight(live: dict | None, cash_rows: list[dict]) -> dict | None:
    """What changed since the baseline, split market-vs-flows.

    Returns None when there is no baseline yet or nothing live to diff — the
    UI renders its designed empty state, not an invented zero."""
    if not CLOSE_PATH.exists() or not live:
        return None
    try:
        base = json.loads(CLOSE_PATH.read_text())
    except json.JSONDecodeError:
        return None

    base_nav = (base.get("kpis") or {}).get("net_liquidation")
    now_nav = (live.get("kpis") or {}).get("net_liquidation")
    if not base_nav or not now_nav:
        return None

    base_date = (base.get("date") or "")[:10]
    flows = sum((r.get("amount_gbp") or 0.0) for r in cash_rows
                if r.get("bucket") == "deposits_withdrawals"
                and (r.get("date") or "") > base_date)

    base_pos = {p["symbol"]: p for p in base.get("positions") or []}
    now_pos = {p.get("symbol"): p for p in live.get("positions") or []}
    base_inv = sum(p.get("value_gbp") or 0 for p in base_pos.values()) or 1
    now_inv = sum(p.get("value_gbp") or 0 for p in now_pos.values()) or 1

    shifts = []
    for sym in set(base_pos) | set(now_pos):
        b = (base_pos.get(sym, {}).get("value_gbp") or 0)
        n = (now_pos.get(sym, {}).get("value_gbp") or 0)
        shifts.append({
            "symbol": sym,
            "w_from": round(b / base_inv * 100, 1),
            "w_to": round(n / now_inv * 100, 1),
            "delta_gbp": round(n - b, 2),
        })
    shifts.sort(key=lambda x: -abs(x["delta_gbp"]))

    return {
        "since": base_date,
        "nav_delta": round(now_nav - base_nav, 2),
        "flows": round(flows, 2),
        "market_delta": round(now_nav - base_nav - flows, 2),
        "shifts": shifts[:6],
    }


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
