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


def test_compose_rejects_a_quote_far_from_the_broker_mark(stores):
    store.merge_nav([{"date": "2026-09-01", "nav_gbp": 1600.0, "source": "flex"}])
    out = compose(quotes={265598: {"last": 20.0, "prev": 200.0, "symbol": "AAPL"}})   # −90%
    assert out["positions"][0]["value_gbp"] == pytest.approx(2000.0 * 0.75)          # the mark stands
