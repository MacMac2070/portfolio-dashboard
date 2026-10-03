"""Scenario generator for Flex EOD repricing tests.

Produces (eod_rows, quotes, base_fx, intraday_fx, nav_rows) for named scenarios.
"""
from __future__ import annotations

import random

NAMED_SCENARIOS = [
    "gbp_pence_line",
    "prev_no_last",
    "large_move",
    "unknown_conid",
    "missing_fx",
    "zero_quantity",
    "missing_mark_with_value",
    "missing_mark_and_value",
    "breaker_open",
    "fx_overlay_mixed",
]


class ScenarioBuilder:
    def __init__(self, seed: int = 0):
        self.seed = seed
        self.rng = random.Random(seed)

    def build(self, name: str) -> tuple[list[dict], dict, dict, dict, list[dict]]:
        handler = getattr(self, name, None)
        if handler is None:
            raise ValueError(f"Unknown scenario name: {name}")
        return handler()

    def gbp_pence_line(self) -> tuple[list[dict], dict, dict, dict, list[dict]]:
        """An LSE row in GBP with a quote 100x the mark (units slip)."""
        eod_rows = [
            {
                "report_date": "2026-09-01",
                "con_id": 909083,
                "symbol": "HSBA",
                "exchange": "LSE",
                "currency": "GBP",
                "quantity": 100,
                "mark_price": 15.0,
                "value": 1500.0,
                "average_cost": 12.0,
                "unrealized_pnl": 300.0,
                "fx_to_base": 1.0,
                "account_id": "U1234567",
            }
        ]
        quotes = {
            909083: {"last": 1500.0, "prev": 15.0, "symbol": "HSBA"}
        }
        base_fx = {"GBP": 1.0}
        intraday_fx = {}
        nav_rows = [
            {"date": "2026-09-01", "nav_gbp": 2500.0, "cash_gbp": 1000.0, "source": "flex"}
        ]
        return eod_rows, quotes, base_fx, intraday_fx, nav_rows

    def prev_no_last(self) -> tuple[list[dict], dict, dict, dict, list[dict]]:
        """Quote with prev but last None."""
        eod_rows = [
            {
                "report_date": "2026-09-01",
                "con_id": 265598,
                "symbol": "AAPL",
                "exchange": "NASDAQ",
                "currency": "USD",
                "quantity": 10,
                "mark_price": 200.0,
                "value": 2000.0,
                "average_cost": 180.0,
                "unrealized_pnl": 200.0,
                "fx_to_base": 0.75,
                "account_id": "U1234567",
            }
        ]
        quotes = {
            265598: {"last": None, "prev": 195.0, "symbol": "AAPL"}
        }
        base_fx = {"GBP": 1.0, "USD": 0.75}
        intraday_fx = {}
        nav_rows = [
            {"date": "2026-09-01", "nav_gbp": 2500.0, "cash_gbp": 1000.0, "source": "flex"}
        ]
        return eod_rows, quotes, base_fx, intraday_fx, nav_rows

    def large_move(self) -> tuple[list[dict], dict, dict, dict, list[dict]]:
        """Last 1.6x mark (>50% move, not a units error)."""
        eod_rows = [
            {
                "report_date": "2026-09-01",
                "con_id": 265598,
                "symbol": "AAPL",
                "exchange": "NASDAQ",
                "currency": "USD",
                "quantity": 10,
                "mark_price": 200.0,
                "value": 2000.0,
                "average_cost": 180.0,
                "unrealized_pnl": 200.0,
                "fx_to_base": 0.75,
                "account_id": "U1234567",
            }
        ]
        quotes = {
            265598: {"last": 320.0, "prev": 200.0, "symbol": "AAPL"}
        }
        base_fx = {"GBP": 1.0, "USD": 0.75}
        intraday_fx = {}
        nav_rows = [
            {"date": "2026-09-01", "nav_gbp": 3400.0, "cash_gbp": 1000.0, "source": "flex"}
        ]
        return eod_rows, quotes, base_fx, intraday_fx, nav_rows

    def unknown_conid(self) -> tuple[list[dict], dict, dict, dict, list[dict]]:
        """A con_id with no quote at all."""
        eod_rows = [
            {
                "report_date": "2026-09-01",
                "con_id": 999999,
                "symbol": "UNKNOWN",
                "exchange": "NASDAQ",
                "currency": "USD",
                "quantity": 20,
                "mark_price": 50.0,
                "value": 1000.0,
                "average_cost": 40.0,
                "unrealized_pnl": 200.0,
                "fx_to_base": 0.75,
                "account_id": "U1234567",
            }
        ]
        quotes = {}
        base_fx = {"GBP": 1.0, "USD": 0.75}
        intraday_fx = {}
        nav_rows = [
            {"date": "2026-09-01", "nav_gbp": 2000.0, "cash_gbp": 1250.0, "source": "flex"}
        ]
        return eod_rows, quotes, base_fx, intraday_fx, nav_rows

    def missing_fx(self) -> tuple[list[dict], dict, dict, dict, list[dict]]:
        """A currency absent from fx."""
        eod_rows = [
            {
                "report_date": "2026-09-01",
                "con_id": 265598,
                "symbol": "AAPL",
                "exchange": "NASDAQ",
                "currency": "USD",
                "quantity": 10,
                "mark_price": 200.0,
                "value": 2000.0,
                "average_cost": 180.0,
                "unrealized_pnl": 200.0,
                "fx_to_base": 0.75,
                "account_id": "U1234567",
            },
            {
                "report_date": "2026-09-01",
                "con_id": 777777,
                "symbol": "CHF_STK",
                "exchange": "SIX",
                "currency": "CHF",
                "quantity": 20,
                "mark_price": 100.0,
                "value": 2000.0,
                "average_cost": 90.0,
                "unrealized_pnl": 200.0,
                "fx_to_base": None,
                "account_id": "U1234567",
            },
        ]
        quotes = {
            265598: {"last": 200.0, "prev": 200.0, "symbol": "AAPL"},
            777777: {"last": 100.0, "prev": 100.0, "symbol": "CHF_STK"},
        }
        base_fx = {"GBP": 1.0, "USD": 0.75}
        intraday_fx = {}
        nav_rows = [
            {"date": "2026-09-01", "nav_gbp": 3000.0, "cash_gbp": 1500.0, "source": "flex"}
        ]
        return eod_rows, quotes, base_fx, intraday_fx, nav_rows

    def zero_quantity(self) -> tuple[list[dict], dict, dict, dict, list[dict]]:
        """Quantity 0 row."""
        eod_rows = [
            {
                "report_date": "2026-09-01",
                "con_id": 265598,
                "symbol": "AAPL",
                "exchange": "NASDAQ",
                "currency": "USD",
                "quantity": 0,
                "mark_price": 200.0,
                "value": 0.0,
                "average_cost": 150.0,
                "unrealized_pnl": 0.0,
                "fx_to_base": 0.75,
                "account_id": "U1234567",
            }
        ]
        quotes = {
            265598: {"last": 210.0, "prev": 200.0, "symbol": "AAPL"}
        }
        base_fx = {"GBP": 1.0, "USD": 0.75}
        intraday_fx = {}
        nav_rows = [
            {"date": "2026-09-01", "nav_gbp": 1000.0, "cash_gbp": 1000.0, "source": "flex"}
        ]
        return eod_rows, quotes, base_fx, intraday_fx, nav_rows

    def missing_mark_with_value(self) -> tuple[list[dict], dict, dict, dict, list[dict]]:
        """Missing mark but value present."""
        eod_rows = [
            {
                "report_date": "2026-09-01",
                "con_id": 123456,
                "symbol": "NO_MARK",
                "exchange": "LSE",
                "currency": "GBP",
                "quantity": 100,
                "mark_price": None,
                "value": 1500.0,
                "average_cost": 12.0,
                "unrealized_pnl": 300.0,
                "fx_to_base": 1.0,
                "account_id": "U1234567",
            }
        ]
        quotes = {}
        base_fx = {"GBP": 1.0}
        intraday_fx = {}
        nav_rows = [
            {"date": "2026-09-01", "nav_gbp": 2500.0, "cash_gbp": 1000.0, "source": "flex"}
        ]
        return eod_rows, quotes, base_fx, intraday_fx, nav_rows

    def missing_mark_and_value(self) -> tuple[list[dict], dict, dict, dict, list[dict]]:
        """Row missing both mark and value must be skipped, not crash."""
        eod_rows = [
            {
                "report_date": "2026-09-01",
                "con_id": 111111,
                "symbol": "SKIPME",
                "exchange": "NASDAQ",
                "currency": "USD",
                "quantity": 10,
                "mark_price": None,
                "value": None,
                "average_cost": 100.0,
                "unrealized_pnl": 0.0,
                "fx_to_base": 0.75,
                "account_id": "U1234567",
            },
            {
                "report_date": "2026-09-01",
                "con_id": 265598,
                "symbol": "AAPL",
                "exchange": "NASDAQ",
                "currency": "USD",
                "quantity": 10,
                "mark_price": 200.0,
                "value": 2000.0,
                "average_cost": 180.0,
                "unrealized_pnl": 200.0,
                "fx_to_base": 0.75,
                "account_id": "U1234567",
            },
        ]
        quotes = {
            265598: {"last": 205.0, "prev": 200.0, "symbol": "AAPL"}
        }
        base_fx = {"GBP": 1.0, "USD": 0.75}
        intraday_fx = {}
        nav_rows = [
            {"date": "2026-09-01", "nav_gbp": 3000.0, "cash_gbp": 1000.0, "source": "flex"}
        ]
        return eod_rows, quotes, base_fx, intraday_fx, nav_rows

    def breaker_open(self) -> tuple[list[dict], dict, dict, dict, list[dict]]:
        """Breaker open: quotes == {}."""
        eod_rows = [
            {
                "report_date": "2026-09-01",
                "con_id": 265598,
                "symbol": "AAPL",
                "exchange": "NASDAQ",
                "currency": "USD",
                "quantity": 10,
                "mark_price": 200.0,
                "value": 2000.0,
                "average_cost": 180.0,
                "unrealized_pnl": 200.0,
                "fx_to_base": 0.75,
                "account_id": "U1234567",
            },
            {
                "report_date": "2026-09-01",
                "con_id": 3691937,
                "symbol": "AMZN",
                "exchange": "NASDAQ",
                "currency": "USD",
                "quantity": 20,
                "mark_price": 150.0,
                "value": 3000.0,
                "average_cost": 140.0,
                "unrealized_pnl": 200.0,
                "fx_to_base": 0.75,
                "account_id": "U1234567",
            },
        ]
        quotes = {}
        base_fx = {"GBP": 1.0, "USD": 0.75}
        intraday_fx = {}
        nav_rows = [
            {"date": "2026-09-01", "nav_gbp": 6000.0, "cash_gbp": 2250.0, "source": "flex"}
        ]
        return eod_rows, quotes, base_fx, intraday_fx, nav_rows

    def fx_overlay_mixed(self) -> tuple[list[dict], dict, dict, dict, list[dict]]:
        """Some intraday rates within 5%, some outside, one for a currency not in base."""
        base_fx = {"GBP": 1.0, "USD": 0.75, "EUR": 0.85, "HKD": 0.095}
        intraday_fx = {
            "USD": 0.76,    # ~1.3% move: within 5%, applied
            "EUR": 0.95,    # ~11.8% move: outside 5%, skipped
            "JPY": 0.0051,  # absent from base: skipped
            "HKD": 0.096,   # ~1.05% move: within 5%, applied
        }
        eod_rows = [
            {
                "report_date": "2026-09-01",
                "con_id": 265598,
                "symbol": "AAPL",
                "exchange": "NASDAQ",
                "currency": "USD",
                "quantity": 10,
                "mark_price": 200.0,
                "value": 2000.0,
                "average_cost": 180.0,
                "unrealized_pnl": 200.0,
                "fx_to_base": 0.75,
                "account_id": "U1234567",
            },
            {
                "report_date": "2026-09-01",
                "con_id": 517397504,
                "symbol": "SAP",
                "exchange": "IBIS",
                "currency": "EUR",
                "quantity": 10,
                "mark_price": 150.0,
                "value": 1500.0,
                "average_cost": 140.0,
                "unrealized_pnl": 100.0,
                "fx_to_base": 0.85,
                "account_id": "U1234567",
            },
            {
                "report_date": "2026-09-01",
                "con_id": 152791428,
                "symbol": "0700.HK",
                "exchange": "SEHK",
                "currency": "HKD",
                "quantity": 100,
                "mark_price": 300.0,
                "value": 30000.0,
                "average_cost": 280.0,
                "unrealized_pnl": 2000.0,
                "fx_to_base": 0.095,
                "account_id": "U1234567",
            },
        ]
        quotes = {
            265598: {"last": 205.0, "prev": 200.0, "symbol": "AAPL"},
            517397504: {"last": 152.0, "prev": 150.0, "symbol": "SAP"},
            152791428: {"last": 305.0, "prev": 300.0, "symbol": "0700.HK"},
        }
        nav_rows = [
            {"date": "2026-09-01", "nav_gbp": 10000.0, "cash_gbp": 4000.0, "source": "flex"}
        ]
        return eod_rows, quotes, base_fx, intraday_fx, nav_rows

    def mixed_book(self) -> tuple[list[dict], dict, dict, dict, list[dict]]:
        """15-40 rows across USD/HKD/EUR/SGD/GBP with randomised marks/quotes."""
        currencies = ["USD", "HKD", "EUR", "SGD", "GBP"]
        base_fx = {
            "GBP": 1.0,
            "USD": 0.75,
            "HKD": 0.095,
            "EUR": 0.85,
            "SGD": 0.58,
        }
        # Randomise intraday FX: some within tolerance, some outside, maybe an extra currency
        intraday_fx = {}
        for c in ["USD", "HKD", "EUR", "SGD"]:
            roll = self.rng.random()
            if roll < 0.4:
                # Within tolerance (within +/- 3%)
                intraday_fx[c] = round(base_fx[c] * (1.0 + self.rng.uniform(-0.03, 0.03)), 6)
            elif roll < 0.7:
                # Outside tolerance (+/- 12%)
                delta = self.rng.choice([-0.12, 0.12])
                intraday_fx[c] = round(base_fx[c] * (1.0 + delta), 6)
        if self.rng.random() < 0.5:
            intraday_fx["CHF"] = 0.90

        num_rows = self.rng.randint(15, 40)
        # Ensure all 5 currencies are represented in the rows
        row_currencies = list(currencies) + [self.rng.choice(currencies) for _ in range(num_rows - len(currencies))]
        self.rng.shuffle(row_currencies)

        eod_rows = []
        quotes = {}

        # If seed is odd, ensure at least one row has missing prev/quote for partial day change
        has_partial_day_pnl = (self.seed % 2 == 1)

        for i in range(num_rows):
            con_id = 100000 + self.seed * 100 + i
            symbol = f"MB_{self.seed}_{i}"
            curr = row_currencies[i]
            exchange = "LSE" if curr == "GBP" else ("SEHK" if curr == "HKD" else "NASDAQ")
            mark = round(self.rng.uniform(10.0, 500.0), 2)
            qty = self.rng.randint(5, 150)
            value = mark * qty
            avg_cost = round(mark * self.rng.uniform(0.7, 1.3), 2)
            unrealised = (mark - avg_cost) * qty

            eod_rows.append({
                "report_date": "2026-09-01",
                "con_id": con_id,
                "symbol": symbol,
                "exchange": exchange,
                "currency": curr,
                "quantity": qty,
                "mark_price": mark,
                "value": value,
                "average_cost": avg_cost,
                "unrealized_pnl": unrealised,
                "fx_to_base": base_fx[curr],
                "account_id": "U1234567",
            })

            # Randomise quote outcome
            q_roll = self.rng.random()
            if i == 0 and has_partial_day_pnl:
                # Force one unquoted row with prev=None so day_change_pct is None
                quotes[con_id] = {"last": None, "prev": None, "symbol": symbol}
            elif q_roll < 0.50:
                # Normal quote: last near mark, prev near mark
                last = round(mark * self.rng.uniform(0.92, 1.08), 2)
                prev = round(mark * self.rng.uniform(0.95, 1.05), 2)
                quotes[con_id] = {"last": last, "prev": prev, "symbol": symbol}
            elif q_roll < 0.65:
                # Units error: 100x mark
                last = round(mark * 100.0, 2)
                prev = mark
                quotes[con_id] = {"last": last, "prev": prev, "symbol": symbol}
            elif q_roll < 0.80:
                # Large move: 1.6x mark
                last = round(mark * 1.6, 2)
                prev = mark
                quotes[con_id] = {"last": last, "prev": prev, "symbol": symbol}
            elif q_roll < 0.90:
                # Prev with no last
                quotes[con_id] = {"last": None, "prev": mark, "symbol": symbol}
            else:
                if has_partial_day_pnl:
                    pass
                else:
                    # Provide normal quote so seed % 2 == 0 stays complete
                    last = round(mark * self.rng.uniform(0.92, 1.08), 2)
                    prev = round(mark * self.rng.uniform(0.95, 1.05), 2)
                    quotes[con_id] = {"last": last, "prev": prev, "symbol": symbol}

        cash = round(self.rng.uniform(2000.0, 8000.0), 2)
        eod_invested = sum(r["value"] * r["fx_to_base"] for r in eod_rows)
        nav_gbp = round(cash + eod_invested, 2)
        nav_rows = [
            {"date": "2026-09-01", "nav_gbp": nav_gbp, "cash_gbp": cash, "source": "flex"}
        ]
        return eod_rows, quotes, base_fx, intraday_fx, nav_rows


def build_scenario(name: str, seed: int = 0) -> tuple[list[dict], dict, dict, dict, list[dict]]:
    builder = ScenarioBuilder(seed)
    return builder.build(name)
