"""Track record: the derivations behind the Performance page's second act.

Everything is computed from the two stores the Flex backfill already writes —
daily NAV and dated cash flows — plus, for the monthly benchmark row, whatever
index history the market feed holds. Nothing here fetches.

The one number everything shares is the funding-aware daily return:

    r_t = (NAV_t − NAV_{t−1} − flow_t) / (NAV_{t−1} + max(flow_t, 0))

Deposits are excluded from performance by construction — a day the account
grew only because money was paid in is a flat day, not a good one. The
denominator carries an inflow so a deposit landing mid-day is not credited
with its own market move at full leverage; the approximation error over daily
bars is negligible against this book's flow sizes.

The monthly grid deliberately does NOT come from this series: it reads the
Flex Change-in-NAV sub-periods, which are IBKR's own audited monthly figures.
The grid is the one view guaranteed to match the broker's statement; the daily
series is ours. Both are honest, and they answer different questions.
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from flex import MONTH_SPAN_DAYS
from store import CASH_PATH, NAV_CHANGE_PATH, NAV_PATH, _read

# Benchmark symbol used for the grid's comparison row when the page does not
# name one. Matches benchmark.DEFAULT_SYMBOL's spirit without importing the
# whole module for a constant.
DEFAULT_BENCHMARK = "^FTSE"


# ---------------------------------------------------------------- daily series

def daily_series() -> list[dict]:
    """[{date, nav, flow, pnl, r}], oldest first, from inception onward."""
    nav_rows = [r for r in _read(NAV_PATH)
                if r.get("date") and isinstance(r.get("nav_gbp"), (int, float))]
    nav_rows.sort(key=lambda r: r["date"])
    # Leading zero rows are Flex's reporting-year padding, not history.
    while nav_rows and not nav_rows[0]["nav_gbp"]:
        nav_rows.pop(0)
    if len(nav_rows) < 2:
        return []

    flows: dict[str, float] = {}
    for row in _read(CASH_PATH):
        if row.get("bucket") == "deposits_withdrawals" and row.get("date"):
            flows[row["date"]] = flows.get(row["date"], 0.0) + (row.get("amount_gbp") or 0.0)

    flow_dates = sorted(flows)
    out: list[dict] = []
    prev = nav_rows[0]
    for row in nav_rows[1:]:
        # Flows stamped after the previous NAV date up to and including this
        # one. NAV dates skip weekends; a Saturday deposit belongs to Monday.
        flow = sum(flows[d] for d in flow_dates if prev["date"] < d <= row["date"])
        base = prev["nav_gbp"] + max(flow, 0.0)
        pnl = row["nav_gbp"] - prev["nav_gbp"] - flow
        out.append({
            "date": row["date"],
            "nav": round(row["nav_gbp"], 2),
            "flow": round(flow, 2),
            "pnl": round(pnl, 2),
            "r": (pnl / base) if base > 0 else 0.0,
        })
        prev = row
    return out


# ------------------------------------------------------------------- drawdown

def drawdown(series: list[dict]) -> dict:
    """Underwater curve plus the ranked episode table."""
    idx = 1.0
    peak = 1.0
    peak_date = series[0]["date"] if series else None
    curve: list[dict] = []
    episodes: list[dict] = []
    open_ep: dict | None = None

    for row in series:
        idx *= (1.0 + row["r"])
        if idx >= peak:
            if open_ep:
                open_ep["recovered"] = row["date"]
                episodes.append(open_ep)
                open_ep = None
            peak, peak_date = idx, row["date"]
            dd = 0.0
        else:
            dd = idx / peak - 1.0
            if open_ep is None:
                open_ep = {"peak_date": peak_date, "trough_date": row["date"],
                           "depth": dd, "recovered": None}
            elif dd < open_ep["depth"]:
                open_ep["depth"] = dd
                open_ep["trough_date"] = row["date"]
        curve.append({"date": row["date"], "dd": round(dd, 5)})

    if open_ep:
        episodes.append(open_ep)     # still underwater

    def days(a: str, b: str) -> int:
        return (date.fromisoformat(b) - date.fromisoformat(a)).days

    for ep in episodes:
        ep["depth"] = round(ep["depth"], 4)
        ep["days_down"] = days(ep["peak_date"], ep["trough_date"])
        ep["days_total"] = days(ep["peak_date"], ep["recovered"] or series[-1]["date"])

    episodes.sort(key=lambda e: e["depth"])
    current = curve[-1]["dd"] if curve else 0.0
    return {
        "curve": curve,
        "episodes": episodes[:6],
        "current": current,
        # The live all-time-high reading: when the last new high was set, and
        # how long the account has been under it. peak_date already tracks the
        # last new high, so this is bookkeeping, not new arithmetic.
        "ath_date": peak_date,
        "days_underwater": days(peak_date, series[-1]["date"])
                           if curve and current < 0 else 0,
    }


# --------------------------------------------------------------- days & streaks

def day_stats(series: list[dict]) -> dict:
    ranked = sorted(series, key=lambda r: r["r"])
    ups = sum(1 for r in series if r["r"] > 0)

    best_streak = worst_streak = current = 0
    best_run = worst_run = 0
    for row in series:
        if row["r"] > 0:
            current = current + 1 if current > 0 else 1
        elif row["r"] < 0:
            current = current - 1 if current < 0 else -1
        else:
            current = 0
        best_run = max(best_run, current)
        worst_run = min(worst_run, current)

    trim = lambda r: {"date": r["date"], "r": round(r["r"], 5), "pnl": r["pnl"]}
    return {
        "best": [trim(r) for r in ranked[-5:]][::-1],
        "worst": [trim(r) for r in ranked[:5]],
        "win_rate": round(ups / len(series), 4) if series else None,
        "n": len(series),
        "best_streak": best_run,
        "worst_streak": worst_run,
        "current_streak": current,
    }


# --------------------------------------------------------------- monthly grid

def monthly_grid(index_history=None, benchmark_symbol: str | None = None) -> dict:
    """Signed monthly % anchored to IBKR's own Change-in-NAV sub-periods."""
    rows = [r for r in _read(NAV_CHANGE_PATH)
            if (r.get("span_days") or 0) <= MONTH_SPAN_DAYS
            and (r.get("from_date") or "")[:7]]
    months: dict[str, float | None] = {}
    for r in sorted(rows, key=lambda r: r["from_date"]):
        month = r["from_date"][:7]
        pnl = (r.get("mtm", 0.0) + r.get("realized", 0.0)
               + r.get("changeInUnrealized", 0.0))
        starting = r.get("startingValue", 0.0)
        # A month whose opening balance was ~zero (the account's first) has no
        # meaningful percentage; the cell renders an honest dash.
        months[month] = round(pnl / starting, 5) if starting > 100 else None

    bench: dict[str, float] = {}
    symbol = benchmark_symbol or DEFAULT_BENCHMARK
    if index_history is not None and months:
        try:
            closes = index_history(symbol) or []
            # last close per month -> month-over-month return
            eom: dict[str, float] = {}
            for stamp, close in closes:
                eom[str(stamp)[:7]] = float(close)
            keys = sorted(eom)
            for prev_m, this_m in zip(keys, keys[1:]):
                if this_m in months:
                    bench[this_m] = round(eom[this_m] / eom[prev_m] - 1.0, 5)
        except Exception:
            bench = {}      # benchmark row is optional context, never an error

    return {"months": months, "benchmark": bench, "benchmark_symbol": symbol}


# ---------------------------------------------------------------------- stats

def _compound(rows) -> float:
    p = 1.0
    for r in rows:
        p *= (1.0 + r["r"])
    return p - 1.0


def _xirr(flows: list[tuple[str, float]]) -> float | None:
    """Annualized money-weighted return from dated GBP flows.

    Investor's sign convention: capital in is negative, capital out positive,
    and the final entry is the closing NAV closing the position. Bisection on
    [-99.99%, +10,000%] rather than Newton: it cannot diverge, and 200 halvings
    resolve far past the two decimals anyone prints. None when the flows do not
    bracket a root (all-in or all-out histories have no rate to find).
    """
    if len(flows) < 2:
        return None
    t0 = date.fromisoformat(flows[0][0])
    times = [(date.fromisoformat(d) - t0).days / 365.25 for d, _ in flows]
    amounts = [a for _, a in flows]
    if not (any(a < 0 for a in amounts) and any(a > 0 for a in amounts)):
        return None

    def npv(rate: float) -> float:
        return sum(a / (1.0 + rate) ** t for a, t in zip(amounts, times))

    lo, hi = -0.9999, 100.0
    f_lo = npv(lo)
    if f_lo * npv(hi) > 0:
        return None
    mid = 0.0
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-9:
            break
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return mid


def stats(series: list[dict], index_history=None,
          benchmark_symbol: str | None = None) -> dict | None:
    """The verdict block: period returns and benchmark-relative ratios.

    Period returns compound the funding-aware daily series, so a deposit can
    never masquerade as performance. Ratios are computed only when at least 60
    aligned portfolio/benchmark observations exist — below that a beta is a
    coin toss wearing two decimals, and the masthead prints em-dashes instead.
    Sharpe uses rf = 0 and says so in the UI label.
    """
    if len(series) < 5:
        return None
    today = series[-1]["date"]
    mtd_rows = [r for r in series if r["date"] >= f"{today[:7]}-01"]
    ytd_rows = [r for r in series if r["date"] >= f"{today[:4]}-01-01"]

    out: dict = {
        "asof": today,
        "si": round(_compound(series), 5),
        "mtd": round(_compound(mtd_rows), 5),
        "ytd": round(_compound(ytd_rows), 5),
        "benchmark_symbol": benchmark_symbol,
        "bench": None,
        "ratios": None,
        "mwr": None,
        "risk": None,
    }

    # Money-weighted return: what the money actually in the account earned,
    # deposits timed and weighted — TWR's complement, not its competitor. The
    # opening base of the first series row recovers the pre-series NAV, so the
    # starting capital is the first "deposit".
    span_years = max((date.fromisoformat(series[-1]["date"])
                      - date.fromisoformat(series[0]["date"])).days, 1) / 365.25
    first = series[0]
    # daily_series() rows carry nav/flow; a caller handing bare returns (the
    # tests do) simply gets no money-weighted figure rather than a KeyError.
    if all(k in first for k in ("nav", "pnl", "flow")) and "nav" in series[-1]:
        nav_start = first["nav"] - first["pnl"] - first["flow"]
        flows: list[tuple[str, float]] = [(first["date"], -nav_start)]
        flows += [(r["date"], -r["flow"]) for r in series if r["flow"]]
        flows.append((series[-1]["date"], series[-1]["nav"]))
        rate = _xirr(flows)
        if rate is not None:
            out["mwr"] = {
                # The period figure sits beside the since-inception TWR; the
                # annualized one belongs in the explainer with its short-window
                # caveat, not on the rail.
                "period": round((1.0 + rate) ** span_years - 1.0, 5),
                "ann": round(rate, 5),
                "flows": len(flows),
            }

    # Sortino, Calmar and max drawdown need only the portfolio's own series —
    # unlike alpha/beta they never wait for a benchmark, only for the same 60
    # observations that make any of these ratios worth two decimals.
    if len(series) >= 60:
        rs = [r["r"] for r in series]
        mean_r = sum(rs) / len(rs)
        downside = (sum(min(r, 0.0) ** 2 for r in rs) / len(rs)) ** 0.5
        idx = peak = 1.0
        maxdd = 0.0
        for r in rs:
            idx *= 1.0 + r
            peak = max(peak, idx)
            maxdd = min(maxdd, idx / peak - 1.0)
        ann_return = (1.0 + _compound(series)) ** (1.0 / span_years) - 1.0
        out["risk"] = {
            "sortino": round((mean_r / downside) * (252 ** 0.5), 2) if downside > 0 else None,
            "calmar": round(ann_return / -maxdd, 2) if maxdd < 0 else None,
            "max_dd": round(maxdd, 4),
        }

    if index_history is None or not benchmark_symbol:
        return out
    try:
        closes = sorted(index_history(benchmark_symbol) or [])
    except Exception:
        return out
    if len(closes) < 2:
        return out

    bret: dict[str, float] = {}
    for (d0, c0), (d1, c1) in zip(closes, closes[1:]):
        if c0:
            bret[str(d1)[:10]] = float(c1) / float(c0) - 1.0

    def bench_compound(rows) -> float | None:
        vals = [bret[r["date"]] for r in rows if r["date"] in bret]
        if len(vals) < max(2, len(rows) // 2):
            return None                 # too sparse to call it the same period
        p = 1.0
        for v in vals:
            p *= (1.0 + v)
        return p - 1.0

    b_si, b_mtd, b_ytd = (bench_compound(x) for x in (series, mtd_rows, ytd_rows))
    out["bench"] = {
        "si": round(b_si, 5) if b_si is not None else None,
        "mtd": round(b_mtd, 5) if b_mtd is not None else None,
        "ytd": round(b_ytd, 5) if b_ytd is not None else None,
    }

    aligned = [(r["r"], bret[r["date"]]) for r in series if r["date"] in bret]
    if len(aligned) >= 60:
        n = len(aligned)
        mp = sum(a for a, _ in aligned) / n
        mb = sum(b for _, b in aligned) / n
        var_b = sum((b - mb) ** 2 for _, b in aligned) / n
        cov = sum((a - mp) * (b - mb) for a, b in aligned) / n
        var_p = sum((a - mp) ** 2 for a, _ in aligned) / n
        sd_p = var_p ** 0.5
        beta = cov / var_b if var_b > 0 else None
        out["ratios"] = {
            "beta": round(beta, 2) if beta is not None else None,
            "alpha_ann": round((mp - (beta or 0) * mb) * 252, 4) if beta is not None else None,
            "vol_ann": round(sd_p * (252 ** 0.5), 4),
            "sharpe": round((mp / sd_p) * (252 ** 0.5), 2) if sd_p > 0 else None,
            "n": n,
        }
    return out


# ---------------------------------------------------------------------- build

def build(index_history=None, benchmark_symbol: str | None = None) -> dict:
    series = daily_series()
    ready = len(series) >= 5
    return {
        "ready": ready,
        # Where the flows that make the return time-weighted came from. "none"
        # with deposits on the books means every deposit counts as return —
        # reconcile.py's flows.deposits check is what catches that, and
        # serve.py lowers `ready` on its verdict.
        "flows_source": "cash_transactions" if any(r["flow"] for r in series) else "none",
        "inception": series[0]["date"] if series else None,
        "daily": [{"date": r["date"], "r": round(r["r"], 5), "pnl": r["pnl"]}
                  for r in series],
        "drawdown": drawdown(series) if ready else None,
        "days": day_stats(series) if ready else None,
        "monthly": monthly_grid(index_history, benchmark_symbol),
        "stats": stats(series, index_history, benchmark_symbol or DEFAULT_BENCHMARK),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


if __name__ == "__main__":
    import json
    out = build()
    print("ready:", out["ready"], "| days:", len(out["daily"]),
          "| inception:", out["inception"])
    if out["drawdown"]:
        print("current dd: %.2f%%" % (out["drawdown"]["current"] * 100))
        for ep in out["drawdown"]["episodes"][:3]:
            print("  episode", json.dumps(ep))
    if out["days"]:
        d = out["days"]
        print("win rate: %.1f%% of %d · best day %s %.2f%%" % (
            d["win_rate"] * 100, d["n"], d["best"][0]["date"], d["best"][0]["r"] * 100))
    print("monthly cells:", {k: (round(v * 100, 2) if v is not None else None)
                             for k, v in out["monthly"]["months"].items()})
