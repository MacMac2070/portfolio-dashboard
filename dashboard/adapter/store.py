"""Append-only JSONL stores for NAV history and transactions.

Both files are merge-by-key rather than blind appends, so re-running the Flex
backfill, or the daily job firing twice, never duplicates a row.
"""
from __future__ import annotations

import json
import time
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
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def merge_nav(new_rows: list[dict]) -> tuple[int, int]:
    """Upsert NAV rows keyed on date. Returns (added, total).

    Flex wins over a local snapshot for the same date: it is the broker's
    end-of-day figure, whereas a snapshot is whenever the job happened to run.
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
        elif row.get("source") == "flex" and existing.get("source") != "flex":
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


def nav_change_stale(hours: float = 20) -> bool:
    """Whether the Change in NAV store is old enough to be worth re-pulling.

    Activity Statement data only changes once a day, at IBKR's close of
    business. Pulling twice in one day spends paced Flex requests to be handed
    back what is already stored, so the daily job checks this first and a
    manual midday re-run costs nothing.
    """
    if not NAV_CHANGE_PATH.exists():
        return True
    return (time.time() - NAV_CHANGE_PATH.stat().st_mtime) >= hours * 3600


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
