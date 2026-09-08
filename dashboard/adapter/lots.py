"""Tax lots from the ledger: what each holding cost on the day it was bought.

The dashboard has always shown cost as IBKR reports it — the average cost per
share in the instrument's own currency — converted to pounds at today's rate.
That number answers "what would this cost to buy now", not "what did I pay".
On a book that is mostly dollars, Hong Kong dollars, won and yen, the gap
between the two is the currency's own move since purchase, and it was being
folded silently into "unrealised P&L".

This module replays the ledger into FIFO lots per contract, each lot carrying
the FX rate that applied on its trade date (`fx_to_base` from the Flex Trades
section, or the ConversionRates store for older rows). From the open lots,
`summary()` gives the quantity, the cost in the trade currency, and the cost
in pounds at trade-date rates — the figure derive.make_position pairs with
the spot-converted one, so the page can show both and the difference between
them, which is the FX P&L.

FIFO is IBKR's own default for the account, so the lots here match what the
broker's `fifoPnlRealized` is computed against. Corporate actions enter as
signed quantity rows the way the replay in reconcile.py takes them; a split
scales the open lots and keeps their cost. Nothing here reads the network.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import reconcile
import store

log = logging.getLogger("lots")


@dataclass
class Lot:
    con_id: int
    date: str
    quantity: float          # open quantity, in shares
    price: float             # per share, trade currency
    currency: str
    fx: float | None         # trade currency -> GBP on the trade date, when known
    exec_id: str = ""
    fees: float = 0.0        # commission and taxes on the open part, trade currency

    @property
    def cost_local(self) -> float:
        """What the open shares cost, fees in — IBKR's cost basis counts
        commission and stamp duty, and a lot that did not would show a fee
        as a currency move on a sterling line."""
        return self.quantity * self.price + self.fees

    @property
    def cost_gbp(self) -> float | None:
        return self.cost_local * self.fx if self.fx is not None else None


@dataclass
class Summary:
    con_id: int
    quantity: float
    cost_local: float
    cost_gbp_tradedate: float | None      # None when any open lot lacks an FX rate
    avg_fx: float | None
    currency: str
    lots: list[Lot] = field(default_factory=list)


def _fees(row: dict) -> float:
    """Commission plus taxes on a fill, as a positive amount in the trade
    currency. A commission charged in another currency is left out rather
    than guessed at a rate; it is pennies, and it would be wrong pennies."""
    total = 0.0
    commission = row.get("commission")
    if commission and (row.get("commission_currency") or row.get("currency")) == row.get("currency"):
        total += abs(float(commission))
    taxes = row.get("taxes")
    if taxes:
        total += abs(float(taxes))
    return total


def _fx_for(row: dict, fx_history: dict[tuple[str, str], float] | None) -> float | None:
    rate = row.get("fx_to_base")
    if rate:
        return float(rate)
    if fx_history:
        key = ((row.get("time") or "")[:10], row.get("currency") or "")
        if key in fx_history:
            return fx_history[key]
    if (row.get("currency") or "") == "GBP":
        return 1.0
    return None


def build_lots(tx_rows: list[dict], *, actions: list[dict] | tuple = (),
               fx_history: dict[tuple[str, str], float] | None = None) -> dict[int, list[Lot]]:
    """Open FIFO lots per con_id after replaying every fill and action.

    A sell consumes the oldest lots first. Selling more than is held (a short,
    or a missing buy) leaves a negative-quantity lot so the discrepancy is
    visible rather than silently clipped; reconcile.py names the cause.
    """
    events = []
    for row in tx_rows:
        if not reconcile.is_position_row(row):
            continue
        when = (row.get("time") or "")[:10]
        if not when:
            continue
        events.append((when, 0, row))
    for action in actions:
        when = (action.get("date") or "")[:10]
        if when:
            events.append((when, 1, {**action, "_action": True}))
    events.sort(key=lambda e: (e[0], e[1], str(e[2].get("exec_id") or "")))

    open_lots: dict[int, list[Lot]] = {}
    for when, _, row in events:
        try:
            con_id = int(row.get("con_id") or 0)
        except (TypeError, ValueError):
            continue
        if not con_id:
            continue
        lots = open_lots.setdefault(con_id, [])
        if row.get("_action"):
            qty = float(row.get("quantity") or 0.0)
            held = sum(l.quantity for l in lots)
            if row.get("kind") == "split" and held:
                # A split multiplies every open lot and leaves its cost alone.
                ratio = (held + qty) / held
                for lot in lots:
                    lot.quantity *= ratio
                    lot.price /= ratio
            elif qty > 0:
                lots.append(Lot(con_id, when, qty, float(row.get("price") or 0.0),
                                row.get("currency") or "", _fx_for(row, fx_history), "action"))
            elif qty < 0:
                _consume(lots, -qty, con_id, when)
            continue
        qty = abs(float(row.get("quantity") or 0.0))
        if not qty:
            continue
        side = str(row.get("side") or "").upper()
        if side.startswith("B"):
            lots.append(Lot(con_id, when, qty, float(row.get("price") or 0.0),
                            row.get("currency") or "", _fx_for(row, fx_history),
                            str(row.get("exec_id") or ""), _fees(row)))
        else:
            _consume(lots, qty, con_id, when)
    return open_lots


def _consume(lots: list[Lot], qty: float, con_id: int, when: str) -> None:
    remaining = qty
    while remaining > 1e-9 and lots:
        lot = lots[0]
        if lot.quantity <= remaining + 1e-9:
            remaining -= lot.quantity
            lots.pop(0)
        else:
            # The fees stay with the shares: a half-sold lot keeps half of them.
            lot.fees *= (lot.quantity - remaining) / lot.quantity
            lot.quantity -= remaining
            remaining = 0.0
    if remaining > 1e-9:
        # More sold than held: keep the shortfall visible as a negative lot.
        lots.append(Lot(con_id, when, -remaining, 0.0, "", None, "short"))


def summarise(open_lots: dict[int, list[Lot]]) -> dict[int, Summary]:
    out: dict[int, Summary] = {}
    for con_id, lots in open_lots.items():
        qty = sum(l.quantity for l in lots)
        if abs(qty) < 1e-9:
            continue
        cost_local = sum(l.cost_local for l in lots)
        gbp = [l.cost_gbp for l in lots]
        cost_gbp = sum(gbp) if all(g is not None for g in gbp) else None
        currency = next((l.currency for l in lots if l.currency), "")
        out[con_id] = Summary(
            con_id=con_id, quantity=qty, cost_local=cost_local,
            cost_gbp_tradedate=cost_gbp,
            avg_fx=(cost_gbp / cost_local) if cost_gbp is not None and cost_local else None,
            currency=currency, lots=list(lots),
        )
    return out


# ---------------------------------------------------------------- cached view

_cache: tuple[float, dict[int, Summary]] | None = None


def summaries(*, actions: list[dict] | tuple = ()) -> dict[int, Summary]:
    """The open-lot summary per con_id from the ledger on disk, cached on the
    ledger's mtime so the live feed's three-second recompose pays nothing."""
    global _cache
    try:
        stamp = store.TX_PATH.stat().st_mtime
    except OSError:
        return {}
    if _cache and _cache[0] == stamp:
        return _cache[1]
    fx_history = store.fx_history()
    all_actions = list(actions) + store.corporate_actions()
    result = summarise(build_lots(store._read(store.TX_PATH), actions=all_actions, fx_history=fx_history))
    _cache = (stamp, result)
    return result


def tradedate_cost(con_id: int, quantity: float, *, tolerance: float = 1e-6) -> float | None:
    """Sterling cost at trade-date rates for a held position, or None.

    None when the ledger's open quantity disagrees with the broker's — the
    replay in reconcile.py names why — or when any open lot lacks an FX rate.
    A cost that does not describe the shares actually held is worse than
    none, so it is withheld rather than scaled.
    """
    summary = summaries().get(int(con_id))
    if summary is None or abs(summary.quantity - float(quantity)) > tolerance:
        return None
    return summary.cost_gbp_tradedate


if __name__ == "__main__":
    for con_id, s in sorted(summaries().items(), key=lambda kv: -abs(kv[1].cost_local)):
        gbp = f"£{s.cost_gbp_tradedate:,.2f}" if s.cost_gbp_tradedate is not None else "(no FX on a lot)"
        print(f"{con_id:>10}  {s.quantity:>9g}  {s.cost_local:>12,.2f} {s.currency:<4} "
              f"{gbp:>14}  lots={len(s.lots)}")
