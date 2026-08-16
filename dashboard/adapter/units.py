"""Minor-unit handling, generalised.

Some venues quote in a minor unit. The London names are the ones that bite here:
yfinance reports HSBA.L as 1576.0 GBp — pence — while IBKR reports 15.76 GBp.
`marketdata.py` and `watchlist.py` each carry their own one-line divisor for the
last price. This module is that rule, generalised to every price-shaped field a
stock detail page shows, because the trap is much wider than one field.

Three things make this harder than "divide everything by 100":

**Not every money field is in the minor unit.** Probed on HSBA.L at price 1576:

    last_price / prev_close / open / bid / ask / high / low   1576.0    pence
    year_high / year_low / ma_50d / ma_200d                   pence
    target_low / target_consensus / target_high / target_median  1444.98  pence
    market_cap                              270,255,783,936   POUNDS
    enterprise_value                                          POUNDS
    trailingEps                             0.90              POUNDS
    trailingPE / forwardPE                  17.511 / 11.261   unitless
    dividend_yield                          3.53              ALREADY A PERCENT

So market cap is already major (it is shares x the *major* price), EPS is already
major, and the ratios are unitless because both of their terms are in the same
unit. Confirmed identically on IAG.L. Only per-share quoted prices and analyst
price targets are in pence.

**The currency to test is the quote currency, not the reporting currency.**
`equity.fundamental.metrics.currency` is `financialCurrency` — the currency the
company *reports* in, and it is a different axis:

    HSBA.L   reports USD, quotes GBp
    IAG.L    reports EUR, quotes GBp
    0700.HK  reports CNY, quotes HKD    <- looks like a match, is not

Testing that field would scale the wrong names and miss the right ones. Use
`quote.currency`, `profile.currency` or `consensus.currency`, all of which
correctly say `GBp`. `financials.py` needs the reporting one, and never scales
by it — see that module's docstring.

**An LSE listing does not imply pence.** Verified across the registry:

    HSBA.L  XDJP.L  IAG.L  BARC.L  LLOY.L   GBp   -> divide
    VUSA.L                                  GBP   -> do not
    IUCS.L                                  USD   -> do not

which is why the divisor is resolved per response from the data, never inferred
from the symbol suffix. That is the same rule `marketdata.py:124` already follows;
this module just applies it to more fields.

Deliberately standalone: `marketdata.py` and `watchlist.py` are untouched and keep
their local divisors. They can adopt this later.
"""
from __future__ import annotations

from dataclasses import dataclass

# Quoted minor unit -> (major currency, how many minor units per major).
# Keyed on .upper() so "GBp"/"GBX"/"gbp" all resolve.
MINOR_UNITS: dict[str, tuple[str, float]] = {
    "GBP": ("GBP", 100.0),   # yfinance writes the minor unit as "GBp"
    "GBX": ("GBP", 100.0),   # some providers use the ISO-ish alias
    "ZAC": ("ZAR", 100.0),   # South African cents
    "ILA": ("ILS", 100.0),   # Israeli agorot
}

# Per-share quoted prices and analyst price targets. These, and only these, carry
# the minor unit. Everything else in a quote/metrics/consensus row is either
# already in the major unit or unitless.
PRICE_SHAPED = frozenset({
    "last_price", "prev_close", "open", "high", "low", "close",
    "bid", "ask", "year_high", "year_low", "ma_50d", "ma_200d",
    "target_low", "target_high", "target_consensus", "target_median",
    "after_hours_price",
})

# Named so the next reader does not "fix" them. Each was probed on HSBA.L.
ALREADY_MAJOR = frozenset({
    "market_cap",        # shares x major price
    "enterprise_value",
    "eps_ttm",           # trailingEps is in pounds
    "eps_forward",
    "book_value",
})


@dataclass(frozen=True)
class PriceScale:
    """How to turn a provider's quoted price into the major unit."""
    quoted: str          # what the provider said, e.g. "GBp"
    major: str           # what to display it as, e.g. "GBP"
    divisor: float       # 100.0 for pence, 1.0 for everything else

    @property
    def minor(self) -> bool:
        return self.divisor != 1.0


def scale_for(currency: str | None, fallback: str | None = None) -> PriceScale:
    """Resolve the scale from a quote currency.

    Callers should pass, in order of preference, `quote.currency`,
    `profile.currency`, `consensus.currency`, then the registry's own
    `Ticker.currency` as `fallback`. **Never pass `metrics.currency`** — see the
    module docstring; it is the reporting currency and is wrong for this.
    """
    code = (currency or fallback or "").strip()
    if not code:
        return PriceScale("", "", 1.0)

    # Only the exact minor-unit spellings scale. "GBP" from a provider that means
    # pounds would collide with the "GBP" key, so the test is case-sensitive on
    # the first pass and only falls back to the table for a known minor spelling.
    if code in ("GBp", "GBX", "GBx", "ZAc", "ILa"):
        major, divisor = MINOR_UNITS[code.upper()]
        return PriceScale(code, major, divisor)
    return PriceScale(code, code, 1.0)


def price(value, scale: PriceScale):
    """One quoted price into the major unit. Passes through None and non-numbers."""
    if value is None or scale.divisor == 1.0:
        return value
    try:
        return float(value) / scale.divisor
    except (TypeError, ValueError):
        return value


def series(values, scale: PriceScale):
    """A close/OHLC series into the major unit."""
    if not values or scale.divisor == 1.0:
        return values
    return [price(v, scale) for v in values]


def scale_record(record: dict, scale: PriceScale) -> dict:
    """Copy of `record` with every PRICE_SHAPED key scaled and nothing else.

    Unknown keys are left alone on purpose — a provider adding a field should not
    silently start being divided by 100.
    """
    if scale.divisor == 1.0:
        return dict(record)
    return {
        k: (price(v, scale) if k in PRICE_SHAPED else v)
        for k, v in record.items()
    }


def display_currency(scale: PriceScale, fallback: str = "") -> str:
    """What the UI should print next to a scaled figure — 'GBP', never 'GBp'."""
    return scale.major or fallback


if __name__ == "__main__":
    # The HSBA.L probe, as a regression check.
    s = scale_for("GBp")
    assert s.minor and s.divisor == 100.0, s
    row = {
        "last_price": 1576.0, "target_consensus": 1444.98,
        "market_cap": 270_255_783_936, "eps_ttm": 0.90, "pe_ratio": 17.511,
    }
    out = scale_record(row, s)
    assert out["last_price"] == 15.76, out
    assert round(out["target_consensus"], 4) == 14.4498, out
    assert out["market_cap"] == 270_255_783_936, "market cap is already major"
    assert out["eps_ttm"] == 0.90, "trailingEps is already major"
    assert out["pe_ratio"] == 17.511, "ratios are unitless"

    for code, expect in (("GBp", 100.0), ("GBP", 1.0), ("USD", 1.0), ("HKD", 1.0), (None, 1.0)):
        got = scale_for(code).divisor
        assert got == expect, f"{code!r} -> {got}, expected {expect}"

    print("units.py: all checks pass")
    print(f"  GBp 1576.0 -> {price(1576.0, s)}")
    print(f"  display currency -> {display_currency(s)}")
