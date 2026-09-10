"""The demo dataset: deterministic, in step with its CSVs, and clean under
reconcile. These run the generator as a subprocess, the way a visitor would."""
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

import lots
import reconcile
import store
import track

DASHBOARD = Path(__file__).resolve().parents[1]
SCRIPT = DASHBOARD / "scripts" / "build_mock_data.py"
CSV_DIR = DASHBOARD / "data" / "mock"
STORE_FILES = (
    ("NAV_PATH", "nav_history.jsonl"), ("TX_PATH", "transactions.jsonl"),
    ("NAV_CHANGE_PATH", "nav_change.jsonl"), ("CASH_PATH", "cash_transactions.jsonl"),
    ("POSITIONS_PATH", "positions_eod.json"), ("LAST_RUN_PATH", "last_run.json"),
    ("QUALITY_PATH", "quality.json"), ("FX_PATH", "fx_rates.jsonl"),
    ("ACTIONS_PATH", "corporate_actions.jsonl"),
)
CORE_CHECKS = ("positions.replay", "nav.continuity", "flows.deposits",
               "nav.composition", "cash.unbucketed")


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          check=True, capture_output=True, text=True)


def point(monkeypatch, out: Path) -> None:
    monkeypatch.setattr(store, "DATA_DIR", out)
    for attr, name in STORE_FILES:
        monkeypatch.setattr(store, attr, out / name)


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("mock") / "a"
    run("--out", str(out))
    return out


def test_generator_is_deterministic(built, tmp_path):
    again = tmp_path / "b"
    run("--out", str(again))
    names = sorted(p.name for p in built.iterdir())
    assert names == sorted(p.name for p in again.iterdir())
    for name in names:
        assert (again / name).read_bytes() == (built / name).read_bytes(), name


def test_committed_csvs_match_the_synthesis(tmp_path):
    # The CSVs are the source of truth; --write-csv must reproduce them exactly,
    # otherwise the committed data and the authoring code have drifted apart.
    run("--write-csv", "--csv-dir", str(tmp_path / "csv"))
    names = sorted(p.name for p in CSV_DIR.glob("*.csv"))
    assert names == sorted(p.name for p in (tmp_path / "csv").glob("*.csv"))
    for name in names:
        assert (tmp_path / "csv" / name).read_bytes() == (CSV_DIR / name).read_bytes(), name


def test_every_reconciliation_check_passes(built, monkeypatch):
    point(monkeypatch, built)
    today = date.fromisoformat(store.nav_latest())
    report = reconcile.build(today=today)
    statuses = {c["id"]: c["status"] for c in report["checks"]}
    assert all(statuses[name] == "ok" for name in CORE_CHECKS), statuses
    assert set(statuses.values()) <= {"ok", "pending"}, statuses


def test_check_flag_exits_zero(tmp_path):
    result = run("--out", str(tmp_path / "c"), "--check")
    assert "reconciliation as of" in result.stdout
    assert (tmp_path / "c" / "quality.json").exists()


def test_store_readers_load_the_files(built, monkeypatch):
    point(monkeypatch, built)
    eod = store.read_positions_eod()
    assert eod and len(eod["positions"]) >= 10
    assert store.fx_history()
    assert store.corporate_actions()
    assert store.nav_dates()
    for position in eod["positions"]:
        assert lots.tradedate_cost(position["con_id"], position["quantity"]) is not None, position["symbol"]
    assert track.daily_series()
    import flexfeed
    snapshot = flexfeed.compose_payload(quotes={}, fx={}, sparks={})
    assert snapshot and snapshot["positions"]
    assert snapshot["kpis"]
