"""Tests for income.py dividend projections and universe contract key resolution."""
from datetime import date

import income
import universe


def test_universe_key_for_resolves_con_id_and_venue_variants():
    # HSBC Holdings is 909083; IBKR reports HSBAl on LSE
    assert universe.key_for(909083, "HSBAl") == "HSBA"
    assert universe.key_for(909083) == "HSBA"
    assert universe.key_for(None, "HSBAl") == "HSBA"
    assert universe.key_for(None, "HSBA") == "HSBA"

    # Regular US ticker
    assert universe.key_for(265598, "AAPL") == "AAPL"

    # Unmapped contract
    assert universe.key_for(99999999, "NONEXISTENT") is None


def test_income_build_matches_venue_suffixed_hsba():
    today = date.today()
    d1 = (today - date.resolution * 300).isoformat()
    d2 = (today - date.resolution * 200).isoformat()
    d3 = (today - date.resolution * 100).isoformat()
    d4 = (today - date.resolution * 20).isoformat()

    stored = {
        "holdings": {
            "HSBA": {
                "symbol": "HSBA.L",
                "currency": "GBP",
                "dividends": [
                    {"date": d1, "amount": 0.20},
                    {"date": d2, "amount": 0.10},
                    {"date": d3, "amount": 0.10},
                    {"date": d4, "amount": 0.20},
                ],
            }
        }
    }
    # Live position carries IBKR localSymbol 'HSBAl' and con_id 909083
    live = {
        "positions": [
            {
                "con_id": 909083,
                "symbol": "HSBAl",
                "quantity": 100.0,
                "cost_gbp": 1500.0,
            }
        ],
        "fx": {"GBP": 1.0},
    }

    out = income.build(stored, live)
    assert out["ready_gbp"] is True

    hsba_model = out["holdings"]["HSBA"]
    # TTM DPS: 0.20 + 0.10 + 0.10 + 0.20 = 0.60
    # Cost per share = 1500 / 100 = 15.0
    # Yield on cost = 0.60 / 15.0 = 0.04 (4%)
    assert hsba_model["yield_on_cost"] is not None
    assert round(hsba_model["yield_on_cost"], 4) == 0.04

    # Events must have gbp populated (not None)
    hsba_events = [e for e in out["next_events"] if e["key"] == "HSBA"]
    assert len(hsba_events) > 0
    for ev in hsba_events:
        assert ev["gbp"] is not None
        assert ev["gbp"] > 0

    assert out["forward_12m_gbp"] is not None
    assert out["forward_12m_gbp"] > 0


def test_income_build_with_foreign_currency_conversion():
    today = date.today()
    d1 = (today - date.resolution * 300).isoformat()
    d2 = (today - date.resolution * 200).isoformat()
    d3 = (today - date.resolution * 100).isoformat()
    d4 = (today - date.resolution * 20).isoformat()

    stored = {
        "holdings": {
            "AAPL": {
                "symbol": "AAPL",
                "currency": "USD",
                "dividends": [
                    {"date": d1, "amount": 0.25},
                    {"date": d2, "amount": 0.25},
                    {"date": d3, "amount": 0.25},
                    {"date": d4, "amount": 0.25},
                ],
            }
        }
    }
    live = {
        "positions": [
            {
                "con_id": 265598,
                "symbol": "AAPL",
                "quantity": 10.0,
                "cost_gbp": 1200.0,
            }
        ],
        "fx": {"USD": 0.75, "GBP": 1.0},
    }

    out = income.build(stored, live)
    model = out["holdings"]["AAPL"]
    # TTM DPS: 1.00 USD * 0.75 = 0.75 GBP
    # Cost per share = 1200 / 10 = 120.0 GBP
    # Yield on cost = 0.75 / 120 = 0.00625 -> 0.0063
    assert model["yield_on_cost"] == 0.0063
    aapl_events = [e for e in out["next_events"] if e["key"] == "AAPL"]
    for ev in aapl_events:
        assert ev["gbp"] == round(ev["amount"] * 10.0 * 0.75, 2)


def test_income_build_handles_missing_live_gracefully():
    stored = {
        "holdings": {
            "HSBA": {
                "symbol": "HSBA.L",
                "currency": "GBP",
                "dividends": [{"date": "2025-05-15", "amount": 0.20}],
            }
        }
    }
    out = income.build(stored, None)
    assert out["ready_gbp"] is False
    assert out["forward_12m_gbp"] is None
    assert out["holdings"]["HSBA"]["yield_on_cost"] is None
