"""flexfeed.py is the floor under the dashboard: EOD positions repriced by
delayed quotes, and the rule that decides when the Gateway feed wins."""
import pytest

import flexfeed
import store


class Feed:
    def __init__(self, payload):
        self.payload = payload
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        pass

    def snapshot(self):
        return self.payload


def live(connected, last_refresh):
    return {"meta": {"connected": connected, "last_refresh": last_refresh, "source": "ib-live"},
            "positions": [{"con_id": 1}]}


def test_failover_prefers_the_gateway_only_when_connected_and_composed():
    flex = Feed({"meta": {"source": "flex-eod", "connected": True}, "positions": [{"con_id": 1}]})
    assert flexfeed.FailoverFeed(Feed(live(True, "2026-09-02T10:00:00+00:00")), flex).snapshot()["meta"]["source"] == "ib-live"
    # A zombie gateway: socket accepted, nothing ever composed.
    assert flexfeed.FailoverFeed(Feed(live(True, None)), flex).snapshot()["meta"]["source"] == "flex-eod"
    assert flexfeed.FailoverFeed(Feed(live(False, "2026-09-02T10:00:00+00:00")), flex).snapshot()["meta"]["source"] == "flex-eod"


def test_failover_serves_whatever_is_left_when_both_are_dark():
    # The live feed's own (disconnected) payload wins over an empty Flex one,
    # so the page keeps the last values it had rather than going blank.
    empty_flex = Feed({"meta": {"source": "flex-eod"}, "positions": []})
    out = flexfeed.FailoverFeed(Feed(live(False, None)), empty_flex).snapshot()
    assert out["meta"]["source"] == "ib-live" and out["meta"]["connected"] is False
    # Nothing at all: an honest empty payload, never an exception.
    out = flexfeed.FailoverFeed(None, None).snapshot()
    assert out["positions"] == [] and out["meta"]["connected"] is False


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store, "NAV_PATH", tmp_path / "nav_history.jsonl")
    monkeypatch.setattr(store, "POSITIONS_PATH", tmp_path / "positions_eod.json")
    store.write_positions_eod([
        {"report_date": "2026-09-01", "con_id": 265598, "symbol": "AAPL", "exchange": "NASDAQ",
         "currency": "USD", "quantity": 10, "mark_price": 200.0, "value": 2000.0,
         "average_cost": 150.0, "unrealized_pnl": 500.0, "fx_to_base": 0.75},
    ])
    return tmp_path


def compose(**kw):
    return flexfeed.compose_payload(quotes=kw.pop("quotes", {}), fx=kw.pop("fx", {"USD": 0.75}),
                                    sparks=kw.pop("sparks", {}), **kw)


def test_compose_reprices_from_a_quote_and_infers_cash(stores):
    store.merge_nav([{"date": "2026-09-01", "nav_gbp": 1600.0, "source": "flex"}])
    out = compose(quotes={265598: {"last": 210.0, "prev": 200.0, "symbol": "AAPL"}})
    pos = out["positions"][0]
    assert pos["value_gbp"] == pytest.approx(210.0 * 10 * 0.75)
    assert out["kpis"]["cash_available"] == pytest.approx(1600.0 - 1500.0)   # NAV − EOD value
    assert out["meta"]["cash_source"] == "inferred"
    assert out["meta"]["source"] == "flex-eod"


def test_compose_uses_the_statement_cash_when_the_nav_row_carries_it(stores):
    store.merge_nav([{"date": "2026-09-01", "nav_gbp": 1600.0, "source": "flex",
                      "cash_gbp": 123.45, "stock_gbp": 1476.55}])
    out = compose()
    assert out["kpis"]["cash_available"] == pytest.approx(123.45)
    assert out["meta"]["cash_source"] == "flex-nav"


def test_compose_keeps_the_mark_on_a_units_slip_and_says_so(stores):
    # 100x the mark is pence for pounds or a magnifier, never a price.
    store.merge_nav([{"date": "2026-09-01", "nav_gbp": 1600.0, "source": "flex"}])
    out = compose(quotes={265598: {"last": 20000.0, "prev": 200.0, "symbol": "AAPL"}})
    pos = out["positions"][0]
    assert pos["value_gbp"] == pytest.approx(2000.0 * 0.75)          # the mark stands
    assert pos["price_source"] == "statement"
    assert out["meta"]["repricing"] == {"quoted": 0, "rejected_units": ["AAPL"],
                                        "large_moves": [], "unquoted": []}


def test_units_reject_also_refuses_the_day_change(stores):
    # Cursor's catch: the price declined the pence print, the day change
    # must not be painted off it either.
    store.merge_nav([{"date": "2026-09-01", "nav_gbp": 1600.0, "source": "flex"}])
    out = compose(quotes={265598: {"last": 20000.0, "prev": 20100.0, "symbol": "AAPL"}})
    pos = out["positions"][0]
    assert pos["price_source"] == "statement"
    assert pos["day_change_pct"] is None and pos["day_pnl_gbp"] is None
    assert pos["day_change_source"] == "none"


def test_markless_row_still_gets_the_units_check(stores):
    # No mark, only a value: the implied price is the reference, so a 100x
    # quote is still refused rather than inflating the row.
    store.write_positions_eod([
        {"report_date": "2026-09-01", "con_id": 265598, "symbol": "AAPL", "exchange": "NASDAQ",
         "currency": "USD", "quantity": 10, "mark_price": None, "value": 2000.0,
         "average_cost": 150.0, "unrealized_pnl": 500.0, "fx_to_base": 0.75},
    ])
    store.merge_nav([{"date": "2026-09-01", "nav_gbp": 1600.0, "source": "flex"}])
    out = compose(quotes={265598: {"last": 20000.0, "prev": 200.0, "symbol": "AAPL"}})
    assert out["positions"][0]["value_gbp"] == pytest.approx(2000.0 * 0.75)
    assert out["meta"]["repricing"]["rejected_units"] == ["AAPL"]


def test_units_error_guards_degenerate_inputs():
    assert flexfeed._is_units_error(100.0, 1.0) and flexfeed._is_units_error(1.0, 100.0)
    assert flexfeed._is_units_error(50.0, 1.0) and flexfeed._is_units_error(200.0, 1.0)
    assert not flexfeed._is_units_error(49.9, 1.0) and not flexfeed._is_units_error(1.6, 1.0)
    for last, ref in ((0.0, 1.0), (1.0, 0.0), (None, 1.0), (1.0, None), (-100.0, 1.0), (100.0, -1.0)):
        assert not flexfeed._is_units_error(last, ref)


def test_intraday_fx_overlay_refuses_booleans_and_infinities():
    base = {"GBP": 1.0, "USD": 1.0}
    fx, skipped = flexfeed.overlay_intraday_fx(base, {"USD": True})
    assert fx["USD"] == 1.0 and skipped == ["USD"]
    fx, skipped = flexfeed.overlay_intraday_fx(base, {"USD": float("inf")})
    assert fx["USD"] == 1.0 and skipped == ["USD"]


def test_compose_applies_a_large_move_but_lists_it(stores):
    # −90% is a price, not a slip: freezing it at the mark would hide a rout.
    store.merge_nav([{"date": "2026-09-01", "nav_gbp": 1600.0, "source": "flex"}])
    out = compose(quotes={265598: {"last": 20.0, "prev": 200.0, "symbol": "AAPL"}})
    pos = out["positions"][0]
    assert pos["value_gbp"] == pytest.approx(20.0 * 10 * 0.75)
    assert pos["price_source"] == "yahoo"
    assert out["meta"]["repricing"]["large_moves"] == ["AAPL"]
    assert out["meta"]["repricing"]["quoted"] == 1


def test_compose_marks_an_unquoted_row(stores):
    store.merge_nav([{"date": "2026-09-01", "nav_gbp": 1600.0, "source": "flex"}])
    out = compose(quotes={})
    assert out["positions"][0]["price_source"] == "statement"
    assert out["meta"]["repricing"]["unquoted"] == ["AAPL"]


def test_snapshot_never_raises_and_reports_the_quote_breaker():
    # The snapshot contract is "never raises, never blocks": a cold feed with
    # no payload must still answer, and say whether quotes are flowing.
    out = flexfeed.FlexFeed().snapshot()
    assert out["meta"]["warming_up"] is True and out["positions"] == []
    assert out["meta"]["quotes_breaker"] == "closed"
    assert out["meta"]["source"] == "flex-eod" and out["meta"]["connected"] is False


def test_quote_symbol_falls_back_to_the_universe(monkeypatch):
    import marketdata
    import universe
    monkeypatch.setattr(marketdata, "QUOTE_SYMBOLS", {})
    # VUSA is watched, not held, so it carries no conId; lend it one.
    t = universe.TICKERS["VUSA"]
    monkeypatch.setitem(universe.TICKERS, "VUSA", type(t)(**{**t.__dict__, "con_id": 4242}))
    assert marketdata.quote_symbol(4242) == "VUSA.L"
    assert marketdata.quote_symbol(999999999) is None
    monkeypatch.setattr(marketdata, "QUOTE_SYMBOLS", {4242: "OVERRIDE.L"})
    assert marketdata.quote_symbol(4242) == "OVERRIDE.L"                # the dict wins


def test_intraday_fx_overlay_takes_only_sane_prints():
    base = {"GBP": 1.0, "USD": 0.75, "HKD": 0.095}
    fx, skipped = flexfeed.overlay_intraday_fx(
        base, {"USD": 0.76,          # within 5%: applied
               "HKD": 0.12,          # 26% off: refused
               "JPY": 0.0051,        # no reference: refused
               "EUR": float("nan")}) # not a number: refused
    assert fx["USD"] == 0.76 and fx["HKD"] == 0.095 and "JPY" not in fx and "EUR" not in fx
    assert sorted(skipped) == ["EUR", "HKD", "JPY"]
    assert base["USD"] == 0.75                                         # the base is not mutated
