"""IBKR Flex Web Service -> historical NAV and trade history.

The TWS API that ib_async speaks keeps no archive: `reqExecutions` covers the
current session, and there is no NAV-history request at all. Flex is IBKR's
own reporting API and does have both, over plain HTTPS with a token. No Claude
session, no Gateway, no browser.

Setup, once, in IBKR Account Management:

  1. Settings -> Account Reporting -> Flex Web Service -> generate a token.
  2. Create two Flex *queries* and note the Query ID of each:
       - an Activity Statement including the "Net Asset Value (NAV) in Base"
         section, which yields the daily NAV series
       - a Trade Confirmation query (or an Activity Statement with the Trades
         section), which yields executions
  3. Copy config.local.json.example to config.local.json and fill in the three
     values.

The protocol is two calls: SendRequest hands back a reference code, then
GetStatement returns the report once IBKR has generated it — which is not
immediate, hence the polling.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
SEND_URL = f"{BASE}/SendRequest"
GET_URL = f"{BASE}/GetStatement"
VERSION = "3"

POLL_ATTEMPTS = 12
POLL_SECONDS = 5
TIMEOUT = 45

# IBKR paces SendRequest at one request per second *and* ten per minute. The
# per-minute cap is the binding one here: a backfill walks one window per month
# since inception, which is already more than ten, so spacing them a second
# apart would be refused partway through. 6.5s clears both without having to
# track a sliding window. Only SendRequest is paced — GetStatement polling is
# not subject to it, and POLL_SECONDS already spaces that out.
SEND_INTERVAL = 6.5
_last_send = 0.0

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.local.json"

log = logging.getLogger(__name__)


class FlexNotConfigured(RuntimeError):
    """config.local.json is missing or incomplete — callers skip Flex quietly."""


class FlexError(RuntimeError):
    """IBKR accepted the request but refused or failed to produce a statement."""


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FlexNotConfigured(f"{CONFIG_PATH.name} not found")
    try:
        config = json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError as exc:
        raise FlexNotConfigured(f"{CONFIG_PATH.name} is not valid JSON: {exc}") from exc

    token = str(config.get("flex_token", "")).strip()
    if not token or token.startswith("PASTE"):
        raise FlexNotConfigured("flex_token is not set")
    return config


def _get(url: str, params: dict) -> bytes:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(full, headers={"User-Agent": "portfolio-dashboard/1.0"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def _pace() -> None:
    """Hold SendRequest to IBKR's documented rate. Cheap when already spaced."""
    global _last_send
    wait = SEND_INTERVAL - (time.monotonic() - _last_send)
    if wait > 0:
        log.debug("pacing SendRequest, sleeping %.1fs", wait)
        time.sleep(wait)
    _last_send = time.monotonic()


def fetch_statement(token: str, query_id: str, *,
                    from_date: date | None = None,
                    to_date: date | None = None) -> ET.Element:
    """Run one Flex query and return the parsed statement root.

    `from_date`/`to_date` are IBKR's `fd`/`td` overrides. They must be sent as a
    pair, cap out at 365 days apart, and are mutually exclusive with the query's
    own configured period — passing them replaces whatever range the query was
    saved with. Omitting both leaves the saved period in force, which is what
    every caller wanting the whole span does.
    """
    params = {"t": token, "q": query_id, "v": VERSION}
    if from_date and to_date:
        params["fd"] = from_date.strftime("%Y%m%d")
        params["td"] = to_date.strftime("%Y%m%d")

    _pace()
    root = ET.fromstring(_get(SEND_URL, params))

    status = (root.findtext("Status") or "").strip()
    if status != "Success":
        raise FlexError(
            f"SendRequest failed: {root.findtext('ErrorCode')} "
            f"{root.findtext('ErrorMessage')}")

    reference = (root.findtext("ReferenceCode") or "").strip()
    base_url = (root.findtext("Url") or GET_URL).strip()

    for attempt in range(POLL_ATTEMPTS):
        try:
            body = _get(base_url, {"t": token, "q": reference, "v": VERSION})
        except OSError:
            if base_url == GET_URL:
                raise
            # IBKR has started advertising GetStatement on hosts that do not
            # always resolve (gdcdyn.…); the documented endpoint serves the
            # same reference codes, so fall back rather than fail the run.
            log.warning("advertised statement host unreachable (%s); "
                        "falling back to %s", base_url.split("/")[2], GET_URL.split("/")[2])
            base_url = GET_URL
            body = _get(base_url, {"t": token, "q": reference, "v": VERSION})
        statement = ET.fromstring(body)

        # While generating, IBKR returns a FlexStatementResponse with a warning
        # rather than the report itself.
        if statement.tag == "FlexStatementResponse":
            code = (statement.findtext("ErrorCode") or "").strip()
            message = (statement.findtext("ErrorMessage") or "").strip()
            if code in {"1019", "1021"}:      # generation in progress
                log.info("Flex statement still generating (attempt %d)", attempt + 1)
                time.sleep(POLL_SECONDS)
                continue
            raise FlexError(f"GetStatement failed: {code} {message}")
        return statement

    raise FlexError("timed out waiting for IBKR to generate the statement")


def _iter(root: ET.Element, tag: str):
    """Flex nests report sections differently per query type; search anywhere."""
    yield from root.iter(tag)


def parse_nav_history(root: ET.Element) -> list[dict]:
    """Daily NAV in base currency, oldest first.

    Reads EquitySummaryByReportDateInBase, the section an Activity Statement
    emits for "Net Asset Value (NAV) in Base".
    """
    rows: dict[str, float] = {}
    for node in _iter(root, "EquitySummaryByReportDateInBase"):
        date = node.get("reportDate")
        total = node.get("total")
        if not date or total is None:
            continue
        try:
            rows[_iso(date)] = float(total)
        except ValueError:
            continue

    return [{"date": d, "nav_gbp": rows[d], "source": "flex"} for d in sorted(rows)]


def parse_trades(root: ET.Element) -> list[dict]:
    """Executions, oldest first."""
    out = []
    for node in _iter(root, "Trade"):
        date = node.get("tradeDate") or node.get("dateTime", "")[:8]
        if not date:
            continue
        try:
            quantity = float(node.get("quantity") or 0)
            price = float(node.get("tradePrice") or 0)
        except ValueError:
            continue

        commission = node.get("ibCommission")
        try:
            commission = float(commission) if commission is not None else None
        except ValueError:
            commission = None

        out.append({
            "exec_id": node.get("tradeID") or node.get("transactionID") or "",
            "time": _iso(date),
            "con_id": int(node.get("conid") or 0),
            "symbol": node.get("symbol") or "",
            "currency": node.get("currency") or "",
            "exchange": node.get("exchange") or "",
            "side": node.get("buySell") or ("BOT" if quantity > 0 else "SLD"),
            "quantity": abs(quantity),
            "price": price,
            "commission": commission,
            "source": "flex",
        })

    out.sort(key=lambda row: (row["time"], row["exec_id"]))
    return out


def _iso(value: str) -> str:
    """Flex emits yyyymmdd; the dashboard wants yyyy-mm-dd."""
    value = value.strip()
    if len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return value


# The longest span still counted as a single month. Flex month periods run
# 28-31 days; 31 would reject a month whose period is stamped inclusively, so
# allow a little slack. Anything wider is a summary covering several months.
MONTH_SPAN_DAYS = 32


def _span_days(node) -> int:
    """How many days a period element covers, for picking the widest of several."""
    try:
        start = date.fromisoformat(_iso(node.get("fromDate") or ""))
        end = date.fromisoformat(_iso(node.get("toDate") or ""))
        return (end - start).days
    except (ValueError, TypeError):
        return 0


def _num(node, name: str) -> float:
    """An attribute as a float, or 0.0. Flex writes "" for a field that had no
    activity, which float() will not take."""
    raw = (node.get(name) or "").strip()
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


# Every ChangeInNAV field this dashboard reads, in the order the Sankey stacks
# them. IBKR emits one such element per report period, covering fromDate to
# toDate. A query with no sub-periods emits exactly one, and it is a summary
# rather than a series; a query configured with monthly sub-periods emits one
# per month as well, which is what the monthly P&L bars read.
#
# Names come from IBKR's Activity Flex schema. They are read defensively:
# anything missing reads 0.0, and parse_change_in_nav reports back which
# attributes the statement actually carried, so a name that turns out to differ
# shows up as an unmapped field rather than as a silently absent flow.
NAV_CHANGE_FIELDS = (
    "startingValue", "mtm", "realized", "changeInUnrealized", "costAdjustments",
    "transferredPnlAdjustments", "depositsWithdrawals", "internalCashTransfers",
    "assetTransfers", "dividends", "withholdingTax", "withholdingTaxCollected",
    "changeInDividendAccruals", "interest", "changeInInterestAccruals",
    "advisorFees", "brokerFees", "brokerFeesSalesTax", "brokerInterest",
    "bondInterest", "cashSettlingMtm", "realizedVm", "cfdCharges",
    "fxTranslation", "otherFees", "other", "endingValue", "twr",
    "corporateActionProceeds", "commissions", "transactionTax",
)


# Attributes that identify a period rather than describe a flow. Excluded from
# `unmapped` so a schema difference stands out against them.
_CHANGE_SKIP = {"accountId", "acctAlias", "model", "currency", "fromDate",
                "toDate", "reportDate", "levelOfDetail"}


def _change_row(node) -> dict:
    """One ChangeInNAV element as a flat row.

    `unmapped` lists any attribute the element carried that NAV_CHANGE_FIELDS
    does not name *and* that carries a non-zero figure. It exists so a schema
    difference surfaces as data rather than as a flow that silently reads zero.

    Non-zero is the whole point of the check. IBKR emits every field it knows
    about on every element, so a plain "not in our list" test names about
    thirty of them — billPay, carbonCredits, paxosTransfers — every single
    time, all of them zero, and the Sankey prints the lot underneath itself.
    A field holding no money is not drift; a field holding money we did not
    map is, and that is the one worth interrupting someone for.
    """
    out: dict = {name: _num(node, name) for name in NAV_CHANGE_FIELDS}
    out["from_date"] = _iso(node.get("fromDate") or "")
    out["to_date"] = _iso(node.get("toDate") or "")
    out["currency"] = node.get("currency") or ""
    out["span_days"] = _span_days(node)
    out["unmapped"] = sorted(
        k for k in node.attrib
        if k not in _CHANGE_SKIP and k not in NAV_CHANGE_FIELDS
        and (node.get(k) or "").strip()
        and _num(node, k)
    )
    out["source"] = "flex"
    return out


def parse_change_in_nav(root: ET.Element) -> dict | None:
    """The widest Change in NAV summary: what moved the account over the period.

    This is the section the NAV flow chart is built from. Returns None when the
    statement has no such element, which is the normal state until the section
    is enabled on the Flex query.
    """
    # One element per *request*, not per month: the section has no sub-period
    # breakdown of its own (see `month_windows`). The store therefore holds a
    # mix of whole-span and per-month rows, gathered by separate requests, and
    # the Sankey wants the whole span — so take the widest rather than the
    # first, since document order guarantees nothing.
    nodes = list(_iter(root, "ChangeInNAV"))
    if not nodes:
        return None
    return _change_row(max(nodes, key=_span_days))


def parse_change_in_nav_periods(root: ET.Element) -> list[dict]:
    """Every Change in NAV element in this statement, one row per period.

    In practice that is a single row, because the section reports one element
    for the range it was asked for. It stays a list because the caller merges
    the results of *many* requests into one store keyed on the period, and
    because reading whatever IBKR sent is cheaper than asserting a count.

    `parse_change_in_nav` keeps only the widest, since the Sankey covers the
    whole span. The monthly P&L bars want the narrow rows, which arrive from
    the month-scoped requests the backfill makes; each reader picks the spans
    it wants by `span_days`.
    """
    return [_change_row(node) for node in _iter(root, "ChangeInNAV")]


# CashTransaction.type values, normalised to the buckets the income and cost
# charts draw. IBKR's exact strings vary a little by report vintage, so the
# match is done on a lowercased substring rather than on equality.
CASH_BUCKETS = (
    ("payment in lieu", "dividends"),
    ("dividend", "dividends"),
    ("withholding", "withholding_tax"),
    ("broker interest paid", "interest_paid"),
    ("broker interest received", "interest_received"),
    ("bond interest", "interest_received"),
    ("interest", "interest_received"),
    ("commission", "commissions"),
    ("sales tax", "sales_tax"),
    ("other fee", "other_fees"),
    ("fee", "other_fees"),
    ("deposit", "deposits_withdrawals"),
    ("withdrawal", "deposits_withdrawals"),
)


def _bucket(kind: str) -> str:
    low = (kind or "").lower()
    for needle, name in CASH_BUCKETS:
        if needle in low:
            return name
    return "other"


def parse_cash_transactions(root: ET.Element) -> list[dict]:
    """Dated cash movements: dividends, tax, interest, fees. Oldest first.

    Every row is converted to base currency here rather than on the page, using
    the `fxRateToBase` IBKR stamps on each transaction. That rate is the one
    that applied on the day, which is the whole point — converting a dividend
    received nine months ago at today's spot would misstate it, and the current
    `fx` map in portfolio.json is all the dashboard would otherwise have.
    """
    # IBKR can emit the same movement twice, once as DETAIL and once as
    # SUMMARY, depending on how the query's level of detail is configured.
    # Counting both would silently double every dividend and every fee — the
    # kind of error that looks like a good month rather than like a bug. Keep
    # one level: DETAIL when the statement carries it, otherwise whatever it
    # does carry.
    nodes = list(_iter(root, "CashTransaction"))
    levels = {(n.get("levelOfDetail") or "").upper() for n in nodes}
    if "DETAIL" in levels and len(levels) > 1:
        nodes = [n for n in nodes if (n.get("levelOfDetail") or "").upper() == "DETAIL"]

    out = []
    for node in nodes:
        stamp = (node.get("settleDate") or node.get("reportDate")
                 or node.get("dateTime", "")[:8])
        if not stamp:
            continue
        amount = _num(node, "amount")
        rate = _num(node, "fxRateToBase") or 1.0
        kind = node.get("type") or ""
        out.append({
            "date": _iso(stamp)[:10],
            "type": kind,
            "bucket": _bucket(kind),
            "amount": amount,
            "currency": node.get("currency") or "",
            "fx_to_base": rate,
            "amount_gbp": round(amount * rate, 4),
            "symbol": node.get("symbol") or "",
            "con_id": int(node.get("conid") or 0) or None,
            "tx_id": node.get("transactionID") or node.get("tradeID") or "",
            "source": "flex",
        })

    out.sort(key=lambda row: (row["date"], row["tx_id"]))
    return out


def fetch_activity(config: dict | None = None, *,
                   from_date: date | None = None,
                   to_date: date | None = None) -> ET.Element:
    """The Activity Statement root, fetched once.

    Four sections are read off the same report — NAV history, Change in NAV,
    Cash Transactions and Trades — and Flex is slow and rate limited, so the
    callers that want more than one parse a single root rather than requesting
    the statement again per section.

    `from_date`/`to_date` narrow the report to one window; see `month_windows`
    for why the backfill asks for a month at a time.
    """
    config = config or load_config()
    query = str(config.get("nav_query_id", "")).strip()
    if not query or query.startswith("PASTE"):
        raise FlexNotConfigured("nav_query_id is not set")
    return fetch_statement(config["flex_token"], query,
                           from_date=from_date, to_date=to_date)


def snap_to_reported(windows: list[tuple[date, date]],
                     reported: list[str]) -> list[tuple[date, date]]:
    """Pull each window's ends in to days IBKR actually reported on.

    A calendar month's first and last day are frequently not trading days, and
    Flex refuses some of those outright: 2026-01-01 -> 2026-01-31 comes back
    "1003 Statement is not available" while 2026-01-02 -> 2026-01-30, the same
    month bounded by its real trading days, succeeds. The refusal is not
    consistent enough to predict — 2025-11-01 (a Saturday) is accepted — so the
    ends are snapped rather than nudged by a weekday rule.

    `reported` is `store.nav_dates()`: the days the NAV series carries, which
    are by definition days the broker reported. A window containing none of
    them is dropped, because there is nothing in it to ask for.
    """
    out: list[tuple[date, date]] = []
    for first, last in windows:
        lo, hi = first.isoformat(), last.isoformat()
        inside = [d for d in reported if lo <= d <= hi]
        if not inside:
            continue
        out.append((date.fromisoformat(inside[0]), date.fromisoformat(inside[-1])))
    return out


def month_windows(start: date, end: date) -> list[tuple[date, date]]:
    """Calendar months spanning start..end inclusive, clipped at both ends.

    Change in NAV reports one element for whatever range it is asked for — it
    has no monthly breakdown of its own — so a month of granularity means a
    request per month. The first and last windows are clipped rather than
    widened, so a mid-month inception does not pull days before the account
    existed and the current month stops at `end` instead of running into the
    future.
    """
    if start > end:
        return []

    out: list[tuple[date, date]] = []
    cursor = start.replace(day=1)
    while cursor <= end:
        nxt = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        out.append((max(cursor, start), min(nxt - timedelta(days=1), end)))
        cursor = nxt
    return out


def fetch_nav_history(config: dict | None = None) -> list[dict]:
    return parse_nav_history(fetch_activity(config))


def fetch_trades(config: dict | None = None) -> list[dict]:
    """Executions.

    Falls back to the Activity Statement when no dedicated Trade Confirmation
    query is configured: adding the Trades section to the query that already
    serves NAV is one report rather than two, and the parser reads the same
    Trade element either way.
    """
    config = config or load_config()
    query = str(config.get("trades_query_id", "")).strip()
    if query and not query.startswith("PASTE"):
        return parse_trades(fetch_statement(config["flex_token"], query))
    return parse_trades(fetch_activity(config))
