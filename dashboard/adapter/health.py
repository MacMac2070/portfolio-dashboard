"""Is the pipeline OK? One verdict, from the files on disk.

Every endpoint already says how fresh its own data is, but nothing answered
the question a person actually has at seven in the morning: did last night's
job run, is the NAV series current, is anything silently stale. The night of
31 Aug 2026 made the case — the job hung in a provider call, two nights went
unrecorded, and every page kept drawing as if nothing had happened.

`build()` reads the heartbeat refresh.py writes (data/last_run.json), the
stores, the snapshot's own meta, and whatever the calling process knows live
— the feed's meta and the provider breakers — and returns a list of checks
with one overall status. Pure and cheap: no network, no locks, so
/api/health can be polled every minute. Every threshold takes `today` and
`now` explicitly so the tests can pin the clock.

Statuses: ok, pending (nothing wrong, not yet knowable), warn, fail. The
overall status is the worst check; pending counts as ok. A failing check
carries a `hint` that says what to do, because a red chip with no next step
is just a worry.
"""
from __future__ import annotations

import importlib.metadata
from datetime import date, datetime, timedelta, timezone

import store

JOB_OK_HOURS = 26          # one nightly run, with slack for a late statement
JOB_WARN_HOURS = 50        # one missed night is a warning; two is a failure
JOB_RUNNING_MINUTES = 45   # past refresh.JOB_BUDGET, a run that never finished
FEED_STALE_SECONDS = 300
LOG_WARN_BYTES = 20 * 1024 * 1024
PACKAGES = ("ib_async", "openbb", "yfinance", "openbb-yfinance")

KICKSTART = "launchctl kickstart -k gui/$UID/com.portfolio-dashboard.refresh"

_RANK = {"ok": 0, "pending": 0, "warn": 1, "fail": 2}


# ---------------------------------------------------------------- calendar

def last_weekday(day: date) -> date:
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def weekdays_behind(asof: str | None, today: date, *, now: datetime | None = None) -> int | None:
    """How many weekdays a store's newest date trails the newest statement
    there can be: the last weekday on or before today, or before yesterday
    when `now` is still earlier than the hour IBKR has today's ready. Friday's
    row read on Monday morning is 0 behind; IBKR reports every weekday,
    holidays included, so a missing weekday is real."""
    if not asof:
        return None
    try:
        have = date.fromisoformat(asof[:10])
    except ValueError:
        return None
    want = date.fromisoformat(store.expected_report_date(now=now.astimezone().replace(tzinfo=None))
                              ) if now else last_weekday(today)
    behind = 0
    day = have
    while day < want:
        day += timedelta(days=1)
        if day.weekday() < 5:
            behind += 1
    return behind


def _parse(ts) -> datetime | None:
    if not ts or not isinstance(ts, str):
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _hours_since(ts, now: datetime) -> float | None:
    parsed = _parse(ts)
    return (now - parsed).total_seconds() / 3600 if parsed else None


def _check(id: str, status: str, summary: str, *, detail=None, asof=None, hint=None) -> dict:
    out = {"id": id, "status": status, "summary": summary}
    if detail is not None:
        out["detail"] = detail
    if asof is not None:
        out["asof"] = asof
    if hint:
        out["hint"] = hint
    return out


def _lag(id: str, label: str, asof: str | None, today: date, hint: str,
         now: datetime | None = None) -> dict:
    behind = weekdays_behind(asof, today, now=now)
    if behind is None:
        return _check(id, "fail", f"No {label} recorded", hint=hint)
    status = "ok" if behind <= 1 else "warn" if behind == 2 else "fail"
    tail = f", {behind} weekdays behind" if behind else ""
    return _check(id, status, f"{label} through {asof}{tail}", asof=asof,
                  hint=hint if status != "ok" else None)


# ---------------------------------------------------------------- checks

def check_job(now: datetime) -> list[dict]:
    run = store.read_json(store.LAST_RUN_PATH)
    if not isinstance(run, dict):
        return [_check("job.last_run", "fail", "Nightly job has never recorded a run",
                       hint=f"Run it once by hand: {KICKSTART}")]
    out = []
    ok = run.get("ok")
    started = run.get("started_at")
    finished = run.get("finished_at")
    if ok is None:
        minutes = (_hours_since(started, now) or 0) * 60
        if minutes <= JOB_RUNNING_MINUTES:
            out.append(_check("job.last_run", "pending",
                              f"Nightly job running since {started} (stage {run.get('stage')})",
                              asof=started))
        else:
            out.append(_check("job.last_run", "fail",
                              f"Nightly job started {started} and never finished "
                              f"(stage {run.get('stage')})", asof=started,
                              hint=f"Stop it and run again: {KICKSTART}"))
    elif ok is False:
        out.append(_check("job.last_run", "fail",
                          f"Nightly job failed at {finished}: {run.get('reason') or 'no portfolio payload'}",
                          asof=finished, hint=f"Check logs/refresh.log, then {KICKSTART}"))
    else:
        age = _hours_since(finished, now)
        if age is None:
            status, summary = "warn", "Nightly job finished at an unreadable time"
        elif age <= JOB_OK_HOURS:
            status, summary = "ok", f"Nightly job ran {finished}"
        elif age <= JOB_WARN_HOURS:
            status, summary = "warn", f"Nightly job last ran {finished}, a night has been missed"
        else:
            status, summary = "fail", f"Nightly job last ran {finished}, {int(age // 24)} days ago"
        out.append(_check("job.last_run", status, summary, asof=finished,
                          hint=None if status == "ok" else f"Run it now: {KICKSTART}"))

    # A Gateway that was simply down is not a degraded run — the snapshot
    # check and the feed pill already say so. This is for stages that broke.
    stages = run.get("stages") or {}
    bad = {name: state for name, state in stages.items()
           if state in ("failed", "abandoned")}
    if bad:
        out.append(_check("job.stages", "warn",
                          "Last run degraded: " + ", ".join(f"{k} {v}" for k, v in sorted(bad.items())),
                          detail=bad))

    previous = run.get("archive_previous") or {}
    current = run.get("archive") or {}
    shrunk = {k: (previous[k], current[k]) for k in current
              if isinstance(previous.get(k), int) and current[k] < previous[k]}
    if shrunk:
        out.append(_check("archive.shrunk", "fail",
                          "An archive lost rows: " + ", ".join(
                              f"{k} {a}→{b}" for k, (a, b) in sorted(shrunk.items())),
                          detail=shrunk, hint="Restore data/ from git before the next run"))
    return out


def check_stores(today: date, now: datetime | None = None) -> list[dict]:
    out = [_lag("nav.lag", "NAV history", store.nav_latest(), today,
                hint="Run the nightly job, or adapter/backfill.py for a longer gap", now=now)]
    eod = store.read_positions_eod()
    out.append(_lag("positions.asof", "EOD positions", (eod or {}).get("asof"), today,
                    hint="Run the nightly job; positions come from the Flex Open Positions section",
                    now=now))
    return out


def check_snapshot(now: datetime) -> list[dict]:
    doc = store.read_json(store.DATA_DIR / "portfolio.json")
    if not isinstance(doc, dict):
        return [_check("snapshot.source", "fail", "No portfolio.json on disk",
                       hint="Run adapter/build.py or the nightly job")]
    meta = doc.get("meta") or {}
    unpriced = [p.get("symbol") for p in doc.get("positions") or [] if p.get("fx_missing")]
    if unpriced:
        return [_check("fx.missing", "fail",
                       "No GBP rate for " + ", ".join(str(s) for s in unpriced) +
                       " — those positions are left out of every total",
                       detail=unpriced,
                       hint="Add the currency to marketdata.FX_CURRENCIES, or check the FX feed")]
    if meta.get("gateway") == "unavailable":
        since = meta.get("stale_since") or meta.get("generated_at")
        return [_check("snapshot.source", "warn",
                       f"Snapshot built without IB Gateway ({meta.get('source') or 'stale marker'}) "
                       f"as of {meta.get('positions_asof') or since}",
                       asof=since, hint="Start IB Gateway on :4001 for live positions")]
    return [_check("snapshot.source", "ok",
                   f"Snapshot from {meta.get('source') or 'IB Gateway'} at {meta.get('generated_at')}",
                   asof=meta.get("generated_at"))]


def check_feed(feed_meta: dict | None, now: datetime) -> list[dict]:
    if not feed_meta:
        return [_check("feed", "pending", "No live feed in this process")]
    source = feed_meta.get("source") or "none"
    if not feed_meta.get("connected"):
        return [_check("feed", "warn", f"Feed disconnected; serving {source}",
                       detail=feed_meta.get("error"),
                       hint="Start IB Gateway on :4001; the Flex EOD fallback serves meanwhile")]
    age = ((now - _parse(feed_meta.get("last_refresh"))).total_seconds()
           if _parse(feed_meta.get("last_refresh")) else None)
    if age is not None and age > max(FEED_STALE_SECONDS, (feed_meta.get("poll_seconds") or 3) * 5):
        return [_check("feed", "warn", f"Feed last refreshed {int(age // 60)} min ago ({source})",
                       asof=feed_meta.get("last_refresh"))]
    if source == "flex-eod":
        return [_check("feed", "warn",
                       f"IB Gateway unavailable; positions from Flex EOD as of "
                       f"{feed_meta.get('positions_asof')}, quotes delayed",
                       asof=feed_meta.get("positions_asof"),
                       hint="Start IB Gateway on :4001 for live positions")]
    return [_check("feed", "ok", f"Live feed connected ({source})",
                   asof=feed_meta.get("last_refresh"))]


def check_breakers(breakers: dict | None) -> list[dict]:
    if not breakers:
        return []
    open_ = {name: b for name, b in breakers.items() if b.get("state") in ("open", "half-open")}
    if not open_:
        return [_check("breakers", "ok", "All providers answering")]
    return [_check("breakers", "warn",
                   "Provider paused: " + ", ".join(
                       f"{name} ({b.get('state')}, retry in {int(b.get('retry_after') or 0)}s)"
                       for name, b in sorted(open_.items())),
                   detail={name: b.get("last_error") for name, b in open_.items()})]


def check_quality() -> list[dict]:
    """Fold in the reconciliation verdicts when the nightly job has written them."""
    doc = store.read_json(store.QUALITY_PATH)
    if not isinstance(doc, dict):
        return []
    out = []
    for check in doc.get("checks") or []:
        if not isinstance(check, dict) or not check.get("id"):
            continue
        out.append({**check, "id": f"quality.{check['id']}",
                    "status": check.get("status") if check.get("status") in _RANK else "warn"})
    return out


def check_quotes(today: date) -> list[dict]:
    """Held symbols and the board's indices whose newest stored close is
    more than three weekdays old — the store is what an outage serves from,
    so this is how long it has been serving old prices."""
    try:
        import markets
        import quotes
        import universe
    except Exception:
        return []
    symbols = [t.symbol for t in universe.TICKERS.values() if t.owned]
    symbols += [i.symbol for m in markets.MARKETS for i in m.indices]
    try:
        stale = quotes.stale_symbols(symbols, today)
    except Exception as exc:
        return [_check("quotes.stale", "warn", f"Quote store unreadable: {exc}")]
    if not stale:
        return [_check("quotes.stale", "ok", f"Stored closes current for {len(symbols)} symbols")]
    if len(stale) == len(symbols):
        return [_check("quotes.stale", "pending", "Quote store not filled yet")]
    return [_check("quotes.stale", "warn",
                   f"{len(stale)} of {len(symbols)} symbols have no close in three weekdays: "
                   + ", ".join(f"{s} ({d or 'never'})" for s, d in sorted(stale.items())[:8]),
                   detail=stale)]


def check_logs() -> list[dict]:
    logs = store.DATA_DIR.parent / "logs"
    big = {}
    try:
        for path in logs.glob("*.log"):
            size = path.stat().st_size
            if size > LOG_WARN_BYTES:
                big[path.name] = size
    except OSError:
        return []
    if not big:
        return []
    return [_check("logs.size", "warn",
                   "Large logs: " + ", ".join(f"{k} {v // (1024 * 1024)} MB" for k, v in sorted(big.items())),
                   detail=big)]


def _versions() -> dict[str, str | None]:
    out = {}
    for name in PACKAGES:
        try:
            out[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            out[name] = None
    return out


# ---------------------------------------------------------------- build

def build(*, feed: dict | None = None, breakers: dict | None = None,
          today: date | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    today = today or now.astimezone().date()
    checks = [
        *check_job(now),
        *check_stores(today, now),
        *check_snapshot(now),
        *check_feed(feed, now),
        *check_breakers(breakers),
        *check_quality(),
        *check_quotes(today),
        *check_logs(),
    ]
    worst = max((_RANK.get(c["status"], 1) for c in checks), default=0)
    status = {0: "ok", 1: "warn", 2: "fail"}[worst]
    counts = {"warn": sum(1 for c in checks if c["status"] == "warn"),
              "fail": sum(1 for c in checks if c["status"] == "fail")}
    return {
        "meta": {"error": None, "served_at": now.isoformat(timespec="seconds"),
                 "versions": _versions()},
        "status": status,
        "counts": counts,
        "checks": checks,
    }


if __name__ == "__main__":
    import json

    report = build()
    print(report["status"].upper(), json.dumps(report["counts"]))
    for check in report["checks"]:
        line = f"  {check['status']:<8} {check['id']:<20} {check['summary']}"
        if check.get("hint"):
            line += f"\n           → {check['hint']}"
        print(line)
