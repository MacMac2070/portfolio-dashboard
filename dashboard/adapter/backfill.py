"""One-time history import from IBKR Flex.

    /opt/anaconda3/bin/python3 adapter/backfill.py
    /opt/anaconda3/bin/python3 adapter/backfill.py --months 1   # last month only

Safe to re-run: both stores merge on a key, so nothing duplicates. Run it again
after a missed day, or whenever you want the authoritative broker figures to
overwrite locally-taken snapshots.

Two passes, for two different questions:

  1. One whole-span request. NAV history, trades, and the Change in NAV summary
     the Sankey draws all come off it — that chart answers "where did the money
     go over the whole period", so it wants IBKR's own figures for the span
     rather than a sum of parts.

  2. One request per calendar month. Change in NAV reports a single element for
     whatever range it is asked for and has no monthly breakdown of its own, so
     month-level granularity means asking a month at a time. This is what fills
     the Monthly P&L bars.

The second pass is paced (see flex.SEND_INTERVAL) and is therefore slow: a
year of history is a dozen requests at ~6.5s apart plus IBKR's generation time.
`--months N` limits it to the last N months for a quick check.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import flex
import store
from flex import MONTH_SPAN_DAYS

log = logging.getLogger("backfill")


def main(months: int | None = None) -> int:
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
        periods = flex.parse_change_in_nav_periods(root)
        if not periods:
            missing.append("Change in NAV")
            print("Change in NAV : absent from the statement")
        else:
            # Store every period, not just the widest. The Sankey reads the
            # widest and the monthly P&L bars read the sub-periods; both live
            # in one store keyed on from_date|to_date.
            added, total = store.merge_nav_change(periods)
            widest = max(periods, key=lambda r: r["span_days"])
            print(f"Change in NAV : {widest['from_date']} -> {widest['to_date']}, "
                  f"{len(periods)} period(s), {added} new, {total} stored")
            subs = [r for r in periods if r["span_days"] <= MONTH_SPAN_DAYS]
            if subs:
                print(f"                {len(subs)} sub-period(s) -> monthly P&L available")
            else:
                # Expected, and not a misconfiguration: the section reports one
                # element per request and has no monthly setting to turn on.
                # The months come from the second pass below.
                print("                whole span only — the monthly pass below "
                      "is what fills the P&L bars")
            unmapped = sorted({f for r in periods for f in r["unmapped"]})
            if unmapped:
                print(f"                unmapped fields: {', '.join(unmapped)}")
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

    # ---- pass 2: one request per month --------------------------------
    #
    # Only worth running once the whole-span pass proved the section is on:
    # without it every window would come back empty, twelve times, slowly.
    if "Change in NAV" not in missing:
        failures += _monthly_pass(config, months)

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


def _monthly_pass(config: dict, months: int | None) -> int:
    """Re-request the history a month at a time, for the monthly P&L bars.

    Returns a failure count. A window that fails is reported and skipped rather
    than aborting the run: eleven good months and one gap is a usable chart,
    and the store merges on the period, so a re-run repairs just the gap.
    """
    inception = store.nav_inception()
    if not inception:
        print()
        print("Monthly       : skipped (no NAV history to date the account from)")
        return 0

    # The far end is the last day IBKR has reported, not today: a window that
    # runs to an unfinished day is refused with "1003 Statement is not
    # available", which failed the current month on every run.
    latest = store.nav_latest()
    start = date.fromisoformat(inception)
    end = date.fromisoformat(latest) if latest else date.today()
    # Bounded by real reporting days, or Flex refuses some months outright.
    windows = flex.snap_to_reported(flex.month_windows(start, end), store.nav_dates())
    if months is not None:
        windows = windows[-months:] if months > 0 else []
    if not windows:
        return 0

    # Pacing dominates the runtime and people kill jobs that look hung.
    estimate = len(windows) * (flex.SEND_INTERVAL + flex.POLL_SECONDS)
    print()
    print(f"Monthly       : {len(windows)} window(s), {windows[0][0]} -> {windows[-1][1]}")
    print(f"                ~{estimate / 60:.0f} min at {flex.SEND_INTERVAL:.1f}s between requests")

    failures = 0
    began = time.monotonic()
    for index, (fd, td) in enumerate(windows, start=1):
        label = fd.strftime("%b %Y")
        try:
            root = flex.fetch_activity(config, from_date=fd, to_date=td)
        except Exception as exc:
            log.error("  %-8s %s -> %s failed: %s", label, fd, td, exc)
            failures += 1
            continue

        periods = flex.parse_change_in_nav_periods(root)
        # A window that comes back wider than it was asked for means fd/td did
        # not take effect. Storing it would put a whole-span row under a
        # month's key and month_pnl would then count all of history as one
        # month, so refuse it and say why.
        wide = [r for r in periods if (r.get("span_days") or 0) > MONTH_SPAN_DAYS]
        if wide:
            log.error("  %-8s ignored: got a %d-day span for a %d-day window — "
                      "fd/td are not being applied",
                      label, wide[0]["span_days"], (td - fd).days)
            failures += 1
            continue

        nav_added, _ = store.merge_nav_change(periods)
        cash = flex.parse_cash_transactions(root)
        cash_added, _ = store.merge_cash(cash) if cash else (0, 0)
        print(f"  [{index:>2}/{len(windows)}] {label:<9} "
              f"{len(periods)} period(s) ({nav_added} new), "
              f"{len(cash):>3} cash row(s) ({cash_added} new)")

    print(f"                done in {(time.monotonic() - began) / 60:.1f} min")
    return failures


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--months", type=int, default=None, metavar="N",
        help="only re-request the last N calendar months (default: all of them)")
    raise SystemExit(main(parser.parse_args().months))
