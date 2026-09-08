"""Issuer marks — the SVG each ticker tile draws, and where it comes from.

The set is TradingView's symbol-logo library: one 56×56 vector per issuer with
a full-bleed background, drawn for the small round or rounded-square tile every
brokerage app uses, so every name sits on the card the same way. It replaced
parqet's symbol-logo service on 2 Sep 2026. That service answered
`?format=svg` with a small raster PNG for 9 of the 37 names in the book (293,
C6L, HY9H, SMSN, SNDK, BIDU, PDD, IAG, LLOY) — soft at 2× — and mixed bare
glyphs with full-bleed squares, so no two tiles read alike, and the monogram
underneath showed round the edges of every one.

Three layers, cheapest first:

  1. `assets/logos/<slug>.svg` — every curated name, vendored and reviewed by
     eye. serve.py serves it beside the CSS: no third-party request on any
     page for a name in the registry.
  2. The remote file, for a slug that is not (yet) vendored.
  3. `resolve()`, for a name that reaches the app through search and so has no
     slug at all: one call to the symbol-search endpoint TradingView's own site
     uses, which reports a `logoid` per listing. The answer is cached in
     memory, the file is vendored in the background, and overlay.py persists
     the slug, so the lookup happens once per name, ever.

A ticker with no slug draws its monogram — every tile renderer already does
that when `logo` is empty — so nothing here can take a page down.

Slugs are TradingView's ids, not ours: "apple", "meta-platforms", "jd-com".
Some are shared on purpose — 3115 and IUCS both wear "ishares", the fund's
issuer — and SK hynix wears "sk-telecom" because the library files the SK
group butterfly under that name and carries no separate hynix mark.

Each mark is the trademark of its issuer, shown for identification only.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("marks")

LOGO_DIR = Path(__file__).resolve().parent.parent / "assets" / "logos"
LOCAL_PREFIX = "/assets/logos/"
REMOTE = "https://s3-symbol-logo.tradingview.com/{slug}--big.svg"
SEARCH = ("https://symbol-search.tradingview.com/symbol_search/v3/"
          "?text={text}&hl=0&lang=en&search_type=undefined&domain=production{exchange}")
TIMEOUT = 5.0
NEG_TTL = 600.0   # seconds before a lookup that found nothing is tried again

# A slug is a path segment we will write to disk, so it is validated as one.
SAFE_SLUG = re.compile(r"^[a-z0-9][a-z0-9\-.]{0,80}$")

# The search endpoint answers 403 to a request that does not carry the site's
# own Origin and Referer; the mark CDN itself needs nothing. This is the same
# read-only lookup the TradingView search box makes, once per new name.
_SEARCH_HEADERS = {
    "User-Agent": "portfolio-dashboard/1.0",
    "Origin": "https://www.tradingview.com",
    "Referer": "https://www.tradingview.com/",
}
_FETCH_HEADERS = {"User-Agent": "portfolio-dashboard/1.0"}

# yfinance venue code -> the exchange filter the search endpoint expects. The
# codes are the ones directory.py and universe._VENUE speak. An unknown code
# searches unfiltered, where only an exact symbol match is accepted.
_TV_EXCHANGE: dict[str, str] = {
    "NMS": "NASDAQ", "NGM": "NASDAQ", "NCM": "NASDAQ",
    "NYQ": "NYSE", "NYS": "NYSE", "ASE": "AMEX", "PCX": "AMEX",
    "OQX": "OTC", "OQB": "OTC", "PNK": "OTC",
    "HKG": "HKEX", "SHH": "SSE", "SHZ": "SZSE",
    "SES": "SGX",
    "LSE": "LSE", "IOB": "LSE",
    "FRA": "FWB", "GER": "XETR",
    "TOR": "TSX", "VAN": "TSXV",
    "KSC": "KRX", "KOE": "KRX",
    "JPX": "TSE", "TAI": "TWSE", "TWO": "TPEX",
    "ASX": "ASX", "NZE": "NZX",
    "PAR": "EURONEXT", "AMS": "EURONEXT", "BRU": "EURONEXT", "LIS": "EURONEXT",
    "MIL": "MIL", "MCE": "BME", "SWX": "SIX", "VIE": "VIE",
    "STO": "OMXSTO", "CPH": "OMXCOP", "HEL": "OMXHEX", "OSL": "OSL",
    "NSI": "NSE", "BSE": "BSE", "SAO": "BMFBOVESPA", "MEX": "BMV", "JNB": "JSE",
    "TLV": "TASE", "IST": "BIST", "JKT": "IDX", "SET": "SET", "KLS": "MYX",
}

# When the caller has no venue code, the symbol suffix still names one.
_SUFFIX_EXCHANGE: dict[str, str] = {
    "HK": "HKEX", "SI": "SGX", "L": "LSE", "IL": "LSE", "F": "FWB", "DE": "XETR",
    "TO": "TSX", "V": "TSXV", "KS": "KRX", "KQ": "KRX", "T": "TSE", "TW": "TWSE",
    "AX": "ASX", "NZ": "NZX", "PA": "EURONEXT", "AS": "EURONEXT", "BR": "EURONEXT",
    "LS": "EURONEXT", "MI": "MIL", "MC": "BME", "SW": "SIX", "VI": "VIE",
    "ST": "OMXSTO", "CO": "OMXCOP", "HE": "OMXHEX", "OL": "OSL", "NS": "NSE",
    "BO": "BSE", "SA": "BMFBOVESPA", "MX": "BMV", "JO": "JSE", "TA": "TASE",
    "IS": "BIST", "JK": "IDX", "BK": "SET", "KL": "MYX", "SS": "SSE", "SZ": "SZSE",
}

# Listing kinds that carry an issuer's mark. Bonds, futures, indices and
# economic series come back from the same search and must not win.
_KINDS = frozenset({"stock", "dr", "fund", "etf"})
_EM = re.compile(r"</?em>")

_cache: dict[str, tuple[str | None, float]] = {}
_lock = threading.Lock()


# ---------------------------------------------------------------- where

def is_vendored(slug: str | None) -> bool:
    return bool(slug) and bool(SAFE_SLUG.match(slug)) and (LOGO_DIR / f"{slug}.svg").is_file()


def url_for(slug: str | None) -> str:
    """The URL a tile fetches for a slug — local when vendored, else remote.

    "" for no slug, which every tile renderer takes as "draw the monogram". A
    full http(s) URL passes through untouched, so a hand-picked mark that
    lives elsewhere still works.
    """
    slug = (slug or "").strip()
    if not slug:
        return ""
    if slug.startswith(("http://", "https://")):
        return slug
    if is_vendored(slug):
        return f"{LOCAL_PREFIX}{slug}.svg"
    return REMOTE.format(slug=slug) if SAFE_SLUG.match(slug) else ""


# ---------------------------------------------------------------- which

def query_for(symbol: str, exchange: str = "") -> tuple[str, str]:
    """A yfinance symbol -> (search text, exchange filter) in TradingView's terms.

    Hong Kong lines lose Yahoo's zero padding ("0700.HK" is HKEX:700), a Korean
    code keeps its zeros ("000660"), and a US class share swaps Yahoo's dash
    for TradingView's dot ("BRK-B" is "BRK.B").
    """
    root, _, suffix = (symbol or "").strip().partition(".")
    suffix = suffix.upper()
    if suffix == "HK":
        root = root.lstrip("0") or root
    if not suffix:
        root = root.replace("-", ".")
    venue = _TV_EXCHANGE.get((exchange or "").upper()) or _SUFFIX_EXCHANGE.get(suffix, "")
    return root.upper(), venue


def pick(rows: list[dict], text: str, filtered: bool) -> str | None:
    """The logoid for `text` among search rows, or None.

    An exact symbol match of a listing kind wins outright. Only when the search
    was already narrowed to one exchange is a prefix match acceptable — an
    unfiltered search for "3115" returns a Taiwanese stock first.
    """
    text = (text or "").upper()
    fallback = None
    for s in rows or []:
        lid = str(s.get("logoid") or "")
        if not SAFE_SLUG.match(lid):
            continue                      # None, "country/US", "indices/…"
        if str(s.get("type") or "").lower() not in _KINDS:
            continue
        sym = _EM.sub("", str(s.get("symbol") or "")).upper()
        if sym == text:
            return lid
        if filtered and fallback is None and sym.startswith(text):
            fallback = lid
    return fallback


def _lookup(symbol: str, exchange: str) -> str | None:
    text, venue = query_for(symbol, exchange)
    if not text:
        return None
    url = SEARCH.format(text=urllib.parse.quote(text),
                        exchange=f"&exchange={urllib.parse.quote(venue)}" if venue else "")
    req = urllib.request.Request(url, headers=_SEARCH_HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
        doc = json.loads(response.read(1_000_000))
    return pick(doc.get("symbols") or [], text, bool(venue))


def resolve(symbol: str, exchange: str = "") -> str | None:
    """The slug for a symbol the registry does not name, or None.

    One network call per symbol per process; a miss is remembered for NEG_TTL
    so a flaky link does not retry on every click. A hit is vendored on a
    daemon thread so the tile that asked never waits for the file.
    """
    symbol = (symbol or "").strip()
    if not symbol:
        return None
    key = symbol.upper()
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and (hit[0] or now - hit[1] < NEG_TTL):
            return hit[0]
    slug = None
    try:
        slug = _lookup(symbol, exchange)
    except Exception as exc:  # network, JSON, 403 — all mean "monogram for now"
        log.info("marks: no mark resolved for %s (%s)", symbol, str(exc)[:120])
    with _lock:
        _cache[key] = (slug, now)
    if slug and not is_vendored(slug):
        threading.Thread(target=fetch, args=(slug,), name=f"mark-{slug}", daemon=True).start()
    return slug


# ---------------------------------------------------------------- vendoring

def fetch(slug: str) -> bool:
    """Copy one mark into LOGO_DIR. True if it is there afterwards.

    Written to a temp file in the same directory and renamed, so a reader
    never sees half an SVG. A body that is not a vector is refused — a raster
    would defeat the point of the whole set.
    """
    if not slug or not SAFE_SLUG.match(slug):
        return False
    dest = LOGO_DIR / f"{slug}.svg"
    if dest.is_file():
        return True
    try:
        req = urllib.request.Request(REMOTE.format(slug=slug), headers=_FETCH_HEADERS)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            body = response.read(512_000)
        if b"<svg" not in body[:1024] or b"<image" in body:
            log.info("marks: %s is not a vector mark, not vendored", slug)
            return False
        LOGO_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=LOGO_DIR, prefix=f".{slug}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(body)
            os.replace(tmp, dest)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        log.info("marks: vendored %s", dest.name)
        return True
    except Exception as exc:
        log.info("marks: could not vendor %s (%s)", slug, str(exc)[:120])
        return False


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if len(sys.argv) > 1:
        # `python marks.py 0700.HK BRK-B` — resolve and vendor, printing each step.
        for sym in sys.argv[1:]:
            slug = resolve(sym)
            print(f"{sym:<12} -> {slug or '—':<40} {url_for(slug) if slug else '(monogram)'}")
            if slug:
                fetch(slug)
    else:
        # No args: audit the registry. Every curated name should be vendored.
        import universe
        missing = [(t.key, t.logo) for t in universe.TICKERS.values() if not is_vendored(t.logo)]
        print(f"{len(universe.TICKERS)} tickers, {len(universe.TICKERS) - len(missing)} vendored under {LOGO_DIR}")
        for key, slug in missing:
            print(f"  {key:<8} {slug or '(no slug — monogram)'}")
