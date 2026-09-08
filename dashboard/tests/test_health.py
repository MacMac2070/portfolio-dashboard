"""health.py turns the files on disk into one verdict. Every clock is pinned."""
import json
from datetime import date, datetime, timezone

import pytest

import health
import store

NOW = datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc)      # Wednesday morning
TODAY = date(2026, 9, 2)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store, "NAV_PATH", tmp_path / "nav_history.jsonl")
    monkeypatch.setattr(store, "TX_PATH", tmp_path / "transactions.jsonl")
    monkeypatch.setattr(store, "POSITIONS_PATH", tmp_path / "positions_eod.json")
    monkeypatch.setattr(store, "LAST_RUN_PATH", tmp_path / "last_run.json")
    monkeypatch.setattr(store, "QUALITY_PATH", tmp_path / "quality.json")
    (tmp_path / "portfolio.json").write_text(json.dumps(
        {"meta": {"source": "ib-live", "generated_at": "2026-09-01T22:35:00+00:00"}}))
    return tmp_path


def good_run(data_dir, finished="2026-09-01T22:40:00+00:00", **extra):
    store.write_json(store.LAST_RUN_PATH, {
        "schema_version": 1, "started_at": "2026-09-01T22:30:00+00:00",
        "finished_at": finished, "ok": True, "exit_code": 0, "stage": "done",
        "stages": {"gateway": "ok"}, "archive": {"nav": 10, "tx": 5},
        "archive_previous": {"nav": 9, "tx": 5}, **extra})


def nav_through(day):
    store.merge_nav([{"date": "2026-08-01", "nav_gbp": 1.0, "source": "flex"},
                     {"date": day, "nav_gbp": 2.0, "source": "flex"}])


def positions_asof(day):
    store.write_positions_eod([{"report_date": day, "con_id": 1, "quantity": 1}])


def by_id(report):
    return {c["id"]: c for c in report["checks"]}


# ---------------------------------------------------------------- job

def test_missing_last_run_is_fail(data_dir):
    nav_through("2026-09-01"); positions_asof("2026-09-01")
    checks = by_id(health.build(today=TODAY, now=NOW))
    assert checks["job.last_run"]["status"] == "fail"
    assert "launchctl kickstart" in checks["job.last_run"]["hint"]


def test_recent_success_is_ok_and_missed_nights_escalate(data_dir):
    nav_through("2026-09-01"); positions_asof("2026-09-01")
    good_run(data_dir)
    assert by_id(health.build(today=TODAY, now=NOW))["job.last_run"]["status"] == "ok"
    good_run(data_dir, finished="2026-08-31T22:40:00+00:00")
    assert by_id(health.build(today=TODAY, now=NOW))["job.last_run"]["status"] == "warn"
    good_run(data_dir, finished="2026-08-29T22:40:00+00:00")
    assert by_id(health.build(today=TODAY, now=NOW))["job.last_run"]["status"] == "fail"


def test_run_that_never_finished_is_fail_after_the_budget(data_dir):
    store.write_json(store.LAST_RUN_PATH, {"started_at": "2026-08-31T22:30:00+00:00",
                                           "ok": None, "stage": "directory"})
    check = by_id(health.build(today=TODAY, now=NOW))["job.last_run"]
    assert check["status"] == "fail" and "directory" in check["summary"]
    # ...but a run that started ten minutes ago is simply in progress.
    store.write_json(store.LAST_RUN_PATH, {"started_at": "2026-09-02T07:50:00+00:00",
                                           "ok": None, "stage": "gateway"})
    assert by_id(health.build(today=TODAY, now=NOW))["job.last_run"]["status"] == "pending"


def test_degraded_stage_and_shrunk_archive_are_reported(data_dir):
    nav_through("2026-09-01"); positions_asof("2026-09-01")
    good_run(data_dir, stages={"gateway": "ok", "directory": "abandoned"},
             archive={"nav": 8, "tx": 5})
    checks = by_id(health.build(today=TODAY, now=NOW))
    assert checks["job.stages"]["status"] == "warn"
    assert checks["archive.shrunk"]["status"] == "fail"


# ---------------------------------------------------------------- stores

def test_friday_nav_is_ok_on_monday_morning(data_dir):
    good_run(data_dir, finished="2026-09-06T22:40:00+00:00")
    nav_through("2026-09-04"); positions_asof("2026-09-04")
    report = health.build(today=date(2026, 9, 7), now=datetime(2026, 9, 7, 8, tzinfo=timezone.utc))
    assert by_id(report)["nav.lag"]["status"] == "ok"


def test_nav_lag_counts_only_statements_that_can_exist(data_dir):
    good_run(data_dir)
    # Wednesday morning: Tuesday's statement is the newest there is, so a
    # series through Friday is two behind (Mon, Tue) and one through Monday is fine.
    nav_through("2026-08-28"); positions_asof("2026-09-01")
    checks = by_id(health.build(today=TODAY, now=NOW))
    assert checks["nav.lag"]["status"] == "warn"
    assert "2 weekdays behind" in checks["nav.lag"]["summary"]
    assert checks["positions.asof"]["status"] == "ok"
    # Wednesday evening, after the cutoff: today's statement is out, so three.
    evening = datetime(2026, 9, 2, 16, 30, tzinfo=timezone.utc)      # 23:30 local (UTC+7)
    checks = by_id(health.build(today=TODAY, now=evening))
    assert checks["nav.lag"]["status"] == "fail"
    # The positions file is a snapshot, so it can be moved back: three behind fails.
    positions_asof("2026-08-27")
    assert by_id(health.build(today=TODAY, now=NOW))["positions.asof"]["status"] == "fail"


# ---------------------------------------------------------------- overall

def test_overall_status_is_the_worst_check_and_pending_counts_as_ok(data_dir):
    good_run(data_dir); nav_through("2026-09-01"); positions_asof("2026-09-01")
    report = health.build(today=TODAY, now=NOW)
    assert by_id(report)["feed"]["status"] == "pending"
    assert report["status"] == "ok"
    (data_dir / "portfolio.json").write_text(json.dumps(
        {"meta": {"gateway": "unavailable", "source": "flex-eod", "positions_asof": "2026-09-01"}}))
    assert health.build(today=TODAY, now=NOW)["status"] == "warn"


def test_open_breaker_is_warn_not_fail(data_dir):
    good_run(data_dir); nav_through("2026-09-01"); positions_asof("2026-09-01")
    report = health.build(today=TODAY, now=NOW,
                          breakers={"yfinance": {"state": "open", "retry_after": 90,
                                                 "last_error": "401"}})
    assert by_id(report)["breakers"]["status"] == "warn"
    assert report["status"] == "warn"


def test_feed_meta_drives_the_feed_check(data_dir):
    good_run(data_dir); nav_through("2026-09-01"); positions_asof("2026-09-01")
    live = {"connected": True, "source": "ib-live", "last_refresh": "2026-09-02T07:59:30+00:00",
            "poll_seconds": 3}
    assert by_id(health.build(feed=live, today=TODAY, now=NOW))["feed"]["status"] == "ok"
    flex = {**live, "source": "flex-eod", "positions_asof": "2026-09-01", "poll_seconds": 60}
    assert by_id(health.build(feed=flex, today=TODAY, now=NOW))["feed"]["status"] == "warn"
    down = {"connected": False, "source": "none", "error": "refused"}
    assert by_id(health.build(feed=down, today=TODAY, now=NOW))["feed"]["status"] == "warn"


def test_quality_checks_are_folded_in(data_dir):
    good_run(data_dir); nav_through("2026-09-01"); positions_asof("2026-09-01")
    store.write_json(store.QUALITY_PATH, {"checks": [
        {"id": "positions.replay", "status": "fail", "summary": "HSBA 100 vs 200"}]})
    report = health.build(today=TODAY, now=NOW)
    assert by_id(report)["quality.positions.replay"]["status"] == "fail"
    assert report["status"] == "fail"
