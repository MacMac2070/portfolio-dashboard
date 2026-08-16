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
from pathlib import Path

BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
SEND_URL = f"{BASE}/SendRequest"
GET_URL = f"{BASE}/GetStatement"
VERSION = "3"

POLL_ATTEMPTS = 12
POLL_SECONDS = 5
TIMEOUT = 45

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


def fetch_statement(token: str, query_id: str) -> ET.Element:
    """Run one Flex query and return the parsed statement root."""
    root = ET.fromstring(_get(SEND_URL, {"t": token, "q": query_id, "v": VERSION}))

    status = (root.findtext("Status") or "").strip()
    if status != "Success":
        raise FlexError(
            f"SendRequest failed: {root.findtext('ErrorCode')} "
            f"{root.findtext('ErrorMessage')}")

    reference = (root.findtext("ReferenceCode") or "").strip()
    base_url = (root.findtext("Url") or GET_URL).strip()

    for attempt in range(POLL_ATTEMPTS):
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


def fetch_nav_history(config: dict | None = None) -> list[dict]:
    config = config or load_config()
    query = str(config.get("nav_query_id", "")).strip()
    if not query or query.startswith("PASTE"):
        raise FlexNotConfigured("nav_query_id is not set")
    return parse_nav_history(fetch_statement(config["flex_token"], query))


def fetch_trades(config: dict | None = None) -> list[dict]:
    config = config or load_config()
    query = str(config.get("trades_query_id", "")).strip()
    if not query or query.startswith("PASTE"):
        raise FlexNotConfigured("trades_query_id is not set")
    return parse_trades(fetch_statement(config["flex_token"], query))
