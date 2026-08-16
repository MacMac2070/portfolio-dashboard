"""Compose data/portfolio.json from IB Gateway + openbb.

Run it directly; it needs no arguments and no Claude session:

    /opt/anaconda3/bin/python3 adapter/build.py

Percentage conventions are taken from the reference design and are not
arbitrary — each was reverse-engineered from the figures printed on it:

    unrealised %  return on cost      1554 / (45147 - 1554) = 3.57%
    daily %       against prior NAV   -925 / (50332 + 925)  = -1.80%
    weights       share of *invested*, not of NAV
    cash weight   share of NAV

Currency exposure groups by listing currency and covers stock only: the
reference's GBP row is HSBA + XDJP, and excludes the GBP cash balance.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import derive
import ibkr
import marketdata

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_PATH = DATA_DIR / "portfolio.json"
NAV_HISTORY = DATA_DIR / "nav_history.jsonl"

log = logging.getLogger("build")

# The portfolio maths lives in derive.py so this path and the live feed cannot
# drift apart. _pct is kept as an alias for the local prints below.
_pct = derive.pct


def build() -> dict:
    snapshot = ibkr.fetch(include_fills=True)
    account = snapshot.account

    con_ids = [p.con_id for p in snapshot.positions]
    fx, fx_source = marketdata.fx_rates(account.exchange_rates)
    quotes = marketdata.prior_closes(con_ids)
    sparks = marketdata.price_series(con_ids)

    to_gbp = derive.converter(fx)

    positions = []
    for raw in snapshot.positions:
        quote = quotes.get(raw.con_id, {})
        change_pct, change_source = marketdata.day_change_pct(
            prev_close=quote.get("prev"),
            last_price=quote.get("last"),
            ibkr_price=raw.market_price,
            ibkr_daily_pnl=raw.daily_pnl,
            ibkr_market_value=raw.market_value,
        )
        positions.append(derive.make_position(
            con_id=raw.con_id,
            symbol=raw.symbol,
            currency=raw.currency,
            exchange=raw.exchange,
            quantity=raw.quantity,
            price=raw.market_price,
            market_value=raw.market_value,
            average_cost=raw.average_cost,
            unrealized_pnl=raw.unrealized_pnl,
            day_change_pct=change_pct,
            day_change_source=change_source,
            spark=sparks.get(raw.con_id, []),
            to_gbp=to_gbp,
        ))

    agg = derive.aggregate(
        positions,
        nav=account.net_liquidation,
        cash=account.total_cash,
        account_daily_pnl=account.daily_pnl,
    )

    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "fetched_at": snapshot.fetched_at,
            "account_id": account.account_id,
            "base_currency": account.base_currency,
            "fx_source": fx_source,
            "daily_pnl_source": agg["daily_pnl_source"],
            "gateway": "ok",
        },
        "kpis": agg["kpis"],
        "positions": positions,
        "regions": agg["regions"],
        "currencies": agg["currencies"],
        "concentration": agg["concentration"],
        "movers": agg["movers"],
        "fx": fx,
        "fills": snapshot.fills,
    }


def write_stale_marker(reason: str) -> None:
    """Gateway down: mark the existing snapshot stale, keep the values."""
    if not OUT_PATH.exists():
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps({
            "meta": {"gateway": "unavailable", "error": reason,
                     "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
            "kpis": {}, "positions": [], "regions": [], "currencies": [],
            "concentration": {}, "movers": {"gainers": [], "losers": []},
        }, indent=2))
        return
    payload = json.loads(OUT_PATH.read_text())
    payload.setdefault("meta", {})
    payload["meta"]["gateway"] = "unavailable"
    payload["meta"]["error"] = reason
    payload["meta"]["stale_since"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    OUT_PATH.write_text(json.dumps(payload, indent=2))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        payload = build()
    except ibkr.GatewayUnavailable as exc:
        log.error("%s", exc)
        write_stale_marker(str(exc))
        return 1

    OUT_PATH.write_text(json.dumps(payload, indent=2))

    kpis, conc = payload["kpis"], payload["concentration"]
    print(f"\nwrote {OUT_PATH.relative_to(OUT_PATH.parent.parent.parent)}")
    print(f"  net liquidation   GBP {kpis['net_liquidation']:>12,.2f}")
    print(f"  invested          GBP {kpis['invested']:>12,.2f}")
    print(f"  cash available    GBP {kpis['cash_available']:>12,.2f}")
    print(f"  daily P/L         GBP {kpis['daily_pnl']:>12,.2f}  "
          f"({kpis['daily_pnl_pct']:+.2f}%)  via {payload['meta']['daily_pnl_source']}")
    print(f"  unrealised P/L    GBP {kpis['unrealised_pnl']:>12,.2f}  "
          f"({kpis['unrealised_pnl_pct']:+.2f}%)")
    print(f"  positions {conc['positions']}   markets {conc['markets']}   "
          f"fx via {payload['meta']['fx_source']}")

    region_total = sum(r["value_gbp"] for r in payload["regions"])
    currency_total = sum(c["value_gbp"] for c in payload["currencies"])
    print(f"\n  regions sum   GBP {region_total:>12,.2f}   "
          f"delta {region_total - kpis['invested']:+.2f}")
    print(f"  currencies sum GBP {currency_total:>12,.2f}   "
          f"delta {currency_total - kpis['invested']:+.2f}")

    print("\n  regions:")
    for r in payload["regions"]:
        print(f"    {r['name']:<20} {r['weight_pct']:>5.1f}%  GBP {r['value_gbp']:>10,.2f}")
    print("  currencies:")
    for c in payload["currencies"]:
        print(f"    {c['code']:<20} {c['weight_pct']:>5.1f}%  GBP {c['value_gbp']:>10,.2f}")
    print("  movers:")
    for p in payload["movers"]["gainers"]:
        print(f"    + {p['symbol']:<6} {p['day_change_pct']:>7.2f}%  ({p['day_change_source']})")
    for p in payload["movers"]["losers"]:
        print(f"    - {p['symbol']:<6} {p['day_change_pct']:>7.2f}%  ({p['day_change_source']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
