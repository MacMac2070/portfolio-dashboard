"""attribution.py turns a Change in NAV row into a flow that must balance."""
import attribution


def change(**over):
    base = {"from_date": "2026-08-01", "to_date": "2026-08-28", "span_days": 27,
            "startingValue": 40000.0, "endingValue": 43000.0,
            "depositsWithdrawals": 2000.0, "mtm": 1300.0, "realized": 0.0, "changeInUnrealized": 0.0,
            "dividends": 100.0, "withholdingTax": -15.0, "commissions": -40.0, "interest": 5.0,
            "fxTranslation": -350.0, "corporateActionProceeds": 0.0, "unmapped": []}
    base.update(over)
    return base


def test_flow_balances_and_reports_drift():
    out = attribution.flow(change())
    assert out["ending"] == 43000.0
    assert abs(out["drift"]) < 0.01
    assert any(n["side"] == "sink" and n["id"] == "Ending NAV" for n in out["nodes"])
    assert all(link["value"] > 0 for link in out["links"])      # signs split, never netted
    costs = {l["target"] for l in out["links"] if l["kind"] == "cost"}
    assert costs and "Ending NAV" not in costs


def test_flow_drift_names_what_the_row_does_not_explain():
    out = attribution.flow(change(endingValue=43500.0))
    assert out["drift"] == -500.0        # derived − reported: £500 the flows cannot explain


def test_flow_none_without_a_row():
    assert attribution.flow(None) is None


def test_monthly_costs_come_from_nav_change_not_cash_and_only_negatives_count():
    cash = [{"date": "2026-08-05", "bucket": "dividends", "amount_gbp": 100.0},
            {"date": "2026-08-06", "bucket": "interest_received", "amount_gbp": 5.0}]
    out = attribution.monthly(cash, [change(commissions=-40.0, interest=5.0)])
    assert out["months"] == ["2026-08"]
    by_key = lambda series: {s["key"]: s["values"] for s in series}
    assert by_key(out["income"])["dividends"] == [100.0]
    assert by_key(out["cost_detail"])["commissions"] == [40.0]
    # Interest received is income, never a cost — only the negative part lands here.
    assert by_key(out["cost_detail"]).get("interest_paid", [0.0]) == [0.0]


def test_flow_routes_corporate_actions_and_transfers_out_of_drift():
    row = change(corporateActionProceeds=250.0, assetTransfers=-100.0, endingValue=43150.0)
    out = attribution.flow(row)
    sources = {l["source"] for l in out["links"] if l["kind"] == "in"}
    costs = {l["target"] for l in out["links"] if l["kind"] == "cost"}
    assert "Corporate actions" in sources and "Transfers out" in costs
    assert abs(out["drift"]) < 0.01
