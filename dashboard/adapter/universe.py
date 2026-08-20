"""The ticker registry and the sectors that reference it.

One rule shapes this file: **ownership is a property of the ticker, not of the
sector it appears in.** A ticker carries its own `con_id`; having one *is*
ownership. Sectors hold nothing but references, so a name reads identically
wherever it turns up and no sector re-implements the distinction.

`con_id` values come from IBKR and match `marketdata.QUOTE_SYMBOLS`, which
already maps them to yfinance symbols — that mapping is the bridge between what
the IB feed reports and what openbb quotes.

Each row shows the issuer's own mark, served as SVG from a public symbol-logo
service and displayed for identification only. `brand` is the issuer's hue,
used for the ticker monogram that stands in wherever no mark is published — the
tile is a light plate because marks are drawn for light ground.

Sector is a *separate axis from region*. `regions.py` drives Overview's
allocation donut and answers "where is this exposure"; sector answers "what kind
of business is this". XDJP is Japan by region and an ETF by sector, and both are
correct.

Venue is a *third* axis, in `_VENUE` below: where the line actually lists, and
what it quotes in. It is kept out of the `Ticker(...)` rows so those stay
readable, and because it answers a different question again — XDJP lists on LSE
in GBp, is Japan by region, and is an ETF by sector, and all three are correct.
`regions.py` only covers the 15 held contracts (it is keyed on conId); `_VENUE`
covers all 35, which is what a stock detail page needs. The self-check at the
bottom asserts the two agree wherever they overlap.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

SEMIS = "Semiconductors"
BIGTECH = "Big tech"
ETFS = "ETFs"
AIRLINES = "Airlines"
FINANCIALS = "Financials"
# Where a name added from the search bar lands. Nothing curated is ever filed
# here — it exists so a symbol you chose to watch has somewhere to live without
# a person first deciding which of the five it belongs to.
OTHER = "Other"

# Display order of the sector pills. OTHER last, and hidden while it is empty.
SECTOR_ORDER = (SEMIS, BIGTECH, ETFS, AIRLINES, FINANCIALS, OTHER)

# The monogram hue for a name with no curated brand colour. The project violet,
# so an auto-added tile reads as part of the system rather than as a stray.
DEFAULT_BRAND = "#7C55E8"


LOGO_BASE = "https://assets.parqet.com/logos/symbol/"


# Canonical venue code -> the label a stock page prints in its sub-line.
# Codes are yfinance's own `exchange` values; the two labels that differ from
# yfinance's `fullExchangeName` are marked, because they read better as the
# exchange's own name than as the provider's abbreviation.
EXCHANGE_NAMES: dict[str, str] = {
    "NMS": "NasdaqGS",     # yfinance's fullExchangeName verbatim
    "NYQ": "NYSE",
    "HKG": "HKEX",         # yfinance says "HKSE"
    "SES": "SGX",          # yfinance says "SES"
    "LSE": "LSE",
    "FRA": "Frankfurt",
    "IOB": "LSE IOB",      # International Order Book — the SMSN line
}

# IBKR's primaryExchange vocabulary -> the canonical code above, so a venue that
# reaches the UI from the live feed renders the same label as one that reaches it
# from this registry. The feed's raw values are LSEETF / LSEIOB1 / SEHK / FWB.
IBKR_EXCHANGE_ALIASES: dict[str, str] = {
    "NASDAQ": "NMS", "SEHK": "HKG", "SGX": "SES",
    "LSE": "LSE", "LSEETF": "LSE", "LSEIOB1": "IOB", "FWB": "FRA",
    "NYSE": "NYQ", "ARCA": "NYQ", "BATS": "NYQ", "AMEX": "NYQ",
}

# Venue codes that only ever arrive from a Yahoo lookup — the wider market the
# search bar can reach but the curated registry never names. Kept separate from
# EXCHANGE_NAMES above so the curated table stays a short, reviewable list, and
# merged into it below. exchange_display() passes an unknown code through
# unchanged, so a gap here degrades to the raw code and never breaks a page.
_REMOTE_EXCHANGE_NAMES = {
    "NGM": "NasdaqGM", "NCM": "NasdaqCM", "NYS": "NYSE", "ASE": "NYSE American",
    "PCX": "NYSE Arca", "BTS": "Cboe BZX", "PNK": "OTC Markets",
    "OQB": "OTCQB", "OQX": "OTCQX", "OTC": "OTC Markets",
    "KSC": "KRX", "KOE": "KOSDAQ", "TOR": "Toronto", "VAN": "TSX Venture",
    "AMS": "Euronext Amsterdam", "PAR": "Euronext Paris", "BRU": "Euronext Brussels",
    "LIS": "Euronext Lisbon", "EBS": "SIX Swiss", "VIE": "Wiener Börse",
    "GER": "XETRA", "STU": "Stuttgart", "MUN": "Munich", "DUS": "Düsseldorf",
    "BER": "Berlin", "HAM": "Hamburg", "MIL": "Borsa Italiana", "MCE": "BME",
    "STO": "Nasdaq Stockholm", "CPH": "Nasdaq Copenhagen", "HEL": "Nasdaq Helsinki",
    "OSL": "Oslo Børs", "ICE": "Nasdaq Iceland",
    "TAI": "TWSE", "TWO": "TPEx", "JPX": "TSE", "SHH": "SSE", "SHZ": "SZSE",
    "ASX": "ASX", "NZE": "NZX", "BSE": "BSE", "NSI": "NSE", "SAO": "B3",
    "MEX": "BMV", "BUE": "BYMA", "SGO": "Santiago", "JNB": "JSE", "TLV": "TASE",
    "DXE": "Cboe Europe", "AQS": "Aquis", "CCS": "Caracas",
}
EXCHANGE_NAMES.update(_REMOTE_EXCHANGE_NAMES)


def exchange_display(code: str | None) -> str:
    """Venue code -> display label. Unknown codes pass through unchanged.

    Accepts either vocabulary, so `exchange_display(position["exchange"])` from
    the IB feed and `ticker.exchange_display` from this registry agree.
    """
    if not code:
        return ""
    canon = IBKR_EXCHANGE_ALIASES.get(code.upper(), code.upper())
    return EXCHANGE_NAMES.get(canon, code)


@dataclass(frozen=True)
class Ticker:
    key: str            # stable id used by the UI
    symbol: str         # yfinance symbol, for anything IB does not cover
    name: str
    sector: str
    brand: str          # issuer hue, for the monogram fallback
    con_id: int | None = None   # present => you hold it => live IB data
    # Symbol the logo service knows this issuer by, when it differs from the
    # quote symbol — HY9H's mark is filed under its Korean primary listing.
    # A full https:// URL here overrides the service entirely.
    logo: str | None = None
    # Filled from _VENUE when TICKERS is built — never passed positionally.
    exchange: str = ""  # canonical venue code, see EXCHANGE_NAMES
    currency: str = ""  # quote currency; the GBp fallback when a provider omits it
    region: str = ""    # regions.py vocabulary — exposure, not listing venue

    @property
    def owned(self) -> bool:
        return self.con_id is not None

    @property
    def exchange_display(self) -> str:
        return exchange_display(self.exchange)

    @property
    def mono(self) -> str:
        """Three alphanumerics — the fallback shown when no mark is published.

        A directory key carries its venue suffix ("0700.HK"), which would
        otherwise monogram as "070". Drop the suffix, and drop the padding zeros
        Yahoo puts on Hong Kong tickers, so it reads "700" like the curated row
        for the same company. No curated monogram changes.
        """
        head = self.key.split(".")[0]
        if head.isdigit():
            head = head.lstrip("0") or head
        return "".join(c for c in head if c.isalnum())[:3].upper()

    @property
    def logo_url(self) -> str:
        """The issuer's own mark, as SVG so it stays crisp at any tile size.

        Verified 31 Jul: 33 of 35 symbols resolve directly from the service.
        The two that 404 carry an explicit override below.
        """
        ref = self.logo or self.symbol
        return ref if ref.startswith("http") else f"{LOGO_BASE}{ref}?format=svg"


# --------------------------------------------------------------------------
# Held positions. con_id ties each to the live IB feed.
# --------------------------------------------------------------------------
_HELD = [
    Ticker("293",   "0293.HK", "Cathay Pacific Airways",           AIRLINES,   "#006564", 1616420),
    Ticker("C6L",   "C6L.SI",  "Singapore Airlines",               AIRLINES,   "#F5A800", 92216536),

    Ticker("700",   "0700.HK", "Tencent Holdings",                 BIGTECH,    "#1E7BEE", 152791428),
    Ticker("AAPL",  "AAPL",    "Apple",                            BIGTECH,    "#A2AAAD", 265598),
    Ticker("AMZN",  "AMZN",    "Amazon.com",                       BIGTECH,    "#FF9900", 3691937),
    Ticker("GOOGL", "GOOGL",   "Alphabet",                         BIGTECH,    "#4285F4", 208813719),
    Ticker("META",  "META",    "Meta Platforms",                   BIGTECH,    "#0064E0", 107113386),

    Ticker("HY9H",  "HY9H.F",  "SK hynix · GDR",                   SEMIS,      "#EA002C", 517397504,
           logo="000660.KS"),   # mark is filed under the Korean primary listing
    Ticker("INTC",  "INTC",    "Intel",                            SEMIS,      "#0068B5", 270639),
    Ticker("SMSN",  "SMSN.IL", "Samsung Electronics · GDR",        SEMIS,      "#1428A0", 16520545),

    # 3115.HK and ES3.SI both 404 on the logo service, so they carry the
    # design's hand-picked marks for the index provider and the fund manager.
    Ticker("3115",  "3115.HK", "iShares Core Hang Seng Index ETF", ETFS,       "#C8102E", 256718140,
           logo="https://s3-symbol-logo.tradingview.com/hang-seng-bank--big.svg"),
    Ticker("ES3",   "ES3.SI",  "SPDR Straits Times Index ETF",     ETFS,       "#0072CE", 92214874,
           logo="https://s3-symbol-logo.tradingview.com/state-street--big.svg"),
    Ticker("IUCS",  "IUCS.L",  "iShares S&P 500 Consumer Staples", ETFS,       "#6E7B8B", 270617971),
    Ticker("XDJP",  "XDJP.L",  "Xtrackers Nikkei 225 UCITS ETF",   ETFS,       "#0018A8", 123279007),

    Ticker("HSBA",  "HSBA.L",  "HSBC Holdings",                    FINANCIALS, "#DB0011", 909083),
]

# --------------------------------------------------------------------------
# Watchlist-only. No con_id, so no quantity, cost or P&L is ever shown.
# --------------------------------------------------------------------------
_WATCHED = [
    Ticker("MU",    "MU",      "Micron Technology",                SEMIS,      "#0084C9"),
    # Relisted after the Western Digital spinoff — verify before assuming stale.
    Ticker("SNDK",  "SNDK",    "Sandisk",                          SEMIS,      "#E31937"),
    Ticker("AMD",   "AMD",     "Advanced Micro Devices",           SEMIS,      "#ED1C24"),
    Ticker("NVDA",  "NVDA",    "NVIDIA",                           SEMIS,      "#76B900"),
    Ticker("TSM",   "TSM",     "Taiwan Semiconductor",             SEMIS,      "#C41230"),
    Ticker("ASML",  "ASML",    "ASML Holding",                     SEMIS,      "#1A5FD0"),
    Ticker("QCOM",  "QCOM",    "QUALCOMM",                         SEMIS,      "#3253DC"),
    Ticker("AVGO",  "AVGO",    "Broadcom",                         SEMIS,      "#CC092F"),

    Ticker("MSFT",  "MSFT",    "Microsoft",                        BIGTECH,    "#00A4EF"),
    Ticker("NFLX",  "NFLX",    "Netflix",                          BIGTECH,    "#E50914"),
    Ticker("TSLA",  "TSLA",    "Tesla",                            BIGTECH,    "#CC0000"),
    Ticker("BABA",  "9988.HK", "Alibaba Group",                    BIGTECH,    "#FF6A00"),
    Ticker("BIDU",  "9888.HK", "Baidu",                            BIGTECH,    "#2932E1"),
    Ticker("JD",    "9618.HK", "JD.com",                           BIGTECH,    "#D22630"),
    Ticker("NTES",  "9999.HK", "NetEase",                          BIGTECH,    "#D6000F"),
    # The one Chinese name with no HK line, so it stays on Nasdaq in USD.
    Ticker("PDD",   "PDD",     "PDD Holdings",                     BIGTECH,    "#E02E24"),

    # Quotes thinly — see the history fallback in watchlist.py.
    Ticker("VUSA",  "VUSA.L",  "Vanguard S&P 500 UCITS ETF",       ETFS,       "#96151D"),

    Ticker("IAG",   "IAG.L",   "Intl. Consolidated Airlines",      AIRLINES,   "#2E5AA8"),

    Ticker("BARC",  "BARC.L",  "Barclays",                         FINANCIALS, "#00AEEF"),
    Ticker("LLOY",  "LLOY.L",  "Lloyds Banking Group",             FINANCIALS, "#006A4D"),
]

# --------------------------------------------------------------------------
# Venue axis: key -> (exchange code, quote currency, region).
#
# Every value probed against the provider on 1 Aug 2026 rather than inferred
# from the symbol suffix, because the suffix lies in both directions:
#
#     TSM    is NYSE, not Nasdaq, despite reading like a US tech ticker
#     IUCS.L is an LSE line quoting in USD
#     VUSA.L is an LSE line quoting in GBP (pounds)
#     XDJP.L is an LSE line quoting in GBp (pence)
#
# so the three LSE ETFs need three different currencies. See units.py.
#
# Region follows regions.py's rule — underlying exposure, not listing venue.
# Taiwan and Netherlands are new here and are deliberately absent from
# regions.REGION_ORDER: both are watchlist-only, so neither can reach the
# allocation donut. They exist so the stock page can pick a benchmark.
# --------------------------------------------------------------------------
_VENUE: dict[str, tuple[str, str, str]] = {
    # held
    "293":   ("HKG", "HKD", "Hong Kong / China"),
    "C6L":   ("SES", "SGD", "Singapore"),
    "700":   ("HKG", "HKD", "Hong Kong / China"),
    "AAPL":  ("NMS", "USD", "United States"),
    "AMZN":  ("NMS", "USD", "United States"),
    "GOOGL": ("NMS", "USD", "United States"),
    "META":  ("NMS", "USD", "United States"),
    "HY9H":  ("FRA", "EUR", "South Korea"),
    "INTC":  ("NMS", "USD", "United States"),
    "SMSN":  ("IOB", "USD", "South Korea"),
    "3115":  ("HKG", "HKD", "Hong Kong / China"),
    "ES3":   ("SES", "SGD", "Singapore"),
    "IUCS":  ("LSE", "USD", "United States"),
    "XDJP":  ("LSE", "GBp", "Japan"),
    "HSBA":  ("LSE", "GBp", "United Kingdom"),
    # watchlist
    "MU":    ("NMS", "USD", "United States"),
    "SNDK":  ("NMS", "USD", "United States"),
    "AMD":   ("NMS", "USD", "United States"),
    "NVDA":  ("NMS", "USD", "United States"),
    "TSM":   ("NYQ", "USD", "Taiwan"),
    "ASML":  ("NMS", "USD", "Netherlands"),
    "QCOM":  ("NMS", "USD", "United States"),
    "AVGO":  ("NMS", "USD", "United States"),
    "MSFT":  ("NMS", "USD", "United States"),
    "NFLX":  ("NMS", "USD", "United States"),
    "TSLA":  ("NMS", "USD", "United States"),
    "BABA":  ("HKG", "HKD", "Hong Kong / China"),
    "BIDU":  ("HKG", "HKD", "Hong Kong / China"),
    "JD":    ("HKG", "HKD", "Hong Kong / China"),
    "NTES":  ("HKG", "HKD", "Hong Kong / China"),
    "PDD":   ("NMS", "USD", "Hong Kong / China"),
    "VUSA":  ("LSE", "GBP", "United States"),
    "IAG":   ("LSE", "GBp", "United Kingdom"),
    "BARC":  ("LSE", "GBp", "United Kingdom"),
    "LLOY":  ("LSE", "GBp", "United Kingdom"),
}


def _with_venue(t: Ticker) -> Ticker:
    """Attach the venue axis. A ticker with no _VENUE row is left blank rather
    than guessed — the self-check below turns that into a loud failure."""
    exchange, currency, region = _VENUE.get(t.key, ("", "", ""))
    return replace(t, exchange=exchange, currency=currency, region=region)


CURATED_KEYS: frozenset[str] = frozenset(t.key for t in (*_HELD, *_WATCHED))

TICKERS: dict[str, Ticker] = {t.key: _with_venue(t) for t in (*_HELD, *_WATCHED)}


def _overlay() -> list[Ticker]:
    """Names added from the search bar, read once at import.

    Deliberately tolerant: a missing, empty or malformed file yields nothing and
    the curated registry stands alone. This is the only input in this module
    that no person reviewed before it was written, so it must never be able to
    take the app down — a bad overlay costs you a watchlist row, not a page.

    These tickers carry their venue inline and must NOT go through
    `_with_venue()`: that looks the key up in the hand-maintained `_VENUE` table
    and would blank exchange, currency and region for every overlay name.
    """
    path = Path(__file__).resolve().parent.parent / "data" / "watchlist_extra.json"
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    out: list[Ticker] = []
    for e in (doc.get("entries") or []):
        try:
            key = str(e["key"]).strip()
            if not key:
                continue
            out.append(Ticker(
                key=key, symbol=str(e.get("symbol") or key),
                name=str(e.get("name") or key),
                sector=str(e.get("sector") or OTHER),
                brand=str(e.get("brand") or DEFAULT_BRAND),
                con_id=None, logo=e.get("logo") or None,
                exchange=str(e.get("exchange") or ""),
                currency=str(e.get("currency") or ""),
                region=str(e.get("region") or ""),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return out


for _extra in _overlay():
    # setdefault, so a curated entry always wins if the two ever name the same key.
    TICKERS.setdefault(_extra.key, _extra)


def sector_members(sector: str) -> list[Ticker]:
    return [t for t in TICKERS.values() if t.sector == sector]


def sector_for(con_id: int | None, symbol: str) -> str:
    """Which sector a held position belongs to.

    Keyed on conId first, as regions.lookup is, because conId is stable for a
    contract across venues; symbol is the fallback for a position opened after
    this registry was last edited. Anything unrecognised is OTHER rather than
    dropped — an unmapped holding has to stay visible in the allocation totals.
    """
    if con_id is not None:
        for t in TICKERS.values():
            if t.con_id == con_id:
                return t.sector
    for t in TICKERS.values():
        if t.symbol == symbol:
            return t.sector
    return OTHER


def sector_sort_key(sector: str) -> int:
    """Fixed display and colour order, so a sector keeps its hue when another
    one drops out of the portfolio — the same contract regions.sort_key holds.

    OTHER is pushed to the last categorical slot deliberately: that slot is the
    neutral grey, and a bucket that means "no identity yet" should not wear a
    hue that reads as one.
    """
    if sector == OTHER:
        return 6
    try:
        return SECTOR_ORDER.index(sector)
    except ValueError:
        return 6


def watchlist_symbols() -> list[str]:
    """Symbols openbb must quote — everything the IB feed does not already cover."""
    seen: dict[str, None] = {}
    for t in TICKERS.values():
        if not t.owned:
            seen.setdefault(t.symbol, None)
    return list(seen)


def _mix(a: str, b: str, t: float) -> str:
    """Blend two hex colours. Used for the monogram tile's fill, edge and ink."""
    ah, bh = a.lstrip("#"), b.lstrip("#")
    parts = []
    for i in (0, 2, 4):
        av, bv = int(ah[i:i + 2], 16), int(bh[i:i + 2], 16)
        parts.append(f"{round(av + (bv - av) * t):02x}")
    return "#" + "".join(parts)


# The monogram sits on a light plate, so its ink is the brand hue darkened.
#
# The design specifies 0.18, which fails WCAG AA for 8 of
# these 35 issuers on that plate: C6L's amber lands at 2.52:1 and Apple's grey
# at 2.91:1. At 0.45 the lowest is 4.77:1 and all 35 pass, with the hue still
# plainly the issuer's. A monogram renders at 8.5px, which is exactly the size
# where contrast matters most.
MONO_INK_MIX = 0.45


def tile_colours(t: Ticker) -> dict:
    """The three colours a ticker tile is painted with.

    Extracted so `to_dict()` and `instrument._identity()` share one blend — the
    stock page paints the same tile from the instrument payload when a symbol is
    not in the registry at all, and the two must not drift.
    """
    return {
        # Light plate, per the design — marks are drawn for light ground.
        "tint": "#EDF0F3",
        "edge": "rgba(10,13,18,.16)",
        "ink": _mix(t.brand or DEFAULT_BRAND, "#0A0D12", MONO_INK_MIX),
    }


def synthetic(symbol: str, name: str = "", *, sector: str = OTHER,
              exchange: str = "", currency: str = "", region: str = "",
              brand: str = DEFAULT_BRAND) -> Ticker:
    """A Ticker for a symbol that is not in the curated registry.

    `key == symbol`, so `#stock/0700.HK` addresses it directly. The registry's
    keys are curated short forms ("700", "293"), so the two namespaces cannot
    collide. `con_id` is None, which means `owned` is False, which means every
    quantity/cost/P&L path in the app already treats it as watch-only — that
    property is doing real work here, not just describing.

    `logo` stays None so `logo_url` points the logo service at the raw symbol.
    Most will 404, and stock.js already removes an <img> that fails to load,
    falling back to the monogram.
    """
    return Ticker(
        key=symbol, symbol=symbol, name=name or symbol, sector=sector,
        brand=brand, con_id=None, logo=None,
        exchange=exchange, currency=currency, region=region,
    )


def register(t: Ticker) -> bool:
    """Add a ticker at runtime. Returns False if the key is already taken.

    Rebinds TICKERS rather than mutating it. `to_dict()` iterates
    `TICKERS.values()`, and a plain `TICKERS[key] = t` from an HTTP thread
    mid-iteration raises "dictionary changed size during iteration"; a reader
    that already grabbed the old dict finishes against it instead.
    """
    global TICKERS
    if t.key in TICKERS:
        return False
    TICKERS = {**TICKERS, t.key: t}
    return True


def to_dict() -> dict:
    """The registry as the browser needs it — tickers by key, sectors by reference.

    Tile colours are computed here rather than in the page so the blend lives in
    one place and the browser just paints what it is given.
    """
    return {
        "tickers": {
            t.key: {
                "key": t.key, "symbol": t.symbol, "name": t.name,
                "sector": t.sector, "con_id": t.con_id, "owned": t.owned,
                "mono": t.mono,
                "logo": t.logo_url,
                # Venue axis. `exchange` is the code, `exchange_name` the label
                # the stock page prints; the page should never map codes itself.
                "exchange": t.exchange,
                "exchange_name": t.exchange_display,
                "currency": t.currency,
                "region": t.region,
                **tile_colours(t),
            }
            for t in TICKERS.values()
        },
        # "Other" is suppressed while it holds nothing, so no dead pill sits in
        # the Watchlist header before the first name is added from search.
        "sectors": [
            {
                "key": name,
                "label": name,
                "members": [t.key for t in sector_members(name)],
                "owned_count": sum(1 for t in sector_members(name) if t.owned),
                "total_count": len(sector_members(name)),
            }
            for name in SECTOR_ORDER
            if name != OTHER or sector_members(name)
        ],
    }


if __name__ == "__main__":
    print(f"{len(TICKERS)} tickers across {len(SECTOR_ORDER)} sectors\n")
    for name in SECTOR_ORDER:
        members = sector_members(name)
        owned = [t.key for t in members if t.owned]
        watch = [t.key for t in members if not t.owned]
        print(f"  {name:<16} {len(owned)} owned · {len(watch)} watching")
        print(f"    owned : {' '.join(owned) or '—'}")
        print(f"    watch : {' '.join(watch) or '—'}")
    held = [t for t in TICKERS.values() if t.owned]
    print(f"\n  held total {len(held)} (must be 15)")
    print(f"  openbb quotes {len(watchlist_symbols())}: {' '.join(watchlist_symbols())}")

    # --- venue axis self-check -------------------------------------------
    # Every ticker must carry all three, every code must resolve, and region
    # must agree with regions.py wherever both know the contract.
    import regions as regions_mod

    # Scoped to the hand-maintained table. This check exists to catch a missing
    # _VENUE row when someone adds a ticker by hand; overlay names come from the
    # UI and are reported below rather than asserted, so a half-resolved one
    # cannot make `python3 universe.py` fail for an unrelated reason.
    curated = [t for t in TICKERS.values() if t.key in CURATED_KEYS]

    missing = [t.key for t in curated if not (t.exchange and t.currency and t.region)]
    assert not missing, f"tickers missing a _VENUE row: {missing}"

    unresolved = sorted({t.exchange for t in curated
                         if t.exchange.upper() not in EXCHANGE_NAMES})
    assert not unresolved, f"exchange codes with no display name: {unresolved}"

    for t in TICKERS.values():
        if not t.owned:
            continue
        hit = regions_mod.BY_CONID.get(t.con_id)
        if hit:
            assert hit[2] == t.region, (
                f"{t.key}: universe says {t.region!r}, regions.py says {hit[2]!r}")

    venues: dict[str, list[str]] = {}
    for t in TICKERS.values():
        venues.setdefault(t.exchange_display, []).append(t.key)
    print("\n  venues")
    for label in sorted(venues):
        print(f"    {label:<10} {len(venues[label]):>2}  {' '.join(sorted(venues[label]))}")

    minor = sorted(t.key for t in curated if t.currency == "GBp")
    print(f"\n  quoted in pence ({len(minor)}): {' '.join(minor)}")
    print(f"  venue axis OK — all {len(curated)} curated mapped, codes resolve, regions agree")

    extra = [t for t in TICKERS.values() if t.key not in CURATED_KEYS]
    if extra:
        incomplete = [t.key for t in extra if not (t.exchange and t.currency)]
        print(f"\n  overlay: {len(extra)} name(s) added from search"
              f" — {len(incomplete)} incomplete{': ' + ' '.join(incomplete) if incomplete else ''}")
        for t in extra:
            print(f"    {t.key:<14} {t.exchange_display or '—':<16} {t.currency or '—':<5} {t.sector}")
    else:
        print("\n  overlay: none")
