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

    # The whole span first — and the NAV history rides the same statement.
    # This merge is what keeps the equity curve moving when the Gateway never
    # runs: for months the daily job never advanced nav_history at all (the
    # only writers were the Gateway snapshot row and a manual backfill), so
    # the curve silently froze at whatever the last hand-run left behind.
    try:
        root = flex.fetch_activity(config)
        nav_rows = flex.parse_nav_history(root)
        if nav_rows:
            added, total = store.merge_nav(nav_rows)
            log.info("nav history: %d new, %d stored (through %s)",
                     added, total, nav_rows[-1]["date"])
        periods = flex.parse_change_in_nav_periods(root)
        cash = flex.parse_cash_transactions(root)
        if periods:
            added, total = store.merge_nav_change(periods)
            log.info("attribution: whole span — %d period(s), %d new, %d stored",
                     len(periods), added, total)
        if cash:
            added, total = store.merge_cash(cash)
            log.info("attribution: whole span — %d cash row(s), %d new, %d stored",
                     len(cash), added, total)
    except Exception:
        # A Flex outage must not fail the job: the stored figures still serve,
        # and the next run picks this up.
        log.exception("attribution: whole span failed; the stored figures still serve")

    # Month windows come from the REFRESHED series, so a freshly-merged week
    # is included the same night it lands. Ends at the last day IBKR has
    # reported rather than today: Activity Statements are generated at close
    # of business, and a window running into an unfinished day is refused
    # with "1003 Statement is not available".
    latest = store.nav_latest()
    end = date.fromisoformat(latest) if latest else date.today() - timedelta(days=1)
    windows = flex.snap_to_reported(
        flex.month_windows(end.replace(day=1), end), store.nav_dates())

    for start, end in windows:
        label = start.strftime("%b %Y")
        try:
            root = flex.fetch_activity(config, from_date=start, to_date=end)
            periods = flex.parse_change_in_nav_periods(root)
            cash = flex.parse_cash_transactions(root)
        except Exception:
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


def _refresh_positions() -> None:
    """Keep the EOD open-positions snapshot current. Never fatal.

    Flex is independent of IB Gateway, so this runs whether or not the Gateway
    was reachable — it is what lets a Gateway-less day still produce a full
    portfolio.json via flexfeed.
    """
    if not store.positions_eod_stale(hours=20):
        log.info("positions: refreshed within the day, skipping")
        return
    try:
        config = flex.load_config()
    except flex.FlexNotConfigured as exc:
        log.info("positions: skipped (%s)", exc)
        return
    try:
        rows = flex.fetch_positions(config)
    except Exception:
        log.exception("positions: fetch failed; the stored snapshot still serves")
        return
    if rows:
        written = store.write_positions_eod(rows)
        log.info("positions: %d rows as of %s", len(rows), written["asof"])
    else:
        log.warning("positions: statement carried no Open Positions section — "
                    "enable it on the Flex query")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    build.DATA_DIR.mkdir(parents=True, exist_ok=True)

    payload = None
    gateway_error = None
    try:
        payload = build.build()
    except ibkr.GatewayUnavailable as exc:
        # Not fatal any more: Flex still serves. Fetch the EOD positions first
        # so the fallback compose below has something to stand on.
        gateway_error = str(exc)
        log.error("gateway unavailable: %s", exc)
    except Exception as exc:
        # A zombie Gateway accepts the socket and then drops it mid-request
        # (ConnectionError, not GatewayUnavailable). Whatever the flavour of
        # broken, the answer is the same: fall to Flex and carry on — a dead
        # Gateway must never abort the store refreshes that don't need it.
        gateway_error = str(exc)
        log.exception("gateway build failed; falling to Flex: %s", exc)

    if payload is None:
        _refresh_positions()
        try:
            import flexfeed
            payload = flexfeed.compose_payload()
        except Exception:
            log.exception("flex fallback failed; the stale marker stands instead")
        if payload is not None:
            payload["meta"]["gateway"] = "unavailable"
            payload["meta"]["error"] = gateway_error
            log.info("portfolio composed from Flex EOD positions + delayed quotes")
        else:
            build.write_stale_marker(gateway_error or "gateway unavailable")

    if payload is not None:
        build.OUT_PATH.write_text(json.dumps(payload, indent=2))
        # The overnight-diff baseline rides every successful build.
        try:
            import desk
            desk.write_close_snapshot(payload)
        except Exception:
            log.exception("close snapshot failed; overnight diff will be stale")

        # Record today's NAV only off the gateway's own account figure. The
        # Flex-composed NAV is EOD cash + repriced positions — close, but the
        # real figure for that day arrives via the Flex NAV series anyway.
        nav = payload.get("kpis", {}).get("net_liquidation")
        if nav is not None and payload["meta"].get("source") != "flex-eod":
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

    _refresh_positions()
    _refresh_attribution()

    try:
        import desk
        desk.refresh_if_stale(hours=20)
    except Exception:
        log.exception("desk refresh failed; the previous file still serves")

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

    # The archive census. Flex's 365-day window means these files are the only
    # copy of the early history; a shrinking count here is the alarm.
    log.info("archive: nav=%d rows, cash=%d, nav_change=%d, tx=%d",
             *(sum(1 for _ in path.read_text().splitlines() if _.strip())
               if path.exists() else 0
               for path in (store.NAV_PATH, store.CASH_PATH,
                            store.NAV_CHANGE_PATH, store.TX_PATH)))

    if payload is None:
        log.error("refresh finished with no portfolio payload — stores were "
                  "still brought up to date")
        return 1
    log.info("refresh complete — %d positions, invested %.2f%s",
             len(payload.get("positions", [])), payload.get("kpis", {}).get("invested", 0),
             " (flex fallback)" if payload["meta"].get("source") == "flex-eod" else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
