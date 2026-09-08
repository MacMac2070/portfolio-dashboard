"""What moved the account, and what it cost: the NAV flow and the monthly bars.

Three shapes, all composed here so the page draws finished figures and does no
financial arithmetic of its own:

  flow()     the two-stage Sankey behind the NAV Flow chart
  monthly()  dividend income, monthly P&L and itemised costs, per calendar month

Adapted from inspiration/premium-tracker-marfusios. That repo's `source/`
folder is not on disk — its README describes files that were never bundled —
so this follows the structure the README and the screenshots describe rather
than porting code.

  The two-stage flow
  ------------------
  Sources feed one Gross Value node, which then splits into Ending NAV and the
  cost stack. That separation is the idea worth taking: "how big did the pot
  get" and "what was skimmed off it" are different questions, and a flat set of
  flows answers neither cleanly.

  FX is its own labelled node rather than netted into mark-to-market, which is
  where the reference put it. With exposure concentrated in Hong Kong, China,
  Korea and Japan, currency is a large enough part of the answer that hiding it
  inside another number loses the plot.

  Monthly P&L
  -----------
  The reference toggles its income chart between Income and P/L. Its income
  series (options premium, stock-yield) do not apply here, but the P/L half
  does, so the toggle is built with dividends on one side and P&L on the other.

  P&L per month is mark-to-market plus realised plus the change in unrealised,
  read straight off that month's own ChangeInNAV sub-period. Deposits are
  excluded by construction rather than netted out afterwards, so a month whose
  NAV rose only because money was paid in reads as flat, which is correct.

  Dividends are deliberately *not* folded in: they are the other half of the
  toggle, and adding them here would show the same money twice.

  Signs
  -----
  Flex reports a decrease as a negative. A Sankey has no negative width, so
  every flow here is a magnitude and the direction is carried by which side of
  Gross Value the node sits on. Fields that can go either way — deposits
  against withdrawals, mark-to-market gains against losses, FX either way — are
  split into the appropriate side by sign rather than being netted, so a period
  with both shows both.
"""
from __future__ import annotations

import logging
from collections import defaultdict

from flex import MONTH_SPAN_DAYS

log = logging.getLogger("attribution")

GROSS = "Gross Value"
ENDING = "Ending NAV"

# What the cost stack draws, in stacking order, and what the tooltip itemises.
#
# Three bands, not five. Five hues that avoid green and red — both reserved for
# the P&L convention — do not separate: run through the dataviz palette
# validator, every five- and four-hue candidate failed either CVD separation or
# the normal-vision floor, the worst pair at ΔE 0.8 for deuteranopia. The
# three-hue set below passes every check on all pairs in both themes.
#
# Nothing is lost, because the itemisation moves to the tooltip — which is what
# the premium-tracker README calls the transferable part of this pattern in the
# first place ("the itemised hover tooltip with a bold total is the reusable
# interaction pattern, more than any single chart type"). Grouping fees with
# tax is also what that repo's own Sankey does, with a "Fees & Tax" node.
COST_BANDS = (
    ("commissions", "Commissions", ("commissions",)),
    ("fees_tax", "Fees & tax",
     ("broker_fees", "sales_tax", "transaction_tax", "withholding_tax", "other_fees")),
    ("interest_paid", "Interest paid", ("interest_paid",)),
)
# The underlying figures, for the hover detail.
COST_DETAIL = (
    ("commissions", "Commissions"),
    ("transaction_tax", "Transaction tax"),
    ("withholding_tax", "Withholding tax"),
    ("sales_tax", "Sales tax"),
    ("broker_fees", "Broker fees"),
    ("other_fees", "Other fees"),
    ("interest_paid", "Interest paid"),
)

# Where each cost figure is read from on a ChangeInNAV sub-period.
#
# Not from cash transactions, which is where this used to come from and why the
# chart was wrong: IBKR has no "Commissions" cash-transaction type at all —
# commission is charged inside the trade record — so that bucket was never
# populated and the card totalled £3.53 against £168.57 actually paid. Nor is
# there a Transaction tax type, which is the second largest cost here.
#
# ChangeInNAV carries every one of them as its own field, already per month,
# and is the same section the Sankey reads. Sourcing both from it means the two
# charts on this page can no longer disagree about what the costs were.
COST_FIELDS = (
    ("commissions", ("commissions",)),
    ("broker_fees", ("brokerFees", "advisorFees")),
    ("sales_tax", ("brokerFeesSalesTax",)),
    ("transaction_tax", ("transactionTax",)),
    ("withholding_tax", ("withholdingTax",)),
    ("interest_paid", ("interest", "brokerInterest", "bondInterest")),
    ("other_fees", ("otherFees",)),
)
INCOME_BUCKETS = (
    ("dividends", "Dividends"),
)


def _pos(v: float) -> float:
    return v if v > 0 else 0.0


def _neg(v: float) -> float:
    """The magnitude of a negative, or zero."""
    return -v if v < 0 else 0.0


def flow(change: dict | None) -> dict | None:
    """The NAV flow as nodes and links, ready to lay out.

    `change` is one parsed ChangeInNAV row. Returns None when there is none,
    which is the state until that section is enabled on the Flex query.
    """
    if not change:
        return None

    # Three separate figures, deliberately not merged.
    #
    # IBKR reports Change in NAV in one of two mutually exclusive modes, set on
    # the Flex query: *Mark-to-Market*, where `mtm` carries the whole
    # investment P&L and the other two are zero, or *Realized & Unrealized*,
    # where it is the other way round. They are two presentations of the same
    # money, never both at once — so listing all three as their own flows shows
    # whichever the query supplies, with the empty ones dropping out on the
    # `v > 0.005` filter below. No mode detection needed, and switching the
    # query's mode splits the node without another code change.
    mtm = change.get("mtm", 0.0)
    unrealized = change.get("changeInUnrealized", 0.0)
    realized = change.get("realized", 0.0)
    deposits = change.get("depositsWithdrawals", 0.0)
    fx = change.get("fxTranslation", 0.0)
    interest = change.get("interest", 0.0) + change.get("brokerInterest", 0.0) \
        + change.get("bondInterest", 0.0)
    # Dividends declared but not yet paid. endingValue counts them, so leaving
    # them out of the sources made the flow end short by exactly the accrual
    # and the drift line carried the difference every time.
    dividends = change.get("dividends", 0.0) + change.get("changeInDividendAccruals", 0.0)

    # The fields that used to have no node at all and so landed in `drift`:
    # a spin-off's cash, a position transferred in, a cost-basis adjustment.
    # Each is money the statement explains, and the chart should too.
    actions = change.get("corporateActionProceeds", 0.0)
    transfers = change.get("assetTransfers", 0.0) + change.get("internalCashTransfers", 0.0)
    adjustments = (change.get("costAdjustments", 0.0) + change.get("transferredPnlAdjustments", 0.0)
                   + change.get("cashSettlingMtm", 0.0) + change.get("realizedVm", 0.0)
                   + change.get("other", 0.0))

    # Into the pot.
    sources = [
        ("Starting value", change.get("startingValue", 0.0)),
        ("Deposits", _pos(deposits)),
        ("Mark-to-market", _pos(mtm)),
        ("Realised gains", _pos(realized)),
        ("Unrealised gains", _pos(unrealized)),
        ("Dividends", _pos(dividends)),
        ("Interest", _pos(interest)),
        ("FX translation", _pos(fx)),
        ("Corporate actions", _pos(actions)),
        ("Transfers in", _pos(transfers)),
        ("Adjustments", _pos(adjustments)),
    ]
    # Off the top. Fee fields arrive negative; take the magnitude.
    costs = [
        ("Mark-to-market losses", _neg(mtm)),
        ("Realised losses", _neg(realized)),
        ("Unrealised losses", _neg(unrealized)),
        ("Withdrawals", _neg(deposits)),
        ("Commissions", _neg(change.get("commissions", 0.0))),
        ("Broker fees", _neg(change.get("brokerFees", 0.0))
                        + _neg(change.get("advisorFees", 0.0))),
        ("Sales tax", _neg(change.get("brokerFeesSalesTax", 0.0))),
        # Stamp duty and exchange levies. Its own node rather than folded into
        # sales tax: with the exposure in Hong Kong and Singapore this is the
        # transaction cost that actually bites, and the two are different taxes.
        ("Transaction tax", _neg(change.get("transactionTax", 0.0))),
        ("Dividend accrual reversal", _neg(dividends)),
        ("Withholding tax", _neg(change.get("withholdingTax", 0.0))),
        ("Interest paid", _neg(interest)),
        ("Other fees", _neg(change.get("otherFees", 0.0))),
        ("FX translation loss", _neg(fx)),
        ("Corporate actions out", _neg(actions)),
        ("Transfers out", _neg(transfers)),
        ("Adjustments out", _neg(adjustments)),
    ]

    sources = [(name, round(v, 2)) for name, v in sources if v > 0.005]
    costs = [(name, round(v, 2)) for name, v in costs if v > 0.005]
    gross = round(sum(v for _, v in sources), 2)
    if gross <= 0:
        return None

    ending = change.get("endingValue", 0.0)
    # Ending NAV is what is left of the pot. Derive it from the flows rather
    # than trusting endingValue directly: if the two disagree the Sankey must
    # still balance, and the discrepancy is reported instead of hidden.
    derived_ending = round(gross - sum(v for _, v in costs), 2)
    drift = round(derived_ending - ending, 2)

    nodes = [{"id": name, "side": "source"} for name, _ in sources]
    nodes.append({"id": GROSS, "side": "middle"})
    nodes.append({"id": ENDING, "side": "sink"})
    nodes += [{"id": name, "side": "sink"} for name, _ in costs]

    links = [{"source": name, "target": GROSS, "value": v, "kind": "in"}
             for name, v in sources]
    links.append({"source": GROSS, "target": ENDING,
                  "value": max(derived_ending, 0.0), "kind": "out"})
    links += [{"source": GROSS, "target": name, "value": v, "kind": "cost"}
              for name, v in costs]

    return {
        "nodes": nodes,
        "links": links,
        "gross": gross,
        "ending": round(ending, 2),
        "ending_derived": derived_ending,
        # Non-zero means the reported ending value and the sum of the flows
        # disagree — usually a field this build does not yet map. The page
        # shows it rather than quietly drawing a chart that does not add up.
        "drift": drift,
        "from_date": change.get("from_date"),
        "to_date": change.get("to_date"),
        "currency": change.get("currency") or "GBP",
        "unmapped": change.get("unmapped") or [],
    }


def month_pnl(changes: list[dict] | None) -> dict[str, float]:
    """Month key -> P&L, from the ChangeInNAV sub-periods.

    Only periods narrow enough to be a single month are read; the widest row in
    the store is the whole-span summary the Sankey draws and would otherwise be
    counted again as if it were a month of its own.

    Returns {} when the query emits no sub-periods, which is what stands the
    P&L toggle down rather than drawing one bar for all of history.
    """
    out: dict[str, float] = {}
    for row in changes or []:
        span = row.get("span_days")
        if span is None or span > MONTH_SPAN_DAYS:
            continue
        month = (row.get("from_date") or "")[:7]
        if len(month) != 7:
            continue
        out[month] = out.get(month, 0.0) + round(
            row.get("mtm", 0.0) + row.get("realized", 0.0)
            + row.get("changeInUnrealized", 0.0), 2)
    return out


def monthly(cash: list[dict], changes: list[dict] | None = None) -> dict:
    """Dividend income, monthly P&L and itemised costs by month, base currency.

    Income is dividends only — the reference carries options premium and
    stock-yield income, neither of which applies here. The P/L half of its
    toggle does apply and is built from `changes`; see the module docstring.
    Costs keep the full itemisation, which is the part that transfers as-is.
    """
    inc = defaultdict(lambda: defaultdict(float))
    cost = defaultdict(lambda: defaultdict(float))
    months = set()

    for row in cash or []:
        month = (row.get("date") or "")[:7]
        if len(month) != 7:
            continue
        bucket = row.get("bucket")
        amount = row.get("amount_gbp", 0.0)
        if bucket in dict(INCOME_BUCKETS):
            if amount > 0:
                months.add(month)
                inc[month][bucket] += amount
        elif bucket == "interest_received" and amount > 0:
            # Cash interest is income, but not dividends. Kept separate so the
            # income chart can stay dividends-only while the figure is not lost.
            months.add(month)
            inc[month]["interest_received"] += amount

    # Costs come off the ChangeInNAV sub-periods, not the cash rows above —
    # see COST_FIELDS for why. Same rows the P&L bars read, so a month that has
    # one has the other.
    fx_by_month: dict[str, float] = {}
    for row in changes or []:
        if (row.get("span_days") or 0) > MONTH_SPAN_DAYS:
            continue                    # the whole-span summary, not a month
        month = (row.get("from_date") or "")[:7]
        if len(month) != 7:
            continue
        # Flex's own currency-translation line, monthly. For a book quoted in
        # six currencies this is the broker-audited answer to "how much of the
        # move was FX", so it ships as its own series rather than being
        # derivable only from the whole-span Sankey node.
        fx = row.get("fxTranslation", 0.0)
        if fx:
            fx_by_month[month] = fx_by_month.get(month, 0.0) + fx
        for key, fields in COST_FIELDS:
            # Flex reports a charge as a negative; the chart wants magnitudes.
            # Taking only the negative part also keeps interest *received* out
            # of "Interest paid" — the same field carries both directions.
            amount = sum(_neg(row.get(f, 0.0)) for f in fields)
            if amount:
                months.add(month)
                cost[month][key] += amount

    # One axis across income, P&L and costs: a month that carries any of the
    # three appears in all of them. Toggling Income to P&L must not reflow the
    # x-axis, and a month with real P&L but no dividends is a zero bar, not a
    # gap in the record.
    pnl_by_month = month_pnl(changes)
    months |= set(pnl_by_month)
    ordered = sorted(months)

    def series(store, buckets):
        return [
            {"key": key, "label": label,
             "values": [round(store[m].get(key, 0.0), 2) for m in ordered]}
            for key, label in buckets
        ]

    income_series = series(inc, INCOME_BUCKETS)
    if any(inc[m].get("interest_received") for m in ordered):
        income_series.append({
            "key": "interest_received", "label": "Interest",
            "values": [round(inc[m].get("interest_received", 0.0), 2) for m in ordered],
        })

    # Three drawn bands, each the sum of the detail rows it covers.
    # `detail_keys` is the band-to-detail mapping the tooltip needs. It travels
    # with the series so hiding a band can hide its detail rows without the
    # page re-deriving which figures sit under which band.
    cost_series = [
        {"key": key, "label": label, "detail_keys": list(parts),
         "values": [round(sum(cost[m].get(p, 0.0) for p in parts), 2) for m in ordered]}
        for key, label, parts in COST_BANDS
    ]
    # A band that is zero in every month is dropped rather than drawn flat.
    # It cannot be seen on the axis, so its legend entry is a control that
    # visibly does nothing when clicked — which reads as a broken toggle rather
    # than as "you have paid no interest". The tooltip's itemisation is where
    # a genuine zero still belongs.
    cost_series = [b for b in cost_series if any(b["values"])]

    # A single signed series, not a stack: P&L is one figure per month that can
    # sit either side of zero, which groupedBars already keeps in domain.
    pnl_series = [{
        "key": "pnl", "label": "P&L",
        "values": [round(pnl_by_month.get(m, 0.0), 2) for m in ordered],
    }] if pnl_by_month else []

    return {
        "months": ordered,
        "income": income_series,
        "pnl": pnl_series,
        # False when the Flex query emits no sub-periods. The page needs to
        # tell "no P&L yet" apart from "P&L of zero", and only the server knows
        # which it is.
        "pnl_available": bool(pnl_by_month),
        "pnl_total": round(sum(pnl_by_month.values()), 2) if pnl_by_month else 0.0,
        "costs": cost_series,
        # Every underlying figure, for the hover detail. Kept separate from the
        # drawn bands so the tooltip can itemise further than the stack does.
        "cost_detail": series(cost, COST_DETAIL),
        "income_total": round(sum(sum(v.values()) for v in inc.values()), 2),
        "cost_total": round(sum(sum(v.values()) for v in cost.values()), 2),
        "fx": [round(fx_by_month.get(m, 0.0), 2) for m in ordered],
        "fx_total": round(sum(fx_by_month.values()), 2),
    }
