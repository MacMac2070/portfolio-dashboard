"""Symbol -> region lookup.

IBKR does not tag a position with an investment region, and neither the listing
venue nor the trading currency implies one:

    XDJP  lists on LSE and trades in GBP, but is Nikkei 225 exposure -> Japan
    IUCS  lists on LSE and trades in USD, but tracks S&P 500 names   -> United States
    HY9H  lists on Frankfurt and trades in EUR, but is SK hynix      -> South Korea
    SMSN  lists on LSE IOB and trades in USD, but is Samsung         -> South Korea

So the mapping is by *underlying exposure* and has to be maintained by hand.

Keyed on IBKR's conId, which is stable for a contract across venues and never
recycled. Symbols are kept only as a human-readable fallback for positions
opened after this table was last edited.

Company names confirmed against the filings in `Stock research/`.
"""

# conId -> (display symbol, company name, region)
BY_CONID: dict[int, tuple[str, str, str]] = {
    1616420:   ("293",   "Cathay Pacific Airways",                 "Hong Kong / China"),
    152791428: ("700",   "Tencent Holdings",                       "Hong Kong / China"),
    256718140: ("3115",  "iShares Core Hang Seng Index ETF",       "Hong Kong / China"),
    16520545:  ("SMSN",  "Samsung Electronics",                    "South Korea"),
    517397504: ("HY9H",  "SK hynix",                               "South Korea"),
    123279007: ("XDJP",  "Xtrackers Nikkei 225 UCITS ETF",         "Japan"),
    92216536:  ("C6L",   "Singapore Airlines",                     "Singapore"),
    92214874:  ("ES3",   "SPDR Straits Times Index ETF",           "Singapore"),
    909083:    ("HSBA",  "HSBC Holdings",                          "United Kingdom"),
    265598:    ("AAPL",  "Apple",                                  "United States"),
    3691937:   ("AMZN",  "Amazon.com",                             "United States"),
    208813719: ("GOOGL", "Alphabet",                               "United States"),
    107113386: ("META",  "Meta Platforms",                         "United States"),
    270639:    ("INTC",  "Intel",                                  "United States"),
    270617971: ("IUCS",  "iShares S&P 500 Consumer Staples ETF",   "United States"),
}

# Fallback for contracts not yet in the table above.
BY_SYMBOL: dict[str, str] = {
    "293": "Hong Kong / China", "700": "Hong Kong / China", "3115": "Hong Kong / China",
    "SMSN": "South Korea", "HY9H": "South Korea",
    "XDJP": "Japan",
    "C6L": "Singapore", "ES3": "Singapore",
    "HSBA": "United Kingdom",
    "AAPL": "United States", "AMZN": "United States", "GOOGL": "United States",
    "META": "United States", "INTC": "United States", "IUCS": "United States",
}

UNCLASSIFIED = "Unclassified"

# Fixed display order. Region colour is assigned by this order, so a region
# keeps its colour when another one drops out of the portfolio.
REGION_ORDER: list[str] = [
    "Hong Kong / China",
    "United States",
    "South Korea",
    "Japan",
    "Singapore",
    "United Kingdom",
    UNCLASSIFIED,
]


def lookup(con_id: int, symbol: str) -> tuple[str, str]:
    """Return (company_name, region) for a position.

    Unknown contracts fall through to UNCLASSIFIED rather than being dropped —
    an unmapped holding must stay visible in the UI, not silently vanish from
    the allocation totals.
    """
    hit = BY_CONID.get(con_id)
    if hit:
        return hit[1], hit[2]
    return symbol, BY_SYMBOL.get(symbol, UNCLASSIFIED)


def sort_key(region: str) -> int:
    try:
        return REGION_ORDER.index(region)
    except ValueError:
        return len(REGION_ORDER)
