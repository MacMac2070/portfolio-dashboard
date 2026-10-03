"""Scenario test suite for the Flex EOD repricing path in flexfeed.py."""
import sys
from pathlib import Path

import pytest

# Ensure adapter modules and tests directory can be imported
adapter_dir = str(Path(__file__).resolve().parent.parent / "adapter")
tests_dir = str(Path(__file__).resolve().parent)
for p in (adapter_dir, tests_dir):
    if p not in sys.path:
        sys.path.insert(0, p)

import flexfeed
import marketdata
import store
import scenarios


def compose(**kw):
    quotes = kw.pop("quotes", {})
    fx = kw.pop("fx", {"GBP": 1.0, "USD": 0.75})
    sparks = kw.pop("sparks", {})
    return flexfeed.compose_payload(quotes=quotes, fx=fx, sparks=sparks, **kw)


def assert_scenario_invariants(payload, eod_rows, quotes, base_fx, intraday_fx, fx, skipped, base_copy):
    # Invariant: overlay: applied currencies equal intraday keys minus skipped, base unchanged
    assert base_fx == base_copy, "Invariant: base unchanged (not mutated)"
    applied = set(intraday_fx.keys()) - set(skipped)
    for c in applied:
        assert fx[c] == intraday_fx[c], f"Invariant: applied currency {c} rate matches intraday rate"
    for c in base_fx:
        if c not in applied:
            assert fx[c] == base_fx[c], f"Invariant: non-applied currency {c} keeps base rate"
    assert set(fx.keys()) == set(base_fx.keys()), "Invariant: currencies absent from base are never added"

    # Invariant: nothing raises (payload composed successfully)
    assert payload is not None, "Invariant: compose_payload returned None"

    # Invariant: kpis.net_liquidation == cash + sum(value_gbp for priced rows) within 1e-6
    cash = payload["kpis"]["cash_available"]
    priced_rows = [p for p in payload["positions"] if p.get("value_gbp") is not None]
    sum_priced = sum(p["value_gbp"] for p in priced_rows)
    assert abs(payload["kpis"]["net_liquidation"] - (cash + sum_priced)) < 1e-6, (
        f"Invariant: kpis.net_liquidation ({payload['kpis']['net_liquidation']}) == "
        f"cash ({cash}) + sum(value_gbp) ({sum_priced})"
    )

    # Invariant: for every row with price_source "statement", value_gbp == mark*quantity*fx within 1e-6
    for p in payload["positions"]:
        if p.get("price_source") == "statement":
            mark = p["price"]
            qty = p["quantity"]
            rate = payload["fx"][p["currency"]]
            expected = mark * qty * rate
            assert abs(p["value_gbp"] - expected) < 1e-6, (
                f"Invariant: statement row {p['symbol']} value_gbp ({p['value_gbp']}) == "
                f"mark*quantity*fx ({expected})"
            )

    # Invariant: for every "yahoo" row, value_gbp == last*quantity*fx
    for p in payload["positions"]:
        if p.get("price_source") == "yahoo":
            quote = quotes.get(p["con_id"], {})
            last = quote.get("last")
            assert last is not None, f"Invariant: yahoo row {p['symbol']} must have quote last price"
            qty = p["quantity"]
            rate = payload["fx"][p["currency"]]
            expected = last * qty * rate
            assert abs(p["value_gbp"] - expected) < 1e-6, (
                f"Invariant: yahoo row {p['symbol']} value_gbp ({p['value_gbp']}) == "
                f"last*quantity*fx ({expected})"
            )

    # Invariant: len(rejected_units)+len(unquoted)+quoted == number of priced rows
    repricing = payload["meta"]["repricing"]
    quoted = repricing["quoted"]
    rejected_units = repricing["rejected_units"]
    unquoted = repricing["unquoted"]
    assert len(rejected_units) + len(unquoted) + quoted == len(priced_rows), (
        f"Invariant: len(rejected_units) ({len(rejected_units)}) + len(unquoted) ({len(unquoted)}) + quoted ({quoted}) == "
        f"number of priced rows ({len(priced_rows)})"
    )

    # Invariant: a row is "statement" iff its symbol is in rejected_units or unquoted
    statement_symbols = {p["symbol"] for p in payload["positions"] if p.get("price_source") == "statement"}
    repricing_statement = set(rejected_units) | set(unquoted)
    assert statement_symbols == repricing_statement, (
        f"Invariant: statement symbols ({statement_symbols}) == "
        f"symbols in rejected_units or unquoted ({repricing_statement})"
    )

    # Invariant: large_moves is a subset of yahoo rows
    yahoo_symbols = {p["symbol"] for p in payload["positions"] if p.get("price_source") == "yahoo"}
    large_moves = set(repricing["large_moves"])
    assert large_moves.issubset(yahoo_symbols), (
        f"Invariant: large_moves ({large_moves}) is subset of yahoo rows ({yahoo_symbols})"
    )

    # Invariant: unpriced rows have value_gbp None, appear in kpis.unpriced count, and are absent from every sum
    unpriced_rows = [p for p in payload["positions"] if p.get("value_gbp") is None]
    for p in unpriced_rows:
        assert p["value_gbp"] is None, f"Invariant: unpriced row {p['symbol']} value_gbp is None"
        assert p.get("fx_missing") is True, f"Invariant: unpriced row {p['symbol']} fx_missing is True"
        assert p.get("price_source") == "none", f"Invariant: unpriced row {p['symbol']} price_source is 'none'"

    assert payload["kpis"]["unpriced"] == len(unpriced_rows), (
        f"Invariant: kpis.unpriced ({payload['kpis']['unpriced']}) == len(unpriced_rows) ({len(unpriced_rows)})"
    )

    priced_values = [p["value_gbp"] for p in priced_rows]
    assert abs(payload["kpis"]["invested"] - sum(priced_values)) < 1e-6, (
        f"Invariant: kpis.invested ({payload['kpis']['invested']}) == sum(priced_values) ({sum(priced_values)})"
    )

    region_sum = sum(r["value_gbp"] for r in payload["regions"])
    assert abs(region_sum - payload["kpis"]["invested"]) < 1e-6, (
        f"Invariant: region sum ({region_sum}) == invested ({payload['kpis']['invested']})"
    )

    sector_sum = sum(s["value_gbp"] for s in payload["sectors"])
    assert abs(sector_sum - payload["kpis"]["invested"]) < 1e-6, (
        f"Invariant: sector sum ({sector_sum}) == invested ({payload['kpis']['invested']})"
    )

    currency_sum = sum(c["value_gbp"] for c in payload["currencies"])
    assert abs(currency_sum - payload["kpis"]["invested"]) < 1e-6, (
        f"Invariant: currency sum ({currency_sum}) == invested ({payload['kpis']['invested']})"
    )

    # Invariant: meta.daily_pnl_source starts with "positions-partial" iff some priced row has day_change_pct None, else "positions-complete"
    has_missing_pct = any(p.get("day_change_pct") is None for p in priced_rows)
    source = payload["meta"]["daily_pnl_source"]
    if has_missing_pct:
        assert source.startswith("positions-partial"), (
            f"Invariant: some priced row has day_change_pct None, but daily_pnl_source is '{source}'"
        )
    else:
        assert source == "positions-complete", (
            f"Invariant: all priced rows have day_change_pct, but daily_pnl_source is '{source}'"
        )


def run_scenario(scenario_name: str, seed: int, tmp_path, monkeypatch):
    eod_rows, quotes, base_fx, intraday_fx, nav_rows = scenarios.build_scenario(scenario_name, seed=seed)

    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store, "NAV_PATH", tmp_path / "nav_history.jsonl")
    monkeypatch.setattr(store, "POSITIONS_PATH", tmp_path / "positions_eod.json")
    monkeypatch.setattr(store, "TX_PATH", tmp_path / "transactions.jsonl")
    monkeypatch.setattr(store, "ACTIONS_PATH", tmp_path / "corporate_actions.jsonl")

    store.write_positions_eod(eod_rows)
    if nav_rows:
        store.merge_nav(nav_rows)

    # Monkeypatch marketdata functions to prevent network calls
    monkeypatch.setattr(marketdata, "prior_closes", lambda con_ids: quotes)
    monkeypatch.setattr(marketdata, "fx_rates", lambda fallback: (base_fx, "flex"))
    monkeypatch.setattr(marketdata, "fx_intraday", lambda currencies: intraday_fx)
    monkeypatch.setattr(marketdata, "price_series", lambda con_ids, days=30: {})

    base_copy = dict(base_fx)
    fx, skipped = flexfeed.overlay_intraday_fx(base_fx, intraday_fx)
    applied = [c for c in intraday_fx if c not in skipped]
    fx_source = "flex+intraday" if applied else "flex"

    payload = compose(quotes=quotes, fx=fx, fx_source=fx_source, sparks={}, fx_skipped=skipped)

    if scenario_name == "missing_mark_and_value":
        # Row must be skipped, not crash
        assert "SKIPME" not in [p["symbol"] for p in payload["positions"]]

    assert_scenario_invariants(payload, eod_rows, quotes, base_fx, intraday_fx, fx, skipped, base_copy)


@pytest.mark.parametrize("scenario_name", scenarios.NAMED_SCENARIOS)
def test_named_scenario(scenario_name, tmp_path, monkeypatch):
    run_scenario(scenario_name, 0, tmp_path, monkeypatch)


@pytest.mark.parametrize("seed", range(20))
def test_mixed_book(seed, tmp_path, monkeypatch):
    run_scenario("mixed_book", seed, tmp_path, monkeypatch)
