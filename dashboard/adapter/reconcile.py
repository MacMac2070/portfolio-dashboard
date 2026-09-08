"""Does the data agree with itself? The balance-assertion layer.

Two sources of truth for the same fifteen positions were picked between and
never compared. Trades were stored and never replayed. The NAV series had no
gap check, and the deposits that make the return time-weighted came from one
optional Flex section with nothing to confirm them. Each of those is a way for
a wrong number to reach the page looking exactly like a right one.

Every check here is a pure function over rows, so it is testable on paper.
`run()` reads the stores, writes the verdicts to data/quality.json, and
health.py folds them into /api/health as `quality.<id>`. The nightly job runs
it after the stores are refreshed; `python adapter/reconcile.py` runs it by
hand and exits 0 / 1 / 2 for ok / warn / fail.

    positions.replay   the ledger replayed vs positions_eod.json
    nav.continuity     duplicates, missing weekdays, zeros, lag, unreplaced snapshots
    flows.deposits     cash deposits vs the Change in NAV's own figure, per period
    nav.composition    positions × FX vs the NAV row's stock; total vs stock + cash + accruals
    cash.unbucketed    cash rows the bucket table did not recognise
    live.positions     the live feed vs the EOD snapshot — only when a snapshot is passed

A check that cannot be made yet says `pending`, never `ok`: the composition
check needs a NAV row for the same date as the positions file, and Flex
reports them a day apart.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import flex
import health
import store

log = logging.getLogger("reconcile")

SHARE_TOLERANCE = 1e-6        # fractional shares are real (AMZN 9.5); dust is not
DEPOSIT_TOLERANCE = 0.01      # £
SETTLEMENT_DAYS = 3           # a deposit dated on a Saturday is booked on the Monday
STOCK_TOLERANCE_PCT = 0.5     # positions × FX vs the statement's own stock figure
TOTAL_TOLERANCE_GBP = 1.0     # stock + cash + accruals vs total
LIVE_TOLERANCE_PCT = 5.0      # intraday moves and delayed quotes explain this much
UNREPLACED_SNAPSHOT_WEEKDAYS = 3
FLEX_REACH_DAYS = 360         # Flex statements reach back about a year; older rows are ours alone

# Ledger rows that are positions. FX conversions sit in the same file (asset
# CASH, venue IDEALFX); legacy rows carry no asset at all, so the venue is the
# second test.
POSITION_ASSETS = (None, "", "STK", "ETF", "FUND")
FX_VENUE = "IDEALFX"

_RANK = {"ok": 0, "pending": 0, "warn": 1, "fail": 2}


def _check(id: str, status: str, summary: str, *, detail=None, hint=None) -> dict:
    out = {"id": id, "status": status, "summary": summary}
    if detail is not None:
        out["detail"] = detail
    if hint:
        out["hint"] = hint
    return out


def _worst(*statuses: str) -> str:
    return max(statuses, key=lambda s: _RANK.get(s, 1), default="ok")


# ---------------------------------------------------------------- replay

def is_position_row(row: dict) -> bool:
    if row.get("exchange") == FX_VENUE:
        return False
    return (row.get("asset") or None) in POSITION_ASSETS


def replay_positions(tx_rows: list[dict], *, upto: str | None = None,
                     actions: list[dict] | tuple = ()) -> dict[int, float]:
    """Net quantity per con_id from the ledger, plus any corporate actions.

    An action is a signed quantity on a date, exactly like a fill — a split
    or a spin-off replays without ratio arithmetic here. `upto` bounds both by
    date (inclusive) so the replay can be compared with a snapshot as of that
    day.
    """
    out: dict[int, float] = {}
    for row in tx_rows:
        if not is_position_row(row):
            continue
        when = (row.get("time") or "")[:10]
        if upto and when > upto:
            continue
        try:
            con_id = int(row.get("con_id") or 0)
            qty = abs(float(row.get("quantity") or 0))
        except (TypeError, ValueError):
            continue
        if not con_id or not qty:
            continue
        sign = 1.0 if str(row.get("side") or "").upper().startswith("B") else -1.0
        out[con_id] = out.get(con_id, 0.0) + sign * qty
    for action in actions:
        when = (action.get("date") or "")[:10]
        if upto and when > upto:
            continue
        try:
            con_id = int(action.get("con_id") or 0)
            qty = float(action.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
        if con_id and qty:
            out[con_id] = out.get(con_id, 0.0) + qty
    return out


def diff_positions(expected: dict[int, float], eod_rows: list[dict],
                   *, tolerance: float = SHARE_TOLERANCE) -> list[dict]:
    """Every con_id where the replay and the snapshot disagree."""
    held = {}
    for row in eod_rows or []:
        try:
            held[int(row.get("con_id") or 0)] = row
        except (TypeError, ValueError):
            continue
    out = []
    for con_id in sorted(set(expected) | set(held)):
        ledger = expected.get(con_id, 0.0)
        row = held.get(con_id)
        eod = float(row.get("quantity") or 0.0) if row else 0.0
        if abs(ledger - eod) <= tolerance:
            continue
        out.append({"con_id": con_id, "symbol": (row or {}).get("symbol") or "",
                    "ledger": round(ledger, 6), "eod": round(eod, 6),
                    "diff": round(ledger - eod, 6)})
    return out


def ledger_latest(tx_rows: list[dict]) -> str | None:
    dates = [(r.get("time") or "")[:10] for r in tx_rows if is_position_row(r)]
    dates = [d for d in dates if d]
    return max(dates) if dates else None


def check_replay(tx_rows: list[dict], eod: dict | None,
                 actions: list[dict] | tuple = ()) -> dict:
    if not eod or not eod.get("positions"):
        return _check("positions.replay", "pending", "No EOD positions snapshot to replay against")
    asof = eod.get("asof")
    expected = replay_positions(tx_rows, upto=asof, actions=actions)
    diffs = diff_positions(expected, eod["positions"])
    latest = ledger_latest(tx_rows)
    if not diffs:
        return _check("positions.replay", "ok",
                      f"Ledger replays to the {len(eod['positions'])} EOD positions as of {asof}")
    names = ", ".join(f"{d['symbol'] or d['con_id']} {d['ledger']:g} vs {d['eod']:g}" for d in diffs[:6])
    if latest and asof and latest < asof:
        return _check("positions.replay", "warn",
                      f"Ledger ends {latest}, positions as of {asof}; {len(diffs)} differ ({names})",
                      detail=diffs,
                      hint="Trades between those dates may be missing — the nightly job merges "
                           "the Flex Trades section; run adapter/backfill.py for a longer gap")
    return _check("positions.replay", "fail",
                  f"{len(diffs)} position(s) disagree with the ledger as of {asof} ({names})",
                  detail=diffs,
                  hint="A trade or corporate action is missing from transactions.jsonl; "
                       "check the Flex Trades and Corporate Actions sections")


# ---------------------------------------------------------------- NAV series

def nav_continuity(nav_rows: list[dict], *, today: date, now: datetime | None = None) -> dict:
    """Duplicates, missing weekdays, zeros after inception, lag, and snapshot
    rows Flex never replaced. IBKR reports every weekday, holidays included,
    so a missing weekday is a real hole — and the daily return would silently
    compress the missing days into one."""
    rows = [r for r in nav_rows if r.get("date")]
    if not rows:
        return _check("nav.continuity", "fail", "No NAV history",
                      hint="Run adapter/backfill.py")
    seen: dict[str, int] = {}
    for r in rows:
        seen[r["date"]] = seen.get(r["date"], 0) + 1
    duplicates = sorted(d for d, n in seen.items() if n > 1)

    ordered = sorted(rows, key=lambda r: r["date"])
    inception = next((r["date"] for r in ordered if r.get("nav_gbp")), None)
    zeros = [r["date"] for r in ordered if inception and r["date"] > inception and not r.get("nav_gbp")]

    gaps = []
    for prev, cur in zip(ordered, ordered[1:]):
        try:
            a, b = date.fromisoformat(prev["date"]), date.fromisoformat(cur["date"])
        except ValueError:
            continue
        if inception and cur["date"] <= inception:
            continue
        missing = health.weekdays_behind(prev["date"], b) - (1 if b.weekday() < 5 else 0)
        if missing > 0:
            gaps.append({"after": prev["date"], "before": cur["date"], "missing_weekdays": missing})

    latest = ordered[-1]["date"]
    lag = health.weekdays_behind(latest, today, now=now) or 0
    reach = (today - timedelta(days=FLEX_REACH_DAYS)).isoformat()
    unreplaced, weekend = [], []
    for r in ordered:
        if r.get("source") != "snapshot":
            continue
        try:
            on_weekend = date.fromisoformat(r["date"]).weekday() >= 5
        except ValueError:
            continue
        if on_weekend:
            weekend.append(r["date"])       # IBKR never reports a weekend; Flex cannot replace it
        elif r["date"] >= reach and (health.weekdays_behind(r["date"], today) or 0) > UNREPLACED_SNAPSHOT_WEEKDAYS:
            unreplaced.append(r["date"])

    status = "ok"
    problems = []
    if duplicates:
        status = "fail"
        problems.append(f"{len(duplicates)} duplicate date(s)")
    if zeros:
        status = "fail"
        problems.append(f"{len(zeros)} zero row(s) after inception")
    if gaps:
        worst_gap = max(g["missing_weekdays"] for g in gaps)
        status = _worst(status, "fail" if worst_gap >= 2 else "warn")
        problems.append(f"{len(gaps)} gap(s), up to {worst_gap} weekday(s) missing")
    if lag >= 2:
        status = _worst(status, "fail" if lag >= 3 else "warn")
        problems.append(f"{lag} weekdays behind")
    if unreplaced:
        status = _worst(status, "warn")
        problems.append(f"{len(unreplaced)} snapshot row(s) Flex never replaced")
    if weekend:
        status = _worst(status, "warn")
        problems.append(f"{len(weekend)} snapshot row(s) dated on a weekend ({', '.join(weekend)})")
    summary = (f"NAV series continuous through {latest}" if status == "ok"
               else f"NAV series through {latest}: " + ", ".join(problems))
    hint = None
    if status != "ok":
        hint = ("Delete the weekend row(s) from data/nav_history.jsonl — IBKR reports no weekend, "
                "so the daily return counts a day that never traded" if weekend and len(problems) == 1
                else "Run adapter/backfill.py --months 1 to re-pull the affected window from Flex")
    return _check("nav.continuity", status, summary,
                  detail={"duplicates": duplicates, "zeros": zeros, "gaps": gaps,
                          "lag_weekdays": lag, "unreplaced_snapshots": unreplaced,
                          "weekend_snapshots": weekend},
                  hint=hint)


# ---------------------------------------------------------------- deposits

def _sub_periods(change_rows: list[dict]) -> list[dict]:
    """One row per month: the sub-period with the latest to_date, because the
    current month is restated on every run and every restatement is stored."""
    by_month: dict[str, dict] = {}
    for row in change_rows or []:
        try:
            span = int(row.get("span_days") or 0)
        except (TypeError, ValueError):
            continue
        if not row.get("from_date") or span > flex.MONTH_SPAN_DAYS:
            continue
        key = row["from_date"][:7]
        if key not in by_month or (row.get("to_date") or "") > (by_month[key].get("to_date") or ""):
            by_month[key] = row
    return [by_month[k] for k in sorted(by_month)]


def _cash_deposits(cash_rows: list[dict], start: str, end: str) -> tuple[float, int]:
    total, n = 0.0, 0
    for row in cash_rows or []:
        if row.get("bucket") != "deposits_withdrawals":
            continue
        when = row.get("date") or ""
        if start <= when <= end:
            total += float(row.get("amount_gbp") or 0.0)
            n += 1
    return total, n


def deposits_vs_nav_change(cash_rows: list[dict], change_rows: list[dict],
                           *, tolerance: float = DEPOSIT_TOLERANCE) -> dict:
    """The deposits the daily return excludes, checked against the broker's
    own Change in NAV figure for every period it reports.

    The two come from different Flex sections. If the Cash Transactions query
    stops carrying Deposits/Withdrawals, `track.daily_series` sees no flows and
    every deposit becomes performance — this is the check that notices.
    """
    periods = _sub_periods(change_rows)
    widest = max((r for r in change_rows or [] if r.get("from_date")),
                 key=lambda r: int(r.get("span_days") or 0), default=None)
    if widest is not None and widest not in periods:
        periods = periods + [widest]
    if not periods:
        return _check("flows.deposits", "pending", "No Change in NAV rows to check deposits against",
                      hint="Add the Change in NAV section to the Flex query")

    mismatches = []
    settled = []
    reported_total = 0.0
    matched_rows = 0
    for row in periods:
        reported = float(row.get("depositsWithdrawals") or 0.0)
        start, end = row["from_date"], row.get("to_date") or row["from_date"]
        got, n = _cash_deposits(cash_rows, start, end)
        matched_rows += n
        reported_total += abs(reported)
        if abs(reported - got) <= tolerance:
            continue
        # The cash row carries the settlement date; the Change in NAV books
        # the deposit on the first business day inside its window. A row a
        # few days outside the edge that squares the figure is that, not a
        # missing deposit.
        # Each edge on its own first: widening both at once can pull in the
        # next period's first deposit and hide a match that was there.
        wide_start = (date.fromisoformat(start) - timedelta(days=SETTLEMENT_DAYS)).isoformat()
        wide_end = (date.fromisoformat(end) + timedelta(days=SETTLEMENT_DAYS)).isoformat()
        if any(abs(reported - _cash_deposits(cash_rows, a, b)[0]) <= tolerance
               for a, b in ((wide_start, end), (start, wide_end), (wide_start, wide_end))):
            settled.append({"from": start, "to": end, "nav_change": round(reported, 2)})
            continue
        mismatches.append({"from": start, "to": end,
                           "nav_change": round(reported, 2), "cash_rows": round(got, 2), "rows": n})
    if reported_total > tolerance and matched_rows == 0:
        return _check("flows.deposits", "fail",
                      "The Change in NAV reports deposits but no cash-transaction row carries any — "
                      "every deposit is being counted as performance",
                      detail=mismatches,
                      hint="Add Deposits/Withdrawals to the Cash Transactions types on the Flex query, "
                           "then run adapter/backfill.py")
    if mismatches:
        worst = mismatches[0]
        return _check("flows.deposits", "warn",
                      f"Deposits disagree in {len(mismatches)} period(s): {worst['from']}–{worst['to']} "
                      f"cash rows £{worst['cash_rows']:,.2f} vs Change in NAV £{worst['nav_change']:,.2f}",
                      detail=mismatches,
                      hint="Run adapter/backfill.py --months 1 to re-pull the cash rows for that window")
    return _check("flows.deposits", "ok",
                  f"Deposits agree with the Change in NAV across {len(periods)} period(s)"
                  + (f" ({len(settled)} matched on settlement date)" if settled else ""),
                  detail={"settled": settled} if settled else None)


# ---------------------------------------------------------------- composition

def positions_vs_nav(eod: dict | None, nav_row: dict | None, *, fx: dict | None = None,
                     stock_tolerance_pct: float = STOCK_TOLERANCE_PCT,
                     total_tolerance_gbp: float = TOTAL_TOLERANCE_GBP) -> dict:
    """Σ positions × FX vs the NAV row's own stock figure, and the row's parts
    vs its total. Both come from Flex, one from Open Positions and one from
    the Equity Summary, so a disagreement is a position the snapshot lost."""
    if not eod or not eod.get("positions"):
        return _check("nav.composition", "pending", "No EOD positions snapshot")
    asof = eod.get("asof")
    if not nav_row:
        return _check("nav.composition", "pending",
                      f"No NAV row for {asof} yet; the statement lands a day later")
    stock = nav_row.get("stock_gbp")
    if stock is None:
        return _check("nav.composition", "pending",
                      f"NAV row for {asof} carries no stock/cash split",
                      hint="The next nightly run stores it; or adapter/backfill.py --months 1")
    fx = fx or {}
    total_value = 0.0
    unpriced = []
    for p in eod["positions"]:
        value = p.get("value")
        rate = p.get("fx_to_base") or fx.get(p.get("currency"))
        if value is None or not rate:
            unpriced.append(p.get("symbol") or p.get("con_id"))
            continue
        total_value += float(value) * float(rate)
    detail = {"positions_gbp": round(total_value, 2), "stock_gbp": round(stock, 2),
              "cash_gbp": nav_row.get("cash_gbp"), "accruals_gbp": nav_row.get("accruals_gbp"),
              "total_gbp": nav_row.get("nav_gbp"), "unpriced": unpriced}
    if unpriced:
        return _check("nav.composition", "warn",
                      f"{len(unpriced)} position(s) carry no value or FX in the snapshot",
                      detail=detail)
    gap_pct = abs(total_value - stock) / stock * 100 if stock else 0.0
    status, problems = "ok", []
    if gap_pct > stock_tolerance_pct:
        status = "fail"
        problems.append(f"positions £{total_value:,.0f} vs statement stock £{stock:,.0f} ({gap_pct:.2f}%)")
    parts = stock + (nav_row.get("cash_gbp") or 0.0) + (nav_row.get("accruals_gbp") or 0.0)
    total = nav_row.get("nav_gbp")
    if total is not None and abs(parts - total) > total_tolerance_gbp:
        status = _worst(status, "warn")
        problems.append(f"stock + cash + accruals £{parts:,.2f} vs total £{total:,.2f}")
    summary = (f"Positions match the statement's stock figure for {asof}" if status == "ok"
               else f"Composition for {asof}: " + "; ".join(problems))
    return _check("nav.composition", status, summary, detail=detail,
                  hint=None if status == "ok" else
                  "Re-pull positions and NAV for that date with adapter/backfill.py --months 1")


# ---------------------------------------------------------------- live vs Flex

def diff_live(live: dict | None, eod: dict | None, nav_rows: list[dict],
              *, tolerance_pct: float = LIVE_TOLERANCE_PCT) -> dict | None:
    """The live feed against the EOD snapshot. None when there is nothing live."""
    if not live or not live.get("positions") or not eod or not eod.get("positions"):
        return None
    meta = live.get("meta") or {}
    if meta.get("source") == "flex-eod":
        return None                       # the same file on both sides
    held = {int(p["con_id"]): p for p in eod["positions"] if p.get("con_id")}
    problems = []
    detail: dict = {"quantity": []}
    for p in live["positions"]:
        try:
            con_id = int(p.get("con_id") or 0)
        except (TypeError, ValueError):
            continue
        row = held.get(con_id)
        eod_qty = float(row.get("quantity") or 0) if row else 0.0
        live_qty = float(p.get("quantity") or 0)
        if abs(live_qty - eod_qty) > SHARE_TOLERANCE:
            detail["quantity"].append({"symbol": p.get("symbol"), "live": live_qty, "eod": eod_qty})
    if detail["quantity"]:
        problems.append(f"{len(detail['quantity'])} quantity difference(s) — an intraday trade?")

    fx = live.get("fx") or {}
    eod_value = sum(float(r.get("value") or 0) * float(r.get("fx_to_base") or fx.get(r.get("currency")) or 0)
                    for r in eod["positions"])
    invested = (live.get("kpis") or {}).get("invested")
    if invested and eod_value:
        gap = abs(invested - eod_value) / eod_value * 100
        detail["invested"] = {"live": round(invested, 2), "eod": round(eod_value, 2), "gap_pct": round(gap, 2)}
        if gap > tolerance_pct:
            problems.append(f"invested £{invested:,.0f} vs EOD £{eod_value:,.0f} ({gap:.1f}%)")
    nav = (live.get("kpis") or {}).get("net_liquidation")
    latest = max((r for r in nav_rows if r.get("nav_gbp")), key=lambda r: r["date"], default=None)
    if nav and latest:
        gap = abs(nav - latest["nav_gbp"]) / latest["nav_gbp"] * 100
        detail["nav"] = {"live": round(nav, 2), "flex": round(latest["nav_gbp"], 2),
                         "flex_date": latest["date"], "gap_pct": round(gap, 2)}
        if gap > tolerance_pct:
            problems.append(f"NAV £{nav:,.0f} vs Flex £{latest['nav_gbp']:,.0f} on {latest['date']} ({gap:.1f}%)")
    if meta.get("invested_check") == "fallback":
        problems.append("the feed fell back to account values (repriced invested disagreed with IBKR)")
    if not problems:
        return _check("live.positions", "ok", "Live feed agrees with the EOD snapshot", detail=detail)
    return _check("live.positions", "warn", "; ".join(problems), detail=detail)


# ---------------------------------------------------------------- cash rows

def unknown_actions(actions: list[dict]) -> dict | None:
    kinds = {}
    for row in actions or []:
        if row.get("kind") == "other":
            kinds[row.get("code") or "?"] = kinds.get(row.get("code") or "?", 0) + 1
    if not kinds:
        return None
    return _check("actions.unbucketed", "warn",
                  "Corporate action code(s) the replay does not know: "
                  + ", ".join(f"{k} ×{n}" for k, n in sorted(kinds.items())),
                  detail=kinds,
                  hint="Add the code to flex.ACTION_KINDS with what it does to the share count")


def positions_repriceable(eod: dict | None) -> list[dict]:
    """A held contract the quote tables cannot name cannot be repriced when
    the Gateway is down, and one Flex marks at nothing has no value at all.
    Both are what a ticker change or a delisting looks like from here."""
    if not eod or not eod.get("positions"):
        return []
    try:
        import marketdata
        known = set(marketdata.QUOTE_SYMBOLS)
    except Exception:
        known = set()
    out = []
    unmapped = [p.get("symbol") or p.get("con_id") for p in eod["positions"]
                if known and int(p.get("con_id") or 0) not in known]
    if unmapped:
        out.append(_check("positions.unmapped", "warn",
                          "No quote symbol for " + ", ".join(str(s) for s in unmapped) +
                          " — it cannot be repriced without the Gateway",
                          detail=unmapped,
                          hint="Add the contract to marketdata.QUOTE_SYMBOLS (a ticker change?)"))
    unmarked = [p.get("symbol") or p.get("con_id") for p in eod["positions"]
                if p.get("mark_price") is None or p.get("value") is None]
    if unmarked:
        out.append(_check("positions.unmarked", "warn",
                          "Flex reports no mark for " + ", ".join(str(s) for s in unmarked),
                          detail=unmarked,
                          hint="A halted or delisted line; check the Corporate Actions section"))
    return out


def unbucketed_cash(cash_rows: list[dict]) -> dict:
    kinds: dict[str, int] = {}
    for row in cash_rows or []:
        if row.get("bucket") == "other":
            kinds[row.get("type") or "?"] = kinds.get(row.get("type") or "?", 0) + 1
    if not kinds:
        return _check("cash.unbucketed", "ok", "Every cash transaction type is recognised")
    return _check("cash.unbucketed", "warn",
                  "Unrecognised cash transaction type(s): " + ", ".join(f"{k} ×{n}" for k, n in sorted(kinds.items())),
                  detail=kinds,
                  hint="Add the type to flex.CASH_TYPES so it lands in the right income or cost band")


# ---------------------------------------------------------------- run

def build(*, today: date | None = None, live: dict | None = None,
          actions: list[dict] | tuple = (), now: datetime | None = None) -> dict:
    """Every check over the stores on disk. Pure apart from reading them."""
    if today is None:
        now = now or datetime.now(timezone.utc)
        today = now.astimezone().date()
    nav_rows = store._read(store.NAV_PATH)
    tx_rows = store._read(store.TX_PATH)
    cash_rows = store._read(store.CASH_PATH)
    change_rows = store._read(store.NAV_CHANGE_PATH)
    eod = store.read_positions_eod()
    nav_for_asof = next((r for r in nav_rows if eod and r.get("date") == eod.get("asof")), None)
    actions = list(actions) + store.corporate_actions()

    checks = [
        check_replay(tx_rows, eod, actions),
        nav_continuity(nav_rows, today=today, now=now),
        deposits_vs_nav_change(cash_rows, change_rows),
        positions_vs_nav(eod, nav_for_asof, fx=(live or {}).get("fx")),
        unbucketed_cash(cash_rows),
    ]
    checks += positions_repriceable(eod)
    unknown = unknown_actions(actions)
    if unknown:
        checks.append(unknown)
    live_check = diff_live(live, eod, nav_rows)
    if live_check:
        checks.append(live_check)
    status = _worst(*(c["status"] for c in checks))
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "asof": {
            "nav": max((r["date"] for r in nav_rows if r.get("date")), default=None),
            "positions": (eod or {}).get("asof"),
            "transactions": ledger_latest(tx_rows),
            "cash": max((r.get("date") or "" for r in cash_rows), default=None) or None,
            "nav_change": max((r.get("to_date") or "" for r in change_rows), default=None) or None,
        },
        "status": status,
        "checks": checks,
    }


def run(*, today: date | None = None, live: dict | None = None,
        actions: list[dict] | tuple = (), write: bool = True) -> dict:
    """Build the verdicts and, by default, write data/quality.json."""
    report = build(today=today, live=live, actions=actions)
    if write:
        store.write_json(store.QUALITY_PATH, report)
    return report


if __name__ == "__main__":
    import argparse
    import json
    import sys
    import urllib.request

    parser = argparse.ArgumentParser(description="Reconcile the stores against each other.")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    parser.add_argument("--live", metavar="URL",
                        help="also diff against a running server's /api/snapshot")
    parser.add_argument("--no-write", action="store_true", help="do not touch data/quality.json")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    live = None
    if args.live:
        try:
            with urllib.request.urlopen(args.live, timeout=10) as response:
                live = json.load(response)
        except Exception as exc:
            print(f"live snapshot unavailable: {exc}", file=sys.stderr)
    report = run(live=live, write=not args.no_write)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(report["status"].upper(), json.dumps(report["asof"]))
        for check in report["checks"]:
            line = f"  {check['status']:<8} {check['id']:<20} {check['summary']}"
            if check.get("hint"):
                line += f"\n           → {check['hint']}"
            print(line)
    sys.exit({"ok": 0, "pending": 0, "warn": 1, "fail": 2}[report["status"]])
