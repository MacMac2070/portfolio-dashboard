"""Portfolio maths shared by the snapshot builder and the live feed.

Two code paths produce the same payload shape: `build.py` (connect, fetch,
disconnect, write portfolio.json) and `feed.py` (one connection held open,
recomposed every few seconds). They must agree exactly — if "invested" or
"cash weight" were implemented twice, the page would show one number on load
and a different one three seconds later.

Everything here is pure: give it positions and rates, get a payload back. It
opens no connections and reads no files, which is also what makes it testable
without a broker.

Percentage conventions come from the reference design and are not arbitrary;
each was reverse-engineered from the figures printed on it:

    unrealised %  return on cost      1554 / (45147 - 1554) = 3.57%
    daily %       against prior NAV   -925 / (50332 + 925)  = -1.80%
    weights       share of *invested*, not of NAV
    cash weight   share of NAV
"""
from __future__ import annotations

import logging

import regions as regions_mod
import universe as universe_mod

# Fixed display and colour order for the currency split, for the same reason
# REGION_ORDER exists: a currency must keep its hue when another one drops out
# of the portfolio. Ordered by how much of this account each has historically
# carried, then the rest alphabetically.
CURRENCY_ORDER = ("GBP", "USD", "HKD", "JPY", "SGD", "EUR", "KRW", "CHF", "CNY")


def _currency_sort_key(code: str) -> int:
    try:
        return CURRENCY_ORDER.index(code)
    except ValueError:
        return 6

log = logging.getLogger(__name__)


def pct(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or not denominator:
        return None
    return numerator / denominator * 100.0


class MissingRate(LookupError):
    """No rate into GBP for a currency the book holds.

    Raised rather than assumed: the old converter treated an unknown currency
    as sterling with a log line nobody read, which would have valued a won
    position at a thousand times its worth. A caller keeps the row, marks it
    `fx_missing`, and the health chip says so.
    """


def converter(fx: dict[str, float]):
    """Build an amount -> GBP converter from a {currency: rate} table."""
    def to_gbp(amount: float, currency: str) -> float:
        rate = fx.get(currency)
        if rate is None:
            raise MissingRate(currency)
        return amount * rate
    return to_gbp


def unpriced_position(*, con_id: int, symbol: str, currency: str, exchange: str,
                      quantity: float, price: float, spark: list[float] | None = None) -> dict:
    """The row a caller keeps for a position it could not convert. Every
    money field is None, so nothing downstream mistakes it for a value; the
    UI prints dashes and the reason."""
    name, region = regions_mod.lookup(con_id, symbol)
    return {
        "con_id": con_id, "symbol": symbol, "name": name, "region": region,
        "sector": universe_mod.sector_for(con_id, symbol),
        "currency": currency, "exchange": exchange,
        "quantity": quantity, "price": price,
        "value_gbp": None, "cost_gbp": None, "unrealised_gbp": None, "unrealised_pct": None,
        "cost_gbp_tradedate": None, "unrealised_gbp_tradedate": None, "fx_pnl_gbp": None,
        "day_change_pct": None, "day_pnl_gbp": None, "day_change_source": "none",
        "spark": spark or [], "fx_missing": True,
    }


def make_position(*, con_id: int, symbol: str, currency: str, exchange: str,
                  quantity: float, price: float, market_value: float,
                  average_cost: float, unrealized_pnl: float,
                  day_change_pct: float | None, day_change_source: str,
                  spark: list[float], to_gbp,
                  cost_gbp_tradedate: float | None = None) -> dict:
    """One position row, with everything converted to GBP.

    Two cost conventions ride every row. `cost_gbp` is the broker's cost in
    the instrument's currency converted at today's rate — what the position
    would cost to buy now — and `unrealised_gbp` is the market return on it.
    `cost_gbp_tradedate`, when the ledger can supply it, is what was actually
    paid in sterling on the trade dates; `unrealised_gbp_tradedate` is the
    return against that, and `fx_pnl_gbp` is the difference between the two —
    the currency's own move since purchase, which the first convention hides
    inside "unrealised".
    """
    name, region = regions_mod.lookup(con_id, symbol)
    # Carried per position, not only rolled up, so the Allocation view can
    # group on any of the three axes with the same code and show which slice a
    # holding sits in. Region answers "where", sector answers "what kind of
    # business" — see the sector rollup in aggregate().
    sector = universe_mod.sector_for(con_id, symbol)

    value_gbp = to_gbp(market_value, currency)
    cost_gbp = to_gbp(average_cost * quantity, currency)
    unrealised_gbp = to_gbp(unrealized_pnl, currency)

    # Day P&L in GBP is derived from the percentage rather than taken from
    # IBKR, so the money figure always agrees with the percentage printed
    # beside it. A day at (or numerically past) -100% has no recoverable
    # opening value — dividing by ~zero was a crash, so it stays None.
    day_pnl_gbp = None
    if day_change_pct is not None and (1.0 + day_change_pct / 100.0) > 1e-9:
        opening = value_gbp / (1.0 + day_change_pct / 100.0)
        day_pnl_gbp = value_gbp - opening

    unrealised_tradedate = fx_pnl = None
    if cost_gbp_tradedate is not None:
        unrealised_tradedate = value_gbp - cost_gbp_tradedate
        fx_pnl = unrealised_tradedate - unrealised_gbp

    return {
        "con_id": con_id,
        "symbol": symbol,
        "name": name,
        "region": region,
        "sector": sector,
        "currency": currency,
        "exchange": exchange,
        "quantity": quantity,
        "price": price,
        "value_gbp": value_gbp,
        "cost_gbp": cost_gbp,
        "unrealised_gbp": unrealised_gbp,
        "unrealised_pct": pct(unrealised_gbp, cost_gbp),
        "cost_gbp_tradedate": cost_gbp_tradedate,
        "unrealised_gbp_tradedate": unrealised_tradedate,
        "unrealised_pct_tradedate": pct(unrealised_tradedate, cost_gbp_tradedate),
        "fx_pnl_gbp": fx_pnl,
        "day_change_pct": day_change_pct,
        "day_pnl_gbp": day_pnl_gbp,
        "day_change_source": day_change_source,
        "spark": spark,
    }


def daily_pnl(positions: list[dict], account_daily_pnl: float | None) -> tuple[float, str]:
    """Account day P&L, preferring the sum of positions over IBKR's own figure.

    That looks backwards — the broker's number ought to win — but IBKR's
    account-level dailyPnL is only the sum of the positions its market-data
    farms managed to value, and it reports that partial total with no
    indication that it is partial. Measured 26 Jul 2026: IBKR said -552.47,
    exactly HSBA + HY9H + IUCS + SMSN + XDJP converted to GBP; the other ten
    returned warning 2150 or never streamed. Summing all fifteen gives -935.60.

    So: use the complete set when there is one, and fall back to IBKR's partial
    figure only when we could not price everything ourselves.
    """
    summed = sum(p["day_pnl_gbp"] for p in positions if p["day_pnl_gbp"] is not None)
    priced = [p for p in positions if p["day_pnl_gbp"] is not None]

    if positions and len(priced) == len(positions):
        return summed, "positions-complete"
    if account_daily_pnl is not None:
        return account_daily_pnl, "ibkr-account-partial"
    return summed, f"positions-partial-{len(priced)}-of-{len(positions)}"


def aggregate(positions: list[dict], *, nav: float, cash: float,
              account_daily_pnl: float | None) -> dict:
    """KPIs, region/sector/currency splits, concentration and movers.

    A row with no sterling value (`fx_missing`) is left out of every sum and
    counted in `kpis.unpriced`, so a missing rate shows as a gap rather than
    as a wrong total.
    """
    unpriced = [p for p in positions if p.get("value_gbp") is None]
    positions = [p for p in positions if p.get("value_gbp") is not None]
    invested = sum(p["value_gbp"] for p in positions)
    unrealised = sum(p["unrealised_gbp"] for p in positions)
    cost_basis = invested - unrealised
    day_pnl, day_source = daily_pnl(positions, account_daily_pnl)
    # The trade-date convention only totals when every row can supply it;
    # a partial sum would compare a full value against a partial cost.
    tradedate_rows = [p for p in positions if p.get("cost_gbp_tradedate") is not None]
    if positions and len(tradedate_rows) == len(positions):
        cost_tradedate = sum(p["cost_gbp_tradedate"] for p in positions)
        unrealised_tradedate = invested - cost_tradedate
        fx_pnl = unrealised_tradedate - unrealised
    else:
        cost_tradedate = unrealised_tradedate = fx_pnl = None

    by_region: dict[str, float] = {}
    for p in positions:
        by_region[p["region"]] = by_region.get(p["region"], 0.0) + p["value_gbp"]
    region_rows = [
        {
            "name": name,
            "value_gbp": value,
            "weight_pct": pct(value, invested),
            "color_index": regions_mod.sort_key(name),
        }
        for name, value in sorted(
            by_region.items(), key=lambda kv: (-kv[1], regions_mod.sort_key(kv[0])))
    ]

    by_currency: dict[str, float] = {}
    for p in positions:
        by_currency[p["currency"]] = by_currency.get(p["currency"], 0.0) + p["value_gbp"]
    currency_rows = [
        {"code": code, "value_gbp": value, "weight_pct": pct(value, invested),
         "color_index": _currency_sort_key(code)}
        for code, value in sorted(by_currency.items(), key=lambda kv: -kv[1])
    ]

    # Sector is a separate axis from region: region answers "where is this
    # exposure", sector answers "what kind of business is this". XDJP is Japan
    # by region and an ETF by sector and both are correct, which is why this
    # rolls up independently rather than deriving one from the other.
    by_sector: dict[str, float] = {}
    for p in positions:
        name = universe_mod.sector_for(p.get("con_id"), p["symbol"])
        by_sector[name] = by_sector.get(name, 0.0) + p["value_gbp"]
    sector_rows = [
        {
            "name": name,
            "value_gbp": value,
            "weight_pct": pct(value, invested),
            "color_index": universe_mod.sector_sort_key(name),
        }
        for name, value in sorted(
            by_sector.items(),
            key=lambda kv: (-kv[1], universe_mod.sector_sort_key(kv[0])))
    ]

    ranked = sorted(positions, key=lambda p: -p["value_gbp"])
    largest = ranked[0] if ranked else None
    movable = [p for p in positions if p["day_change_pct"] is not None]
    movable.sort(key=lambda p: -p["day_change_pct"])

    return {
        "daily_pnl_source": day_source,
        "kpis": {
            "net_liquidation": nav,
            "daily_pnl": day_pnl,
            "daily_pnl_pct": pct(day_pnl, nav - day_pnl),
            "unrealised_pnl": unrealised,
            "unrealised_pnl_pct": pct(unrealised, cost_basis),
            "unrealised_pnl_tradedate": unrealised_tradedate,
            "unrealised_pnl_pct_tradedate": pct(unrealised_tradedate, cost_tradedate),
            "fx_pnl": fx_pnl,
            "invested": invested,
            "cash_available": cash,
            "unpriced": len(unpriced),
        },
        "regions": region_rows,
        "sectors": sector_rows,
        "currencies": currency_rows,
        "concentration": {
            "largest_symbol": largest["symbol"] if largest else None,
            "largest_weight_pct": pct(largest["value_gbp"], invested) if largest else None,
            "top3_weight_pct": pct(sum(p["value_gbp"] for p in ranked[:3]), invested),
            "positions": len(positions) + len(unpriced),
            "markets": len({p["region"] for p in positions}),
            "cash_weight_pct": pct(cash, nav),
        },
        "movers": {
            "gainers": [p for p in movable if p["day_change_pct"] > 0][:3],
            "losers": [p for p in reversed(movable) if p["day_change_pct"] < 0][:3],
        },
    }
