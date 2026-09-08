"""derive.py is where every £ figure on the page is computed — the highest
value per test in the codebase. Expected values are hand-computed."""
import math

import pytest

import derive


GBP = derive.converter({"GBP": 1.0, "USD": 0.8, "HKD": 0.1})


def make(**overrides):
    base = dict(
        con_id=1, symbol="TEST", currency="GBP", exchange="LSE",
        quantity=10.0, price=100.0, market_value=1000.0, average_cost=90.0,
        unrealized_pnl=100.0, day_change_pct=None, day_change_source="t",
        spark=[], to_gbp=GBP,
    )
    base.update(overrides)
    return derive.make_position(**base)


# ---------------------------------------------------------------- pct

def test_pct_normal():
    assert derive.pct(50, 200) == 25.0


def test_pct_zero_denominator():
    assert derive.pct(50, 0) is None


def test_pct_none_inputs():
    assert derive.pct(None, 10) is None
    assert derive.pct(10, None) is None


# ---------------------------------------------------------------- converter

def test_converter_known_rate():
    assert GBP(100, "USD") == pytest.approx(80.0)


def test_converter_unknown_currency_raises():
    with pytest.raises(derive.MissingRate):
        GBP(100, "XXX")


def test_make_position_exposes_both_cost_conventions():
    # $1,000 of stock bought for $900 when the pound bought $1.25 (fx 0.80);
    # today the rate is 0.75.
    to_gbp = derive.converter({"USD": 0.75, "GBP": 1.0})
    row = derive.make_position(
        con_id=1, symbol="X", currency="USD", exchange="NASDAQ", quantity=10, price=100.0,
        market_value=1000.0, average_cost=90.0, unrealized_pnl=100.0,
        day_change_pct=None, day_change_source="none", spark=[], to_gbp=to_gbp,
        cost_gbp_tradedate=900.0 * 0.80)
    assert row["cost_gbp"] == pytest.approx(675.0)                 # at today's rate
    assert row["unrealised_gbp"] == pytest.approx(75.0)            # the market return
    assert row["cost_gbp_tradedate"] == pytest.approx(720.0)       # what was paid
    assert row["unrealised_gbp_tradedate"] == pytest.approx(30.0)  # 750 − 720
    assert row["fx_pnl_gbp"] == pytest.approx(-45.0)               # 900 × (0.75 − 0.80)
    # Without a ledger figure the second convention is simply absent.
    bare = derive.make_position(
        con_id=1, symbol="X", currency="USD", exchange="NASDAQ", quantity=10, price=100.0,
        market_value=1000.0, average_cost=90.0, unrealized_pnl=100.0,
        day_change_pct=None, day_change_source="none", spark=[], to_gbp=to_gbp)
    assert bare["cost_gbp_tradedate"] is None and bare["fx_pnl_gbp"] is None


def test_aggregate_skips_unpriced_rows_and_totals_tradedate_only_when_complete():
    to_gbp = derive.converter({"USD": 0.75, "GBP": 1.0})
    priced = derive.make_position(
        con_id=1, symbol="A", currency="USD", exchange="", quantity=10, price=100.0,
        market_value=1000.0, average_cost=90.0, unrealized_pnl=100.0,
        day_change_pct=1.0, day_change_source="x", spark=[], to_gbp=to_gbp,
        cost_gbp_tradedate=720.0)
    unpriced = derive.unpriced_position(con_id=2, symbol="W", currency="KRW", exchange="",
                                        quantity=5, price=1.0)
    agg = derive.aggregate([priced, unpriced], nav=1000.0, cash=250.0, account_daily_pnl=None)
    assert agg["kpis"]["invested"] == pytest.approx(750.0)
    assert agg["kpis"]["unpriced"] == 1
    assert agg["kpis"]["fx_pnl"] == pytest.approx(-45.0)
    assert agg["concentration"]["positions"] == 2
    # One priced row without a trade-date cost leaves the convention untotalled.
    partial = dict(priced, cost_gbp_tradedate=None, unrealised_gbp_tradedate=None, fx_pnl_gbp=None)
    agg = derive.aggregate([priced, dict(partial, con_id=3, symbol="B")], nav=2000.0, cash=0.0,
                           account_daily_pnl=None)
    assert agg["kpis"]["fx_pnl"] is None


# ---------------------------------------------------------------- make_position

def test_gbp_position_values_pass_through():
    row = make()
    assert row["value_gbp"] == pytest.approx(1000.0)
    assert row["cost_gbp"] == pytest.approx(900.0)      # 90 * 10
    assert row["unrealised_gbp"] == pytest.approx(100.0)
    assert row["unrealised_pct"] == pytest.approx(100.0 / 900.0 * 100)


def test_fx_conversion_applies_to_all_money_fields():
    row = make(currency="USD")
    assert row["value_gbp"] == pytest.approx(800.0)
    assert row["cost_gbp"] == pytest.approx(720.0)
    assert row["unrealised_gbp"] == pytest.approx(80.0)


def test_day_pnl_derived_from_percentage():
    # value 1000 after a +2.5% day: opening 975.61, day P&L +24.39.
    row = make(day_change_pct=2.5)
    assert row["day_pnl_gbp"] == pytest.approx(1000 - 1000 / 1.025)


def test_day_pnl_negative_day():
    row = make(day_change_pct=-2.0)
    assert row["day_pnl_gbp"] == pytest.approx(1000 - 1000 / 0.98)


def test_day_pnl_none_when_pct_missing():
    assert make(day_change_pct=None)["day_pnl_gbp"] is None


def test_day_pnl_survives_total_wipeout():
    # -100% used to divide by zero. The opening value is unrecoverable, so
    # the honest answer is None — and no crash.
    assert make(day_change_pct=-100.0, market_value=0.0)["day_pnl_gbp"] is None


def test_day_pnl_survives_past_minus_100():
    assert make(day_change_pct=-140.0)["day_pnl_gbp"] is None


# ---------------------------------------------------------------- daily_pnl

def rows_with_day_pnl(*vals):
    return [make(day_change_pct=None) | {"day_pnl_gbp": v} for v in vals]


def test_daily_pnl_prefers_complete_position_sum():
    total, source = derive.daily_pnl(rows_with_day_pnl(10.0, -4.0), account_daily_pnl=99.0)
    assert total == pytest.approx(6.0)
    assert source == "positions-complete"


def test_daily_pnl_falls_back_to_account_when_partial():
    total, source = derive.daily_pnl(rows_with_day_pnl(10.0, None), account_daily_pnl=99.0)
    assert total == pytest.approx(99.0)
    assert source == "ibkr-account-partial"


def test_daily_pnl_partial_sum_when_no_account_figure():
    total, source = derive.daily_pnl(rows_with_day_pnl(10.0, None), account_daily_pnl=None)
    assert total == pytest.approx(10.0)
    assert source == "positions-partial-1-of-2"


# ---------------------------------------------------------------- aggregate

def test_aggregate_kpis_and_concentration():
    positions = [
        make(con_id=1, symbol="BIG", market_value=3000.0, unrealized_pnl=300.0,
             average_cost=270.0, day_change_pct=1.0),
        make(con_id=2, symbol="SMALL", market_value=1000.0, unrealized_pnl=-50.0,
             average_cost=105.0, day_change_pct=-1.0),
    ]
    agg = derive.aggregate(positions, nav=4500.0, cash=500.0, account_daily_pnl=None)
    k = agg["kpis"]
    assert k["net_liquidation"] == 4500.0
    assert k["invested"] == pytest.approx(4000.0)
    assert k["cash_available"] == 500.0
    assert k["unrealised_pnl"] == pytest.approx(250.0)
    assert agg["concentration"]["largest_symbol"] == "BIG"
    assert agg["concentration"]["largest_weight_pct"] == pytest.approx(75.0)
    assert agg["daily_pnl_source"] == "positions-complete"
    # Every currency row's value adds back to invested.
    assert sum(c["value_gbp"] for c in agg["currencies"]) == pytest.approx(4000.0)


def test_aggregate_empty_book_does_not_crash():
    agg = derive.aggregate([], nav=0.0, cash=0.0, account_daily_pnl=None)
    assert agg["kpis"]["invested"] == 0.0
    assert agg["concentration"]["largest_symbol"] is None
