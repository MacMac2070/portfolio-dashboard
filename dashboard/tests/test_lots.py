"""lots.py: FIFO lots with the FX rate of their own trade date."""
import lots


def fill(con_id, side, qty, price, when, fx=None, currency="USD", exec_id=""):
    return {"con_id": con_id, "side": side, "quantity": qty, "price": price, "time": when,
            "currency": currency, "fx_to_base": fx, "exchange": "NASDAQ", "asset": "STK",
            "exec_id": exec_id or f"{con_id}-{when}-{side}"}


def test_fifo_partial_sell_consumes_the_oldest_lot():
    rows = [fill(1, "BUY", 10, 100.0, "2026-01-05", fx=0.80),
            fill(1, "BUY", 10, 120.0, "2026-02-05", fx=0.75),
            fill(1, "SELL", 5, 130.0, "2026-03-05", fx=0.70)]
    s = lots.summarise(lots.build_lots(rows))[1]
    assert s.quantity == 15
    assert s.cost_local == 5 * 100.0 + 10 * 120.0          # 5 left of the first lot
    assert s.cost_gbp_tradedate == 5 * 100.0 * 0.80 + 10 * 120.0 * 0.75


def test_cost_gbp_uses_trade_date_fx_and_isolates_the_currency_move():
    # Buy $100 at 0.80 → £80. Price flat, sterling strengthens to 0.75.
    rows = [fill(1, "BUY", 1, 100.0, "2026-01-05", fx=0.80)]
    s = lots.summarise(lots.build_lots(rows))[1]
    spot = 0.75
    value_gbp = 100.0 * spot                       # £75 today
    unrealised_spot = (100.0 - 100.0) * spot       # 0: the share did nothing
    unrealised_tradedate = value_gbp - s.cost_gbp_tradedate
    assert unrealised_tradedate == -5.0            # the pound's move against the cost
    assert unrealised_tradedate - unrealised_spot == -5.0


def test_legacy_row_without_fx_uses_the_rate_store_then_gbp_then_none():
    rows = [fill(1, "BUY", 1, 100.0, "2026-01-05"),                       # no fx on the row
            fill(2, "BUY", 1, 10.0, "2026-01-05", currency="GBP"),
            fill(3, "BUY", 1, 10.0, "2026-01-06", currency="HKD")]
    out = lots.summarise(lots.build_lots(rows, fx_history={("2026-01-05", "USD"): 0.79}))
    assert out[1].cost_gbp_tradedate == 79.0
    assert out[2].cost_gbp_tradedate == 10.0
    assert out[3].cost_gbp_tradedate is None and out[3].avg_fx is None


def test_split_scales_open_lots_and_keeps_their_cost():
    rows = [fill(1, "BUY", 10, 100.0, "2026-01-05", fx=0.80)]
    split = [{"con_id": 1, "date": "2026-02-01", "quantity": 10, "kind": "split"}]   # 2-for-1
    s = lots.summarise(lots.build_lots(rows, actions=split))[1]
    assert s.quantity == 20
    assert s.cost_local == 1000.0 and s.cost_gbp_tradedate == 800.0
    assert s.lots[0].price == 50.0


def test_fees_join_the_cost_and_follow_the_shares():
    rows = [dict(fill(1, "BUY", 10, 100.0, "2026-01-05", fx=0.80), commission=-3.0, taxes=5.0),
            fill(1, "SELL", 5, 100.0, "2026-02-05", fx=0.80)]
    s = lots.summarise(lots.build_lots(rows))[1]
    assert s.cost_local == 5 * 100.0 + 4.0                    # half the £8 of fees stays
    assert s.cost_gbp_tradedate == (500.0 + 4.0) * 0.80
    # A commission charged in another currency is left out rather than guessed.
    other = [dict(fill(2, "BUY", 1, 100.0, "2026-01-05", fx=0.80), commission=-3.0,
                  commission_currency="EUR")]
    assert lots.summarise(lots.build_lots(other))[2].cost_local == 100.0


def test_overselling_leaves_the_shortfall_visible():
    rows = [fill(1, "BUY", 5, 100.0, "2026-01-05", fx=0.80), fill(1, "SELL", 8, 100.0, "2026-01-06", fx=0.80)]
    s = lots.summarise(lots.build_lots(rows))[1]
    assert s.quantity == -3 and s.lots[-1].exec_id == "short"


def test_fx_sweeps_are_ignored():
    rows = [fill(1, "BUY", 5, 100.0, "2026-01-05", fx=0.80),
            {**fill(9, "SELL", 1000, 1.27, "2026-01-05"), "exchange": "IDEALFX", "asset": "CASH"}]
    assert set(lots.build_lots(rows)) == {1}
