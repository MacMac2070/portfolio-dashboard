"""Build the demo dataset the published repository ships with.

The real ledgers under dashboard/data/ are one account's history and never
enter git. What the repository carries instead is a small set of CSVs under
dashboard/data/mock/ and this script, which turns them into every file the
app reads, so a visitor can run the dashboard end to end on invented figures.

Two stages, both deterministic:

  --write-csv        synthesise the CSVs from seeded random walks. This is how
                     the committed CSVs were authored; run it again with
                     --end to move the demo to a newer date.
  --out DIR          read the CSVs and write the ledgers and snapshots into
                     DIR through store.py's own writers, so the files cannot
                     drift from the shapes the adapter expects.
  --install          the same, into dashboard/data/ (refuses to overwrite a
                     real ledger).
  --check            run adapter/reconcile.py over what was written and print
                     every verdict.

The synthesis is arranged so the reconciliation passes on its own terms: the
ledger replays to the positions snapshot (one split included), the NAV series
has a row for every weekday, each Change in NAV period's deposits equal the
cash rows inside it, and positions × FX equal the statement's stock figure to
the penny. London lines are held in pounds, as IBKR reports them.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DASHBOARD / "adapter"))

import flex  # noqa: E402
import lots  # noqa: E402
import reconcile  # noqa: E402
import store  # noqa: E402

CSV_DIR = DASHBOARD / "data" / "mock"
DATA_DIR = DASHBOARD / "data"
ACCOUNT_ID = "DU0000000"          # a paper-account shape, deliberately not a real one
SOURCE = "flex"
DEFAULT_SEED = 20260911
DEFAULT_END = "2026-09-11"
DEFAULT_WEEKDAYS = 90
INTEREST_RATE = 0.0325
WHT_RATE = 0.15

CSV_FILES = ("positions", "transactions", "cash_transactions", "nav_history",
             "nav_change", "fx_rates", "corporate_actions")


# ---------------------------------------------------------------- universe

@dataclass(frozen=True)
class Line:
    key: str
    con_id: int
    symbol: str
    exchange: str
    currency: str
    anchor: float      # price on the last day of the window, in the reporting currency
    drift: float       # daily log drift
    vol: float         # daily log volatility
    dp: int            # price decimals the venue quotes in


# The held universe of adapter/universe.py, so sectors, regions, venues and
# logos resolve. Quantities and dates are invented; each anchor is a real
# quote from around the window's end, because the app reprices EOD marks off
# today's delayed quotes and a mark far from the market would read as a
# day's move of tens of percent. Refresh the anchors when the window moves.
LINES = (
    Line("293", 1616420, "293", "SEHK", "HKD", 8.42, 0.0004, 0.018, 2),
    Line("C6L", 92216536, "C6L", "SGX", "SGD", 6.66, 0.0002, 0.012, 3),
    Line("700", 152791428, "700", "SEHK", "HKD", 428.4, 0.0006, 0.019, 1),
    Line("AAPL", 265598, "AAPL", "NASDAQ", "USD", 332.27, 0.0005, 0.016, 2),
    Line("AMZN", 3691937, "AMZN", "NASDAQ", "USD", 256.78, 0.0004, 0.019, 2),
    Line("GOOGL", 208813719, "GOOGL", "NASDAQ", "USD", 195.39, 0.0007, 0.018, 2),
    Line("META", 107113386, "META", "NASDAQ", "USD", 648.03, -0.0003, 0.021, 2),
    Line("HY9H", 517397504, "HY9H", "FWB", "EUR", 23.62, 0.0012, 0.024, 2),
    Line("INTC", 270639, "INTC", "NASDAQ", "USD", 102.94, -0.0009, 0.026, 2),
    Line("SMSN", 16520545, "SMSN", "LSEIOB1", "USD", 1364.0, 0.0008, 0.020, 1),
    Line("3115", 256718140, "3115", "SEHK", "HKD", 19.51, 0.0003, 0.013, 2),
    Line("ES3", 92214874, "ES3", "SGX", "SGD", 3.646, 0.0002, 0.008, 3),
    Line("IUCS", 270617971, "IUCS", "LSEETF", "USD", 7.649, 0.0001, 0.009, 3),
    Line("XDJP", 123279007, "XDJP", "LSEETF", "GBP", 26.86, 0.0003, 0.011, 2),
    Line("HSBA", 909083, "HSBA", "LSE", "GBP", 15.526, 0.0004, 0.012, 3),
)
BY_KEY = {line.key: line for line in LINES}

# Rates into GBP on the last day of the window, again from the real market.
FX_ANCHOR = {"USD": 0.73916, "HKD": 0.09434, "SGD": 0.584249, "EUR": 0.858295}
FX_VOL = 0.003

# One 2-for-1 split, so the replay and the lot engine exercise an action.
SPLIT_KEY, SPLIT_INDEX, SPLIT_RATIO = "C6L", 48, 2

DEPOSITS = (20000.0, 5000.0, 5000.0, 5000.0)     # first weekday of the first four months

# (weekday index or ("deposit", n, offset), key, side, quantity)
TRADES = (
    (1, "AAPL", "BUY", 12), (1, "AMZN", "BUY", 9.5),
    (2, "GOOGL", "BUY", 10), (2, "META", "BUY", 3),
    (3, "HSBA", "BUY", 250), (3, "XDJP", "BUY", 60),
    (4, "700", "BUY", 20), (4, "293", "BUY", 1000), (4, "C6L", "BUY", 300),
    (("deposit", 1, 1), "3115", "BUY", 400), (("deposit", 1, 1), "ES3", "BUY", 800),
    (("deposit", 1, 2), "INTC", "BUY", 60), (("deposit", 1, 2), "SMSN", "BUY", 2),
    (("deposit", 2, 1), "HY9H", "BUY", 80), (("deposit", 2, 2), "IUCS", "BUY", 250),
    (("deposit", 3, 1), "AAPL", "BUY", 3),
    (32, "293", "SELL", 300), (55, "INTC", "SELL", 40), (72, "AMZN", "SELL", 2.5),
)

# (key, ex-date index, pay-date index, gross rate per share, withholding rate)
DIVIDENDS = (
    ("AAPL", 8, 15, 0.26, WHT_RATE), ("AAPL", 70, 77, 0.26, WHT_RATE),
    ("GOOGL", 25, 40, 0.21, WHT_RATE), ("META", 30, 44, 0.525, WHT_RATE),
    ("HSBA", 20, 40, 0.075, 0.0), ("700", 35, 52, 4.5, 0.0),
    ("3115", 60, 74, 0.40, 0.0), ("C6L", 58, 66, 0.30, 0.0),
    ("ES3", 62, 68, 0.06, 0.0), ("SMSN", 50, 63, 0.25, WHT_RATE),
    ("HY9H", 66, 75, 0.60, WHT_RATE),
)
MARKET_DATA_FEE_USD = -4.50


# ---------------------------------------------------------------- helpers

def weekdays_ending(end: date, count: int) -> list[date]:
    days: list[date] = []
    day = end
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return list(reversed(days))


def walk(seed: int, name: str, anchor: float, drift: float, vol: float, count: int) -> list[float]:
    """A seeded log-normal path that ends exactly at `anchor`: the steps are
    drawn forwards and the path is unrolled backwards from the last day."""
    rng = random.Random(f"{seed}:{name}")
    steps = [drift + vol * rng.gauss(0.0, 1.0) for _ in range(count - 1)]
    out = [anchor]
    for step in reversed(steps):
        out.append(out[-1] / math.exp(step))
    return list(reversed(out))


def nth_weekday_of_month(days: list[date], n: int) -> dict[str, int]:
    """Index of the n-th reported weekday in each calendar month of the window."""
    seen: dict[str, int] = {}
    out: dict[str, int] = {}
    for i, day in enumerate(days):
        month = day.isoformat()[:7]
        seen[month] = seen.get(month, 0) + 1
        if seen[month] == n:
            out[month] = i
    return out


def costs(line: Line, side: str, qty: float, price: float) -> tuple[float, float | None]:
    """Commission and taxes for a fill, negative, in the trade currency."""
    notional = qty * price
    ex = line.exchange
    tax = 0.0
    if ex == "NASDAQ":
        comm = max(1.0, 0.005 * qty)
        if side == "SELL":
            tax = 0.0000278 * notional
    elif ex == "SEHK":
        comm = max(18.0, 0.0008 * notional)
        tax = (0.001 + 0.000027) * notional
    elif ex == "SGX":
        comm = max(2.5, 0.0008 * notional)
    elif ex == "LSE":
        comm = max(1.0, 0.0005 * notional)
        if side == "BUY":
            tax = 0.005 * notional
    elif ex in ("LSEETF", "LSEIOB1"):
        comm = max(1.0, 0.0005 * notional)
    elif ex == "FWB":
        comm = max(4.0, 0.001 * notional)
    else:
        comm = 1.0
    tax = round(tax, 2)
    return -round(comm, 2), (-tax if tax else None)


def money(x: float) -> float:
    return round(x + 0.0, 2)


# ---------------------------------------------------------------- synthesis

def synthesise(seed: int, end: date, count: int) -> dict[str, list[dict]]:
    """Every CSV's rows, derived from one set of seeded price paths."""
    days = weekdays_ending(end, count)
    iso = [d.isoformat() for d in days]
    n = len(days)

    fx: dict[str, list[float]] = {"GBP": [1.0] * n}
    for ccy, anchor in FX_ANCHOR.items():
        fx[ccy] = [round(v, 6) for v in walk(seed, f"fx:{ccy}", anchor, 0.0, FX_VOL, n)]

    prices: dict[str, list[float]] = {}
    for line in LINES:
        path = walk(seed, line.symbol, line.anchor, line.drift, line.vol, n)
        if line.key == SPLIT_KEY:
            path = [p * SPLIT_RATIO if i < SPLIT_INDEX else p for i, p in enumerate(path)]
        prices[line.key] = [round(p, line.dp) for p in path]

    first = nth_weekday_of_month(days, 1)
    months = sorted(first)
    deposit_days = [first[m] for m in months[:len(DEPOSITS)]]

    def when(spec) -> int:
        if isinstance(spec, tuple):
            _, k, offset = spec
            return deposit_days[k] + offset
        return spec

    # Fills, oldest first, with FIFO realised P&L on the sells.
    fills = sorted(((when(spec), key, side, float(qty)) for spec, key, side, qty in TRADES),
                   key=lambda f: (f[0], f[1]))
    open_lots: dict[str, list[list[float]]] = {}
    tx_rows: list[dict] = []
    for seq, (i, key, side, qty) in enumerate(fills, start=1):
        line = BY_KEY[key]
        price = prices[key][i]
        comm, tax = costs(line, side, qty, price)
        realised = 0.0
        if side == "BUY":
            open_lots.setdefault(key, []).append([qty, price])
        else:
            remaining, cost = qty, 0.0
            for lot in open_lots.get(key, []):
                take = min(lot[0], remaining)
                cost += take * lot[1]
                lot[0] -= take
                remaining -= take
                if remaining <= 1e-9:
                    break
            open_lots[key] = [lot for lot in open_lots.get(key, []) if lot[0] > 1e-9]
            realised = money(qty * price - cost + comm + (tax or 0.0))
        tx_rows.append({
            "exec_id": f"MOCK-{seq:04d}", "time": iso[i], "con_id": line.con_id,
            "symbol": line.symbol, "currency": line.currency, "exchange": line.exchange,
            "side": side, "quantity": qty, "price": price, "commission": comm,
            "asset": "STK", "fx_to_base": fx[line.currency][i], "realized_pnl": realised,
            "open_close": "O" if side == "BUY" else "C", "taxes": tax,
            "commission_currency": line.currency,
        })

    split_line = BY_KEY[SPLIT_KEY]
    held_at_split = reconcile.replay_positions(tx_rows, upto=iso[SPLIT_INDEX]).get(split_line.con_id, 0.0)
    actions = [{
        "action_id": "MOCKCA-0001", "con_id": split_line.con_id, "symbol": split_line.symbol,
        "date": iso[SPLIT_INDEX], "code": "FS", "quantity": held_at_split * (SPLIT_RATIO - 1),
        "currency": split_line.currency, "fx_to_base": fx[split_line.currency][SPLIT_INDEX],
        "description": f"{split_line.symbol} SPLIT {SPLIT_RATIO} FOR 1",
    }]

    # Dated cash effects in GBP, then the daily series they produce.
    cash_rows: list[dict] = []
    cash_seq = 0

    def cash_row(i: int, kind: str, amount: float, ccy: str, symbol: str = "", con_id: int | None = None) -> None:
        nonlocal cash_seq
        cash_seq += 1
        cash_rows.append({
            "tx_id": f"MOCKCASH-{cash_seq:04d}", "date": iso[i], "type": kind,
            "amount": money(amount), "currency": ccy, "fx_to_base": fx[ccy][i],
            "symbol": symbol, "con_id": con_id,
        })

    for k, amount in enumerate(DEPOSITS):
        cash_row(deposit_days[k], "Deposits/Withdrawals", amount, "GBP")
    for key, ex_i, pay_i, rate, wht in DIVIDENDS:
        line = BY_KEY[key]
        qty = reconcile.replay_positions(tx_rows, upto=iso[ex_i], actions=actions).get(line.con_id, 0.0)
        if qty <= 0:
            raise RuntimeError(f"{key} pays a dividend on {iso[ex_i]} but is not held")
        gross = money(rate * qty)
        cash_row(pay_i, "Dividends", gross, line.currency, line.symbol, line.con_id)
        if wht:
            cash_row(pay_i, "Withholding Tax", -money(gross * wht), line.currency, line.symbol, line.con_id)
    fifth = nth_weekday_of_month(days, 5)
    for month in months[1:]:
        if month in fifth:
            cash_row(fifth[month], "Other Fees", MARKET_DATA_FEE_USD, "USD")

    def gbp_effects(i: int) -> float:
        total = 0.0
        for row in cash_rows:
            if row["date"] == iso[i]:
                total += row["amount"] * row["fx_to_base"]
        for row in tx_rows:
            if row["time"] != iso[i]:
                continue
            gross = row["quantity"] * row["price"]
            fees = row["commission"] + (row["taxes"] or 0.0)     # both negative
            signed = -gross if row["side"] == "BUY" else gross
            total += (signed + fees) * row["fx_to_base"]
        return total

    third = nth_weekday_of_month(days, 3)
    interest_days = {third[m]: m for m in months[1:] if m in third}
    cash_gbp: list[float] = []
    nav_rows: list[dict] = []
    balance = 0.0
    for i in range(n):
        if i in interest_days:
            prev_month = months[months.index(interest_days[i]) - 1]
            accrued = sum(cash_gbp[j] * INTEREST_RATE / 365 for j in range(i) if iso[j][:7] == prev_month)
            cash_row(i, "Broker Interest Received", accrued, "GBP")
        balance += gbp_effects(i)
        if balance < 0:
            raise RuntimeError(f"cash would go negative on {iso[i]}: adjust the trade sizes")
        cash_gbp.append(balance)
        held = reconcile.replay_positions(tx_rows, upto=iso[i], actions=actions)
        stock = sum(qty * prices[line.key][i] * fx[line.currency][i]
                    for line in LINES for qty in [held.get(line.con_id, 0.0)] if qty)
        stock_r, cash_r = money(stock), money(balance)
        nav_rows.append({"date": iso[i], "nav_gbp": money(stock_r + cash_r),
                         "cash_gbp": cash_r, "stock_gbp": stock_r, "accruals_gbp": 0.0})
    cash_rows.sort(key=lambda r: (r["date"], r["tx_id"]))

    # Positions at the end, priced at the last close, costed by the app's own lots.
    summaries = lots.summarise(lots.build_lots(tx_rows, actions=actions))
    held = reconcile.replay_positions(tx_rows, upto=iso[-1], actions=actions)
    position_rows: list[dict] = []
    for line in LINES:
        qty = held.get(line.con_id, 0.0)
        if qty <= 1e-9:
            continue
        mark = prices[line.key][-1]
        value = money(qty * mark)
        summary = summaries.get(line.con_id)
        avg_cost = round(summary.cost_local / summary.quantity, 4) if summary else mark
        position_rows.append({
            "report_date": iso[-1], "con_id": line.con_id, "symbol": line.symbol,
            "exchange": line.exchange, "currency": line.currency, "asset": "STK",
            "quantity": round(qty, 4), "mark_price": mark, "value": value,
            "average_cost": avg_cost, "unrealized_pnl": money(value - avg_cost * qty),
            "fx_to_base": fx[line.currency][-1], "account_id": ACCOUNT_ID,
        })
    position_rows.sort(key=lambda r: -abs(r["value"] * r["fx_to_base"]))

    change_rows = change_in_nav(days, nav_rows, cash_rows, tx_rows, deposit_days, months)
    fx_rows = [{"date": iso[i], "currency": ccy, "rate": fx[ccy][i]}
               for i in range(n) for ccy in sorted(FX_ANCHOR)]
    return {
        "positions": position_rows, "transactions": tx_rows, "cash_transactions": cash_rows,
        "nav_history": nav_rows, "nav_change": change_rows, "fx_rates": fx_rows,
        "corporate_actions": actions,
    }


def change_in_nav(days: list[date], nav_rows: list[dict], cash_rows: list[dict],
                  tx_rows: list[dict], deposit_days: list[int], months: list[str]) -> list[dict]:
    """One Change in NAV row per calendar month plus the whole window, with
    mark-to-market as the residual so the flow attribution shows no drift."""
    iso = [d.isoformat() for d in days]
    nav = {r["date"]: r["nav_gbp"] for r in nav_rows}
    deposits_on = {iso[i] for i in deposit_days}

    def window(start: str, end: str) -> dict:
        cash = [r for r in cash_rows if start <= r["date"] <= end]
        fills = [r for r in tx_rows if start <= r["time"] <= end]

        def bucket(name: str) -> float:
            return money(sum(r["amount"] * r["fx_to_base"] for r in cash if flex._bucket(r["type"]) == name))

        starting = 0.0
        prior = [d for d in iso if d < start]
        if prior:
            starting = nav[prior[-1]]
        ending = nav[end]
        deposits = bucket("deposits_withdrawals")
        dividends = bucket("dividends")
        wht = bucket("withholding_tax")
        interest = bucket("interest_received")
        fees = bucket("other_fees")
        commissions = money(sum(r["commission"] * r["fx_to_base"] for r in fills))
        taxes = money(sum((r["taxes"] or 0.0) * r["fx_to_base"] for r in fills))
        explained = deposits + dividends + wht + interest + fees + commissions + taxes
        mtm = money(ending - starting - explained)

        chain = 1.0
        previous = None
        for d in iso:
            if d < start or d > end:
                continue
            if previous is not None and nav[previous]:
                flow = deposits_on_day(cash_rows, d) if d in deposits_on else 0.0
                chain *= 1.0 + (nav[d] - nav[previous] - flow) / (nav[previous] + max(flow, 0.0))
            previous = d
        return {
            "from_date": start, "to_date": end, "startingValue": money(starting),
            "endingValue": money(ending), "depositsWithdrawals": deposits,
            "dividends": dividends, "withholdingTax": wht, "interest": interest,
            "otherFees": fees, "commissions": commissions, "transactionTax": taxes,
            "mtm": mtm, "twr": round((chain - 1.0) * 100.0, 4),
        }

    rows = []
    for month in months:
        in_month = [d for d in iso if d[:7] == month]
        rows.append(window(in_month[0], in_month[-1]))
    rows.append(window(iso[0], iso[-1]))
    return rows


def deposits_on_day(cash_rows: list[dict], day: str) -> float:
    return sum(r["amount"] * r["fx_to_base"] for r in cash_rows
               if r["date"] == day and flex._bucket(r["type"]) == "deposits_withdrawals")


# ---------------------------------------------------------------- csv

def write_csvs(csv_dir: Path, tables: dict[str, list[dict]]) -> None:
    csv_dir.mkdir(parents=True, exist_ok=True)
    for name in CSV_FILES:
        rows = tables[name]
        columns = list(rows[0].keys())
        with (csv_dir / f"{name}.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
            writer.writeheader()
            for row in rows:
                writer.writerow({k: ("" if v is None else v) for k, v in row.items()})


def read_csv(csv_dir: Path, name: str) -> list[dict]:
    path = csv_dir / f"{name}.csv"
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def num(value: str | None) -> float | None:
    return None if value in (None, "") else float(value)


def integer(value: str | None) -> int | None:
    return None if value in (None, "") else int(float(value))


# ---------------------------------------------------------------- build

def point_store_at(out: Path) -> None:
    """Re-point store.py's module-level paths, the way the tests do."""
    store.DATA_DIR = out
    for attr, name in (("NAV_PATH", "nav_history.jsonl"), ("TX_PATH", "transactions.jsonl"),
                       ("NAV_CHANGE_PATH", "nav_change.jsonl"), ("CASH_PATH", "cash_transactions.jsonl"),
                       ("POSITIONS_PATH", "positions_eod.json"), ("LAST_RUN_PATH", "last_run.json"),
                       ("QUALITY_PATH", "quality.json"), ("FX_PATH", "fx_rates.jsonl"),
                       ("ACTIONS_PATH", "corporate_actions.jsonl")):
        setattr(store, attr, out / name)


def build(csv_dir: Path, out: Path) -> dict[str, int]:
    """Write every file the app needs from the CSVs, through the store."""
    out.mkdir(parents=True, exist_ok=True)
    point_store_at(out)
    census: dict[str, int] = {}

    nav = [{"date": r["date"], "nav_gbp": num(r["nav_gbp"]), "source": SOURCE,
            "cash_gbp": num(r["cash_gbp"]), "stock_gbp": num(r["stock_gbp"]),
            "accruals_gbp": num(r["accruals_gbp"])} for r in read_csv(csv_dir, "nav_history")]
    census["nav_history.jsonl"] = store.merge_nav(nav)[1]

    tx = []
    for r in read_csv(csv_dir, "transactions"):
        tx.append({
            "exec_id": r["exec_id"], "time": r["time"], "con_id": integer(r["con_id"]),
            "symbol": r["symbol"], "currency": r["currency"], "exchange": r["exchange"],
            "side": r["side"], "quantity": num(r["quantity"]), "price": num(r["price"]),
            "commission": num(r["commission"]), "asset": r["asset"], "fx_to_base": num(r["fx_to_base"]),
            "realized_pnl": num(r["realized_pnl"]), "open_close": r["open_close"],
            "taxes": num(r["taxes"]), "commission_currency": r["commission_currency"], "source": SOURCE,
        })
    census["transactions.jsonl"] = store.merge_transactions(tx)[1]

    cash = []
    for r in read_csv(csv_dir, "cash_transactions"):
        amount, rate = num(r["amount"]), num(r["fx_to_base"])
        cash.append({
            "date": r["date"], "type": r["type"], "bucket": flex._bucket(r["type"]),
            "amount": amount, "currency": r["currency"], "fx_to_base": rate,
            "amount_gbp": round(amount * rate, 4), "symbol": r["symbol"],
            "con_id": integer(r["con_id"]), "tx_id": r["tx_id"], "source": SOURCE,
        })
    census["cash_transactions.jsonl"] = store.merge_cash(cash)[1]

    change = []
    for r in read_csv(csv_dir, "nav_change"):
        row = {name: (num(r.get(name)) or 0.0) for name in flex.NAV_CHANGE_FIELDS}
        span = (date.fromisoformat(r["to_date"]) - date.fromisoformat(r["from_date"])).days
        row.update({"from_date": r["from_date"], "to_date": r["to_date"], "currency": "GBP",
                    "span_days": span, "unmapped": [], "source": SOURCE})
        change.append(row)
    census["nav_change.jsonl"] = store.merge_nav_change(change)[1]

    fx_rows = [{"date": r["date"], "currency": r["currency"], "rate": num(r["rate"])}
               for r in read_csv(csv_dir, "fx_rates")]
    census["fx_rates.jsonl"] = store.merge_fx(fx_rows)[1]

    actions = []
    for r in read_csv(csv_dir, "corporate_actions"):
        actions.append({
            "action_id": r["action_id"], "con_id": integer(r["con_id"]), "symbol": r["symbol"],
            "date": r["date"], "code": r["code"], "kind": flex.ACTION_KINDS.get(r["code"], "other"),
            "quantity": num(r["quantity"]), "proceeds": None, "value": None,
            "currency": r["currency"], "fx_to_base": num(r["fx_to_base"]),
            "description": r["description"], "source": SOURCE,
        })
    census["corporate_actions.jsonl"] = store.merge_corporate_actions(actions)[1]

    positions = []
    for r in read_csv(csv_dir, "positions"):
        positions.append({
            "report_date": r["report_date"], "con_id": integer(r["con_id"]), "symbol": r["symbol"],
            "exchange": r["exchange"], "currency": r["currency"], "asset": r["asset"],
            "quantity": num(r["quantity"]), "mark_price": num(r["mark_price"]), "value": num(r["value"]),
            "average_cost": num(r["average_cost"]), "unrealized_pnl": num(r["unrealized_pnl"]),
            "fx_to_base": num(r["fx_to_base"]), "account_id": r["account_id"], "source": SOURCE,
        })
    payload = store.write_positions_eod(positions)
    stamp = f"{payload['asof']}T22:30:00+00:00"
    payload["fetched_at"] = stamp          # pinned, so two builds are byte-identical
    store._atomic_write(store.POSITIONS_PATH, json.dumps(payload, indent=2))
    census["positions_eod.json"] = len(positions)

    store.write_json(out / "watchlist_extra.json", {"entries": []})
    census["watchlist_extra.json"] = 0

    # The health gate asks when the nightly job last ran; a demo has no job,
    # so record one successful run on the evening of the window's last day.
    # It ages honestly from there: a day later the chip warns, later it fails,
    # exactly as it would for a real account whose job had stopped.
    asof = payload["asof"]
    store.write_json(store.LAST_RUN_PATH, {
        "schema_version": 1,
        "started_at": f"{asof}T22:35:00+00:00", "finished_at": f"{asof}T22:41:00+00:00",
        "pid": 0, "ok": True, "exit_code": 0, "stage": "done", "stages": {},
        "forced": False, "payload_source": "flex-eod", "reason": None,
        "archive": {"nav": census["nav_history.jsonl"], "cash": census["cash_transactions.jsonl"],
                    "nav_change": census["nav_change.jsonl"], "tx": census["transactions.jsonl"]},
    })
    census["last_run.json"] = 0

    last_rates = {r["currency"]: r["rate"] for r in fx_rows if r["date"] == payload["asof"]}
    try:
        import flexfeed
        snapshot = flexfeed.compose_payload(quotes={}, fx=last_rates, sparks={})
    except Exception as exc:  # the snapshot is a convenience; the ledgers are the product
        print(f"warning: portfolio.json not written ({exc.__class__.__name__}: {exc})", file=sys.stderr)
        snapshot = None
    if snapshot:
        snapshot["meta"]["generated_at"] = stamp
        store.write_json(out / "portfolio.json", snapshot)
        census["portfolio.json"] = len(snapshot["positions"])
    return census


def check(out: Path) -> int:
    """Reconcile what was written; today is pinned to the data's own last day."""
    point_store_at(out)
    last = store.nav_latest()
    today = date.fromisoformat(last) if last else date.today()
    report = reconcile.build(today=today)
    store.write_json(store.QUALITY_PATH, report)
    print(f"reconciliation as of {today.isoformat()} (the lag check is pinned to the data's last day): "
          f"{report['status'].upper()}")
    for item in report["checks"]:
        print(f"  {item['status']:<8} {item['id']:<20} {item['summary']}")
    return 0 if report["status"] in ("ok", "pending") else 1


# ---------------------------------------------------------------- cli

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv-dir", type=Path, default=CSV_DIR, help="where the CSVs live")
    parser.add_argument("--write-csv", action="store_true", help="synthesise the CSVs (authoring mode)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--end", default=DEFAULT_END, help="last weekday of the window, YYYY-MM-DD")
    parser.add_argument("--weekdays", type=int, default=DEFAULT_WEEKDAYS)
    parser.add_argument("--out", type=Path, help="build the app's files into this directory")
    parser.add_argument("--install", action="store_true", help="build into dashboard/data/")
    parser.add_argument("--check", action="store_true", help="reconcile what was built")
    args = parser.parse_args(argv)

    if args.write_csv:
        end = date.fromisoformat(args.end)
        if end.weekday() >= 5:
            parser.error("--end must be a weekday")
        tables = synthesise(args.seed, end, args.weekdays)
        write_csvs(args.csv_dir, tables)
        for name in CSV_FILES:
            print(f"  {name}.csv: {len(tables[name])} rows")
        return 0

    if args.install:
        out = DATA_DIR
        if (out / "nav_history.jsonl").exists():
            print(f"refusing to overwrite {out / 'nav_history.jsonl'}: move the real ledgers aside first, e.g.\n"
                  f"  mv '{out}' '{out}.real'  (or point --out somewhere else)", file=sys.stderr)
            return 2
    elif args.out:
        out = args.out
    else:
        parser.error("pass --out DIR or --install (or --write-csv to author the CSVs)")

    census = build(args.csv_dir, out)
    for name, rows in census.items():
        print(f"  {name}: {rows} rows" if rows else f"  {name}")
    return check(out) if args.check else 0


if __name__ == "__main__":
    sys.exit(main())
