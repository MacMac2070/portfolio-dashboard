"""Local storage: SQLite now, the same shape Supabase gets later.

One file, `data/expenses.sqlite3`, schema applied from db/schema.sqlite.sql on
first open. The dedupe contract lives here: `insert_transactions` uses
INSERT OR IGNORE against the UNIQUE source_transaction_id, so a re-pulled
transaction is SKIPPED — `category` and `reviewed` may carry manual edits and
a pull must never overwrite them. At migration time this module is the only
thing that swaps for a Supabase client; every caller goes through it.
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "expenses.sqlite3"
SCHEMA_PATH = ROOT / "db" / "schema.sqlite.sql"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = path or DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text())
    return conn


# ---------------------------------------------------------------- connections

def save_connection(conn: sqlite3.Connection, *, bank: str, access_token: str,
                    refresh_token: str | None, consent_expires_at: str) -> str:
    """One row per bank: a re-consent replaces the old tokens."""
    conn.execute("delete from expense_tracker_bank_connections where bank = ?", (bank,))
    row_id = str(uuid.uuid4())
    conn.execute(
        """insert into expense_tracker_bank_connections
           (id, bank, access_token, refresh_token, consent_expires_at)
           values (?, ?, ?, ?, ?)""",
        (row_id, bank, access_token, refresh_token, consent_expires_at))
    conn.commit()
    return row_id


def connections(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "select * from expense_tracker_bank_connections order by bank"))


def mark_pulled(conn: sqlite3.Connection, connection_id: str, at: str | None = None) -> None:
    conn.execute(
        "update expense_tracker_bank_connections set last_pulled_at = ? where id = ?",
        (at or now_iso(), connection_id))
    conn.commit()


# --------------------------------------------------------------- transactions

def insert_transactions(conn: sqlite3.Connection, rows: list[dict]) -> tuple[list[str], int]:
    """INSERT OR IGNORE each row; returns (new row ids, skipped count).

    Only the newly inserted ids come back — the caller categorises exactly
    those, so a manual category on an old row is never touched.
    """
    inserted: list[str] = []
    skipped = 0
    for row in rows:
        row_id = str(uuid.uuid4())
        cur = conn.execute(
            """insert or ignore into expense_tracker_transactions
               (id, bank, account_id, source_transaction_id, date, amount,
                currency, raw_description)
               values (?, ?, ?, ?, ?, ?, ?, ?)""",
            (row_id, row["bank"], row["account_id"], row["source_transaction_id"],
             row["date"], row["amount"], row.get("currency", "GBP"),
             row["raw_description"]))
        if cur.rowcount:
            inserted.append(row_id)
        else:
            skipped += 1
    conn.commit()
    return inserted, skipped


def set_category(conn: sqlite3.Connection, row_id: str, category: str) -> None:
    conn.execute(
        "update expense_tracker_transactions set category = ? where id = ?",
        (category, row_id))
    conn.commit()


def uncategorised(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    """Rows still awaiting a category — how a run with Ollama down heals later."""
    return list(conn.execute(
        """select * from expense_tracker_transactions
           where category is null order by date limit ?""", (limit,)))
