"""The scheduled job: refresh the snapshot and record the day.

    /opt/anaconda3/bin/python3 adapter/refresh.py [--force]

Run by the LaunchAgent once a day, and safe to run by hand any time you want
the dashboard to catch up. Five things happen:

  1. data/portfolio.json is rewritten from IB Gateway + openbb, or composed
     from Flex EOD positions when the Gateway is down
  2. today's net liquidation is appended to data/nav_history.jsonl
  3. executions — the Gateway's own fills and the Flex Trades section — are
     merged into transactions.jsonl
  4. the current month's Flex attribution is refreshed, at most once a day
  5. data/last_run.json records how it went, stage by stage, which is what
     /api/health and the header chip read the next morning

If the Gateway is down the job does not fail loudly: it composes from Flex,
marks the snapshot accordingly, and carries on. The day's NAV is recoverable
later by re-running backfill.py, because Flex holds the authoritative series.

Two guards exist because of the night of 31 Aug 2026, when a provider call
inside the directory rebuild never returned. The process sat asleep for 42
hours, launchd would not start a second instance of a running label, and two
nights went unrecorded with nothing saying so. A watchdog thread now ends the
run at JOB_BUDGET whatever stage it is in — every write is atomic, so a hard
exit costs nothing but the stages not reached — and the directory rebuild
runs on a thread with a budget of its own, abandoned rather than waited for.

A run that succeeded within GATE_HOURS is not repeated *if the stores are
already current*: the LaunchAgent fires on login as well as at 23:30, and
without the second condition an afternoon hand run silenced that night's
scheduled one (2 Sep 2026). `--force` is the way to say "I mean it".
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build
import directory
import flex
import ibkr
import logsetup
import notify
import reconcile
import store

log = logging.getLogger("refresh")

JOB_BUDGET = 40 * 60          # seconds; a normal run is four to six minutes
DIRECTORY_BUDGET = 10 * 60    # the one stage with an unbounded provider call
GATE_HOURS = 20               # a success this recent means nothing to do
SOCKET_TIMEOUT = 60           # belt-and-braces for every urllib/requests call

_stage = "start"
_stages: dict[str, str] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _enter(stage: str) -> None:
    global _stage
    _stage = stage
    log.info("stage: %s", stage)


def _record(stage: str, status: str) -> None:
    """A stage's outcome. An earlier "ok" is never downgraded by a later
    skip — positions run twice when the Gateway is down."""
    if _stages.get(stage) != "ok":
        _stages[stage] = status


# ---------------------------------------------------------------- heartbeat

def _archive_census() -> dict[str, int]:
    """Row counts of the four archives. Flex's 365-day window means these
    files are the only copy of the early history; a count that shrinks
    between two runs is the alarm, and health.py compares them."""
    counts = {}
    for name, path in (("nav", store.NAV_PATH), ("cash", store.CASH_PATH),
                       ("nav_change", store.NAV_CHANGE_PATH), ("tx", store.TX_PATH)):
        counts[name] = (sum(1 for line in path.read_text().splitlines() if line.strip())
                        if path.exists() else 0)
    return counts


def _recent_success(hours: float, now: datetime | None = None) -> str | None:
    """When the last run finished OK within `hours`, its finish time."""
    run = store.read_json(store.LAST_RUN_PATH)
    if not isinstance(run, dict) or run.get("ok") is not True:
        return None
    finished = run.get("finished_at")
    try:
        stamp = datetime.fromisoformat(finished)
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return finished if (now - stamp) < timedelta(hours=hours) else None


def _start_run(*, forced: bool) -> dict:
    previous = store.read_json(store.LAST_RUN_PATH)
    previous = previous if isinstance(previous, dict) else {}
    run = {
        "schema_version": 1,
        "started_at": _now(),
        "finished_at": None,
        "pid": os.getpid(),
        "ok": None,
        "exit_code": None,
        "stage": "start",
        "stages": {},
        "forced": forced,
        "payload_source": None,
        "reason": None,
        "archive_previous": previous.get("archive"),
        "archive": None,
    }
    store.write_json(store.LAST_RUN_PATH, run)
    return run


def _finish_run(run: dict, *, ok: bool, exit_code: int,
                payload_source: str | None, reason: str | None = None) -> None:
    run.update({
        "finished_at": _now(),
        "ok": ok,
        "exit_code": exit_code,
        "stage": _stage,
        "stages": dict(_stages),
        "payload_source": payload_source,
        "reason": reason,
        "archive": _archive_census(),
    })
    store.write_json(store.LAST_RUN_PATH, run)


def _watchdog(run: dict) -> None:
    """End the run at JOB_BUDGET, whatever it is doing.

    Records the stage it was in, tells the desk, and hard-exits: the writes
    are all atomic, so the worst case is the stages after this one not
    happening tonight — which is exactly what would have happened anyway,
    except now the next night's run can start.
    """
    time.sleep(JOB_BUDGET)
    reason = f"timeout after {JOB_BUDGET // 60} min in stage {_stage}"
    log.error("watchdog: %s — ending the run", reason)
    try:
        _finish_run(run, ok=False, exit_code=3, payload_source=None, reason=reason)
    except Exception:
        log.exception("watchdog: could not record the timeout")
    notify.send("Portfolio refresh timed out", reason)
    logging.shutdown()
    os._exit(3)


# ---------------------------------------------------------------- archive

# The files that exist nowhere else once Flex's year-long window has moved
# past them. Committing data/ is the backup; this makes it happen.
ARCHIVE_FILES = ("nav_history.jsonl", "nav_change.jsonl", "cash_transactions.jsonl",
                 "transactions.jsonl", "positions_eod.json", "corporate_actions.jsonl",
                 "fx_rates.jsonl", "last_run.json", "quality.json")


def _autocommit_enabled() -> bool:
    try:
        doc = json.loads((Path(__file__).resolve().parent.parent / "config.local.json").read_text())
    except (OSError, ValueError):
        return False
    return bool(doc.get("git_autocommit"))


def _autocommit(today: date) -> str:
    """Commit tonight's archive files, locally, when config asks for it.

    Only the archive paths are staged, only when they changed, and only if
    nothing else is already staged — a half-finished hand edit must not ride
    along under "Data: nightly". Never pushes. Returns a one-word status.
    """
    root = Path(__file__).resolve().parent.parent
    paths = [f"dashboard/data/{name}" for name in ARCHIVE_FILES
             if (root / "dashboard" / "data" / name).exists()]

    def git(*args, check=True):
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                              timeout=60, check=check)
    try:
        if git("diff", "--cached", "--quiet", check=False).returncode != 0:
            log.info("autocommit: something else is staged; leaving the archive uncommitted")
            return "skipped"
        if git("diff", "--quiet", "--", *paths, check=False).returncode == 0 \
                and not git("ls-files", "--others", "--exclude-standard", "--", *paths).stdout.strip():
            return "unchanged"
        git("add", "--", *paths)
        git("commit", "-q", "-m", f"Data: nightly {today.isoformat()}")
        log.info("autocommit: committed %d archive file(s)", len(paths))
        return "ok"
    except Exception as exc:
        log.warning("autocommit failed: %s", str(exc)[:200])
        return "failed"


# ---------------------------------------------------------------- stages

def _refresh_attribution() -> str:
    """Bring the Change in NAV, cash and trade stores up to date. Never fatal.

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
        return "fresh"

    try:
        config = flex.load_config()
    except flex.FlexNotConfigured as exc:
        log.info("attribution: skipped (%s)", exc)
        return "skipped"

    status = "ok"
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
        # The Trades section rides the same statement. Until Sep 2026 only the
        # one-time backfill ever merged it, so the ledger quietly stopped at
        # the last hand run while positions moved on — the Gateway's own
        # fills, merged below, are empty on any night the Gateway is down.
        trades = flex.parse_trades(root)
        if trades:
            added, total = store.merge_transactions(trades)
            log.info("trades: whole span — %d row(s), %d new, %d stored (through %s)",
                     len(trades), added, total, trades[-1]["time"])
        rates = flex.parse_conversion_rates(root)
        if rates:
            added, total = store.merge_fx(rates)
            log.info("fx rates: whole span — %d row(s), %d new, %d stored", len(rates), added, total)
        actions = flex.parse_corporate_actions(root) + flex.parse_transfers(root)
        if actions:
            added, total = store.merge_corporate_actions(actions)
            log.info("corporate actions: whole span — %d row(s), %d new, %d stored",
                     len(actions), added, total)
    except Exception:
        # A Flex outage must not fail the job: the stored figures still serve,
        # and the next run picks this up.
        log.exception("attribution: whole span failed; the stored figures still serve")
        status = "failed"

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
            status = "failed"
            continue

        if periods:
            added, total = store.merge_nav_change(periods)
            log.info("attribution: %s — %d period(s), %d new, %d stored",
                     label, len(periods), added, total)
        if cash:
            added, total = store.merge_cash(cash)
            log.info("attribution: %s — %d cash row(s), %d new, %d stored",
                     label, len(cash), added, total)
    return status


def _refresh_positions() -> str:
    """Keep the EOD open-positions snapshot current. Never fatal.

    Flex is independent of IB Gateway, so this runs whether or not the Gateway
    was reachable — it is what lets a Gateway-less day still produce a full
    portfolio.json via flexfeed.
    """
    if not store.positions_eod_stale(hours=20):
        log.info("positions: refreshed within the day, skipping")
        return "fresh"
    try:
        config = flex.load_config()
    except flex.FlexNotConfigured as exc:
        log.info("positions: skipped (%s)", exc)
        return "skipped"
    try:
        rows = flex.fetch_positions(config)
    except Exception:
        log.exception("positions: fetch failed; the stored snapshot still serves")
        return "failed"
    if rows:
        written = store.write_positions_eod(rows)
        log.info("positions: %d rows as of %s", len(rows), written["asof"])
        return "ok"
    log.warning("positions: statement carried no Open Positions section — "
                "enable it on the Flex query")
    return "empty"


def _refresh_directory() -> str:
    """Rebuild the symbol directory, on a thread with its own budget.

    Its three provider calls carry no timeout, and on 31 Aug 2026 one of them
    never returned. Rather than wait, the rebuild is abandoned at
    DIRECTORY_BUDGET: the thread is a daemon, so it dies with the process,
    and SQLite rolls back whatever it was mid-way through. The previous
    directory keeps serving either way. `stale()` keeps a manual midday run
    from re-fetching three files.
    """
    if not directory.stale(hours=20):
        log.info("directory: fresh, %d symbols", directory.count())
        return "fresh"

    result: dict = {}

    def run() -> None:
        try:
            result["stats"] = directory.build(log_to=log.info)
        except Exception as exc:  # reported below, on the main thread
            result["error"] = exc

    worker = threading.Thread(target=run, name="directory-build", daemon=True)
    worker.start()
    worker.join(DIRECTORY_BUDGET)
    if worker.is_alive():
        log.error("directory: abandoned after %d min — a provider call never "
                  "returned; the previous directory still serves", DIRECTORY_BUDGET // 60)
        return "abandoned"
    if "error" in result:
        log.error("directory rebuild failed; the previous directory still serves: %s",
                  result["error"])
        return "failed"
    stats = result["stats"]
    log.info("directory: %d symbols, version %s (added %d, removed %d)",
             stats["rows"], stats["version"], stats["added"], stats["removed"])
    return "ok"


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh the dashboard's stores.")
    parser.add_argument("--force", action="store_true",
                        help="run even if the last run succeeded within %d hours" % GATE_HOURS)
    args = parser.parse_args(argv)

    logsetup.configure("refresh", max_bytes=2 * 1024 * 1024)
    socket.setdefaulttimeout(SOCKET_TIMEOUT)
    build.DATA_DIR.mkdir(parents=True, exist_ok=True)

    recent = None if args.force else _recent_success(GATE_HOURS)
    if recent and store.stores_current():
        log.info("last run succeeded at %s, within %dh, and the stores carry the newest "
                 "statement — nothing to do (pass --force to run anyway)", recent, GATE_HOURS)
        return 0

    run = _start_run(forced=args.force)
    threading.Thread(target=_watchdog, args=(run,), name="watchdog", daemon=True).start()

    payload = None
    gateway_error = None
    _enter("gateway")
    try:
        payload = build.build()
        _record("gateway", "ok")
    except ibkr.GatewayUnavailable as exc:
        # Not fatal any more: Flex still serves. Fetch the EOD positions first
        # so the fallback compose below has something to stand on.
        gateway_error = str(exc)
        log.error("gateway unavailable: %s", exc)
        _record("gateway", "unavailable")
    except Exception as exc:
        # A zombie Gateway accepts the socket and then drops it mid-request
        # (ConnectionError, not GatewayUnavailable). Whatever the flavour of
        # broken, the answer is the same: fall to Flex and carry on — a dead
        # Gateway must never abort the store refreshes that don't need it.
        gateway_error = str(exc)
        log.exception("gateway build failed; falling to Flex: %s", exc)
        _record("gateway", "unavailable")

    if payload is None:
        _enter("positions")
        _record("positions", _refresh_positions())
        _enter("compose")
        try:
            import flexfeed
            payload = flexfeed.compose_payload()
        except Exception:
            log.exception("flex fallback failed; the stale marker stands instead")
        if payload is not None:
            payload["meta"]["gateway"] = "unavailable"
            payload["meta"]["error"] = gateway_error
            log.info("portfolio composed from Flex EOD positions + delayed quotes")
            _record("compose", "ok")
        else:
            build.write_stale_marker(gateway_error or "gateway unavailable")
            _record("compose", "failed")

    if payload is not None:
        _enter("snapshot")
        try:
            store.write_json(build.OUT_PATH, payload)
            _record("snapshot", "ok")
        except Exception:
            # Uncaught, this used to kill the whole process: main() has no
            # top-level guard and the watchdog is a daemon thread, so it died
            # right along with it — last_run.json stuck at stage "start" and
            # not even a notification fired. Recording "failed" here instead
            # feeds the existing degraded-run notify at the end of main().
            log.exception("portfolio.json write failed; the previous snapshot still serves")
            _record("snapshot", "failed")

        # The overnight-diff baseline rides every successful build.
        try:
            import desk
            desk.write_close_snapshot(payload)
        except Exception:
            log.exception("close snapshot failed; overnight diff will be stale")

        # Record today's NAV only off the gateway's own account figure. The
        # Flex-composed NAV is EOD cash + repriced positions — close, but the
        # real figure for that day arrives via the Flex NAV series anyway.
        # ...and never on a weekend: IBKR reports no Saturday or Sunday, so a
        # row dated then would never be replaced by Flex and would sit in the
        # series as a trading day that never happened (2026-07-26 did).
        nav = payload.get("kpis", {}).get("net_liquidation")
        if (nav is not None and payload["meta"].get("source") != "flex-eod"
                and date.today().weekday() < 5):
            try:
                added, total = store.merge_nav([{
                    "date": date.today().isoformat(),
                    "nav_gbp": nav,
                    "source": "snapshot",
                    "recorded_at": _now(),
                }])
                log.info("NAV %.2f recorded (%d new, %d points stored)", nav, added, total)
                _record("nav", "ok")
            except Exception:
                log.exception("NAV history append failed; today's point is missing")
                _record("nav", "failed")

        fills = payload.get("fills") or []
        if fills:
            try:
                added, total = store.merge_transactions(fills)
                log.info("executions: %d new, %d stored", added, total)
                _record("transactions", "ok")
            except Exception:
                log.exception("transaction merge failed; today's fills are missing")
                _record("transactions", "failed")

    _enter("positions")
    _record("positions", _refresh_positions())
    _enter("attribution")
    _record("attribution", _refresh_attribution())

    # The stores agree with each other, or the morning chip says which do not.
    _enter("reconcile")
    try:
        report = reconcile.run()
        log.info("reconcile: %s — %s", report["status"],
                 ", ".join(f"{c['id']} {c['status']}" for c in report["checks"]))
        _record("reconcile", "ok")
    except Exception:
        log.exception("reconcile failed; the previous verdicts still serve")
        _record("reconcile", "failed")

    _enter("desk")
    try:
        import desk
        _record("desk", "ok" if desk.refresh_if_stale(hours=20) else "fresh")
    except Exception:
        log.exception("desk refresh failed; the previous file still serves")
        _record("desk", "failed")

    # Last, and deliberately so: nothing above depends on the directory, and
    # it is the stage most likely to stall, so it runs once everything that
    # matters is on disk — see _refresh_directory for the budget.
    _enter("directory")
    _record("directory", _refresh_directory())

    # The archive census. Flex's 365-day window means these files are the only
    # copy of the early history; a shrinking count here is the alarm.
    census = _archive_census()
    log.info("archive: nav=%d rows, cash=%d, nav_change=%d, tx=%d",
             census["nav"], census["cash"], census["nav_change"], census["tx"])

    if _autocommit_enabled():
        _enter("autocommit")
        _record("autocommit", _autocommit(date.today()))

    _enter("done")
    source = payload["meta"].get("source") if payload is not None else None
    if payload is None:
        log.error("refresh finished with no portfolio payload — stores were "
                  "still brought up to date")
        _finish_run(run, ok=False, exit_code=1, payload_source=None,
                    reason="no portfolio payload: " + (gateway_error or "unknown"))
        notify.send("Portfolio refresh: no snapshot",
                    "Neither IB Gateway nor Flex produced a portfolio tonight.")
        return 1
    _finish_run(run, ok=True, exit_code=0, payload_source=source)
    degraded = {k: v for k, v in _stages.items() if v in ("failed", "abandoned")}
    if degraded:
        notify.send("Portfolio refresh: degraded",
                    ", ".join(f"{k} {v}" for k, v in sorted(degraded.items())))
    log.info("refresh complete — %d positions, invested %.2f%s",
             len(payload.get("positions", [])), payload.get("kpis", {}).get("invested", 0),
             " (flex fallback)" if source == "flex-eod" else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
