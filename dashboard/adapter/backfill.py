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
    missing = []

    # One request, four sections. Flex is slow and rate limited, and every
    # section below lives in the same Activity Statement.
    try:
        root = flex.fetch_activity(config)
    except flex.FlexNotConfigured as exc:
        print(f"Activity      : skipped ({exc})")
        return 2
    except Exception as exc:
        log.error("could not fetch the activity statement: %s", exc)
        return 1

    try:
        nav_rows = flex.parse_nav_history(root)
        added, total = store.merge_nav(nav_rows)
        print(f"NAV history   : {len(nav_rows):>5} fetched, {added:>4} new, {total:>4} stored")
        if nav_rows:
            print(f"                {nav_rows[0]['date']} -> {nav_rows[-1]['date']}")
    except Exception as exc:
        log.error("NAV backfill failed: %s", exc)
        failures += 1

    try:
        change = flex.parse_change_in_nav(root)
        if change is None:
            missing.append("Change in NAV")
            print("Change in NAV : absent from the statement")
        else:
            added, total = store.merge_nav_change([change])
            print(f"Change in NAV : {change['from_date']} -> {change['to_date']}, "
                  f"{added} new, {total} stored")
            if change["unmapped"]:
                print(f"                unmapped fields: {', '.join(change['unmapped'])}")
    except Exception as exc:
        log.error("change-in-NAV backfill failed: %s", exc)
        failures += 1

    try:
        cash = flex.parse_cash_transactions(root)
        if not cash:
            missing.append("Cash Transactions")
            print("Cash          : absent from the statement")
        else:
            added, total = store.merge_cash(cash)
            kinds = sorted({r["type"] for r in cash})
            print(f"Cash          : {len(cash):>5} fetched, {added:>4} new, {total:>4} stored")
            print(f"                types: {', '.join(kinds)}")
    except Exception as exc:
        log.error("cash backfill failed: %s", exc)
        failures += 1

    try:
        trades = flex.fetch_trades(config)
        if not trades:
            missing.append("Trades")
            print("Transactions  : absent from the statement")
        else:
            added, total = store.merge_transactions(trades)
            print(f"Transactions  : {len(trades):>5} fetched, {added:>4} new, {total:>4} stored")
    except flex.FlexNotConfigured as exc:
        print(f"Transactions  : skipped ({exc})")
    except Exception as exc:
        log.error("trade backfill failed: %s", exc)
        failures += 1

    if missing:
        print()
        print("Sections the Flex query is not emitting yet:")
        for name in missing:
            print(f"  - {name}")
        print()
        print("Add them in Account Management -> Settings -> Account Reporting")
        print("-> Flex Queries, on the Activity Statement behind nav_query_id.")
        print("Adding Trades there too means one report rather than two, and")
        print("trades_query_id can be left unset.")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
