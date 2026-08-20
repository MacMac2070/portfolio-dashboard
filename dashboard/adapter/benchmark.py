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

log = logging.getLogger("benchmark")

# Mirrors RANGE_DAYS in js/main.js. Trailing point counts, not calendar days:
# the series is one point per recorded day, so a gap in recording shortens the
# window rather than emptying it.
RANGE_DAYS: dict[str, float] = {
    "1D": 2, "7D": 7, "1M": 30, "3M": 90, "6M": 180, "1Y": 365, "ALL": float("inf"),
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


def slice_range(points: list, key: str) -> list:
    """The page's own slice, so the two arrays cannot come out different lengths."""
    days = RANGE_DAYS.get(key, 30)
    if days == float("inf"):
        return list(points)
    return list(points[-max(2, int(days)):])


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


def build(nav_rows: list[dict], index_rows: list[tuple[str, float]],
          symbol: str, name: str, currency: str) -> dict:
    """Every range at once, each sliced then rebased, ready to draw.

    `nav_rows` is the parsed nav_history: [{date, nav_gbp}, ...] oldest first.
    Returns the same shape whether or not the index had data, so the page has
    one code path and an unwarmed feed is an empty line rather than an error.
    """
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

    ranges: dict[str, dict] = {}
    for key in RANGE_DAYS:
        nav_slice = slice_range(values, key)
        idx_slice = slice_range(carried, key)
        points = rebase(nav_slice, idx_slice)

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

        ranges[key] = {
            "points": points,
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
        "note": ("rebased to the portfolio's first value in each range; "
                 f"shown in {currency}, unhedged"),
        "ranges": ranges,
    }
