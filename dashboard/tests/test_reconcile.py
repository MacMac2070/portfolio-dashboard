"""reconcile.py: every check is a pure function over rows, so every case here
is checkable on paper. Dates are pinned; nothing reads the real stores."""
from datetime import date

import pytest

import reconcile
import store

TODAY = date(2026, 9, 2)   # Wednesday


def tx(con_id, side, qty, when, symbol="X", exchange="NASDAQ", asset=None):
    return {"con_id": con_id, "side": side, "quantity": qty, "time": when,
            "symbol": symbol, "exchange": exchange, "asset": asset}


def eod(asof, *positions):
    return {"asof": asof, "positions": [
        {"con_id": c, "symbol": s, "quantity": q, "value": v, "currency": "USD", "fx_to_base": fx}
        for c, s, q, v, fx in positions]}


# ---------------------------------------------------------------- replay

def test_replay_excludes_fx_sweeps_and_counts_legacy_rows():
    rows = [tx(1, "BUY", 100, "2026-01-05"),
            tx(1, "SELL", 40, "2026-02-01"),
            tx(9, "BUY", 1000, "2026-01-06", symbol="GBP.USD", exchange="IDEALFX"),
            tx(8, "SELL", 500, "2026-01-06", symbol="EUR.GBP", exchange="IDEALFX", asset="CASH"),
            tx(2, "BOT", 7.5, "2026-03-01", asset="STK")]
    assert reconcile.replay_positions(rows) == {1: 60.0, 2: 7.5}
    assert reconcile.replay_positions(rows, upto="2026-01-31") == {1: 100.0}


def test_replay_tolerates_fractional_shares_and_actions():
    rows = [tx(2, "BUY", 7.5, "2026-03-01"), tx(2, "BUY", 2, "2026-08-25")]
    split = [{"con_id": 2, "quantity": 9.5, "date": "2026-08-30"}]     # 2-for-1
    assert reconcile.replay_positions(rows) == {2: 9.5}
    assert reconcile.replay_positions(rows, actions=split) == {2: 19.0}
    assert reconcile.diff_positions({2: 9.5}, eod("2026-08-31", (2, "AMZN", 9.5, 1, 1))["positions"]) == []


def test_replay_ledger_older_than_positions_is_warn_not_fail():
    rows = [tx(1, "BUY", 100, "2026-07-15", symbol="HSBA")]
    snap = eod("2026-08-31", (1, "HSBA", 200, 1, 1))
    check = reconcile.check_replay(rows, snap)
    assert check["status"] == "warn"
    assert "Ledger ends 2026-07-15" in check["summary"]
    # Same disagreement with a ledger that reaches the snapshot date is a failure.
    rows.append(tx(3, "BUY", 1, "2026-08-31", symbol="Z"))
    assert reconcile.check_replay(rows, snap)["status"] == "fail"
    rows.append(tx(1, "BUY", 100, "2026-08-25", symbol="HSBA"))
    check = reconcile.check_replay(rows, eod("2026-08-31", (1, "HSBA", 200, 1, 1), (3, "Z", 1, 1, 1)))
    assert check["status"] == "ok"


def test_replay_without_snapshot_is_pending():
    assert reconcile.check_replay([], None)["status"] == "pending"


# ---------------------------------------------------------------- NAV series

def nav(*pairs, source="flex"):
    return [{"date": d, "nav_gbp": v, "source": source} for d, v in pairs]


def test_nav_weekend_is_not_a_gap_but_two_missing_weekdays_fail():
    fri_mon = nav(("2026-08-28", 100), ("2026-08-31", 101), ("2026-09-01", 102))
    assert reconcile.nav_continuity(fri_mon, today=TODAY)["status"] == "ok"
    hole = nav(("2026-08-26", 100), ("2026-08-31", 101), ("2026-09-01", 102))   # Thu, Fri missing
    check = reconcile.nav_continuity(hole, today=TODAY)
    assert check["status"] == "fail"
    assert check["detail"]["gaps"][0]["missing_weekdays"] == 2
    one = nav(("2026-08-27", 100), ("2026-08-31", 101), ("2026-09-01", 102))    # Fri missing
    assert reconcile.nav_continuity(one, today=TODAY)["status"] == "warn"


def test_nav_lag_duplicates_zeros_and_unreplaced_snapshots():
    stale = nav(("2026-08-27", 100), ("2026-08-28", 101))
    check = reconcile.nav_continuity(stale, today=TODAY)
    assert check["status"] == "fail" and check["detail"]["lag_weekdays"] == 3
    dup = nav(("2026-09-01", 100), ("2026-09-01", 100))
    assert reconcile.nav_continuity(dup, today=TODAY)["detail"]["duplicates"] == ["2026-09-01"]
    padded = nav(("2025-07-30", 0), ("2025-10-13", 10), ("2025-10-14", 0), ("2026-09-01", 12))
    check = reconcile.nav_continuity(padded, today=TODAY)
    assert check["detail"]["zeros"] == ["2025-10-14"]      # the leading zero is padding
    snap = nav(("2026-08-24", 100, ), ("2026-09-01", 101))
    snap[0]["source"] = "snapshot"
    assert "2026-08-24" in reconcile.nav_continuity(snap, today=TODAY)["detail"]["unreplaced_snapshots"]


# ---------------------------------------------------------------- deposits

def cash(*rows):
    return [{"date": d, "bucket": "deposits_withdrawals", "amount_gbp": a} for d, a in rows]


def change(from_date, to_date, deposits, span=None):
    return {"from_date": from_date, "to_date": to_date, "depositsWithdrawals": deposits,
            "span_days": span if span is not None else 30}


def test_deposits_check_clips_cash_to_the_latest_subperiod():
    changes = [change("2026-08-01", "2026-08-15", 1000.0),      # superseded restatement
               change("2026-08-01", "2026-08-28", 3000.0),
               change("2025-08-20", "2026-08-28", 3000.0, span=373)]
    rows = cash(("2026-08-05", 1000.0), ("2026-08-20", 2000.0), ("2026-08-30", 500.0))
    check = reconcile.deposits_vs_nav_change(rows, changes)
    assert check["status"] == "ok", check


def test_deposits_dated_on_a_weekend_match_within_the_settlement_window():
    # A £10 deposit dated Saturday 11 Oct; IBKR books it on Monday the 13th.
    rows = cash(("2025-10-11", 10.0), ("2025-10-16", -10.0), ("2025-10-16", 1000.0),
                ("2025-11-03", 500.0))                                   # next month's, not ours
    check = reconcile.deposits_vs_nav_change(rows, [change("2025-10-13", "2025-10-31", 1000.0)])
    assert check["status"] == "ok" and check["detail"]["settled"]


def test_nav_weekend_snapshot_row_is_named_and_not_blamed_on_flex():
    rows = nav(("2026-07-24", 100), ("2026-07-27", 101), ("2026-07-28", 102))
    rows.insert(1, {"date": "2026-07-26", "nav_gbp": 100.5, "source": "snapshot"})   # a Sunday
    check = reconcile.nav_continuity(rows, today=date(2026, 7, 29))
    assert check["status"] == "warn"
    assert check["detail"]["weekend_snapshots"] == ["2026-07-26"]
    assert "Delete the weekend row" in check["hint"]


def test_deposits_present_in_nav_change_but_no_cash_rows_is_fail():
    check = reconcile.deposits_vs_nav_change([], [change("2026-08-01", "2026-08-28", 3000.0)])
    assert check["status"] == "fail"
    assert "Deposits/Withdrawals" in check["hint"]


def test_deposits_mismatch_is_warn_and_no_periods_is_pending():
    rows = cash(("2026-08-05", 1000.0))
    check = reconcile.deposits_vs_nav_change(rows, [change("2026-08-01", "2026-08-28", 3000.0)])
    assert check["status"] == "warn" and check["detail"][0]["cash_rows"] == 1000.0
    assert reconcile.deposits_vs_nav_change(rows, [])["status"] == "pending"


# ---------------------------------------------------------------- composition

def test_positions_vs_nav_pending_without_a_same_date_row_or_split():
    snap = eod("2026-09-01", (1, "A", 10, 1000, 0.75))
    assert reconcile.positions_vs_nav(snap, None)["status"] == "pending"
    assert reconcile.positions_vs_nav(snap, {"date": "2026-09-01", "nav_gbp": 800})["status"] == "pending"


def test_positions_vs_nav_within_half_percent_passes_else_fails():
    snap = eod("2026-09-01", (1, "A", 10, 1000, 0.75), (2, "B", 5, 200, 1.0))   # £950
    row = {"date": "2026-09-01", "nav_gbp": 1051.0, "stock_gbp": 952.0, "cash_gbp": 99.0}
    assert reconcile.positions_vs_nav(snap, row)["status"] == "ok"
    row["stock_gbp"], row["nav_gbp"] = 1000.0, 1099.0
    check = reconcile.positions_vs_nav(snap, row)
    assert check["status"] == "fail" and "statement stock" in check["summary"]
    row["stock_gbp"], row["nav_gbp"] = 952.0, 1200.0                              # parts ≠ total
    assert reconcile.positions_vs_nav(snap, row)["status"] == "warn"


# ---------------------------------------------------------------- live vs Flex

def test_live_diff_is_skipped_for_the_flex_feed_and_warns_on_a_quantity_change():
    snap = eod("2026-09-01", (1, "A", 10, 1000, 0.75))
    flex_live = {"meta": {"source": "flex-eod"}, "positions": [{"con_id": 1, "quantity": 10}]}
    assert reconcile.diff_live(flex_live, snap, []) is None
    live = {"meta": {"source": "ib-live"}, "kpis": {"invested": 760.0, "net_liquidation": 900.0},
            "positions": [{"con_id": 1, "symbol": "A", "quantity": 12}], "fx": {}}
    check = reconcile.diff_live(live, snap, nav(("2026-09-01", 890.0)))
    assert check["status"] == "warn" and "quantity difference" in check["summary"]
    live["positions"][0]["quantity"] = 10
    assert reconcile.diff_live(live, snap, nav(("2026-09-01", 890.0)))["status"] == "ok"


# ---------------------------------------------------------------- run

@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    for name, filename in (("NAV_PATH", "nav_history.jsonl"), ("TX_PATH", "transactions.jsonl"),
                           ("CASH_PATH", "cash_transactions.jsonl"), ("NAV_CHANGE_PATH", "nav_change.jsonl"),
                           ("POSITIONS_PATH", "positions_eod.json"), ("QUALITY_PATH", "quality.json"),
                           ("ACTIONS_PATH", "corporate_actions.jsonl"), ("FX_PATH", "fx_rates.jsonl")):
        monkeypatch.setattr(store, name, tmp_path / filename)
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    return tmp_path


def test_run_writes_quality_json_with_the_worst_status(data_dir):
    store.merge_nav(nav(("2026-08-28", 100), ("2026-08-31", 101), ("2026-09-01", 102)))
    store.merge_transactions([tx(1, "BUY", 100, "2026-07-15", symbol="HSBA")])
    store.write_positions_eod([{"report_date": "2026-09-01", "con_id": 1, "symbol": "HSBA",
                               "quantity": 200, "value": 100, "currency": "GBP", "fx_to_base": 1}])
    report = reconcile.run(today=TODAY)
    assert report["status"] == "warn"                       # ledger older than positions
    by_id = {c["id"]: c["status"] for c in report["checks"]}
    assert by_id["positions.replay"] == "warn"
    assert by_id["nav.continuity"] == "ok"
    assert by_id["flows.deposits"] == "pending"
    assert by_id["nav.composition"] == "pending"
    assert store.read_json(store.QUALITY_PATH)["asof"]["positions"] == "2026-09-01"


def test_split_in_the_actions_store_reconciles_the_replay(data_dir):
    store.merge_nav(nav(("2026-08-31", 100), ("2026-09-01", 102)))
    store.merge_transactions([tx(1, "BUY", 100, "2026-07-15", symbol="NVDA")])
    store.write_positions_eod([{"report_date": "2026-09-01", "con_id": 1, "symbol": "NVDA",
                               "quantity": 400, "value": 100, "mark_price": 1.0, "currency": "USD", "fx_to_base": 0.75}])
    assert {c["id"]: c["status"] for c in reconcile.run(today=TODAY)["checks"]}["positions.replay"] == "warn"
    store.merge_corporate_actions([{"action_id": "a1", "con_id": 1, "date": "2026-08-01",
                                    "kind": "split", "code": "FS", "quantity": 300}])
    by_id = {c["id"]: c for c in reconcile.run(today=TODAY)["checks"]}
    assert by_id["positions.replay"]["status"] == "ok"
    assert "actions.unbucketed" not in by_id


def test_unknown_action_codes_and_unmarked_positions_are_named(data_dir):
    store.write_positions_eod([{"report_date": "2026-09-01", "con_id": 1, "symbol": "GONE",
                               "quantity": 5, "value": None, "mark_price": None, "currency": "USD"}])
    store.merge_corporate_actions([{"action_id": "x", "con_id": 1, "date": "2026-08-01",
                                    "kind": "other", "code": "QQ", "quantity": 0}])
    by_id = {c["id"]: c for c in reconcile.run(today=TODAY)["checks"]}
    assert by_id["actions.unbucketed"]["status"] == "warn" and "QQ" in by_id["actions.unbucketed"]["summary"]
    assert by_id["positions.unmarked"]["status"] == "warn"
