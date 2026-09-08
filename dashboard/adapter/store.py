"""Append-only JSONL stores for NAV history and transactions.

Both files are merge-by-key rather than blind appends, so re-running the Flex
backfill, or the daily job firing twice, never duplicates a row.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
NAV_PATH = DATA_DIR / "nav_history.jsonl"
TX_PATH = DATA_DIR / "transactions.jsonl"
# The Change in NAV summary, one row per report period. Keyed on the period so
# re-running the backfill over an overlapping window replaces rather than
# accumulates.
NAV_CHANGE_PATH = DATA_DIR / "nav_change.jsonl"
# Dated cash movements: dividends, withholding tax, interest, fees.
CASH_PATH = DATA_DIR / "cash_transactions.jsonl"
# The EOD open-positions snapshot from Flex. A snapshot, not a ledger: each
# write replaces the file, so the no-shrink guard does not apply here.
POSITIONS_PATH = DATA_DIR / "positions_eod.json"
# The nightly job's heartbeat and the reconciliation verdicts. Documents, not
# ledgers: rewritten whole each run. /api/health reads both.
LAST_RUN_PATH = DATA_DIR / "last_run.json"
QUALITY_PATH = DATA_DIR / "quality.json"
# Daily FX into GBP from the Flex ConversionRates section, keyed date|currency.
FX_PATH = DATA_DIR / "fx_rates.jsonl"
# Splits, ticker changes, spin-offs, transfers: signed share quantities the
# position replay and the lot engine apply beside the fills.
ACTIONS_PATH = DATA_DIR / "corporate_actions.jsonl"


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _write(path: Path, rows: list[dict], *, allow_shrink: bool = False) -> None:
    """Write the store, refusing to shrink it.

    Flex reaches back at most 365 days, so from about Oct 2026 the oldest rows
    in these files exist nowhere else — the store is the archive, not a cache.
    Every merge above is union-semantics and can only grow the file; a write
    carrying fewer rows than are on disk therefore means a bug upstream, and
    losing rows to it would be silent and permanent. Refuse loudly instead.
    (Committing data/ to git is the backup — see README.)"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not allow_shrink and path.exists():
        existing = sum(1 for line in path.read_text().splitlines() if line.strip())
        if len(rows) < existing:
            raise RuntimeError(
                f"refusing to shrink {path.name}: {existing} rows on disk, "
                f"asked to write {len(rows)} — pass allow_shrink=True only if "
                "this loss is intended")
    _atomic_write(path, "".join(json.dumps(row) + "\n" for row in rows))


def _atomic_write(path: Path, text: str) -> None:
    """Write via a same-directory temp file and an atomic rename.

    These files are the archive, not a cache — a write interrupted by a full
    disk or a kill signal must leave the old file intact, never a truncated
    one. os.replace is atomic on POSIX when source and target share a
    filesystem, which same-directory guarantees.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path: Path, payload, *, indent: int = 2) -> None:
    """Write a JSON document atomically — the document twin of `_write`.

    portfolio.json is read straight off disk by the browser and desk.json by
    three services, and a bare write_text interrupted mid-way leaves a
    truncated file that takes the Overview down until the next run. The
    payload is serialised first, so one that cannot be encoded fails before
    the old file is touched.
    """
    text = json.dumps(payload, indent=indent)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, text)


def read_json(path: Path):
    """A JSON document, or None when absent or unreadable. Never raises."""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


# IBKR has today's Activity Statement ready by about this hour, local time;
# before it, "current" means yesterday's weekday. Observed: the 23:30 job
# fetches positions dated the same day.
STATEMENT_CUTOFF_HOUR = 22


def expected_report_date(today: date | None = None, *, now: datetime | None = None) -> str:
    """The newest date a store can be current through.

    The last weekday on or before today — or before yesterday when it is
    still earlier than STATEMENT_CUTOFF_HOUR, because today's statement does
    not exist yet and a store cannot be behind it. Pass `today` to pin the
    day and skip the cutoff (the tests do); leave it out for the live rule.

    IBKR generates Activity Statements after close of business, so a store
    whose newest row carries this date is current and one a weekday behind it
    is a day late. Judged on content, not file mtime — a run that merged
    nothing still touches the file, which is how nav_history sat five days
    stale in Sep 2026 while every mtime gate said "fresh".
    """
    if today is None:
        now = now or datetime.now()
        day = now.date()
        if now.hour < STATEMENT_CUTOFF_HOUR:
            day -= timedelta(days=1)
    else:
        day = today
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day.isoformat()


def stores_current() -> bool:
    """Whether both Flex stores already carry the newest statement there is."""
    return not positions_eod_stale() and not nav_change_stale()


def merge_nav(new_rows: list[dict]) -> tuple[int, int]:
    """Upsert NAV rows keyed on date. Returns (added, total).

    Flex wins over a local snapshot for the same date: it is the broker's
    end-of-day figure, whereas a snapshot is whenever the job happened to run.
    A Flex row also replaces an earlier Flex row — the newer statement is the
    restated one, and it is how rows gain fields (the cash/stock split) the
    query did not carry when they were first stored.
    """
    by_date = {}
    for row in _read(NAV_PATH):
        if row.get("date"):
            by_date[row["date"]] = row

    added = 0
    for row in new_rows:
        date = row.get("date")
        if not date:
            continue
        existing = by_date.get(date)
        if existing is None:
            added += 1
            by_date[date] = row
        elif row.get("source") == "flex":
            by_date[date] = row

    merged = [by_date[d] for d in sorted(by_date)]
    _write(NAV_PATH, merged)
    return added, len(merged)


def merge_transactions(new_rows: list[dict]) -> tuple[int, int]:
    """Upsert executions keyed on exec_id, falling back to a composite key."""
    def key(row: dict) -> str:
        if row.get("exec_id"):
            return str(row["exec_id"])
        return f"{row.get('time')}|{row.get('con_id')}|{row.get('quantity')}|{row.get('price')}"

    by_key = {key(row): row for row in _read(TX_PATH)}

    added = 0
    for row in new_rows:
        k = key(row)
        if k not in by_key:
            added += 1
            by_key[k] = row
        elif row.get("source") == "flex":
            by_key[k] = row

    merged = sorted(by_key.values(), key=lambda row: (row.get("time") or "", key(row)))
    _write(TX_PATH, merged)
    return added, len(merged)


def merge_nav_change(new_rows: list[dict]) -> tuple[int, int]:
    """Upsert Change in NAV summaries keyed on their reporting period.

    A later run over the same period wins: Flex restates a period as trades
    settle, and the newer figure is the corrected one.
    """
    by_period = {}
    for row in _read(NAV_CHANGE_PATH):
        key = f"{row.get('from_date')}|{row.get('to_date')}"
        if row.get("from_date"):
            by_period[key] = row

    added = 0
    for row in new_rows:
        if not row or not row.get("from_date"):
            continue
        key = f"{row.get('from_date')}|{row.get('to_date')}"
        if key not in by_period:
            added += 1
        by_period[key] = row

    merged = [by_period[k] for k in sorted(by_period)]
    _write(NAV_CHANGE_PATH, merged)
    return added, len(merged)


def merge_cash(new_rows: list[dict]) -> tuple[int, int]:
    """Upsert cash transactions keyed on IBKR's transaction id.

    Falls back to a composite key for rows that carry none — some fee and tax
    lines do not — so a re-run still recognises them instead of duplicating.
    """
    def key(row: dict) -> str:
        if row.get("tx_id"):
            return str(row["tx_id"])
        return (f"{row.get('date')}|{row.get('type')}|{row.get('symbol')}"
                f"|{row.get('amount')}|{row.get('currency')}")

    by_key = {key(row): row for row in _read(CASH_PATH)}

    added = 0
    for row in new_rows:
        k = key(row)
        if k not in by_key:
            added += 1
        by_key[k] = row

    merged = sorted(by_key.values(), key=lambda r: (r.get("date") or "", key(r)))
    _write(CASH_PATH, merged)
    return added, len(merged)


def merge_fx(new_rows: list[dict]) -> tuple[int, int]:
    """Upsert daily FX rates keyed on date and currency. Later wins."""
    def key(row: dict) -> str:
        return f"{row.get('date')}|{row.get('currency')}"

    by_key = {key(row): row for row in _read(FX_PATH) if row.get("date") and row.get("currency")}
    added = 0
    for row in new_rows:
        if not row.get("date") or not row.get("currency"):
            continue
        k = key(row)
        if k not in by_key:
            added += 1
        by_key[k] = row
    merged = [by_key[k] for k in sorted(by_key)]
    _write(FX_PATH, merged)
    return added, len(merged)


def merge_corporate_actions(new_rows: list[dict]) -> tuple[int, int]:
    """Upsert corporate actions and transfers keyed on their id and contract."""
    def key(row: dict) -> str:
        return f"{row.get('action_id') or row.get('date')}|{row.get('con_id')}|{row.get('kind')}"

    by_key = {key(row): row for row in _read(ACTIONS_PATH) if row.get("con_id")}
    added = 0
    for row in new_rows:
        if not row.get("con_id") or not row.get("date"):
            continue
        k = key(row)
        if k not in by_key:
            added += 1
        by_key[k] = row
    merged = sorted(by_key.values(), key=lambda r: (r.get("date") or "", key(r)))
    _write(ACTIONS_PATH, merged)
    return added, len(merged)


def corporate_actions() -> list[dict]:
    return _read(ACTIONS_PATH)


def fx_history() -> dict[tuple[str, str], float]:
    """{(date, currency): rate into GBP} from the FX store. Empty when absent."""
    return {(r["date"], r["currency"]): float(r["rate"])
            for r in _read(FX_PATH)
            if r.get("date") and r.get("currency") and r.get("rate")}


def nav_count() -> int:
    return len(_read(NAV_PATH))


def nav_latest() -> str | None:
    """The most recent date the NAV series carries, ISO, or None.

    This is the edge of what IBKR has actually reported. Activity Statements
    are generated at close of business, so *today* is not available until the
    day is over — asking Flex for a window ending today is refused outright
    with "1003 Statement is not available". Callers building date windows end
    them here rather than at date.today().
    """
    dates = [r["date"] for r in _read(NAV_PATH) if r.get("date")]
    return max(dates) if dates else None


def nav_dates() -> list[str]:
    """Every date the NAV series carries, ISO, ascending.

    These are exactly the days IBKR reported on, which is what a Flex date
    window has to be bounded by — see `nav_latest`.
    """
    return sorted({r["date"] for r in _read(NAV_PATH) if r.get("date") and r.get("nav_gbp")})


RETRY_BEHIND_HOURS = 2


def _stale(path: Path, asof: str | None, hours: float) -> bool:
    """The staleness rule both Flex stores share: judged on content first.

    A store whose newest date is the last weekday is current, however old the
    file. One that is behind is re-pulled, but not more often than every
    RETRY_BEHIND_HOURS — a midday re-run within that window still costs
    nothing, and a statement that is simply not out yet is not asked for on
    every run. The plain mtime rule this replaces called a file fresh because
    a run had touched it, while its newest row sat five days old.
    """
    if not path.exists():
        return True
    if asof and asof >= expected_report_date():
        return False
    return (time.time() - path.stat().st_mtime) >= min(hours, RETRY_BEHIND_HOURS) * 3600


def nav_change_stale(hours: float = 20) -> bool:
    """Whether the Change in NAV store is worth re-pulling — see `_stale`."""
    latest = max((r.get("to_date") or "" for r in _read(NAV_CHANGE_PATH)), default="")
    return _stale(NAV_CHANGE_PATH, latest or None, hours)


def write_positions_eod(rows: list[dict]) -> dict:
    """Replace the EOD positions snapshot. Returns the written payload."""
    from datetime import datetime, timezone
    asof = max((r.get("report_date") or "" for r in rows), default="") or None
    payload = {
        "asof": asof,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "positions": rows,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(POSITIONS_PATH, json.dumps(payload, indent=2))
    return payload


def read_positions_eod() -> dict | None:
    """The EOD positions snapshot, or None when it has never been fetched."""
    if not POSITIONS_PATH.exists():
        return None
    try:
        payload = json.loads(POSITIONS_PATH.read_text())
    except json.JSONDecodeError:
        return None
    return payload if payload.get("positions") else None


def positions_eod_stale(hours: float = 20) -> bool:
    """Whether the EOD positions snapshot is worth re-pulling — see `_stale`."""
    eod = read_positions_eod()
    return _stale(POSITIONS_PATH, (eod or {}).get("asof"), hours)


def nav_row_on_or_before(day: str | None) -> dict | None:
    """The NAV row for `day`, or the nearest earlier reported day."""
    best = None
    for row in _read(NAV_PATH):
        d = row.get("date")
        if not d or not row.get("nav_gbp"):
            continue
        if day is None or d <= day:
            if best is None or d > best["date"]:
                best = row
    return best


def nav_on_or_before(day: str | None) -> float | None:
    """The NAV figure for `day`, or the nearest earlier reported day."""
    row = nav_row_on_or_before(day)
    return row["nav_gbp"] if row else None


def nav_inception() -> str | None:
    """The first date the account actually held anything, ISO, or None.

    Flex pads the NAV series with zero-value rows back to the start of its
    reporting year, so the earliest row in the file is not the earliest
    *position* — this account's file opens on 2025-07-30 at zero and does not
    reach a real figure until 2025-10-13. Anything asking "since inception"
    wants that second date; taking rows[0] gets a flat zero line instead.
    """
    for row in _read(NAV_PATH):
        if row.get("date") and row.get("nav_gbp"):
            return row["date"]
    return None
