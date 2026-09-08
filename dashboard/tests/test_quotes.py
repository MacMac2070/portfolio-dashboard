"""quotes.py: the daily-close cache. A temp database per test."""
from datetime import date

import pytest

import quotes


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(quotes, "DB_PATH", tmp_path / "quotes.sqlite3")
    monkeypatch.setattr(quotes, "_ready", False)
    yield tmp_path


def test_upsert_is_idempotent_and_skips_junk():
    assert quotes.upsert("^FTSE", [("2026-09-01", 9100.5), ("2026-09-02", 9120.0)], currency="GBP") == 2
    assert quotes.upsert("^FTSE", [("2026-09-02", 9125.0), ("2026-09-03", None),
                                   ("2026-09-04", float("nan")), ("2026-09-05", 0.0)]) == 1
    assert quotes.series("^FTSE") == [("2026-09-01", 9100.5), ("2026-09-02", 9125.0)]   # restated
    assert quotes.coverage("^FTSE") == ("2026-09-01", "2026-09-02", 2)


def test_series_is_ordered_and_bounded():
    quotes.upsert("X", [("2026-09-03", 3.0), ("2026-09-01", 1.0), ("2026-09-02", 2.0)])
    assert quotes.series("X") == [("2026-09-01", 1.0), ("2026-09-02", 2.0), ("2026-09-03", 3.0)]
    assert quotes.series("X", start="2026-09-02") == [("2026-09-02", 2.0), ("2026-09-03", 3.0)]
    assert quotes.series("X", end="2026-09-02") == [("2026-09-01", 1.0), ("2026-09-02", 2.0)]
    assert quotes.latest("X") == ("2026-09-03", 3.0)
    assert quotes.latest("nope") is None


def test_at_or_before_never_returns_a_future_close():
    quotes.upsert("X", [("2026-09-01", 1.0), ("2026-09-04", 4.0)])
    assert quotes.at_or_before("X", "2026-09-03") == ("2026-09-01", 1.0)
    assert quotes.at_or_before("X", "2026-09-04") == ("2026-09-04", 4.0)
    assert quotes.at_or_before("X", "2026-08-31") is None


def test_plan_fetch_full_when_coverage_is_missing_or_old_else_one_week():
    today = date(2026, 9, 2)
    assert quotes.plan_fetch("X", today) == "2025-07-09"                   # 420 days back
    quotes.upsert("X", [("2026-08-01", 1.0)])
    assert quotes.plan_fetch("X", today) == "2025-07-09"                   # 32 days old: full
    quotes.upsert("X", [("2026-09-01", 1.0)])
    assert quotes.plan_fetch("X", today) == "2026-08-25"                   # a week's overlap
    quotes.upsert("Y", [("2026-09-01", 1.0)])
    assert quotes.plan_fetch_all(["X", "Y", "Z"], today) == "2025-07-09"   # Z has nothing


def test_stale_symbols_counts_weekdays():
    quotes.upsert("FRESH", [("2026-09-01", 1.0)])
    quotes.upsert("OLD", [("2026-08-26", 1.0)])
    out = quotes.stale_symbols(["FRESH", "OLD", "MISSING"], date(2026, 9, 2))
    assert out == {"OLD": "2026-08-26", "MISSING": None}
