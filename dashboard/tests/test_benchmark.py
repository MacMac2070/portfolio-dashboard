"""The range windows: calendar cutoffs, mirrored between benchmark.py and
js/main.js. These tests pin the server half of that mirror — the client
asserts count parity at runtime, so a drift here makes the benchmark line
silently vanish."""
from datetime import date, timedelta

import benchmark


def business_days(start: str, n: int) -> list[str]:
    """n weekday dates from start — the shape nav_history actually has."""
    out, d = [], date.fromisoformat(start)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def test_one_month_is_a_calendar_month_not_thirty_rows():
    dates = business_days("2026-06-01", 60)          # ~12 calendar weeks
    start = benchmark.slice_start(dates, "1M")
    window = dates[start:]
    span = (date.fromisoformat(window[-1]) - date.fromisoformat(window[0])).days
    assert span <= 30                                 # a month, not 6 weeks
    assert 20 <= len(window) <= 23                    # ~22 trading days


def test_one_day_is_the_last_two_rows():
    dates = business_days("2026-06-01", 10)
    assert benchmark.slice_start(dates, "1D") == 8


def test_all_is_everything():
    dates = business_days("2026-06-01", 10)
    assert benchmark.slice_start(dates, "ALL") == 0


def test_window_anchors_on_the_data_edge_not_the_wall_clock():
    # A series that stopped a week ago still yields a full month ending at
    # its own last date — staleness must not shrink the window.
    dates = business_days("2026-06-01", 40)
    start = benchmark.slice_start(dates, "1M")
    cutoff = date.fromisoformat(dates[-1]) - timedelta(days=30)
    assert date.fromisoformat(dates[start]) >= cutoff
    assert date.fromisoformat(dates[start - 1]) < cutoff


def test_short_series_falls_back_to_two_rows():
    dates = business_days("2026-06-01", 1)
    assert benchmark.slice_start(dates, "1Y") == 0
    assert benchmark.slice_start([], "1M") == 0


def test_build_counts_stay_parallel():
    # The count contract: the nav slice and the index slice must always be
    # the same length, whatever the range.
    dates = business_days("2026-01-05", 120)
    nav_rows = [{"date": d, "nav_gbp": 50000 + i} for i, d in enumerate(dates)]
    index_rows = [(d, 100.0 + i) for i, d in enumerate(dates)]
    out = benchmark.build(nav_rows, index_rows, "^X", "Index", "GBP")
    for key, slot in out["ranges"].items():
        assert slot["count"] == len(slot["points"]), key
    # And 1M genuinely covers about a month of those dates.
    month = out["ranges"]["1M"]
    assert 20 <= month["count"] <= 23
