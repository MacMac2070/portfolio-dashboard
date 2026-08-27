"""The Flex XML parsers, against hand-written statement fragments — the same
shapes IBKR emits, small enough to verify by eye. Network never involved:
every parser takes a parsed ElementTree root."""
import xml.etree.ElementTree as ET
from datetime import date

import pytest

import flex


def root_of(xml: str) -> ET.Element:
    return ET.fromstring(xml)


# ---------------------------------------------------------------- nav history

def test_parse_nav_history_sorted_and_deduped():
    root = root_of("""
    <FlexStatement>
      <EquitySummaryByReportDateInBase reportDate="20260107" total="50100.5"/>
      <EquitySummaryByReportDateInBase reportDate="20260106" total="50000"/>
      <EquitySummaryByReportDateInBase reportDate="20260106" total="50050"/>
      <EquitySummaryByReportDateInBase reportDate="20260108" total="notanumber"/>
      <EquitySummaryByReportDateInBase total="1"/>
    </FlexStatement>""")
    rows = flex.parse_nav_history(root)
    assert [r["date"] for r in rows] == ["2026-01-06", "2026-01-07"]
    assert rows[0]["nav_gbp"] == 50050.0          # later duplicate wins
    assert all(r["source"] == "flex" for r in rows)


# ---------------------------------------------------------------- trades

def test_parse_trades_side_and_commission():
    root = root_of("""
    <FlexStatement>
      <Trade tradeID="t1" tradeDate="20260106" conid="42" symbol="INTC"
             currency="USD" exchange="NASDAQ" quantity="30" tradePrice="99.39"
             ibCommission="-1.0" buySell="BUY"/>
      <Trade tradeID="t2" tradeDate="20260107" conid="42" symbol="INTC"
             currency="USD" exchange="NASDAQ" quantity="-10" tradePrice="101.2"
             ibCommission=""/>
    </FlexStatement>""")
    rows = flex.parse_trades(root)
    assert len(rows) == 2
    buy, sell = rows
    assert buy["side"] == "BUY" and buy["quantity"] == 30.0
    assert sell["side"] == "SLD"                  # derived from the sign
    assert sell["quantity"] == 10.0               # stored unsigned
    assert sell["commission"] is None             # empty string, not zero


# ---------------------------------------------------------------- open positions

POSITIONS_XML = """
<FlexStatement>
  <OpenPosition conid="1" symbol="AAPL" position="7" levelOfDetail="SUMMARY"
     markPrice="310.6" positionValue="2174.2" costBasisPrice="186.52"
     currency="USD" assetCategory="STK" reportDate="20260825" fxRateToBase="0.74"/>
  <OpenPosition conid="1" symbol="AAPL" position="3" levelOfDetail="LOT"
     markPrice="310.6" positionValue="931.8" currency="USD" reportDate="20260825"/>
  <OpenPosition conid="2" symbol="HSBA" position="100" levelOfDetail="SUMMARY"
     positionValue="1528" currency="GBP" reportDate="20260825"/>
  <OpenPosition conid="3" symbol="GONE" position="0" levelOfDetail="SUMMARY"
     positionValue="0" currency="USD" reportDate="20260825"/>
</FlexStatement>"""


def test_parse_open_positions_keeps_summary_rows_only():
    rows = flex.parse_open_positions(root_of(POSITIONS_XML))
    assert [r["symbol"] for r in rows] == ["AAPL", "HSBA"]   # by value, desc
    aapl = rows[0]
    assert aapl["quantity"] == 7.0                # SUMMARY, not the LOT
    assert aapl["fx_to_base"] == pytest.approx(0.74)


def test_parse_open_positions_missing_mark_is_none_not_zero():
    rows = flex.parse_open_positions(root_of(POSITIONS_XML))
    hsba = rows[1]
    assert hsba["mark_price"] is None
    assert hsba["average_cost"] is None
    assert hsba["value"] == pytest.approx(1528.0)


def test_parse_open_positions_zero_quantity_dropped():
    rows = flex.parse_open_positions(root_of(POSITIONS_XML))
    assert all(r["symbol"] != "GONE" for r in rows)


# ---------------------------------------------------------------- date windows

def test_month_windows_spans_calendar_months():
    windows = flex.month_windows(date(2026, 1, 15), date(2026, 3, 10))
    assert windows[0] == (date(2026, 1, 15), date(2026, 1, 31))
    assert windows[1] == (date(2026, 2, 1), date(2026, 2, 28))
    assert windows[-1] == (date(2026, 3, 1), date(2026, 3, 10))


def test_month_windows_leap_february():
    windows = flex.month_windows(date(2028, 2, 1), date(2028, 3, 1))
    assert windows[0] == (date(2028, 2, 1), date(2028, 2, 29))


def test_snap_to_reported_pulls_ends_to_trading_days():
    windows = [(date(2026, 1, 1), date(2026, 1, 31))]
    reported = ["2026-01-02", "2026-01-15", "2026-01-30"]
    snapped = flex.snap_to_reported(windows, reported)
    assert snapped == [(date(2026, 1, 2), date(2026, 1, 30))]


def test_snap_to_reported_drops_empty_windows():
    windows = [(date(2026, 2, 1), date(2026, 2, 28))]
    assert flex.snap_to_reported(windows, ["2026-01-15"]) == []


# ---------------------------------------------------------------- helpers

def test_iso_converts_flex_dates():
    assert flex._iso("20260825") == "2026-08-25"
    assert flex._iso("2026-08-25") == "2026-08-25"   # already ISO passes through


def test_num_or_none_distinguishes_absent_from_zero():
    node = root_of('<x a="0" b="" c="bad"/>')
    assert flex._num_or_none(node, "a") == 0.0
    assert flex._num_or_none(node, "b") is None
    assert flex._num_or_none(node, "c") is None
    assert flex._num_or_none(node, "missing") is None
