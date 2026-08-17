"""What moved the account, and what it cost: the NAV flow and the monthly bars.

Three shapes, all composed here so the page draws finished figures and does no
financial arithmetic of its own:

  flow()     the two-stage Sankey behind the NAV Flow chart
  monthly()  dividend income and itemised costs, per calendar month

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
    ("fees_tax", "Fees & tax", ("other_fees", "sales_tax", "withholding_tax")),
    ("interest_paid", "Interest paid", ("interest_paid",)),
)
# The five underlying figures, for the hover detail.
COST_DETAIL = (
    ("commissions", "Commissions"),
    ("other_fees", "Other fees"),
    ("sales_tax", "Sales tax"),
    ("withholding_tax", "Withholding tax"),
    ("interest_paid", "Interest paid"),
)
COST_BUCKETS = COST_DETAIL          # what parse_cash_transactions may emit
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

    mtm = change.get("mtm", 0.0) + change.get("changeInUnrealized", 0.0)
    realized = change.get("realized", 0.0)
    deposits = change.get("depositsWithdrawals", 0.0)
    fx = change.get("fxTranslation", 0.0)
    interest = change.get("interest", 0.0) + change.get("brokerInterest", 0.0) \
        + change.get("bondInterest", 0.0)

    # Into the pot.
    sources = [
        ("Starting value", change.get("startingValue", 0.0)),
        ("Deposits", _pos(deposits)),
        ("Mark-to-market", _pos(mtm) + _pos(realized)),
        ("Dividends", change.get("dividends", 0.0)),
        ("Interest", _pos(interest)),
        ("FX translation", _pos(fx)),
    ]
    # Off the top. Fee fields arrive negative; take the magnitude.
    costs = [
        ("Mark-to-market losses", _neg(mtm) + _neg(realized)),
        ("Withdrawals", _neg(deposits)),
        ("Commissions", _neg(change.get("commissions", 0.0))),
        ("Broker fees", _neg(change.get("brokerFees", 0.0))
                        + _neg(change.get("advisorFees", 0.0))),
        ("Sales tax", _neg(change.get("brokerFeesSalesTax", 0.0))),
        ("Withholding tax", _neg(change.get("withholdingTax", 0.0))),
        ("Interest paid", _neg(interest)),
        ("Other fees", _neg(change.get("otherFees", 0.0))),
        ("FX translation loss", _neg(fx)),
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


def monthly(cash: list[dict]) -> dict:
    """Dividend income and itemised costs by calendar month, base currency.

    Income is dividends only — the reference toggles between income and P/L and
    carries options premium and stock-yield income, none of which apply here.
    Costs keep the full itemisation, which is the part that does transfer.
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
        elif bucket in dict(COST_BUCKETS):
            # Costs arrive negative; charts want magnitudes.
            if amount < 0:
                months.add(month)
                cost[month][bucket] += -amount
        elif bucket == "interest_received" and amount > 0:
            # Cash interest is income, but not dividends. Kept separate so the
            # income chart can stay dividends-only while the figure is not lost.
            months.add(month)
            inc[month]["interest_received"] += amount

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
    cost_series = [
        {"key": key, "label": label,
         "values": [round(sum(cost[m].get(p, 0.0) for p in parts), 2) for m in ordered]}
        for key, label, parts in COST_BANDS
    ]

    return {
        "months": ordered,
        "income": income_series,
        "costs": cost_series,
        # Every underlying figure, for the hover detail. Kept separate from the
        # drawn bands so the tooltip can itemise further than the stack does.
        "cost_detail": series(cost, COST_DETAIL),
        "income_total": round(sum(sum(v.values()) for v in inc.values()), 2),
        "cost_total": round(sum(sum(v.values()) for v in cost.values()), 2),
    }
