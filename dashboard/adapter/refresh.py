"""The scheduled job: refresh the snapshot and record the day.

    /opt/anaconda3/bin/python3 adapter/refresh.py

Run by the LaunchAgent once a day, and safe to run by hand any time you want
the dashboard to catch up. Three things happen:

  1. data/portfolio.json is rewritten from IB Gateway + openbb
  2. today's net liquidation is appended to data/nav_history.jsonl
  3. any executions the Gateway still holds are merged into transactions.jsonl
  4. the current month's Flex attribution is refreshed, at most once a day

If the Gateway is down the job does not fail loudly: it marks the existing
snapshot stale so the UI can say so, and exits. The day's NAV is recoverable
later by re-running backfill.py, because Flex holds the authoritative series.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build
import directory
import flex
import ibkr
import store

log = logging.getLogger("refresh")


def _refresh_attribution() -> None:
    """Bring the Change in NAV and cash stores up to date. Never fatal.

    Two requests, not a full backfill: the whole span, which is what the NAV
    flow chart draws, and the current month, which is the only month whose
    figures can still move. Earlier months are settled and already stored, and
    re-requesting them would spend paced Flex requests to be handed back what
    is on disk — `adapter/backfill.py` is the tool for that when history needs
    rebuilding.

    Flex is independent of IB Gateway, so this still runs on a day the Gateway
    was down; it is the same statement the backfill reads.
    """
    if not store.nav_change_stale(hours=20):
        log.info("attribution: refreshed within the day, skipping")
        return

    try:
        config = flex.load_config()
    except flex.FlexNotConfigured as exc:
        log.info("attribution: skipped (%s)", exc)
        return

    # Ends at the last day IBKR has reported rather than today: Activity
    # Statements are generated at close of business, and a window running into
    # an unfinished day is refused with "1003 Statement is not available".
    latest = store.nav_latest()
    end = date.fromisoformat(latest) if latest else date.today() - timedelta(days=1)
    windows = [(None, None)]                      # the whole span, for the Sankey
    windows += flex.snap_to_reported(
        flex.month_windows(end.replace(day=1), end), store.nav_dates())

    for start, end in windows:
        label = "whole span" if start is None else start.strftime("%b %Y")
        try:
            root = flex.fetch_activity(config, from_date=start, to_date=end)
            periods = flex.parse_change_in_nav_periods(root)
            cash = flex.parse_cash_transactions(root)
        except Exception:
            # A Flex outage must not fail the job: the snapshot and the NAV
            # point are already written, and the next run picks this up.
            log.exception("attribution: %s failed; the stored figures still serve", label)
            continue

        if periods:
            added, total = store.merge_nav_change(periods)
            log.info("attribution: %s — %d period(s), %d new, %d stored",
                     label, len(periods), added, total)
        if cash:
            added, total = store.merge_cash(cash)
            log.info("attribution: %s — %d cash row(s), %d new, %d stored",
                     label, len(cash), added, total)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    build.DATA_DIR.mkdir(parents=True, exist_ok=True)

    try:
        payload = build.build()
    except ibkr.GatewayUnavailable as exc:
        log.error("gateway unavailable: %s", exc)
        build.write_stale_marker(str(exc))
        return 1
    except Exception as exc:
        log.exception("refresh failed: %s", exc)
        return 1

    build.OUT_PATH.write_text(json.dumps(payload, indent=2))

    nav = payload.get("kpis", {}).get("net_liquidation")
    if nav is not None:
        added, total = store.merge_nav([{
            "date": date.today().isoformat(),
            "nav_gbp": nav,
            "source": "snapshot",
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }])
        log.info("NAV %.2f recorded (%d new, %d points stored)", nav, added, total)

    fills = payload.get("fills") or []
    if fills:
        added, total = store.merge_transactions(fills)
        log.info("executions: %d new, %d stored", added, total)

    _refresh_attribution()

    # Last, and deliberately so. The directory does not depend on any of the
    # above and nothing above depends on it — but its three provider calls carry
    # no timeout, and a stall in one of them would hold the job open past the
    # LaunchAgent's once-a-day window. Running it after the NAV is recorded
    # means the worst a hung provider costs is a stale symbol list. The
    # try/except only catches exceptions; ordering is what covers a hang.
    # `stale()` keeps a manual midday run from re-fetching three files.
    try:
        if directory.stale(hours=20):
            stats = directory.build(log_to=log.info)
            log.info("directory: %d symbols, version %s (added %d, removed %d)",
                     stats["rows"], stats["version"], stats["added"], stats["removed"])
        else:
            log.info("directory: fresh, %d symbols", directory.count())
    except Exception:
        log.exception("directory rebuild failed; the previous directory still serves")

    log.info("refresh complete — %d positions, invested %.2f",
             len(payload.get("positions", [])), payload.get("kpis", {}).get("invested", 0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
