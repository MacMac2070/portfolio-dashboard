"""marks.py decides which SVG a ticker tile draws and where it comes from.

The network path (`resolve`) is exercised only through its pure pieces —
`query_for` and `pick` — plus a cache/vendoring round trip against a fake
lookup, so the suite never touches TradingView."""
import pytest

import marks
import universe


# ---------------------------------------------------------------- the set

def test_every_curated_name_is_vendored():
    """A registry name must never depend on a third party at render time."""
    missing = [t.key for t in universe.TICKERS.values()
               if t.key in universe.CURATED_KEYS and not marks.is_vendored(t.logo)]
    assert missing == []


def test_curated_logo_urls_are_local():
    for t in universe.TICKERS.values():
        if t.key in universe.CURATED_KEYS:
            assert t.logo_url == f"/assets/logos/{t.logo}.svg", t.key


def test_vendored_files_are_vectors():
    """Every file in the set is an SVG with no embedded raster."""
    files = sorted(marks.LOGO_DIR.glob("*.svg"))
    assert files, "assets/logos is empty"
    for f in files:
        body = f.read_bytes()
        assert b"<svg" in body[:1024], f.name
        assert b"<image" not in body, f.name
        assert marks.SAFE_SLUG.match(f.stem), f.name


# ---------------------------------------------------------------- url_for

def test_url_for_empty_means_monogram():
    assert marks.url_for(None) == ""
    assert marks.url_for("") == ""
    assert marks.url_for("   ") == ""


def test_url_for_full_url_passes_through():
    assert marks.url_for("https://example.test/x.svg") == "https://example.test/x.svg"


def test_url_for_unvendored_slug_is_remote():
    assert marks.url_for("no-such-issuer-zz") == marks.REMOTE.format(slug="no-such-issuer-zz")


def test_url_for_refuses_a_slug_that_is_not_a_path_segment():
    assert marks.url_for("../etc/passwd") == ""
    assert marks.url_for("Apple") == ""          # slugs are lower-case by construction


# ---------------------------------------------------------------- query_for

@pytest.mark.parametrize("symbol, exchange, expected", [
    ("AAPL", "NMS", ("AAPL", "NASDAQ")),
    ("TSM", "NYQ", ("TSM", "NYSE")),
    ("0700.HK", "HKG", ("700", "HKEX")),      # Yahoo's zero padding goes
    ("0700.HK", "", ("700", "HKEX")),         # suffix alone names the venue
    ("000660.KS", "KSC", ("000660", "KRX")),  # Korean codes keep their zeros
    ("BRK-B", "NYQ", ("BRK.B", "NYSE")),      # US class share: dash -> dot
    ("SHOP.TO", "TOR", ("SHOP", "TSX")),
    ("RHHBY", "OQX", ("RHHBY", "OTC")),
    ("XYZ", "ZZZ", ("XYZ", "")),              # unknown venue: unfiltered search
])
def test_query_for(symbol, exchange, expected):
    assert marks.query_for(symbol, exchange) == expected


# ---------------------------------------------------------------- pick

ROWS = [
    {"symbol": "<em>3115</em>", "exchange": "TPEX", "type": "stock", "logoid": "trust-search-ltd"},
    {"symbol": "XS3204601317", "exchange": "VIE", "type": "bond", "logoid": None},
    {"symbol": "IPN31152N", "exchange": "FRED", "type": "economic", "logoid": "country/US"},
    {"symbol": "3115", "exchange": "HKEX", "type": "fund", "logoid": "ishares"},
]


def test_pick_prefers_the_exact_symbol_of_a_listing_kind():
    # Two exact matches ("<em>3115</em>" and "3115"); the first listing kind wins,
    # and here that is the highlighted Taiwanese stock — which is exactly why an
    # unfiltered search is only trusted for an exact hit and callers pass a venue.
    assert marks.pick(ROWS, "3115", filtered=False) == "trust-search-ltd"
    assert marks.pick(ROWS[3:], "3115", filtered=False) == "ishares"


def test_pick_skips_non_listing_kinds_and_namespaced_ids():
    assert marks.pick(ROWS[1:3], "XS3204601317", filtered=True) is None


def test_pick_prefix_fallback_only_when_filtered():
    rows = [{"symbol": "IUCSL", "type": "fund", "logoid": "ishares"}]
    assert marks.pick(rows, "IUCS", filtered=False) is None
    assert marks.pick(rows, "IUCS", filtered=True) == "ishares"


def test_pick_strips_search_highlighting():
    rows = [{"symbol": "<em>AAPL</em>", "type": "stock", "logoid": "apple"}]
    assert marks.pick(rows, "aapl", filtered=False) == "apple"


# ---------------------------------------------------------------- resolve

@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """resolve() against a fake lookup, vendoring into tmp, with a clean cache."""
    monkeypatch.setattr(marks, "LOGO_DIR", tmp_path)
    monkeypatch.setattr(marks, "_cache", {})
    calls = []

    def fake_lookup(symbol, exchange):
        calls.append((symbol, exchange))
        if symbol == "BOOM":
            raise OSError("no route to host")
        return {"NEW": "new-issuer"}.get(symbol)
    monkeypatch.setattr(marks, "_lookup", fake_lookup)
    # No background download in tests: vendoring is covered separately.
    monkeypatch.setattr(marks.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda self: None})())
    return calls


def test_resolve_caches_a_hit(isolated):
    assert marks.resolve("NEW", "NMS") == "new-issuer"
    assert marks.resolve("new") == "new-issuer"
    assert len(isolated) == 1


def test_resolve_remembers_a_miss_and_never_raises(isolated):
    assert marks.resolve("NOPE") is None
    assert marks.resolve("NOPE") is None
    assert marks.resolve("BOOM") is None
    assert [c[0] for c in isolated] == ["NOPE", "BOOM"]


def test_resolve_blank_is_none(isolated):
    assert marks.resolve("") is None
    assert isolated == []


def test_fetch_refuses_a_raster_and_a_bad_slug(monkeypatch, tmp_path):
    monkeypatch.setattr(marks, "LOGO_DIR", tmp_path)

    class Resp:
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=-1): return self.body

    monkeypatch.setattr(marks.urllib.request, "urlopen",
                        lambda req, timeout: Resp(b'<svg><image href="x.png"/></svg>'))
    assert marks.fetch("rasterised") is False
    assert not (tmp_path / "rasterised.svg").exists()
    assert marks.fetch("../escape") is False

    monkeypatch.setattr(marks.urllib.request, "urlopen",
                        lambda req, timeout: Resp(b'<!-- by TradingView --><svg width="56"/>'))
    assert marks.fetch("clean-issuer") is True
    assert (tmp_path / "clean-issuer.svg").read_bytes().startswith(b"<!-- by TradingView -->")
    assert marks.is_vendored("clean-issuer")
    assert marks.url_for("clean-issuer") == "/assets/logos/clean-issuer.svg"
