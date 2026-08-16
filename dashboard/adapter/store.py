"""Append-only JSONL stores for NAV history and transactions.

Both files are merge-by-key rather than blind appends, so re-running the Flex
backfill, or the daily job firing twice, never duplicates a row.
"""
from __future__ import annotations

import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
NAV_PATH = DATA_DIR / "nav_history.jsonl"
TX_PATH = DATA_DIR / "transactions.jsonl"


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


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def nav_count() -> int:
    return len(_read(NAV_PATH))
