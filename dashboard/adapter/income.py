"""Income: the dividend model behind the Performance rail and the stock panel.

Three tiers, in descending honesty, and the UI never blurs them:

  paid       — Flex cash transactions. Broker fact, GBP at payment-date FX.
  declared   — Flex Change-in-NAV dividend accruals: declared but not yet
               paid. Broker fact, aggregate only (Flex does not attribute
               accruals per holding), so it is a headline figure, not a bar.
  estimated  — projected from each holding's own payment history (cadence and
               last amount) via desk.json. Everything in this tier is marked
               "est." by the UI; the model's job is to be *plainly* a model.

Projections are composed at request time against the live feed's share counts
and FX, so the forward £ figures move with the day rather than being frozen at
whatever the rates were when desk.json was written. With the feed down, the
per-share schedule still renders and the £ column stands down honestly.
"""
from __future__ import annotations

import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from flex import MONTH_SPAN_DAYS
from store import CASH_PATH, NAV_CHANGE_PATH, _read
import universe

# Payment-gap medians map to a cadence; anything slower than ~13 months means
# the history is too irregular to project and the holding is left out of the
# forward rail rather than given an invented schedule.
CADENCES = (("monthly", 45), ("quarterly", 135), ("semiannual", 270), ("annual", 400))


def _cadence(gaps: list[int]) -> tuple[str, int] | None:
    if not gaps:
        return None
    med = statistics.median(gaps)
    for name, ceiling in CADENCES:
        if med <= ceiling:
            return name, round(med)
    return None


def _holding_model(block: dict, today: date) -> dict:
    """Per-holding history stats + a 12-month forward schedule (per share)."""
    divs = [d for d in (block.get("dividends") or []) if d.get("date") and d.get("amount")]
    divs.sort(key=lambda d: d["date"])

    annual: dict[int, float] = {}
    for d in divs:
        annual[int(d["date"][:4])] = round(
            annual.get(int(d["date"][:4]), 0.0) + d["amount"], 6)

    ttm = round(sum(d["amount"] for d in divs
                    if d["date"] > (today - timedelta(days=365)).isoformat()), 6)

    # Streak of consecutive raised (or held) full calendar years, most recent
    # backwards; the current partial year is skipped — it cannot be judged yet.
    years = sorted(y for y in annual if y < today.year)
    streak = 0
    for later, earlier in zip(years[::-1], years[::-1][1:]):
        if later == earlier + 1 and annual[later] >= annual[earlier]:
            streak += 1
        else:
            break

    cagr5 = None
    if len(years) >= 6 and annual[years[-6]] > 0:
        cagr5 = round((annual[years[-1]] / annual[years[-6]]) ** 0.2 - 1.0, 4)

    # ---- forward schedule ----
    recent = divs[-8:]
    gaps = [(date.fromisoformat(b["date"]) - date.fromisoformat(a["date"])).days
            for a, b in zip(recent, recent[1:])]
    cadence = _cadence(gaps)
    schedule: list[dict] = []
    if cadence and recent:
        name, step = cadence
        amount = recent[-1]["amount"]        # last payment, the honest naive estimate
        cursor = date.fromisoformat(recent[-1]["date"])
        # A broker-declared ex-date beats the projection for the first event.
        declared_ex = block.get("next_ex_div")
        if declared_ex and declared_ex > today.isoformat():
            schedule.append({"date": declared_ex, "amount": amount, "tier": "declared_date"})
            cursor = date.fromisoformat(declared_ex)
        horizon = today + timedelta(days=365)
        while True:
            cursor = cursor + timedelta(days=step)
            if cursor > horizon:
                break
            if cursor.isoformat() <= today.isoformat():
                continue
            schedule.append({"date": cursor.isoformat(), "amount": amount, "tier": "estimated"})

    return {
        "currency": block.get("currency") or "",
        "annual": [{"year": y, "dps": annual[y]} for y in sorted(annual)],
        "ttm_dps": ttm,
        "cadence": cadence[0] if cadence else None,
        "streak_years": streak,
        "dps_cagr5": cagr5,
        "schedule": schedule,
    }


def build(stored: dict, live: dict | None) -> dict:
    """The income payload: per-holding models + the composed forward rail."""
    today = date.today()
    holdings = stored.get("holdings") or {}

    # Map positions by raw symbol and canonical universe key so venue variants
    # (such as HSBAl on LSE) match their holding model key (HSBA).
    positions: dict[str, dict] = {}
    for p in (live or {}).get("positions") or []:
        sym = p.get("symbol")
        if sym:
            positions[sym] = p
        canon = universe.key_for(p.get("con_id"), sym or "")
        if canon:
            positions[canon] = p

    fx = (live or {}).get("fx") or {}
    ready_gbp = bool(positions and fx)

    models: dict[str, dict] = {}
    events: list[dict] = []
    for key, block in holdings.items():
        model = _holding_model(block, today)
        pos = positions.get(key)
        qty = pos.get("quantity") if pos else None
        rate = fx.get(model["currency"]) if model["currency"] else None
        if model["currency"] == "GBP":
            rate = 1.0

        # Yield on cost: trailing DPS against the position's average cost.
        if pos and qty and rate and pos.get("cost_gbp"):
            model["yield_on_cost"] = round(
                (model["ttm_dps"] * rate) / (pos["cost_gbp"] / qty), 4)
        else:
            model["yield_on_cost"] = None

        for ev in model["schedule"]:
            gbp = round(ev["amount"] * qty * rate, 2) if (qty and rate) else None
            events.append({"key": key, "date": ev["date"], "tier": ev["tier"],
                           "amount": ev["amount"], "currency": model["currency"],
                           "gbp": gbp})
        models[key] = model

    events.sort(key=lambda e: e["date"])

    # ---- the rail's months: trailing 12 confirmed + forward 12 estimated ----
    months: dict[str, dict] = {}
    def month_key(iso: str) -> str: return iso[:7]
    start = (today.replace(day=1) - timedelta(days=365)).replace(day=1)
    cursor = start
    horizon = today + timedelta(days=365)
    while cursor <= horizon:
        months[cursor.isoformat()[:7]] = {"confirmed": 0.0, "estimated": 0.0}
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)

    for row in _read(CASH_PATH):
        if row.get("bucket") == "dividends" and (row.get("amount_gbp") or 0) > 0:
            m = month_key(row.get("date") or "")
            if m in months:
                months[m]["confirmed"] = round(
                    months[m]["confirmed"] + row["amount_gbp"], 2)

    for ev in events:
        if ev["gbp"]:
            m = month_key(ev["date"])
            if m in months:
                months[m]["estimated"] = round(months[m]["estimated"] + ev["gbp"], 2)

    # ---- declared-but-unpaid headline from the latest monthly accrual row ----
    declared = 0.0
    monthly_rows = [r for r in _read(NAV_CHANGE_PATH)
                    if (r.get("span_days") or 0) <= MONTH_SPAN_DAYS]
    if monthly_rows:
        latest = max(monthly_rows, key=lambda r: r.get("to_date") or "")
        declared = round(max(latest.get("changeInDividendAccruals", 0.0), 0.0), 2)

    forward_total = round(sum(e["gbp"] or 0 for e in events), 2)
    return {
        "ready_gbp": ready_gbp,
        "months": [{"month": m, **v} for m, v in sorted(months.items())],
        "next_events": events[:10],
        "forward_12m_gbp": forward_total if ready_gbp else None,
        "declared_gbp": declared,
        "holdings": models,
    }


if __name__ == "__main__":
    import json
    import desk
    stored = desk.read() or {}
    out = build(stored, None)
    print("holdings modelled:", len(out["holdings"]),
          "| declared £:", out["declared_gbp"])
    for e in out["next_events"][:6]:
        print(" ", e["date"], e["key"], e["amount"], e["currency"], e["tier"])
    payers = {k: v["cadence"] for k, v in out["holdings"].items() if v["cadence"]}
    print("cadences:", json.dumps(payers))
