"""The scheduled pull — build plan §6, run every 12h by launchd locally
(scripts/install-pull-job.sh, not installed until the provider is live).

Per valid connection: accounts -> transactions since last_pulled_at ->
INSERT OR IGNORE (dedupe on source_transaction_id, manual edits sacred) ->
categorise the NEW rows only. An expired consent is logged loudly and
skipped, never thrown; a sleeping Ollama leaves category null and the next
run heals it via store.uncategorised. At migration time this file becomes
the Vercel Cron function.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import aggregator_client, categorise, store  # noqa: E402

log = logging.getLogger("expense-pull")


def _expired(consent_expires_at: str) -> bool:
    try:
        expires = datetime.fromisoformat(consent_expires_at.replace("Z", "+00:00"))
    except ValueError:
        return True                       # unparseable consent is no consent
    return expires <= datetime.now(timezone.utc)


def pull(dry_run: bool = False) -> int:
    client = aggregator_client.client()
    with store.connect() as conn:
        rows = store.connections(conn)
        if not rows:
            log.info("no bank connections yet — run api/server.py and complete "
                     "a consent first")
            return 0

        for connection in rows:
            bank = connection["bank"]
            if _expired(connection["consent_expires_at"]):
                # The 90-day cliff. Loud, specific, and non-fatal by design.
                log.warning("consent expired for %s — needs manual re-auth "
                            "(api/server.py, /oauth/start?bank=%s)", bank, bank)
                continue

            try:
                accounts = client.get_accounts(connection["access_token"])
                fetched: list[dict] = []
                for account in accounts:
                    for tx in client.get_transactions(
                            connection["access_token"], account.account_id,
                            connection["last_pulled_at"]):
                        fetched.append({
                            "bank": bank,
                            "account_id": tx.account_id,
                            "source_transaction_id": tx.source_transaction_id,
                            "date": tx.date,
                            "amount": tx.amount,
                            "currency": tx.currency,
                            "raw_description": tx.raw_description,
                        })
            except NotImplementedError as exc:
                log.warning("%s: aggregator not implemented yet — %s", bank, exc)
                continue
            except Exception:
                log.exception("%s: pull failed; connection left for next run", bank)
                continue

            if dry_run:
                log.info("%s: DRY RUN — %d transaction(s) would be considered",
                         bank, len(fetched))
                continue

            inserted, skipped = store.insert_transactions(conn, fetched)
            store.mark_pulled(conn, connection["id"])
            log.info("%s: %d new, %d already known", bank, len(inserted), skipped)

            # New rows only — a category or reviewed flag set by hand on an
            # older row is never revisited by the pull.
            categorised = 0
            for row_id in inserted:
                row = conn.execute(
                    "select raw_description, amount from expense_tracker_transactions "
                    "where id = ?", (row_id,)).fetchone()
                category = categorise.categorise(row["raw_description"], row["amount"])
                if category:
                    store.set_category(conn, row_id, category)
                    categorised += 1
            if inserted:
                log.info("%s: categorised %d of %d (nulls heal next run)",
                         bank, categorised, len(inserted))

        # Second chance for rows a sleeping Ollama left null on earlier runs.
        healed = 0
        for row in store.uncategorised(conn):
            category = categorise.categorise(row["raw_description"], row["amount"])
            if category:
                store.set_category(conn, row["id"], category)
                healed += 1
        if healed:
            log.info("healed %d previously uncategorised row(s)", healed)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    raise SystemExit(pull(dry_run="--dry-run" in sys.argv))
