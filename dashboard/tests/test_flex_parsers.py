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


def test_parse_trades_reads_asset_and_fx_and_keeps_executions_only():
    """Both levels of detail ticked reports every fill twice; a replay wants
    one. The asset class is what tells an FX sweep from a stock fill."""
    root = root_of("""
    <FlexStatement>
      <Trade tradeID="t1" tradeDate="20260106" conid="42" symbol="INTC" currency="USD"
             exchange="NASDAQ" quantity="30" tradePrice="99.39" buySell="BUY"
             assetCategory="STK" fxRateToBase="0.79" levelOfDetail="EXECUTION"/>
      <Trade tradeID="o1" tradeDate="20260106" conid="42" symbol="INTC" currency="USD"
             exchange="NASDAQ" quantity="30" tradePrice="99.39" buySell="BUY"
             assetCategory="STK" levelOfDetail="ORDER"/>
      <Trade tradeID="t2" tradeDate="20260106" conid="7" symbol="GBP.USD" currency="USD"
             exchange="IDEALFX" quantity="-1000" tradePrice="1.27" buySell="SELL"
             assetCategory="CASH" levelOfDetail="EXECUTION"/>
    </FlexStatement>""")
    rows = flex.parse_trades(root)
    assert [r["exec_id"] for r in rows] == ["t1", "t2"]
    assert rows[0]["asset"] == "STK" and rows[0]["fx_to_base"] == 0.79
    assert rows[1]["asset"] == "CASH" and rows[1]["fx_to_base"] is None


def test_parse_trades_without_level_of_detail_keeps_every_row():
    root = root_of("""
    <FlexStatement>
      <Trade tradeID="a" tradeDate="20260106" conid="42" symbol="X" quantity="1" tradePrice="1"/>
      <Trade tradeID="b" tradeDate="20260107" conid="42" symbol="X" quantity="1" tradePrice="1"/>
    </FlexStatement>""")
    assert len(flex.parse_trades(root)) == 2


def test_parse_nav_history_carries_the_split_when_the_query_has_it():
    root = root_of("""
    <FlexStatement>
      <EquitySummaryByReportDateInBase reportDate="20260901" total="50000" cash="2500"
          stock="47400" dividendAccruals="80" interestAccruals="20"/>
    </FlexStatement>""")
    row = flex.parse_nav_history(root)[0]
    assert row["cash_gbp"] == 2500.0 and row["stock_gbp"] == 47400.0
    assert row["accruals_gbp"] == 100.0


def test_bucket_uses_the_exact_table_first():
    assert flex._bucket("Bond Interest Paid") == "interest_paid"      # substring order would say received
    assert flex._bucket("Broker Interest Received") == "interest_received"
    assert flex._bucket("Deposits/Withdrawals") == "deposits_withdrawals"
    assert flex._bucket("Payment In Lieu Of Dividends") == "dividends"
    assert flex._bucket("Commission Adjustments") == "commissions"
    assert flex._bucket("Something New") == "other"


def test_parse_cash_transactions_keeps_one_level_of_detail():
    root = root_of("""
    <FlexStatement>
      <CashTransaction settleDate="20260805" type="Dividends" amount="100" currency="USD"
          fxRateToBase="0.75" levelOfDetail="DETAIL" transactionID="c1" symbol="AAPL" conid="1"/>
      <CashTransaction settleDate="20260805" type="Dividends" amount="100" currency="USD"
          fxRateToBase="0.75" levelOfDetail="SUMMARY" symbol="AAPL" conid="1"/>
    </FlexStatement>""")
    rows = flex.parse_cash_transactions(root)
    assert len(rows) == 1
    assert rows[0]["bucket"] == "dividends" and rows[0]["amount_gbp"] == 75.0


def test_parse_change_in_nav_periods_reports_every_row_and_flags_unmapped():
    root = root_of("""
    <FlexStatement>
      <ChangeInNAV fromDate="20250820" toDate="20260828" startingValue="0" endingValue="49000"
          depositsWithdrawals="42500" mtm="6500" billPay="0"/>
      <ChangeInNAV fromDate="20260801" toDate="20260828" startingValue="47000" endingValue="49000"
          depositsWithdrawals="2000" mtm="0" someNewFlow="12.5"/>
    </FlexStatement>""")
    rows = flex.parse_change_in_nav_periods(root)
    assert [r["span_days"] for r in rows] == [373, 27]
    assert rows[1]["unmapped"] == ["someNewFlow"]
    assert rows[0]["unmapped"] == []                                   # zero-valued unknowns are noise


def test_parse_conversion_rates_keeps_rates_into_gbp_only():
    root = root_of("""
    <FlexStatement>
      <ConversionRate reportDate="20260901" fromCurrency="USD" toCurrency="GBP" rate="0.7451"/>
      <ConversionRate reportDate="20260901" fromCurrency="HKD" toCurrency="GBP" rate="0.0955"/>
      <ConversionRate reportDate="20260901" fromCurrency="USD" toCurrency="EUR" rate="0.86"/>
      <ConversionRate reportDate="20260902" fromCurrency="USD" toCurrency="GBP" rate="0"/>
    </FlexStatement>""")
    rows = flex.parse_conversion_rates(root)
    assert [(r["date"], r["currency"], r["rate"]) for r in rows] == [
        ("2026-09-01", "HKD", 0.0955), ("2026-09-01", "USD", 0.7451)]


def test_parse_corporate_actions_and_transfers_are_signed_quantities():
    root = root_of("""
    <FlexStatement>
      <CorporateAction actionID="a1" conid="42" symbol="NVDA" reportDate="20240610" type="FS"
          quantity="300" proceeds="0" value="0" currency="USD" fxRateToBase="0.78"
          actionDescription="NVDA(US67066G1040) SPLIT 4 FOR 1" levelOfDetail="DETAIL"/>
      <CorporateAction actionID="a1" conid="42" symbol="NVDA" reportDate="20240610" type="FS"
          quantity="300" levelOfDetail="SUMMARY"/>
      <CorporateAction actionID="a2" conid="77" symbol="ZZZ" reportDate="20250101" type="XX" quantity="-5"/>
      <Transfer conid="99" symbol="VUSA" reportDate="20250301" type="ACATS" direction="IN"
          quantity="12" positionAmount="1000" currency="GBP" transactionID="t9"/>
    </FlexStatement>""")
    actions = flex.parse_corporate_actions(root)
    assert [(a["con_id"], a["kind"], a["quantity"]) for a in actions] == [(42, "split", 300.0), (77, "other", -5.0)]
    transfers = flex.parse_transfers(root)
    assert transfers == [{"action_id": "t9", "con_id": 99, "symbol": "VUSA", "date": "2025-03-01",
                          "code": "ACATS", "kind": "transfer", "quantity": 12.0, "proceeds": None,
                          "value": 1000.0, "currency": "GBP", "fx_to_base": None,
                          "description": "in", "source": "flex"}]
