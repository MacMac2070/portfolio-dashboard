"""One-time history import from IBKR Flex.

    /opt/anaconda3/bin/python3 adapter/backfill.py

Safe to re-run: both stores merge on a key, so nothing duplicates. Run it again
after a missed day, or whenever you want the authoritative broker figures to
overwrite locally-taken snapshots.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import flex
import store

log = logging.getLogger("backfill")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        config = flex.load_config()
    except flex.FlexNotConfigured as exc:
        print(f"Flex is not configured ({exc}).\n")
        print("The dashboard works without it — the chart shows its 'collecting")
        print("history' state and fills in from the daily job instead.\n")
        print("To backfill from IBKR rather than waiting:")
        print("  1. Account Management -> Settings -> Account Reporting")
        print("     -> Flex Web Service -> generate a token")
        print("  2. Create an Activity Statement query including the")
        print("     'Net Asset Value (NAV) in Base' section, and a Trade")
        print("     Confirmation query; note both Query IDs")
        print("  3. cp config.local.json.example config.local.json, fill it in")
        return 2

    failures = 0

    try:
        nav_rows = flex.fetch_nav_history(config)
        added, total = store.merge_nav(nav_rows)
        print(f"NAV history   : {len(nav_rows):>5} fetched, {added:>4} new, {total:>4} stored")
        if nav_rows:
            print(f"                {nav_rows[0]['date']} -> {nav_rows[-1]['date']}")
    except flex.FlexNotConfigured as exc:
        print(f"NAV history   : skipped ({exc})")
    except Exception as exc:
        log.error("NAV backfill failed: %s", exc)
        failures += 1

    try:
        trades = flex.fetch_trades(config)
        added, total = store.merge_transactions(trades)
        print(f"Transactions  : {len(trades):>5} fetched, {added:>4} new, {total:>4} stored")
    except flex.FlexNotConfigured as exc:
        print(f"Transactions  : skipped ({exc})")
    except Exception as exc:
        log.error("trade backfill failed: %s", exc)
        failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
