"""ib_async -> raw account, positions and executions from IB Gateway.

Talks to the Gateway already running on this machine at 127.0.0.1:4001. Nothing
here needs a Claude session, an internet API key, or a browser.

Deliberate choices, each of which has bitten this integration:

  clientId=7      `test_ibkr_connection.py` holds clientId=1. Two clients with
                  the same id do not error clearly — the second connect just
                  fails or silently steals the session. Keep them distinct.

  ib.portfolio()  not ib.positions(). positions() returns quantity and average
                  cost only; portfolio() adds marketPrice, marketValue and
                  unrealizedPNL, which is most of what the dashboard shows.

  readonly=True   this process must never be able to place an order.

  reqPnLSingle    day-change is a streaming subscription, not a field on the
                  position. It needs a moment to populate before you read it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

from ib_async import IB, ExecutionFilter

HOST = "127.0.0.1"
PORT = 4001          # IB Gateway, live account
CLIENT_ID = 7
CONNECT_TIMEOUT = 8
PNL_SETTLE_SECONDS = 3.0

log = logging.getLogger(__name__)


class GatewayUnavailable(RuntimeError):
    """IB Gateway is not running, not logged in, or not accepting API clients."""


@dataclass
class RawPosition:
    con_id: int
    symbol: str
    exchange: str
    currency: str
    sec_type: str
    quantity: float
    market_price: float
    market_value: float      # in `currency`
    average_cost: float      # per share, in `currency`
    unrealized_pnl: float    # in `currency`
    daily_pnl: float | None = None   # in `currency`; None if not delivered


@dataclass
class RawAccount:
    account_id: str
    base_currency: str
    net_liquidation: float
    total_cash: float
    gross_position_value: float
    daily_pnl: float | None
    unrealized_pnl: float | None
    # currency -> rate INTO base currency (IBKR's own rates; FX fallback)
    exchange_rates: dict[str, float] = field(default_factory=dict)


@dataclass
class RawSnapshot:
    fetched_at: str
    account: RawAccount
    positions: list[RawPosition]
    fills: list[dict]

    def to_dict(self) -> dict:
        return {
            "fetched_at": self.fetched_at,
            "account": asdict(self.account),
            "positions": [asdict(p) for p in self.positions],
            "fills": self.fills,
        }


def _num(raw: str) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def fetch(include_fills: bool = True) -> RawSnapshot:
    """Connect, pull everything the dashboard needs, disconnect.

    Raises GatewayUnavailable if the Gateway cannot be reached — callers treat
    that as "render the stale state", not as a crash.
    """
    ib = IB()
    try:
        try:
            ib.connect(HOST, PORT, clientId=CLIENT_ID,
                       timeout=CONNECT_TIMEOUT, readonly=True)
        except Exception as exc:
            raise GatewayUnavailable(
                f"could not reach IB Gateway at {HOST}:{PORT} — is it running "
                f"and logged in, with API clients enabled? ({exc})"
            ) from exc

        accounts = ib.managedAccounts()
        if not accounts:
            raise GatewayUnavailable("connected, but the session manages no accounts")
        account_id = accounts[0]

        summary = {row.tag: row for row in ib.accountSummary(account_id)}
        base_currency = (
            summary["NetLiquidation"].currency if "NetLiquidation" in summary else "GBP"
        )

        # IBKR publishes its own FX rates per currency. marketdata.py prefers
        # openbb, but falls back to these so an unattended run still completes
        # when the network is down.
        exchange_rates: dict[str, float] = {}
        for value in ib.accountValues(account_id):
            if value.tag == "ExchangeRate":
                rate = _num(value.value)
                if rate is not None:
                    exchange_rates[value.currency] = rate

        items = ib.portfolio(account_id) if _takes_account(ib.portfolio) else ib.portfolio()

        positions: list[RawPosition] = []
        for item in items:
            contract = item.contract
            positions.append(RawPosition(
                con_id=contract.conId,
                symbol=contract.symbol,
                exchange=contract.primaryExchange or contract.exchange or "",
                currency=contract.currency,
                sec_type=contract.secType,
                quantity=float(item.position),
                market_price=float(item.marketPrice),
                market_value=float(item.marketValue),
                average_cost=float(item.averageCost),
                unrealized_pnl=float(item.unrealizedPNL),
            ))

        account_daily, account_unrealized = _subscribe_pnl(ib, account_id, positions)

        fills: list[dict] = []
        if include_fills:
            fills = _fetch_fills(ib)

        return RawSnapshot(
            fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            account=RawAccount(
                account_id=account_id,
                base_currency=base_currency,
                net_liquidation=_num(summary["NetLiquidation"].value) or 0.0,
                total_cash=_num(summary["TotalCashValue"].value) or 0.0,
                gross_position_value=_num(summary["GrossPositionValue"].value) or 0.0,
                daily_pnl=account_daily,
                unrealized_pnl=account_unrealized,
                exchange_rates=exchange_rates,
            ),
            positions=positions,
            fills=fills,
        )
    finally:
        if ib.isConnected():
            ib.disconnect()


def _takes_account(fn) -> bool:
    """ib_async has changed portfolio()'s signature across versions."""
    try:
        import inspect
        return bool(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        return False


def _subscribe_pnl(ib: IB, account_id: str,
                   positions: list[RawPosition]) -> tuple[float | None, float | None]:
    """Populate per-position daily_pnl in place; return account-level P&L.

    Day-change arrives over a streaming subscription, so everything is
    subscribed first and read once after a single settle window — subscribing
    and reading one position at a time would cost PNL_SETTLE_SECONDS each.

    Market-data entitlements vary by venue, so some positions legitimately
    never report. That is not an error; the caller falls back to openbb.
    """
    account_daily = account_unrealized = None
    try:
        account_pnl = ib.reqPnL(account_id)
        singles = {}
        for position in positions:
            try:
                singles[position.con_id] = ib.reqPnLSingle(account_id, "", position.con_id)
            except Exception as exc:
                log.debug("no per-position P&L for %s: %s", position.symbol, exc)

        ib.sleep(PNL_SETTLE_SECONDS)

        by_con_id = {p.con_id: p for p in positions}
        for con_id, single in singles.items():
            value = getattr(single, "dailyPnL", None)
            # IBKR sends a sentinel for "not available" rather than omitting it.
            if value is not None and abs(value) < 1e307:
                by_con_id[con_id].daily_pnl = float(value)

        if account_pnl is not None:
            if account_pnl.dailyPnL is not None and abs(account_pnl.dailyPnL) < 1e307:
                account_daily = float(account_pnl.dailyPnL)
            if account_pnl.unrealizedPnL is not None and abs(account_pnl.unrealizedPnL) < 1e307:
                account_unrealized = float(account_pnl.unrealizedPnL)

        ib.cancelPnL(account_id)
        for con_id in singles:
            try:
                ib.cancelPnLSingle(account_id, "", con_id)
            except Exception:
                pass
    except Exception as exc:
        log.warning("P&L subscription failed, falling back to market data: %s", exc)

    return account_daily, account_unrealized


def _fetch_fills(ib: IB) -> list[dict]:
    """Executions the Gateway still has.

    TWS API keeps no trade archive — this covers the current session and, with
    a filter, at best the last few days. Full history comes from Flex; this
    only tops up transactions.jsonl between Flex runs.
    """
    try:
        ib.reqExecutions(ExecutionFilter())
        ib.sleep(1.0)
    except Exception as exc:
        log.warning("could not request executions: %s", exc)

    out = []
    for fill in ib.fills():
        execution, contract = fill.execution, fill.contract
        commission = getattr(fill.commissionReport, "commission", None)
        out.append({
            "exec_id": execution.execId,
            "time": execution.time.isoformat() if execution.time else None,
            "con_id": contract.conId,
            "symbol": contract.symbol,
            "currency": contract.currency,
            "exchange": execution.exchange,
            "side": execution.side,
            "quantity": float(execution.shares),
            "price": float(execution.price),
            "commission": float(commission) if commission is not None else None,
            "source": "tws",
        })
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    snap = fetch()
    acct = snap.account
    print(f"account {acct.account_id}  base {acct.base_currency}")
    print(f"  net liquidation      {acct.net_liquidation:,.2f}")
    print(f"  total cash           {acct.total_cash:,.2f}")
    print(f"  gross position value {acct.gross_position_value:,.2f}")
    print(f"  daily P&L            {acct.daily_pnl}")
    print(f"  fx rates             {acct.exchange_rates}")
    print(f"\n{len(snap.positions)} positions:")
    for p in snap.positions:
        day = f"{p.daily_pnl:>12,.2f}" if p.daily_pnl is not None else "           -"
        print(f"  {p.symbol:<6} {p.currency}  qty {p.quantity:>9,.2f}  "
              f"mv {p.market_value:>13,.2f}  day {day}")
    print(f"\n{len(snap.fills)} fills from TWS")
