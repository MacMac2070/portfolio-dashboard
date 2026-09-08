"""Company financial statements. Serves /api/financials/<key>.

Sixth independent source, and the second click-driven one after instrument.py.
Everything about the warmup thread, the gate and openbb's import cost is the
story instrument.py's docstring already tells; this module repeats the
mechanism, not the reasoning.

Three deliberate differences from instrument.py
----------------------------------------------
**yfinance only for the three statements, no default tier.** The line-item
curation below is written against yfinance's field vocabulary. The default tier
for these commands resolves to fmp (402 on this free key), then intrinio (no
key), then sec — and sec answers with an entirely different vocabulary, which
would render as a table with three rows in it and read as "this company reports
almost nothing". An honest "unavailable" beats a misleading table. `metrics`
keeps the two-tier fallback because its field names are standardised.

**One request slot, not two.** instrument.py holds `BoundedSemaphore(2)`; this
holds one, so the ceiling across both openbb consumers is three concurrent
fan-outs rather than four. That semaphore exists to keep the IB feed's asyncio
loop breathing, and a second click-driven service must not quietly double the
pressure on it.

**Long TTLs.** A quote moves every second; a statement moves once a quarter.

Currency: never scaled, and not the quote currency
--------------------------------------------------
Statements are reported in the company's *reporting* currency, which is a
different thing from what the share quotes in:

    HSBA.L   reports USD, quotes GBp
    IAG.L    reports EUR, quotes GBp
    0700.HK  reports CNY, quotes HKD      <- looks like a match, is not

So `units.scale_for()` and every one of its helpers are **never** called on a
figure in this module. They are already in major units, and applying the GBp
divisor would be wrong twice over — see units.ALREADY_MAJOR, which documents the
same reasoning for market cap and EPS.

The reporting currency has no source in the statement responses at all. It comes
from `metrics.currency`, which the yfinance model aliases to `financialCurrency`.
If that call fails the currency is null and the page labels its units without a
glyph rather than guessing one.

Why a missing row means something
---------------------------------
openbb runs `df.dropna(axis=1, how="all")` on the result, so a line item the
company never reports is dropped as a column. That makes "column absent" a
clean, free signal: HSBC has no gross profit, no operating income and no EBITDA
because it is a bank, and this module renders none of those rows for it rather
than printing an em-dash that implies the number exists and is merely missing.
Individual cells inside a present column can still be NaN — that is a gap, and
does render as an em-dash.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import directory
import resilience
import universe

log = logging.getLogger("financials")

# --------------------------------------------------------------------------
# Tuning
# --------------------------------------------------------------------------
TTL = {
    "income": 12 * 3600.0,
    "balance": 12 * 3600.0,
    "cash": 12 * 3600.0,
    "metrics": 6 * 3600.0,
}
MAX_ENTRIES = 512
NEG_TTL = 15 * 60.0       # an empty statement is remembered this long, not 12 h and not 0
BREAKER = "yfinance"      # shared with every other module that asks Yahoo
FOLLOWER_WAIT = 8.0
REQUEST_BUDGET = 12.0     # statements are slower than quotes; three calls deep
GATE_TIMEOUT = 90.0
# yfinance's own models declare `limit: int | None = Field(default=5, le=5)`, so
# five is both the maximum and everything there is. Passing more raises.
LIMIT = 5

PERIODS = ("annual", "quarter")
STATEMENTS = ("income", "balance", "cash")

STATEMENT_LABEL = {
    "income": "Income statement",
    "balance": "Balance sheet",
    "cash": "Cash flow",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _finite(value):
    """A JSON-safe float, or None.

    Load-bearing, not defensive. Three things arrive from pandas that
    `json.dumps` cannot emit and the browser cannot parse:

      NaN            -> json.dumps writes a bare `NaN`, which JSON.parse
                        REJECTS, killing the whole response rather than one
                        cell. HSBA.L's oldest total_assets is genuinely NaN.
      numpy.float64  -> not JSON serialisable at all
      datetime.date  -> likewise; period_ending is one (handled by _stamp)
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _stamp(value) -> str:
    """period_ending arrives as a datetime.date, which json.dumps refuses."""
    return str(value)[:10] if value is not None else ""


# --------------------------------------------------------------------------
# Line-item curation
#
# (field, label, kind, indent). `kind` picks the formatter on the page:
#   money      one shared unit per statement, e.g. "52,853"
#   per_share  the reporting currency at 2dp, e.g. "−$0.06"
#   shares     compact, e.g. "4.53B"
#
# Every field below was probed present on at least one of INTC / 0700.HK /
# HSBA.L / C6L.SI. A field absent for a given company is dropped, not blanked.
# --------------------------------------------------------------------------
INCOME_ROWS = [
    ("Revenue", [
        ("total_revenue", "Total revenue", "money", 0),
        ("operating_revenue", "Operating revenue", "money", 1),
        ("cost_of_revenue", "Cost of revenue", "money", 1),
        ("gross_profit", "Gross profit", "money", 0),
    ]),
    ("Operating", [
        ("research_and_development_expense", "Research and development", "money", 1),
        ("selling_general_and_admin_expense", "Selling, general and admin", "money", 1),
        ("operating_expense", "Total operating expense", "money", 1),
        ("operating_income", "Operating income", "money", 0),
        ("ebitda", "EBITDA", "money", 1),
        ("ebit", "EBIT", "money", 1),
    ]),
    ("Interest and other", [
        ("net_interest_income", "Net interest income", "money", 1),
        ("interest_income", "Interest income", "money", 1),
        ("interest_expense", "Interest expense", "money", 1),
        ("other_income_expense", "Other income and expense", "money", 1),
    ]),
    ("Earnings", [
        ("total_pre_tax_income", "Pre-tax income", "money", 0),
        ("tax_provision", "Tax provision", "money", 1),
        ("net_income", "Net income", "money", 0),
        ("net_income_attributable_to_common_shareholders",
         "Attributable to common shareholders", "money", 1),
        ("minority_interests", "Minority interests", "money", 1),
        ("normalized_income", "Normalised income", "money", 1),
    ]),
    ("Per share", [
        ("basic_earnings_per_share", "Basic EPS", "per_share", 0),
        ("diluted_earnings_per_share", "Diluted EPS", "per_share", 0),
        ("weighted_average_basic_shares_outstanding", "Basic shares", "shares", 1),
        ("weighted_average_diluted_shares_outstanding", "Diluted shares", "shares", 1),
    ]),
]

BALANCE_ROWS = [
    ("Assets", [
        ("cash_and_cash_equivalents", "Cash and equivalents", "money", 1),
        ("short_term_investments", "Short-term investments", "money", 1),
        ("cash_cash_equivalents_and_short_term_investments",
         "Cash and short-term investments", "money", 0),
        ("net_receivables", "Receivables", "money", 1),
        ("inventories", "Inventories", "money", 1),
        ("other_current_assets", "Other current assets", "money", 1),
        ("total_current_assets", "Total current assets", "money", 0),
        ("plant_property_equipment_net", "Property, plant and equipment", "money", 1),
        ("goodwill", "Goodwill", "money", 1),
        ("other_intangible_assets", "Other intangibles", "money", 1),
        ("goodwill_and_other_intangible_assets", "Goodwill and intangibles", "money", 1),
        ("long_term_equity_investment", "Long-term investments", "money", 1),
        ("investments_and_advances", "Investments and advances", "money", 1),
        ("total_non_current_assets", "Total non-current assets", "money", 0),
        ("total_assets", "Total assets", "money", 0),
    ]),
    ("Liabilities", [
        ("payables", "Payables", "money", 1),
        ("current_debt", "Current debt", "money", 1),
        ("current_liabilities", "Total current liabilities", "money", 0),
        ("long_term_debt", "Long-term debt", "money", 1),
        ("total_non_current_liabilities_net_minority_interest",
         "Total non-current liabilities", "money", 0),
        ("total_liabilities_net_minority_interest", "Total liabilities", "money", 0),
        ("total_debt", "Total debt", "money", 1),
        ("net_debt", "Net debt", "money", 1),
    ]),
    ("Equity", [
        ("common_stock", "Common stock", "money", 1),
        ("retained_earnings", "Retained earnings", "money", 1),
        ("total_common_equity", "Total common equity", "money", 0),
        ("minority_interest", "Minority interest", "money", 1),
        ("total_equity_non_controlling_interests", "Total equity", "money", 0),
        ("tangible_book_value", "Tangible book value", "money", 1),
        ("working_capital", "Working capital", "money", 1),
        ("invested_capital", "Invested capital", "money", 1),
        ("total_capitalization", "Total capitalisation", "money", 1),
        ("ordinary_shares_number", "Ordinary shares", "shares", 1),
        ("treasury_shares_number", "Treasury shares", "shares", 1),
    ]),
]

CASH_ROWS = [
    ("Operating", [
        ("net_income_from_continuing_operations", "Net income", "money", 1),
        ("depreciation_and_amortization", "Depreciation and amortisation", "money", 1),
        ("stock_based_compensation", "Stock-based compensation", "money", 1),
        ("deferred_income_tax", "Deferred income tax", "money", 1),
        ("change_in_working_capital", "Change in working capital", "money", 1),
        ("other_non_cash_items", "Other non-cash items", "money", 1),
        ("operating_cash_flow", "Operating cash flow", "money", 0),
    ]),
    ("Investing", [
        ("capital_expenditure", "Capital expenditure", "money", 1),
        ("investments_in_property_plant_and_equipment",
         "Investment in PP&E", "money", 1),
        ("purchase_of_investment", "Purchase of investments", "money", 1),
        ("sale_of_investment", "Sale of investments", "money", 1),
        ("net_business_purchase_and_sale", "Net business acquisitions", "money", 1),
        ("investing_cash_flow", "Investing cash flow", "money", 0),
    ]),
    ("Financing", [
        ("issuance_of_debt", "Issuance of debt", "money", 1),
        ("repayment_of_debt", "Repayment of debt", "money", 1),
        ("net_issuance_payments_of_debt", "Net debt issuance", "money", 1),
        ("issuance_of_common_equity", "Issuance of equity", "money", 1),
        ("repurchase_of_common_equity", "Buybacks", "money", 1),
        ("cash_dividends_paid", "Dividends paid", "money", 1),
        ("net_other_financing_charges", "Other financing", "money", 1),
        ("financing_cash_flow", "Financing cash flow", "money", 0),
    ]),
    ("Net change", [
        ("effect_of_exchange_rate_changes", "Effect of exchange rates", "money", 1),
        ("net_change_in_cash_and_equivalents", "Net change in cash", "money", 0),
        ("beginning_cash_position", "Cash at start of period", "money", 1),
        ("end_cash_position", "Cash at end of period", "money", 0),
        ("free_cash_flow", "Free cash flow", "money", 0),
    ]),
]

ROWS = {"income": INCOME_ROWS, "balance": BALANCE_ROWS, "cash": CASH_ROWS}

# Rows we compute, because no free provider serves ratios — equity.fundamental
# .ratios is fmp/intrinio only. Each is (label, numerator, denominator).
MARGINS = {
    "income": [
        ("Gross margin", "gross_profit", "total_revenue"),
        ("Operating margin", "operating_income", "total_revenue"),
        ("Net margin", "net_income", "total_revenue"),
    ],
    "balance": [
        ("Debt to equity", "total_debt", "total_equity_non_controlling_interests"),
    ],
    "cash": [
        ("Free cash flow margin", "free_cash_flow", "operating_cash_flow"),
    ],
}

# The series the trend chart draws, per statement. Drawn only if present — a
# company that does not report one simply has a shorter legend, never a
# substituted line that means something else.
CHART_SERIES = {
    "income": [("total_revenue", "Revenue"), ("operating_income", "Operating income"),
               ("net_income", "Net income")],
    "balance": [("total_assets", "Assets"),
                ("total_liabilities_net_minority_interest", "Liabilities"),
                ("total_equity_non_controlling_interests", "Equity")],
    "cash": [("operating_cash_flow", "Operating"), ("investing_cash_flow", "Investing"),
             ("financing_cash_flow", "Financing")],
}


@dataclass
class _Entry:
    value: Any
    stored: float
    ttl: float
    provider: str
    errors: list = field(default_factory=list)

    def fresh(self) -> bool:
        return (time.monotonic() - self.stored) < self.ttl


class FinancialsService:
    """Cached statements. Never raises, never blocks the IB feed."""

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
        self._slots = threading.BoundedSemaphore(1)
        self._warm_error: str | None = None

    # ---------------- lifecycle ----------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._warmup, name="financials-warmup",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)
        if self._thread:
            self._thread.join(timeout=8)

    def _warmup(self) -> None:
        if self._gate:
            deadline = time.monotonic() + self._gate_timeout
            opened = False
            while not self._stop.is_set() and time.monotonic() < deadline:
                try:
                    if self._gate():
                        opened = True
                        break
                except Exception:
                    opened = True
                    break
                self._stop.wait(1.0)
            if not opened:
                log.warning("financials: gate timed out after %.0fs, warming anyway",
                            self._gate_timeout)
        if self._stop.is_set():
            return
        try:
            t0 = time.monotonic()
            self._obb()
            self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="fin")
            self._ready.set()
            log.info("financials: openbb ready in %.1fs", time.monotonic() - t0)
        except Exception as exc:
            self._warm_error = str(exc)
            log.error("financials: openbb import failed: %s", exc)

    @staticmethod
    def _obb():
        from openbb import obb
        obb.user.preferences.output_type = "dataframe"
        return obb

    # ---------------- cache ----------------

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
        """Single-flight read-through, same shape as instrument.py's."""
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
            # Funds file no statements; Yahoo says so with a 404 every time.
            # Remember the empty answer for NEG_TTL rather than re-asking on
            # every click, but not for the full TTL in case it was an outage.
            empty = value is None or (hasattr(value, "__len__") and len(value) == 0)
            entry = _Entry(value, time.monotonic(), NEG_TTL if empty else ttl, provider, errors)
            with self._lock:
                self._cache[key] = entry
                self._evict()
            return entry
        except Exception as exc:
            log.exception("financials: producer failed for %s", key)
            with self._lock:
                stale = self._cache.get(key)
            if stale:
                stale.errors = list(stale.errors) + [("producer", str(exc)[:160])]
            return stale
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            waiter.set()

    # ---------------- producers ----------------

    @staticmethod
    def _records(df) -> list[dict]:
        try:
            return df.reset_index().to_dict("records")
        except Exception:
            return df.to_dict("records")

    def _statement(self, symbol: str, which: str, period: str):
        """One statement. yfinance only — see the module docstring."""
        obb = self._obb()
        call = {"income": obb.equity.fundamental.income,
                "balance": obb.equity.fundamental.balance,
                "cash": obb.equity.fundamental.cash}[which]
        breaker = resilience.get(BREAKER)
        if not breaker.allow():
            return None, "none", [("yfinance", breaker.reason())]
        try:
            df = call(symbol=symbol, provider="yfinance", period=period, limit=LIMIT)
        except Exception as exc:
            breaker.record_failure(exc)
            return None, "none", [("yfinance", f"{type(exc).__name__}: {exc}"[:160])]
        breaker.record_success()
        if df is None or not len(df):
            return None, "none", [("yfinance", "empty")]
        return self._records(df), "yfinance", []

    def _metrics(self, symbol: str):
        """Only for the reporting currency. Two-tier, since its names are standard."""
        obb = self._obb()
        errors = []
        breaker = resilience.get(BREAKER)
        for tag, kwargs in (("yfinance", {"provider": "yfinance"}), ("default", {})):
            if tag == "yfinance" and not breaker.allow():
                errors.append((tag, breaker.reason()))
                continue
            try:
                df = obb.equity.fundamental.metrics(symbol=symbol, **kwargs)
                if tag == "yfinance":
                    breaker.record_success()
                recs = self._records(df)
                if recs and recs[0].get("currency"):
                    return recs, tag, errors
                errors.append((tag, "no currency"))
            except Exception as exc:
                if tag == "yfinance":
                    breaker.record_failure(exc)
                errors.append((tag, f"{type(exc).__name__}: {exc}"[:160]))
        return None, "none", errors

    # ---------------- shaping ----------------

    @staticmethod
    def _shape(records: list[dict], which: str) -> dict:
        """Records -> periods + sections of rows, newest first.

        The provider already returns newest-first and the page renders the
        tables that way (finance convention), so no reversal happens here. The
        trend chart reverses for itself — a time axis reads left to right.
        """
        if not records:
            return {"available": False, "periods": [], "sections": [], "reason": "no data"}

        periods = [_stamp(r.get("period_ending")) for r in records]
        present = set()
        for r in records:
            present.update(k for k, v in r.items() if v is not None)

        sections = []
        for title, specs in ROWS[which]:
            rows = []
            for fieldname, label, kind, indent in specs:
                if fieldname not in present:
                    continue          # the company does not report this line
                values = [_finite(r.get(fieldname)) for r in records]
                if not any(v is not None for v in values):
                    continue
                rows.append({"field": fieldname, "label": label, "kind": kind,
                             "indent": indent, "values": values})
            if rows:
                sections.append({"title": title, "rows": rows})

        # Derived rows, clearly marked. No provider serves ratios on this install.
        margins = []
        for label, num, den in MARGINS.get(which, []):
            if num not in present or den not in present:
                continue
            values = []
            for r in records:
                n, d = _finite(r.get(num)), _finite(r.get(den))
                values.append((n / d * 100.0) if (n is not None and d) else None)
            if any(v is not None for v in values):
                margins.append({"field": f"margin_{num}", "label": label, "kind": "pct",
                                "indent": 0, "values": values, "derived": True})
        if margins:
            sections.append({"title": "Margins and ratios", "rows": margins,
                             "derived": True})

        chart = []
        for fieldname, label in CHART_SERIES.get(which, []):
            if fieldname not in present:
                continue
            values = [_finite(r.get(fieldname)) for r in records]
            if any(v is not None for v in values):
                chart.append({"key": fieldname, "label": label, "values": values})

        return {"available": bool(sections), "periods": periods,
                "sections": sections, "chart": chart, "reason": None}

    # ---------------- compose ----------------

    def snapshot(self, key: str, period: str = "annual") -> dict:
        if period not in PERIODS:
            period = "annual"
        ticker = self._resolve(key)
        if ticker is None:
            return _empty(key, f"unknown ticker {key!r}", period=period)

        if not self._ready.is_set():
            return _empty(key, self._warm_error, state="warming", retry_after=2,
                          ticker=ticker, period=period)
        if not self._slots.acquire(timeout=1.0):
            return _empty(key, "service busy", state="busy", retry_after=1,
                          ticker=ticker, period=period)
        try:
            return self._compose(ticker, period)
        finally:
            self._slots.release()

    @staticmethod
    def _resolve(key: str) -> universe.Ticker | None:
        t = universe.TICKERS.get(key)
        if t is not None:
            return t
        try:
            row = directory.row(key)
        except Exception:
            return None
        if not row:
            return None
        return universe.synthetic(
            row["symbol"], row.get("name") or "",
            exchange=row.get("exchange") or "", currency=row.get("currency") or "",
            region=row.get("region") or "")

    def _compose(self, t: universe.Ticker, period: str) -> dict:
        symbol = t.symbol
        jobs = {
            **{w: (f"{symbol}|{w}|{period}", TTL[w],
                   (lambda w=w: self._statement(symbol, w, period)))
               for w in STATEMENTS},
            "metrics": (f"{symbol}|metrics", TTL["metrics"], lambda: self._metrics(symbol)),
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
            pass
        for name in jobs:
            if name not in entries:
                pending.append(name)
                with self._lock:
                    entries[name] = self._cache.get(jobs[name][0])

        metrics_entry = entries.get("metrics")
        metrics = (metrics_entry.value or [{}])[0] if metrics_entry and metrics_entry.value else {}
        # financialCurrency, NOT the quote currency. See the module docstring.
        reporting = (metrics.get("currency") or "").strip() or None

        statements = {}
        for w in STATEMENTS:
            e = entries.get(w)
            shaped = self._shape(e.value if e else None, w)
            if not shaped["available"]:
                shaped["reason"] = (e.errors[0][1] if (e and e.errors)
                                    else "no statements published for this instrument")
            statements[w] = shaped

        quote_ccy = units_display(t.currency)
        return {
            "meta": {
                "source": "openbb", "state": "ready",
                "connected": any(s["available"] for s in statements.values()),
                "served_at": _now(), "period": period,
                "cache_age_s": {n: int(time.monotonic() - e.stored)
                                for n, e in entries.items() if e},
                "stale": sorted(n for n, e in entries.items() if e and not e.fresh()),
                "pending": sorted(pending),
                "retry_after": 2 if pending else None,
                "error": None,
            },
            "coverage": {n: {"provider": (e.provider if e else "none"),
                             "errors": [list(x) for x in (e.errors if e else [])]}
                         for n, e in entries.items()},
            "instrument": _identity(t),
            "reporting": {
                "currency": reporting,
                "source": "metrics.currency" if reporting else "unknown",
                "quote_currency": quote_ccy,
                # Surfaced as a fact, not a warning. HSBA reports USD and quotes
                # in pence; 0700.HK reports CNY and quotes HKD.
                "differs_from_quote": bool(reporting and quote_ccy
                                           and reporting.upper() != quote_ccy.upper()),
                "note": "As reported by the company. Not adjusted, not converted.",
            },
            "periods": PERIODS,
            "statements": statements,
        }


def units_display(currency: str | None) -> str:
    """The major-unit name for a quote currency, for the mismatch line only.

    Deliberately local and tiny rather than importing units.scale_for: nothing
    in this module scales a figure, and reaching for that module here is exactly
    the mistake its docstring warns against.
    """
    code = (currency or "").strip()
    return "GBP" if code in ("GBp", "GBX", "GBx") else code


def _identity(t: universe.Ticker) -> dict:
    return {
        "key": t.key, "symbol": t.symbol, "name": t.name,
        "exchange": t.exchange, "exchange_name": t.exchange_display,
        "currency": t.currency, "region": t.region, "sector": t.sector,
        "owned": t.owned, "logo": t.logo_url, "mono": t.mono,
        **universe.tile_colours(t),
    }


def _empty(key: str, error: str | None, state: str = "ready",
           retry_after: int | None = None,
           ticker: universe.Ticker | None = None,
           period: str = "annual") -> dict:
    return {
        "meta": {"source": "openbb", "state": state, "connected": False,
                 "served_at": _now(), "period": period, "cache_age_s": {},
                 "stale": [], "pending": [], "retry_after": retry_after,
                 "error": error},
        "coverage": {},
        "instrument": _identity(ticker) if ticker is not None else {"key": key},
        "reporting": {"currency": None, "source": "unknown", "quote_currency": "",
                      "differs_from_quote": False, "note": ""},
        "periods": PERIODS,
        "statements": {w: {"available": False, "periods": [], "sections": [],
                           "chart": [], "reason": error or "unavailable"}
                       for w in STATEMENTS},
    }


# --------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    keys = sys.argv[1:] or ["INTC"]

    svc = FinancialsService(gate=None)
    svc.start()
    for _ in range(300):
        if svc._ready.is_set():
            break
        time.sleep(0.1)

    for key in keys:
        for period in ("annual",):
            t0 = time.monotonic()
            payload = svc.snapshot(key, period)
            took = time.monotonic() - t0
            i, rep = payload["instrument"], payload["reporting"]
            print("\n" + "=" * 78)
            print(f"{key}  {i.get('symbol')}  {i.get('exchange_name')}  [{took:.1f}s]")
            print(f"  reports in {rep['currency'] or '—'} · quotes in "
                  f"{rep['quote_currency'] or '—'}"
                  f"{'   <-- MISMATCH' if rep['differs_from_quote'] else ''}")
            print("=" * 78)
            for name, c in sorted(payload["coverage"].items()):
                err = c["errors"][0][1][:46] if c["errors"] else ""
                print(f"  {name:<9} {c['provider']:<10} {err}")
            for w in STATEMENTS:
                s = payload["statements"][w]
                if not s["available"]:
                    print(f"  {STATEMENT_LABEL[w]:<18} unavailable — {s['reason'][:44]}")
                    continue
                rows = sum(len(sec["rows"]) for sec in s["sections"])
                print(f"  {STATEMENT_LABEL[w]:<18} {len(s['periods'])} periods, "
                      f"{len(s['sections'])} sections, {rows} rows, "
                      f"chart {len(s['chart'])} series  {s['periods']}")

            # Units regression: the figure must be raw and unscaled, and the
            # currency must be the REPORTING one.
            inc = payload["statements"]["income"]
            if inc["available"]:
                for sec in inc["sections"]:
                    for r in sec["rows"]:
                        if r["field"] == "total_revenue":
                            print(f"  total_revenue (unscaled): {r['values'][0]}")
                            break

            # JSON regression: NaN or a date here kills the browser, not a cell.
            try:
                blob = json.dumps(payload)
                assert "NaN" not in blob and "Infinity" not in blob, "non-finite leaked"
                json.loads(blob)
                print(f"  JSON OK ({len(blob)/1024:.0f} KB), no NaN")
            except (TypeError, ValueError, AssertionError) as exc:
                print(f"  JSON FAILED: {exc}")
    svc.stop()
    directory.close()
