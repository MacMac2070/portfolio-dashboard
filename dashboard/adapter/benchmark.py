"""The comparison line on Overview's equity curve: an index beside the NAV.

Adapted from inspiration/shadcn-fintech-abderrahimghazali, whose performance
chart overlays a dashed S&P 500 on the portfolio area. Two things differ here.

The index is not hardcoded. That repo's portfolio is US-only, so one index is
the whole answer; this one is 31% Hong Kong/China, 28% US, 16% Korea, 13%
Japan, 8% Singapore and 3% UK, and no single index describes it. So the pick is
the reader's, drawn from the eleven indices Market watch already tracks, and
FTSE 100 is only the default because it is the one quoted in the currency the
portfolio reports in.

And the arithmetic happens here rather than in the browser. The page receives
a finished series per range and draws it; it does not slice, rebase or divide.

  Currency
  --------
  Rebasing compares growth, not levels, so the index's own currency cancels
  and no FX conversion is involved. That is the standard comparison and it is
  what makes one endpoint serve eleven indices — but it does mean a non-GBP
  index shows the return a local investor earned, not the return a sterling
  investor would have earned after currency moves. For FTSE 100 the two are
  the same, which is the other half of why it is the default. Anything else is
  labelled with its currency so the difference is visible rather than implied.

  Why the ranges are precomputed
  ------------------------------
  The equity curve slices by trailing point count, not by date (see sliceRange
  in js/main.js). Rebasing has to happen after that slice — a 1M line anchored
  to a 1Y starting point would not start where the portfolio line starts — so
  the slice rule lives here too, mirrored from the page, and every range is
  returned already sliced and already rebased. RANGE_DAYS below must stay in
  step with the copy in js/main.js; the page checks the lengths agree and drops
  the benchmark rather than drawing a sheared line if they ever do not.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

log = logging.getLogger("benchmark")

# Mirrors RANGE_DAYS in js/main.js — CALENDAR day spans, anchored on the
# series' own last date. They used to be trailing point counts, which made
# "1M" thirty trading days (five and a half weeks) and "1Y" seventeen months;
# IBKR's own app measures calendar windows, and the two disagreed by whole
# percentage points. "1D" stays a two-row special case (yesterday's close
# against the one before), "ALL" is everything. If you change one side,
# change the other.
RANGE_DAYS: dict[str, float] = {
    "1D": 2, "7D": 7, "1M": 30, "3M": 91, "6M": 182, "1Y": 365, "ALL": float("inf"),
}

DEFAULT_SYMBOL = "^FTSE"

# The anchor has to be worth anchoring to.
#
# nav_history is a record of what the account was worth, not of what it earned,
# and this account was funded progressively: £10 on 13 Oct 2025, then repeated
# top-ups — £7.0k, £6.8k, £10.7k — up to roughly £50k. Over a long window that
# growth is overwhelmingly money going in, not performance.
#
# Rebasing an index against such a window produces a technically correct and
# completely misleading line: anchored at £10 the FTSE ends at £11.54 while the
# portfolio ends at £50,262, so the benchmark is pinned to the axis floor and
# the portfolio appears to have destroyed it. It has not; it has been fed.
#
# So a range is only drawn when the portfolio started it holding at least this
# share of what it ended holding. Below that the window is dominated by flows
# and the series is withheld with a reason rather than drawn as a lie. Once the
# Change in NAV report is being pulled (adapter/flex.py) deposits are known per
# period and this can become a true time-weighted comparison, at which point
# the threshold stops being needed.
MIN_ANCHOR_SHARE = 0.20


def slice_start(dates: list[str], key: str) -> int:
    """Index where a range's window opens — the page's own rule, mirrored.

    Calendar cutoff against the series' last date, so a business-day series
    yields ~22 rows for "1M" whatever holidays fell in it. Returns an index
    rather than a slice because build() cuts three parallel arrays with it.
    Falls back to the last two rows when the window would be shorter.
    """
    if not dates:
        return 0
    days = RANGE_DAYS.get(key, 30)
    if days == float("inf"):
        return 0
    if key == "1D":
        return max(0, len(dates) - 2)
    cutoff = (date.fromisoformat(dates[-1]) - timedelta(days=int(days))).isoformat()
    for i, stamp in enumerate(dates):
        if stamp >= cutoff:
            return min(i, max(0, len(dates) - 2))
    return max(0, len(dates) - 2)


def align(nav_dates: list[str], index_rows: list[tuple[str, float]]) -> list[float | None]:
    """One index close per NAV date, forward-filled, oldest first.

    Venues keep different holidays — the LSE trades on days HKEX does not —
    so the index is carried forward onto the portfolio's dates rather than
    zipped positionally, which would silently shear the two series apart. This
    is the same rule instrument.py applies to the stock page's benchmark.

    Dates before the index's own first close emit None. They are not
    back-filled: an invented earlier value would read as a real one.
    """
    by_date = dict(index_rows)
    out: list[float | None] = []
    carried: float | None = None
    for stamp in nav_dates:
        hit = by_date.get(stamp)
        if hit is not None:
            carried = hit
        out.append(carried)
    return out


def rebase(nav_values: list[float], index_values: list[float | None]) -> list[float | None]:
    """Put the index on the portfolio's axis, anchored where the portfolio starts.

    Both series are pinned at the same point and the index then tracks its own
    growth from there, so the two lines answer "which grew faster" rather than
    being two unrelated magnitudes sharing an axis. One axis, never two scales.

    The anchor is the first date where BOTH series have a usable value, and
    where the portfolio's is non-zero. That last condition is not theoretical:
    nav_history carries 53 leading rows at 0.0 from before the account was
    funded, and anchoring on one of those would divide by zero and flatten the
    whole line. Everything before the anchor emits None.
    """
    anchor = None
    for i, (nav, idx) in enumerate(zip(nav_values, index_values)):
        if idx is None or not idx:
            continue
        if nav is None or not isinstance(nav, (int, float)) or nav <= 0:
            continue
        anchor = i
        break
    if anchor is None:
        return [None] * len(nav_values)

    nav_base = nav_values[anchor]
    idx_base = index_values[anchor]
    out: list[float | None] = []
    for i, idx in enumerate(index_values):
        if i < anchor or idx is None:
            out.append(None)
        else:
            out.append(round(nav_base * (idx / idx_base), 2))
    return out


def _since_inception(nav_rows: list[dict]) -> list[dict]:
    """The series from the account's first real value onward.

    Flex reports back to the start of its reporting year rather than to the
    first position, so this file opens on 2025-07-30 at 0.0 and does not reach
    a real figure until 2025-10-13. Leading only: a zero after inception means
    the account was emptied, which is a real event and stays in the series.
    """
    for i, row in enumerate(nav_rows):
        if row.get("nav_gbp"):
            return nav_rows[i:]
    return []


def fx_adjust(index_rows: list[tuple[str, float]],
              fx_rows: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Index closes restated in sterling: each close × that day's rate into
    GBP, the rate carried forward over a day the FX series lacks and never
    taken from a later one. Returns the input untouched without FX rows."""
    if not index_rows or not fx_rows:
        return index_rows
    fx = sorted(fx_rows)
    out = []
    k = 0
    rate = None
    for day, close in sorted(index_rows):
        while k < len(fx) and fx[k][0] <= day:
            rate = fx[k][1]
            k += 1
        if rate:
            out.append((day, close * rate))
    return out


def build(nav_rows: list[dict], index_rows: list[tuple[str, float]],
          symbol: str, name: str, currency: str,
          fx_rows: list[tuple[str, float]] | None = None) -> dict:
    """Every range at once, each sliced then rebased, ready to draw.

    `nav_rows` is the parsed nav_history: [{date, nav_gbp}, ...] oldest first.
    Returns the same shape whether or not the index had data, so the page has
    one code path and an unwarmed feed is an empty line rather than an error.

    With `fx_rows` — (date, rate into GBP) for the index's currency — the
    line is the index restated in sterling: the return a sterling investor
    would have earned, currency moves included, which is the only honest
    comparison for a NAV reported in pounds. The unhedged line rides along as
    `points_local` for anyone who wants the local-investor view.
    """
    local_rows = index_rows
    adjusted = fx_adjust(index_rows, fx_rows) if currency != "GBP" and fx_rows else []
    index_rows = adjusted or index_rows
    fx_adjusted = bool(adjusted)
    # Drop the leading zero-NAV rows Flex pads the series with, before anything
    # is sliced. The page does the same in `sinceInception` (js/main.js) — these
    # two are deliberate mirrors, and if you change one, change the other. The
    # per-range `count` below is a contract the page checks against its own
    # point count before it will draw the line, so a series that starts on a
    # different row here means the benchmark silently disappears rather than
    # drawing misaligned.
    nav_rows = _since_inception(nav_rows)

    dates = [r["date"] for r in nav_rows]
    values = [r["nav_gbp"] for r in nav_rows]
    carried = align(dates, index_rows)
    carried_local = align(dates, local_rows) if fx_adjusted else None

    ranges: dict[str, dict] = {}
    for key in RANGE_DAYS:
        start = slice_start(dates, key)
        nav_slice = values[start:]
        idx_slice = carried[start:]
        points = rebase(nav_slice, idx_slice)
        points_local = rebase(nav_slice, carried_local[start:]) if carried_local else None

        # Is the anchor representative of the window, or was the account still
        # being filled? See MIN_ANCHOR_SHARE.
        withheld = None
        anchor_i = next((i for i, p in enumerate(points) if p is not None), None)
        if anchor_i is not None:
            closing = next((v for v in reversed(nav_slice) if v), 0.0)
            share = (nav_slice[anchor_i] / closing) if closing else 0.0
            if share < MIN_ANCHOR_SHARE:
                withheld = "funding"
                points = [None] * len(points)
                points_local = [None] * len(points) if points_local else None

        ranges[key] = {
            "points": points,
            "points_local": points_local,
            # The page asserts this against its own slice before drawing.
            "count": len(points),
            "covered": sum(1 for p in points if p is not None),
            # None when the line is drawable. "funding" means the portfolio grew
            # mostly by deposits over this window, so there is nothing honest to
            # compare; the page says so instead of drawing.
            "withheld": withheld,
        }

    return {
        "symbol": symbol,
        "name": name,
        "currency": currency,
        "available": bool(index_rows),
        "as_of": index_rows[-1][0] if index_rows else None,
        "fx_adjusted": fx_adjusted,
        "note": ("rebased to the portfolio's first value in each range; "
                 + (f"restated in GBP at daily rates from {currency}" if fx_adjusted
                    else f"shown in {currency}, unhedged")),
        "ranges": ranges,
    }
