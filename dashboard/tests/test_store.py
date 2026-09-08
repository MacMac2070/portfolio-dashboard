"""store.py is the archive. These pin the two promises everything else leans
on: a write never leaves a half-file behind, and a merge never loses a row."""
import json

import pytest

import store


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store, "NAV_PATH", tmp_path / "nav_history.jsonl")
    monkeypatch.setattr(store, "TX_PATH", tmp_path / "transactions.jsonl")
    monkeypatch.setattr(store, "NAV_CHANGE_PATH", tmp_path / "nav_change.jsonl")
    monkeypatch.setattr(store, "CASH_PATH", tmp_path / "cash_transactions.jsonl")
    monkeypatch.setattr(store, "POSITIONS_PATH", tmp_path / "positions_eod.json")
    monkeypatch.setattr(store, "LAST_RUN_PATH", tmp_path / "last_run.json")
    return tmp_path


# ---------------------------------------------------------------- write_json

def test_write_json_replaces_whole_file_and_leaves_no_temp(data_dir):
    path = data_dir / "doc.json"
    store.write_json(path, {"a": 1})
    store.write_json(path, {"b": [1, 2]})
    assert json.loads(path.read_text()) == {"b": [1, 2]}
    assert [p.name for p in data_dir.iterdir()] == ["doc.json"]


def test_write_json_keeps_old_file_when_payload_cannot_be_serialised(data_dir):
    path = data_dir / "doc.json"
    store.write_json(path, {"ok": True})
    with pytest.raises(TypeError):
        store.write_json(path, {"bad": object()})
    assert json.loads(path.read_text()) == {"ok": True}
    assert [p.name for p in data_dir.iterdir()] == ["doc.json"]


def test_read_json_never_raises(data_dir):
    assert store.read_json(data_dir / "missing.json") is None
    (data_dir / "broken.json").write_text("{not json")
    assert store.read_json(data_dir / "broken.json") is None


# ---------------------------------------------------------------- _write

def test_write_refuses_to_shrink(data_dir):
    store.merge_nav([{"date": "2026-01-05", "nav_gbp": 1.0, "source": "flex"},
                     {"date": "2026-01-06", "nav_gbp": 2.0, "source": "flex"}])
    with pytest.raises(RuntimeError):
        store._write(store.NAV_PATH, [{"date": "2026-01-05", "nav_gbp": 1.0}])
    assert store.nav_count() == 2


# ---------------------------------------------------------------- merges

def test_merge_nav_flex_replaces_a_snapshot_row_on_the_same_date(data_dir):
    store.merge_nav([{"date": "2026-01-05", "nav_gbp": 100.0, "source": "snapshot"}])
    added, total = store.merge_nav([{"date": "2026-01-05", "nav_gbp": 101.0, "source": "flex"}])
    assert (added, total) == (0, 1)
    assert store._read(store.NAV_PATH)[0]["nav_gbp"] == 101.0
    # But a snapshot never overwrites Flex.
    store.merge_nav([{"date": "2026-01-05", "nav_gbp": 99.0, "source": "snapshot"}])
    assert store._read(store.NAV_PATH)[0]["nav_gbp"] == 101.0


def test_merge_transactions_composite_key_without_exec_id(data_dir):
    row = {"time": "2026-01-05", "con_id": 1, "quantity": 5, "price": 10.0, "source": "ib"}
    assert store.merge_transactions([row]) == (1, 1)
    assert store.merge_transactions([dict(row)]) == (0, 1)
    assert store.merge_transactions([{**row, "price": 11.0}]) == (1, 2)


def test_merge_cash_dedupes_on_tx_id_and_later_wins(data_dir):
    store.merge_cash([{"tx_id": "t1", "date": "2026-01-05", "amount": 1.0}])
    added, total = store.merge_cash([{"tx_id": "t1", "date": "2026-01-05", "amount": 1.5}])
    assert (added, total) == (0, 1)
    assert store._read(store.CASH_PATH)[0]["amount"] == 1.5


# ---------------------------------------------------------------- calendar

@pytest.mark.parametrize("today, expected", [
    ("2026-09-02", "2026-09-02"),   # Wednesday: today
    ("2026-09-05", "2026-09-04"),   # Saturday: Friday
    ("2026-09-06", "2026-09-04"),   # Sunday: Friday
])
def test_expected_report_date_is_last_weekday(today, expected):
    from datetime import date
    assert store.expected_report_date(date.fromisoformat(today)) == expected


def test_expected_report_date_waits_for_the_evening_statement():
    from datetime import datetime
    # Wednesday 3 Sep, 09:00: Tuesday's statement is the newest there is.
    assert store.expected_report_date(now=datetime(2026, 9, 3, 9, 0)) == "2026-09-02"
    # Wednesday 3 Sep, 23:30: today's is out.
    assert store.expected_report_date(now=datetime(2026, 9, 3, 23, 30)) == "2026-09-03"
    # Monday 7 Sep, 09:00: Friday's.
    assert store.expected_report_date(now=datetime(2026, 9, 7, 9, 0)) == "2026-09-04"


def test_stale_gates_judge_content_first(data_dir, monkeypatch):
    from datetime import datetime
    import os, time
    monkeypatch.setattr(store, "datetime", type("DT", (), {"now": staticmethod(lambda: datetime(2026, 9, 2, 23, 30))}))
    store.write_positions_eod([{"report_date": "2026-09-01", "con_id": 1, "quantity": 1}])
    assert store.positions_eod_stale() is False         # a day behind, but asked minutes ago
    old = time.time() - 3 * 3600
    os.utime(store.POSITIONS_PATH, (old, old))
    assert store.positions_eod_stale() is True          # a day behind and not asked for hours
    store.write_positions_eod([{"report_date": "2026-09-02", "con_id": 1, "quantity": 1}])
    os.utime(store.POSITIONS_PATH, (old, old))
    assert store.positions_eod_stale() is False         # current, however old the file
