"""openbb -> FX rates and prior closes.

Two jobs, both of which must degrade rather than fail: this module runs from a
scheduled job with nobody watching, so a flaky network call is allowed to cost
precision but never the whole snapshot.

  FX          openbb first, IBKR's own ExchangeRate rows as fallback. The two
              agree to ~4dp (openbb USD->GBP 0.75072 vs IBKR 0.7505896).

  Day change  IBKR's per-position dailyPnL is not usable here: HK and SGX lines
              come back as warning 2150 "Invalid position trade derived value"
              and US lines often haven't streamed by the time we read them. So
              day change is computed from prices instead.

Three gotchas, all found by probing rather than assuming:

  GBp not GBP     LSE quotes in pence. HSBA.L prints 1552.80 against IBKR's
                  15.5247. Divide by 100 or every LSE move is 100x wrong.
  SMSN.IL         Samsung's tradeable IOB line. SMSN.L exists but is stale —
                  it printed an unchanged 1179.50 while .IL moved -4.19%.
  HY9H.F          SK hynix on Frankfurt. HY9H.DE returns nothing at all.
"""
from __future__ import annotations

import logging
import math

import providers
import quotes
import resilience

log = logging.getLogger(__name__)

# IBKR conId -> yfinance symbol. conId because it is stable; see regions.py.
QUOTE_SYMBOLS: dict[int, str] = {
    265598:    "AAPL",
    3691937:   "AMZN",
    208813719: "GOOGL",
    107113386: "META",
    270639:    "INTC",
    909083:    "HSBA.L",
    123279007: "XDJP.L",
    270617971: "IUCS.L",
    16520545:  "SMSN.IL",
    517397504: "HY9H.F",
    1616420:   "0293.HK",
    152791428: "0700.HK",
    256718140: "3115.HK",
    92216536:  "C6L.SI",
    92214874:  "ES3.SI",
}

BASE = "GBP"
FX_CURRENCIES = ("USD", "HKD", "EUR", "SGD", "JPY")
BREAKER = "yfinance"        # shared with every other module that asks Yahoo


def _obb():
    return providers.obb()


def _finite(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def fx_rates(fallback: dict[str, float]) -> tuple[dict[str, float], str]:
    """Return {currency: rate into GBP}, and which source supplied it.

    openbb quotes GBP as the base (GBPUSD = dollars per pound), so the rate we
    want is its reciprocal.
    """
    rates = {BASE: 1.0}
    breaker = resilience.get(BREAKER)
    if not breaker.allow():
        log.info("FX: %s; using the broker's rates", breaker.reason())
    else:
        try:
            obb = _obb()
            for currency in FX_CURRENCIES:
                try:
                    df = obb.currency.price.historical(
                        symbol=f"{BASE}{currency}", provider="yfinance", interval="1d")
                    close = _finite(df["close"].iloc[-1])
                    if close and close > 0:
                        rates[currency] = 1.0 / close
                except Exception as exc:
                    log.debug("openbb FX %s%s failed: %s", BASE, currency, exc)
            if len(rates) > 1:
                breaker.record_success()
            else:
                breaker.record_failure("no FX rows")
        except Exception as exc:
            log.warning("openbb unavailable for FX: %s", exc)
            breaker.record_failure(exc)

    missing = [c for c in FX_CURRENCIES if c not in rates]
    if missing:
        # Second source before the broker's account rates: the ECB's daily
        # reference rates, keyless and covering every currency here.
        try:
            ecb = providers.fx_ecb(missing)
            for currency in list(missing):
                if currency in ecb:
                    rates[currency] = ecb[currency]
                    missing.remove(currency)
            if ecb:
                log.info("FX from ECB reference rates for: %s", ", ".join(sorted(ecb)))
        except Exception as exc:
            log.info("ECB reference rates unavailable: %s", str(exc)[:120])
    used_ecb = any(c in rates for c in FX_CURRENCIES) and missing != [c for c in FX_CURRENCIES if c not in rates]
    if missing:
        for currency in missing:
            if currency in fallback:
                rates[currency] = fallback[currency]
        source = "ibkr" if len(missing) == len(FX_CURRENCIES) else "openbb+ibkr"
        log.info("FX fell back to IBKR rates for: %s", ", ".join(missing))
    else:
        source = "openbb+ecb" if used_ecb else "openbb"

    for currency, rate in fallback.items():
        rates.setdefault(currency, rate)
    return rates, source


def prior_closes(con_ids: list[int]) -> dict[int, dict]:
    """Fetch last price and prior close per position, normalised to major units.

    Returns {con_id: {"last": float|None, "prev": float|None, "symbol": str}}.
    Either value may be None — thinly-quoted ETFs routinely return a prior close
    with no live price, which the caller repairs using IBKR's market price.
    """
    wanted = {c: QUOTE_SYMBOLS[c] for c in con_ids if c in QUOTE_SYMBOLS}
    if not wanted:
        return {}

    out: dict[int, dict] = {}
    breaker = resilience.get(BREAKER)
    if not breaker.allow():
        log.info("quotes: %s; day change will fall back to IBKR", breaker.reason())
        return out

    obb = _obb()
    last_exc: Exception | str | None = None
    for tag, kwargs in providers.tiers("quote"):
        try:
            df = obb.equity.price.quote(symbol=",".join(wanted.values()), **kwargs)
            by_symbol = {}
            for row in df.to_dict("records"):
                symbol = row.get("symbol")
                if not symbol:
                    continue
                # GBp is pence; IBKR reports pounds.
                divisor = 100.0 if str(row.get("currency", "")).strip() == "GBp" else 1.0
                last = _finite(row.get("last_price"))
                prev = _finite(row.get("prev_close"))
                by_symbol[symbol] = {
                    "last": last / divisor if last is not None else None,
                    "prev": prev / divisor if prev is not None else None,
                }
            for con_id, symbol in wanted.items():
                hit = by_symbol.get(symbol)
                if hit:
                    out[con_id] = {**hit, "symbol": symbol}
            if out:
                breaker.record_success()
                return out
        except Exception as exc:
            last_exc = exc
            log.debug("%s quotes failed: %s", tag, exc)

    log.warning("openbb quotes unavailable from every tier, day change will fall back to IBKR: %s",
                last_exc)
    breaker.record_failure(last_exc or "no tier returned data")
    return out


def price_series(con_ids: list[int], days: int = 30) -> dict[int, list[float]]:
    """Daily closes per position, for in-row sparklines.

    Returns {con_id: [close, ...]} oldest first. Symbols that fail are simply
    absent — a missing sparkline renders as empty space, it does not block the
    row. Values stay in their native unit because a sparkline is scaled to its
    own min/max; only the shape matters.
    """
    from datetime import date, timedelta

    wanted = {c: QUOTE_SYMBOLS[c] for c in con_ids if c in QUOTE_SYMBOLS}
    if not wanted:
        return {}

    window = (date.today() - timedelta(days=days * 2 + 10)).isoformat()
    out: dict[int, list[float]] = {}
    breaker = resilience.get(BREAKER)
    if breaker.allow():
        obb = _obb()
        last_exc: Exception | str | None = None
        fetched = False
        for tag, kwargs in providers.tiers("history"):
            try:
                df = obb.equity.price.historical(
                    symbol=",".join(wanted.values()),
                    start_date=quotes.plan_fetch_all(list(wanted.values()), full_days=days * 2 + 10),
                    interval="1d", **kwargs)
                df = df.reset_index()
                if "symbol" in df.columns:
                    grouped = {str(sym): sub for sym, sub in df.groupby("symbol")}
                else:
                    # A single symbol comes back without a symbol column.
                    grouped = {next(iter(wanted.values())): df}
                for symbol, sub in grouped.items():
                    if "close" in sub and "date" in sub:
                        quotes.upsert(symbol, zip((str(d)[:10] for d in sub["date"]), sub["close"]),
                                      source=tag)
                fetched = True
                breaker.record_success()
                break
            except Exception as exc:
                last_exc = exc
                log.debug("%s history failed: %s", tag, exc)
        if not fetched:
            log.warning("openbb history unavailable from every tier, sparklines from the store: %s",
                        last_exc)
            breaker.record_failure(last_exc or "no tier returned data")
    else:
        log.info("history: %s; sparklines from the store", breaker.reason())

    # Whatever the store holds now, fetched today or earlier.
    for con_id, symbol in wanted.items():
        closes = [c for _, c in quotes.series(symbol, start=window)]
        if len(closes) >= 2:
            out[con_id] = closes[-days:]
    return out


def day_change_pct(prev_close: float | None,
                   last_price: float | None,
                   ibkr_price: float | None,
                   ibkr_daily_pnl: float | None,
                   ibkr_market_value: float | None) -> tuple[float | None, str]:
    """Day change as a percentage, best source first.

    1. openbb last vs openbb prior close
    2. IBKR live price vs openbb prior close  (thin ETFs: prior close but no quote)
    3. IBKR dailyPnL as a share of yesterday's value

    Returns (percent, source). Percent is None when no source could supply one —
    the UI shows a dash rather than inventing a zero.
    """
    if prev_close and prev_close > 0:
        if last_price is not None:
            return (last_price / prev_close - 1.0) * 100.0, "openbb"
        if ibkr_price is not None:
            return (ibkr_price / prev_close - 1.0) * 100.0, "openbb+ibkr"

    if ibkr_daily_pnl is not None and ibkr_market_value is not None:
        opening = ibkr_market_value - ibkr_daily_pnl
        if abs(opening) > 1e-9 and abs(ibkr_daily_pnl) > 1e-9:
            return ibkr_daily_pnl / opening * 100.0, "ibkr"

    return None, "none"
